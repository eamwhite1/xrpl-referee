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
from google.genai.types import HttpOptions  # Crucial for v1 forcing

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

# --- INITIALIZE AI CLIENT (FORCING STABLE V1) ---
# This stops the 404s caused by the SDK defaulting to v1beta
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
    """Scouts the Google API for all generation-capable models via Stable v1."""
    try:
        available = await asyncio.to_thread(ai_client.models.list)
        
        gen_models = [
            m.name for m in available 
            if "generateContent" in m.supported_generation_methods
        ]
        
        # Sort so that latest Flash models (2.0/1.5) are first, then Pro models
        flash = sorted([m for m in gen_models if "flash" in m.lower()], reverse=True)
        pro = sorted([m for m in gen_models if "pro" in m.lower()], reverse=True)
        
        # Return discovered list or a safe fallback if list is empty
        final_list = flash + pro
        return final_list if final_list else ["models/gemini-1.5-flash"]
    except Exception as e:
        logger.warning(f"Discovery failed, using hardcoded fallback: {e}")
        return ["models/gemini-1.5-flash"]

async def smart_audit(task: str, work: str):
    """Attempts audit by cycling through discovered models until success."""
    models = await get_best_available_models()
    logger.info(f"Discovered models on v1: {models}")

    last_err = ""
    for model_name in models:
        try:
            logger.info(f"🚀 Attempting audit with {model_name}...")
            response = await asyncio.to_thread(
                ai_client.models.generate_content,
                model=model_name,
                contents=f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence reason."
            )
            return response.text, model_name
        except Exception as e:
            last_err = str(e)
            logger.warning(f"⚠️ {model_name} failed: {last_err}")
            continue
            
    raise Exception(f"All AI discovery paths failed. Latest error: {last_err}")

# --- MAIN ENDPOINT ---

@app.get("/")
def health():
    return {"status": "online", "bot_address": referee_wallet.address if referee_wallet else "Error"}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet: raise HTTPException(status_code=500, detail="Wallet config error")
    if not x_payment_hash: raise HTTPException(status_code=400, detail="Missing hash")

    # 🧪 TEST CHEAT: Reuse enabled for testing
    # if x_payment_hash in getattr(app, 'used_hashes', set()):
    #     raise HTTPException(status_code=403, detail="Hash used")

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
                logger.info(f"✅ Payment of {amount_received} drops verified.")
    except Exception as e:
        logger.error(f"XRPL Check Error: {e}")

    if not verified:
        raise HTTPException(status_code=404, detail="Payment verification failed.")

    # Execute Dynamic Audit
    try:
        verdict, final_model = await smart_audit(req.task, req.work)
        
        # Internal tracking (re-enable hash security later)
        if not hasattr(app, 'used_hashes'): app.used_hashes = set()
        
        return {
            "ai_verdict": verdict,
            "model_used": final_model,
            "status": "success",
            "payment_confirmed": f"{drops_to_xrp(str(amount_received))} XRP"
        }
    except Exception as e:
        return {"ai_verdict": f"AI Error: {str(e)}", "status": "error"}
