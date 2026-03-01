import os
import httpx
import logging
import sys
import hashlib
import secrets
import json
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
from xrpl.core.addresscodec import decode_seed

# XUMM SDK Import
try:
    from xumm import XummSdk
except ImportError:
    XummSdk = None

# Database Imports
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# ---------------------------------------------------------------------------
# 1. INITIAL SETUP & LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")
load_dotenv()

app = FastAPI(title="AgentTrust Protocol Core")

# ---------------------------------------------------------------------------
# 2. CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 3. STATIC FILES & HEALTH
# ---------------------------------------------------------------------------
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    logger.warning("⚠️ 'static' directory not found.")

@app.get("/")
@app.head("/")
def serve_ui():
    path = "static/index.html"
    if os.path.exists(path):
        return FileResponse(path)
    return {"status": "Referee Online", "message": "UI not found in /static"}

@app.get("/status")
def health_check():
    return {"status": "online", "timestamp": datetime.now(timezone.utc)}

# ---------------------------------------------------------------------------
# 4. DATABASE
# ---------------------------------------------------------------------------
db_url_raw = os.getenv("DATABASE_URL")
if not db_url_raw:
    logger.error("❌ DATABASE_URL missing! Using SQLite fallback.")
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
    """
    Every consumed fee hash lives here.
    One hash = one use, forever. This is the anti-replay guard.
    """
    __tablename__ = "payment_logs"
    id = Column(Integer, primary_key=True, index=True)
    payment_hash = Column(String, unique=True, index=True, nullable=False)
    purpose = Column(String, nullable=True)       # "setup_fee"
    sender = Column(String, nullable=True)
    amount_xrp = Column(Float, nullable=True)
    escrow_id = Column(String, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class EscrowVault(Base):
    """
    Single source of truth for all escrow state.
    Stores the fulfillment key, job details, and audit result.
    The Banker service is no longer needed.
    """
    __tablename__ = "escrow_vault"
    escrow_id       = Column(String, primary_key=True, index=True)
    condition       = Column(String, nullable=False)
    fulfillment     = Column(String, nullable=False)
    status          = Column(String, default="LOCKED")  # LOCKED | RELEASED | CANCELLED
    # Job metadata stored at vault creation so the audit endpoint has full context
    buyer_name       = Column(String, nullable=True)
    task_description = Column(Text, nullable=True)
    worker_address   = Column(String, nullable=True)
    amount_xrp       = Column(Float, nullable=True)
    cancel_after_ts  = Column(DateTime, nullable=True)
    # Buyer's uploaded brief/spec — stored as JSON list of {filename, mime_type, data}
    buyer_attachments = Column(Text, nullable=True)
    # Audit result
    ai_verdict      = Column(Text, nullable=True)       # JSON string
    model_used      = Column(String, nullable=True)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)


def run_migrations():
    """
    Safely adds any missing columns to existing tables.
    Uses ALTER TABLE IF NOT EXISTS pattern — safe to run on every startup.
    This replaces the need for Alembic for a project of this size.
    """
    migrations = [
        # escrow_vault new columns added after initial deploy
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_name       VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS task_description  TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_address    VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS amount_xrp        FLOAT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS cancel_after_ts   TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_attachments  TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS ai_verdict         TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS model_used         VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS created_at         TIMESTAMP",
        # payment_logs new columns
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS purpose   VARCHAR",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS sender    VARCHAR",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS amount_xrp FLOAT",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS escrow_id  VARCHAR",
    ]

    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                logger.warning(f"Migration skipped ({sql[:60]}...): {e}")

    logger.info("✅ Database migrations complete.")

run_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. CONFIGURATION
# ---------------------------------------------------------------------------
XRPL_URL        = os.getenv("XRPL_URL", "https://xrplcluster.com")
PROTOCOL_WALLET = "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR"
MIN_FEE_XRP     = 0.1   # Single upfront fee — covers setup + AI audit

try:
    seed = os.getenv("XRPL_SEED")
    if not seed:
        raise ValueError("XRPL_SEED not found.")
    _, algo = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    logger.info(f"🚀 AGENT ACTIVE: {referee_wallet.address}")
except Exception as e:
    logger.error(f"STARTUP ERROR (wallet): {e}")
    referee_wallet = None

