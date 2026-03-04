import os
import httpx
import logging
import sys
import hashlib
import secrets
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import resend

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
# 2b. MCP SERVER
# ---------------------------------------------------------------------------
try:
    from mcp_server import mcp
    app.mount("/mcp", mcp.http_app())
    logger.info("✅ MCP server mounted at /mcp")
except Exception as e:
    logger.warning(f"⚠️ MCP server not loaded: {e} — continuing without it")

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
    return {"status": "online", "version": "5.1", "service": "AgentTrust Referee", "playground": "/playground", "docs": "/docs"}

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
    return {"status": "online", "version": "5.1", "timestamp": datetime.now(timezone.utc)}

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
        "agentVersion": "5.1.0",
        "protocolVersion": "0.3.0",
        "provider": {"organization": "AgentTrust Protocol", "url": "https://xrpl-referee.onrender.com"},
        "capabilities": {"streaming": False, "pushNotifications": False, "multimodal": True, "escrow": True},
        "authentication": {
            "schemes": ["x-payment-hash"],
            "description": "Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR. Pass tx hash as x-payment-hash header."
        },
        "payment": {"currency": "XRP", "amount": "0.1", "destination": "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR", "network": "XRPL Mainnet"},
        "skills": [
            {"id": "standalone-audit", "name": "AI Verdict", "description": "POST task+work+fee to /audit. Returns PASS/FAIL with score, summary, criteria.", "endpoint": "/audit", "method": "POST", "tags": ["audit", "xrpl", "verification", "ai", "escrow", "legal-tech"]},
            {"id": "escrow-create",    "name": "Create Escrow Vault",          "description": "Lock XRPL funds in crypto-condition escrow gated by AI verdict.", "endpoint": "/escrow/generate", "method": "POST"},
            {"id": "escrow-evaluate",  "name": "Submit Work for Escrow Audit", "description": "Worker submits proof. On PASS returns fulfillment key.", "endpoint": "/evaluate", "method": "POST"}
        ],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"]
    }

