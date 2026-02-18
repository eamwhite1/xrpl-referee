import os
import asyncio
import httpx
from decimal import Decimal
from typing import Set
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx, AccountInfo
from xrpl.models.transactions import Payment
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.utils import drops_to_xrp, xrp_to_drops
from xrpl.core.addresscodec import decode_seed

# --- INITIALIZATION ---
load_dotenv()
app = FastAPI()

# Configuration
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
XAMAN_ADDRESS = os.getenv("XAMAN_ADDRESS")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REFEREE_FEE_DROPS = 100000 # 0.1 XRP

# --- STORAGE ---
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
MAINTENANCE_MODE = False

def load_referee_wallet():
    seed = os.getenv("XRPL_SEED")
    if not seed: raise ValueError("XRPL_SEED MISSING")
    _, algo = decode_seed(seed)
    return Wallet.from_seed(seed, algorithm=algo)

referee_wallet = load_referee_wallet()

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

# --- API ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "online", "referee_address": referee_wallet.address}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    global MAINTENANCE_MODE, USED_HASHES
    
    if MAINTENANCE_MODE: raise HTTPException(status_code=503, detail="Maintenance")
    if not x_payment_hash: raise HTTPException(status_code=400, detail="Missing Hash")
    if x_payment_hash in USED_HASHES: raise HTTPException(status_code=403, detail="Payment already used")

    verified, customer_address, amount_received = False, "", 0
    client = AsyncJsonRpcClient(XRPL_URL)
    
    try:
        for _ in range(5):
            tx_res = await client.request(Tx(transaction=x_payment_hash))
            res = tx_res.result
            
            # UNIVERSAL FINDER: Look in main result, 'tx', or 'tx_json'
            data = res.get("tx") or res.get("tx_json") or res
            
            # Destination Check
            if data.get("Destination") == referee_wallet.address:
                # Meta check for success
                meta = res.get("meta") or res.get("meta_data")
                status = meta.get("TransactionResult") if meta else "tesSUCCESS"

                if status == "tesSUCCESS":
                    raw_amt = data.get("Amount")
                    # XRP is always a string of drops or an int
                    amount_received = int(raw_amt) if isinstance(raw_amt, (str, int)) else 0
                    
                    if amount_received >= REFEREE_FEE_DROPS:
                        verified = True
                        customer_address = data.get("Account")
                        break
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Error: {e}")

    if not verified:
        raise HTTPException(status_code=404, detail="Payment check failed. Check address/amount.")

    USED_HASHES.add(x_payment_hash)
    save_hash_to_disk(x_payment_hash)
    
    await notify_telegram(f"✅ **Payment Verified!** Received `{drops_to_xrp(str(amount_received))} XRP`.")

    # AI AUDIT
    try:
        prompt = f"AUDIT TASK: {req.task}\nSUBMITTED WORK: {req.work}\n\nVerdict: APPROVED or REJECTED. Provide 1 sentence reason."
        ai_res = await asyncio.to_thread(ai_client.models.generate_content, model="gemini-1.5-flash", contents=prompt)
        return {"ai_verdict": ai_res.text, "status": "success"}
    except Exception as e:
        return {"ai_verdict": "AI Timeout. Contact Support.", "status": "error"}
