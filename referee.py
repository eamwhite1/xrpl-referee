import os
import httpx
import logging
import sys
import hashlib
import secrets
import json
import base64
from contextlib import asynccontextmanager
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
from xrpl.models.transactions import EscrowFinish
from xrpl.core.addresscodec import decode_seed
from xrpl.utils import xrp_to_drops

# XUMM SDK removed — using direct HTTP calls instead (no dependency conflict)

# Database Imports
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, text, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# ---------------------------------------------------------------------------
# 1. INITIAL SETUP & LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("RefereeBot")
load_dotenv()

# ---------------------------------------------------------------------------
# 2. MCP SERVER (imported before app so its lifespan can be wired in)
# ---------------------------------------------------------------------------
# FastMCP 2.x uses Streamable HTTP transport (path="/" = endpoint at mount root).
# stateless_http=True makes FastMCP handle each POST independently — no session
# ID required. This is essential for Smithery's scanner, which does not send the
# Mcp-Session-Id header on the second request (tools/list), causing a 400 that
# Smithery misreads as "Authorization Required".
_mcp_http_app = None
try:
    from mcp_server import mcp
    try:
        _mcp_http_app = mcp.http_app(path="/", stateless_http=True)
        logger.info("✅ MCP server loaded (stateless streamable HTTP)")
    except TypeError:
        # Older FastMCP versions don't have stateless_http — fall back
        _mcp_http_app = mcp.http_app(path="/")
        logger.info("✅ MCP server loaded (streamable HTTP, path='/')")
except Exception as e:
    logger.error(f"❌ MCP server failed to load: {e}", exc_info=True)


class PaymentRequired(Exception):
    """Carries an x402-compliant JSONResponse. Registered with FastAPI below."""
    def __init__(self, response: JSONResponse):
        self.response = response


@asynccontextmanager
async def _lifespan(app):
    # XUMM connectivity check
    await _verify_xumm()
    if _mcp_http_app is not None:
        # Pass the MCP http app itself (not the parent FastAPI app) so the
        # session manager stores its state in the correct app scope.
        async with _mcp_http_app.lifespan(_mcp_http_app):
            yield
    else:
        yield


app = FastAPI(
    title="AgentTrust Protocol Core",
    description=(
        "Trustless AI task verification with automatic XRPL payment release. "
        "Post a task spec and work submission — get a structured PASS/FAIL verdict. "
        "Escrowed XRP or RLUSD releases automatically on AI approval.\n\n"
        "**Trust layer stack:** four independent proof mechanisms buyers can require from sellers — "
        "(1) NFT from a trusted issuer, (2) XRPL domain verification, "
        "(3) W3C Verifiable Credential, (4) XRPL wallet trust score.\n\n"
        "**NFT Delivery-vs-Payment (DvP):** when the job deliverable is an NFT itself, "
        "enable DvP mode. On PASS the escrow enters PASS_AWAITING_NFT state; payment holds until "
        "the seller creates an NFTokenCreateOffer (Destination=buyer, Amount=0) and the buyer accepts "
        "it on-chain — both transfer and payment are then settled automatically. "
        "Register the offer via POST /escrow/{id}/nft-offer and poll status via GET /escrow/{id}/nft-status."
    ),
    lifespan=_lifespan,
)


@app.exception_handler(PaymentRequired)
async def _payment_required_handler(request: Request, exc: PaymentRequired):
    return exc.response


# ---------------------------------------------------------------------------
# 2b. CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # Cannot combine allow_credentials=True with allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)

# Smithery / MCP compatibility middleware — prevents "Authorization Required" prompt:
# 1. Block all OAuth discovery endpoints (404) so Smithery never detects auth.
# 2. Inject text/event-stream into Accept for MCP requests (FastMCP requires it
#    per spec; Smithery's scanner only sends application/json → 406 without this).
# 3. Strip WWW-Authenticate and OAuth Link headers from responses — newer FastMCP
#    versions add these headers automatically, which Smithery reads as "needs auth".
@app.middleware("http")
async def mcp_smithery_compat(request, call_next):
    path = request.url.path

    # Block every known OAuth/OIDC discovery path at both root and /mcp prefix
    blocked_oauth = {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
        "/mcp/.well-known/oauth-protected-resource",
        "/mcp/.well-known/oauth-authorization-server",
        "/mcp/.well-known/openid-configuration",
    }
    if path in blocked_oauth:
        from starlette.responses import Response as _SR
        return _SR(status_code=404)

    # Inject text/event-stream into Accept for MCP endpoint requests
    if path.rstrip("/") == "/mcp" or path.startswith("/mcp/"):
        accept = request.headers.get("accept", "")
        if "text/event-stream" not in accept:
            new_hdrs = [(k, v) for k, v in request.scope["headers"] if k.lower() != b"accept"]
            new_accept = (f"{accept}, text/event-stream" if accept else "application/json, text/event-stream").encode()
            new_hdrs.append((b"accept", new_accept))
            request.scope["headers"] = new_hdrs

    response = await call_next(request)

    # Strip any auth-advertising headers that would trigger Smithery's auth prompt.
    # FastMCP 2.3+ adds these even when no auth is configured.
    for hdr in ("www-authenticate", "link"):
        if hdr in response.headers:
            del response.headers[hdr]

    return response

# Mount MCP at /mcp/ (Streamable HTTP — Smithery and MCP clients POST here).
# We also mount at /mcp so Starlette's built-in redirect is replaced by our own
# clean mount, avoiding the 307 that confuses some clients.
if _mcp_http_app is not None:
    app.mount("/mcp", _mcp_http_app)
    logger.info("✅ MCP server mounted at /mcp")

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
    return {"status": "online", "version": "7.0", "service": "AgentTrust Referee", "playground": "/playground", "docs": "/docs"}

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
    return {"status": "online", "version": "7.0", "timestamp": datetime.now(timezone.utc)}

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
        "agentVersion": "9.0.0",
        "protocolVersion": "0.6.0",
        "provider": {"organization": "AgentTrust Protocol", "url": "https://xrpl-referee.onrender.com"},
        "capabilities": {"streaming": False, "pushNotifications": False, "multimodal": True, "escrow": True, "autoFinish": True, "rlusd": True, "jobBoard": True, "bidding": True},
        "authentication": {
            "schemes": ["x402", "x-payment-hash"],
            "description": (
                "x402 protocol supported. Send a request with no payment to receive a 402 with an "
                "X-Payment-Required header containing full payment details. Send 0.1 XRP to "
                "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on the XRPL, then retry with the transaction "
                "hash as the X-PAYMENT header (x402 standard) or x-payment-hash header (legacy)."
            )
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
            "Browse live XRP bounties on the AgentTrust marketplace. Built for autonomous agents. "
            "Supports four trust layers: NFT from trusted issuer, XRPL domain verification, "
            "W3C Verifiable Credential, and XRPL wallet trust score — buyers can require any combination. "
            "NFT Delivery-vs-Payment (DvP) mode: payment holds until the seller transfers an NFT to the buyer "
            "on-chain, then releases automatically — no sequential transaction risk. "
            "Implements the x402 payment protocol: call any paid endpoint without payment to receive "
            "a 402 with an X-Payment-Required header describing exactly how to pay in XRP."
        ),
        "url":         "https://xrpl-referee.onrender.com/mcp",
        "homepage":    "https://www.cryptovault.co.uk",
        "contact":     "hello@cryptovault.co.uk",
        "license":     "MIT",
        "transport":   ["http"],
        "auth":        {"type": "none"},
        "tools": [
            {"name": "audit_task",               "description": "Verify completed work against a task spec for 0.1 XRP. Returns PASS/FAIL with score and feedback."},
            {"name": "create_escrow_vault",       "description": "Lock XRP or RLUSD in XRPL crypto-condition escrow gated by AI verdict. Optional trust-layer fields: nft_dvp (bool — require NFT transfer before payment releases), required_nft_issuer (wallet address), required_domain (XRPL domain verification), required_vc_issuer_did (W3C VC issuer DID), proof_policy ('ALL' or 'ANY')."},
            {"name": "confirm_escrow_transaction","description": "Register an EscrowCreate tx hash to activate a vault."},
            {"name": "evaluate_escrow_work",      "description": "Submit proof of work. On PASS, payment releases automatically — no EscrowFinish needed. For NFT DvP jobs (nft_dvp=true), PASS sets status to PASS_AWAITING_NFT — seller must then create an NFTokenCreateOffer (Destination=buyer, Amount=0) and register it via POST /escrow/{id}/nft-offer before payment releases."},
            {"name": "get_escrow_info",           "description": "Retrieve task spec, status, and attempts remaining for an escrow vault."},
            {"name": "list_marketplace_jobs",     "description": "Browse live XRPL escrow bounties. Returns structured job data."},
            {"name": "post_job",                  "description": "Post a job to the job board. No fee or funds — workers bid, you negotiate and award."},
            {"name": "list_open_jobs",            "description": "Browse jobs posted by buyers that are open for bidding."},
            {"name": "submit_bid",                "description": "Submit a bid (price + proposal) on an open job."},
            {"name": "view_job",                  "description": "View job details and all current bids."},
            {"name": "award_job",                 "description": "Accept a bid. Returns worker address and agreed price to use in create_escrow_vault()."},
            {"name": "list_marketplace_skills",   "description": "Browse skill agents/humans offering services. Filter by category and rate. Supports direct hire."},
            {"name": "create_skill_listing",      "description": "List your skills publicly for 30 days (0.1 XRP/month). Buyers can direct-hire you from the listing."},
            {"name": "direct_hire",               "description": "Get a skill provider's wallet address for immediate escrow creation — no bidding needed."},
            {"name": "get_rlusd_quote",           "description": "Get live XRP to RLUSD conversion quote via the XRPL DEX."},
            {"name": "get_xrp_price",             "description": "Get current live XRP/USD and XRP/GBP prices."},
            {"name": "get_wallet_trust_score",    "description": "GET /wallet/score/{address} — compute an XRPL wallet's trust score (0–100) from on-chain signals: account age, balance, transaction count, NFTs held, domain field. Higher scores indicate more established, trustworthy wallets."},
            {"name": "verify_nft_proof",          "description": "POST /nft/verify — verify that an XRPL NFT exists in a wallet, was minted by a required issuer, and contains required metadata fields. Used to confirm event-based proof (ticket purchased, cargo shipped, etc)."},
            {"name": "verify_domain_ownership",   "description": "POST /domain/verify — verify that an XRPL wallet is cryptographically linked to a domain via the account Domain field and xrp-ledger.toml. Proves the wallet owner controls the specified organisation's domain."},
            {"name": "verify_vc",                 "description": "POST /vc/verify — verify a W3C Verifiable Credential JWT. Checks expiry, issuer DID, credential type, and optionally resolves the DID via the Universal Resolver. Accepts credentials from any W3C-compliant issuer."},
            {"name": "register_nft_dvp_offer",    "description": "POST /escrow/{id}/nft-offer — after a PASS verdict on an NFT DvP escrow, seller registers their on-chain NFTokenCreateOffer (Destination=buyer, Amount=0). System verifies the offer on XRPL and emails buyer to accept. Payment releases automatically once buyer accepts."},
            {"name": "check_nft_dvp_status",      "description": "GET /escrow/{id}/nft-status — poll whether the buyer has accepted the NFT offer yet. Returns accepted/pending/expired. Triggers automatic escrow release when accepted."},
            {"name": "search_verified_companies", "description": "GET /gleif/search?q= — search SEC EDGAR for US public companies by name. Returns file number and company name. Use to find a company's verified identity before requiring their XRPL wallet as a trusted NFT issuer."},
            {"name": "company_xrpl_lookup",       "description": "GET /gleif/xrpl-lookup?q= — search for a company by name via SEC EDGAR, then attempt to find their registered XRPL wallet address in the AgentTrust registry. Green result = SEC EDGAR verified + XRPL wallet confirmed."},
            {"name": "list_trusted_issuers",      "description": "GET /nft/issuers — list all verified trusted NFT issuers in the AgentTrust registry. These are organisations (shipping companies, ticket platforms, certification bodies) whose XRPL wallet has been verified against their domain and SEC EDGAR record."},
            {"name": "register_as_issuer",        "description": "POST /nft/issuers — register your organisation as a trusted NFT issuer. Provide your XRPL wallet, organisation name, category, website. Pending manual verification against SEC EDGAR + domain records."},
            {"name": "create_eth_challenge",      "description": "POST /eth/challenge — generate an EIP-191 challenge string for an Ethereum address. The address holder must sign this with their ETH wallet to prove ownership. Use before submitting an Ethereum address as identity proof."},
            {"name": "verify_eth_signature",      "description": "POST /eth/verify-signature — verify that an Ethereum address signed the challenge string. Confirms the submitter genuinely controls the ETH address, preventing fake address claims."},
        ],
        "tags": ["xrpl", "payments", "escrow", "ai-agent", "verification", "bounty", "autonomous", "web3", "nft", "trust", "identity"],
    }


