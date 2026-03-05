import os
import httpx
import logging
import sys
import hashlib
import secrets
import json
import base64
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import resend

# Encryption for fulfillment keys at rest
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

# XRPL Imports
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import submit_and_wait as async_submit_and_wait
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx
from xrpl.models.transactions import EscrowFinish, OfferCreate
from xrpl.core.addresscodec import decode_seed
from xrpl.utils import xrp_to_drops

# XUMM SDK removed — using direct HTTP calls instead (no dependency conflict)

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
# 2b. MCP SERVER (optional — requires fastmcp in requirements.txt)
# ---------------------------------------------------------------------------
try:
    from mcp_server import mcp
    try:
        app.mount("/mcp", mcp.streamable_http_app())
        logger.info("✅ MCP server mounted at /mcp (streamable HTTP)")
    except AttributeError:
        app.mount("/mcp", mcp.sse_app())
        logger.info("✅ MCP server mounted at /mcp (SSE fallback)")
except ImportError:
    logger.debug("fastmcp not installed — MCP server disabled")

# ---------------------------------------------------------------------------
# 3. ROUTES — HEALTH, PLAYGROUND, DISCOVERY
# ---------------------------------------------------------------------------
@app.get("/")
@app.head("/")
def serve_ui(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content="""<!DOCTYPE html>
<html><head><meta http-equiv="refresh" content="0; url=/playground">
<title>AgentTrust Referee</title></head>
<body>Redirecting to <a href="/playground">playground</a>...</body>
</html>""", status_code=200)
    return {"status": "online", "version": "6.0", "service": "AgentTrust Referee", "playground": "/playground", "docs": "/docs"}

@app.get("/playground")
def serve_playground():
    path = "playground.html"
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content="""<!DOCTYPE html>
<html><head><title>AgentTrust Referee</title>
<style>body{font-family:monospace;background:#0d0f14;color:#e0e4f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
.box{text-align:center;padding:2rem;}a{color:#00e5a0;}h1{font-size:1.4rem;margin-bottom:1rem;}</style>
</head><body><div class="box">
<h1>AgentTrust Referee</h1>
<p>API is online. Full playground coming soon.</p>
<p style="margin-top:1rem"><a href="/docs">API Docs →</a></p>
<p><a href="https://www.cryptovault.co.uk">AgentTrust App →</a></p>
</div></body></html>""", status_code=200)

@app.get("/status")
@app.head("/status")
def health_check():
    return {"status": "online", "version": "6.0", "timestamp": datetime.now(timezone.utc)}

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Allow: /.well-known/",
        "Allow: /openapi.json",
        "Allow: /docs",
        "Allow: /playground",
        "Allow: /audit",
        "Allow: /status",
        "",
        "Sitemap: https://xrpl-referee.onrender.com/openapi.json",
    ])

@app.get("/.well-known/agent.json")
def serve_agent_json():
    path = ".well-known/agent.json"
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return {
        "schemaVersion": "1.0",
        "name": "AgentTrust Referee",
        "description": "Trustless AI verdict engine. Pay 0.1 XRP to /audit — get PASS/FAIL on any task. Optional XRPL escrow protocol available.",
        "url": "https://xrpl-referee.onrender.com",
        "agentVersion": "6.0.0",
        "protocolVersion": "0.4.0",
        "provider": {"organization": "AgentTrust Protocol", "url": "https://xrpl-referee.onrender.com"},
        "capabilities": {"streaming": False, "pushNotifications": False, "multimodal": True, "escrow": True, "autoFinish": True, "rlusd": True},
        "authentication": {
            "schemes": ["x-payment-hash"],
            "description": "Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR. Pass tx hash as x-payment-hash header."
        },
        "payment": {"currency": "XRP", "amount": "0.1", "destination": "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR", "network": "XRPL Mainnet"},
        "skills": [
            {"id": "standalone-audit",  "name": "AI Verdict",                   "description": "POST task+work+fee to /audit. Returns PASS/FAIL with score, summary, criteria.", "endpoint": "/audit",            "method": "POST", "tags": ["audit", "xrpl", "verification", "ai", "escrow"]},
            {"id": "escrow-create",     "name": "Create Escrow Vault",           "description": "Lock XRP or RLUSD in crypto-condition escrow gated by AI verdict.",              "endpoint": "/escrow/generate",  "method": "POST"},
            {"id": "escrow-evaluate",   "name": "Submit Work for Escrow Audit",  "description": "Seller submits proof. On PASS the referee auto-releases funds to seller.",        "endpoint": "/evaluate",         "method": "POST"},
        ],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
    }

@app.get("/.well-known/mcp/server-card.json")
def serve_mcp_server_card():
    """Smithery server card — lets Smithery skip scanning and use this metadata directly."""
    return {
        "name":        "AgentTrust Referee",
        "version":     "7.0.0",
        "description": (
            "Trustless AI task verification with automatic XRP payment release. "
            "Post a task spec and work submission — get PASS/FAIL from an AI referee. "
            "Escrowed XRP releases automatically to the worker on approval. "
            "Browse live XRP bounties on the AgentTrust marketplace. Built for autonomous agents."
        ),
        "url":         "https://xrpl-referee.onrender.com/mcp",
        "homepage":    "https://www.cryptovault.co.uk",
        "contact":     "hello@cryptovault.co.uk",
        "license":     "MIT",
        "transport":   ["http"],
        "tools": [
            {"name": "audit_task",               "description": "Verify completed work against a task spec for 0.1 XRP. Returns PASS/FAIL with score and feedback."},
            {"name": "create_escrow_vault",       "description": "Lock XRP or RLUSD in XRPL crypto-condition escrow gated by AI verdict."},
            {"name": "confirm_escrow_transaction","description": "Register an EscrowCreate tx hash to activate a vault."},
            {"name": "evaluate_escrow_work",      "description": "Submit proof of work. On PASS, payment releases automatically — no EscrowFinish needed."},
            {"name": "get_escrow_info",           "description": "Retrieve task spec, status, and attempts remaining for an escrow vault."},
            {"name": "list_marketplace_jobs",     "description": "Browse open XRP bounties agents can claim. Returns structured job data."},
            {"name": "get_rlusd_quote",           "description": "Get live XRP to RLUSD conversion quote via the XRPL DEX."},
            {"name": "get_xrp_price",             "description": "Get current live XRP/USD and XRP/GBP prices."},
        ],
        "tags": ["xrpl", "payments", "escrow", "ai-agent", "verification", "bounty", "autonomous", "web3"],
    }


def serve_ai_plugin():
    path = ".well-known/ai-plugin.json"
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return {
        "schema_version": "v1",
        "name_for_human": "AgentTrust Referee",
        "name_for_model": "agenttrust_referee",
        "description_for_human": (
            "Trustless AI task verification with automatic XRPL payment release. "
            "Post a task spec and a work submission — get a structured PASS/FAIL verdict. "
            "Optional escrow: lock XRP or RLUSD on-chain, funds release automatically on approval."
        ),
        "description_for_model": (
            "Use this tool to verify whether a seller has completed a task to specification and auto-release escrowed funds. "
            "POST the task requirements and the completed work to /audit with a 0.1 XRP fee hash (x-payment-hash header). "
            "Returns structured JSON: verdict (PASS/FAIL), score (0-100), summary, details, criteria_met, criteria_failed. "
            "task_category options: creative, code, bug_bounty, legal, supply_chain, data, default. "
            "Set require_consensus=true for high-stakes decisions requiring two-model agreement. "
            "For full escrow-gated payments: POST to /escrow/generate to lock funds, then POST to /evaluate to audit and auto-release. "
            "Supports XRP and RLUSD. Sellers may include XRPL transaction hashes as proof of on-chain delivery — the referee will verify them on the ledger."
        ),
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": "https://xrpl-referee.onrender.com/openapi.json"},
        "logo_url": "https://www.cryptovault.co.uk/logo.png",
        "contact_email": "hello@cryptovault.co.uk",
        "legal_info_url": "https://www.cryptovault.co.uk",
    }

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

engine       = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()


class PaymentLog(Base):
    __tablename__ = "payment_logs"
    id           = Column(Integer, primary_key=True, index=True)
    payment_hash = Column(String, unique=True, index=True, nullable=False)
    purpose      = Column(String, nullable=True)
    sender       = Column(String, nullable=True)
    amount_xrp   = Column(Float,  nullable=True)
    escrow_id    = Column(String, nullable=True)
    timestamp    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class EscrowVault(Base):
    __tablename__ = "escrow_vault"
    escrow_id         = Column(String, primary_key=True, index=True)
    condition         = Column(String,  nullable=False)
    fulfillment       = Column(String,  nullable=False)
    status            = Column(String,  default="LOCKED")
    # Currency — XRP or RLUSD
    currency          = Column(String,  default="XRP")          # NEW v6
    amount_xrp        = Column(Float,   nullable=True)          # kept for XRP flows
    amount_rlusd      = Column(Float,   nullable=True)          # NEW v6
    # Job metadata
    project_label     = Column(String,  nullable=True)
    buyer_name        = Column(String,  nullable=True)
    buyer_address     = Column(String,  nullable=True)
    buyer_email       = Column(String,  nullable=True)
    worker_email      = Column(String,  nullable=True)
    task_description  = Column(Text,    nullable=True)
    worker_address    = Column(String,  nullable=True)
    cancel_after_ts   = Column(DateTime, nullable=True)
    buyer_attachments = Column(Text,    nullable=True)
    # EscrowCreate tx
    escrow_tx_hash    = Column(String,  nullable=True)
    escrow_sequence   = Column(Integer, nullable=True)
    # Seller preferred payout currency
    seller_currency   = Column(String,  default="XRP")          # NEW v6
    # Audit result
    ai_verdict        = Column(Text,    nullable=True)
    model_used        = Column(String,  nullable=True)
    created_at        = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Auto-finish tracking
    auto_finish_hash  = Column(String,  nullable=True)          # NEW v6
    auto_finish_error = Column(String,  nullable=True)          # NEW v6
    # Delivery
    worker_submission   = Column(Text,    nullable=True)
    delivery_expires_at = Column(DateTime, nullable=True)
    delivery_status     = Column(String,   nullable=True)
    # URL snapshots — v7
    spec_link_snapshots     = Column(Text, nullable=True)   # JSON: [{url, content, fetched_at}]
    evidence_link_snapshots = Column(Text, nullable=True)   # JSON: [{url, content, fetched_at}]
    # Submission limits — v7
    submission_count  = Column(Integer, default=0)          # how many times seller has submitted
    max_submissions   = Column(Integer, default=3)          # configurable per-vault


