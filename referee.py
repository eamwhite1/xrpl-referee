import os
import asyncio
import httpx
import logging
import sys
from typing import Set, List
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai.types import HttpOptions

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

# --- INITIALIZE AI CLIENT (STABLE V1) ---
ai_client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY"),
    http_options=HttpOptions(api_version="v1")
)

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

# --- DYNAMIC AI DISCOVERY ENGINE ---

async def get_best_available_models() -> List[str]:
    """Scouts Google for available models via v1."""
    try:
        available = await asyncio.to_thread(ai_client.models.list)
        gen_models = [m.name for m in available if "generateContent" in m.supported_generation_methods]
        
        flash = sorted([m for m in gen_models if "flash" in m.lower()], reverse=True)
        pro = sorted([m for m in gen_models if "pro" in m.lower()], reverse=True)
        
        return flash + pro if (flash + pro) else []
    except Exception as e:
        logger.warning(f"Discovery list failed: {e}")
        return []

async def smart_audit(task: str, work: str):
    """Tries every possible naming convention to bypass the 404 issue."""
    discovered = await get_best_available_models()
    
    # If discovery fails or is empty, we use every known string variant
    fallbacks = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro"]
    
    # Create a unique list of all candidates
    candidates = []
    for m in (discovered + fallbacks):
        if m not in candidates: candidates.append(m)

    last_err = ""
    for model_id in candidates:
        # We try the name as-is, and with/without the 'models/' prefix
        # This handles the inconsistent way Google's v1 vs v1beta handles IDs
        attempts = [model_id]
        if "/" in model_id:
            attempts.append(model_id.split("/")[-1])
        else:
            attempts.append(f"models/{model_id}")

        for path in attempts:
            try:
                logger.info(f"🚀 Attempting path: {path}")
                response = await asyncio.to_thread(
                    ai_client.models.generate_content,
                    model=path,
                    contents=f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence."
                )
                return response.text, path
            except Exception as e:
                last_err = str(e)
                logger.warning(f"⚠️ Failed {path}: {last_err}")
                continue
            
    raise Exception(f"Final AI Failure. Check API Key permissions in AI Studio. Error: {last_err}")

# --- MAIN ENDPOINT ---

@app.get("/")
def health():
    return {"status": "online", "address": referee_wallet.address if referee_wallet else "Error"}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet: raise HTTPException(status_code=500, detail="Wallet error")
    if not x_payment_hash: raise HTTPException(status_code=400, detail="Missing hash")

    # 🧪 TEST CHEAT: Keep hash reusable for now
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

    if not verified:
        raise HTTPException(status_code=404, detail="Payment check failed.")

    # Execute Adaptive Audit
    try:
        verdict, final_model = await smart_audit(req.task, req.work)
        return {
            "ai_verdict": verdict,
            "model_used": final_model,
            "status": "success",
            "payment": f"{drops_to_xrp(str(amount_received))} XRP"
        }
    except Exception as e:
        return {"ai_verdict": f"AI Path Failure: {str(e)}", "status": "error"}
