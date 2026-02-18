import os
import asyncio
import httpx
import logging
import sys
from typing import Set
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx
from xrpl.utils import drops_to_xrp
from xrpl.core.addresscodec import decode_seed

# --- FORCE LOGS TO BE VISIBLE ON RENDER ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RefereeBot")

load_dotenv()
app = FastAPI()

# Configuration
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REFEREE_FEE_DROPS = 100000  # 0.1 XRP

# Initialize Wallet with Debug Logging
try:
    seed = os.getenv("XRPL_SEED")
    if not seed:
        raise ValueError("XRPL_SEED is missing from Environment Variables!")
    
    # Auto-detect algorithm (ED25519 vs SECP256K1)
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    
    # These will appear in your Render "Logs" tab
    print(f"\n🚀 STARTUP SUCCESS")
    print(f"🤖 BOT IS MONITORING: {referee_wallet.address}")
    print(f"📡 CONNECTED TO: {XRPL_URL}\n")
    logger.info(f"Wallet Loaded: {referee_wallet.address}")
except Exception as e:
    print(f"\n❌ STARTUP ERROR: {str(e)}\n")
    logger.error(f"Failed to initialize wallet: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str

# --- UTILS ---
async def notify_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except Exception as e:
        logger.error(f"Telegram failed: {e}")

# --- API ENDPOINTS ---

@app.get("/")
def health_check():
    return {
        "status": "online",
        "bot_address": referee_wallet.address if referee_wallet else "NOT_CONFIGURED"
    }

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet:
        raise HTTPException(status_code=500, detail="Bot wallet not configured.")
    if not x_payment_hash:
        raise HTTPException(status_code=400, detail="Missing x-payment-hash header.")

    client = AsyncJsonRpcClient(XRPL_URL)
    verified = False
    customer_address = ""
    amount_received = 0

    logger.info(f"🔎 Validating Transaction: {x_payment_hash}")

    try:
        # Loop to handle ledger propagation
        for attempt in range(5):
            response = await client.request(Tx(transaction=x_payment_hash))
            if not response.is_successful():
                logger.warning(f"Attempt {attempt+1}: Transaction not found yet.")
                await asyncio.sleep(2)
                continue

            res = response.result
            # Handle nested transaction data (tx or tx_json)
            data = res.get("tx") or res.get("tx_json") or res
            
            # CASE-INSENSITIVE ADDRESS COMPARISON
            ledger_dest = str(data.get("Destination", "")).lower()
            bot_dest = str(referee_wallet.address).lower()

            logger.info(f"Comparing Dest: {ledger_dest} == {bot_dest}")

            if ledger_dest == bot_dest:
                # Check Metadata for Success
                meta = res.get("meta") or res.get("meta_data") or {}
                tx_result = meta.get("TransactionResult", "tesSUCCESS")

                if tx_result == "tesSUCCESS":
                    # Parse Amount
                    raw_amt = data.get("Amount")
                    amount_received = int(raw_amt) if isinstance(raw_amt, (str, int)) else 0
                    
                    if amount_received >= REFEREE_FEE_DROPS:
                        verified = True
                        customer_address = data.get("Account")
                        logger.info(f"✅ Verified {drops_to_xrp(str(amount_received))} XRP from {customer_address}")
                        break
            
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"XRPL Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Ledger Error.")

    if not verified:
        raise HTTPException(status_code=404, detail="Payment verification failed. Check address, amount, or hash.")

    # AI AUDIT SECTION
    await notify_telegram(f"💸 *Payment Confirmed!*\nReceived `{drops_to_xrp(str(amount_received))} XRP`.\nRunning AI Audit...")
    
    try:
        prompt = f"AUDIT TASK: {req.task}\nCODE TO REVIEW: {req.work}\n\nProvide a 1-2 sentence verdict (APPROVED/REJECTED)."
        ai_response = await asyncio.to_thread(ai_client.models.generate_content, model="gemini-1.5-flash", contents=prompt)
        verdict = ai_response.text
        return {"ai_verdict": verdict, "status": "success"}
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return {"ai_verdict": "AI Analysis failed, but payment was received.", "status": "error"}