@app.get("/.well-known/ai-plugin.json")
def serve_ai_plugin():
    path = ".well-known/ai-plugin.json"
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return {
        "schema_version": "v1",
        "name_for_human": "AgentTrust Referee",
        "name_for_model": "agenttrust_referee",
        "description_for_human": "Trustless AI task verification. Pay 0.1 XRP, get PASS/FAIL verdict.",
        "description_for_model": "Verify task completion. POST task+work to /audit with x-payment-hash header (0.1 XRP fee). Returns JSON with verdict, score, criteria_met, criteria_failed, details.",
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": "https://xrpl-referee.onrender.com/openapi.json"},
        "legal_info_url": "https://xrpl-referee.onrender.com"
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

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class PaymentLog(Base):
    __tablename__ = "payment_logs"
    id           = Column(Integer, primary_key=True, index=True)
    payment_hash = Column(String, unique=True, index=True, nullable=False)
    purpose      = Column(String, nullable=True)
    sender       = Column(String, nullable=True)
    amount_xrp   = Column(Float, nullable=True)
    escrow_id    = Column(String, nullable=True)
    timestamp    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class EscrowVault(Base):
    __tablename__ = "escrow_vault"
    escrow_id         = Column(String, primary_key=True, index=True)
    condition         = Column(String, nullable=False)
    fulfillment       = Column(String, nullable=False)
    status            = Column(String, default="LOCKED")
    # Job metadata
    project_label     = Column(String, nullable=True)
    buyer_name        = Column(String, nullable=True)
    buyer_address     = Column(String, nullable=True)
    buyer_email       = Column(String, nullable=True)   # V2: PASS notification
    worker_email      = Column(String, nullable=True)   # V2: receipt code notification
    task_description  = Column(Text, nullable=True)
    worker_address    = Column(String, nullable=True)
    amount_xrp        = Column(Float, nullable=True)
    cancel_after_ts   = Column(DateTime, nullable=True)
    buyer_attachments = Column(Text, nullable=True)
    # EscrowCreate tx
    escrow_tx_hash    = Column(String, nullable=True)
    escrow_sequence   = Column(Integer, nullable=True)
    # Audit result
    ai_verdict        = Column(Text, nullable=True)
    model_used        = Column(String, nullable=True)
    created_at        = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # V2: delivery
    worker_submission   = Column(Text, nullable=True)    # JSON blob, wiped on expiry
    delivery_expires_at = Column(DateTime, nullable=True)
    delivery_status     = Column(String, nullable=True)  # PENDING/RELEASED/COLLECTED/EXPIRED


Base.metadata.create_all(bind=engine)


def run_migrations():
    migrations = [
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_name         VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS task_description    TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_address      VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS amount_xrp          FLOAT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS cancel_after_ts     TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_attachments   TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS ai_verdict          TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS model_used          VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS created_at          TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS project_label       VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_address       VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS escrow_tx_hash      VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS escrow_sequence     INTEGER",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS purpose             VARCHAR",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS sender              VARCHAR",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS amount_xrp          FLOAT",
        "ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS escrow_id           VARCHAR",
        # V2 columns
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS buyer_email         VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_email        VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS worker_submission   TEXT",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS delivery_expires_at TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS delivery_status     VARCHAR",
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

RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
RESEND_FROM          = os.getenv("RESEND_FROM", "noreply@cryptovault.co.uk")
DELIVERY_EXPIRY_DAYS = 7
SITE_URL             = os.getenv("SITE_URL", "https://www.cryptovault.co.uk")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY
    logger.info("✅ Resend email configured")
else:
    logger.warning("⚠️ RESEND_API_KEY missing — email notifications disabled")

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
    data:      str

class EscrowSetupRequest(BaseModel):
    escrow_id:          str
    fee_hash:           str
    project_label:      Optional[str] = None
    buyer_name:         str
    buyer_address:      str
    buyer_email:        Optional[str] = None   # V2: notify buyer on PASS
    worker_email:       Optional[str] = None   # V2: send receipt code to worker
    task_description:   str
    worker_address:     str
    amount_xrp:         float
    cancel_after_hrs:   int = 168
    buyer_attachments:  Optional[list[Attachment]] = None

class AuditRequest(BaseModel):
    escrow_id:           str
    work:                str
    worker_attachments:  Optional[list[Attachment]] = None
    callback_url:        Optional[str] = None
    task_category:       str  = "default"
    require_consensus:   bool = False

class StandaloneAuditRequest(BaseModel):
    task:                str
    work:                str
    fee_hash:            Optional[str]  = None
    attachments:         Optional[list[Attachment]] = None
    task_category:       str  = "default"
    require_consensus:   bool = False

class XummPayloadRequest(BaseModel):
    txjson: dict


# ---------------------------------------------------------------------------
# 7. FEE VERIFICATION
# ---------------------------------------------------------------------------
async def verify_fee_payment(fee_hash: str, escrow_id: str, db: Session) -> dict:
    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == fee_hash).first()
    if already_used:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Payment hash already used for escrow '{already_used.escrow_id}' "
                f"on {already_used.timestamp.strftime('%Y-%m-%d %H:%M UTC')}."
            )
        )

    client = AsyncJsonRpcClient(XRPL_URL)
    try:
        tx_res = await client.request(Tx(transaction=fee_hash))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ledger lookup failed: {str(e)}")

    if not tx_res.is_successful():
        raise HTTPException(status_code=402, detail="Transaction hash not found on the XRPL ledger.")

    body    = tx_res.result
    logger.info(f"🔍 RAW TX KEYS: {list(body.keys())}")
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
    if amount_xrp < (MIN_FEE_XRP - 0.000001):
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient fee. Required ≥{MIN_FEE_XRP} XRP, received {amount_xrp:.6f} XRP."
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
# 8. EMAIL HELPERS
# ---------------------------------------------------------------------------
def _email_styles() -> str:
    """Shared inline CSS for all emails."""
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
    worker_email:  str,
    worker_name:   str,
    escrow_id:     str,
    buyer_name:    str,
    amount_xrp:    float,
    task_preview:  str,
    deadline:      str,
):
    """
    Fires immediately after escrow is created.
    Sends the worker their receipt code and a link to the worker tab.
    """
    if not RESEND_API_KEY or not worker_email:
        logger.info(f"📧 Worker email skipped for {escrow_id}")
        return

    worker_url   = f"{SITE_URL}?worker={escrow_id}"
    preview_safe = task_preview[:300] + ("…" if len(task_preview) > 300 else "")

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
           padding:12px 16px;font-size:.88rem;color:#333;line-height:1.65;
           margin-bottom:20px;}}