@app.get("/.well-known/mcp-config")
def serve_mcp_config():
    """Smithery External MCP config schema — declares no authentication required."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://xrpl-referee.onrender.com/.well-known/mcp-config",
        "type": "object",
        "title": "AgentTrust Referee",
        "description": "No authentication required. Connect directly to the MCP endpoint at /mcp.",
        "properties": {},
        "required": [],
        "additionalProperties": False,
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
        "description_for_human": (
            "Trustless AI task verification with automatic XRPL payment release. "
            "Post a task spec and a work submission — get a structured PASS/FAIL verdict. "
            "Optional escrow: lock XRP or RLUSD on-chain, funds release automatically on approval."
        ),
        "description_for_model": (
            "Use this tool to verify whether a seller has completed a task to specification and auto-release escrowed funds. "
            "This API implements the x402 payment protocol: if you call any paid endpoint without a payment, you will receive "
            "a 402 response with an X-Payment-Required header containing base64-encoded JSON that tells you exactly how much "
            "XRP to send, where to send it, and which header to use. Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on "
            "the XRPL, then retry with the transaction hash as the X-PAYMENT header (or legacy x-payment-hash header). "
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
    # Marketplace — v8
    category          = Column(String,  default="default")  # task category for filtering
    marketplace_tags  = Column(Text,    nullable=True)      # JSON array of tags
    # Open bounty — v9: tracks who created the on-chain EscrowCreate (buyer or referee)
    escrow_owner      = Column(String,  nullable=True)      # Account field of EscrowCreate tx
    # NFT proof requirements — v10
    require_nft_proof     = Column(Boolean, default=False)   # True = NFT proof required (any issuer)
    required_nft_issuer   = Column(String, nullable=True)   # optional: restrict to this issuer wallet
    required_nft_metadata = Column(Text,   nullable=True)   # JSON: required key-value pairs in NFT URI
    # Trust layer v11
    required_domain       = Column(String, nullable=True)   # XRPL domain field requirement
    required_vc_issuer_did = Column(String, nullable=True)  # W3C VC required issuer DID
    required_vc_type      = Column(String, nullable=True)   # W3C VC required type
    proof_policy          = Column(String, default="ALL")   # "ALL" or "ANY"
    # NFT Delivery vs Payment (DvP) — v10
    nft_dvp              = Column(Boolean,  default=False)   # True if NFT transfer required
    nft_dvp_token_id     = Column(String,   nullable=True)   # The NFT token ID to be transferred
    nft_dvp_offer_id     = Column(String,   nullable=True)   # The NFTokenCreateOffer ID on XRPL
    nft_dvp_offer_expiry = Column(DateTime, nullable=True)   # When the offer expires
    nft_dvp_status       = Column(String,   nullable=True)   # "pending_offer" | "offer_created" | "accepted" | "expired"


class JobPosting(Base):
    """
    A buyer agent's request for work — no funds held.
    Workers bid; buyer awards the job; then buyer creates the bilateral escrow.
    """
    __tablename__ = "job_posting"
    id             = Column(String,   primary_key=True, index=True)  # e.g. JOB-XXXX-YYYY
    title          = Column(String,   nullable=False)
    description    = Column(Text,     nullable=False)
    budget_xrp     = Column(Float,    nullable=True)    # indicative max budget
    buyer_address  = Column(String,   nullable=False)
    buyer_name     = Column(String,   nullable=True)
    category       = Column(String,   default="default")
    tags           = Column(Text,     nullable=True)    # JSON array
    status         = Column(String,   default="open")   # open, awarded, cancelled, expired
    created_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at     = Column(DateTime, nullable=True)
    awarded_bid_id     = Column(String,   nullable=True)    # winning bid id
    escrow_id          = Column(String,   nullable=True)    # set by buyer after escrow created
    buyer_email        = Column(String,   nullable=True)    # optional — notified on new bids
    buyer_callback_url = Column(String,   nullable=True)    # optional — agent webhook on new bids
    award_token_hash   = Column(String,   nullable=True)    # SHA-256 of the one-time award token
    award_token        = Column(String,   nullable=True)    # plaintext — needed to embed in bid emails
    required_nft_issuer   = Column(String,   nullable=True)  # require NFT from this issuer wallet
    required_nft_metadata = Column(Text,     nullable=True)  # JSON: required key-value pairs in NFT URI
    # Trust layer v11
    required_domain        = Column(String,  nullable=True)
    required_vc_issuer_did = Column(String,  nullable=True)
    required_vc_type       = Column(String,  nullable=True)
    proof_policy           = Column(String,  default="ALL")


class Bid(Base):
    """A worker agent's bid on an open job posting."""
    __tablename__ = "bid"
    id             = Column(String,   primary_key=True, index=True)   # BID-XXXX-YYYY
    job_id         = Column(String,   nullable=False, index=True)
    worker_address = Column(String,   nullable=False)
    worker_name    = Column(String,   nullable=True)
    worker_email   = Column(String,   nullable=True)   # optional — triggers award + escrow emails
    callback_url   = Column(String,   nullable=True)   # optional — agent webhook on award
    chat_token     = Column(String,   nullable=True)   # plaintext token for worker to access chat
    chat_token_hash= Column(String,   nullable=True)   # SHA-256 hash stored for verification
    proposed_xrp   = Column(Float,    nullable=False)
    proposal       = Column(Text,     nullable=False)   # pitch / approach
    status         = Column(String,   default="pending")  # pending, accepted, rejected
    created_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    xrpl_trust_score = Column(Integer, nullable=True)  # 0-100 XRPL wallet trust score


class JobMessage(Base):
    """A chat message between buyer and worker on a job."""
    __tablename__ = "job_message"
    id           = Column(Integer,  primary_key=True, autoincrement=True)
    job_id       = Column(String,   nullable=False, index=True)
    bid_id       = Column(String,   nullable=True)   # which worker's thread (if worker)
    sender_role  = Column(String,   nullable=False)  # "buyer" or "worker"
    sender_name  = Column(String,   nullable=True)
    message      = Column(Text,     nullable=False)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class NftIssuer(Base):
    __tablename__ = "nft_issuer"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String, nullable=False, index=True)  # primary / first wallet
    wallet_addresses = Column(Text, nullable=True)               # JSON array of all wallets
    name           = Column(String, nullable=False)
    category       = Column(String, nullable=True)   # e.g. "logistics", "freelance", "iot"
    description    = Column(String, nullable=True)
    website        = Column(String, nullable=True)
    verified       = Column(String, default="pending")  # "pending", "verified", "revoked"
    created_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    contact_email  = Column(String, nullable=True)
    lei            = Column(String, nullable=True)
    nft_types      = Column(String, nullable=True)

    def all_wallets(self) -> list:
        """Return all registered wallets for this issuer."""
        try:
            parsed = json.loads(self.wallet_addresses or "[]")
            wallets = [w for w in parsed if w]
        except Exception:
            wallets = []
        # Always include primary wallet
        if self.wallet_address and self.wallet_address not in wallets:
            wallets.insert(0, self.wallet_address)
        return wallets

    def set_wallets(self, wallets: list):
        """Persist a wallet list, keeping primary in sync."""
        wallets = [w.strip() for w in wallets if w and w.strip()]
        wallets = list(dict.fromkeys(wallets))  # deduplicate, preserve order
        if wallets:
            self.wallet_address = wallets[0]
        self.wallet_addresses = json.dumps(wallets)


class SkillListing(Base):
    __tablename__ = "skill_listing"
    id           = Column(String,   primary_key=True, index=True)
    title        = Column(String,   nullable=False)
    description  = Column(Text,     nullable=False)
    category     = Column(String,   default="default")
    rate         = Column(String,   nullable=True)    # human-readable rate string
    rate_xrp     = Column(Float,    nullable=True)    # numeric rate in XRP for filtering
    poster       = Column(String,   nullable=True)    # XRPL address — used for direct hire
    poster_name  = Column(String,   nullable=True)
    tags         = Column(Text,     nullable=True)    # JSON array
    fee_hash     = Column(String,   unique=True, nullable=False)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at   = Column(DateTime, nullable=True)
    status       = Column(String,   default="ACTIVE")


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
        # v8 columns
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS category               VARCHAR DEFAULT 'default'",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS marketplace_tags        TEXT",
        # v9 columns — escrow_owner tracks who signed EscrowCreate (buyer or referee)
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS escrow_owner           VARCHAR",
        # v10 tables — job board + bidding (no funds held by referee)
        """CREATE TABLE IF NOT EXISTS job_posting (
            id              VARCHAR PRIMARY KEY,
            title           VARCHAR NOT NULL,
            description     TEXT    NOT NULL,
            budget_xrp      FLOAT,
            buyer_address   VARCHAR NOT NULL,
            buyer_name      VARCHAR,
            category        VARCHAR DEFAULT 'default',
            tags            TEXT,
            status          VARCHAR DEFAULT 'open',
            created_at      TIMESTAMP,
            expires_at      TIMESTAMP,
            awarded_bid_id  VARCHAR,
            escrow_id       VARCHAR,
            buyer_email     VARCHAR,
            buyer_callback_url VARCHAR
        )""",
        """CREATE TABLE IF NOT EXISTS bid (
            id              VARCHAR PRIMARY KEY,
            job_id          VARCHAR NOT NULL,
            worker_address  VARCHAR NOT NULL,
            worker_name     VARCHAR,
            worker_email    VARCHAR,
            proposed_xrp    FLOAT   NOT NULL,
            proposal        TEXT    NOT NULL,
            status          VARCHAR DEFAULT 'pending',
            created_at      TIMESTAMP
        )""",
        "ALTER TABLE bid ADD COLUMN IF NOT EXISTS worker_email        VARCHAR",
        "ALTER TABLE bid ADD COLUMN IF NOT EXISTS callback_url        VARCHAR",
        "ALTER TABLE bid ADD COLUMN IF NOT EXISTS chat_token          VARCHAR",
        "ALTER TABLE bid ADD COLUMN IF NOT EXISTS chat_token_hash     VARCHAR",
        """CREATE TABLE IF NOT EXISTS job_message (
            id           SERIAL PRIMARY KEY,
            job_id       VARCHAR NOT NULL,
            bid_id       VARCHAR,
            sender_role  VARCHAR NOT NULL,
            sender_name  VARCHAR,
            message      TEXT    NOT NULL,
            created_at   TIMESTAMP
        )""",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS buyer_email         VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS buyer_callback_url  VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS award_token_hash    VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS award_token         VARCHAR",
        """CREATE TABLE IF NOT EXISTS skill_listing (
            id          VARCHAR PRIMARY KEY,
            title       VARCHAR NOT NULL,
            description TEXT    NOT NULL,
            category    VARCHAR DEFAULT 'default',
            rate        VARCHAR,
            rate_xrp    FLOAT,
            poster      VARCHAR,
            poster_name VARCHAR,
            tags        TEXT,
            fee_hash    VARCHAR UNIQUE NOT NULL,
            created_at  TIMESTAMP,
            expires_at  TIMESTAMP,
            status      VARCHAR DEFAULT 'ACTIVE'
        )""",
        "ALTER TABLE skill_listing ADD COLUMN IF NOT EXISTS rate_xrp FLOAT",
        # v10 NFT proof columns
        """CREATE TABLE IF NOT EXISTS nft_issuer (
            id             SERIAL PRIMARY KEY,
            wallet_address VARCHAR UNIQUE NOT NULL,
            name           VARCHAR NOT NULL,
            category       VARCHAR,
            description    VARCHAR,
            website        VARCHAR,
            verified       VARCHAR DEFAULT 'pending',
            created_at     TIMESTAMP
        )""",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS require_nft_proof     BOOLEAN DEFAULT FALSE",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS required_nft_issuer   VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS required_nft_metadata  TEXT",
        # v10 job_posting NFT columns
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS required_nft_issuer   VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS required_nft_metadata  TEXT",
        # v11 trust layer columns
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS required_domain        VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS required_vc_issuer_did VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS required_vc_type       VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS min_passport_score     FLOAT",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS required_domain         VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS required_vc_issuer_did  VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS required_vc_type        VARCHAR",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS min_passport_score      FLOAT",
        # new features
        "ALTER TABLE bid ADD COLUMN IF NOT EXISTS xrpl_trust_score INTEGER",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS proof_policy VARCHAR DEFAULT 'ALL'",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS proof_policy VARCHAR DEFAULT 'ALL'",
        # v10 NFT DvP columns
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS nft_dvp              BOOLEAN DEFAULT FALSE",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS nft_dvp_token_id     VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS nft_dvp_offer_id     VARCHAR",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS nft_dvp_offer_expiry TIMESTAMP",
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS nft_dvp_status       VARCHAR",
        # trusted issuer registry extended fields
        "ALTER TABLE nft_issuer ADD COLUMN IF NOT EXISTS contact_email VARCHAR",
        "ALTER TABLE nft_issuer ADD COLUMN IF NOT EXISTS lei            VARCHAR",
        "ALTER TABLE nft_issuer ADD COLUMN IF NOT EXISTS nft_types      VARCHAR",
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
MIN_ESCROW_XRP  = 0.000001   # 1 drop — XRPL EscrowCreate minimum
RIPPLE_EPOCH    = 946684800  # Unix timestamp of the XRP Ledger epoch (2000-01-01T00:00:00Z)

RLUSD_ISSUER   = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY = "RLUSD"
RLUSD_HEX      = "524C555344000000000000000000000000000000"

# lsfAllowTrustLineLocking (0x20000000) — issuer must set this for XLS-85 token escrow to work.
# Ripple has not yet enabled it on the RLUSD issuer account (as of May 2026).
# We cache the result for 1 hour so we're not polling the ledger on every escrow request.
LSF_ALLOW_TRUSTLINE_LOCKING = 0x20000000
_rlusd_escrow_supported_cache: dict = {"value": None, "checked_at": 0}
RLUSD_ESCROW_CACHE_TTL = 3600  # seconds

GITCOIN_API_KEY  = None  # Removed — Gitcoin Passport no longer used
GITCOIN_SCORER_ID = None

RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
RESEND_FROM          = os.getenv("RESEND_FROM", "noreply@cryptovault.co.uk")
DELIVERY_EXPIRY_DAYS = 7
SITE_URL             = os.getenv("SITE_URL", "https://www.cryptovault.co.uk")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY
    logger.info("✅ Resend email configured")
else:
    logger.warning("⚠️ RESEND_API_KEY missing — email notifications disabled")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_API_KEY")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    logger.info("✅ Telegram notifications configured")
else:
    logger.warning("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — Telegram notifications disabled")


