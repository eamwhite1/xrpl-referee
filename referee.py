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
# Mounts the static directory to serve your frontend
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    logger.warning("⚠️ 'static' directory not found. UI will not be served.")

@app.get("/")
def serve_ui():
    if os.path.exists("static/index.html"):
        return FileResponse('static/index.html')
    return {"status": "API is running, but static/index.html is missing"}

@app.head("/health")
@app.get("/health")
def health():
    return {
        "status": "Referee is Online", 
        "address": referee_wallet.address if referee_wallet else "Config Error"
    }

# --- 4. DATABASE CONFIGURATION ---
db_url_raw = os.getenv("DATABASE_URL")
if not db_url_raw:
    logger.error("❌ DATABASE_URL missing!")
    DATABASE_URL = "sqlite:///./fallback.db"
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
    amount = Column(Float)       # Multi-currency ready
    currency = Column(String)    # Multi-currency ready
    task_summary = Column(String)
    ai_verdict = Column(Text)
    model_used = Column(String)
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

# XUMM Configuration
xumm_api_key = os.getenv("XUMM_API_KEY")
xumm_api_secret = os.getenv("XUMM_API_SECRET")
xumm_sdk = None
if xumm_api_key and xumm_api_secret and XummSdk:
    xumm_sdk = XummSdk(xumm_api_key, xumm_api_secret)
    logger.info("🔌 XUMM SDK Initialized")
else:
    logger.warning("⚠️ XUMM credentials missing or SDK not installed. Mobile signing disabled.")

class AuditRequest(BaseModel):
    task: str
    work: str
    escrow_id: str 

class EscrowSetupRequest(BaseModel):
    escrow_id: str

class XummPayloadRequest(BaseModel):
    txjson: dict

# --- 6. AI AUDIT ENGINE ---
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

# --- 7. PROTOCOL ENDPOINTS ---

@app.post("/escrow/generate")
def generate_escrow_crypto(req: EscrowSetupRequest, db: Session = Depends(get_db)):
    """Generates the cryptographic lock for the XRPL Escrow."""
    existing = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Escrow ID already exists")

    # Generate a secure 32-byte secret (Fulfillment)
    fulfillment_bytes = secrets.token_bytes(32)
    fulfillment_hex = fulfillment_bytes.hex().upper()
    
    # Hash the secret to create the public Condition (PREIMAGE-SHA-256)
    condition_hex = hashlib.sha256(fulfillment_bytes).hexdigest().upper()
    
    vault = EscrowVault(escrow_id=req.escrow_id, condition=condition_hex, fulfillment=fulfillment_hex)
    db.add(vault)
    db.commit()
    
    return {"escrow_id": req.escrow_id, "condition": condition_hex}

@app.post("/xumm/create-payload")
async def create_xumm_payload(req: XummPayloadRequest):
    """Bridges the web app to Xaman for mobile signing."""
    if not xumm_sdk:
        raise HTTPException(status_code=500, detail="Xumm SDK not configured on server.")
    try:
        result = xumm_sdk.payload.create(req.txjson)
        return {"nextUrl": result.next.always}
    except Exception as e:
        logger.error(f"Xumm Payload Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/evaluate")
async def evaluate_work(
    req: AuditRequest, 
    x_payment_hash: str = Header(None), 
    db: Session = Depends(get_db)
):
    """The Oracle: Verifies the payment, queries the AI, and unlocks the escrow."""
    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing payment hash for audit fee")

    # A. REPLAY PROTECTION
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if already_used:
        raise HTTPException(status_code=403, detail="Audit fee hash already used")

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

    # C. VERIFY DESTINATION & MULTI-CURRENCY AMOUNT
    body = tx_data.get("tx_json") or tx_data.get("tx") or tx_data
    meta = tx_data.get("meta") or tx_data.get("metaData") or {}
    dest = body.get("Destination")
    delivered = meta.get("delivered_amount") or body.get("Amount")

    if str(dest).lower() != TARGET_ADDRESS.lower():
        raise HTTPException(status_code=402, detail="Wrong destination for audit fee")

    if isinstance(delivered, dict):
        amount_val = float(delivered.get("value", 0))
        currency_val = delivered.get("currency")
    else:
        amount_val = float(drops_to_xrp(str(delivered)))
        currency_val = "XRP"

    # D. AI AUDIT
    verdict, model_used = await raw_smart_audit(req.task, req.work)
    
    # E. LOG & NATIVE PAYOUT LOGIC
    is_approved = "APPROVED" in verdict.upper()
    revealed_fulfillment = None
    
    if is_approved:
        vault_entry = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
        if vault_entry:
            revealed_fulfillment = vault_entry.fulfillment
            vault_entry.status = "RELEASED"
            logger.info(f"🔓 Escrow {req.escrow_id} APPROVED. Fulfillment revealed.")
        else:
            logger.error(f"❌ Escrow {req.escrow_id} not found in vault!")

    # F. COMMIT TO DB
    new_log = PaymentLog(
        payment_hash=x_payment_hash,
        sender=body.get("Account"),
        amount=amount_val,
        currency=currency_val,
        task_summary=req.task[:100],
        ai_verdict=verdict,
        model_used=model_used
    )
    db.add(new_log)
    db.commit()

    return {
        "ai_verdict": verdict, 
        "model_used": model_used, 
        "status": "success" if is_approved else "rejected",
        "fulfillment": revealed_fulfillment  # Frontend uses this to trigger EscrowFinish
    }