</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>You have a new job</h1>
  <p>Hi{' ' + worker_name if worker_name else ''}, <strong>{buyer_name}</strong> has locked
     <strong>{amount_xrp} XRP</strong> in escrow for you. Complete the work,
     submit your proof, and claim payment instantly on AI approval.</p>

  <div class="detail"><span>Your Receipt Code</span></div>
  <div class="code">{escrow_id}</div>

  <div class="detail"><span>Amount locked for you</span><br>
    <strong>{amount_xrp} XRP</strong>
  </div>
  <div class="detail"><span>Deadline</span><br><strong>{deadline}</strong></div>

  <p style="font-size:.85rem;font-weight:700;margin-bottom:.4rem;color:#0d0d12;">Task brief:</p>
  <div class="task-box">{preview_safe}</div>

  <a href="{worker_url}" class="btn">Submit Your Work →</a>

  <p style="font-size:.85rem;">Enter your receipt code <strong>{escrow_id}</strong> on the
     Worker tab to load the full job details, submit your work, and claim payment.</p>

  <div class="footer">
    Payment is held securely on the XRP Ledger — neither party can access it
    until the AI referee approves your submission.<br><br>
    AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a>
  </div>
</div></body></html>""",
        })
        logger.info(f"📧 Worker receipt email sent to {worker_email} for {escrow_id}")
    except Exception as e:
        logger.error(f"❌ Worker email failed for {escrow_id}: {e}")


async def send_delivery_email(
    buyer_email: str,
    buyer_name:  str,
    escrow_id:   str,
    amount_xrp:  float,
    verdict:     dict,
):
    """
    Fires when AI verdict is PASS.
    Sends buyer a collect link valid for 7 days.
    """
    if not RESEND_API_KEY or not buyer_email:
        logger.info(f"📧 Buyer delivery email skipped for {escrow_id}")
        return

    collect_url = f"{SITE_URL}?collect={escrow_id}"
    score       = verdict.get("score", "—")
    summary     = verdict.get("summary", "Work verified by AI referee.")

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
.expiry{{font-size:.8rem;color:#9999aa;margin-top:24px;
         padding-top:20px;border-top:1px solid #eee;}}
</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>Your delivery is ready to collect</h1>
  <p>Hi {buyer_name}, the work for your escrow has passed AI verification
     and your delivery is waiting for you.</p>

  <div class="detail"><span>Escrow ID</span><br><strong>{escrow_id}</strong></div>
  <div class="detail"><span>Amount locked</span><br><strong>{amount_xrp} XRP</strong></div>

  <div class="verdict-box">
    <div class="score">✓ PASS &nbsp;{score}/100</div>
    <div class="summary">{summary}</div>
  </div>

  <a href="{collect_url}" class="btn">Collect Your Delivery →</a>

  <p>Click the button above to view and download everything the worker submitted.
     Your receipt code is <strong>{escrow_id}</strong>.</p>

  <div class="expiry">
    ⏳ This delivery will expire in <strong>7 days</strong>. After that the
    submission data is permanently deleted. Please collect it before then.<br><br>
    AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a>
  </div>
</div></body></html>""",
        })
        logger.info(f"📧 Buyer delivery email sent to {buyer_email} for {escrow_id}")
    except Exception as e:
        logger.error(f"❌ Buyer delivery email failed for {escrow_id}: {e}")


