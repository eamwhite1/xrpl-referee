import os
import re
import httpx
import logging
import sys
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Body
from pydantic import BaseModel
from dotenv import load_dotenv

# XRPL Imports
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx
from xrpl.utils import drops_to_xrp
from xrpl.core.addresscodec import decode_seed

# Database Imports
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- 1. INITIAL SETUP & LOGGING ---
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")
load_dotenv()

app = FastAPI(title="XRPL Referee Pro")

# --- 2. DATABASE CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL")
# Fix for Render/Heroku postgres dialect
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class PaymentLog(Base):
    __tablename__ = "payment_logs"
    id = Column(Integer, primary_key=True, index=True)
    payment_hash = Column(String, unique=True, index=True, nullable=False)
    sender = Column(String)
    amount_xrp = Column(Float)
    task_summary = Column(String)
    ai_verdict = Column(Text)
    model_used = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

# Initialize tables
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 3. XRPL WALLET CONFIG ---
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
REFEREE_FEE_DROPS = 100000  # 0.1 XRP

try:
    seed = os.getenv("XRPL_SEED")
    if not seed:
        raise ValueError("XRPL_SEED not found in environment variables.")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    logger.info(f"🚀 AGENT ACTIVE: Monitoring {referee_wallet.address}")
except Exception as e:
    logger.error(f"STARTUP ERROR: Wallet configuration failed: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str

# --- 4. INTELLIGENT DISCOVERY ENGINE ---
async def raw_smart_audit(task: str, work: str):
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing.")

    async with httpx.AsyncClient() as client:
        # 1. Discovery
        list_url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
        try:
            list_res = await client.get(list_url)
            discovered = [m['name'] for m in list_res.json().get('models', []) 
                          if 'generateContent' in m.get('supportedGenerationMethods', [])] if list_res.status_code == 200 else []
        except Exception:
            discovered = []

        if not discovered:
            discovered = ["models/gemini-1.5-pro", "models/gemini-1.5-flash"]

        # 2. Ranking
        def model_rank(name):
            version_match = re.findall(r'\d+\.\d+|\d+', name)
            score = float(version_match[0]) if version_match else 0.0
            if any(term in name.lower() for term in ['pro', 'ultra', 'deep', 'think']):
                score += 100 
            return score

        candidates = sorted(discovered, key=model_rank, reverse=True)
        
        # 3. Execution
        payload = {
            "contents": [{"parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence summary."}]}]
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
        raise Exception("AI Gateway Failure")

# --- 5. MAIN ENDPOINT ---
@app.post("/evaluate")
async def evaluate_work(
    req: AuditRequest, 
    x_payment_hash: str = Header(None), 
    db: Session = Depends(get_db)
):
    if not referee_wallet: 
        raise HTTPException(status_code=500, detail="Referee wallet not initialized.")
    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing x-payment-hash header.")

    # A. DATABASE SECURITY: Check for used hashes (Replaces the old 'set')
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if already_used:
        logger.warning(f"🚫 Replay attempt blocked: {x_payment_hash}")
        raise HTTPException(status_code=403, detail="Payment hash already used.")

    # B. XRPL VERIFICATION
    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=x_payment_hash))
        result = tx_res.result
        tx_body = result.get("tx") or result.get("transaction") or result.get("tx_json") or result
        meta = result.get("meta") or result.get("meta_data") or tx_body.get("meta") or {}
        
        dest = tx_body.get("Destination") or tx_body.get("destination") or result.get("Destination") or ""
        raw_amt = meta.get("delivered_amount") or tx_body.get("Amount") or "0"
        
        dest = str(dest).strip()
        my_addr = str(referee_wallet.address).strip()
        delivered = int(raw_amt) if isinstance(raw_amt, str) else int(raw_amt.get("value", 0))
        status = meta.get("TransactionResult") or result.get("status")

        if dest.lower() != my_addr.lower() or delivered < REFEREE_FEE_DROPS or status not in ["tesSUCCESS", "success"]:
             raise Exception(f"Verification failed. Dest: {dest}, Amt: {delivered}, Status: {status}")
             
    except Exception as e:
        logger.error(f"❌ XRPL ERROR: {e}")
        raise HTTPException(status_code=402, detail=f"Verification Failed: {str(e)}")

    # C. AI AUDIT & COMMIT
    try:
        verdict, model_used = await raw_smart_audit(req.task, req.work)
        
        # SAVE TO DATABASE: Burn the hash permanently
        new_log = PaymentLog(
            payment_hash=x_payment_hash,
            sender=tx_body.get("Account"),
            amount_xrp=float(drops_to_xrp(str(delivered))),
            task_summary=req.task[:100],
            ai_verdict=verdict,
            model_used=model_used
        )
        db.add(new_log)
        db.commit()
        
        return {
            "ai_verdict": verdict,
            "model_used": model_used,
            "status": "success",
            "payment_verified": f"{new_log.amount_xrp} XRP"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Audit Failure: {e}")
        return {"ai_verdict": f"Audit Error: {str(e)}", "status": "error"}

@app.get("/")
def health():
    return {"status": "online", "referee_address": referee_wallet.address if referee_wallet else "Error"}
