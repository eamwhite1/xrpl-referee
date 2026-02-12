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

# Configuration from Environment (Set these in Render Dashboard)
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me")
XAMAN_ADDRESS = os.getenv("XAMAN_ADDRESS")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- PERSISTENT STORAGE LOGIC ---
HASH_FILE = "used_hashes.txt"

def load_used_hashes() -> Set[str]:
    """Reads processed payment hashes from disk so we don't forget after a restart."""
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_hash_to_disk(tx_hash: str):
    """Appends a new hash to our permanent list."""
    with open(HASH_FILE, "a") as f:
        f.write(f"{tx_hash}\n")

# Global state
USED_HASHES = load_used_hashes()
AI_FAIL_COUNT = 0
MAX_FAILS = 3
MAINTENANCE_MODE = False

def load_referee_wallet():
    seed = os.getenv("XRPL_SEED")
    _, algo = decode_seed(seed)
    return Wallet.from_seed(seed, algorithm=algo)

referee_wallet = load_referee_wallet()

class AuditRequest(BaseModel):
    task: str
    work: str

# --- NOTIFICATIONS & UTILS ---

async def notify_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"Telegram Error: {e}")

async def sweep_profits():
    """Auto-moves XRP above 10.0 to your Xaman wallet."""
    if not XAMAN_ADDRESS: return
    async with AsyncJsonRpcClient(XRPL_URL) as client:
        try:
            acct_info = await client.request(AccountInfo(account=referee_wallet.address, ledger_index="validated"))
            balance_xrp = float(drops_to_xrp(acct_info.result["account_data"]["Balance"]))
            
            threshold = float(os.getenv("SWEEP_THRESHOLD", 20.0))
            retention = 10.0 # Standard reserve to keep the wallet active
            
            if balance_xrp >= threshold:
                amount_to_send = balance_xrp - retention
                payment = Payment(
                    account=referee_wallet.address,
                    destination=XAMAN_ADDRESS,
                    amount=xrp_to_drops(Decimal(str(round(amount_to_send, 4))))
                )
                await submit_and_wait(payment, client, referee_wallet)
                await notify_telegram(f"🚀 **Profit Sweep!** Sent `{amount_to_send} XRP` to your Xaman wallet.")
        except Exception as e:
            print(f"Sweep Failed: {e}")

# --- API ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "online", "referee_address": referee_wallet.address}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    global AI_FAIL_COUNT, MAINTENANCE_MODE, USED_HASHES
    
    if MAINTENANCE_MODE: raise HTTPException(status_code=503, detail="System in maintenance mode")
    if not x_payment_hash: raise HTTPException(status_code=400, detail="Missing payment hash")
    if x_payment_hash in USED_HASHES: raise HTTPException(status_code=403, detail="Payment already used")

    # 1. VERIFY PAYMENT
    verified, customer_address, amount_drops = False, "", ""
    async with AsyncJsonRpcClient(XRPL_URL) as client:
        for _ in range(10): # Try for ~20 seconds
            try:
                tx = await client.request(Tx(transaction=x_payment_hash))
                if tx.result.get("validated") and tx.result.get("meta").get("TransactionResult") == "tesSUCCESS":
                    if tx.result.get("Destination") == referee_wallet.address:
                        verified = True
                        customer_address = tx.result.get("Account")
                        amount_drops = tx.result.get("Amount")
                        break
            except: pass
            await asyncio.sleep(2)

    if not verified:
        raise HTTPException(status_code=404, detail="Payment not found or incorrect destination")

    # Permanently record the hash
    USED_HASHES.add(x_payment_hash)
    save_hash_to_disk(x_payment_hash)
    
    await notify_telegram(f"💸 **Incoming Payment:** `{drops_to_xrp(amount_drops)} XRP` from `{customer_address[:8]}...`")

    # 2. AI AUDIT
    try:
        prompt = f"AUDIT TASK: {req.task}\nSUBMITTED WORK: {req.work}\n\nVerdict: APPROVED or REJECTED. Provide 1 sentence reason."
        res = await asyncio.to_thread(ai_client.models.generate_content, model="gemini-1.5-flash", contents=prompt)
        verdict = res.text
        AI_FAIL_COUNT = 0 
    except Exception as e:
        AI_FAIL_COUNT += 1
        if AI_FAIL_COUNT >= MAX_FAILS: MAINTENANCE_MODE = True
        return {"ai_verdict": "AI Service Error. Please try later.", "status": "error"}

    # 3. BACKGROUND TASKS
    asyncio.create_task(sweep_profits())

    return {"ai_verdict": verdict, "status": "success"}