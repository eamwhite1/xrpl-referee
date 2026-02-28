import os
import httpx
import logging
import sys
import asyncio
import hmac
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# XRPL Imports
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx
from xrpl.utils import drops_to_xrp
from xrpl.core.addresscodec import decode_seed

# XUMM SDK Import (Graceful fallback if not installed)
try:
    from xumm import XummSdk
except ImportError:
    XummSdk = None

# Database Imports
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- 1. INITIAL SETUP & LOGGING ---
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")
load_dotenv()

app = FastAPI(title="AgentTrust Protocol Core")

# --- 2. CORS MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. UI & STATIC FILE ROUTING ---
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    logger.warning("⚠️ 'static' directory not found. UI will not be served.")

@app.get("/")
@app.head("/")
def serve_ui():
    """Satisfies UptimeRobot (HEAD) and Users (GET)"""
    path = "static/index.html"
    if os.path.exists(path):
        return FileResponse(path)
    return {"status": "Referee Online", "message": "UI file not found in /static"}

@app.get("/status")
def health_check():
    """Explicit secondary endpoint for health checks"""
    return {"status": "online", "timestamp": datetime.now(timezone.utc)}

# --- 4. DATABASE CONFIGURATION ---
db_url_raw = os.getenv("DATABASE_URL")
if not db_url_raw:
    logger.error("❌ DATABASE_URL missing!")
    DATABASE_URL = "sqlite:///./fallback.db"
else:
    DATABASE_URL = db_url_raw.replace("postgres://", "postgresql://", 1)
    if "neon.tech" in DATABASE_URL and "sslmode" not in DATABASE_URL:
        DATABASE_URL += "?sslmode=require"

engine_args = {"pool_pre_ping": True, "pool_recycle": 300}
if "sqlite" not in DATABASE_URL:
    engine_args["connect_args"] = {"sslmode": "require"}

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class PaymentLog(Base):
    __tablename__ = "payment_logs"
    id = Column(Integer, primary_key=True, index=True)
    payment_hash = Column(String, unique=True, index=True, nullable=False)
    sender = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    task_summary = Column(String, nullable=True)
    ai_verdict = Column(Text, nullable=True)
    model_used = Column(String, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class EscrowVault(Base):
    __tablename__ = "escrow_vault"
    escrow_id = Column(String, primary_key=True, index=True)
    condition = Column(String, nullable=False)
    fulfillment = Column(String, nullable=False)
    status = Column(String, default="LOCKED")

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 5. CONFIGURATION ---
XRPL_URL = os.getenv("XRPL_URL", "https://s.altnet.rippletest.net:51234/")
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

# XUMM SDK Setup
xumm_api_key = os.getenv("XUMM_API_KEY")
xumm_api_secret = os.getenv("XUMM_API_SECRET")

# This will tell us in the logs if the keys are actually being "seen"
if xumm_api_key:
    logger.info(f"✅ XUMM_API_KEY found (Starts with: {xumm_api_key[:4]}...)")
else:
    logger.error("❌ XUMM_API_KEY is EMPTY in Render environment variables!")

if xumm_api_secret:
    logger.info("✅ XUMM_API_SECRET found.")
else:
    logger.error("❌ XUMM_API_SECRET is EMPTY in Render environment variables!")

xumm_sdk = None
if xumm_api_key and xumm_api_secret and XummSdk:
    try:
        xumm_sdk = XummSdk(xumm_api_key, xumm_api_secret)
        # Check connection to Xumm
        pong = xumm_sdk.ping()
        logger.info(f"🔌 XUMM SDK Initialized: {pong.application.name}")
    except Exception as e:
        logger.error(f"❌ XUMM SDK Failed to ping: {e}")

class AuditRequest(BaseModel):
    task: str
    work: str
    escrow_id: str 

class EscrowSetupRequest(BaseModel):
    escrow_id: str
    fee_hash: Optional[str] = None

class XummPayloadRequest(BaseModel):
    txjson: dict

# --- 6. AI AUDIT ENGINE ---
async def raw_smart_audit(task: str, work: str):
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing")
    candidates = ["gemini-3-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
    payload = {"contents": [{"parts": [{"text": f"You are a strict autonomous escrow auditor.\nTASK: {task}\nWORK SUBMITTED: {work}\n\nProvide a verdict: APPROVED or REJECTED followed by a 1-sentence explanation."}]}]}
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

# --- 7. PROTOCOL ENDPOINTS ---

@app.post("/escrow/generate")
async def generate_escrow_crypto(req: EscrowSetupRequest, db: Session = Depends(get_db)):
    existing = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Escrow ID already exists")

    if req.fee_hash:
        already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == req.fee_hash).first()
        if already_used:
            raise HTTPException(status_code=403, detail="Fee hash already used")
        client = AsyncJsonRpcClient(XRPL_URL)
        try:
            tx_res = await client.request(Tx(transaction=req.fee_hash))
            if not tx_res.is_successful():
                raise HTTPException(status_code=402, detail="Fee transaction not found")
            body = tx_res.result
            dest = body.get("Destination")
            if str(dest).lower() != TARGET_ADDRESS.lower():
                raise HTTPException(status_code=402, detail="Fee sent to wrong wallet")
            db.add(PaymentLog(payment_hash=req.fee_hash, task_summary=f"Fee for {req.escrow_id}"))
        except Exception as e:
            logger.error(f"Manual Hash Verify Error: {e}")
            raise HTTPException(status_code=400, detail="Ledger verification failed")

    fulfillment_bytes = secrets.token_bytes(32)
    fulfillment_hex = fulfillment_bytes.hex().upper()
    condition_hex = hashlib.sha256(fulfillment_bytes).hexdigest().upper()
    vault = EscrowVault(escrow_id=req.escrow_id, condition=condition_hex, fulfillment=fulfillment_hex)
    db.add(vault)
    db.commit()
    return {"escrow_id": req.escrow_id, "condition": condition_hex}

@app.post("/xumm/create-payload")
async def create_xumm_payload(req: XummPayloadRequest):
    if not xumm_sdk:
        logger.error("❌ XUMM SDK not initialized.")
        raise HTTPException(status_code=500, detail="Xumm SDK not configured.")
    try:
        logger.info(f"🚀 Sending to Xaman: {req.txjson}")
        result = xumm_sdk.payload.create(req.txjson)
        return {"nextUrl": result.next.always}
    except Exception as e:
        logger.error(f"❌ XUMM API ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, x_payment_hash: str = Header(None), db: Session = Depends(get_db)):
    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing payment hash")

    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if already_used and already_used.ai_verdict is not None:
        raise HTTPException(status_code=403, detail="Audit fee hash already used")

    client = AsyncJsonRpcClient(XRPL_URL)
    tx_res = await client.request(Tx(transaction=x_payment_hash))
    if not tx_res.is_successful():
        raise HTTPException(status_code=402, detail="Transaction not found")

    verdict, model_used = await raw_smart_audit(req.task, req.work)
    is_approved = "APPROVED" in verdict.upper()
    revealed_fulfillment = None
    
    if is_approved:
        vault_entry = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
        if vault_entry:
            revealed_fulfillment = vault_entry.fulfillment
            vault_entry.status = "RELEASED"

    log_entry = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if not log_entry:
        log_entry = PaymentLog(payment_hash=x_payment_hash)
        db.add(log_entry)
        
    log_entry.ai_verdict = verdict
    log_entry.model_used = model_used
    db.commit()

    return {
        "ai_verdict": verdict, 
        "model_used": model_used, 
        "status": "success" if is_approved else "rejected",
        "fulfillment": revealed_fulfillment 
    }