# XUMM SDK
xumm_api_key    = os.getenv("XUMM_API_KEY")
xumm_api_secret = os.getenv("XUMM_API_SECRET")
xumm_sdk        = None

if xumm_api_key:
    logger.info(f"✅ XUMM_API_KEY found (starts: {xumm_api_key[:4]}...)")
else:
    logger.error("❌ XUMM_API_KEY missing!")

if xumm_api_key and xumm_api_secret and XummSdk:
    try:
        xumm_sdk = XummSdk(xumm_api_key, xumm_api_secret)
        pong = xumm_sdk.ping()
        logger.info(f"🔌 XUMM SDK connected: {pong.application.name}")
    except Exception as e:
        logger.error(f"❌ XUMM SDK ping failed: {e}")


# ---------------------------------------------------------------------------
# 6. PYDANTIC MODELS
# ---------------------------------------------------------------------------
class Attachment(BaseModel):
    filename:  str
    mime_type: str
    data:      str   # base64 encoded

class EscrowSetupRequest(BaseModel):
    escrow_id:          str
    fee_hash:           str
    buyer_name:         str
    task_description:   str
    worker_address:     str
    amount_xrp:         float
    cancel_after_hrs:   int = 168
    buyer_attachments:  Optional[list[Attachment]] = None  # Spec docs, briefs, etc.

class AuditRequest(BaseModel):
    escrow_id:           str
    work:                str                  # Worker's text submission
    worker_attachments:  Optional[list[Attachment]] = None  # Proof of work files
    callback_url:        Optional[str] = None

class XummPayloadRequest(BaseModel):
    txjson: dict


# ---------------------------------------------------------------------------
# 7. FEE VERIFICATION HELPER
# ---------------------------------------------------------------------------
async def verify_fee_payment(fee_hash: str, escrow_id: str, db: Session) -> dict:
    """
    Verifies a tx hash on the XRPL and enforces:
      - It is a Payment transaction
      - Destination is the protocol wallet
      - Amount is >= MIN_FEE_XRP
      - Hash has never been used before (anti-replay attack prevention)

    Logs the hash immediately on success so it can never be reused.
    Returns {"sender": str, "amount_xrp": float} on success.
    Raises HTTPException on any failure.
    """
    # Anti-replay: reject if hash already consumed
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == fee_hash).first()
    if already_used:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Payment hash already used for escrow '{already_used.escrow_id}' "
                f"on {already_used.timestamp.strftime('%Y-%m-%d %H:%M UTC')}."
            )
        )

    # Ledger lookup
    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=fee_hash))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ledger lookup failed: {str(e)}")

    if not tx_res.is_successful():
        raise HTTPException(status_code=402, detail="Transaction hash not found on the XRPL ledger.")

    body    = tx_res.result
    tx_data = body.get("tx_json") or body.get("tx") or body

    tx_type    = tx_data.get("TransactionType", "")
    dest       = str(tx_data.get("Destination", "")).strip()
    raw_amount = tx_data.get("Amount", "0")
    sender     = tx_data.get("Account", "unknown")

    logger.info(f"🔍 LEDGER: type={tx_type} | dest={dest} | amount={raw_amount} | from={sender}")

    # Must be a Payment
    if tx_type != "Payment":
        raise HTTPException(
            status_code=400,
            detail=f"Transaction is '{tx_type}', not a Payment."
        )

    # Destination must be the protocol wallet
    if dest.lower() != PROTOCOL_WALLET.lower():
        raise HTTPException(
            status_code=402,
            detail=f"Wrong destination. Expected {PROTOCOL_WALLET}, got {dest}."
        )

    # Issued currency not supported for fees
    if isinstance(raw_amount, dict):
        raise HTTPException(
            status_code=400,
            detail="Protocol fees must be paid in XRP, not issued currency."
        )

    # Amount must meet minimum
    amount_xrp = int(raw_amount) / 1_000_000
    if amount_xrp < MIN_FEE_XRP:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient fee. Required ≥{MIN_FEE_XRP} XRP, received {amount_xrp:.6f} XRP."
        )

    # Log the hash — consumed, can never be reused
    db.add(PaymentLog(
        payment_hash=fee_hash,
        purpose="setup_fee",
        sender=sender,
        amount_xrp=amount_xrp,
        escrow_id=escrow_id,
    ))
    db.commit()

    logger.info(f"✅ FEE VERIFIED: {amount_xrp} XRP from {sender} for escrow '{escrow_id}'")
    return {"sender": sender, "amount_xrp": amount_xrp}


