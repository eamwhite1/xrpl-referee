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

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
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

# Initialize Wallet
try:
    seed = os.getenv("XRPL_SEED")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    print(f"\n🚀 STARTUP SUCCESS")
    print(f"🤖 BOT IS MONITORING: {referee_wallet.address}\n")
except Exception as e:
    logger.error(f"STARTUP ERROR: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str

async def notify_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except: pass

@app.get("/")
def health_check():
    addr = referee_wallet.address if referee_wallet else "Not Configured"
    return {"status": "online", "bot_address": addr}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet:
        raise HTTPException(status_code=500, detail="Wallet not configured")
    if not x_payment_hash:
        raise HTTPException(status_code=400, detail="Missing hash")

    client = AsyncJsonRpcClient(XRPL_URL)
    verified = False
    amount_received = 0

    logger.info(f"🔎 Validating Hash: {x_payment_hash}")

    try:
        response = await client.request(Tx(transaction=x_payment_hash))
        res = response.result
        
        # Look in all possible locations for transaction data
        data = res.get("tx") or res.get("tx_json") or res
        meta = res.get("meta") or res.get("meta_data") or {}

        # 1. Address Check
        ledger_dest = str(data.get("Destination", "")).lower()
        bot_dest = str(referee_wallet.address).lower()
        logger.info(f"Comparing Dest: {ledger_dest} == {bot_dest}")

        if ledger_dest == bot_dest:
            # 2. Amount Check - Priority: meta.delivered_amount -> data.Amount
            raw_amt = meta.get("delivered_amount") or data.get("Amount")
            
            if isinstance(raw_amt, (str, int)):
                amount_received = int(raw_amt)
            elif isinstance(raw_amt, dict):
                amount_received = int(raw_amt.get("value", 0))

            logger.info(f"💰 Amount verified: {amount_received} drops")

            # 3. Final Verification
            if amount_received >= REFEREE_FEE_DROPS:
                if meta.get("TransactionResult") == "tesSUCCESS" or res.get("validated"):
                    verified = True
                    logger.info("✅ Payment check PASSED.")

    except Exception as e:
        logger.error(f"Verification Logic Error: {e}")

    if not verified:
        raise HTTPException(status_code=404, detail="Payment check failed.")

    # --- AI AUDIT ---
    await notify_telegram(f"💸 **Audit Paid!** Received `{drops_to_xrp(str(amount_received))} XRP`.")
    
    try:
        prompt = f"AUDIT TASK: {req.task}\nSUBMITTED WORK: {req.work}\n\nVerdict: APPROVED or REJECTED. 1-2 sentence reason."
        ai_res = await asyncio.to_thread(ai_client.models.generate_content, model="gemini-1.5-flash", contents=prompt)
        return {"ai_verdict": ai_res.text, "status": "success"}
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return {"ai_verdict": "AI Audit failed, but payment received.", "status": "error"}
