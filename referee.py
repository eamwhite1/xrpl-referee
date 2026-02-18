import os
import re
import httpx
import logging
import sys
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx
from xrpl.utils import drops_to_xrp
from xrpl.core.addresscodec import decode_seed

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")

load_dotenv()
app = FastAPI()

# --- CONFIGURATION & SECURITY ---
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
REFEREE_FEE_DROPS = 100000  # 0.1 XRP
USED_HASHES = set()         # Prevents double-spending of transaction hashes

# Initialize Wallet
try:
    seed = os.getenv("XRPL_SEED")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    print(f"\n🚀 AGENT ACTIVE: Monitoring {referee_wallet.address}\n")
except Exception as e:
    logger.error(f"STARTUP ERROR: Wallet configuration failed: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str

# --- THE INTELLIGENT DISCOVERY ENGINE ---
async def raw_smart_audit(task: str, work: str):
    """
    Scouts available models, sorts them by intelligence (Pro > Flash) 
    and version number (e.g., 7.4 > 3.0), and executes the audit.
    """
    api_key = os.getenv('GEMINI_API_KEY')
    
    async with httpx.AsyncClient() as client:
        # 1. Ask Google what models this specific API key is authorized to use
        list_url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
        try:
            list_res = await client.get(list_url)
            if list_res.status_code != 200:
                raise Exception(f"Model listing failed: {list_res.text}")
            
            available = list_res.json().get('models', [])
            discovered = [m['name'] for m in available if 'generateContent' in m.get('supportedGenerationMethods', [])]
        except Exception as e:
            logger.warning(f"Discovery failed, using standard fallbacks. Error: {e}")
            discovered = ["models/gemini-1.5-flash", "models/gemini-1.5-pro"]

        # 2. Intelligent Sorting (Pro preference + Version Number extraction)
        def model_rank(name):
            # Extract version (e.g., 'gemini-7.4' -> 7.4)
            version_match = re.findall(r'\d+\.\d+|\d+', name)
            score = float(version_match[0]) if version_match else 0.0
            # Intelligence multiplier: Always prefer 'Pro' or 'Ultra' over 'Flash'
            if any(term in name.lower() for term in ['pro', 'ultra', 'deep']):
                score += 100 
            return score

        candidates = sorted(discovered, key=model_rank, reverse=True)
        logger.info(f"🏆 Best model found: {candidates[0] if candidates else 'None'}")

        # 3. Execution Loop (Tries the best model, falls back if busy)
        payload = {
            "contents": [{
                "parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence summary."}]
            }]
        }

        for model_path in candidates:
            # Normalize path (ensure it looks like /v1/models/...)
            clean_id = model_path.split('/')[-1]
            for version in ["v1", "v1beta"]:
                try:
                    url = f"https://generativelanguage.googleapis.com/{version}/models/{clean_id}:generateContent?key={api_key}"
                    res = await client.post(url, json=payload, timeout=30.0)
                    if res.status_code == 200:
                        return res.json()['candidates'][0]['content']['parts'][0]['text'], clean_id
                except:
                    continue

        raise Exception("AI Gateway Failure: All discovery paths exhausted.")

# --- MAIN ENDPOINT ---
@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet: 
        raise HTTPException(status_code=500, detail="Referee wallet not initialized.")
    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing x-payment-hash header.")

    # 1. SECURITY: Check if hash has already been used
    if x_payment_hash in USED_HASHES:
        logger.warning(f"🚫 Replay attempt blocked for hash: {x_payment_hash}")
        raise HTTPException(status_code=403, detail="Payment hash already used.")

    # 2. XRPL VERIFICATION: Check the ledger for 0.1 XRP
    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=x_payment_hash))
        tx_data = tx_res.result
        meta = tx_data.get("meta") or tx_data.get("meta_data") or {}

        # Validate destination, amount, and success status
        is_dest = str(tx_data.get("Destination", "")).lower() == str(referee_wallet.address).lower()
        
        # Handle different XRP amount formats
        raw_amt = meta.get("delivered_amount") or tx_data.get("Amount", "0")
        delivered = int(raw_amt) if isinstance(raw_amt, str) else int(raw_amt.get("value", 0))

        if not is_dest or delivered < REFEREE_FEE_DROPS or meta.get("TransactionResult") != "tesSUCCESS":
            raise Exception("Payment verification failed (wrong address, amount, or status).")
            
    except Exception as e:
        logger.error(f"XRPL Error: {e}")
        raise HTTPException(status_code=402, detail=f"XRP Verification Error: {str(e)}")

    # 3. AI AUDIT: Perform the intelligence-ranked audit
    try:
        verdict, model_used = await raw_smart_audit(req.task, req.work)
        
        # 4. COMMIT: Only burn the hash if the audit was successful
        USED_HASHES.add(x_payment_hash)
        
        return {
            "ai_verdict": verdict,
            "model_used": model_used,
            "status": "success",
            "payment_verified": f"{drops_to_xrp(str(delivered))} XRP"
        }
    except Exception as e:
        logger.error(f"Audit Failure: {e}")
        return {"ai_verdict": f"Audit Error: {str(e)}", "status": "error"}

@app.get("/")
def health():
    return {"status": "online", "referee_address": referee_wallet.address if referee_wallet else "Error"}
