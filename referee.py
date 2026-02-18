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
REFEREE_FEE_DROPS = 100000  # 0.1 XRP

# Initialize Wallet
try:
    seed = os.getenv("XRPL_SEED")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    print(f"\n🚀 STARTUP SUCCESS: Bot is monitoring {referee_wallet.address}\n")
except Exception as e:
    logger.error(f"STARTUP ERROR: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str

# --- DYNAMIC AI ENGINE ---

async def get_best_available_models() -> List[str]:
    """Scouts the Google API for all generation-capable models, prioritizing Flash."""
    try:
        # Query Google for what is currently available for your API key
        available = await asyncio.to_thread(ai_client.models.list)
        
        # Filter for models that support generating content
        # We look for 'flash' first (fast/cheap), then 'pro' (smart/expensive)
        gen_models = [
            m.name for m in available 
            if "generateContent" in m.supported_generation_methods
        ]
        
        flash = sorted([m for m in gen_models if "flash" in m.lower()], reverse=True)
        pro = sorted([m for m in gen_models if "pro" in m.lower()], reverse=True)
        
        return flash + pro
    except Exception as e:
        logger.warning(f"Discovery failed, using hardcoded fallbacks: {e}")
        return ["models/gemini-1.5-flash", "models/gemini-1.0-pro"]

async def smart_audit(task: str, work: str):
    """Discovers models and attempts the audit, falling back automatically on error."""
    models = await get_best_available_models()
    logger.info(f"Discovered models in order of preference: {models}")

    last_err = ""
    for model_name in models:
        try:
            logger.info(f"🚀 Auditing with {model_name}...")
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
            
    raise Exception(f"All AI discovery paths failed. Last error: {last_err}")

# --- MAIN ENDPOINT ---

@app.get("/")
def health():
    return {"status": "online", "address": referee_wallet.address if referee_wallet else "Error"}

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None)):
    if not referee_wallet: raise HTTPException(status_code=500, detail="Wallet config error")
    if not x_payment_hash: raise HTTPException(status_code=400, detail="Missing hash")

    # 🧪 TEST CHEAT: Re-using your hash is allowed for now!
    # To go live, uncomment the lines below:
    # if x_payment_hash in getattr(app, 'used_hashes', set()):
    #     raise HTTPException(status_code=403, detail="Hash already used")

    client = AsyncJsonRpcClient(XRPL_URL)
    verified = False
    amount_received = 0

    try:
        # Verify Payment on XRPL
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
        
        # Track hash only after successful audit
        if not hasattr(app, 'used_hashes'): app.used_hashes = set()
        # app.used_hashes.add(x_payment_hash) # Uncomment to prevent reuse
        
        return {
            "ai_verdict": verdict,
            "model_used": final_model,
            "status": "success",
            "payment_confirmed": f"{drops_to_xrp(str(amount_received))} XRP"
        }
    except Exception as e:
        return {"ai_verdict": f"AI Error: {str(e)}", "status": "error"}