Base.metadata.create_all(bind=engine)


def run_migrations():
    migrations = [
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_name          VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS task_description     TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_address       VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS amount_xrp           FLOAT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS cancel_after_ts      TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_attachments    TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS ai_verdict           TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS model_used           VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS created_at           TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS project_label        VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_address        VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS escrow_tx_hash       VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS escrow_sequence      INTEGER",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS purpose              VARCHAR",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS sender               VARCHAR",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS amount_xrp           FLOAT",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS escrow_id            VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_email          VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_email         VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_submission    TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS delivery_expires_at  TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS delivery_status      VARCHAR",
        # v6 columns
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS currency             VARCHAR DEFAULT 'XRP'",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS amount_rlusd         FLOAT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS seller_currency      VARCHAR DEFAULT 'XRP'",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS auto_finish_hash     VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS auto_finish_error    VARCHAR",
        # v7 columns
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS spec_link_snapshots     TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS evidence_link_snapshots TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS submission_count        INTEGER DEFAULT 0",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS max_submissions         INTEGER DEFAULT 3",
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
MIN_FEE_XRP     = 0.1

RLUSD_ISSUER   = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY = "RLUSD"
RLUSD_HEX      = "524C555344000000000000000000000000000000"

RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
RESEND_FROM          = os.getenv("RESEND_FROM", "noreply@cryptovault.co.uk")
DELIVERY_EXPIRY_DAYS = 7
SITE_URL             = os.getenv("SITE_URL", "https://www.cryptovault.co.uk")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY
    logger.info("✅ Resend email configured")
else:
    logger.warning("⚠️ RESEND_API_KEY missing — email notifications disabled")

# ---------------------------------------------------------------------------
# FULFILLMENT KEY ENCRYPTION (AES-256-GCM)
# ---------------------------------------------------------------------------
# Set FULFILLMENT_ENCRYPTION_KEY in your Render env vars.
# Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"
# A 64-char hex string = 32 bytes = AES-256 key.
# Without this env var, fulfillments are stored unencrypted (backwards-compatible).

_RAW_ENC_KEY = os.getenv("FULFILLMENT_ENCRYPTION_KEY", "")

def _get_aesgcm() -> "AESGCM | None":
    """Return an AESGCM instance if key + library are available, else None."""
    if not CRYPTO_AVAILABLE or not _RAW_ENC_KEY:
        return None
    try:
        key_bytes = bytes.fromhex(_RAW_ENC_KEY)
        if len(key_bytes) not in (16, 24, 32):
            logger.error("❌ FULFILLMENT_ENCRYPTION_KEY must be 32, 48, or 64 hex chars (16/24/32 bytes)")
            return None
        return AESGCM(key_bytes)
    except ValueError as e:
        logger.error(f"❌ FULFILLMENT_ENCRYPTION_KEY is not valid hex: {e}")
        return None

def encrypt_fulfillment(plaintext: str) -> str:
    """
    Encrypt fulfillment hex string.
    Returns: "enc:v1:<base64(nonce+ciphertext)>" or original string if no key configured.
    """
    aesgcm = _get_aesgcm()
    if not aesgcm:
        return plaintext  # fallback: store plaintext if no key configured
    nonce      = secrets.token_bytes(12)   # 96-bit nonce for GCM
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    encoded    = base64.b64encode(nonce + ciphertext).decode()
    return f"enc:v1:{encoded}"

def decrypt_fulfillment(stored: str) -> str:
    """
    Decrypt a fulfillment string.
    Handles both encrypted ("enc:v1:...") and legacy plaintext values.
    """
    if not stored.startswith("enc:v1:"):
        return stored  # legacy plaintext — return as-is
    aesgcm = _get_aesgcm()
    if not aesgcm:
        raise ValueError("Fulfillment is encrypted but FULFILLMENT_ENCRYPTION_KEY is not set.")
    try:
        raw        = base64.b64decode(stored[7:])  # strip "enc:v1:"
        nonce      = raw[:12]
        ciphertext = raw[12:]
        return aesgcm.decrypt(nonce, ciphertext, None).decode()
    except Exception as e:
        raise ValueError(f"Failed to decrypt fulfillment: {e}")

if _RAW_ENC_KEY and CRYPTO_AVAILABLE:
    aesgcm_test = _get_aesgcm()
    if aesgcm_test:
        logger.info("🔐 Fulfillment key encryption: ACTIVE (AES-256-GCM)")
    else:
        logger.warning("⚠️ Fulfillment key encryption: MISCONFIGURED — check FULFILLMENT_ENCRYPTION_KEY")
else:
    logger.warning("⚠️ Fulfillment key encryption: DISABLED — set FULFILLMENT_ENCRYPTION_KEY in env vars")

# ---------------------------------------------------------------------------
# SUBMISSION LIMITS
# ---------------------------------------------------------------------------
DEFAULT_MAX_SUBMISSIONS = int(os.getenv("DEFAULT_MAX_SUBMISSIONS", "3"))
EXTRA_ATTEMPT_FEE_XRP   = 0.05  # charged per extra submission beyond the limit

try:
    seed = os.getenv("XRPL_SEED")
    if not seed:
        raise ValueError("XRPL_SEED not found.")
    _, algo        = decode_seed(seed)
    referee_wallet = Wallet.from_seed(seed, algorithm=algo)
    logger.info(f"🚀 REFEREE WALLET ACTIVE: {referee_wallet.address}")
except Exception as e:
    logger.error(f"STARTUP ERROR (wallet): {e}")
    referee_wallet = None

xumm_api_key    = os.getenv("XUMM_API_KEY")
xumm_api_secret = os.getenv("XUMM_API_SECRET")

if xumm_api_key:
    logger.info(f"✅ XUMM_API_KEY found (starts: {xumm_api_key[:4]}...)")
else:
    logger.error("❌ XUMM_API_KEY missing!")

async def xumm_create_payload(txjson: dict) -> dict:
    """Create a XUMM payload via direct REST API call. Returns {nextUrl, uuid, qr}."""
    if not xumm_api_key or not xumm_api_secret:
        raise HTTPException(status_code=500, detail="XUMM API credentials not configured.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            "https://xumm.app/api/v1/platform/payload",
            json={"txjson": txjson},
            headers={
                "X-API-Key":    xumm_api_key,
                "X-API-Secret": xumm_api_secret,
                "Content-Type": "application/json",
            },
        )
        if not res.is_success():
            raise HTTPException(status_code=500, detail=f"XUMM API error: {res.text}")
        data = res.json()
        return {
            "nextUrl": data["next"]["always"],
            "uuid":    data["uuid"],
            "qr":      data["refs"]["qr_png"],
        }

async def xumm_get_payload(uuid: str) -> dict:
    """Get XUMM payload status via direct REST API call. Returns {signed, tx_hash, signer}."""
    if not xumm_api_key or not xumm_api_secret:
        raise HTTPException(status_code=500, detail="XUMM API credentials not configured.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"https://xumm.app/api/v1/platform/payload/{uuid}",
            headers={
                "X-API-Key":    xumm_api_key,
                "X-API-Secret": xumm_api_secret,
            },
        )
        if not res.is_success():
            raise HTTPException(status_code=500, detail=f"XUMM API error: {res.text}")
        data   = res.json()
        signed  = data["meta"]["signed"]
        tx_hash = data["response"].get("txid")  if signed else None
        signer  = data["response"].get("account") if signed else None
        return {"signed": signed, "tx_hash": tx_hash, "signer": signer}

# Verify XUMM connectivity at startup
async def _verify_xumm():
    if not xumm_api_key or not xumm_api_secret:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(
                "https://xumm.app/api/v1/platform/ping",
                headers={"X-API-Key": xumm_api_key, "X-API-Secret": xumm_api_secret},
            )
            if res.is_success():
                name = res.json().get("application", {}).get("name", "unknown")
                logger.info(f"🔌 XUMM SDK connected: {name}")
            else:
                logger.warning(f"⚠️ XUMM ping failed: {res.status_code}")
    except Exception as e:
        logger.warning(f"⚠️ XUMM ping error: {e}")

import asyncio as _asyncio
try:
    loop = _asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(_verify_xumm())
    else:
        loop.run_until_complete(_verify_xumm())
except Exception:
    pass


# ---------------------------------------------------------------------------
# 6. PYDANTIC MODELS
# ---------------------------------------------------------------------------
class Attachment(BaseModel):
    filename:  str
    mime_type: str
    data:      str

class EscrowSetupRequest(BaseModel):
    escrow_id:          str
    fee_hash:           str
    project_label:      Optional[str]   = None
    buyer_name:         str
    buyer_address:      str
    buyer_email:        Optional[str]   = None
    worker_email:       Optional[str]   = None
    task_description:   str
    worker_address:     str
    # Currency selection — XRP (default) or RLUSD
    currency:           str             = "XRP"
    amount_xrp:         Optional[float] = None
    amount_rlusd:       Optional[float] = None
    # Seller's preferred payout currency
    seller_currency:    str             = "XRP"
    cancel_after_hrs:   int             = 168
    buyer_attachments:  Optional[list[Attachment]] = None
    # Spec links — up to 3 URLs the buyer provides as reference material
    spec_links:         Optional[list[str]] = None
    # How many submission attempts the seller gets (default 3, buyer can raise for complex work)
    max_submissions:    int             = 3

class AuditRequest(BaseModel):
    escrow_id:           str
    work:                str
    worker_attachments:  Optional[list[Attachment]] = None
    callback_url:        Optional[str]  = None
    task_category:       str            = "default"
    require_consensus:   bool           = False
    # Evidence links — up to 3 URLs the seller provides as proof
    evidence_links:      Optional[list[str]] = None

class StandaloneAuditRequest(BaseModel):
    task:                str
    work:                str
    fee_hash:            Optional[str]  = None
    attachments:         Optional[list[Attachment]] = None
    task_category:       str            = "default"
    require_consensus:   bool           = False

class XummPayloadRequest(BaseModel):
    txjson: dict

class QuoteRequest(BaseModel):
    worker_address:  str
    xrp_amount:      float
    seller_currency: str = "XRP"


# ---------------------------------------------------------------------------
# 7. FEE VERIFICATION
# ---------------------------------------------------------------------------
async def verify_fee_payment(fee_hash: str, escrow_id: str, db: Session, min_xrp: float = None) -> dict:
    required_xrp = min_xrp if min_xrp is not None else MIN_FEE_XRP
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == fee_hash).first()
    if already_used:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Payment hash already used for escrow '{already_used.escrow_id}' "
                f"on {already_used.timestamp.strftime('%Y-%m-%d %H:%M UTC')}."
            ),
        )

    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=fee_hash))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ledger lookup failed: {str(e)}")

    if not tx_res.is_successful():
        raise HTTPException(status_code=402, detail="Transaction hash not found on the XRPL ledger.")

    body    = tx_res.result
    tx_data = body.get("tx_json") or body.get("tx") or body
    meta    = body.get("meta") or body.get("metaData") or {}

    tx_type    = tx_data.get("TransactionType", "")
    dest       = str(tx_data.get("Destination", "")).strip()
    sender     = tx_data.get("Account", "unknown")
    raw_amount = (
        meta.get("delivered_amount")
        or meta.get("DeliveredAmount")
        or tx_data.get("Amount")
        or "0"
    )

    logger.info(f"🔍 LEDGER: type={tx_type} | dest={dest} | amount={raw_amount} | from={sender}")

    if tx_type != "Payment":
        raise HTTPException(status_code=400, detail=f"Transaction is '{tx_type}', not a Payment.")
    if dest.lower() != PROTOCOL_WALLET.lower():
        raise HTTPException(status_code=402, detail=f"Wrong destination. Expected {PROTOCOL_WALLET}, got {dest}.")
    if isinstance(raw_amount, dict):
        raise HTTPException(status_code=400, detail="Protocol fees must be paid in XRP, not issued currency.")

    amount_xrp = round(int(raw_amount) / 1_000_000, 6)
    if amount_xrp < (required_xrp - 0.000001):
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient fee. Required ≥{required_xrp} XRP, received {amount_xrp:.6f} XRP.",
        )

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
# 8. TRUSTLINE CHECK
# ---------------------------------------------------------------------------
async def check_rlusd_trustline(address: str) -> bool:
    """Returns True if the address has a RLUSD trustline with the official issuer."""
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                XRPL_URL,
                json={"method": "account_lines", "params": [{"account": address, "peer": RLUSD_ISSUER}]},
                timeout=10.0,
            )
            lines = res.json().get("result", {}).get("lines", [])
            return any(l.get("currency") == RLUSD_CURRENCY for l in lines)
    except Exception as e:
        logger.warning(f"⚠️ Trustline check failed for {address}: {e}")
        return False


