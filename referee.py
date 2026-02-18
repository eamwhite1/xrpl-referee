import os
import asyncio
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

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")

load_dotenv()
app = FastAPI()

# Configuration
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
REFEREE_FEE_DROPS = 100000 

# Initialize Wallet
try:
    seed = os.getenv("XRPL_SEED")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    print(f"\n🚀 STARTUP SUCCESS: Monitoring {referee_wallet.address}\n")
except Exception as e:
    logger.error(f"STARTUP ERROR: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str

# --- THE RAW HTTP AUDIT ENGINE ---
async def raw_smart_audit(task: str, work: str):
    """Direct HTTP call to Google API - No library needed."""
    api_key = os.getenv('GEMINI_API_KEY')
    # Core production endpoint
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash-latest:generateContent?key={api_key}"
    
    payload = {
        "contents": [{
            "parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence."}]
        }]
    }

    async with httpx.AsyncClient() as client:
        logger.info("📡 Sending direct HTTP request to Gemini...")
        try:
            response = await client.post(url, json=payload, timeout=30.0)
            
            if response.status_code == 200:
                data = response.json()
                return data['candidates'][0]['content']['parts'][0]['text']
            
            # Fallback to v1beta if v1 fails
            elif response.status_code == 404:
                logger.warning("v1 404'd. Trying v1beta fallback...")
                alt_url = url.replace("/v1/", "/v1beta/")
                alt_res = await client.post(alt_url, json=payload, timeout=30.0)
                if alt_res.status_code == 200:
                    return alt_res.json()['candidates'][0]['content']['parts'][0]['text']
                
            raise Exception(f"Google API Error {response.status_code}: {response.text}")
            
        except Exception as e:
            logger.error(f"Network Error: {str(e)}")
            raise e

# --- ENDPOINTS ---
@app.get("/")
def health(): return {"status": "online", "address": referee_wallet.address if referee_wallet else "Error"}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet: raise HTTPException(status_code=500, detail="Wallet config error")
    if not x_payment_hash: raise HTTPException(status_code=400, detail="Missing hash")

    client = AsyncJsonRpcClient(XRPL_URL)
    verified = False
    amount_received = 0

    try:
        tx_res = await client.request(Tx(transaction=x_payment_hash))
        res = tx_res.result
        data = res.get("tx") or res.get("tx_json") or res
        meta = res.get("meta") or res.get("meta_data") or {}

        if str(data.get("Destination", "")).lower() == str(referee_wallet.address).lower():
            raw_amt = meta.get("delivered_amount") or data.get("Amount")
            amount_received = int(raw_amt) if isinstance(raw_amt, (str, int)) else int(raw_amt.get("value", 0))
            if amount_received >= REFEREE_FEE_DROPS and meta.get("TransactionResult") == "tesSUCCESS":
                verified = True
    except Exception as e:
        logger.error(f"XRPL Verify Error: {e}")

    if not verified:
        raise HTTPException(status_code=404, detail="Payment check failed.")

    # Execute Audit
    try:
        verdict = await raw_smart_audit(req.task, req.work)
        return {
            "ai_verdict": verdict,
            "status": "success",
            "payment": f"{drops_to_xrp(str(amount_received))} XRP"
        }
    except Exception as e:
        return {"ai_verdict": f"API Connection Error: {str(e)}", "status": "error"}

