import os
import re
import httpx
import logging
import sys
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Body
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
DATABASE_URL = os.getenv("DATABASE_URL")
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

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 4. CONFIGURATION ---
XRPL_URL = os.getenv("XRPL_URL", "https://s.altnet.rippletest.net:51234/")
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

# --- 5. AI AUDIT ENGINE ---
async def raw_smart_audit(task: str, work: str):
    api_key = os.getenv('GEMINI_API_KEY')
    async with httpx.AsyncClient() as client:
        # Simple model fallback list
        candidates = ["gemini-1.5-pro", "gemini-1.5-flash"]
        payload = {"contents": [{"parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence summary."}]}]}

        for model_id in candidates:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
                res = await client.post(url, json=payload, timeout=30.0)
                if res.status_code == 200:
                    return res.json()['candidates'][0]['content']['parts'][0]['text'], model_id
            except:
                continue
        raise Exception("AI Gateway Failure")

# --- 6. ENDPOINTS ---

@app.get("/")
def health():
    return {
        "status": "online", 
        "referee_address": referee_wallet.address if referee_wallet else "Config Error",
        "target_match": TARGET_ADDRESS
    }

@app.post("/evaluate")
async def evaluate_work(
    req: AuditRequest, 
    x_payment_hash: str = Header(None), 
    db: Session = Depends(get_db)
):
    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing x-payment-hash header.")

    # A. REPLAY PROTECTION
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if already_used:
        raise HTTPException(status_code=403, detail="Payment hash already used.")

    # B. AGGRESSIVE XRPL VERIFICATION
    client = AsyncJsonRpcClient(XRPL_URL)
    tx_data = None
    
    # Retry loop to wait for ledger validation
    for i in range(6): 
        try:
            tx_res = await client.request(Tx(transaction=x_payment_hash))
            if tx_res.is_successful():
                tx_data = tx_res.result
                if tx_data.get("validated") == True:
                    break
        except Exception as e:
            logger.warning(f"Connection attempt {i+1} failed: {e}")
        await asyncio.sleep(2)

    if not tx_data:
        raise HTTPException(status_code=402, detail="Transaction not found or not validated by Ledger.")

    try:
        # Deep search for transaction details
        body = tx_data.get("tx") or tx_data.get("transaction") or tx_data
        meta = tx_data.get("meta") or tx_data.get("metaData") or {}
        
        # Extract Destination, Amount, and Result
        dest = body.get("Destination") or tx_data.get("Destination")
        delivered = meta.get("delivered_amount") or body.get("Amount")
        status = meta.get("TransactionResult") or tx_data.get("status")

        if not dest:
            raise Exception(f"Destination field missing from Ledger response. Keys found: {list(body.keys())}")

        # Final Verification Check (Case-Insensitive for safety)
        allowed = [referee_wallet.address.lower(), TARGET_ADDRESS.lower()]
        
        if str(dest).lower() not in allowed:
            raise Exception(f"Wrong destination. Ledger saw: {dest}")
        
        if int(str(delivered)) < REFEREE_FEE_DROPS:
            raise Exception(f"Payment too low. Expected 100000 drops, got {delivered}")

        if status not in ["tesSUCCESS", "success"]:
            raise Exception(f"Transaction not successful: {status}")
             
        logger.info(f"✅ Verified {delivered} drops to {dest}")

    except Exception as e:
        raise HTTPException(status_code=402, detail=f"Verification Failed: {str(e)}")

    # C. AI AUDIT
    try:
        verdict, model_used = await raw_smart_audit(req.task, req.work)
        
        # D. COMMIT TO LOGS
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

        return {"ai_verdict": verdict, "model_used": model_used, "status": "success"}
    except Exception as e:
        db.rollback()
        return {"ai_verdict": f"Audit Error: {str(e)}", "status": "error"}