# ---------------------------------------------------------------------------
# 9. AUTO-FINISH + SERVER-SIDE DEX SWAP
# ---------------------------------------------------------------------------
def _calc_finish_fee(fulfillment_hex: str) -> str:
    """
    XRPL formula: 330 + ceil(fulfillment_bytes / 16) * 10 drops.
    Add a generous buffer to avoid insufficient-fee rejections.
    """
    try:
        byte_len = len(bytes.fromhex(fulfillment_hex))
    except Exception:
        byte_len = 100
    base_fee = 330 + (((byte_len + 15) // 16) * 10)
    return str(base_fee + 100)  # small buffer


async def auto_finish_escrow(
    escrow_id:   str,
    sequence:    int,
    owner:       str,
    fulfillment: str,
    condition:   str,
    worker_addr: str,
    db_session_factory,
):
    """
    Submits EscrowFinish on-chain using the referee wallet.
    Referee pays the network fee (~0.005 XRP) from protocol income.
    Seller receives the exact escrowed amount — no deductions.
    After successful finish, triggers DEX swap if seller wants RLUSD.
    """
    if not referee_wallet:
        logger.error(f"❌ AUTO-FINISH: referee wallet not loaded for {escrow_id}")
        return

    finish_fee = _calc_finish_fee(fulfillment)
    logger.info(f"🔄 AUTO-FINISH starting: {escrow_id} | seq={sequence} | fee={finish_fee} drops")

    try:
        client    = AsyncJsonRpcClient(XRPL_URL)
        finish_tx = EscrowFinish(
            account        = referee_wallet.address,
            owner          = owner,
            offer_sequence = sequence,
            fulfillment    = fulfillment.upper(),
            condition      = condition.upper(),
            fee            = finish_fee,
        )
        result   = await async_submit_and_wait(finish_tx, client, referee_wallet)
        tx_hash  = result.result.get("hash", "unknown")
        logger.info(f"✅ AUTO-FINISH SUCCESS: {escrow_id} | hash={tx_hash[:16]}... | worker={worker_addr}")

        # Persist the finish hash
        db = db_session_factory()
        try:
            vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
            if vault:
                vault.auto_finish_hash = tx_hash
                db.commit()
        finally:
            db.close()

    except Exception as e:
        logger.error(f"❌ AUTO-FINISH FAILED for {escrow_id}: {e}")
        db = db_session_factory()
        try:
            vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
            if vault:
                vault.auto_finish_error = str(e)[:500]
                db.commit()
        finally:
            db.close()


async def server_side_dex_swap(
    escrow_id:   str,
    worker_addr: str,
    xrp_amount:  float,
    db_session_factory,
):
    """
    After auto-finish delivers XRP to worker, triggers an OfferCreate
    on behalf of the worker (via Xaman webhook, since we cannot sign for them).
    Instead we store a flag so the frontend can offer one-tap Xaman swap,
    or agents can call /dex/swap directly.

    NOTE: A true server-side swap would require signing with the worker's key,
    which we never hold. So this function fetches a live quote and stores it,
    then the frontend presents a single-tap Xaman swap. For agent flows the
    fulfillment + quote are returned in the /evaluate response.
    """
    logger.info(f"💱 Fetching post-finish DEX quote for {escrow_id}")
    try:
        async with httpx.AsyncClient() as client:
            pf_res = await client.post(
                XRPL_URL,
                json={
                    "method": "ripple_path_find",
                    "params": [{
                        "source_account":      worker_addr,
                        "source_amount":       str(int(xrp_amount * 1_000_000)),
                        "destination_account": worker_addr,
                        "destination_amount":  {
                            "currency": RLUSD_CURRENCY,
                            "issuer":   RLUSD_ISSUER,
                            "value":    "999999999",
                        },
                    }],
                },
                timeout=15.0,
            )
            alt = pf_res.json().get("result", {}).get("alternatives", [])
            if alt:
                dest = alt[0].get("destination_amount", {})
                estimated = float(dest.get("value", 0)) if isinstance(dest, dict) else 0
                logger.info(f"💱 Post-finish DEX quote: {xrp_amount} XRP → ~{estimated:.4f} RLUSD for {escrow_id}")
                return estimated
    except Exception as e:
        logger.warning(f"⚠️ Post-finish DEX quote failed for {escrow_id}: {e}")
    return None


# ---------------------------------------------------------------------------
# 10. EMAIL HELPERS
# ---------------------------------------------------------------------------
def _email_styles() -> str:
    return """
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f4f6fb;margin:0;padding:40px 20px;color:#0d0d12;}
        .card{background:#fff;border-radius:16px;max-width:560px;
              margin:0 auto;padding:40px;box-shadow:0 4px 24px rgba(0,0,0,.08);}
        .logo{font-size:1.4rem;font-weight:700;margin-bottom:28px;color:#0d0d12;}
        .logo span{color:#0066FF;}
        h1{font-size:1.4rem;margin:0 0 8px;}
        p{color:#5c5c6e;line-height:1.6;margin:0 0 16px;font-size:.95rem;}
        .btn{display:inline-block;background:#0066FF;color:#fff;
             text-decoration:none;padding:14px 28px;border-radius:10px;
             font-weight:700;font-size:.95rem;margin:8px 0 24px;}
        .detail{font-size:.85rem;background:#f8f9fc;border-radius:8px;
                padding:12px 16px;margin-bottom:12px;}
        .detail span{color:#9999aa;}
        .footer{font-size:.8rem;color:#9999aa;margin-top:24px;
                padding-top:20px;border-top:1px solid #eee;}
    """


async def send_worker_receipt_email(
    worker_email: str,
    worker_name:  str,
    escrow_id:    str,
    buyer_name:   str,
    amount:       float,
    currency:     str,
    task_preview: str,
    deadline:     str,
):
    if not RESEND_API_KEY or not worker_email:
        return
    worker_url   = f"{SITE_URL}?worker={escrow_id}"
    preview_safe = task_preview[:300] + ("…" if len(task_preview) > 300 else "")
    amount_str   = f"{amount} {currency}"

    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      worker_email,
            "subject": f"📋 You have a new job waiting — {escrow_id}",
            "html": f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>{_email_styles()}
.code{{font-family:'Courier New',monospace;font-size:1.3rem;font-weight:800;
       letter-spacing:.06em;background:#f0f4ff;border:1px solid #c8d8ff;
       border-radius:10px;padding:14px 20px;display:inline-block;
       color:#0033cc;margin:12px 0 20px;}}
.task-box{{background:#f8f9fc;border-left:3px solid #0066FF;border-radius:0 8px 8px 0;
           padding:12px 16px;font-size:.88rem;color:#333;line-height:1.65;margin-bottom:20px;}}
</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>You have a new job</h1>
  <p>Hi{' ' + worker_name if worker_name else ''}, <strong>{buyer_name}</strong> has locked
     <strong>{amount_str}</strong> in escrow for you. Complete the work,
     submit your proof, and payment is released automatically on AI approval — no further action needed.</p>
  <div class="detail"><span>Your Receipt Code</span></div>
  <div class="code">{escrow_id}</div>
  <div class="detail"><span>Amount locked for you</span><br><strong>{amount_str}</strong></div>
  <div class="detail"><span>Deadline</span><br><strong>{deadline}</strong></div>
  <p style="font-size:.85rem;font-weight:700;margin-bottom:.4rem;color:#0d0d12;">Task brief:</p>
  <div class="task-box">{preview_safe}</div>
  <a href="{worker_url}" class="btn">Submit Your Work →</a>
  <p style="font-size:.85rem;">Enter your receipt code <strong>{escrow_id}</strong> on the
     Seller tab to load the full job details and submit your work. Payment arrives in your
     wallet automatically on approval.</p>
  <div class="footer">
    Payment is held securely on the XRP Ledger and released automatically when
    the AI referee approves your submission. No manual claim required.<br><br>
    AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a>
  </div>
</div></body></html>""",
        })
        logger.info(f"📧 Seller receipt email sent to {worker_email} for {escrow_id}")
    except Exception as e:
        logger.error(f"❌ Seller email failed for {escrow_id}: {e}")


async def send_delivery_email(
    buyer_email: str,
    buyer_name:  str,
    escrow_id:   str,
    amount:      float,
    currency:    str,
    verdict:     dict,
):
    if not RESEND_API_KEY or not buyer_email:
        return
    collect_url = f"{SITE_URL}?collect={escrow_id}"
    score       = verdict.get("score", "—")
    summary     = verdict.get("summary", "Work verified by AI referee.")
    amount_str  = f"{amount} {currency}"

    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      buyer_email,
            "subject": f"✅ Your delivery is ready — {escrow_id}",
            "html": f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>{_email_styles()}
.verdict-box{{background:#f0faf5;border:1px solid #00B97A;border-radius:10px;
              padding:16px 20px;margin:20px 0;}}
.verdict-box .score{{font-size:1.5rem;font-weight:800;color:#00B97A;}}
.verdict-box .summary{{color:#0d6644;font-size:.9rem;margin-top:4px;}}
</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>Your delivery is ready to collect</h1>
  <p>Hi {buyer_name}, the work for your escrow has passed AI verification
     and payment has been automatically released to the seller.</p>
  <div class="detail"><span>Escrow ID</span><br><strong>{escrow_id}</strong></div>
  <div class="detail"><span>Amount released</span><br><strong>{amount_str}</strong></div>
  <div class="verdict-box">
    <div class="score">✓ PASS &nbsp;{score}/100</div>
    <div class="summary">{summary}</div>
  </div>
  <a href="{collect_url}" class="btn">Collect Your Delivery →</a>
  <p>Click above to view and download everything the seller submitted.</p>
  <div class="footer">
    ⏳ This delivery link expires in <strong>7 days</strong>.<br><br>
    AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a>
  </div>
</div></body></html>""",
        })
        logger.info(f"📧 Buyer delivery email sent to {buyer_email} for {escrow_id}")
    except Exception as e:
        logger.error(f"❌ Buyer delivery email failed for {escrow_id}: {e}")


# ---------------------------------------------------------------------------
# 10b. URL SNAPSHOT FETCHER
# ---------------------------------------------------------------------------
# Blocked TLDs / patterns — private networks, localhost, cloud metadata
_BLOCKED_URL_PATTERNS = [
    r"^https?://localhost",
    r"^https?://127\.",
    r"^https?://10\.",
    r"^https?://192\.168\.",
    r"^https?://172\.(1[6-9]|2[0-9]|3[01])\.",
    r"^https?://169\.254\.",           # link-local / AWS metadata
    r"^https?://metadata\.google",
    r"^https?://0\.",
    r"file://",
]

# Max content we inject per URL (chars) — keeps token usage sane
_URL_SNAPSHOT_MAX_CHARS = 8_000

# Suspiciously long content that might be prompt injection
_INJECTION_MARKERS = [
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "you are now",
    "new system prompt",
    "override your instructions",
    "forget everything",
    "act as",
]

async def fetch_url_snapshot(url: str) -> dict:
    """
    Fetch a URL and return a snapshot dict:
    {url, content, content_type, fetched_at, error}

    Security:
    - Blocks private/loopback/metadata IPs
    - Caps content at _URL_SNAPSHOT_MAX_CHARS
    - Strips HTML tags to plain text
    - Detects and neutralises prompt injection attempts
    - 10s timeout, follows up to 3 redirects
    """
    import re as _re
    from datetime import datetime, timezone

    result = {"url": url, "content": None, "content_type": None,
              "fetched_at": datetime.now(timezone.utc).isoformat(), "error": None}

    # Basic URL validation
    if not url.startswith(("http://", "https://")):
        result["error"] = "Only http/https URLs are supported."
        return result

    for pattern in _BLOCKED_URL_PATTERNS:
        if _re.search(pattern, url, _re.IGNORECASE):
            result["error"] = "URL resolves to a blocked network range."
            return result

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": "AgentTrust-Referee/1.0 (evidence-snapshot)"},
        ) as client:
            resp = await client.get(url)
            content_type = resp.headers.get("content-type", "")
            result["content_type"] = content_type

            # Only process text content — no binary, no PDFs (those go via attachments)
            if not any(t in content_type for t in ["text/", "application/json", "application/xml"]):
                result["error"] = f"Non-text content type '{content_type[:60]}' — use file attachments for binary content."
                return result

            raw = resp.text

            # Strip HTML tags to plain text
            if "text/html" in content_type:
                # Remove scripts and styles entirely
                raw = _re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", raw,
                              flags=_re.DOTALL | _re.IGNORECASE)
                raw = _re.sub(r"<[^>]+>", " ", raw)
                raw = _re.sub(r"\s{3,}", "\n", raw).strip()

            # Truncate
            if len(raw) > _URL_SNAPSHOT_MAX_CHARS:
                raw = raw[:_URL_SNAPSHOT_MAX_CHARS] + f"\n[... truncated at {_URL_SNAPSHOT_MAX_CHARS} chars]"

            # Prompt injection detection — neutralise rather than reject
            lower = raw.lower()
            injection_detected = any(marker in lower for marker in _INJECTION_MARKERS)
            if injection_detected:
                logger.warning(f"⚠️ Potential prompt injection in URL snapshot: {url}")
                raw = "[CONTENT SANITISED: this page contained text that could interfere with AI evaluation. It has been removed. The AI will evaluate based on the seller's written submission only.]"

            result["content"] = raw

    except httpx.TimeoutException:
        result["error"] = "Request timed out after 10 seconds."
    except httpx.TooManyRedirects:
        result["error"] = "Too many redirects."
    except Exception as e:
        result["error"] = f"Fetch failed: {str(e)[:120]}"

    return result


async def fetch_url_snapshots(urls: list[str]) -> list[dict]:
    """Fetch up to 3 URLs concurrently."""
    import asyncio
    if not urls:
        return []
    urls = [u.strip() for u in urls[:3] if u and u.strip()]  # hard cap at 3
    return await asyncio.gather(*[fetch_url_snapshot(u) for u in urls])



DOMAIN_PROMPTS = {
    "bug_bounty": (
        "You are auditing a security bug bounty submission. Be extremely rigorous. "
        "A PASS should only be given if: (1) the vulnerability is clearly real and reproducible, "
        "(2) the proof-of-concept demonstrates actual impact, (3) the submission includes steps to reproduce. "
        "Treat any vague or unverifiable claims as FAIL. The financial stakes may be very high."
    ),
    "legal": (
        "You are auditing a legal settlement deliverable. Be precise and literal. "
        "Only evaluate whether the submitted documents/text satisfy the exact criteria stated. "
        "Do not infer intent. If a requirement is ambiguous, note it in details but do not penalise. "
        "You are not giving legal advice — you are verifying whether stated conditions have been met."
    ),
    "supply_chain": (
        "You are auditing a supply chain compliance deliverable. "
        "Check for: document completeness, consistency of dates/quantities/parties, "
        "presence of required fields (e.g. HS codes, port of entry, consignee details). "
        "Flag any discrepancies between the task spec and submitted documents."
    ),
    "real_estate": (
        "You are auditing a real estate transaction milestone. "
        "Evaluate whether submitted documents satisfy the conditions stated in the task spec. "
        "Flag missing documents, date inconsistencies, or unresolved conditions."
    ),
    "creative": (
        "You are auditing a creative deliverable (writing, design, code, media). "
        "Evaluate quality, completeness, and adherence to the stated brief. "
        "For writing: check word count, tone, structure, and coverage of required topics. "
        "Be fair but hold the work to the standard the buyer specified."
    ),
    "code": (
        "You are auditing a software development deliverable. "
        "Evaluate: does the submitted work address the stated requirements? "
        "Check for completeness, correctness of described approach, presence of required components."
    ),
    "data": (
        "You are auditing a data or research deliverable. "
        "Evaluate completeness of the dataset/report, format compliance, coverage of required fields. "
        "Check that the volume, structure, and content match what was specified."
    ),
    "default": (
        "You are an autonomous escrow auditor — a neutral, objective third party determining "
        "whether a seller has fulfilled a task specification well enough to be paid."
    ),
}


# ---------------------------------------------------------------------------
# 11b. XRPL TRANSACTION HASH AUTO-VERIFICATION
# ---------------------------------------------------------------------------
import re as _re

_XRPL_HASH_RE = _re.compile(r'\b([0-9A-Fa-f]{64})\b')

async def extract_and_verify_xrpl_hashes(text: str) -> str | None:
    """
    Scan submission text for 64-char hex strings (XRPL tx hashes).
    Look up each one on the ledger and return a formatted context block
    to inject into the AI prompt, or None if no hashes found.
    """
    hashes = list(dict.fromkeys(_XRPL_HASH_RE.findall(text)))[:3]  # deduplicate, cap at 3
    if not hashes:
        return None

    results = []
    client  = AsyncJsonRpcClient(XRPL_URL)

    for h in hashes:
        try:
            tx_res = await client.request(Tx(transaction=h.upper()))
            if not tx_res.is_successful():
                results.append(f"Hash {h}: not found on ledger.")
                continue

            body    = tx_res.result
            tx_data = body.get("tx_json") or body.get("tx") or body
            meta    = body.get("meta") or body.get("metaData") or {}

            tx_type  = tx_data.get("TransactionType", "unknown")
            account  = tx_data.get("Account", "—")
            dest     = tx_data.get("Destination", "—")
            amount   = tx_data.get("Amount", "—")
            ledger   = body.get("ledger_index", "—")
            result   = meta.get("TransactionResult", "—")

            # Human-readable amount
            if isinstance(amount, dict):
                amt_str = f"{amount.get('value')} {amount.get('currency')} (issuer: {amount.get('issuer','')})"
            elif isinstance(amount, str) and amount.isdigit():
                amt_str = f"{int(amount)/1_000_000:.6f} XRP"
            else:
                amt_str = str(amount)

            # NFT-specific fields
            nft_fields = ""
            if tx_type in ("NFTokenMint", "NFTokenBurn", "NFTokenCreateOffer",
                           "NFTokenAcceptOffer", "NFTokenCancelOffer"):
                nft_id = tx_data.get("NFTokenID") or meta.get("nftoken_id", "")
                if nft_id:
                    nft_fields = f"\n  NFTokenID: {nft_id}"
                uri = tx_data.get("URI", "")
                if uri:
                    try:
                        import binascii
                        nft_fields += f"\n  URI: {binascii.unhexlify(uri).decode(errors='replace')}"
                    except Exception:
                        pass

            results.append(
                f"Hash {h}:\n"
                f"  Type: {tx_type}\n"
                f"  From: {account}\n"
                f"  To: {dest}\n"
                f"  Amount: {amt_str}\n"
                f"  Ledger: {ledger}\n"
                f"  Result: {result}{nft_fields}"
            )
        except Exception as e:
            results.append(f"Hash {h}: ledger lookup failed ({str(e)[:80]}).")

    if not results:
        return None

    return (
        "\nON-CHAIN EVIDENCE (auto-verified from XRPL ledger):\n"
        + "\n\n".join(results)
        + "\n\nNote: use this verified on-chain data as authoritative proof. "
          "If the task required an on-chain transfer, NFT mint, or payment, "
          "verify the above matches the task requirements.\n"
    )


# ---------------------------------------------------------------------------
# 12. AI AUDIT ENGINE
# ---------------------------------------------------------------------------
async def run_ai_audit(
    task:                    str,
    work:                    str,
    buyer_attachments:       list = None,
    worker_attachments:      list = None,
    task_category:           str  = "default",
    require_consensus:       bool = False,
    spec_link_snapshots:     list = None,
    evidence_link_snapshots: list = None,
) -> tuple[dict, str]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing from environment.")

    candidates = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    domain_context = DOMAIN_PROMPTS.get(task_category, DOMAIN_PROMPTS["default"])

    prompt_text = (
        f"{domain_context}\n\n"
        "Your analysis must be:\n"
        "- STRICT: only pass work that genuinely meets the stated requirements\n"
        "- SPECIFIC: reference exact requirements when citing criteria met or failed\n"
        "- FAIR: do not penalise for things not stated in the requirements\n"
        "- HONEST: a low score with clear feedback is more valuable than a generous pass\n\n"
        "Your response must be valid JSON and nothing else — no markdown, no backticks, no preamble.\n\n"
        f"TASK REQUIREMENTS:\n{task}\n"
    )

    if buyer_attachments:
        prompt_text += f"\nThe buyer has provided {len(buyer_attachments)} supporting document(s) as part of the task specification.\n"

    # Spec links — buyer-provided reference URLs, snapshotted at vault creation
    if spec_link_snapshots:
        ok = [s for s in spec_link_snapshots if s.get("content")]
        if ok:
            prompt_text += f"\nThe buyer provided {len(ok)} reference URL(s) as part of the specification:\n"
            for snap in ok:
                prompt_text += f"\n--- REFERENCE URL: {snap['url']} (fetched {snap['fetched_at'][:10]}) ---\n"
                prompt_text += snap["content"] + "\n--- END REFERENCE URL ---\n"

    prompt_text += f"\nWORK SUBMITTED:\n{work}\n"

    # Auto-detect and verify any XRPL transaction hashes in the submission
    xrpl_evidence = await extract_and_verify_xrpl_hashes(work)
    if xrpl_evidence:
        prompt_text += xrpl_evidence

    # Evidence links — seller-provided proof URLs, snapshotted at submission time
    if evidence_link_snapshots:
        ok = [s for s in evidence_link_snapshots if s.get("content")]
        failed = [s for s in evidence_link_snapshots if s.get("error")]
        if ok:
            prompt_text += f"\nThe seller provided {len(ok)} evidence URL(s) as supporting proof:\n"
            for snap in ok:
                prompt_text += f"\n--- EVIDENCE URL: {snap['url']} (fetched {snap['fetched_at'][:10]}) ---\n"
                prompt_text += snap["content"] + "\n--- END EVIDENCE URL ---\n"
        if failed:
            prompt_text += f"\nNote: {len(failed)} evidence URL(s) could not be fetched: "
            prompt_text += ", ".join(f"{s['url']} ({s['error']})" for s in failed) + "\n"
            prompt_text += "Evaluate based on what was successfully retrieved and the written submission.\n"

    if worker_attachments:
        prompt_text += f"\nThe seller has submitted {len(worker_attachments)} document(s) as proof of work.\n"

    prompt_text += (
        "\nRespond with ONLY this JSON object:\n"
        "{\n"
        '  "verdict": "PASS" or "FAIL",\n'
        '  "score": <integer 0-100>,\n'
        '  "summary": "<one sentence conclusion>",\n'
        '  "details": "<2-3 sentences of specific feedback>",\n'
        '  "criteria_met": ["<requirement 1>", "..."],\n'
        '  "criteria_failed": ["<requirement 1>", "..."]\n'
        "}"
    )

    parts = []

    if buyer_attachments:
        for att in buyer_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({"inline_data": {"mime_type": mime, "data": att.get("data")}})

    if worker_attachments:
        for att in worker_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({"inline_data": {"mime_type": mime, "data": att.get("data")}})

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
                    data         = res.json()
                    raw_text     = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    clean        = raw_text.replace("```json", "").replace("```", "").strip()
                    verdict_dict = json.loads(clean)
                    verdict_dict["verdict"] = str(verdict_dict.get("verdict", "FAIL")).strip().upper()

                    logger.info(f"✅ AI VERDICT: {verdict_dict['verdict']} | score={verdict_dict.get('score')} | model={model_id}")

                    if require_consensus:
                        second_candidates = [m for m in candidates if m != model_id]
                        for model_2 in second_candidates:
                            try:
                                url2 = (
                                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                                    f"{model_2}:generateContent?key={api_key}"
                                )
                                res2 = await client.post(url2, json=payload, timeout=60.0)
                                if res2.status_code == 200:
                                    raw2  = res2.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                                    clean2 = raw2.replace("```json", "").replace("```", "").strip()
                                    v2     = json.loads(clean2)
                                    v2["verdict"] = str(v2.get("verdict", "FAIL")).strip().upper()
                                    if v2["verdict"] != verdict_dict["verdict"]:
                                        logger.warning(f"⚖️ CONSENSUS SPLIT — defaulting FAIL")
                                        verdict_dict["verdict"]   = "FAIL"
                                        verdict_dict["summary"]   = f"Models disagreed. Conservative FAIL applied."
                                        verdict_dict["consensus"] = False
                                        verdict_dict["models"]    = [model_id, model_2]
                                    else:
                                        verdict_dict["consensus"] = True
                                        verdict_dict["models"]    = [model_id, model_2]
                                    break
                            except Exception as e2:
                                logger.warning(f"Consensus model {model_2} failed: {e2}")

                    return verdict_dict, model_id
                else:
                    logger.warning(f"Model {model_id} HTTP {res.status_code}")
            except Exception as e:
                logger.warning(f"Model {model_id} failed: {e}")
                continue

    raise Exception("AI Gateway Failure: all models exhausted.")


# ---------------------------------------------------------------------------
# 13. STANDALONE AUDIT ENDPOINT
# ---------------------------------------------------------------------------
@app.post("/audit")
async def standalone_audit(
    req: StandaloneAuditRequest,
    x_payment_hash: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    fee_hash = (req.fee_hash or x_payment_hash or "").strip()
    if not fee_hash:
        raise HTTPException(
            status_code=402,
            detail="Payment required. Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR and include the tx hash as fee_hash or x-payment-hash header.",
        )

    audit_id = f"audit-{fee_hash[:16].lower()}"
    await verify_fee_payment(fee_hash=fee_hash, escrow_id=audit_id, db=db)

    verdict_dict, model_used = await run_ai_audit(
        task               = req.task,
        work               = req.work,
        buyer_attachments  = [],
        worker_attachments = [
            {"filename": a.filename, "mime_type": a.mime_type, "data": a.data}
            for a in (req.attachments or [])
        ],
        task_category      = req.task_category,
        require_consensus  = req.require_consensus,
    )

    return {
        "status":          "approved" if verdict_dict.get("verdict") == "PASS" else "rejected",
        "verdict":         verdict_dict.get("verdict"),
        "score":           verdict_dict.get("score"),
        "summary":         verdict_dict.get("summary"),
        "details":         verdict_dict.get("details"),
        "criteria_met":    verdict_dict.get("criteria_met", []),
        "criteria_failed": verdict_dict.get("criteria_failed", []),
        "model_used":      model_used,
    }


# ---------------------------------------------------------------------------
# 14. XUMM ENDPOINTS
# ---------------------------------------------------------------------------
@app.post("/xumm/fee-payload")
async def create_fee_payload():
    tx = {
        "TransactionType": "Payment",
        "Destination":     PROTOCOL_WALLET,
        "Amount":          str(int(MIN_FEE_XRP * 1_000_000)),
    }
    return await xumm_create_payload(tx)


@app.get("/xumm/payload/{uuid}")
async def get_xumm_payload_status(uuid: str):
    return await xumm_get_payload(uuid)


@app.post("/xumm/create-payload")
async def create_xumm_payload(req: XummPayloadRequest):
    result = await xumm_create_payload(req.txjson)
    return {"nextUrl": result["nextUrl"], "uuid": result["uuid"]}


# ---------------------------------------------------------------------------
# 15. ESCROW GENERATE — supports XRP and RLUSD
# ---------------------------------------------------------------------------
@app.post("/escrow/generate")
async def generate_escrow(req: EscrowSetupRequest, db: Session = Depends(get_db)):
    existing = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Project ID '{req.escrow_id}' already exists.")

    await verify_fee_payment(fee_hash=req.fee_hash, escrow_id=req.escrow_id, db=db)

    # Validate currency + amount
    currency = req.currency.upper()
    if currency not in ("XRP", "RLUSD"):
        raise HTTPException(status_code=400, detail="currency must be XRP or RLUSD.")

    amount_xrp   = req.amount_xrp   if currency == "XRP"   else None
    amount_rlusd = req.amount_rlusd if currency == "RLUSD" else None

    if currency == "XRP" and (not amount_xrp or amount_xrp <= 0):
        raise HTTPException(status_code=400, detail="amount_xrp required for XRP escrow.")
    if currency == "RLUSD" and (not amount_rlusd or amount_rlusd <= 0):
        raise HTTPException(status_code=400, detail="amount_rlusd required for RLUSD escrow.")

    # For RLUSD escrow, validate both wallets have trustlines
    if currency == "RLUSD":
        buyer_tl  = await check_rlusd_trustline(req.buyer_address)
        worker_tl = await check_rlusd_trustline(req.worker_address)
        if not buyer_tl:
            raise HTTPException(
                status_code=400,
                detail=f"Buyer wallet {req.buyer_address} does not have a RLUSD trustline. Please add one in Xaman: Assets → Add Asset → RLUSD (issuer: {RLUSD_ISSUER}).",
            )
        if not worker_tl:
            raise HTTPException(
                status_code=400,
                detail=f"Seller wallet {req.worker_address} does not have a RLUSD trustline. The seller must add one before this escrow can be created: Assets → Add Asset → RLUSD (issuer: {RLUSD_ISSUER}).",
            )

    # Generate crypto-condition
    preimage_bytes    = secrets.token_bytes(32)
    preimage_hex      = preimage_bytes.hex().upper()
    hash_hex          = hashlib.sha256(preimage_bytes).hexdigest().upper()
    final_condition   = f"A0258020{hash_hex}810120"
    final_fulfillment = f"A0228020{preimage_hex}"

    cancel_after_ts = None
    if req.cancel_after_hrs:
        cancel_after_ts = datetime.now(timezone.utc) + timedelta(hours=req.cancel_after_hrs)

    attachments_json = None
    if req.buyer_attachments:
        attachments_json = json.dumps([a.dict() for a in req.buyer_attachments])

    # Fetch and snapshot spec links provided by the buyer
    spec_snapshots_json = None
    if req.spec_links:
        logger.info(f"🔗 Fetching {len(req.spec_links[:3])} spec link(s) for {req.escrow_id}")
        snapshots = await fetch_url_snapshots(req.spec_links)
        spec_snapshots_json = json.dumps(snapshots)
        ok    = sum(1 for s in snapshots if s.get("content"))
        failed = sum(1 for s in snapshots if s.get("error"))
        logger.info(f"🔗 Spec links: {ok} fetched, {failed} failed for {req.escrow_id}")

    vault = EscrowVault(
        escrow_id             = req.escrow_id,
        condition             = final_condition,
        fulfillment           = encrypt_fulfillment(final_fulfillment),
        status                = "LOCKED",
        currency              = currency,
        amount_xrp            = amount_xrp,
        amount_rlusd          = amount_rlusd,
        project_label         = req.project_label,
        buyer_name            = req.buyer_name,
        buyer_address         = req.buyer_address,
        buyer_email           = req.buyer_email,
        worker_email          = req.worker_email,
        task_description      = req.task_description,
        worker_address        = req.worker_address,
        seller_currency       = req.seller_currency.upper(),
        cancel_after_ts       = cancel_after_ts,
        buyer_attachments     = attachments_json,
        spec_link_snapshots   = spec_snapshots_json,
        delivery_status       = "PENDING",
        submission_count      = 0,
        max_submissions       = max(1, min(req.max_submissions, 10)),  # clamp 1–10
    )
    db.add(vault)
    db.commit()

    logger.info(f"🔒 VAULT CREATED: {req.escrow_id} | currency={currency} | seller_wants={req.seller_currency}")

    # Send worker receipt email
    if req.worker_email:
        import asyncio
        deadline_str = cancel_after_ts.strftime("%A %d %B %Y at %H:%M UTC") if cancel_after_ts else "Not specified"
        amount_val   = amount_rlusd if currency == "RLUSD" else amount_xrp
        asyncio.create_task(send_worker_receipt_email(
            worker_email = req.worker_email,
            worker_name  = "",
            escrow_id    = req.escrow_id,
            buyer_name   = req.buyer_name,
            amount       = amount_val,
            currency     = currency,
            task_preview = req.task_description,
            deadline     = deadline_str,
        ))

    RIPPLE_EPOCH        = 946684800
    cancel_after_ripple = (
        int(cancel_after_ts.timestamp()) - RIPPLE_EPOCH
        if cancel_after_ts else None
    )

    # Build the EscrowCreate amount field for the frontend
    if currency == "RLUSD":
        escrow_amount = {
            "currency": RLUSD_HEX,
            "issuer":   RLUSD_ISSUER,
            "value":    str(amount_rlusd),
        }
    else:
        escrow_amount = str(int(amount_xrp * 1_000_000))

    return {
        "escrow_id":           req.escrow_id,
        "condition":           final_condition,
        "escrow_amount":       escrow_amount,      # ready for EscrowCreate tx
        "currency":            currency,
        "status":              "LOCKED",
        "cancel_after_ripple": cancel_after_ripple,
        "cancel_after_human":  cancel_after_ts.strftime("%Y-%m-%d %H:%M UTC") if cancel_after_ts else None,
        "worker_email_sent":   bool(req.worker_email),
    }


@app.post("/escrow/{escrow_id}/confirm")
async def confirm_escrow_tx(escrow_id: str, body: dict, db: Session = Depends(get_db)):
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Vault '{escrow_id}' not found.")

    tx_hash = body.get("tx_hash", "").strip().upper()
    if not tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash is required.")

    sequence = None
    try:
        client  = AsyncJsonRpcClient(XRPL_URL)
        tx_res  = await client.request(Tx(transaction=tx_hash))
        if tx_res.is_successful():
            tx_data  = tx_res.result.get("tx_json") or tx_res.result.get("tx") or tx_res.result
            sequence = tx_data.get("Sequence")
            logger.info(f"✅ EscrowCreate confirmed: hash={tx_hash[:16]}... seq={sequence}")
    except Exception as e:
        logger.warning(f"Could not look up sequence for {tx_hash}: {e}")

    vault.escrow_tx_hash  = tx_hash
    vault.escrow_sequence = sequence
    db.commit()

    return {"status": "confirmed", "escrow_id": escrow_id, "sequence": sequence}


@app.get("/escrow/{escrow_id}")
async def get_escrow_info(escrow_id: str, db: Session = Depends(get_db)):
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Receipt code '{escrow_id}' not found.")

    deadline_str = (
        vault.cancel_after_ts.strftime("%A %d %B %Y at %H:%M UTC")
        if vault.cancel_after_ts else "Not specified"
    )

    # Determine display amount
    if vault.currency == "RLUSD":
        display_amount = f"{vault.amount_rlusd} RLUSD"
    else:
        display_amount = f"{vault.amount_xrp} XRP"

    # Trustline warning for RLUSD seller-wants-RLUSD flows
    trustline_ok = True
    trustline_warning = None
    if vault.seller_currency == "RLUSD":
        trustline_ok = await check_rlusd_trustline(vault.worker_address)
        if not trustline_ok:
            trustline_warning = (
                f"⚠️ Your wallet does not have a RLUSD trustline. "
                f"You must add one before submitting your work or payment cannot be converted to RLUSD. "
                f"In Xaman: Assets → Add Asset → RLUSD → issuer {RLUSD_ISSUER}."
            )

    return {
        "escrow_id":            vault.escrow_id,
        "project_label":        vault.project_label,
        "status":               vault.status,
        "buyer_name":           vault.buyer_name,
        "buyer_address":        vault.buyer_address,
        "task_description":     vault.task_description,
        "currency":             vault.currency,
        "amount_xrp":           vault.amount_xrp,
        "amount_rlusd":         vault.amount_rlusd,
        "display_amount":       display_amount,
        "seller_currency":      vault.seller_currency,
        "deadline":             deadline_str,
        "worker_address":       vault.worker_address,
        "escrow_sequence":      vault.escrow_sequence,
        "escrow_tx_hash":       vault.escrow_tx_hash,
        "trustline_ok":         trustline_ok,
        "trustline_warning":    trustline_warning,
        "submission_count":     vault.submission_count or 0,
        "max_submissions":      vault.max_submissions  or DEFAULT_MAX_SUBMISSIONS,
        "attempts_remaining":   max(0, (vault.max_submissions or DEFAULT_MAX_SUBMISSIONS) - (vault.submission_count or 0)),
    }


# ---------------------------------------------------------------------------
# 16. EVALUATE — audit + auto-finish + server-side DEX quote
# ---------------------------------------------------------------------------
@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, db: Session = Depends(get_db)):
    import asyncio, base64

    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if not vault:
        all_ids = [v.escrow_id for v in db.query(EscrowVault).all()]
        logger.error(f"❌ VAULT MISS: '{req.escrow_id}' | stored: {all_ids}")
        raise HTTPException(status_code=404, detail=f"Project ID '{req.escrow_id}' not found.")

    if vault.status == "RELEASED":
        raise HTTPException(status_code=409, detail="This escrow has already been released.")
    if vault.status == "CANCELLED":
        raise HTTPException(status_code=409, detail="This escrow has been cancelled.")

    # ── SUBMISSION LIMIT CHECK ──
    current_count = vault.submission_count or 0
    max_allowed   = vault.max_submissions   or DEFAULT_MAX_SUBMISSIONS
    if current_count >= max_allowed:
        attempts_left = 0
        raise HTTPException(
            status_code=429,
            detail=(
                f"Submission limit reached ({max_allowed} attempt{'s' if max_allowed != 1 else ''} allowed). "
                f"Contact the buyer to request additional attempts, or purchase an extra attempt for "
                f"{EXTRA_ATTEMPT_FEE_XRP} XRP via POST /evaluate/purchase-attempt."
            ),
        )

    # Increment submission count immediately (before audit — counts even failed attempts)
    vault.submission_count = current_count + 1
    db.commit()

    # 50 MB attachment cap
    total_bytes = 0
    for att in (req.worker_attachments or []):
        try:
            total_bytes += len(base64.b64decode(att.data))
        except Exception:
            pass
    if total_bytes > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Total attachment size exceeds 50 MB.")

    stored_buyer_attachments = None
    if vault.buyer_attachments:
        try:
            stored_buyer_attachments = json.loads(vault.buyer_attachments)
        except Exception:
            logger.warning("⚠️ Could not parse stored buyer attachments")

    # Load stored spec link snapshots (fetched at vault creation)
    stored_spec_snapshots = None
    if vault.spec_link_snapshots:
        try:
            stored_spec_snapshots = json.loads(vault.spec_link_snapshots)
        except Exception:
            logger.warning("⚠️ Could not parse stored spec link snapshots")

    # Fetch evidence link snapshots now (at submission time = tamper-proof snapshot)
    evidence_snapshots = None
    if req.evidence_links:
        logger.info(f"🔗 Fetching {len(req.evidence_links[:3])} evidence link(s) for {req.escrow_id}")
        evidence_snapshots = await fetch_url_snapshots(req.evidence_links)
        # Store the snapshots on the vault so the collect page can show them
        vault.evidence_link_snapshots = json.dumps(evidence_snapshots)
        ok     = sum(1 for s in evidence_snapshots if s.get("content"))
        failed = sum(1 for s in evidence_snapshots if s.get("error"))
        logger.info(f"🔗 Evidence links: {ok} fetched, {failed} failed for {req.escrow_id}")

    verdict_dict, model_used = await run_ai_audit(
        task                    = vault.task_description,
        work                    = req.work,
        buyer_attachments       = stored_buyer_attachments,
        worker_attachments      = [a.dict() for a in req.worker_attachments] if req.worker_attachments else None,
        task_category           = req.task_category,
        require_consensus       = req.require_consensus,
        spec_link_snapshots     = stored_spec_snapshots,
        evidence_link_snapshots = evidence_snapshots,
    )

    is_approved          = verdict_dict.get("verdict") == "PASS"
    revealed_fulfillment = None
    dex_quote            = None

    if is_approved:
        try:
            plaintext_fulfillment = decrypt_fulfillment(vault.fulfillment)
        except ValueError as e:
            logger.error(f"❌ Could not decrypt fulfillment for {req.escrow_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal error: could not decrypt fulfillment key. Contact support.")

        revealed_fulfillment      = plaintext_fulfillment
        vault.status              = "RELEASED"
        vault.delivery_status     = "RELEASED"
        vault.delivery_expires_at = datetime.now(timezone.utc) + timedelta(days=DELIVERY_EXPIRY_DAYS)

        vault.worker_submission = json.dumps({
            "work":               req.work,
            "attachments":        [a.dict() for a in (req.worker_attachments or [])],
            "evidence_links":     req.evidence_links or [],
            "evidence_snapshots": evidence_snapshots or [],
            "verdict":            verdict_dict,
            "delivered_at":       datetime.now(timezone.utc).isoformat(),
            "escrow_id":          req.escrow_id,
        })

        # ── AUTO-FINISH: referee submits EscrowFinish, seller gets paid automatically ──
        if vault.escrow_sequence and vault.buyer_address and vault.worker_address and referee_wallet:
            asyncio.create_task(auto_finish_escrow(
                escrow_id            = req.escrow_id,
                sequence             = vault.escrow_sequence,
                owner                = vault.buyer_address,
                fulfillment          = plaintext_fulfillment,
                condition            = vault.condition,
                worker_addr          = vault.worker_address,
                db_session_factory   = SessionLocal,
            ))
            logger.info(f"🚀 AUTO-FINISH queued for {req.escrow_id}")
        else:
            logger.warning(
                f"⚠️ AUTO-FINISH skipped for {req.escrow_id}: "
                f"seq={vault.escrow_sequence} | buyer={vault.buyer_address} | "
                f"worker={vault.worker_address} | wallet={'ok' if referee_wallet else 'MISSING'}"
            )

        # ── DEX quote if seller wants RLUSD but escrow is in XRP ──
        if vault.seller_currency == "RLUSD" and vault.currency == "XRP" and vault.amount_xrp:
            dex_quote = await server_side_dex_swap(
                escrow_id          = req.escrow_id,
                worker_addr        = vault.worker_address,
                xrp_amount         = vault.amount_xrp,
                db_session_factory = SessionLocal,
            )

        if vault.buyer_email:
            amount_val = vault.amount_rlusd if vault.currency == "RLUSD" else vault.amount_xrp
            asyncio.create_task(send_delivery_email(
                buyer_email = vault.buyer_email,
                buyer_name  = vault.buyer_name or "there",
                escrow_id   = req.escrow_id,
                amount      = amount_val or 0,
                currency    = vault.currency,
                verdict     = verdict_dict,
            ))
    else:
        logger.info(f"❌ AUDIT FAILED: '{req.escrow_id}' | score={verdict_dict.get('score')}")

    vault.ai_verdict = json.dumps(verdict_dict)
    vault.model_used = model_used
    db.commit()

    # Webhook for agent flows
    if req.callback_url:
        webhook_payload = {"escrow_id": req.escrow_id, "verdict": verdict_dict}
        if is_approved:
            webhook_payload["auto_finish_queued"] = True
            webhook_payload["delivery"] = {
                "work":        req.work,
                "attachments": [{"filename": a.filename, "mime_type": a.mime_type} for a in (req.worker_attachments or [])],
                "collect_url": f"{SITE_URL}?collect={req.escrow_id}",
                "expires_at":  vault.delivery_expires_at.isoformat() if vault.delivery_expires_at else None,
            }
            if dex_quote:
                webhook_payload["dex_quote_rlusd"] = dex_quote
        try:
            async with httpx.AsyncClient() as client:
                await client.post(req.callback_url, json=webhook_payload, timeout=10.0)
            logger.info(f"📡 Webhook delivered to {req.callback_url}")
        except Exception as e:
            logger.warning(f"⚠️ Webhook failed: {e}")

    return {
        "escrow_id":            req.escrow_id,
        "status":               "approved" if is_approved else "rejected",
        "verdict":              verdict_dict,
        "model_used":           model_used,
        # fulfillment key still returned for agent fallback / manual claim
        "fulfillment":          revealed_fulfillment,
        "condition":            vault.condition if is_approved else None,
        "worker_address":       vault.worker_address,
        "buyer_address":        vault.buyer_address,
        "escrow_sequence":      vault.escrow_sequence,
        "amount_xrp":           vault.amount_xrp,
        "amount_rlusd":         vault.amount_rlusd,
        "currency":             vault.currency,
        "auto_finish_queued":   is_approved and bool(vault.escrow_sequence),
        # DEX quote for XRP→RLUSD swap (if seller wants RLUSD)
        "dex_quote_rlusd":      dex_quote,
        "rlusd_issuer":         RLUSD_ISSUER if dex_quote else None,
        "seller_currency":      vault.seller_currency,
    }



# ---------------------------------------------------------------------------
# 16b. PURCHASE EXTRA SUBMISSION ATTEMPT
# ---------------------------------------------------------------------------
class PurchaseAttemptRequest(BaseModel):
    escrow_id: str
    fee_hash:  str   # 0.05 XRP payment hash

@app.post("/evaluate/purchase-attempt")
async def purchase_extra_attempt(req: PurchaseAttemptRequest, db: Session = Depends(get_db)):
    """
    Seller pays EXTRA_ATTEMPT_FEE_XRP (0.05 XRP) to unlock one more submission.
    Returns updated attempts_remaining.
    """
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Escrow '{req.escrow_id}' not found.")
    if vault.status == "RELEASED":
        raise HTTPException(status_code=409, detail="Escrow already released — no more submissions needed.")
    if vault.status == "CANCELLED":
        raise HTTPException(status_code=409, detail="Escrow is cancelled.")

    # Verify the 0.05 XRP payment
    await verify_fee_payment(
        fee_hash  = req.fee_hash,
        escrow_id = f"{req.escrow_id}-attempt",
        db        = db,
        min_xrp   = EXTRA_ATTEMPT_FEE_XRP,
    )

    # Grant one extra submission
    vault.max_submissions = (vault.max_submissions or DEFAULT_MAX_SUBMISSIONS) + 1
    db.commit()

    attempts_remaining = vault.max_submissions - (vault.submission_count or 0)
    logger.info(f"🎟️ Extra attempt purchased for {req.escrow_id} — now {vault.max_submissions} max, {attempts_remaining} remaining")

    return {
        "escrow_id":         req.escrow_id,
        "max_submissions":   vault.max_submissions,
        "submission_count":  vault.submission_count or 0,
        "attempts_remaining": attempts_remaining,
    }


# ---------------------------------------------------------------------------
# 17. DELIVERY RETRIEVAL
# ---------------------------------------------------------------------------
@app.get("/escrow/{escrow_id}/delivery")
async def get_delivery(escrow_id: str, db: Session = Depends(get_db)):
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Escrow '{escrow_id}' not found.")

    if (
        vault.delivery_expires_at
        and datetime.now(timezone.utc) > vault.delivery_expires_at.replace(tzinfo=timezone.utc)
        and vault.delivery_status != "EXPIRED"
    ):
        vault.worker_submission = None
        vault.delivery_status   = "EXPIRED"
        db.commit()

    if vault.delivery_status == "EXPIRED":
        raise HTTPException(status_code=410, detail=f"Delivery expired. Receipt: {escrow_id}")
    if vault.status != "RELEASED":
        raise HTTPException(status_code=403, detail="Delivery only available after PASS verdict.")
    if not vault.worker_submission:
        raise HTTPException(status_code=404, detail="Delivery data not found.")

    if vault.delivery_status == "RELEASED":
        vault.delivery_status = "COLLECTED"
        db.commit()

    submission = json.loads(vault.worker_submission)

    return {
        "escrow_id":       escrow_id,
        "project_label":   vault.project_label,
        "buyer_name":      vault.buyer_name,
        "currency":        vault.currency,
        "amount_xrp":      vault.amount_xrp,
        "amount_rlusd":    vault.amount_rlusd,
        "delivery_status": vault.delivery_status,
        "expires_at":      vault.delivery_expires_at.isoformat() if vault.delivery_expires_at else None,
        "work":            submission.get("work"),
        "attachments":     submission.get("attachments", []),
        "verdict":         submission.get("verdict"),
        "delivered_at":    submission.get("delivered_at"),
        "auto_finish_hash": vault.auto_finish_hash,
    }


# ---------------------------------------------------------------------------
# 18. XRP PRICE
# ---------------------------------------------------------------------------
@app.get("/xrp/price")
async def get_xrp_price():
    """
    Fetch live XRP price. Primary: CoinGecko. Fallback: Binance.
    Returns last cached value if both fail — never logs a warning for expected transient failures.
    """
    global _xrp_price_cache
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res  = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ripple", "vs_currencies": "usd,gbp"},
            )
            data = res.json()
            usd  = data["ripple"]["usd"]
            gbp  = data["ripple"]["gbp"]
            _xrp_price_cache = {"usd": usd, "gbp": gbp}
            return _xrp_price_cache
    except Exception:
        pass  # try fallback silently

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res  = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT")
            usd  = float(res.json()["price"])
            # Approximate GBP via fixed ~0.79 ratio if no better source
            gbp  = round(usd * 0.79, 4)
            _xrp_price_cache = {"usd": usd, "gbp": gbp}
            return _xrp_price_cache
    except Exception:
        pass

    # Return last cached value if available, else null
    if _xrp_price_cache:
        return {**_xrp_price_cache, "cached": True}
    return {"usd": None, "gbp": None}

# Module-level price cache — survives across requests within a process
_xrp_price_cache: dict = {}


# ---------------------------------------------------------------------------
# 19. DEX QUOTE ENDPOINT (used by frontend for pre-submission quote display)
# ---------------------------------------------------------------------------
@app.post("/dex/quote")
async def get_dex_quote(req: QuoteRequest):
    drops = str(int(req.xrp_amount * 1_000_000))

    async with httpx.AsyncClient() as client:
        trust_line_ok = False
        try:
            tl_res = await client.post(
                XRPL_URL,
                json={"method": "account_lines", "params": [{"account": req.worker_address, "peer": RLUSD_ISSUER}]},
                timeout=10.0,
            )
            lines         = tl_res.json().get("result", {}).get("lines", [])
            trust_line_ok = any(l.get("currency") == RLUSD_CURRENCY for l in lines)
        except Exception as e:
            logger.warning(f"⚠️ Trust line check failed: {e}")

        estimated_rlusd  = None
        slippage_warning = False
        try:
            pf_res = await client.post(
                XRPL_URL,
                json={
                    "method": "ripple_path_find",
                    "params": [{
                        "source_account":      req.worker_address,
                        "source_amount":       drops,
                        "destination_account": req.worker_address,
                        "destination_amount":  {"currency": RLUSD_CURRENCY, "issuer": RLUSD_ISSUER, "value": "999999999"},
                    }],
                },
                timeout=15.0,
            )
            alt = pf_res.json().get("result", {}).get("alternatives", [])
            if alt:
                best        = alt[0]
                source_used = best.get("source_amount", drops)
                dest_amount = best.get("destination_amount", {})
                if isinstance(dest_amount, dict):
                    estimated_rlusd = float(dest_amount.get("value", 0))
                if isinstance(source_used, str) and int(source_used) / 1_000_000 > req.xrp_amount * 1.02:
                    slippage_warning = True
        except Exception as e:
            logger.warning(f"⚠️ Pathfinding failed: {e}")

    return {
        "xrp_amount":             req.xrp_amount,
        "estimated_rlusd":        round(estimated_rlusd, 4) if estimated_rlusd else None,
        "trust_line_ok":          trust_line_ok,
        "slippage_warning":       slippage_warning,
        "rlusd_issuer":           RLUSD_ISSUER,
        "trust_line_instructions": None if trust_line_ok else (
            f"Your wallet needs a RLUSD trust line. In Xaman: Assets → Add Asset → RLUSD → issuer {RLUSD_ISSUER}."
        ),
    }



# ---------------------------------------------------------------------------
# 20. MARKETPLACE JOBS API — machine-readable bounty board for agents
# ---------------------------------------------------------------------------
# Seed jobs mirror the frontend demo listings in marketplace.html.
# Real jobs posted via the marketplace UI are stored in localStorage (frontend only).
# This endpoint serves the seed data plus any jobs stored in the DB
# (future: marketplace jobs backed by on-chain escrow use the vault table).

_MARKETPLACE_SEED_JOBS = [
    {
        "id": "AT-MKT-001",
        "title": "Scrape and summarise 100 arXiv AI papers from last 30 days",
        "description": "Fetch the 100 most-cited arXiv papers tagged cs.AI or cs.LG published in the last 30 days. For each paper output: title, authors, abstract summary (≤80 words), key contributions (3 bullet points), and citation count. Deliver as a valid JSON array.",
        "category": "data",
        "bounty": 200,
        "currency": "XRP",
        "poster": "rAgentLabsXXXXXXXXXXXXXXXXXXXXXXX",
        "poster_name": "AgentLabs",
        "deadline": "6 days",
        "deadline_hrs": 144,
        "tags": ["python", "nlp", "json", "research"],
        "status": "OPEN",
        "is_demo": True,
    },
    {
        "id": "AT-MKT-002",
        "title": "Find and document 5 critical XSS vulnerabilities in open-source CMS",
        "description": "Identify and document at least 5 stored or reflected XSS vulnerabilities in a widely-used open-source CMS (WordPress, Joomla, or Drupal) plugin with >10k installs. Each finding must include: CVE-style description, reproduction steps, affected versions, proof-of-concept payload, and recommended fix.",
        "category": "bug_bounty",
        "bounty": 2500,
        "currency": "XRP",
        "poster": "rSecurityDAOXXXXXXXXXXXXXXXXXXXX",
        "poster_name": "SecurityDAO",
        "deadline": "13 days",
        "deadline_hrs": 312,
        "tags": ["security", "xss", "vulnerability", "cms"],
        "status": "OPEN",
        "is_demo": True,
    },
    {
        "id": "AT-MKT-003",
        "title": "Generate 500 synthetic customer support dialogues for LLM fine-tuning",
        "description": "Create 500 realistic customer support conversation pairs for a SaaS product. Cover: billing issues, technical bugs, feature requests, account access, cancellations. Each dialogue must be unique, natural-sounding, 2–6 turns, and delivered as JSONL.",
        "category": "data",
        "bounty": 350,
        "currency": "XRP",
        "poster": "rMLOpsAgentXXXXXXXXXXXXXXXXXXXXX",
        "poster_name": "MLOps.ai",
        "deadline": "4 days",
        "deadline_hrs": 96,
        "tags": ["synthetic-data", "jsonl", "llm", "fine-tuning"],
        "status": "OPEN",
        "is_demo": True,
    },
    {
        "id": "AT-MKT-004",
        "title": "Build a Python script that monitors XRPL escrow events via WebSocket",
        "description": "Write a Python script using xrpl-py that subscribes to the XRPL public WebSocket, filters for EscrowCreate and EscrowFinish events, and logs them to a SQLite database with fields: tx_hash, type, account, destination, amount, condition, sequence, timestamp. Must include README, requirements.txt, and pass provided unit tests.",
        "category": "code",
        "bounty": 180,
        "currency": "XRP",
        "poster": "rXRPLDevAgentXXXXXXXXXXXXXXXXXXX",
        "poster_name": "XRPLDev",
        "deadline": "5 days",
        "deadline_hrs": 120,
        "tags": ["python", "xrpl", "websocket", "sqlite"],
        "status": "OPEN",
        "is_demo": True,
    },
    {
        "id": "AT-MKT-005",
        "title": "Legal memo: analyse enforceability of smart contract arbitration clause",
        "description": "Write a 1,500–2,000 word legal memo analysing the enforceability of AI-arbitrated smart contract dispute resolution clauses under English law and New York law. Address: contract formation, arbitrability, recognition of algorithmic verdicts, and recommendations for drafting enforceable clauses.",
        "category": "legal",
        "bounty": 800,
        "currency": "XRP",
        "poster": "rLexDAOXXXXXXXXXXXXXXXXXXXXXXXXX",
        "poster_name": "LexDAO",
        "deadline": "10 days",
        "deadline_hrs": 240,
        "tags": ["legal", "smart-contracts", "arbitration", "memo"],
        "status": "OPEN",
        "is_demo": True,
    },
    {
        "id": "AT-MKT-006",
        "title": "Write 10 product description variations for an AI SaaS landing page",
        "description": "Write 10 distinct product description variations for an AI-powered escrow SaaS product targeting: enterprise procurement teams, freelance developers, and AI agent builders. Each variation: 60–90 words, value-focused, no jargon. Deliver as markdown.",
        "category": "creative",
        "bounty": 120,
        "currency": "XRP",
        "poster": "rCopyAgentXXXXXXXXXXXXXXXXXXXXXX",
        "poster_name": "CopyAgent",
        "deadline": "2 days",
        "deadline_hrs": 48,
        "tags": ["copywriting", "saas", "marketing", "markdown"],
        "status": "OPEN",
        "is_demo": True,
    },
    {
        "id": "AT-MKT-007",
        "title": "Analyse DeFi protocol TVL trends Q1 2025 — structured report",
        "description": "Compile and analyse Total Value Locked data for the top 20 DeFi protocols by TVL for Q1 2025. Identify top 5 gainers, top 5 losers, correlations with BTC price movement, and 3 key macro factors. Deliver as structured markdown with a data table and chart descriptions.",
        "category": "data_analysis",
        "bounty": 420,
        "currency": "XRP",
        "poster": "rDeFiAnalyticsXXXXXXXXXXXXXXXXXX",
        "poster_name": "DeFiAnalytics",
        "deadline": "7 days",
        "deadline_hrs": 168,
        "tags": ["defi", "tvl", "analysis", "report"],
        "status": "OPEN",
        "is_demo": True,
    },
]

@app.get("/marketplace/jobs")
async def marketplace_jobs(
    category:       str   = "all",
    min_bounty_xrp: float = 0,
    limit:          int   = 20,
    db: Session = Depends(get_db),
):
    """
    Machine-readable marketplace job listing for agents and API consumers.
    Returns open bounties in structured JSON.

    Real on-chain jobs (vaults with status=OPEN and a marketplace flag) are
    returned first, followed by demo seed jobs. Agents should check is_demo —
    demo jobs have no live escrow to claim against.
    """
    limit = min(limit, 100)

    # Real jobs: vaults that are OPEN and have a project_label (posted via marketplace)
    # For now this returns all LOCKED vaults as potential claimable jobs.
    # Future: add a marketplace_visible column to filter precisely.
    real_jobs = []
    try:
        vaults = (
            db.query(EscrowVault)
            .filter(EscrowVault.status == "LOCKED")
            .order_by(EscrowVault.created_at.desc())
            .limit(200)
            .all()
        )
        for v in vaults:
            if not v.task_description:
                continue
            bounty = v.amount_xrp or 0
            if bounty < min_bounty_xrp:
                continue
            if category != "all":
                # We don't store category on vault yet — skip category filter for real jobs
                pass
            real_jobs.append({
                "id":           v.escrow_id,
                "title":        v.project_label or f"Job {v.escrow_id}",
                "description":  v.task_description,
                "category":     "default",
                "bounty":       bounty,
                "currency":     v.currency or "XRP",
                "poster":       v.buyer_address or "",
                "poster_name":  v.buyer_name or "",
                "deadline":     f"{v.cancel_after_ts.strftime('%d %b %Y %H:%M UTC')}" if v.cancel_after_ts else "—",
                "deadline_hrs": max(0, int((v.cancel_after_ts - datetime.now(timezone.utc)).total_seconds() / 3600)) if v.cancel_after_ts else None,
                "tags":         [],
                "status":       "OPEN",
                "is_demo":      False,
            })
    except Exception as e:
        logger.warning(f"⚠️ marketplace_jobs DB query failed: {e}")

    # Seed demo jobs — filter by category and bounty
    seed = _MARKETPLACE_SEED_JOBS
    if category != "all":
        seed = [j for j in seed if j["category"] == category]
    if min_bounty_xrp > 0:
        seed = [j for j in seed if j["bounty"] >= min_bounty_xrp]

    combined = (real_jobs + seed)[:limit]

    return {
        "jobs":            combined,
        "total":           len(combined),
        "real_jobs":       len(real_jobs),
        "demo_jobs":       len([j for j in combined if j.get("is_demo")]),
        "marketplace_url": f"{SITE_URL}/marketplace",
        "note":            "Demo jobs (is_demo=true) are illustrative examples — no live escrow exists to claim against.",
    }


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🚀 Starting AgentTrust Referee v6.0 on port {port}")
    uvicorn.run("referee:app", host="0.0.0.0", port=port, reload=False)