# ---------------------------------------------------------------------------
# 9. DOMAIN-SPECIFIC PROMPT LIBRARY
# ---------------------------------------------------------------------------
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
        "Flag any discrepancies between the task spec and submitted documents. "
        "Note: you are evaluating submitted documents, not independently verifying against live databases."
    ),
    "real_estate": (
        "You are auditing a real estate transaction milestone. "
        "Evaluate whether submitted documents (surveys, registry excerpts, inspection reports) "
        "satisfy the conditions stated in the task spec. "
        "Flag missing documents, date inconsistencies, or unresolved conditions. "
        "Note: you are evaluating submitted documents, not independently verifying land registry data."
    ),
    "creative": (
        "You are auditing a creative deliverable (writing, design, code, media). "
        "Evaluate quality, completeness, and adherence to the stated brief. "
        "For writing: check word count, tone, structure, and coverage of required topics. "
        "For design: evaluate based on described requirements and any attached files. "
        "Be fair but hold the work to the standard the buyer specified."
    ),
    "code": (
        "You are auditing a software development deliverable. "
        "Evaluate: does the submitted work address the stated requirements? "
        "Check for: completeness, correctness of described approach, presence of required components. "
        "If a repo link or code is provided, evaluate its structure and described functionality. "
        "You cannot execute code, so judge based on what is visible and described."
    ),
    "data": (
        "You are auditing a data or research deliverable. "
        "Evaluate: completeness of the dataset/report, format compliance, coverage of required fields. "
        "Check that the volume, structure, and content match what was specified. "
        "Flag any gaps, missing fields, or format deviations."
    ),
    "default": (
        "You are an autonomous escrow auditor — a neutral, objective third party determining "
        "whether a worker has fulfilled a task specification well enough to be paid."
    ),
}

