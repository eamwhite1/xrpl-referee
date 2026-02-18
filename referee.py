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

# --- PERSISTENT STORAGE ---
HASH_FILE = "used_hashes.txt"

def load_used_hashes() -> Set[str]:
    if os.path.exists(HASH_FILE):
        try:
            with open(HASH_FILE, "r") as f:
                return set(line.strip() for line in f if line.strip())
        except: return set()
    return set()

def save_hash_to_disk(tx_hash: str):
    try:
        with open(HASH_FILE, "a") as f:
            f.write(f"{tx_hash}\n")
    except: pass

USED_HASHES = load_used_hashes()

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
    global USED_HASHES
    if not referee_wallet:
        raise HTTPException(status_code=500, detail="Wallet not configured")
    if not x_payment_hash:
        raise HTTPException(status_code=400, detail="Missing hash")
    
    # REPLAY PROTECTION (Option 2: Active)
    if x_payment_hash in USED_HASHES:
        raise HTTPException(status_code=403, detail="This payment hash has already been used for an audit.")

    client = AsyncJsonRpcClient(XRPL_URL)
    verified = False
    amount_received = 0

    logger.info(f"🔎 Validating Hash: {x_payment_hash}")

    try:
        response = await client.request(Tx(transaction=x_payment_hash))
        res = response.result
        
        data = res.get("tx") or res.get("tx_json") or res
        meta = res.get("meta") or res.get("meta_data") or {}

        ledger_dest = str(data.get("Destination", "")).lower()
        bot_dest = str(referee_wallet.address).lower()

        if ledger_dest == bot_dest:
            raw_amt = meta.get("delivered_amount") or data.get("Amount")
            
            if isinstance(raw_amt, (str, int)):
                amount_received = int(raw_amt)
            elif isinstance(raw_amt, dict):
                amount_received = int(raw_amt.get("value", 0))

            logger.info(f"💰 Amount verified: {amount_received} drops")

            if amount_received >= REFEREE_FEE_DROPS:
                if meta.get("TransactionResult") == "tesSUCCESS" or res.get("validated"):
                    verified = True
                    logger.info("✅ Payment check PASSED.")

    except Exception as e:
        logger.error(f"Verification Logic Error: {e}")

    if not verified:
        raise HTTPException(status_code=404, detail="Payment check failed.")

    # Save hash to prevent reuse
    USED_HASHES.add(x_payment_hash)
    save_hash_to_disk(x_payment_hash)

    # --- AI AUDIT SECTION ---
    try:
        logger.info("Calling Gemini AI...")
        # Note the "models/" prefix below for the 2026 stable API path
        ai_res = await asyncio.to_thread(
            ai_client.models.generate_content, 
            model="models/gemini-1.5-flash", 
            contents=f"TASK: {req.task}\nCODE: {req.work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence."
        )
        
        verdict = ai_res.text
        await notify_telegram(f"✅ **Audit Complete!** Paid {drops_to_xrp(str(amount_received))} XRP.")
        return {"ai_verdict": verdict, "status": "success"}
        
    except Exception as e:
        logger.error(f"AI Final Error: {e}")
        return {"ai_verdict": f"AI Error: {str(e)}", "status": "error"}
