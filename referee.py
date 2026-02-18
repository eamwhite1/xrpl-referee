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

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")

load_dotenv()
app = FastAPI()

# Configuration
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
REFEREE_FEE_DROPS = 100000 

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

# --- DEEP DISCOVERY ENGINE ---
async def raw_smart_audit(task: str, work: str):
    api_key = os.getenv('GEMINI_API_KEY')
    
    async with httpx.AsyncClient() as client:
        # STEP 1: Scout for available models
        logger.info("🔍 Scouting for available models...")
        list_url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
        list_res = await client.get(list_url)
        
        # Hardcoded fallbacks in case ListModels is also restricted
        candidates = ["models/gemini-1.5-flash", "models/gemini-1.5-pro", "models/gemini-1.0-pro"]
        
        if list_res.status_code == 200:
            available = list_res.json().get('models', [])
            discovered = [m['name'] for m in available if 'generateContent' in m.get('supportedGenerationMethods', [])]
            if discovered:
                logger.info(f"✨ Found models on your account: {discovered}")
                candidates = discovered + candidates # Put discovered first
        else:
            logger.warning(f"⚠️ Could not list models (Status {list_res.status_code}). Using fallbacks.")

        # STEP 2: Loop through candidates and versions
        last_err = ""
        payload = {"contents": [{"parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence."}]}]}

        for model_path in candidates:
            for version in ["v1", "v1beta"]:
                try:
                    url = f"https://generativelanguage.googleapis.com/{version}/{model_path}:generateContent?key={api_key}"
                    logger.info(f"🚀 Trying {version} with {model_path}...")
                    
                    res = await client.post(url, json=payload, timeout=20.0)
                    if res.status_code == 200:
                        logger.info(f"✅ SUCCESS! Found working path: {version}/{model_path}")
                        return res.json()['candidates'][0]['content']['parts'][0]['text']
                    
                    last_err = f"{res.status_code}: {res.text}"
                except Exception as e:
                    last_err = str(e)
                    continue

        raise Exception(f"All 20+ discovery paths failed. Last error: {last_err}")

# --- ENDPOINTS ---
@app.get("/")
def health(): return {"status": "online"}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet: raise HTTPException(status_code=500, detail="Wallet error")
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
        logger.error(f"XRPL Error: {e}")

    if not verified: raise HTTPException(status_code=404, detail="Payment check failed.")

    try:
        verdict = await raw_smart_audit(req.task, req.work)
        return {"ai_verdict": verdict, "status": "success", "payment": f"{drops_to_xrp(str(amount_received))} XRP"}
    except Exception as e:
        return {"ai_verdict": f"Discovery Failure: {str(e)}", "status": "error"}