# ---------------------------------------------------------------------------
# 10. AI AUDIT ENGINE
# ---------------------------------------------------------------------------
async def run_ai_audit(
    task:               str,
    work:               str,
    buyer_attachments:  list = None,
    worker_attachments: list = None,
    task_category:      str  = "default",
    require_consensus:  bool = False,
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

    parts = []

    if buyer_attachments:
        for att in buyer_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({"inline_data": {"mime_type": mime, "data": att.get("data")}})
                logger.info(f"📎 Buyer attachment added to AI: {att.get('filename')} ({mime})")
            else:
                logger.info(f"ℹ️ Buyer attachment {att.get('filename')} is text-extracted")

    if worker_attachments:
        for att in worker_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({"inline_data": {"mime_type": mime, "data": att.get("data")}})
                logger.info(f"📎 Worker attachment added to AI: {att.get('filename')} ({mime})")
            else:
                logger.info(f"ℹ️ Worker attachment {att.get('filename')} is text-extracted")

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
                                    raw2   = res2.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                                    clean2 = raw2.replace("```json","").replace("```","").strip()
                                    v2     = json.loads(clean2)
                                    v2["verdict"] = str(v2.get("verdict","FAIL")).strip().upper()
                                    logger.info(f"🤖 CONSENSUS model {model_2}: {v2['verdict']}")

                                    if v2["verdict"] != verdict_dict["verdict"]:
                                        logger.warning(f"⚖️ CONSENSUS SPLIT: {model_id}={verdict_dict['verdict']} vs {model_2}={v2['verdict']} — defaulting FAIL")
                                        verdict_dict["verdict"]   = "FAIL"
                                        verdict_dict["summary"]   = f"Models disagreed ({model_id}: {verdict_dict.get('score')}, {model_2}: {v2.get('score')}). Conservative FAIL."
                                        verdict_dict["consensus"] = False
                                        verdict_dict["models"]    = [model_id, model_2]
                                    else:
                                        verdict_dict["consensus"] = True
                                        verdict_dict["models"]    = [model_id, model_2]
                                    break
                            except Exception as e2:
                                logger.warning(f"Consensus model {model_2} failed: {e2}")
                                continue

                    return verdict_dict, model_id

                else:
                    logger.warning(f"Model {model_id} HTTP {res.status_code}: {res.text[:200]}")

            except Exception as e:
                logger.warning(f"Model {model_id} failed: {e}")
                continue

    raise Exception("AI Gateway Failure: all models exhausted.")


# ---------------------------------------------------------------------------
# 11. STANDALONE AUDIT ENDPOINT
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
            detail="Payment required. Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR and include the tx hash as fee_hash in the body or x-payment-hash header."
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

    is_approved = verdict_dict.get("verdict", "").upper() == "PASS"
    logger.info(f"📋 STANDALONE AUDIT {audit_id}: {verdict_dict.get('verdict')} | model={model_used}")

    return {
        "status":          "approved" if is_approved else "rejected",
        "verdict":         verdict_dict.get("verdict"),
        "score":           verdict_dict.get("score"),
        "summary":         verdict_dict.get("summary"),
        "details":         verdict_dict.get("details"),
        "criteria_met":    verdict_dict.get("criteria_met", []),
        "criteria_failed": verdict_dict.get("criteria_failed", []),
        "model_used":      model_used,
    }


# ---------------------------------------------------------------------------
# 12. XUMM ENDPOINTS
# ---------------------------------------------------------------------------
@app.post("/xumm/fee-payload")
async def create_fee_payload():
    if not xumm_sdk:
        raise HTTPException(status_code=500, detail="Xumm SDK not configured.")
    tx = {
        "TransactionType": "Payment",
        "Destination": PROTOCOL_WALLET,
        "Amount": str(int(MIN_FEE_XRP * 1_000_000)),
    }
    try:
        result = xumm_sdk.payload.create(tx)
        return {"nextUrl": result.next.always, "uuid": result.uuid, "qr": result.refs.qr_png}
    except Exception as e:
        logger.error(f"❌ XUMM fee payload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/xumm/payload/{uuid}")
async def get_xumm_payload_status(uuid: str):
    if not xumm_sdk:
        raise HTTPException(status_code=500, detail="Xumm SDK not configured.")
    try:
        result  = xumm_sdk.payload.get(uuid)
        signed  = result.meta.signed
        tx_hash = result.response.txid if signed else None
        return {"signed": signed, "tx_hash": tx_hash}
    except Exception as e:
        logger.error(f"❌ XUMM poll error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/xumm/create-payload")
async def create_xumm_payload(req: XummPayloadRequest):
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
# 13. CORE PROTOCOL ENDPOINTS
# ---------------------------------------------------------------------------
@app.post("/escrow/generate")
async def generate_escrow(req: EscrowSetupRequest, db: Session = Depends(get_db)):
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
        cancel_after_ts = datetime.now(timezone.utc) + timedelta(hours=req.cancel_after_hrs)

    attachments_json = None
    if req.buyer_attachments:
        attachments_json = json.dumps([a.dict() for a in req.buyer_attachments])
        logger.info(f"📎 Storing {len(req.buyer_attachments)} buyer attachment(s) for '{req.escrow_id}'")

    vault = EscrowVault(
        escrow_id         = req.escrow_id,
        condition         = final_condition,
        fulfillment       = final_fulfillment,
        status            = "LOCKED",
        project_label     = req.project_label,
        buyer_name        = req.buyer_name,
        buyer_address     = req.buyer_address,
        buyer_email       = req.buyer_email,
        worker_email      = req.worker_email,
        task_description  = req.task_description,
        worker_address    = req.worker_address,
        amount_xrp        = req.amount_xrp,
        cancel_after_ts   = cancel_after_ts,
        buyer_attachments = attachments_json,
        delivery_status   = "PENDING",
    )
    db.add(vault)
    db.commit()

    logger.info(f"🔒 VAULT CREATED: escrow_id='{req.escrow_id}'")

    # Fire worker receipt email immediately (non-blocking)
    if req.worker_email:
        import asyncio
        deadline_str = cancel_after_ts.strftime("%A %d %B %Y at %H:%M UTC") if cancel_after_ts else "Not specified"
        asyncio.create_task(send_worker_receipt_email(
            worker_email  = req.worker_email,
            worker_name   = "",
            escrow_id     = req.escrow_id,
            buyer_name    = req.buyer_name,
            amount_xrp    = req.amount_xrp,
            task_preview  = req.task_description,
            deadline      = deadline_str,
        ))

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
            tx_body  = tx_res.result
            tx_data  = tx_body.get("tx_json") or tx_body.get("tx") or tx_body
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

    return {
        "escrow_id":       vault.escrow_id,
        "project_label":   vault.project_label,
        "status":          vault.status,
        "buyer_name":      vault.buyer_name,
        "buyer_address":   vault.buyer_address,
        "task_description": vault.task_description,
        "amount_xrp":      vault.amount_xrp,
        "deadline":        deadline_str,
        "worker_address":  vault.worker_address,
        "escrow_sequence": vault.escrow_sequence,
        "escrow_tx_hash":  vault.escrow_tx_hash,
    }


# ---------------------------------------------------------------------------
# 14. EVALUATE — core audit + delivery store + notifications
# ---------------------------------------------------------------------------
@app.post("/evaluate")
async def evaluate_work(req: AuditRequest, db: Session = Depends(get_db)):
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()

    if not vault:
        all_ids = [v.escrow_id for v in db.query(EscrowVault).all()]
        logger.error(f"❌ VAULT MISS: '{req.escrow_id}' not found. Stored: {all_ids}")
        raise HTTPException(
            status_code=404,
            detail=f"Project ID '{req.escrow_id}' not found. Check the ID is exactly correct."
        )

    if vault.status == "RELEASED":
        raise HTTPException(status_code=409, detail="This escrow has already been released.")
    if vault.status == "CANCELLED":
        raise HTTPException(status_code=409, detail="This escrow has been cancelled by the buyer.")

    # Enforce 50MB total attachment cap
    import base64
    total_bytes = 0
    for att in (req.worker_attachments or []):
        try:
            total_bytes += len(base64.b64decode(att.data))
        except Exception:
            pass
    if total_bytes > 50 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Total attachment size exceeds 50 MB ({total_bytes / 1024 / 1024:.1f} MB submitted)."
        )

    stored_buyer_attachments = None
    if vault.buyer_attachments:
        try:
            stored_buyer_attachments = json.loads(vault.buyer_attachments)
        except Exception:
            logger.warning("⚠️ Could not parse stored buyer attachments")

    verdict_dict, model_used = await run_ai_audit(
        task               = vault.task_description,
        work               = req.work,
        buyer_attachments  = stored_buyer_attachments,
        worker_attachments = [a.dict() for a in req.worker_attachments] if req.worker_attachments else None,
        task_category      = req.task_category,
        require_consensus  = req.require_consensus,
    )

    is_approved          = verdict_dict.get("verdict") == "PASS"
    revealed_fulfillment = None

    if is_approved:
        import asyncio
        revealed_fulfillment        = vault.fulfillment
        vault.status                = "RELEASED"
        vault.delivery_status       = "RELEASED"
        vault.delivery_expires_at   = datetime.now(timezone.utc) + timedelta(days=DELIVERY_EXPIRY_DAYS)

        vault.worker_submission = json.dumps({
            "work":         req.work,
            "attachments":  [a.dict() for a in (req.worker_attachments or [])],
            "verdict":      verdict_dict,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
            "escrow_id":    req.escrow_id,
        })

        logger.info(f"✅ KEY RELEASED + DELIVERY STORED: '{req.escrow_id}' | expires in {DELIVERY_EXPIRY_DAYS}d")

        # Notify buyer by email
        if vault.buyer_email:
            asyncio.create_task(send_delivery_email(
                buyer_email = vault.buyer_email,
                buyer_name  = vault.buyer_name or "there",
                escrow_id   = req.escrow_id,
                amount_xrp  = vault.amount_xrp or 0,
                verdict     = verdict_dict,
            ))
    else:
        logger.info(f"❌ AUDIT FAILED: '{req.escrow_id}' | score={verdict_dict.get('score')}")

    vault.ai_verdict = json.dumps(verdict_dict)
    vault.model_used = model_used
    db.commit()

    # Webhook for agent flows — V2 includes delivery payload
    if req.callback_url:
        webhook_payload = {"escrow_id": req.escrow_id, "verdict": verdict_dict}
        if is_approved and revealed_fulfillment:
            webhook_payload["fulfillment"] = revealed_fulfillment
            webhook_payload["delivery"]    = {
                "work":        req.work,
                "attachments": [
                    {"filename": a.filename, "mime_type": a.mime_type}
                    for a in (req.worker_attachments or [])
                ],
                "collect_url": f"{SITE_URL}?collect={req.escrow_id}",
                "expires_at":  vault.delivery_expires_at.isoformat() if vault.delivery_expires_at else None,
            }
        try:
            async with httpx.AsyncClient() as client:
                await client.post(req.callback_url, json=webhook_payload, timeout=10.0)
            logger.info(f"📡 Webhook delivered to {req.callback_url}")
        except Exception as e:
            logger.warning(f"⚠️ Webhook delivery failed: {e}")

    return {
        "escrow_id":       req.escrow_id,
        "status":          "approved" if is_approved else "rejected",
        "verdict":         verdict_dict,
        "model_used":      model_used,
        "fulfillment":     revealed_fulfillment,
        "condition":       vault.condition if is_approved else None,
        "worker_address":  vault.worker_address,
        "buyer_address":   vault.buyer_address,
        "escrow_sequence": vault.escrow_sequence,
        "amount_xrp":      vault.amount_xrp,
    }


