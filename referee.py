import os
import httpx
import logging
import sys
import asyncio
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
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

# --- 2. CORS MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. DATABASE CONFIGURATION ---
db_url_raw = os.getenv("DATABASE_URL")
if not db_url_raw:
    # This prevents the server from crashing silently with a 'NoneType' error
    logger.error("❌ DATABASE_URL missing!")
    DATABASE_URL = "sqlite:///./fallback.db" # Local fallback to keep server alive
else:
    DATABASE_URL = db_url_raw.replace("postgres://", "postgresql://", 1)

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
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 4. CONFIGURATION ---
XRPL_URL = os.getenv("XRPL_URL", "https://s.altnet.rippletest.net:51234/")
BANKER_URL = os.getenv("BANKER_URL") 
SHARED_SECRET = os.getenv("SHARED_SECRET", "default-secret").encode()
REFEREE_FEE_DROPS = 100000  # 0.1 XRP
TARGET_ADDRESS = "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR"

try:
    seed = os.getenv("XRPL_SEED")
    if not seed:
        raise ValueError("XRPL_SEED not found.")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    logger.info(f"🚀 AGENT ACTIVE: Monitoring {referee_wallet.address}")
except Exception as e:
    logger.error(f"STARTUP ERROR: {e}")
    referee_wallet = None

class AuditRequest(BaseModel):
    task: str
    work: str
    escrow_id: str 

# --- 5. AI AUDIT ENGINE ---
async def raw_smart_audit(task: str, work: str):
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing")

    candidates = ["gemini-3-flash", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"]
    payload = {
        "contents": [{
            "parts": [{"text": f"You are a strict autonomous escrow auditor.\nTASK: {task}\nWORK SUBMITTED: {work}\n\nProvide a verdict: APPROVED or REJECTED followed by a 1-sentence explanation."}]
        }]
    }

    async with httpx.AsyncClient() as client:
        for model_id in candidates:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
                res = await client.post(url, json=payload, timeout=20.0)
                if res.status_code == 200:
                    data = res.json()
                    verdict_text = data['candidates'][0]['content']['parts'][0]['text']
                    return verdict_text, model_id
            except:
                continue
        raise Exception("AI Gateway Failure")

# --- 6. ENDPOINTS ---

@app.get("/")
def health():
    return {"status": "Referee is Online", "address": referee_wallet.address if referee_wallet else "Config Error"}

@app.post("/evaluate")
async def evaluate_work(
    req: AuditRequest, 
    x_payment_hash: str = Header(None), 
    db: Session = Depends(get_db)
):
    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing payment hash")

    # A. REPLAY PROTECTION
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if already_used:
        raise HTTPException(status_code=403, detail="Hash already used")

    # B. XRPL VERIFICATION
    client = AsyncJsonRpcClient(XRPL_URL)
    tx_data = None
    for i in range(6): 
        try:
            tx_res = await client.request(Tx(transaction=x_payment_hash))
            if tx_res.is_successful():
                tx_data = tx_res.result
                if tx_data.get("validated"): break
        except: pass
        await asyncio.sleep(2)

    if not tx_data:
        raise HTTPException(status_code=402, detail="Transaction not validated")

    # C. VERIFY DESTINATION & AMOUNT
    body = tx_data.get("tx_json") or tx_data.get("tx") or tx_data
    meta = tx_data.get("meta") or tx_data.get("metaData") or {}
    dest = body.get("Destination")
    delivered = meta.get("delivered_amount") or body.get("Amount")

    if str(dest).lower() != TARGET_ADDRESS.lower():
        raise HTTPException(status_code=402, detail="Wrong destination")

    # D. AI AUDIT
    verdict, model_used = await raw_smart_audit(req.task, req.work)
    
    # E. LOG & PAYOUT LOGIC
    is_approved = "APPROVED" in verdict.upper()
    
    if is_approved:
        # Generate HMAC Signature for the Banker
        signature = hmac.new(SHARED_SECRET, req.escrow_id.encode(), hashlib.sha256).hexdigest()
        
        # Trigger Banker Payout
        if BANKER_URL:
            async with httpx.AsyncClient() as bank_client:
                try:
                    await bank_client.post(
                        f"{BANKER_URL.strip('/')}/payout/{req.escrow_id}",
                        headers={"X-Signature": signature},
                        timeout=10.0
                    )
                    logger.info(f"💰 Payout triggered for {req.escrow_id}")
                except Exception as e:
                    logger.error(f"❌ Banker Payout Call Failed: {e}")
        else:
            logger.warning("⚠️ BANKER_URL not configured. Payout not triggered.")

    # F. COMMIT TO DB
    new_log = PaymentLog(
        payment_hash=x_payment_hash,
        sender=body.get("Account"),
        amount_xrp=float(drops_to_xrp(str(delivered))),
        task_summary=req.task[:100],
        ai_verdict=verdict,
        model_used=model_used
    )
    db.add(new_log)
    db.commit()

    return {"ai_verdict": verdict, "model_used": model_used, "status": "success" if is_approved else "rejected"}