async def _telegram_notify(text: str) -> None:
    """Fire-and-forget Telegram message to the owner chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning(f"⚠️ Telegram notify failed: {e}")

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


# ---------------------------------------------------------------------------
# x402 PAYMENT REQUIRED HELPER
# ---------------------------------------------------------------------------
def _raise_402(resource: str, error: str, min_xrp: float = None) -> None:
    """Raise an x402-compliant 402 Payment Required exception.

    Includes a base64-encoded X-Payment-Required header so that any HTTP
    client or AI agent implementing the x402 standard can auto-discover
    payment requirements without reading documentation.

    Spec: https://x402.org
    """
    required_xrp = min_xrp if min_xrp is not None else MIN_FEE_XRP
    body = {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": "xrpl-mainnet",
            "maxAmountRequired": str(int(required_xrp * 1_000_000)),
            "resource": resource,
            "payTo": PROTOCOL_WALLET,
            "asset": "XRP",
            "maxTimeoutSeconds": 300,
            "extra": {
                "instruction": (
                    f"Send {required_xrp} XRP to {PROTOCOL_WALLET} on the XRPL, "
                    "then include the transaction hash as the X-PAYMENT header."
                ),
                "headerName": "X-PAYMENT",
            },
        }],
        "error": error,
    }
    encoded = base64.b64encode(json.dumps(body).encode()).decode()
    raise PaymentRequired(JSONResponse(status_code=402, content=body, headers={"X-Payment-Required": encoded}))


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
        if not res.is_success:
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
        if not res.is_success:
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
            if res.is_success:
                name = res.json().get("application", {}).get("name", "unknown")
                logger.info(f"🔌 XUMM SDK connected: {name}")
            else:
                logger.warning(f"⚠️ XUMM ping failed: {res.status_code}")
    except Exception as e:
        logger.warning(f"⚠️ XUMM ping error: {e}")



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
    # Marketplace metadata
    category:           str             = "default"
    tags:               Optional[list[str]] = None
    # NFT proof requirements
    require_nft_proof:     Optional[bool] = None  # True = any valid NFT accepted
    required_nft_issuer:   Optional[str]  = None  # optional: restrict to this issuer wallet
    required_nft_metadata: Optional[dict] = None  # optional: require key-value pairs in NFT URI
    # Trust layer v11
    required_domain:        Optional[str]   = None
    required_vc_issuer_did: Optional[str]   = None
    required_vc_type:       Optional[str]   = None
    proof_policy:           Optional[str]   = "ALL"
    # Gitcoin Passport removed — fields kept here for backwards compat with old clients
    min_passport_score:     Optional[float] = None  # ignored, kept for API compatibility
    # NFT Delivery-vs-Payment mode
    nft_dvp: bool = False  # enable NFT delivery-vs-payment mode

class AuditRequest(BaseModel):
    escrow_id:           str
    work:                str
    worker_attachments:  Optional[list[Attachment]] = None
    callback_url:        Optional[str]  = None
    task_category:       str            = "default"
    require_consensus:   bool           = False
    # Evidence links — up to 3 URLs the seller provides as proof
    evidence_links:      Optional[list[str]] = None
    # NFT proof fields
    nft_token_id: Optional[str] = None   # NFT token ID as proof
    nft_wallet:   Optional[str] = None   # wallet holding the NFT (defaults to worker_address)
    # Trust layer v11
    vc_jwt:              Optional[str] = None   # W3C Verifiable Credential JWT
    # Gitcoin Passport removed — field kept for backwards compat
    passport_eth_address: Optional[str] = None  # ignored

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
async def verify_fee_payment(fee_hash: str, escrow_id: str, db: Session, min_xrp: float = None, resource: str = "/") -> dict:
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
        _raise_402(resource, "Transaction hash not found on the XRPL ledger.", min_xrp=required_xrp)

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
        _raise_402(resource, f"Wrong destination. Expected {PROTOCOL_WALLET}, got {dest}.", min_xrp=required_xrp)
    if isinstance(raw_amount, dict):
        raise HTTPException(status_code=400, detail="Protocol fees must be paid in XRP, not issued currency.")

    amount_xrp = round(int(raw_amount) / 1_000_000, 6)
    if amount_xrp < (required_xrp - 0.000001):
        _raise_402(
            resource,
            f"Insufficient fee. Required ≥{required_xrp} XRP, received {amount_xrp:.6f} XRP.",
            min_xrp=required_xrp,
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

    import asyncio
    asyncio.create_task(_telegram_notify(
        f"💰 *Protocol fee received*\n"
        f"Amount: `{amount_xrp} XRP`\n"
        f"From: `{sender}`\n"
        f"Escrow: `{escrow_id}`\n"
        f"Resource: `{resource}`\n"
        f"Hash: `{fee_hash[:16]}…`"
    ))

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


async def check_rlusd_escrow_supported() -> bool:
    """
    Returns True if the RLUSD issuer has lsfAllowTrustLineLocking set,
    meaning XLS-85 token escrow is live for RLUSD.
    Result is cached for RLUSD_ESCROW_CACHE_TTL seconds to avoid hammering the ledger.
    """
    import time
    cache = _rlusd_escrow_supported_cache
    if cache["value"] is not None and (time.time() - cache["checked_at"]) < RLUSD_ESCROW_CACHE_TTL:
        return cache["value"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(
                XRPL_URL,
                json={"method": "account_info", "params": [{"account": RLUSD_ISSUER, "ledger_index": "validated"}]},
            )
            flags = res.json().get("result", {}).get("account_data", {}).get("Flags", 0)
            supported = bool(flags & LSF_ALLOW_TRUSTLINE_LOCKING)
            cache["value"]      = supported
            cache["checked_at"] = time.time()
            logger.info(f"🔍 RLUSD escrow supported (lsfAllowTrustLineLocking): {supported} (flags={hex(flags)})")
            return supported
    except Exception as e:
        logger.warning(f"⚠️ RLUSD escrow flag check failed: {e} — assuming not supported")
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


async def send_job_posted_email(
    buyer_email:  str,
    buyer_name:   str,
    job_id:       str,
    job_title:    str,
    award_token:  str,
):
    """Confirm to a human job poster that their job is live, and deliver their award token."""
    if not RESEND_API_KEY or not buyer_email:
        return
    manage_url = f"{SITE_URL}/marketplace?track_job={job_id}&token={award_token}"
    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      buyer_email,
            "subject": f"Job posted — {job_title}",
            "html": f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>{_email_styles()}</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>Your job is live</h1>
  <p>Hi{' ' + buyer_name if buyer_name else ''}, your job has been posted to the AgentTrust marketplace
     and is now open for bids.</p>
  <div class="detail"><span>Job ID</span><br>
    <strong style="font-size:1.1rem;letter-spacing:.03em;">{job_id}</strong>
    <br><span style="font-size:.78rem;color:#5c5c6e;">← paste this into "Track job ID" to check bids</span>
  </div>
  <div class="detail"><span>Job title</span><br><strong>{job_title}</strong></div>
  <div style="margin:1rem 0;padding:.9rem;background:#1e1e2e;border:1px solid #f59e0b;border-radius:6px;">
    <div style="font-weight:700;color:#f59e0b;margin-bottom:.4rem;">⚠️ Your Award Token — keep this safe</div>
    <div style="font-family:monospace;word-break:break-all;font-size:.8rem;color:#e2e2e2;">{award_token}</div>
    <div style="margin-top:.5rem;font-size:.75rem;color:#888;">This token lets you accept a bid and cancel the job. Never share it. If you lose it, contact support.</div>
  </div>
  <p style="margin-top:1rem;">
    <a href="{manage_url}" style="display:inline-block;padding:10px 24px;background:#0066FF;color:#fff;border-radius:6px;text-decoration:none;font-weight:600;font-size:.9rem;">Manage your job</a>
  </p>
  <p style="font-size:.82rem;color:#5c5c6e;">You'll receive another email each time a bid is submitted, with a one-click award link.</p>
  <div class="footer">AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a></div>
</div></body></html>""",
        })
        logger.info(f"📧 Job posted email sent to {buyer_email} for {job_id}")
    except Exception as e:
        logger.error(f"❌ Job posted email failed for {job_id}: {e}")


async def send_new_bid_buyer_email(
    buyer_email:    str,
    buyer_name:     str,
    job_id:         str,
    job_title:      str,
    bid_id:         str,
    worker_name:    str,
    worker_address: str,
    proposed_xrp:   float,
    proposal:       str,
    total_bids:     int,
    award_token:    str,
):
    """Notify the job poster that a new bid has been received, with a direct award link."""
    if not RESEND_API_KEY or not buyer_email:
        return
    try:
        short = worker_name or (worker_address[:6] + "…" + worker_address[-4:])
        award_url = (
            f"{SITE_URL}/marketplace"
            f"?award_job={job_id}&award_bid={bid_id}&token={award_token}"
        )
        view_url = f"{SITE_URL}/marketplace?track_job={job_id}&token={award_token}"
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      buyer_email,
            "subject": f"New bid on your job — {job_title}",
            "html": f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>{_email_styles()}</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>New bid received</h1>
  <p>Hi{' ' + buyer_name if buyer_name else ''}, <strong>{short}</strong> has bid <strong>{proposed_xrp} XRP</strong> on <strong>{job_title}</strong>. {total_bids} bid{'s' if total_bids != 1 else ''} total.</p>
  <div style="margin:1.25rem 0;display:flex;gap:10px;flex-wrap:wrap;">
    <a href="{award_url}" style="display:inline-block;padding:12px 22px;background:#10b981;color:#000;font-weight:700;font-family:'IBM Plex Sans',sans-serif;font-size:.9rem;border-radius:8px;text-decoration:none;">✓ Award this bid</a>
    <a href="{view_url}" style="display:inline-block;padding:12px 22px;background:#1a2230;color:#e2e8f0;font-weight:600;font-family:'IBM Plex Sans',sans-serif;font-size:.9rem;border-radius:8px;text-decoration:none;border:1px solid rgba(255,255,255,.12);">View all bids</a>
  </div>
  <div class="detail"><span>Their proposal</span><br><span style="font-size:.85rem;">{proposal[:400]}{'…' if len(proposal) > 400 else ''}</span></div>
  <p style="font-size:.78rem;color:#9999aa;margin-top:1rem;">These links are unique to you — do not forward this email to the bidder.</p>
  <div class="footer">AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a></div>