# ---------------------------------------------------------------------------
# 8. AI AUDIT ENGINE
# ---------------------------------------------------------------------------
async def run_ai_audit(
    task: str,
    work: str,
    buyer_attachments: list = None,
    worker_attachments: list = None,
) -> tuple[dict, str]:
    """
    Calls Gemini with a multimodal prompt.
    Supports text + files (PDF, images) from both buyer (spec) and worker (proof).
    Response is always structured JSON — no regex needed by agents.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing from environment.")

    # gemini-2.5-flash and 2.0-flash both support multimodal (PDF + images)
    candidates = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

    prompt_text = (
        "You are a strict autonomous escrow auditor. "
        "Your response must be valid JSON and nothing else — no markdown, no backticks, no preamble.\n\n"
        f"TASK REQUIREMENTS:\n{task}\n"
    )

    if buyer_attachments:
        prompt_text += f"\nThe buyer has also provided {len(buyer_attachments)} supporting document(s) above as part of the task specification. Use these to inform your evaluation.\n"

    prompt_text += f"\nWORK SUBMITTED (text):\n{work}\n"

    if worker_attachments:
        prompt_text += f"\nThe worker has also submitted {len(worker_attachments)} document(s)/image(s) above as proof of work. Evaluate these as part of the submission.\n"

    prompt_text += (
        "\nRespond with ONLY this JSON object:\n"
        "{\n"
        '  "verdict": "PASS" or "FAIL",\n'
        '  "score": <integer 0-100>,\n'
        '  "summary": "<one sentence conclusion>",\n'
        '  "details": "<2-3 sentences of specific feedback for the worker>",\n'
        '  "criteria_met": ["<requirement 1>", "..."],\n'
        '  "criteria_failed": ["<requirement 1>", "..."]\n'
        "}"
    )

    # Build multimodal parts list
    # Order: buyer attachments → task prompt → worker attachments → evaluation prompt
    parts = []

    # Buyer spec documents (if any)
    if buyer_attachments:
        for att in buyer_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            # Gemini supports: image/jpeg, image/png, image/gif, image/webp, application/pdf
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": att.get("data")   # already base64
                    }
                })
                logger.info(f"📎 Buyer attachment added to AI: {att.get('filename')} ({mime})")
            else:
                # For unsupported types (DOCX, TXT etc) the text was extracted client-side
                # and will appear in the work/task text fields — nothing extra needed here
                logger.info(f"ℹ️ Buyer attachment {att.get('filename')} is text-extracted, skipping inline_data")

    # Worker proof documents (if any)
    if worker_attachments:
        for att in worker_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": att.get("data")
                    }
                })
                logger.info(f"📎 Worker attachment added to AI: {att.get('filename')} ({mime})")
            else:
                logger.info(f"ℹ️ Worker attachment {att.get('filename')} is text-extracted, skipping inline_data")

    # Main text prompt always goes last
    parts.append({"text": prompt_text})

    payload = {"contents": [{"parts": parts}]}

    async with httpx.AsyncClient() as client:
        for model_id in candidates:
            try:
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_id}:generateContent?key={api_key}"
                )
                res = await client.post(url, json=payload, timeout=60.0)

                if res.status_code == 200:
                    data     = res.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

                    logger.info(f"🤖 AI raw ({model_id}): {repr(raw_text[:300])}")

                    clean        = raw_text.replace("```json", "").replace("```", "").strip()
                    verdict_dict = json.loads(clean)
                    verdict_dict["verdict"] = str(verdict_dict.get("verdict", "FAIL")).strip().upper()

                    logger.info(
                        f"✅ AI VERDICT: {verdict_dict['verdict']} | "
                        f"score={verdict_dict.get('score')} | model={model_id}"
                    )
                    return verdict_dict, model_id

                else:
                    logger.warning(f"Model {model_id} HTTP {res.status_code}: {res.text[:200]}")

            except Exception as e:
                logger.warning(f"Model {model_id} failed: {e}")
                continue

    raise Exception("AI Gateway Failure: all models exhausted.")


# ---------------------------------------------------------------------------
# 10. XUMM ENDPOINTS
# ---------------------------------------------------------------------------
@app.post("/xumm/fee-payload")
async def create_fee_payload():
    """
    Step 1A — Creates a Xaman sign request for the 0.2 XRP protocol fee.
    The buyer calls this first. After signing in Xaman, they get a tx hash
    which they pass to /escrow/generate.
    """
    if not xumm_sdk:
        raise HTTPException(status_code=500, detail="Xumm SDK not configured.")

    tx = {
        "TransactionType": "Payment",
        "Destination": PROTOCOL_WALLET,
        "Amount": str(int(MIN_FEE_XRP * 1_000_000)),  # drops = 100000
    }

    try:
        result = xumm_sdk.payload.create(tx)
        return {
            "nextUrl": result.next.always,
            "uuid":    result.uuid,
            "qr":      result.refs.qr_png,
        }
    except Exception as e:
        logger.error(f"❌ XUMM fee payload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/xumm/payload/{uuid}")
async def get_xumm_payload_status(uuid: str):
    """
    Poll this after showing Xaman QR/deeplink.
    Returns signed=true and the tx_hash once the user has signed.
    The frontend polls this every 3 seconds until signed=true.
    """
    if not xumm_sdk:
        raise HTTPException(status_code=500, detail="Xumm SDK not configured.")
    try:
        result   = xumm_sdk.payload.get(uuid)
        signed   = result.meta.signed
        tx_hash  = result.response.txid if signed else None
        return {"signed": signed, "tx_hash": tx_hash}
    except Exception as e:
        logger.error(f"❌ XUMM poll error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/xumm/create-payload")
async def create_xumm_payload(req: XummPayloadRequest):
    """Generic Xaman payload for EscrowCreate / EscrowFinish."""
    if not xumm_sdk:
        raise HTTPException(status_code=500, detail="Xumm SDK not configured.")
    try:
        logger.info(f"🚀 Xaman payload: {req.txjson}")
        result = xumm_sdk.payload.create(req.txjson)
        return {"nextUrl": result.next.always, "uuid": result.uuid}
    except Exception as e:
        logger.error(f"❌ XUMM payload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# 11. CORE PROTOCOL ENDPOINTS
# ---------------------------------------------------------------------------

@app.post("/escrow/generate")
async def generate_escrow(req: EscrowSetupRequest, db: Session = Depends(get_db)):
    """
    BUYER — Step 1B.
    Verifies fee, generates crypto-condition primitives, stores vault with
    all job metadata including any uploaded spec documents.
    Returns the Condition for EscrowCreate via Xaman.
    """
    existing = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Project ID '{req.escrow_id}' already exists. Please choose a different ID."
        )

    await verify_fee_payment(fee_hash=req.fee_hash, escrow_id=req.escrow_id, db=db)

    preimage_bytes    = secrets.token_bytes(32)
    preimage_hex      = preimage_bytes.hex().upper()
    hash_hex          = hashlib.sha256(preimage_bytes).hexdigest().upper()
    final_condition   = f"A0258020{hash_hex}810120"
    final_fulfillment = f"A0228020{preimage_hex}"

    cancel_after_ts = None
    if req.cancel_after_hrs:
        from datetime import timedelta
        cancel_after_ts = datetime.now(timezone.utc) + timedelta(hours=req.cancel_after_hrs)

    # Serialise buyer attachments as JSON for storage
    attachments_json = None
    if req.buyer_attachments:
        attachments_json = json.dumps([a.dict() for a in req.buyer_attachments])
        logger.info(f"📎 Storing {len(req.buyer_attachments)} buyer attachment(s) for '{req.escrow_id}'")

    vault = EscrowVault(
        escrow_id         = req.escrow_id,
        condition         = final_condition,
        fulfillment       = final_fulfillment,
        status            = "LOCKED",
        buyer_name        = req.buyer_name,
        task_description  = req.task_description,
        worker_address    = req.worker_address,
        amount_xrp        = req.amount_xrp,
        cancel_after_ts   = cancel_after_ts,
        buyer_attachments = attachments_json,
    )
    db.add(vault)
    db.commit()

    logger.info(f"🔒 VAULT CREATED: escrow_id='{req.escrow_id}'")

    RIPPLE_EPOCH = 946684800
    cancel_after_ripple = (
        int(cancel_after_ts.timestamp()) - RIPPLE_EPOCH
        if cancel_after_ts else None
    )

    return {
        "escrow_id":           req.escrow_id,
        "condition":           final_condition,
        "status":              "LOCKED",
        "cancel_after_ripple": cancel_after_ripple,
        "cancel_after_human":  cancel_after_ts.strftime("%Y-%m-%d %H:%M UTC") if cancel_after_ts else None,
    }


@app.get("/escrow/{escrow_id}")
async def get_escrow_info(escrow_id: str, db: Session = Depends(get_db)):
    """
    WORKER — Called when the worker lands on the portal with a Project ID.
    Returns non-sensitive job info so the worker can see what they need to deliver.
    The fulfillment key is never returned here.
    """
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Project '{escrow_id}' not found.")

    deadline_str = (
        vault.cancel_after_ts.strftime("%A %d %B %Y at %H:%M UTC")
        if vault.cancel_after_ts else "Not specified"
    )

    return {
        "escrow_id":       vault.escrow_id,
        "status":          vault.status,
        "buyer_name":      vault.buyer_name,
        "task_description":vault.task_description,
        "amount_xrp":      vault.amount_xrp,
        "deadline":        deadline_str,
        "worker_address":  vault.worker_address,
    }


@app.post("/evaluate")
async def evaluate_work(
    req: AuditRequest,
    db: Session = Depends(get_db),
):
    """
    WORKER — Step 2.
    The worker submits their proof of work. No payment required here —
    the buyer already covered the audit fee in Step 1.

    1. Looks up the vault by escrow_id
    2. Verifies the vault is LOCKED (not already released or cancelled)
    3. Runs the AI audit using the stored task description vs submitted work
    4. If PASS: reveals the fulfillment key so the worker can do EscrowFinish
    5. If callback_url provided (agents): posts the fulfillment there too
    """
    # Look up the vault
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()

    if not vault:
        all_ids = [v.escrow_id for v in db.query(EscrowVault).all()]
        logger.error(f"❌ VAULT MISS: '{req.escrow_id}' not found. Stored: {all_ids}")
        raise HTTPException(
            status_code=404,
            detail=f"Project ID '{req.escrow_id}' not found. Check the ID is exactly correct."
        )

    if vault.status == "RELEASED":
        raise HTTPException(
            status_code=409,
            detail="This escrow has already been released."
        )

    if vault.status == "CANCELLED":
        raise HTTPException(
            status_code=409,
            detail="This escrow has been cancelled by the buyer."
        )

    # Deserialise buyer attachments from vault storage
    stored_buyer_attachments = None
    if vault.buyer_attachments:
        try:
            stored_buyer_attachments = json.loads(vault.buyer_attachments)
        except Exception:
            logger.warning("⚠️ Could not parse stored buyer attachments — proceeding without them.")

    # Run the AI audit — passes buyer spec docs + worker proof files to Gemini
    verdict_dict, model_used = await run_ai_audit(
        task               = vault.task_description,
        work               = req.work,
        buyer_attachments  = stored_buyer_attachments,
        worker_attachments = [a.dict() for a in req.worker_attachments] if req.worker_attachments else None,
    )

    is_approved       = verdict_dict.get("verdict") == "PASS"
    revealed_fulfillment = None

    if is_approved:
        revealed_fulfillment = vault.fulfillment
        vault.status         = "RELEASED"
        logger.info(f"✅ KEY RELEASED: escrow_id='{req.escrow_id}'")
    else:
        logger.info(f"❌ AUDIT FAILED: escrow_id='{req.escrow_id}' | score={verdict_dict.get('score')}")

    # Store audit result in vault for audit trail
    vault.ai_verdict = json.dumps(verdict_dict)
    vault.model_used = model_used
    db.commit()

    # Webhook for agent flows
    if is_approved and revealed_fulfillment and req.callback_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    req.callback_url,
                    json={
                        "escrow_id":   req.escrow_id,
                        "fulfillment": revealed_fulfillment,
                        "verdict":     verdict_dict,
                    },
                    timeout=10.0,
                )
            logger.info(f"📡 Webhook delivered to {req.callback_url}")
        except Exception as e:
            logger.warning(f"⚠️ Webhook delivery failed: {e}")

    return {
        "escrow_id":      req.escrow_id,
        "status":         "approved" if is_approved else "rejected",
        "verdict":        verdict_dict,
        "model_used":     model_used,
        "fulfillment":    revealed_fulfillment,
        "worker_address": vault.worker_address,
    }


# ---------------------------------------------------------------------------
# 12. DEX QUOTE ENDPOINT
# ---------------------------------------------------------------------------

# RLUSD issuer on XRPL mainnet (Ripple's official issuer address)
RLUSD_ISSUER   = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY = "RLUSD"

class QuoteRequest(BaseModel):
    worker_address: str    # Used to check trust line exists
    xrp_amount:     float  # The XRP amount coming out of escrow

@app.post("/dex/quote")
async def get_dex_quote(req: QuoteRequest):
    """
    Returns a live RLUSD quote for a given XRP amount using XRPL pathfinding.
    Also checks whether the worker's wallet has a RLUSD trust line set up.
    Called by the frontend when the worker selects RLUSD as payout currency.
    """
    drops = str(int(req.xrp_amount * 1_000_000))

    async with httpx.AsyncClient() as client:

        # --- 1. Check trust line ---
        trust_line_ok = False
        try:
            tl_res = await client.post(
                XRPL_URL,
                json={
                    "method": "account_lines",
                    "params": [{
                        "account": req.worker_address,
                        "peer":    RLUSD_ISSUER,
                    }]
                },
                timeout=10.0,
            )
            tl_data = tl_res.json()
            lines   = tl_data.get("result", {}).get("lines", [])
            trust_line_ok = any(
                l.get("currency") == RLUSD_CURRENCY
                for l in lines
            )
            logger.info(f"🔍 RLUSD trust line for {req.worker_address}: {trust_line_ok}")
        except Exception as e:
            logger.warning(f"⚠️ Trust line check failed: {e}")

        # --- 2. Pathfinding quote ---
        estimated_rlusd = None
        slippage_warning = False
        try:
            pf_res = await client.post(
                XRPL_URL,
                json={
                    "method": "ripple_path_find",
                    "params": [{
                        "source_account":     req.worker_address,
                        "source_amount":      drops,          # XRP in (drops)
                        "destination_account": req.worker_address,
                        "destination_amount": {
                            "currency": RLUSD_CURRENCY,
                            "issuer":   RLUSD_ISSUER,
                            "value":    "999999999",          # We want best available
                        },
                    }]
                },
                timeout=15.0,
            )
            pf_data     = pf_res.json()
            alt         = pf_data.get("result", {}).get("alternatives", [])

            if alt:
                # Best path is first alternative
                best          = alt[0]
                source_used   = best.get("source_amount", drops)
                dest_amount   = best.get("destination_amount", {})

                if isinstance(dest_amount, dict):
                    estimated_rlusd = float(dest_amount.get("value", 0))

                # Warn if path uses significantly more XRP than expected
                if isinstance(source_used, str):
                    actual_xrp = int(source_used) / 1_000_000
                    if actual_xrp > req.xrp_amount * 1.02:
                        slippage_warning = True

                logger.info(f"💱 DEX QUOTE: {req.xrp_amount} XRP → ~{estimated_rlusd} RLUSD")
            else:
                logger.warning("⚠️ No DEX paths found for XRP → RLUSD")

        except Exception as e:
            logger.warning(f"⚠️ Pathfinding failed: {e}")

    return {
        "xrp_amount":        req.xrp_amount,
        "estimated_rlusd":   round(estimated_rlusd, 4) if estimated_rlusd else None,
        "trust_line_ok":     trust_line_ok,
        "slippage_warning":  slippage_warning,
        "rlusd_issuer":      RLUSD_ISSUER,
        # If no trust line, instruct the worker to add one in Xaman
        "trust_line_instructions": None if trust_line_ok else (
            f"Your wallet does not have a RLUSD trust line. "
            f"In Xaman: go to Assets → Add Asset → search RLUSD → "
            f"select issuer {RLUSD_ISSUER} → Add Trust Line. "
            f"Then return here and try again."
        ),
    }