# ---------------------------------------------------------------------------
# 15. DELIVERY RETRIEVAL ENDPOINT
# ---------------------------------------------------------------------------
@app.get("/escrow/{escrow_id}/delivery")
async def get_delivery(escrow_id: str, db: Session = Depends(get_db)):
    """
    Buyer retrieves the worker's full submission after PASS.
    Handles lazy expiry — no cron job needed.
    """
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Escrow '{escrow_id}' not found.")

    # Lazy expiry check
    if (
        vault.delivery_expires_at
        and datetime.now(timezone.utc) > vault.delivery_expires_at.replace(tzinfo=timezone.utc)
        and vault.delivery_status != "EXPIRED"
    ):
        vault.worker_submission = None
        vault.delivery_status   = "EXPIRED"
        db.commit()
        logger.info(f"🗑️ Delivery expired and wiped: {escrow_id}")

    if vault.delivery_status == "EXPIRED":
        raise HTTPException(
            status_code=410,
            detail=f"This delivery expired 7 days after the PASS verdict and has been permanently deleted. Receipt: {escrow_id}"
        )

    if vault.status != "RELEASED":
        raise HTTPException(
            status_code=403,
            detail="Delivery is only available after the AI verdict is PASS."
        )

    if not vault.worker_submission:
        raise HTTPException(status_code=404, detail="Delivery data not found.")

    # Mark collected on first access
    if vault.delivery_status == "RELEASED":
        vault.delivery_status = "COLLECTED"
        db.commit()
        logger.info(f"📦 Delivery collected: {escrow_id}")

    submission = json.loads(vault.worker_submission)

    return {
        "escrow_id":       escrow_id,
        "project_label":   vault.project_label,
        "buyer_name":      vault.buyer_name,
        "amount_xrp":      vault.amount_xrp,
        "delivery_status": vault.delivery_status,
        "expires_at":      vault.delivery_expires_at.isoformat() if vault.delivery_expires_at else None,
        "work":            submission.get("work"),
        "attachments":     submission.get("attachments", []),
        "verdict":         submission.get("verdict"),
        "delivered_at":    submission.get("delivered_at"),
    }


