import os
import re
import httpx
import logging
import sys
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Body
from fastapi.responses import FileResponse 
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

# We set openapi_url=None to stop FastAPI from generating its own generic map.
# This forces the app to use YOUR custom openapi.json file.
app = FastAPI(title="XRPL Referee Pro", openapi_url=None)

# --- 2. DATABASE CONFIGURATION ---
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

# --- 3. CONFIGURATION ---
XRPL_URL = os.getenv("XRPL_URL", "https://xrplcluster.com")
REFEREE_FEE_DROPS = 100000  # 0.1 XRP
DATA_CAP = 10000            # Max characters allowed (Task + Work)

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

# --- 4. INTELLIGENT DISCOVERY ENGINE ---
async def raw_smart_audit(task: str, work: str):
    api_key = os.getenv('GEMINI_API_KEY')
    async with httpx.AsyncClient() as client:
        list_url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
        try:
            list_res = await client.get(list_url)
            discovered = [m['name'] for m in list_res.json().get('models', []) 
                          if 'generateContent' in m.get('supportedGenerationMethods', [])] if list_res.status_code == 200 else []
        except:
            discovered = []

        if not discovered:
            discovered = ["models/gemini-1.5-pro", "models/gemini-1.5-flash"]

        def model_rank(name):
            version_match = re.findall(r'\d+\.\d+|\d+', name)
            score = float(version_match[0]) if version_match else 0.0
            if any(term in name.lower() for term in ['pro', 'ultra', 'deep']):
                score += 100 
            return score

        candidates = sorted(discovered, key=model_rank, reverse=True)
        payload = {"contents": [{"parts": [{"text": f"TASK: {task}\nWORK: {work}\n\nVerdict (APPROVED/REJECTED) + 1 sentence summary."}]}]}

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

# --- 5. ENDPOINTS ---

# FIXED: We use api_route to allow HEAD requests from UptimeRobot
@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"status": "online", "referee_address": referee_wallet.address if referee_wallet else "Error"}

# Serves your custom openapi.json file
@app.get("/openapi.json")
def get_openapi():
    return FileResponse("openapi.json")

# The A2A Discovery standard route
@app.get("/.well-known/agent.json")
def get_a2a_card():
    return FileResponse("agent.json")

# --- NEW: SMITHERY & MCP DISCOVERY (Fixes the 404 Error) ---
@app.get("/.well-known/mcp/server-card.json")
def get_mcp_card():
    return {
        "mcpVersion": "2026.1.0",
        "name": "XRPL AI Referee Pro",
        "description": "Audits tasks for 0.1 XRP fee.",
        "tools": [
            {
                "name": "evaluate",
                "description": "Audits work against a task. Requires 'x-payment-hash' header.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "The requirements"},
                        "work": {"type": "string", "description": "The completed work"}
                    },
                    "required": ["task", "work"]
                }
            }
        ]
    }

@app.get("/mcp")
async def mcp_handshake():
    return {"status": "connected", "protocol": "mcp-http", "capabilities": ["resources", "tools"]}
# -----------------------------------------------------------

@app.post("/evaluate")
async def evaluate_work(
    req: AuditRequest, 
    x_payment_hash: str = Header(None), 
    db: Session = Depends(get_db)
):
    # PROTECTOR: Data Cap Check
    if len(req.task) + len(req.work) > DATA_CAP:
        raise HTTPException(status_code=413, detail=f"Payload too large. Max {DATA_CAP} chars.")

    if not x_payment_hash: 
        raise HTTPException(status_code=400, detail="Missing x-payment-hash header.")

    # A. REPLAY PROTECTION
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == x_payment_hash).first()
    if already_used:
        raise HTTPException(status_code=403, detail="Payment hash already used.")

    # B. XRPL VERIFICATION
    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=x_payment_hash))
        result = tx_res.result
        tx_body = result.get("tx") or result.get("transaction") or result
        meta = result.get("meta") or tx_body.get("meta") or {}
        
        dest = str(tx_body.get("Destination", "")).strip()
        delivered = int(meta.get("delivered_amount", tx_body.get("Amount", 0)))
        status = meta.get("TransactionResult") or result.get("status")

        if dest.lower() != referee_wallet.address.lower():
             raise Exception("Wrong destination.")
        if delivered < REFEREE_FEE_DROPS:
             raise Exception("Payment too low.")
        if status not in ["tesSUCCESS", "success"]:
            raise Exception("Transaction failed on-chain.")
             
    except Exception as e:
        raise HTTPException(status_code=402, detail=f"Verification Failed: {str(e)}")

    # C. AI AUDIT
    try:
        verdict, model_used = await raw_smart_audit(req.task, req.work)
        
        # D. COMMIT (Burn the hash only after AI succeeds)
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
        
        return {"ai_verdict": verdict, "model_used": model_used, "status": "success"}
    except Exception as e:
        db.rollback()
        return {"ai_verdict": f"Audit Error: {str(e)}", "status": "error"}