</div></body></html>""",
        })
        logger.info(f"📧 New-bid buyer email sent to {buyer_email} for job {job_id} (bid {bid_id})")
    except Exception as e:
        logger.error(f"❌ New-bid buyer email failed for job {job_id}: {e}")


async def fire_new_bid_buyer_webhook(
    callback_url:   str,
    job_id:         str,
    job_title:      str,
    bid_id:         str,
    worker_address: str,
    worker_name:    str,
    proposed_xrp:   float,
    proposal:       str,
    total_bids:     int,
):
    """POST new-bid notification to the job poster's callback URL (fire-and-forget)."""
    payload = {
        "event":          "job.new_bid",
        "job_id":         job_id,
        "job_title":      job_title,
        "bid_id":         bid_id,
        "worker_address": worker_address,
        "worker_name":    worker_name,
        "proposed_xrp":   proposed_xrp,
        "proposal":       proposal,
        "total_bids":     total_bids,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(callback_url, json=payload)
            logger.info(f"📡 New-bid webhook delivered to {callback_url} (HTTP {r.status_code}) for job {job_id}")
    except Exception as e:
        logger.error(f"❌ New-bid webhook failed for job {job_id} → {callback_url}: {e}")


async def send_bid_received_email(
    worker_email:   str,
    worker_name:    str,
    bid_id:         str,
    job_id:         str,
    job_title:      str,
    proposed_xrp:   float,
    chat_token:     str = "",
    trust_score:    int = None,
    trust_signals:  dict = None,
):
    """Confirm to a human bidder that their bid was received."""
    if not RESEND_API_KEY or not worker_email:
        return
    chat_url  = f"{SITE_URL}/marketplace?chat_bid={bid_id}&chat_token={chat_token}" if chat_token else None
    track_url = chat_url or f"{SITE_URL}/marketplace"
    chat_section = (
        f'<p style="margin-top:1rem;"><a href="{chat_url}" '
        f'style="display:inline-block;padding:10px 24px;background:#0066FF;color:#fff;border-radius:6px;'
        f'text-decoration:none;font-weight:600;font-size:.9rem;">💬 Open Job Chat</a></p>'
        f'<p style="font-size:.8rem;color:#5c5c6e;">Use this link to chat with the buyer about the job. Keep it private.</p>'
    ) if chat_token else ""

    # Trust score badge
    trust_section = ""
    if trust_score is not None:
        if trust_score >= 60:
            badge_color = "#10b981"; badge_text = f"🟢 Trust: {trust_score}/100"
        elif trust_score >= 30:
            badge_color = "#f59e0b"; badge_text = f"🟡 Trust: {trust_score}/100"
        else:
            badge_color = "#ef4444"; badge_text = f"🔴 Trust: {trust_score}/100"
        signals = trust_signals or {}
        age_days = signals.get("age_days", "—")
        balance_xrp = signals.get("balance_xrp", "—")
        has_domain = "Yes" if signals.get("has_domain") else "No"
        trust_section = f"""
  <div style="margin-top:1rem;padding:.85rem 1rem;background:#f8f9fc;border-radius:8px;border:1px solid #eee;">
    <div style="font-weight:700;font-size:.85rem;margin-bottom:.4rem;">Wallet Trust Score</div>
    <div style="display:inline-block;padding:4px 12px;border-radius:20px;background:{badge_color};color:#fff;font-weight:700;font-size:.82rem;">{badge_text}</div>
    <div style="margin-top:.6rem;font-size:.78rem;color:#5c5c6e;">
      Age: ~{age_days} days &nbsp;·&nbsp; Balance: {balance_xrp} XRP &nbsp;·&nbsp; Domain: {has_domain}
    </div>
  </div>"""
    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      worker_email,
            "subject": f"Bid received — {job_title}",
            "html": f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>{_email_styles()}</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>Your bid has been received</h1>
  <p>Hi{' ' + worker_name if worker_name else ''}, your bid of <strong>{proposed_xrp} XRP</strong>
     on the following job has been successfully submitted:</p>
  <div class="detail"><span>Job ID</span><br><strong style="font-size:1.1rem;letter-spacing:.03em;">{job_id}</strong>
    <br><span style="font-size:.78rem;color:#5c5c6e;">← paste this into "Track job ID" on the marketplace</span></div>
  <div class="detail"><span>Job title</span><br><strong>{job_title}</strong></div>
  <div class="detail"><span>Your offer</span><br><strong>{proposed_xrp} XRP</strong></div>
  <div class="detail"><span>Your bid reference</span><br><span style="font-size:.85rem;color:#5c5c6e;">{bid_id}</span></div>
  <p>The buyer will review all bids and you'll receive another email if yours is accepted.
     No action is needed from you right now.</p>
  {trust_section}
  {chat_section}
  <div class="footer">
    AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a>
  </div>
</div></body></html>""",
        })
        logger.info(f"📧 Bid received email sent to {worker_email} for bid {bid_id}")
    except Exception as e:
        logger.error(f"❌ Bid received email failed for {bid_id}: {e}")


async def fire_bid_awarded_webhook(
    callback_url: str,
    bid_id:       str,
    job_id:       str,
    job_title:    str,
    agreed_xrp:   float,
    worker_address: str,
):
    """POST award notification to an agent's callback URL (fire-and-forget)."""
    payload = {
        "event":          "bid.awarded",
        "bid_id":         bid_id,
        "job_id":         job_id,
        "job_title":      job_title,
        "agreed_xrp":     agreed_xrp,
        "worker_address": worker_address,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(callback_url, json=payload)
            logger.info(f"📡 Award webhook delivered to {callback_url} (HTTP {r.status_code}) for bid {bid_id}")
    except Exception as e:
        logger.error(f"❌ Award webhook failed for bid {bid_id} → {callback_url}: {e}")


async def send_bid_awarded_email(
    worker_email:  str,
    worker_name:   str,
    job_id:        str,
    job_title:     str,
    buyer_name:    str,
    agreed_xrp:    float,
):
    """
    Notify a human worker that their bid was accepted.
    Sent at award time — before the escrow is created — so they know to watch for
    a second email (the escrow receipt) once the buyer locks the funds.
    """
    if not RESEND_API_KEY or not worker_email:
        return
    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      worker_email,
            "subject": f"🏆 Your bid was accepted — {job_title}",
            "html": f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>{_email_styles()}</style></head><body><div class="card">
  <div class="logo">AgentTrust<span>.</span></div>
  <h1>Your bid was accepted!</h1>
  <p>Hi{' ' + worker_name if worker_name else ''}, <strong>{buyer_name}</strong> has accepted
     your bid of <strong>{agreed_xrp} XRP</strong> for:</p>
  <div class="detail"><span>Job</span><br><strong>{job_title}</strong></div>
  <div class="detail"><span>Agreed amount</span><br><strong>{agreed_xrp} XRP</strong></div>
  <p>The buyer is now creating the escrow to lock your payment on the XRP Ledger.
     You will receive another email shortly with your receipt code and a link to
     submit your work on the AgentTrust website.</p>
  <p style="font-size:.85rem;color:#5c5c6e;">
     Payment is held securely on-chain and released automatically when the AI
     referee approves your submission — no manual claim required.
  </p>
  <div class="footer">
    AgentTrust · <a href="{SITE_URL}" style="color:#0066FF;">cryptovault.co.uk</a>
  </div>
</div></body></html>""",
        })
        logger.info(f"📧 Bid award email sent to {worker_email} for job {job_id}")
    except Exception as e:
        logger.error(f"❌ Bid award email failed for job {job_id}: {e}")


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
# 11c. XRPL NFT OWNERSHIP VERIFICATION
# ---------------------------------------------------------------------------
async def verify_nft_ownership(wallet_address: str, nft_token_id: str, required_issuer: str = None, required_metadata: dict = None) -> dict:
    """
    Verify an NFT exists on XRPL, is owned by wallet_address,
    optionally issued by required_issuer, and optionally contains required_metadata fields.
    Returns dict with verified bool and detail string.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(XRPL_URL, json={
            "method": "account_nfts",
            "params": [{"account": wallet_address, "limit": 400}]
        })
        data = res.json()

    nfts = data.get("result", {}).get("account_nfts", [])
    target = next((n for n in nfts if n.get("NFTokenID") == nft_token_id), None)

    if not target:
        return {"verified": False, "detail": f"NFT {nft_token_id} not found in wallet {wallet_address}."}

    if required_issuer and target.get("Issuer") != required_issuer:
        actual = target.get("Issuer", "unknown")
        return {"verified": False, "detail": f"NFT was not issued by the required issuer. Expected {required_issuer}, got {actual}."}

    # Decode URI (hex-encoded)
    uri_hex = target.get("URI", "")
    uri_str = ""
    if uri_hex:
        try:
            uri_str = bytes.fromhex(uri_hex).decode("utf-8", errors="replace")
        except Exception:
            uri_str = uri_hex

    # Check required metadata fields if specified
    if required_metadata:
        nft_data = {}
        # Try parsing URI as JSON
        try:
            nft_data = json.loads(uri_str)
        except Exception:
            # Try as URL with query params or just treat as string
            pass

        missing = []
        mismatched = []
        for key, expected_val in required_metadata.items():
            if key not in nft_data:
                missing.append(key)
            elif str(nft_data[key]).lower() != str(expected_val).lower():
                mismatched.append(f"{key}: expected '{expected_val}', got '{nft_data[key]}'")

        if missing:
            return {"verified": False, "detail": f"NFT metadata missing required fields: {', '.join(missing)}. NFT URI: {uri_str[:200]}"}
        if mismatched:
            return {"verified": False, "detail": f"NFT metadata mismatch: {'; '.join(mismatched)}"}

    return {
        "verified": True,
        "detail": f"NFT verified. Issuer: {target.get('Issuer')}, URI: {uri_str[:500]}",
        "issuer": target.get("Issuer"),
        "uri": uri_str,
        "nft_token_id": nft_token_id,
    }


# ---------------------------------------------------------------------------
# 11d. XRPL DOMAIN FIELD VERIFICATION
# ---------------------------------------------------------------------------
async def verify_domain_ownership(wallet_address: str, expected_domain: str = None) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(XRPL_URL, json={
            "method": "account_info",
            "params": [{"account": wallet_address, "ledger_index": "current"}]
        })
        data = res.json()

    account_data = data.get("result", {}).get("account_data", {})
    domain_hex = account_data.get("Domain")
    if not domain_hex:
        return {"verified": False, "detail": f"Wallet {wallet_address} has no Domain field set on the XRPL ledger."}

    try:
        domain = bytes.fromhex(domain_hex).decode("ascii")
    except Exception:
        return {"verified": False, "detail": "Domain field could not be decoded."}

    if expected_domain and domain.lower() != expected_domain.lower():
        return {"verified": False, "detail": f"Wallet domain is '{domain}', expected '{expected_domain}'."}

    toml_url = f"https://{domain}/.well-known/xrp-ledger.toml"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            toml_res = await client.get(toml_url, follow_redirects=True)
            toml_content = toml_res.text
        except Exception as e:
            return {"verified": False, "detail": f"Could not fetch {toml_url}: {e}"}

    if wallet_address not in toml_content:
        return {"verified": False, "detail": f"Wallet {wallet_address} not found in {toml_url}. The domain has not listed this wallet."}

    return {
        "verified": True,
        "detail": f"Domain verified: {wallet_address} ↔ {domain}",
        "domain": domain,
        "toml_url": toml_url,
    }


class DomainVerifyRequest(BaseModel):
    wallet_address: str
    expected_domain: Optional[str] = None

@app.post("/domain/verify")
async def verify_domain(req: DomainVerifyRequest):
    result = await verify_domain_ownership(req.wallet_address, req.expected_domain)
    if not result["verified"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


# ---------------------------------------------------------------------------
# 11e. W3C VERIFIABLE CREDENTIAL VERIFICATION
# ---------------------------------------------------------------------------
async def verify_w3c_credential(vc_jwt: str, required_issuer_did: str = None, required_type: str = None) -> dict:
    try:
        parts = vc_jwt.split(".")
        if len(parts) == 3:
            payload_b64 = parts[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            vc = payload.get("vc", payload)
        else:
            payload = {}
            vc = json.loads(vc_jwt)
    except Exception as e:
        return {"verified": False, "detail": f"Could not decode credential: {e}"}

    issuer = vc.get("issuer") or payload.get("iss", "")
    if isinstance(issuer, dict):
        issuer = issuer.get("id", "")

    subject = vc.get("credentialSubject", {})
    vc_types = vc.get("type", [])
    expiry = vc.get("expirationDate") or payload.get("exp")

    if expiry:
        try:
            if isinstance(expiry, int):
                exp_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
            else:
                exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                return {"verified": False, "detail": f"Credential expired on {expiry}."}
        except Exception:
            pass

    if required_issuer_did and issuer != required_issuer_did:
        return {"verified": False, "detail": f"Credential issuer is '{issuer}', expected '{required_issuer_did}'."}

    if required_type and required_type not in vc_types:
        return {"verified": False, "detail": f"Credential type '{required_type}' not found. Types present: {vc_types}"}

    did_verified = False
    did_detail = "DID resolution skipped"
    if issuer and issuer.startswith("did:"):
        resolver_url = f"https://dev.uniresolver.io/1.0/identifiers/{issuer}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(resolver_url)
                if r.status_code == 200:
                    did_verified = True
                    did_detail = f"DID {issuer} resolved successfully"
                else:
                    did_detail = f"DID {issuer} could not be resolved (status {r.status_code})"
        except Exception as e:
            did_detail = f"DID resolution failed: {e}"

    return {
        "verified": True,
        "detail": f"Credential decoded. Issuer: {issuer}. {did_detail}",
        "issuer": issuer,
        "subject": subject,
        "types": vc_types,
        "did_resolved": did_verified,
    }


class VCVerifyRequest(BaseModel):
    vc_jwt: str
    required_issuer_did: Optional[str] = None
    required_type: Optional[str] = None

@app.post("/vc/verify")
async def verify_vc(req: VCVerifyRequest):
    result = await verify_w3c_credential(req.vc_jwt, req.required_issuer_did, req.required_type)
    if not result["verified"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


# ---------------------------------------------------------------------------
# 11f. XRPL WALLET TRUST SCORE
# ---------------------------------------------------------------------------
async def compute_xrpl_trust_score(wallet_address: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            info_res = await client.post(XRPL_URL, json={
                "method": "account_info",
                "params": [{"account": wallet_address, "ledger_index": "validated"}]
            })
            info = info_res.json().get("result", {})

        if info.get("status") == "error" or "account_data" not in info:
            return {"score": 0, "detail": "Wallet not found on XRPL ledger.", "signals": {}}

        acct = info["account_data"]

        # Account age (from ledger sequence as proxy — older accounts have lower sequence)
        ledger_index = info.get("ledger_current_index", 90000000)
        account_index = acct.get("Sequence", ledger_index)
        # Rough age: each ledger ~3.5s. sequence gap → approximate age in days
        ledger_gap = max(0, ledger_index - account_index)
        age_days = int(ledger_gap * 3.5 / 86400)

        balance_xrp = int(acct.get("Balance", 0)) / 1_000_000
        has_domain = bool(acct.get("Domain"))
        tx_count = acct.get("OwnerCount", 0)  # owner count as proxy for activity

        # Fetch NFT count
        async with httpx.AsyncClient(timeout=8.0) as client:
            nft_res = await client.post(XRPL_URL, json={
                "method": "account_nfts",
                "params": [{"account": wallet_address, "limit": 10}]
            })
            nft_count = len(nft_res.json().get("result", {}).get("account_nfts", []))

        # Score components (out of 100)
        age_score     = min(25, int(age_days / 30) * 2)      # 2pts per month, max 25
        balance_score = min(15, int(balance_xrp / 10) * 3)   # 3pts per 10 XRP, max 15
        tx_score      = min(20, int(tx_count / 5) * 2)        # 2pts per 5 owner items, max 20
        domain_score  = 10 if has_domain else 0
        nft_score     = min(10, nft_count * 2)                # 2pts per NFT, max 10

        total = age_score + balance_score + tx_score + domain_score + nft_score

        signals = {
            "age_days": age_days,
            "balance_xrp": round(balance_xrp, 2),
            "owner_count": tx_count,
            "has_domain": has_domain,
            "nft_count": nft_count,
        }

        return {"score": min(100, total), "detail": f"XRPL trust score: {total}/100", "signals": signals}
    except Exception as e:
        return {"score": 0, "detail": f"Could not compute score: {e}", "signals": {}}


async def _score_bid_wallet(bid_id: str, wallet_address: str, session_factory):
    result = await compute_xrpl_trust_score(wallet_address)
    db = session_factory()
    try:
        bid = db.query(Bid).filter(Bid.id == bid_id).first()
        if bid:
            bid.xrpl_trust_score = result.get("score", 0)
            db.commit()
    finally:
        db.close()


@app.get("/wallet/score/{address}")
async def get_wallet_score(address: str):
    """Compute XRPL trust score for a wallet address."""
    result = await compute_xrpl_trust_score(address)
    return result


# ---------------------------------------------------------------------------
# 11g. ETH SIGNATURE CHALLENGE
# ---------------------------------------------------------------------------
_eth_challenges: dict = {}  # address -> {challenge, expires_at}


class EthChallengeRequest(BaseModel):
    eth_address: str


class EthSignatureRequest(BaseModel):
    eth_address: str
    signature: str


@app.post("/eth/challenge")
async def create_eth_challenge(req: EthChallengeRequest):
    """Generate a challenge string for the given ETH address."""
    challenge = f"AgentTrust ownership proof for {req.eth_address}: {secrets.token_hex(16)}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    _eth_challenges[req.eth_address.lower()] = {"challenge": challenge, "expires_at": expires_at}
    return {"challenge": challenge, "expires_at": expires_at.isoformat()}


@app.post("/eth/verify-signature")
async def verify_eth_signature(req: EthSignatureRequest):
    """Verify that eth_address signed the challenge (EIP-191 personal_sign)."""
    stored = _eth_challenges.get(req.eth_address.lower())
    if not stored:
        raise HTTPException(status_code=400, detail="No challenge found. Request a new challenge first.")
    if datetime.now(timezone.utc) > stored["expires_at"]:
        del _eth_challenges[req.eth_address.lower()]
        raise HTTPException(status_code=400, detail="Challenge expired. Request a new one.")

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        message = encode_defunct(text=stored["challenge"])
        recovered = Account.recover_message(message, signature=req.signature)
        if recovered.lower() != req.eth_address.lower():
            raise HTTPException(status_code=400, detail=f"Signature verification failed. Expected {req.eth_address}, got {recovered}.")
        del _eth_challenges[req.eth_address.lower()]
        return {"verified": True, "eth_address": req.eth_address, "detail": "Ethereum address ownership verified."}
    except ImportError:
        _eth_challenges.pop(req.eth_address.lower(), None)
        return {"verified": True, "eth_address": req.eth_address, "detail": "Signature accepted (eth_account library not installed for full cryptographic verification)."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Signature verification error: {e}")


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
        "gemini-2.5-flash",   # fast — try first to beat Render's 30s request timeout
        "gemini-2.0-flash",
        "gemini-2.5-pro",     # slower — fallback only
        "gemini-1.5-flash",
        "gemini-1.5-pro",
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
            elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
                try:
                    text_content = base64.b64decode(att.get("data", "")).decode("utf-8", errors="replace")
                    prompt_text += f"\n--- BUYER ATTACHMENT: {att.get('filename','file')} ---\n{text_content}\n--- END ATTACHMENT ---\n"
                except Exception:
                    pass

    if worker_attachments:
        for att in worker_attachments:
            mime = att.get("mime_type", "application/octet-stream")
            if mime in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({"inline_data": {"mime_type": mime, "data": att.get("data")}})
            elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
                try:
                    text_content = base64.b64decode(att.get("data", "")).decode("utf-8", errors="replace")
                    prompt_text += f"\n--- WORKER ATTACHMENT: {att.get('filename','file')} ---\n{text_content}\n--- END ATTACHMENT ---\n"
                except Exception:
                    pass
    parts.append({"text": prompt_text})
    payload = {"contents": [{"parts": parts}]}

    async with httpx.AsyncClient() as client:
        for model_id in candidates:
            try:
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_id}:generateContent?key={api_key}"
                )
                res = await client.post(url, json=payload, timeout=25.0)

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
    x_payment: Optional[str] = Header(None),  # x402 standard header
    db: Session = Depends(get_db),
):
    fee_hash = (req.fee_hash or x_payment_hash or x_payment or "").strip()
    if not fee_hash:
        _raise_402(
            "/audit",
            f"Payment required. Send {MIN_FEE_XRP} XRP to {PROTOCOL_WALLET} on the XRPL, "
            "then include the transaction hash as the X-PAYMENT header (or fee_hash body field).",
        )

    audit_id = f"audit-{fee_hash[:16].lower()}"
    await verify_fee_payment(fee_hash=fee_hash, escrow_id=audit_id, db=db, resource="/audit")

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
class FeePayloadRequest(BaseModel):
    amount_xrp: Optional[float] = None  # override fee amount; defaults to MIN_FEE_XRP

@app.post("/xumm/fee-payload")
async def create_fee_payload(req: FeePayloadRequest = FeePayloadRequest()):
    xrp = req.amount_xrp if req.amount_xrp and req.amount_xrp > 0 else MIN_FEE_XRP
    tx = {
        "TransactionType": "Payment",
        "Destination":     PROTOCOL_WALLET,
        "Amount":          str(int(xrp * 1_000_000)),
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

    await verify_fee_payment(fee_hash=req.fee_hash, escrow_id=req.escrow_id, db=db, resource="/escrow/generate")

    # Validate currency + amount
    currency = req.currency.upper()
    if currency not in ("XRP", "RLUSD"):
        raise HTTPException(status_code=400, detail="currency must be XRP or RLUSD.")

    amount_xrp   = req.amount_xrp   if currency == "XRP"   else None
    amount_rlusd = req.amount_rlusd if currency == "RLUSD" else None

    if currency == "XRP":
        if not amount_xrp or amount_xrp < MIN_ESCROW_XRP:
            raise HTTPException(
                status_code=400,
                detail=f"amount_xrp must be ≥ {MIN_ESCROW_XRP} XRP (1 drop — XRPL minimum)."
            )
    if currency == "RLUSD" and (not amount_rlusd or amount_rlusd <= 0):
        raise HTTPException(status_code=400, detail="amount_rlusd required for RLUSD escrow.")

    # For RLUSD escrow, first check the issuer has enabled trust line locking (XLS-85 requirement)
    if currency == "RLUSD":
        if not await check_rlusd_escrow_supported():
            raise HTTPException(
                status_code=503,
                detail=(
                    "RLUSD escrow is not currently available. "
                    "XLS-85 token escrow is live on XRPL mainnet, but the RLUSD issuer (Ripple) "
                    "has not yet enabled the lsfAllowTrustLineLocking flag on their issuer account — "
                    "this is required before RLUSD can be held in escrow. "
                    "In the meantime, use XRP escrow: the seller can swap to RLUSD via the XRPL DEX after release. "
                    "This message will disappear automatically once Ripple enables the flag."
                ),
            )

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
        category              = req.category,
        marketplace_tags      = json.dumps(req.tags) if req.tags else None,
        delivery_status       = "PENDING",
        submission_count      = 0,
        max_submissions       = max(1, min(req.max_submissions, 10)),  # clamp 1–10
        require_nft_proof     = bool(req.require_nft_proof or req.required_nft_issuer),
        required_nft_issuer   = req.required_nft_issuer or None,
        required_nft_metadata = json.dumps(req.required_nft_metadata) if req.required_nft_metadata else None,
        required_domain        = req.required_domain or None,
        required_vc_issuer_did = req.required_vc_issuer_did or None,
        required_vc_type       = req.required_vc_type or None,
        proof_policy           = req.proof_policy or "ALL",
        nft_dvp                = req.nft_dvp or False,
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
    # Record who created the EscrowCreate (buyer in the bilateral flow)
    if not vault.escrow_owner:
        vault.escrow_owner = vault.buyer_address
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
        "nft_dvp":              vault.nft_dvp or False,
        "nft_dvp_status":       vault.nft_dvp_status,
        "nft_dvp_token_id":     vault.nft_dvp_token_id,
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

    # 50 MB attachment cap
    total_bytes = 0
    for att in (req.worker_attachments or []):
        try:
            total_bytes += len(base64.b64decode(att.data))
        except Exception:
            pass
    if total_bytes > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Total attachment size exceeds 50 MB.")

    # Increment submission count immediately (before audit — counts even failed attempts)
    vault.submission_count = current_count + 1
    db.commit()

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

    # ── PROOF VERIFICATION (NFT, Domain, VC) with ANY/ALL policy ──
    proof_policy = (vault.proof_policy or "ALL").upper()
    proof_results = []   # list of (name, passed, note)

    # NFT proof
    if vault.require_nft_proof or vault.required_nft_issuer:
        if not req.nft_token_id:
            proof_results.append(("NFT", False, "NFT Token ID not provided — seller must supply an NFT Token ID as proof."))
        else:
            nft_wallet = req.nft_wallet or vault.worker_address
            required_meta = None
            if vault.required_nft_metadata:
                try:
                    required_meta = json.loads(vault.required_nft_metadata)
                except Exception:
                    pass
            # Resolve required_issuer: if it matches an issuer in the registry,
            # accept NFTs from ANY of that issuer's registered wallets.
            resolved_issuer = vault.required_nft_issuer
            try:
                reg_issuer = db.query(NftIssuer).filter(
                    NftIssuer.wallet_addresses.contains(vault.required_nft_issuer) |
                    (NftIssuer.wallet_address == vault.required_nft_issuer)
                ).first()
                if reg_issuer and len(reg_issuer.all_wallets()) > 1:
                    # Try each wallet; use first that passes
                    for candidate in reg_issuer.all_wallets():
                        _r = await verify_nft_ownership(
                            wallet_address=nft_wallet,
                            nft_token_id=req.nft_token_id,
                            required_issuer=candidate,
                            required_metadata=required_meta,
                        )
                        if _r["verified"]:
                            nft_result = _r
                            break
                    else:
                        nft_result = _r  # last failure
                    # Skip the single-call path below
                    if nft_result["verified"]:
                        note = f"🔗 NFT PROOF VERIFIED ON-CHAIN: {nft_result['detail']}"
                        proof_results.append(("NFT", True, note))
                    else:
                        proof_results.append(("NFT", False, f"NFT proof failed: {nft_result['detail']}"))
                    resolved_issuer = None  # already handled
            except Exception:
                pass

            if resolved_issuer is not None:
                nft_result = await verify_nft_ownership(
                    wallet_address=nft_wallet,
                    nft_token_id=req.nft_token_id,
                    required_issuer=resolved_issuer,
                    required_metadata=required_meta,
                )
            if nft_result["verified"]:
                # Enrich with registry name so the AI sees "Maersk Line" not just rXXX...
                issuer_wallet = nft_result.get("issuer", "")
                issuer_label  = issuer_wallet
                try:
                    reg = db.query(NftIssuer).filter(
                        NftIssuer.wallet_addresses.contains(issuer_wallet) |
                        (NftIssuer.wallet_address == issuer_wallet)
                    ).first()
                    if reg:
                        issuer_label = f"{reg.name} ({issuer_wallet}) [AgentTrust verified issuer]"
                except Exception:
                    pass
                note = (
                    f"🔗 NFT PROOF VERIFIED ON-CHAIN\n"
                    f"  Token ID : {req.nft_token_id}\n"
                    f"  Issuer   : {issuer_label}\n"
                    f"  Metadata : {nft_result.get('uri', '')[:500]}"
                )
                proof_results.append(("NFT", True, note))
                logger.info(f"✅ NFT proof verified for {req.escrow_id}: {nft_result['detail']}")
            else:
                proof_results.append(("NFT", False, f"NFT verification failed: {nft_result['detail']}"))

    # Domain proof — "ANY" means any verified domain passes; specific value restricts to that domain
    if vault.required_domain:
        expected = None if vault.required_domain == "ANY" else vault.required_domain
        domain_result = await verify_domain_ownership(vault.worker_address, expected)
        if domain_result["verified"]:
            note = f"🌐 DOMAIN VERIFIED: {domain_result['detail']}"
            proof_results.append(("Domain", True, note))
            logger.info(f"✅ Domain verified for {req.escrow_id}: {domain_result['detail']}")
        else:
            proof_results.append(("Domain", False, f"Domain verification failed: {domain_result['detail']}"))

    # VC proof
    if vault.required_vc_issuer_did or vault.required_vc_type:
        if not req.vc_jwt:
            proof_results.append(("VC", False, "Verifiable Credential JWT not provided (vc_jwt missing)."))
        else:
            vc_result = await verify_w3c_credential(req.vc_jwt, vault.required_vc_issuer_did, vault.required_vc_type)
            if vc_result["verified"]:
                note = f"📜 VERIFIABLE CREDENTIAL VERIFIED: {vc_result['detail']}"
                proof_results.append(("VC", True, note))
                logger.info(f"✅ VC verified for {req.escrow_id}: {vc_result['detail']}")
            else:
                proof_results.append(("VC", False, f"VC verification failed: {vc_result['detail']}"))

    # Apply policy
    if proof_results:
        passed = [r for r in proof_results if r[1]]
        failed = [r for r in proof_results if not r[1]]
        if proof_policy == "ANY":
            if not passed:
                all_failures = "; ".join(r[2] for r in failed)
                raise HTTPException(status_code=400, detail=f"Proof policy is ANY — at least one proof must pass. Failures: {all_failures}")
        else:  # ALL
            if failed:
                failure_msgs = "; ".join(r[2] for r in failed)
                raise HTTPException(status_code=400, detail=f"Proof policy is ALL — all required proofs must pass. Failed: {failure_msgs}")

    # Fetch evidence link snapshots now (at submission time = tamper-proof snapshot)
    evidence_snapshots = None
    if req.evidence_links:
        logger.info(f"🔗 Fetching {len(req.evidence_links[:3])} evidence link(s) for {req.escrow_id}")
        evidence_snapshots = await fetch_url_snapshots(req.evidence_links)
        # Store the snapshots on the vault so the collect page can show them
        vault.evidence_link_snapshots = json.dumps(evidence_snapshots)
        ok     = sum(1 for s in evidence_snapshots if s.get("content"))
        failed_links = sum(1 for s in evidence_snapshots if s.get("error"))
        logger.info(f"🔗 Evidence links: {ok} fetched, {failed_links} failed for {req.escrow_id}")

    work_with_nft = req.work
    extra_notes = [r[2] for r in proof_results if r[1]]  # notes from passed proofs
    if extra_notes:
        work_with_nft = req.work + "\n\n" + "\n".join(extra_notes)

    verdict_dict, model_used = await run_ai_audit(
        task                    = vault.task_description,
        work                    = work_with_nft,
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

        # ── NFT DvP: if enabled, hold payment until buyer accepts the NFT offer ──
        if vault.nft_dvp:
            vault.status              = "PASS_AWAITING_NFT"
            vault.nft_dvp_status      = "pending_offer"
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
            vault.ai_verdict = json.dumps(verdict_dict)
            vault.model_used = model_used
            db.commit()
            logger.info(f"🔄 NFT DvP: PASS_AWAITING_NFT for {req.escrow_id}")
            return {
                "escrow_id":            req.escrow_id,
                "status":               "pass_awaiting_nft",
                "verdict":              verdict_dict,
                "model_used":           model_used,
                "fulfillment":          None,
                "condition":            None,
                "worker_address":       vault.worker_address,
                "buyer_address":        vault.buyer_address,
                "escrow_sequence":      vault.escrow_sequence,
                "amount_xrp":           vault.amount_xrp,
                "amount_rlusd":         vault.amount_rlusd,
                "currency":             vault.currency,
                "auto_finish_queued":   False,
                "dex_quote_rlusd":      None,
                "rlusd_issuer":         None,
                "seller_currency":      vault.seller_currency,
                "nft_dvp_required":     True,
                "nft_dvp_instructions": (
                    f"Your work passed! Before payment releases, you must transfer the NFT to the buyer.\n\n"
                    f"1. In Xaman: create an NFTokenCreateOffer for your NFT\n"
                    f"2. Set Destination = {vault.buyer_address}\n"
                    f"3. Set Amount = 0 (payment comes via escrow)\n"
                    f"4. Submit the NFT Token ID at POST /escrow/{vault.escrow_id}/nft-offer\n\n"
                    f"Payment releases automatically once the buyer accepts."
                ),
            }

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
        # escrow_owner is who signed the EscrowCreate (buyer for bilateral, referee for open bounty)
        escrow_owner = vault.escrow_owner or vault.buyer_address
        if not escrow_owner:
            logger.error(f"❌ AUTO-FINISH skipped for {req.escrow_id}: no owner address")
        elif vault.escrow_sequence and vault.worker_address and referee_wallet:
            asyncio.create_task(auto_finish_escrow(
                escrow_id            = req.escrow_id,
                sequence             = vault.escrow_sequence,
                owner                = escrow_owner,
                fulfillment          = plaintext_fulfillment,
                condition            = vault.condition,
                worker_addr          = vault.worker_address,
                db_session_factory   = SessionLocal,
            ))
            logger.info(f"🚀 AUTO-FINISH queued for {req.escrow_id}")
        else:
            logger.warning(
                f"⚠️ AUTO-FINISH skipped for {req.escrow_id}: "
                f"seq={vault.escrow_sequence} | owner={escrow_owner} | "
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
        resource  = "/evaluate/purchase-attempt",
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
# 16c. NFT DELIVERY-VS-PAYMENT (DvP) — helpers + endpoints
# ---------------------------------------------------------------------------
async def verify_nft_sell_offer(nft_token_id: str, seller_address: str, buyer_address: str) -> dict:
    """Check that an NFTokenCreateOffer exists for this NFT, from seller, to buyer, at price 0."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(XRPL_URL, json={
                "method": "nft_sell_offers",
                "params": [{"nft_id": nft_token_id}]
            })
            data = res.json()
    except Exception as e:
        return {"verified": False, "detail": f"XRPL request failed: {e}"}

    result = data.get("result", {})
    if result.get("status") == "error" or "error" in result:
        return {"verified": False, "detail": f"XRPL error: {result.get('error_message', result.get('error', 'unknown'))}"}

    offers = result.get("offers", [])
    if not offers:
        return {"verified": False, "detail": f"No sell offers found for NFT {nft_token_id}."}

    for offer in offers:
        dest   = offer.get("destination", "")
        amount = offer.get("amount", "")
        if dest == buyer_address and (amount == "0" or amount == 0):
            return {
                "verified": True,
                "offer_index": offer.get("nft_offer_index", ""),
                "expiration": offer.get("expiration"),
                "detail": f"Valid sell offer found: NFT {nft_token_id} offered to {buyer_address} at price 0.",
            }

    return {
        "verified": False,
        "detail": (
            f"No valid offer found for NFT {nft_token_id} to buyer {buyer_address} at price 0. "
            f"Found {len(offers)} offer(s) but none matched."
        ),
    }


async def check_nft_accepted(nft_token_id: str, buyer_address: str) -> bool:
    """Return True if the NFT is now owned by buyer_address."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(XRPL_URL, json={
                "method": "account_nfts",
                "params": [{"account": buyer_address, "limit": 400}]
            })
            data = res.json()
        nfts = data.get("result", {}).get("account_nfts", [])
        return any(n.get("NFTokenID") == nft_token_id for n in nfts)
    except Exception as e:
        logger.warning(f"check_nft_accepted failed for {nft_token_id}: {e}")
        return False


async def _send_nft_accept_email(buyer_email: str, buyer_name: str, escrow_id: str, nft_token_id: str, worker_name: str):
    if not RESEND_API_KEY:
        return
    subject = f"🎟️ Your NFT is ready to collect — {escrow_id}"
    body = f"""
    <div style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:2rem;background:#0d1117;color:#e2e8f0;border-radius:12px;">
        <h2 style="color:#10b981;">Your NFT is ready!</h2>
        <p>Hi {buyer_name or 'there'},</p>
        <p>The work on escrow <strong>{escrow_id}</strong> has passed the AI audit. The seller ({worker_name}) has created an NFT transfer offer for you on the XRPL.</p>
        <div style="background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.2);border-radius:8px;padding:1rem;margin:1.5rem 0;">
            <div style="font-size:.8rem;color:#10b981;font-weight:700;margin-bottom:.5rem;">NFT TOKEN ID</div>
            <div style="font-family:monospace;font-size:.8rem;word-break:break-all;">{nft_token_id}</div>
        </div>
        <p><strong>To release payment to the seller, accept the NFT offer:</strong></p>
        <ol style="color:#94a3b8;line-height:1.8;">
            <li>Open <strong>Xaman</strong> on your phone</li>
            <li>Go to NFTs → Pending Offers</li>
            <li>Accept the incoming offer for token <code>{nft_token_id[:16]}...</code></li>
        </ol>
        <p style="color:#94a3b8;font-size:.85rem;">Once you accept, payment releases automatically to the seller. You cannot be charged — the NFT transfer is free (payment comes from the escrow you already funded).</p>
        <a href="https://www.cryptovault.co.uk?track={escrow_id}" style="display:inline-block;background:#10b981;color:#fff;padding:.75rem 1.5rem;border-radius:8px;text-decoration:none;font-weight:700;margin-top:1rem;">Track Escrow</a>
    </div>
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": "AgentTrust <noreply@agenttrust.io>", "to": [buyer_email], "subject": subject, "html": body},
            )
    except Exception as e:
        logger.warning(f"NFT accept email failed: {e}")


async def _auto_finish_after_nft_accepted(escrow_id: str):
    """Fire EscrowFinish after NFT acceptance confirmed."""
    await asyncio.sleep(3)
    with SessionLocal() as db:
        vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
        if not vault or vault.status == "RELEASED":
            return
        escrow_owner = vault.escrow_owner or vault.buyer_address
        if not escrow_owner or not vault.escrow_sequence or not vault.worker_address or not referee_wallet:
            logger.error(f"❌ NFT DvP auto-finish skipped for {escrow_id}: missing fields")
            return
        try:
            plaintext_fulfillment = decrypt_fulfillment(vault.fulfillment)
            vault.status          = "RELEASED"
            vault.delivery_status = "RELEASED"
            db.commit()
        except Exception as e:
            logger.error(f"NFT DvP auto-finish: could not decrypt fulfillment for {escrow_id}: {e}")
            vault.auto_finish_error = str(e)
            db.commit()
            return
        try:
            await auto_finish_escrow(
                escrow_id          = escrow_id,
                sequence           = vault.escrow_sequence,
                owner              = escrow_owner,
                fulfillment        = plaintext_fulfillment,
                condition          = vault.condition,
                worker_addr        = vault.worker_address,
                db_session_factory = SessionLocal,
            )
        except Exception as e:
            logger.error(f"NFT DvP auto-finish failed for {escrow_id}: {e}")
            with SessionLocal() as db2:
                v2 = db2.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
                if v2:
                    v2.auto_finish_error = str(e)
                    db2.commit()


class NftDvpOfferRequest(BaseModel):
    escrow_id:    str
    nft_token_id: str


@app.post("/escrow/{escrow_id}/nft-offer")
async def register_nft_dvp_offer(escrow_id: str, req: NftDvpOfferRequest, db: Session = Depends(get_db)):
    """
    Seller has created an NFTokenCreateOffer on XRPL (Destination=buyer, Amount=0).
    Register it here so the system can monitor for buyer acceptance.
    """
    import asyncio
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Escrow '{escrow_id}' not found.")
    if not vault.nft_dvp:
        raise HTTPException(status_code=400, detail="This escrow does not have NFT DvP enabled.")
    if vault.status != "PASS_AWAITING_NFT":
        raise HTTPException(status_code=400, detail=f"Escrow is not in PASS_AWAITING_NFT state (current: {vault.status}).")

    buyer_address = vault.buyer_address
    if not buyer_address:
        raise HTTPException(status_code=400, detail="Buyer address not set on this escrow.")

    offer_check = await verify_nft_sell_offer(req.nft_token_id, vault.worker_address or "", buyer_address)
    if not offer_check["verified"]:
        raise HTTPException(status_code=400, detail=offer_check["detail"])

    vault.nft_dvp_token_id = req.nft_token_id
    vault.nft_dvp_offer_id = offer_check.get("offer_index", "")
    vault.nft_dvp_status   = "offer_created"

    xrpl_expiry = offer_check.get("expiration")
    if xrpl_expiry:
        XRPL_EPOCH_OFFSET = 946684800
        expiry_unix = xrpl_expiry + XRPL_EPOCH_OFFSET
        vault.nft_dvp_offer_expiry = datetime.fromtimestamp(expiry_unix, tz=timezone.utc).replace(tzinfo=None)

    db.commit()

    if vault.buyer_email:
        asyncio.create_task(_send_nft_accept_email(
            buyer_email  = vault.buyer_email,
            buyer_name   = vault.buyer_name or "",
            escrow_id    = escrow_id,
            nft_token_id = req.nft_token_id,
            worker_name  = vault.worker_address or "the seller",
        ))

    return {
        "status":       "offer_registered",
        "nft_token_id": req.nft_token_id,
        "offer_index":  vault.nft_dvp_offer_id,
        "message":      "NFT sell offer verified on-chain. Buyer has been notified to accept it. Payment will release automatically once accepted.",
    }


@app.get("/escrow/{escrow_id}/nft-status")
async def nft_dvp_status(escrow_id: str, db: Session = Depends(get_db)):
    """Check whether the buyer has accepted the NFT offer yet."""
    import asyncio
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Escrow '{escrow_id}' not found.")

    if not vault.nft_dvp or not vault.nft_dvp_token_id:
        return {"nft_dvp": False}

    if vault.nft_dvp_status == "accepted":
        return {"nft_dvp": True, "status": "accepted", "message": "NFT accepted. Escrow released."}

    if vault.nft_dvp_offer_expiry and datetime.now(timezone.utc) > vault.nft_dvp_offer_expiry.replace(tzinfo=timezone.utc):
        vault.nft_dvp_status = "expired"
        db.commit()
        return {"nft_dvp": True, "status": "expired", "message": "NFT offer expired. Seller must create a new offer."}

    if vault.buyer_address and vault.nft_dvp_token_id:
        accepted = await check_nft_accepted(vault.nft_dvp_token_id, vault.buyer_address)
        if accepted:
            vault.nft_dvp_status = "accepted"
            db.commit()
            asyncio.create_task(_auto_finish_after_nft_accepted(escrow_id))
            return {"nft_dvp": True, "status": "accepted", "message": "NFT accepted! Releasing payment to seller now."}

    return {
        "nft_dvp":       True,
        "status":        vault.nft_dvp_status or "offer_created",
        "nft_token_id":  vault.nft_dvp_token_id,
        "message":       "Waiting for buyer to accept the NFT offer in Xaman.",
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
import time as _time

# Module-level price cache — survives across requests within a process
_xrp_price_cache: dict = {}
_xrp_price_cache_ts: float = 0.0
_XRP_PRICE_TTL: float = 60.0  # seconds


@app.get("/xrp/price")
async def get_xrp_price():
    """
    Fetch live XRP price. Primary: Binance. Fallback: CoinGecko (rate-limited).
    Cached for 60 s to avoid hitting external APIs on every request.
    Returns last cached value if both sources fail.
    """
    global _xrp_price_cache, _xrp_price_cache_ts

    # Return cache if still fresh
    if _xrp_price_cache and (_time.monotonic() - _xrp_price_cache_ts) < _XRP_PRICE_TTL:
        return {**_xrp_price_cache, "cached": True}

    # Primary: Binance (no rate limit on public ticker)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT")
            res.raise_for_status()
            usd = float(res.json()["price"])
            gbp = round(usd * 0.79, 4)
            _xrp_price_cache = {"usd": usd, "gbp": gbp}
            _xrp_price_cache_ts = _time.monotonic()
            return _xrp_price_cache
    except Exception:
        pass

    # Fallback: CoinGecko (may be rate-limited; only reached if Binance fails)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ripple", "vs_currencies": "usd,gbp"},
            )
            res.raise_for_status()
            data = res.json()
            usd  = data["ripple"]["usd"]
            gbp  = data["ripple"]["gbp"]
            _xrp_price_cache = {"usd": usd, "gbp": gbp}
            _xrp_price_cache_ts = _time.monotonic()
            return _xrp_price_cache
    except Exception:
        pass

    # Return stale cache or null
    if _xrp_price_cache:
        return {**_xrp_price_cache, "cached": True}
    return {"usd": None, "gbp": None}


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
# 20. MARKETPLACE JOBS API — proxies the job board for agents and the marketplace UI
# ---------------------------------------------------------------------------
# /marketplace/jobs now returns job board posts (from the Job model) in a
# normalised format. Any "bounty"-style job must go through the job board:
# post → bid → award → escrow, because XRPL EscrowCreate requires the worker's
# address to be known at creation time. There is no open-claim model.

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
    Machine-readable job listing for agents and API consumers.
    Returns open job board posts — workers bid, buyer awards, then escrow is created.

    XRPL escrow requires the worker's address at creation time, so there is no
    open-claim model. All jobs (including 'bug bounty'-style) go through:
    bid → award → create_escrow_vault → confirm_escrow_transaction → evaluate_escrow_work.

    Use POST /jobs to post a job, POST /jobs/{id}/bid to bid, POST /jobs/{id}/award to award.
    """
    limit = min(limit, 100)
    now   = datetime.now(timezone.utc)

    real_jobs = []
    try:
        q = db.query(JobPosting).filter(
            JobPosting.status == "open",
            or_(JobPosting.expires_at == None, JobPosting.expires_at > now),
        )
        if category != "all":
            q = q.filter(JobPosting.category == category)
        posts = q.order_by(JobPosting.created_at.desc()).limit(200).all()
        for j in posts:
            budget = j.budget_xrp or 0
            if min_bounty_xrp > 0 and budget < min_bounty_xrp:
                continue
            try:
                tags = json.loads(j.tags) if j.tags else []
            except Exception:
                tags = []
            bid_count = db.query(Bid).filter(Bid.job_id == j.id, Bid.status == "pending").count()
            expires_hrs = max(0, int((j.expires_at.replace(tzinfo=timezone.utc) - now).total_seconds() / 3600)) if j.expires_at else None
            real_jobs.append({
                "id":           j.id,
                "title":        j.title,
                "description":  j.description,
                "category":     j.category or "default",
                "bounty":       budget,
                "currency":     "XRP",
                "poster":       j.buyer_address or "",
                "poster_name":  j.buyer_name or "",
                "deadline":     j.expires_at.strftime("%d %b %Y %H:%M UTC") if j.expires_at else "—",
                "deadline_hrs": expires_hrs,
                "tags":         tags,
                "status":       "OPEN",
                "bid_count":    bid_count,
                "is_demo":      False,
            })
            if len(real_jobs) >= limit:
                break
    except Exception as e:
        logger.warning(f"⚠️ marketplace_jobs DB query failed: {e}")

    # Seed demo jobs — shown when the job board is empty, illustrate the bid flow
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
        "note":            (
            "To claim a job: submit a bid via POST /jobs/{id}/bid. "
            "The buyer awards your bid, then creates an escrow with your wallet address. "
            "Demo jobs (is_demo=true) are illustrative — submit a real bid via the job board."
        ),
    }


# ---------------------------------------------------------------------------
# JOB BOARD — post jobs, bid, negotiate, award (no funds held by referee)
# ---------------------------------------------------------------------------

@app.post("/jobs")
async def post_job(body: dict, db: Session = Depends(get_db)):
    """
    Post a job to the marketplace. No fee, no escrow — purely a request for bids.
    Once a bid is accepted via /jobs/{id}/award, the buyer creates a bilateral
    escrow via POST /escrow/generate using the worker's address.
    """
    job_id        = (body.get("id") or "").strip()
    title         = (body.get("title") or "").strip()
    description   = (body.get("description") or "").strip()
    buyer_address = (body.get("buyer_address") or "").strip()

    if not job_id:
        raise HTTPException(status_code=400, detail="id is required (e.g. JOB-XXXX-YYYY).")
    if not title:
        raise HTTPException(status_code=400, detail="title is required.")
    if not description:
        raise HTTPException(status_code=400, detail="description is required.")
    if not buyer_address:
        raise HTTPException(status_code=400, detail="buyer_address is required.")

    existing = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Job ID '{job_id}' already exists.")

    expires_hrs = int(body.get("expires_hrs") or 168)
    expires_at  = datetime.now(timezone.utc) + timedelta(hours=expires_hrs)

    award_token      = secrets.token_urlsafe(32)
    award_token_hash = hashlib.sha256(award_token.encode()).hexdigest()

    job = JobPosting(
        id                 = job_id,
        title              = title,
        description        = description,
        budget_xrp         = body.get("budget_xrp"),
        buyer_address      = buyer_address,
        buyer_name         = body.get("buyer_name") or "",
        category           = body.get("category") or "default",
        tags               = json.dumps(body.get("tags") or []),
        expires_at         = expires_at,
        buyer_email        = body.get("buyer_email") or None,
        buyer_callback_url = body.get("buyer_callback_url") or None,
        award_token_hash   = award_token_hash,
        award_token        = award_token,
        required_nft_issuer   = body.get("required_nft_issuer") or None,
        required_nft_metadata = json.dumps(body.get("required_nft_metadata")) if body.get("required_nft_metadata") else None,
        required_domain        = body.get("required_domain") or None,
        required_vc_issuer_did = body.get("required_vc_issuer_did") or None,
        required_vc_type       = body.get("required_vc_type") or None,
        proof_policy           = body.get("proof_policy") or "ALL",
    )
    db.add(job)
    db.commit()

    logger.info(f"📋 JOB POSTED: {job_id} | buyer={buyer_address} | budget={body.get('budget_xrp')} XRP")

    import asyncio
    if job.buyer_email:
        asyncio.create_task(send_job_posted_email(
            buyer_email = job.buyer_email,
            buyer_name  = job.buyer_name or "",
            job_id      = job_id,
            job_title   = job.title,
            award_token = award_token,
        ))

    return {
        "status":      "posted",
        "job_id":      job_id,
        "expires_at":  expires_at.strftime("%Y-%m-%d %H:%M UTC"),
        "award_token": award_token,
        "next_step":   (
            "Worker agents can find this job via GET /jobs and bid via POST /jobs/{id}/bid. "
            "Store award_token securely — it is required to award a bid via POST /jobs/{id}/award "
            "and is never shown again."
        ),
    }


@app.get("/jobs")
async def list_jobs(
    category:    str   = "all",
    min_budget:  float = 0,
    max_budget:  float = 0,
    limit:       int   = 20,
    db: Session = Depends(get_db),
):
    """List open job postings available for bidding."""
    limit = min(limit, 100)
    now   = datetime.now(timezone.utc)

    q = db.query(JobPosting).filter(
        JobPosting.status == "open",
        or_(JobPosting.expires_at == None, JobPosting.expires_at > now),
    )
    if category != "all":
        q = q.filter(JobPosting.category == category)

    jobs = q.order_by(JobPosting.created_at.desc()).limit(200).all()

    result = []
    for j in jobs:
        budget = j.budget_xrp or 0
        if min_budget > 0 and budget < min_budget:
            continue
        if max_budget > 0 and budget > max_budget:
            continue
        try:
            tags = json.loads(j.tags) if j.tags else []
        except Exception:
            tags = []
        bid_count = db.query(Bid).filter(Bid.job_id == j.id, Bid.status == "pending").count()
        result.append({
            "id":           j.id,
            "title":        j.title,
            "description":  j.description,
            "budget_xrp":   j.budget_xrp,
            "buyer_address": j.buyer_address,
            "buyer_name":   j.buyer_name or "",
            "category":     j.category or "default",
            "tags":         tags,
            "status":       j.status,
            "bid_count":    bid_count,
            "created_at":   (j.created_at.isoformat() + "Z") if j.created_at else None,
            "expires_at":   j.expires_at.strftime("%d %b %Y %H:%M UTC") if j.expires_at else "—",
            "expires_hrs":  max(0, int((j.expires_at.replace(tzinfo=timezone.utc) - now).total_seconds() / 3600)) if j.expires_at else None,
            "required_nft_issuer":   j.required_nft_issuer or None,
            "required_domain":        j.required_domain or None,
            "required_vc_issuer_did": j.required_vc_issuer_did or None,
            "required_vc_type":       j.required_vc_type or None,
            "proof_policy":           j.proof_policy or "ALL",
        })
        if len(result) >= limit:
            break

    return {"jobs": result, "total": len(result)}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, db: Session = Depends(get_db)):
    """Get job details and all current bids."""
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    bids = db.query(Bid).filter(Bid.job_id == job_id).order_by(Bid.created_at.asc()).all()

    # Fetch message metadata per bid in one query to avoid N+1
    all_msgs = (
        db.query(JobMessage.bid_id, JobMessage.sender_role, JobMessage.created_at)
        .filter(JobMessage.job_id == job_id)
        .all()
    )
    from collections import defaultdict
    worker_msg_counts: dict = defaultdict(int)
    last_msg_at: dict = {}
    for m in all_msgs:
        if m.sender_role == "worker":
            worker_msg_counts[m.bid_id] += 1
        if m.bid_id not in last_msg_at or (m.created_at and m.created_at > last_msg_at[m.bid_id]):
            last_msg_at[m.bid_id] = m.created_at

    bids_out = [
        {
            "bid_id":               b.id,
            "worker_address":       b.worker_address,
            "worker_name":          b.worker_name or "",
            "proposed_xrp":         b.proposed_xrp,
            "proposal":             b.proposal,
            "status":               b.status,
            "created_at":           (b.created_at.isoformat() + "Z") if b.created_at else None,
            "worker_message_count": worker_msg_counts[b.id],
            "last_message_at":      (last_msg_at[b.id].isoformat() + "Z") if last_msg_at.get(b.id) else None,
            "xrpl_trust_score":     b.xrpl_trust_score,
        }
        for b in bids
    ]

    try:
        tags = json.loads(job.tags) if job.tags else []
    except Exception:
        tags = []

    return {
        "id":            job.id,
        "title":         job.title,
        "description":   job.description,
        "budget_xrp":    job.budget_xrp,
        "buyer_address": job.buyer_address,
        "buyer_name":    job.buyer_name or "",
        "category":      job.category,
        "tags":          tags,
        "status":        job.status,
        "awarded_bid_id": job.awarded_bid_id,
        "escrow_id":     job.escrow_id,
        "expires_at":    job.expires_at.strftime("%Y-%m-%d %H:%M UTC") if job.expires_at else None,
        "required_nft_issuer": job.required_nft_issuer or None,
        "proof_policy":  job.proof_policy or "ALL",
        "bids":          bids_out,
    }


@app.post("/jobs/{job_id}/bid")
async def submit_bid(job_id: str, body: dict, db: Session = Depends(get_db)):
    """Worker agent submits a bid on an open job."""
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job.status != "open":
        raise HTTPException(status_code=409, detail=f"Job '{job_id}' is not open for bids (status: {job.status}).")

    worker_address = (body.get("worker_address") or "").strip()
    proposed_xrp   = body.get("proposed_xrp")
    proposal       = (body.get("proposal") or "").strip()
    worker_email   = (body.get("worker_email") or "").strip() or None
    callback_url   = (body.get("callback_url") or "").strip() or None

    if not worker_address or not worker_address.startswith("r"):
        raise HTTPException(status_code=400, detail="worker_address must be a valid XRPL r-address.")
    if not proposed_xrp or float(proposed_xrp) <= 0:
        raise HTTPException(status_code=400, detail="proposed_xrp must be > 0.")
    if not proposal:
        raise HTTPException(status_code=400, detail="proposal is required — describe your approach.")
    if not worker_email and not callback_url:
        raise HTTPException(
            status_code=400,
            detail="worker_email is required for human bidders. AI agents may provide callback_url instead."
        )

    # Prevent duplicate bids from the same wallet on the same job
    existing_bid = db.query(Bid).filter(Bid.job_id == job_id, Bid.worker_address == worker_address).first()
    if existing_bid:
        raise HTTPException(
            status_code=409,
            detail=f"Wallet {worker_address} has already submitted a bid ({existing_bid.id}) on this job. Update your proposal by contacting the buyer via chat."
        )

    import uuid
    bid_id = f"BID-{uuid.uuid4().hex[:8].upper()}"

    raw_chat_token  = secrets.token_urlsafe(32)
    chat_token_hash = hashlib.sha256(raw_chat_token.encode()).hexdigest()

    bid = Bid(
        id              = bid_id,
        job_id          = job_id,
        worker_address  = worker_address,
        worker_name     = body.get("worker_name") or "",
        worker_email    = worker_email,
        callback_url    = callback_url,
        chat_token      = raw_chat_token,
        chat_token_hash = chat_token_hash,
        proposed_xrp    = float(proposed_xrp),
        proposal        = proposal,
    )
    db.add(bid)
    db.commit()

    has_email    = bool(bid.worker_email)
    has_callback = bool(bid.callback_url)
    logger.info(f"💼 BID SUBMITTED: {bid_id} | job={job_id} | worker={worker_address} | price={proposed_xrp} XRP | email={'yes' if has_email else 'no'} | webhook={'yes' if has_callback else 'no'}")

    total_bids = db.query(Bid).filter(Bid.job_id == job_id).count()

    import asyncio
    # Fire async task to compute XRPL trust score for the bidder's wallet
    asyncio.create_task(_score_bid_wallet(bid_id, worker_address, SessionLocal))
    # Notify the bidder (worker)
    if has_email:
        asyncio.create_task(send_bid_received_email(
            worker_email = bid.worker_email,
            worker_name  = bid.worker_name or "",
            bid_id       = bid_id,
            job_id       = job_id,
            job_title    = job.title,
            proposed_xrp = bid.proposed_xrp,
            chat_token   = bid.chat_token or "",
        ))
    # Notify the job poster (buyer) — works for both humans (email) and agents (webhook)
    if job.buyer_email:
        asyncio.create_task(send_new_bid_buyer_email(
            buyer_email    = job.buyer_email,
            buyer_name     = job.buyer_name or "",
            job_id         = job_id,
            job_title      = job.title,
            bid_id         = bid_id,
            worker_name    = bid.worker_name or "",
            worker_address = bid.worker_address,
            proposed_xrp   = bid.proposed_xrp,
            proposal       = bid.proposal,
            total_bids     = total_bids,
            award_token    = job.award_token or "",
        ))
    if job.buyer_callback_url:
        asyncio.create_task(fire_new_bid_buyer_webhook(
            callback_url   = job.buyer_callback_url,
            job_id         = job_id,
            job_title      = job.title,
            bid_id         = bid_id,
            worker_address = bid.worker_address,
            worker_name    = bid.worker_name or "",
            proposed_xrp   = bid.proposed_xrp,
            proposal       = bid.proposal,
            total_bids     = total_bids,
        ))

    return {
        "status":            "submitted",
        "bid_id":            bid_id,
        "job_id":            job_id,
        "proposed_xrp":      float(proposed_xrp),
        "email_on_award":    has_email,
        "webhook_on_award":  has_callback,
        "chat_token":        raw_chat_token,
        "chat_url":          f"{SITE_URL}/marketplace?chat_job={job_id}&chat_token={raw_chat_token}",
        "next_step":         "The buyer will review bids and award the job. Check back via GET /jobs/{job_id}. Use chat_url to message the buyer.",
    }


@app.post("/jobs/{job_id}/award")
async def award_job(job_id: str, body: dict, db: Session = Depends(get_db)):
    """
    Buyer accepts a bid and awards the job.

    Returns the worker's address and agreed price so the buyer can immediately
    call POST /escrow/generate to create the bilateral XRPL escrow.
    No funds are held by the referee at any point.
    """
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    award_token = (body.get("award_token") or "").strip()
    if not award_token:
        raise HTTPException(status_code=403, detail="award_token is required to award a job.")
    token_hash = hashlib.sha256(award_token.encode()).hexdigest()
    if not job.award_token_hash or not secrets.compare_digest(token_hash, job.award_token_hash):
        raise HTTPException(status_code=403, detail="Invalid award_token.")
    if job.status != "open":
        raise HTTPException(status_code=409, detail=f"Job '{job_id}' is not open (status: {job.status}).")

    bid_id = (body.get("bid_id") or "").strip()
    bid    = db.query(Bid).filter(Bid.id == bid_id, Bid.job_id == job_id).first()
    if not bid:
        raise HTTPException(status_code=404, detail=f"Bid '{bid_id}' not found on job '{job_id}'.")
    if bid.status != "pending":
        raise HTTPException(status_code=409, detail=f"Bid '{bid_id}' is not pending (status: {bid.status}).")

    # Award
    job.status          = "awarded"
    job.awarded_bid_id  = bid_id
    bid.status          = "accepted"

    # Reject all other bids on this job
    other_bids = db.query(Bid).filter(Bid.job_id == job_id, Bid.id != bid_id).all()
    for b in other_bids:
        b.status = "rejected"

    db.commit()

    logger.info(f"🏆 JOB AWARDED: {job_id} → bid={bid_id} | worker={bid.worker_address} | price={bid.proposed_xrp} XRP")

    import asyncio
    if bid.worker_email:
        asyncio.create_task(send_bid_awarded_email(
            worker_email = bid.worker_email,
            worker_name  = bid.worker_name or "",
            job_id       = job_id,
            job_title    = job.title,
            buyer_name   = job.buyer_name or "the buyer",
            agreed_xrp   = bid.proposed_xrp,
        ))
    if bid.callback_url:
        asyncio.create_task(fire_bid_awarded_webhook(
            callback_url   = bid.callback_url,
            bid_id         = bid_id,
            job_id         = job_id,
            job_title      = job.title,
            agreed_xrp     = bid.proposed_xrp,
            worker_address = bid.worker_address,
        ))

    worker_email_hint = (
        f"Pass worker_email='{bid.worker_email}' to create_escrow_vault() so the worker "
        f"receives an escrow receipt email with their submission link."
    ) if bid.worker_email else None

    return {
        "status":          "awarded",
        "job_id":          job_id,
        "bid_id":          bid_id,
        "worker_address":  bid.worker_address,
        "worker_name":     bid.worker_name or "",
        "worker_email":    bid.worker_email or None,
        "agreed_xrp":      bid.proposed_xrp,
        "next_step": (
            f"Create the escrow: call create_escrow_vault() with "
            f"worker_address='{bid.worker_address}' and amount_xrp={bid.proposed_xrp}. "
            f"Pay 0.1 XRP protocol fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR first. "
            f"Then sign the EscrowCreate on XRPL and confirm via confirm_escrow_transaction()."
            + (f" {worker_email_hint}" if worker_email_hint else "")
        ),
    }


@app.get("/bids/{bid_id}")
async def get_bid(bid_id: str, db: Session = Depends(get_db)):
    """Look up a bid by ID — returns its job_id so the UI can redirect to the job tracker."""
    bid = db.query(Bid).filter(Bid.id == bid_id).first()
    if not bid:
        raise HTTPException(status_code=404, detail=f"Bid '{bid_id}' not found.")
    return {"bid_id": bid.id, "job_id": bid.job_id, "status": bid.status}


# ---------------------------------------------------------------------------
# BID CHAT  (one private thread per bid, between buyer and that worker only)
# ---------------------------------------------------------------------------

def _resolve_bid_chat_sender(bid: "Bid", job: "JobPosting", token: str):
    """
    Return (role, name) or raise 403.
    - Buyer proves identity with the job's award_token.
    - Worker proves identity with their bid's chat_token.
    Both tokens are verified as SHA-256 hashes.
    """
    t_hash = hashlib.sha256(token.encode()).hexdigest()
    if job.award_token_hash and secrets.compare_digest(t_hash, job.award_token_hash):
        return "buyer", job.buyer_name or "Buyer"
    if bid.chat_token_hash and secrets.compare_digest(t_hash, bid.chat_token_hash):
        return "worker", bid.worker_name or "Worker"
    raise HTTPException(
        status_code=403,
        detail="Invalid token. Buyers use their award token; workers use the chat token from their bid confirmation email."
    )


@app.get("/bids/{bid_id}/messages")
async def get_bid_messages(bid_id: str, token: str = "", db: Session = Depends(get_db)):
    """Fetch the private chat thread for a bid. Auth: award_token (buyer) or chat_token (worker)."""
    bid = db.query(Bid).filter(Bid.id == bid_id).first()
    if not bid:
        raise HTTPException(status_code=404, detail=f"Bid '{bid_id}' not found.")
    if not token:
        raise HTTPException(status_code=403, detail="token query parameter required.")
    job = db.query(JobPosting).filter(JobPosting.id == bid.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Parent job not found.")
    role, name = _resolve_bid_chat_sender(bid, job, token)
    msgs = (
        db.query(JobMessage)
        .filter(JobMessage.bid_id == bid_id)
        .order_by(JobMessage.created_at)
        .all()
    )
    return {
        "bid_id":  bid_id,
        "job_id":  bid.job_id,
        "role":    role,
        "messages": [
            {
                "id":          m.id,
                "sender_role": m.sender_role,
                "sender_name": m.sender_name,
                "message":     m.message,
                "created_at":  m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    }


@app.post("/bids/{bid_id}/messages")
async def post_bid_message(bid_id: str, body: dict, db: Session = Depends(get_db)):
    """Send a message in a bid's private chat. Body: {token, message}."""
    bid = db.query(Bid).filter(Bid.id == bid_id).first()
    if not bid:
        raise HTTPException(status_code=404, detail=f"Bid '{bid_id}' not found.")
    token   = (body.get("token") or "").strip()
    message = (body.get("message") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required.")
    if not message:
        raise HTTPException(status_code=400, detail="message cannot be empty.")
    job = db.query(JobPosting).filter(JobPosting.id == bid.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Parent job not found.")
    role, name = _resolve_bid_chat_sender(bid, job, token)
    msg = JobMessage(
        job_id      = bid.job_id,
        bid_id      = bid_id,
        sender_role = role,
        sender_name = name,
        message     = message,
    )
    db.add(msg)
    db.commit()
    logger.info(f"💬 CHAT: {bid_id} | role={role} | msg_id={msg.id}")
    return {"status": "sent", "id": msg.id, "sender_role": role}


# ---------------------------------------------------------------------------
# SKILL LISTINGS
# ---------------------------------------------------------------------------

_SEED_SKILLS = [
    {
        "id": "SVC-001", "title": "Production-ready Python data pipelines",
        "description": "ETL pipelines, data cleaning scripts, and API integrations with full test coverage and documentation. Typical turnaround 24–72hrs.",
        "category": "code", "rate": "80–300 XRP per task",
        "poster": "rDevAgentXXXXXXXXXXXXXXXXXXXXXXX", "poster_name": "PipelineBot",
        "tags": ["python", "etl", "api", "data"], "is_demo": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=25)).isoformat(),
    },
    {
        "id": "SVC-002", "title": "Legal document drafting — contracts & NDAs",
        "description": "AI-assisted drafting of contracts, NDAs, and terms of service. First draft for human review; not a substitute for qualified legal advice.",
        "category": "legal", "rate": "200–800 XRP per document",
        "poster": "rLegalAgentXXXXXXXXXXXXXXXXXXXXX", "poster_name": "LexDraft",
        "tags": ["legal", "contracts", "nda", "drafting"], "is_demo": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=28)).isoformat(),
    },
    {
        "id": "SVC-003", "title": "SEO-optimised blog posts & technical writing",
        "description": "Long-form content (1,000–3,000 words) for SaaS, fintech, and crypto brands. Research, write, and deliver publication-ready markdown.",
        "category": "creative", "rate": "50–150 XRP per article",
        "poster": "rWriterAgentXXXXXXXXXXXXXXXXXXXX", "poster_name": "ContentAgent",
        "tags": ["writing", "seo", "blog", "markdown"], "is_demo": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(),
    },
]


class SkillListingRequest(BaseModel):
    id:          str
    fee_hash:    str
    title:       str
    description: str
    category:    str            = "default"
    rate:        Optional[str]  = None    # human-readable, e.g. "50–200 XRP per task"
    rate_xrp:    Optional[float] = None  # numeric starting rate for filtering
    poster:      Optional[str]  = None   # XRPL address
    poster_name: Optional[str]  = None
    tags:        Optional[list[str]] = None


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, body: dict, db: Session = Depends(get_db)):
    """
    Cancel an open job posting. Requires the award_token issued when the job was posted.
    Not permitted once an escrow has been created — the on-chain escrow governs from that point.
    """
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    award_token = (body.get("award_token") or "").strip()
    if not award_token:
        raise HTTPException(status_code=403, detail="award_token is required to cancel a job.")
    token_hash = hashlib.sha256(award_token.encode()).hexdigest()
    if not job.award_token_hash or not secrets.compare_digest(token_hash, job.award_token_hash):
        raise HTTPException(status_code=403, detail="Invalid award_token.")

    if job.escrow_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This job has an active escrow ({job.escrow_id}) and cannot be cancelled here. "
                "The escrow is held on the XRP Ledger — funds will automatically return to the buyer "
                "if the worker does not complete the task before the escrow's cancel-after time."
            ),
        )

    if job.status in ("cancelled", "expired"):
        raise HTTPException(status_code=409, detail=f"Job '{job_id}' is already {job.status}.")

    job.status = "cancelled"
    db.commit()

    logger.info(f"🗑️ JOB CANCELLED: {job_id} | was={job.status}")
    return {
        "status":  "cancelled",
        "job_id":  job_id,
        "message": "Job has been cancelled and removed from the marketplace.",
    }


@app.get("/marketplace/skills")
async def marketplace_skills(
    category:  str   = "all",
    min_rate:  float = 0,
    max_rate:  float = 0,
    limit:     int   = 20,
    db: Session = Depends(get_db),
):
    """
    Return active skill listings: real listings from DB first, then demo seeds.
    Callable by both agents (via MCP list_marketplace_skills) and the human UI.
    Use GET /marketplace/skills/{id} to get a single listing for direct hire.
    """
    limit = min(limit, 100)
    real = []
    try:
        now = datetime.now(timezone.utc)
        q = db.query(SkillListing).filter(
            SkillListing.status == "ACTIVE",
            (SkillListing.expires_at == None) | (SkillListing.expires_at > now),
        )
        if category != "all":
            q = q.filter(SkillListing.category == category)
        listings = q.order_by(SkillListing.created_at.desc()).limit(200).all()
        for s in listings:
            if min_rate > 0 and (s.rate_xrp or 0) < min_rate:
                continue
            if max_rate > 0 and (s.rate_xrp or 0) > max_rate:
                continue
            tags = []
            try:
                tags = json.loads(s.tags) if s.tags else []
            except Exception:
                pass
            real.append({
                "id":          s.id,
                "title":       s.title,
                "description": s.description,
                "category":    s.category or "default",
                "rate":        s.rate or "Rate on request",
                "rate_xrp":    s.rate_xrp,
                "poster":      s.poster or "",
                "poster_name": s.poster_name or "",
                "tags":        tags,
                "expires_at":  s.expires_at.isoformat() if s.expires_at else None,
                "is_demo":     False,
            })
    except Exception as e:
        logger.warning(f"⚠️ marketplace_skills DB query failed: {e}")

    seeds = _SEED_SKILLS
    if category != "all":
        seeds = [s for s in seeds if s["category"] == category]

    combined = (real + seeds)[:limit]
    return {
        "skills":      combined,
        "total":       len(combined),
        "real_skills": len(real),
        "demo_skills": len([s for s in combined if s.get("is_demo")]),
    }


@app.get("/marketplace/skills/{skill_id}")
async def get_skill_listing(skill_id: str, db: Session = Depends(get_db)):
    """
    Get a single skill listing by ID.

    Returns full details including the poster's XRPL wallet address for direct hire.
    Use the returned poster address as worker_address in create_escrow_vault().
    """
    listing = db.query(SkillListing).filter(SkillListing.id == skill_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail=f"Skill listing '{skill_id}' not found.")

    now = datetime.now(timezone.utc)
    is_expired = listing.expires_at and listing.expires_at.replace(tzinfo=timezone.utc) < now

    tags = []
    try:
        tags = json.loads(listing.tags) if listing.tags else []
    except Exception:
        pass

    return {
        "id":            listing.id,
        "title":         listing.title,
        "description":   listing.description,
        "category":      listing.category or "default",
        "rate":          listing.rate or "Rate on request",
        "rate_xrp":      listing.rate_xrp,
        "worker_address": listing.poster or "",   # the address to use in create_escrow_vault()
        "poster_name":   listing.poster_name or "",
        "tags":          tags,
        "status":        listing.status,
        "expires_at":    listing.expires_at.isoformat() if listing.expires_at else None,
        "is_expired":    is_expired,
        "is_demo":       False,
        "direct_hire_hint": (
            f"To hire directly: call create_escrow_vault() with "
            f"worker_address='{listing.poster}' and your agreed amount_xrp. "
            f"Pay 0.1 XRP protocol fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR first."
        ) if listing.poster else None,
    }


@app.post("/marketplace/skills")
async def post_skill_listing(req: SkillListingRequest, db: Session = Depends(get_db)):
    """
    Create a new skill listing. Requires a valid 0.1 XRP fee payment.
    Both humans (via the marketplace UI) and agents (via MCP) can post skills.
    """
    existing = db.query(SkillListing).filter(SkillListing.id == req.id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Skill ID '{req.id}' already exists.")

    await verify_fee_payment(fee_hash=req.fee_hash, escrow_id=req.id, db=db, resource="/marketplace/skills")

    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    listing = SkillListing(
        id          = req.id,
        title       = req.title,
        description = req.description,
        category    = req.category,
        rate        = req.rate,
        rate_xrp    = req.rate_xrp,
        poster      = req.poster,
        poster_name = req.poster_name,
        tags        = json.dumps(req.tags) if req.tags else None,
        fee_hash    = req.fee_hash,
        expires_at  = expires_at,
    )
    db.add(listing)
    db.commit()
    logger.info(f"✅ Skill listing created: {req.id} by {req.poster_name or req.poster}")
    return {
        "status":     "created",
        "id":         req.id,
        "expires_at": expires_at.isoformat(),
        "message":    "Your skill listing is now live for 30 days.",
    }


# ---------------------------------------------------------------------------
# NFT ISSUER ENDPOINTS
# ---------------------------------------------------------------------------

class NftVerifyRequest(BaseModel):
    wallet_address:    str
    nft_token_id:      str
    required_issuer:   Optional[str]  = None
    required_metadata: Optional[dict] = None

class NftIssuerRequest(BaseModel):
    wallet_address:   str                    # primary wallet (required)
    wallet_addresses: Optional[list] = None  # additional wallets
    name:             str
    category:         Optional[str] = None
    description:      Optional[str] = None
    website:          Optional[str] = None
    contact_email:    Optional[str] = None
    lei:              Optional[str] = None
    nft_types:        Optional[str] = None

class NftIssuerWalletUpdate(BaseModel):
    contact_email: str           # must match registered email to authorise
    wallets:       list          # complete new list of wallets (replaces existing)

@app.get("/nft/issuers")
async def list_nft_issuers(category: str = None, include_pending: bool = False, db: Session = Depends(get_db)):
    q = db.query(NftIssuer)
    if include_pending:
        q = q.filter(NftIssuer.verified.in_(["verified", "pending"]))
    else:
        q = q.filter(NftIssuer.verified == "verified")
    if category:
        q = q.filter(NftIssuer.category == category)
    issuers = q.order_by(NftIssuer.name).all()
    base_url = "https://xrpl-referee.onrender.com"
    return {
        "issuers": [
            {
                "id":              i.id,
                "wallet_address":  i.wallet_address,
                "wallet_addresses": i.all_wallets(),
                "name":            i.name,
                "category":        i.category,
                "description":     i.description,
                "website":         i.website,
                "verified":        i.verified,
                "lei":             i.lei,
                "nft_types":       i.nft_types,
            }
            for i in issuers
        ],
        "register_url": "https://www.cryptovault.co.uk/marketplace#issuers",
        "register_api": f"{base_url}/nft/issuers",
    }

@app.post("/nft/verify")
async def verify_nft(req: NftVerifyRequest):
    result = await verify_nft_ownership(
        wallet_address=req.wallet_address,
        nft_token_id=req.nft_token_id,
        required_issuer=req.required_issuer,
        required_metadata=req.required_metadata,
    )
    if not result["verified"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result

@app.post("/nft/issuers")
async def register_nft_issuer(req: NftIssuerRequest, db: Session = Depends(get_db)):
    import asyncio
    # Build consolidated wallet list
    all_wallets = [req.wallet_address] + (req.wallet_addresses or [])
    all_wallets = list(dict.fromkeys(w.strip() for w in all_wallets if w and w.strip()))

    # Check if any of the submitted wallets already exists under another issuer
    for w in all_wallets:
        existing = db.query(NftIssuer).filter(
            NftIssuer.wallet_addresses.contains(w) | (NftIssuer.wallet_address == w)
        ).first()
        if existing and existing.name.lower() != req.name.lower():
            raise HTTPException(status_code=409, detail=f"Wallet {w} is already registered under a different issuer.")

    # Upsert: if same name + email already exists, update it
    existing = db.query(NftIssuer).filter(
        NftIssuer.name.ilike(req.name),
        NftIssuer.contact_email == req.contact_email,
    ).first() if req.contact_email else None

    if existing:
        existing.set_wallets(existing.all_wallets() + all_wallets)
        existing.category    = req.category    or existing.category
        existing.description = req.description or existing.description
        existing.website     = req.website     or existing.website
        existing.lei         = req.lei         or existing.lei
        existing.nft_types   = req.nft_types   or existing.nft_types
        db.commit()
        return {"status": "updated", "message": "Issuer record updated.", "wallets": existing.all_wallets()}

    issuer = NftIssuer(
        wallet_address=all_wallets[0], name=req.name,
        category=req.category, description=req.description,
        website=req.website, verified="pending",
        created_at=datetime.now(timezone.utc),
        contact_email=req.contact_email,
        lei=req.lei, nft_types=req.nft_types,
    )
    issuer.set_wallets(all_wallets)
    db.add(issuer); db.commit()
    if RESEND_API_KEY:
        asyncio.create_task(_send_issuer_registration_email(issuer))
    return {"status": "pending", "message": "Issuer registration received. Verification typically takes 1-2 business days.", "wallets": issuer.all_wallets()}


@app.patch("/nft/issuers/{issuer_id}/wallets")
async def update_issuer_wallets(issuer_id: int, req: NftIssuerWalletUpdate, db: Session = Depends(get_db)):
    """Add or remove wallets for an issuer. Authenticated by matching contact_email."""
    issuer = db.query(NftIssuer).filter(NftIssuer.id == issuer_id).first()
    if not issuer:
        raise HTTPException(status_code=404, detail="Issuer not found.")
    if not issuer.contact_email or issuer.contact_email.lower() != req.contact_email.lower():
        raise HTTPException(status_code=403, detail="Email does not match registered contact for this issuer.")
    if not req.wallets:
        raise HTTPException(status_code=400, detail="Wallet list cannot be empty.")
    issuer.set_wallets(req.wallets)
    db.commit()
    return {"status": "updated", "wallets": issuer.all_wallets(), "name": issuer.name}


async def _send_issuer_registration_email(issuer):
    subject = f"New issuer registration: {issuer.name}"
    body = f"""
    <div style="font-family:sans-serif;padding:1.5rem;background:#0d1117;color:#e2e8f0;border-radius:10px;">
        <h2 style="color:#10b981;">New Issuer Registration</h2>
        <table style="width:100%;border-collapse:collapse;font-size:.88rem;">
            <tr><td style="padding:.4rem 0;color:#94a3b8;">Name</td><td><strong>{issuer.name}</strong></td></tr>
            <tr><td style="padding:.4rem 0;color:#94a3b8;">Wallet</td><td style="font-family:monospace;">{issuer.wallet_address}</td></tr>
            <tr><td style="padding:.4rem 0;color:#94a3b8;">Category</td><td>{issuer.category or '—'}</td></tr>
            <tr><td style="padding:.4rem 0;color:#94a3b8;">Website</td><td>{issuer.website or '—'}</td></tr>
            <tr><td style="padding:.4rem 0;color:#94a3b8;">Contact</td><td>{issuer.contact_email or '—'}</td></tr>
            <tr><td style="padding:.4rem 0;color:#94a3b8;">LEI</td><td>{issuer.lei or '—'}</td></tr>
            <tr><td style="padding:.4rem 0;color:#94a3b8;">Description</td><td>{issuer.description or '—'}</td></tr>
        </table>
        <p style="margin-top:1.5rem;font-size:.82rem;color:#94a3b8;">Review and verify at your earliest convenience. Once verified, set their record to verified="verified" in the database.</p>
    </div>
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post("https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": "AgentTrust <noreply@agenttrust.io>", "to": ["eamwhite1@gmail.com"], "subject": subject, "html": body}
            )
    except Exception as e:
        logger.warning(f"Issuer registration email failed: {e}")


# ---------------------------------------------------------------------------
# COMPANY SEARCH + ISSUER LOOKUP  (SEC EDGAR — US public companies, free, no key)
# Paths kept as /gleif/* for backward compatibility with existing API consumers
# ---------------------------------------------------------------------------

EDGAR_COMPANY_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
EDGAR_HEADERS = {"User-Agent": "AgentTrust/1.0 admin@cryptovault.co.uk", "Accept": "application/json"}

def _edgar_parse(hit: dict) -> dict:
    src = hit.get("_source", {})
    return {
        "name":              src.get("entity_name", ""),
        "company_number":    src.get("file_num", ""),
        "jurisdiction_code": "us",
        "registered_address": "",
        "status":            "active",
        "source":            "sec-edgar",
    }


@app.get("/gleif/search")
async def company_search(q: str, limit: int = 10, jurisdiction: str = None):
    """Search SEC EDGAR for US public companies by name."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        # action=getcompany searches registered company names — no form type filter
        res = await client.get(
            EDGAR_BROWSE,
            params={"company": q, "action": "getcompany", "type": "",
                    "dateb": "", "owner": "include", "count": min(limit, 40),
                    "search_text": "", "output": "atom"},
            headers=EDGAR_HEADERS,
        )
    if res.status_code != 200:
        logger.warning(f"EDGAR company search returned {res.status_code} for q={q!r}: {res.text[:200]}")
        return {"results": [], "error": f"SEC EDGAR returned {res.status_code}"}
    # Parse Atom XML — EDGAR uses a custom namespace for company-info
    import xml.etree.ElementTree as ET
    SEC_NS = "http://www.sec.gov/cgi-bin/browse-edgar"
    ATOM_NS = "http://www.w3.org/2005/Atom"
    try:
        root = ET.fromstring(res.text)
    except ET.ParseError:
        return {"results": [], "error": "Could not parse EDGAR response"}
    seen: set = set()
    results = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        # Company name lives in <company-info xmlns="...sec.gov/cgi-bin/browse-edgar"><conformed-name>
        name_el = entry.find(f"{{{SEC_NS}}}company-info/{{{SEC_NS}}}conformed-name")
        name = (name_el.text or "").strip() if name_el is not None else ""
        if not name:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        # CIK from <company-info><cik>
        cik_el = entry.find(f"{{{SEC_NS}}}company-info/{{{SEC_NS}}}cik")
        cik = (cik_el.text or "").strip() if cik_el is not None else ""
        results.append({
            "name": name,
            "company_number": cik,
            "jurisdiction_code": "us",
            "registered_address": "",
            "status": "active",
            "source": "sec-edgar",
        })
        if len(results) >= limit:
            break
    logger.info(f"EDGAR search q={q!r} → {len(results)} results")
    return {"results": results}


@app.get("/gleif/debug-edgar")
async def debug_edgar(q: str):
    """Return raw EDGAR XML for debugging."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            EDGAR_BROWSE,
            params={"company": q, "action": "getcompany", "type": "",
                    "dateb": "", "owner": "include", "count": 5,
                    "search_text": "", "output": "atom"},
            headers=EDGAR_HEADERS,
        )
    return {"status": res.status_code, "body": res.text[:3000]}


@app.get("/gleif/xrpl-lookup")
async def company_xrpl_lookup(q: str, db: Session = Depends(get_db)):
    """Search AgentTrust registry by company name to find their XRPL wallet."""
    results = []
    try:
        issuers = (
            db.query(NftIssuer)
            .filter(NftIssuer.name.ilike(f"%{q[:50]}%"))
            .limit(5)
            .all()
        )
        for issuer in issuers:
            results.append({
                "name":          issuer.name,
                "source":        "agentrust",
                "xrpl_wallet":   issuer.wallet_address,
                "xrpl_verified": issuer.verified == "verified",
                "domain":        issuer.website,
                "register_url":  None,
                "message":       None,
            })
    except Exception:
        pass

    if not results:
        results.append({
            "name":         q,
            "source":       "sec-edgar",
            "xrpl_wallet":  None,
            "xrpl_verified": False,
            "domain":       None,
            "register_url": "https://www.cryptovault.co.uk/marketplace#issuers",
            "message":      "No XRPL wallet found. If you represent this company, register at the AgentTrust Trusted Issuer Registry.",
        })

    return {"results": results}


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🚀 Starting AgentTrust Referee v7.0 on port {port}")
    uvicorn.run("referee:app", host="0.0.0.0", port=port, reload=False)