# ---------------------------------------------------------------------------
# 16. XRP PRICE ENDPOINT
# ---------------------------------------------------------------------------
@app.get("/xrp/price")
async def get_xrp_price():
    """Live XRP/USD/GBP price for buyer amount display."""
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ripple", "vs_currencies": "usd,gbp"},
                timeout=5.0,
            )
            data = res.json()
            return {"usd": data["ripple"]["usd"], "gbp": data["ripple"]["gbp"]}
    except Exception as e:
        logger.warning(f"⚠️ XRP price fetch failed: {e}")
        return {"usd": None, "gbp": None}


# ---------------------------------------------------------------------------
# 17. DEX QUOTE ENDPOINT
# ---------------------------------------------------------------------------
RLUSD_ISSUER   = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY = "RLUSD"

class QuoteRequest(BaseModel):
    worker_address: str
    xrp_amount:     float

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
                    }]
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
                if isinstance(source_used, str):
                    if int(source_used) / 1_000_000 > req.xrp_amount * 1.02:
                        slippage_warning = True
                logger.info(f"💱 DEX QUOTE: {req.xrp_amount} XRP → ~{estimated_rlusd} RLUSD")
        except Exception as e:
            logger.warning(f"⚠️ Pathfinding failed: {e}")

    return {
        "xrp_amount":        req.xrp_amount,
        "estimated_rlusd":   round(estimated_rlusd, 4) if estimated_rlusd else None,
        "trust_line_ok":     trust_line_ok,
        "slippage_warning":  slippage_warning,
        "rlusd_issuer":      RLUSD_ISSUER,
        "trust_line_instructions": None if trust_line_ok else (
            f"Your wallet does not have a RLUSD trust line. "
            f"In Xaman: go to Assets → Add Asset → search RLUSD → "
            f"select issuer {RLUSD_ISSUER} → Add Trust Line."
        ),
    }


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🚀 Starting AgentTrust Referee v5.1 on port {port}")
    uvicorn.run("referee:app", host="0.0.0.0", port=port, reload=False)
