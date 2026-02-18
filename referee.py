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
    if not seed:
        raise ValueError("XRPL_SEED not found in environment variables.")
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
    Scouts available models, sorts them by intelligence, and executes the audit.
    """
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing.")

    async with httpx.AsyncClient() as client:
        # 1. Ask Google what models this specific API key is authorized to use
        list_url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
        try:
            list_res = await client.get(list_url)
            if list_res.status_code == 200:
                available = list_res.json().get('models', [])
                discovered = [m['name'] for m in available if 'generateContent' in m.get('supportedGenerationMethods', [])]
            else:
                discovered = []
        except Exception as e:
            logger.warning(f"Discovery network error: {e}")
            discovered = []

        if not discovered:
            discovered = ["models/gemini-1.5-pro", "models/gemini-1.5-flash"]

        # 2. Intelligent Sorting (Pro preference + Version Number)
        def model_rank(name):
            version_match = re.findall(r'\d+\.\d+|\d+', name)
            score = float(version_match[0]) if version_match else 0.0
            if any(term in name.lower() for term in ['pro', 'ultra', 'deep', 'think']):
                score += 100 
            return score

        candidates = sorted(discovered, key=model_rank, reverse=True)
        logger.info(f"🏆 Best model selected: {candidates[0]}")

        # 3. Execution Loop
        payload = {
            "contents": [{
                "parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence summary."}]
            }]
        }

        for model_path in candidates:
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

    # 1. REPLAY PROTECTION
    if x_payment_hash in USED_HASHES:
        logger.warning(f"🚫 Replay attempt blocked: {x_payment_hash}")
        raise HTTPException(status_code=403, detail="Payment hash already used.")

    # 2. ROBUST XRPL VERIFICATION
    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=x_payment_hash))
        result = tx_res.result
        
        # Unpack nested structures (result -> tx/transaction -> Destination)
        tx_body = result.get("tx") or result.get("transaction") or result
        meta = result.get("meta") or result.get("meta_data") or tx_body.get("meta") or {}
        
        # Deep search for Destination
        dest = tx_body.get("Destination") or result.get("Destination")
        dest = str(dest).strip() if dest else ""
        
        my_addr = str(referee_wallet.address).strip()
        
        # Safe amount extraction
        raw_amt = meta.get("delivered_amount") or tx_body.get("Amount") or "0"
        delivered = int(raw_amt) if isinstance(raw_amt, str) else int(raw_amt.get("value", 0))
            
        status = meta.get("TransactionResult") or result.get("status")

        logger.info(f"🔎 VERIFYING HASH: {x_payment_hash}")
        logger.info(f"   Expected: {my_addr}")
        logger.info(f"   Found:    {dest if dest else 'NOT FOUND'}")
        logger.info(f"   Amount:   {delivered} drops | Status: {status}")

        if dest.lower() != my_addr.lower():
            raise Exception(f"Destination mismatch! Found '{dest}', expected '{my_addr}'")
        if delivered < REFEREE_FEE_DROPS:
             raise Exception(f"Insufficient funds! {delivered} < {REFEREE_FEE_DROPS}")
        if status != "tesSUCCESS":
            raise Exception(f"Tx not successful. Status: {status}")
            
    except Exception as e:
        logger.error(f"❌ XRPL VERIFICATION ERROR: {e}")
        raise HTTPException(status_code=402, detail=f"Verification Failed: {str(e)}")

    # 3. AI AUDIT
    try:
        verdict, model_used = await raw_smart_audit(req.task, req.work)
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
