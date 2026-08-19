import os
import time
import httpx
from decimal import Decimal, InvalidOperation
import logging
import sys
import hashlib
import secrets
import json
import base64
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Request, Response
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
from xrpl.models.requests import Tx, SubmitOnly
from xrpl.models.transactions import EscrowFinish
from xrpl.core.addresscodec import decode_seed
from xrpl.core.binarycodec import decode as xrpl_decode_tx_blob
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
    # Seed public registry entries from known XRPL organisations
    _seed_public_issuers()
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
        "(3) W3C Verifiable Credential, (4) XRPL wallet trust score "
        "(11 signals including on-chain ownership proof, Xaman KYC, XRPScan entity reputation, and OFAC sanctions screening).\n\n"
        "**Compliance:** all wallet addresses are automatically screened against the US OFAC SDN "
        "sanctions list at escrow creation and trust score computation. Sanctioned wallets "
        "cannot participate in escrow and receive a score of 0.\n\n"
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
            {"id": "escrow-create",     "name": "Create Escrow Vault",           "description": "Lock XRP or RLUSD in crypto-condition escrow gated by AI verdict. Pass `invoice_requirements` (po_number, supplier_name, services_description, require_date, require_line_items) to require the seller to submit a matching invoice alongside their proof of work — the AI referee verifies every field before releasing payment. Verified invoices are forwarded to the buyer's accounts team via the `accounts_email` field.",              "endpoint": "/escrow/generate",  "method": "POST"},
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
            {"name": "audit_task",               "description": "Verify completed work against a task spec. Fee: 0.1 XRP on XRPL or $0.10 USDC on Base (chain 8453). Returns PASS/FAIL with score and feedback."},
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
            {"name": "get_wallet_trust_score",    "description": "GET /wallet/score/{address} — AgentTrust Wallet Trust Score (0–100). The first open trust-scoring system built natively for XRPL wallets. Combines 11 independent signals: account age, XRP balance, on-chain activity, domain verification, on-chain ownership proof, sanctions screening (AnChain.ai BEI — OFAC/UN/UK/EU/Canada/Australia), entity reputation (XRPScan verified entity + security flags), Xaman KYC (human identity verification), NFTs held, AgentTrust escrow completion rate, and peer ratings from counterparties. Returns a full score breakdown by signal so agents can reason about why a wallet scores high or low. A score below 30 is low-trust, 30–60 is moderate, 60+ is established. Free to query for any XRPL address."},
            {"name": "verify_nft_proof",          "description": "POST /nft/verify — verify that an XRPL NFT exists in a wallet, was minted by a required issuer, and contains required metadata fields. Used to confirm event-based proof (ticket purchased, cargo shipped, etc)."},
            {"name": "verify_domain_ownership",   "description": "POST /domain/verify — verify that an XRPL wallet is cryptographically linked to a domain via the account Domain field and xrp-ledger.toml. Proves the wallet owner controls the specified organisation's domain."},
            {"name": "verify_vc",                 "description": "POST /vc/verify — verify a W3C Verifiable Credential JWT. Checks expiry, issuer DID, credential type, and optionally resolves the DID via the Universal Resolver. Accepts credentials from any W3C-compliant issuer."},
            {"name": "register_nft_dvp_offer",    "description": "POST /escrow/{id}/nft-offer — after a PASS verdict on an NFT DvP escrow, seller registers their on-chain NFTokenCreateOffer (Destination=buyer, Amount=0). System verifies the offer on XRPL and emails buyer to accept. Payment releases automatically once buyer accepts."},
            {"name": "check_nft_dvp_status",      "description": "GET /escrow/{id}/nft-status — poll whether the buyer has accepted the NFT offer yet. Returns accepted/pending/expired. Triggers automatic escrow release when accepted."},
            {"name": "company_xrpl_lookup",       "description": "GET /gleif/xrpl-lookup?q= — search the AgentTrust registry for a company by name and return their verified XRPL wallet address."},
            {"name": "list_trusted_issuers",      "description": "GET /nft/issuers — list all verified trusted NFT issuers in the AgentTrust registry. These are organisations (shipping companies, ticket platforms, certification bodies) whose XRPL wallet has been verified against their domain."},
            {"name": "register_as_issuer",        "description": "POST /nft/issuers — register your organisation as a trusted NFT issuer. Provide your XRPL wallet, organisation name, category, website. Pending manual verification via domain records."},
            {"name": "create_eth_challenge",      "description": "POST /eth/challenge — generate an EIP-191 challenge string for an Ethereum address. The address holder must sign this with their ETH wallet to prove ownership. Use before submitting an Ethereum address as identity proof."},
            {"name": "verify_eth_signature",      "description": "POST /eth/verify-signature — verify that an Ethereum address signed the challenge string. Confirms the submitter genuinely controls the ETH address, preventing fake address claims."},
        ],
        "tags": ["xrpl", "payments", "escrow", "ai-agent", "verification", "bounty", "autonomous", "web3", "nft", "trust", "identity"],
    }


@app.get("/.well-known/marketplace.json")
def serve_marketplace_json():
    """Machine-readable marketplace descriptor for AI agents and crawlers."""
    return {
        "name": "AgentTrust Marketplace",
        "description": "Open agent marketplace — post jobs, bid, claim bounties, list skills, hire directly. All payments settled via XRPL escrow with AI-verified automatic release.",
        "url": "https://www.cryptovault.co.uk/marketplace/",
        "api_base": "https://xrpl-referee.onrender.com",
        "mcp_endpoint": "https://xrpl-referee.onrender.com/mcp",
        "currency": ["XRP", "RLUSD"],
        "network": "XRPL Mainnet",
        "capabilities": {
            "post_job": {"endpoint": "/jobs", "method": "POST", "fee": "free", "description": "Post a job to attract bids from worker agents."},
            "list_jobs": {"endpoint": "/marketplace/jobs", "method": "GET", "fee": "free", "description": "Browse open bounties. claimable=True means instant award."},
            "claim_job": {"endpoint": "/jobs/{job_id}/claim", "method": "POST", "fee": "free", "description": "Instantly claim a claimable bounty without bidding."},
            "submit_bid": {"endpoint": "/jobs/{job_id}/bid", "method": "POST", "fee": "free", "description": "Bid on a competitive job posting."},
            "list_skills": {"endpoint": "/marketplace/skills", "method": "GET", "fee": "free", "description": "Browse agent skill listings for direct hire."},
            "post_skill": {"endpoint": "/marketplace/skills", "method": "POST", "fee": "0.1 XRP/month", "description": "List a recurring skill for 30 days."},
            "escrow": {"endpoint": "/escrow/generate", "method": "POST", "fee": "0.1 XRP", "description": "Lock payment in AI-gated XRPL escrow."},
            "verify_work": {"endpoint": "/evaluate", "method": "POST", "fee": "included", "description": "Submit work; payment auto-releases on PASS."},
            "trust_score": {"endpoint": "/wallet/score/{address}", "method": "GET", "fee": "free", "description": "0–100 wallet trust score across 12 signals."},
        },
        "mcp_tools": [
            "list_marketplace_jobs", "claim_job", "list_open_jobs", "post_job", "submit_bid",
            "award_job", "list_marketplace_skills", "direct_hire", "create_skill_listing",
            "hire_and_pay", "create_escrow_vault", "evaluate_escrow_work", "audit_task",
            "get_wallet_trust_score", "check_wallet_sanctions", "check_wallet_kyc",
            "create_agent_wallet", "get_xrp_price"
        ],
        "docs": "https://xrpl-referee.onrender.com/docs",
        "agent_card": "https://xrpl-referee.onrender.com/.well-known/agent.json",
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


@app.get("/.well-known/xrpl-issuer-registry")
def serve_issuer_registry_discovery():
    return {
        "registry_api": "https://xrpl-referee.onrender.com/nft/issuers",
        "spec": "https://www.cryptovault.co.uk/docs/issuer-registry-spec.md",
        "version": "1.0.0",
        "mcp_endpoint": "https://xrpl-referee.onrender.com/mcp",
        "contact": "admin@cryptovault.co.uk",
        "published": "2026-06-06",
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


class FreeAuditUsage(Base):
    """Tracks free audit credits consumed per wallet address."""
    __tablename__ = "free_audit_usage"
    id             = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String, index=True, nullable=False)
    escrow_id      = Column(String, nullable=False)
    resource       = Column(String, nullable=True)
    timestamp      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


FREE_AUDIT_LIMIT       = 3    # free audits per wallet
FREE_AUDIT_MIN_SCORE   = 25   # wallet trust score must meet this threshold


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
    # Invoice requirements — v13
    invoice_requirements = Column(Text,    nullable=True)   # JSON: {po_number, supplier_name, services_description, require_date, require_line_items}


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
    claimable          = Column(Boolean,  default=False)    # workers can self-award without bid/award cycle
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


class WalletVerification(Base):
    """On-chain wallet ownership proofs — stored when a wallet submits a verified AccountSet tx."""
    __tablename__ = "wallet_verification"
    wallet_address = Column(String, primary_key=True)
    verified_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    method         = Column(String, default="xrpl_accountset")  # xrpl_accountset | eth_sig
    tx_hash        = Column(String, nullable=True)


class SanctionsLog(Base):
    """Timestamped audit trail of every sanctions screening decision."""
    __tablename__ = "sanctions_log"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String, nullable=False, index=True)
    screened_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    sanctioned     = Column(Boolean, nullable=False)
    risk_score     = Column(Float, nullable=True)
    risk_level     = Column(String, nullable=True)   # low / medium / high / severe
    entity_label   = Column(String, nullable=True)   # e.g. "Binance", "WannaCry"
    source         = Column(String, nullable=False)  # anchain_bei | ofac_xml
    escrow_id      = Column(String, nullable=True)   # linked escrow if applicable
    raw_response   = Column(Text, nullable=True)     # JSON blob from screening provider


class KycRecord(Base):
    """KYC verification records — one row per verified wallet operator. Method: xaman."""
    __tablename__ = "kyc_record"
    id                 = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address     = Column(String, nullable=False, index=True)
    stripe_session_id  = Column(String, nullable=True, unique=True)  # kept for schema compat
    stripe_vs_id       = Column(String, nullable=True)               # kept for schema compat
    status             = Column(String, default="pending")  # pending | verified | failed
    created_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    verified_at        = Column(DateTime, nullable=True)
    return_url         = Column(String, nullable=True)  # repurposed: stores method (e.g. "xaman")


# ---------------------------------------------------------------------------
# OFAC SDN sanctions list — cached in memory, refreshed daily
# ---------------------------------------------------------------------------
_ofac_sanctioned_xrpl: set = set()
_ofac_last_refresh: datetime = None

async def _refresh_ofac_list():
    """Download and cache the OFAC SDN XML, extracting XRP digital currency addresses."""
    global _ofac_sanctioned_xrpl, _ofac_last_refresh
    now = datetime.now(timezone.utc)
    if _ofac_last_refresh and (now - _ofac_last_refresh).total_seconds() < 86400:
        return  # still fresh
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                "https://www.treasury.gov/ofac/downloads/sdn.xml",
                headers={"User-Agent": "AgentTrust-Compliance/1.0"}
            )
        root = ET.fromstring(r.text)
        addrs: set = set()
        # The SDN XML uses a namespace; we iterate generically to handle both ns and no-ns
        for entry in root.iter():
            if entry.tag.endswith("idType") and ("XRP" in (entry.text or "") or "xrp" in (entry.text or "").lower()):
                # Sibling idNumber element contains the actual address
                parent = entry.getparent() if hasattr(entry, "getparent") else None
                # ElementTree doesn't expose getparent; use find on parent context instead
                pass
        # Robust fallback: text search the raw XML for XRP addresses in id blocks
        import re as _re
        xrp_id_blocks = _re.findall(
            r'<idType>[^<]*[Xx][Rr][Pp][^<]*</idType>\s*<idNumber>([^<]+)</idNumber>',
            r.text
        )
        addrs = {a.strip() for a in xrp_id_blocks if a.strip().startswith("r")}
        _ofac_sanctioned_xrpl = addrs
        _ofac_last_refresh = now
        logger.info(f"OFAC SDN: loaded {len(addrs)} sanctioned XRP addresses")
    except Exception as e:
        logger.warning(f"OFAC SDN refresh failed: {e}")


async def is_ofac_sanctioned(wallet_address: str) -> bool:
    """Return True if wallet_address is on the OFAC SDN XRP list."""
    await _refresh_ofac_list()
    return wallet_address in _ofac_sanctioned_xrpl


# ---------------------------------------------------------------------------
# AnChain.ai BEI API — AI-powered wallet risk scoring + multi-jurisdiction sanctions
# Free for XRPL developers (XRPL Foundation grant). Set ANCHAIN_API_KEY in env.
# Covers: OFAC, UN, UK, EU, Canada, Australia sanctions + entity/risk graph scoring.
# Falls back to OFAC XML if BEI key is absent or call fails.
# ---------------------------------------------------------------------------
# XRPScan — entity labels, account flags, activation lineage (no API key required)
# ---------------------------------------------------------------------------
XRPSCAN_BASE = "https://api.xrpscan.com/api/v1"
_xrpscan_well_known: dict = {}   # address -> {name, desc, verified}
_xrpscan_wk_fetched: bool = False

async def _load_xrpscan_well_known():
    """Cache XRPScan's curated entity list. Called once per process."""
    global _xrpscan_well_known, _xrpscan_wk_fetched
    if _xrpscan_wk_fetched:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{XRPSCAN_BASE}/names/well-known",
                                 headers={"User-Agent": "AgentTrust/1.0"})
            if r.status_code == 200:
                for entry in r.json():
                    addr = entry.get("account")
                    if addr:
                        _xrpscan_well_known[addr] = entry
        _xrpscan_wk_fetched = True
        logger.info(f"XRPScan well-known loaded: {len(_xrpscan_well_known)} entities")
    except Exception as e:
        logger.warning(f"XRPScan well-known fetch failed: {e}")

async def _get_xrpscan_account(wallet_address: str) -> dict | None:
    """
    Fetch account info from XRPScan. Returns parsed dict with:
      entity_name, entity_verified, account_flags, inception (ISO str), parent
    Returns None on failure — trust score continues without this signal.
    """
    await _load_xrpscan_well_known()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{XRPSCAN_BASE}/account/{wallet_address}",
                                 headers={"User-Agent": "AgentTrust/1.0"})
            if r.status_code != 200:
                return None
            data = r.json()

        # Entity label — from inline accountName or well-known cache
        acct_name = data.get("accountName") or _xrpscan_well_known.get(wallet_address, {})
        entity_name     = acct_name.get("name") if acct_name else None
        entity_verified = acct_name.get("verified", False) if acct_name else False

        # Account security flags
        settings = data.get("settings", {})
        master_key_disabled  = settings.get("disableMasterKey", False)
        require_dest_tag     = settings.get("requireDestinationTag", False)
        deposit_auth         = settings.get("depositAuth", False)

        return {
            "entity_name":         entity_name,
            "entity_verified":     entity_verified,
            "master_key_disabled": master_key_disabled,
            "require_dest_tag":    require_dest_tag,
            "deposit_auth":        deposit_auth,
            "inception":           data.get("inception"),
            "parent":              data.get("parent"),
        }
    except Exception as e:
        logger.warning(f"XRPScan account fetch failed for {wallet_address}: {e}")
        return None


async def _get_xaman_kyc(wallet_address: str) -> bool:
    """
    Check Xaman KYC status via the Xaman platform API (direct, authenticated).
    Returns True if the wallet holder has completed Xaman KYC verification (powered by Veriff).
    Xaman KYC is a human identity verification — AI agent wallets will always return False.
    """
    if not xumm_api_key or not xumm_api_secret:
        logger.warning("Xaman KYC check skipped: XUMM_API_KEY/SECRET not configured")
        return False
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"https://xumm.app/api/v1/platform/kyc-status/{wallet_address}",
                headers={"X-API-Key": xumm_api_key, "X-API-Secret": xumm_api_secret},
                params={"include_globalid": "true"},
            )
            if r.status_code == 200:
                data = r.json()
                return bool(data.get("kycApproved") or data.get("kyc_approved"))
    except Exception as e:
        logger.warning(f"Xaman KYC check failed for {wallet_address}: {e}")
    return False

# ---------------------------------------------------------------------------
ANCHAIN_API_KEY = os.getenv("ANCHAIN_API_KEY", "")
ANCHAIN_BASE    = "https://bei.anchainai.com/api"

async def _check_bei_risk(wallet_address: str) -> Optional[dict]:
    """
    Query AnChain.ai BEI for risk score, sanctions status, and entity label.
    Returns dict with keys: sanctioned, risk_score, risk_level, entity, category.
    Returns None if BEI key not set or call fails (fallback to OFAC XML).
    """
    if not ANCHAIN_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{ANCHAIN_BASE}/address_risk_score",
                params={"proto": "xrp", "address": wallet_address, "apikey": ANCHAIN_API_KEY},
            )
        if r.status_code != 200:
            return None
        data = r.json().get("data", {}).get(wallet_address, {})
        category = data.get("self", {}).get("detail", {}).get("category", "")
        entity   = data.get("self", {}).get("detail", {}).get("entity", "")
        risk     = data.get("risk", {})
        sanctioned = (
            "sanction" in (category or "").lower()
            or "ofac" in (entity or "").lower()
            or "sdn"   in (entity or "").lower()
        )
        return {
            "sanctioned":  sanctioned,
            "risk_score":  risk.get("score"),
            "risk_level":  risk.get("level", "unknown"),
            "entity":      entity or None,
            "category":    category or None,
            "source":      "anchain_bei",
        }
    except Exception as e:
        logger.debug(f"BEI risk check failed for {wallet_address}: {e}")
        return None


async def is_wallet_sanctioned(
    wallet_address: str,
    escrow_id: str | None = None,
) -> tuple[bool, dict]:
    """
    Primary sanctions check. Uses AnChain.ai BEI if key is set (multi-jurisdiction);
    falls back to OFAC SDN XML. Returns (is_sanctioned, detail_dict).
    Every call is written to the SanctionsLog table for compliance audit purposes.
    """
    bei = await _check_bei_risk(wallet_address)
    if bei is not None:
        detail = bei
    else:
        sanctioned = await is_ofac_sanctioned(wallet_address)
        detail = {
            "sanctioned": sanctioned,
            "risk_score": None,
            "risk_level": "unknown",
            "entity": None,
            "category": None,
            "source": "ofac_sdn_xml",
        }

    # Write audit record
    try:
        db: Session = SessionLocal()
        log_entry = SanctionsLog(
            wallet_address=wallet_address,
            screened_at=datetime.now(timezone.utc),
            sanctioned=detail.get("sanctioned", False),
            risk_score=detail.get("risk_score"),
            risk_level=detail.get("risk_level"),
            entity_label=detail.get("entity"),
            source=detail.get("source", "unknown"),
            escrow_id=escrow_id,
            raw_response=json.dumps(detail),
        )
        db.add(log_entry)
        db.commit()
        db.close()
    except Exception as log_err:
        logger.warning(f"SanctionsLog write failed (non-fatal): {log_err}")

    return detail["sanctioned"], detail


# ---------------------------------------------------------------------------
# REGULATORY THRESHOLD CHECKS
# FATF Travel Rule: $1,000 USD — VASPs must exchange originator/beneficiary data
# FinCEN recordkeeping: $3,000 USD — threshold above which AgentTrust has no KYC
# ---------------------------------------------------------------------------
THRESHOLD_WARN_USD       = 1_000   # Travel Rule trigger — issue compliance_warning
THRESHOLD_BLOCK_USD      = 3_000   # Require KYC for non-verified wallets
THRESHOLD_BLOCK_KYC_USD  = 10_000  # Hard ceiling even for KYC-verified wallets

async def _get_xrp_price_usd() -> Optional[float]:
    """Return cached XRP/USD price, or None if unavailable."""
    import time as _time2
    global _xrp_price_cache, _xrp_price_cache_ts
    if _xrp_price_cache:
        return _xrp_price_cache.get("usd")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT")
            return float(r.json()["price"])
    except Exception:
        return None


async def check_value_threshold(amount_xrp: Optional[float], amount_rlusd: Optional[float], currency: str, buyer_address: Optional[str] = None, db=None) -> dict:
    """
    Check transaction value against regulatory thresholds.
    KYC-verified wallets may escrow up to THRESHOLD_BLOCK_KYC_USD; others are capped at THRESHOLD_BLOCK_USD.
    RLUSD is ~1 USD, so 1 RLUSD ≈ 1 USD for threshold purposes.
    """
    usd_value: Optional[float] = None

    if currency == "RLUSD" and amount_rlusd:
        usd_value = float(amount_rlusd)  # RLUSD ≈ 1 USD

    elif currency == "XRP" and amount_xrp:
        price = await _get_xrp_price_usd()
        if price:
            usd_value = float(amount_xrp) * price

    if usd_value is None:
        return {"ok": True}  # can't determine value — don't block

    # Check KYC status for the buyer wallet — AgentTrust cache first, then live Xaman query
    kyc_verified = False
    if buyer_address and db:
        try:
            kyc_row = db.query(KycRecord).filter(
                KycRecord.wallet_address == buyer_address,
                KycRecord.status == "verified"
            ).first()
            kyc_verified = kyc_row is not None
        except Exception:
            pass
        if not kyc_verified:
            try:
                xaman_ok = await _get_xaman_kyc(buyer_address)
                if xaman_ok:
                    kyc_verified = True
                    # Cache the Xaman result so future calls skip the HTTP round-trip
                    try:
                        db.add(KycRecord(
                            wallet_address=buyer_address,
                            status="verified",
                            verified_at=datetime.now(timezone.utc),
                            return_url=None,
                        ))
                        db.commit()
                    except Exception:
                        db.rollback()
            except Exception:
                pass

    effective_limit = THRESHOLD_BLOCK_KYC_USD if kyc_verified else THRESHOLD_BLOCK_USD

    if usd_value >= THRESHOLD_BLOCK_KYC_USD:
        return {
            "ok": False,
            "level": "block",
            "usd_value": round(usd_value, 2),
            "message": (
                f"This escrow value (~${usd_value:,.0f} USD) exceeds the maximum permitted transaction "
                f"size of ${THRESHOLD_BLOCK_KYC_USD:,} USD. Please contact hello@cryptovault.co.uk."
            ),
        }

    if usd_value >= THRESHOLD_BLOCK_USD and not kyc_verified:
        return {
            "ok": False,
            "level": "kyc_required",
            "usd_value": round(usd_value, 2),
            "error": "kyc_required",
            "kyc_url": "/kyc/verify",
            "message": (
                f"This escrow value (~${usd_value:,.0f} USD) exceeds the ${THRESHOLD_BLOCK_USD:,} USD "
                f"limit for unverified wallets. Complete identity verification to unlock escrows up to "
                f"${THRESHOLD_BLOCK_KYC_USD:,} USD."
            ),
        }
    if usd_value >= THRESHOLD_WARN_USD:
        return {
            "ok": True,
            "level": "warn",
            "usd_value": round(usd_value, 2),
            "compliance_warning": (
                f"This escrow (~${usd_value:,.0f} USD) meets or exceeds the FATF Travel Rule threshold "
                f"of $1,000 USD. In many jurisdictions, VASPs are required to exchange originator and "
                f"beneficiary identity data for transfers of this size. AgentTrust does not currently "
                f"implement the Travel Rule. Participants should ensure their own compliance with "
                f"applicable regulations. See cryptovault.co.uk/compliance for details."
            ),
        }
    return {"ok": True}


# ---------------------------------------------------------------------------
# In-memory store for on-chain verification challenges (TTL 30 min)
# ---------------------------------------------------------------------------
_verify_challenges: dict = {}  # wallet_address -> (challenge_str, expires_at)


_PUBLIC_ISSUERS = [
    {
        "wallet_address": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",
        "name": "Ripple Labs (RLUSD)",
        "category": "Stablecoin issuer",
        "description": "Issuer of RLUSD, a USD-backed stablecoin on the XRP Ledger. Launched December 2024.",
        "website": "ripple.com",
    },
    {
        "wallet_address": "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B",
        "name": "Bitstamp",
        "category": "Exchange / Gateway",
        "description": "One of the longest-standing XRPL gateways, issuing USD, EUR, BTC and ETH IOUs. Domain verified on XRPL.",
        "website": "bitstamp.net",
    },
    {
        "wallet_address": "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq",
        "name": "GateHub",
        "category": "Exchange / Gateway",
        "description": "Multi-currency XRPL gateway issuing USD, EUR, GBP, ETH and BTC IOUs. Publishes all wallet addresses at gatehub.net/legal/xrpl-addresses.",
        "website": "gatehub.net",
    },
    {
        "wallet_address": "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz",
        "name": "Sologenic",
        "category": "DeFi / Asset tokenisation",
        "description": "Real-world asset tokenisation platform on XRPL. Issues SOLO token and tokenised stocks. Domain verified on XRPL.",
        "website": "sologenic.com",
    },
    {
        "wallet_address": "rCSCManTZ8ME9EoLrSHHYKW8PPwWMgkwr",
        "name": "CasinoCoin Foundation",
        "category": "Regulated gaming",
        "description": "Digital currency for the regulated gaming industry. Listed as the canonical example in the XRPL Foundation XLS-26 xrp-ledger.toml standard.",
        "website": "casinocoin.im",
    },
    {
        "wallet_address": "rDBMvpjV6DoWvr3LqMUG8JBgd4QbBoU1E2",
        "name": "BPM Wallet (Twotixx)",
        "category": "NFT ticketing",
        "description": "XRPL-native NFT ticketing platform issuing event tickets to KYC'd wallets. XRPL Foundation Wave 4 Grant recipient. Combats touting and scalping.",
        "website": "missionbpm.com",
    },
    {
        "wallet_address": "rrno7Nj4RkFJLzC4nRaZiLF5aHwcTVon3d",
        "name": "onXRP",
        "category": "NFT marketplace / DeFi",
        "description": "Largest NFT marketplace and launchpad on XRPL, plus a DEX, Play2Win game and fiat on-ramp. Issues OXP token.",
        "website": "onxrp.com",
    },
]


def _seed_public_issuers():
    """Insert known public XRPL organisations using raw SQL to avoid ORM schema mismatches."""
    db = SessionLocal()
    try:
        inserted = 0
        for entry in _PUBLIC_ISSUERS:
            exists = db.execute(
                text("SELECT id FROM nft_issuer WHERE wallet_address = :w"),
                {"w": entry["wallet_address"]}
            ).fetchone()
            if exists:
                continue
            db.execute(text("""
                INSERT INTO nft_issuer
                    (wallet_address, name, category, description, website, verified, created_at)
                VALUES
                    (:wallet_address, :name, :category, :description, :website, :verified, :created_at)
            """), {
                "wallet_address": entry["wallet_address"],
                "name":           entry["name"],
                "category":       entry["category"],
                "description":    entry["description"],
                "website":        entry["website"],
                "verified":       "public",
                "created_at":     datetime.now(timezone.utc).isoformat(),
            })
            inserted += 1
        db.commit()
        logger.info(f"Public issuer seed complete — {inserted} inserted.")
    except Exception as e:
        logger.warning(f"Issuer seed failed: {e}")
        db.rollback()
    finally:
        db.close()


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
        # v13 — invoice requirements
        "ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS invoice_requirements  TEXT",
        # v14 — KYC verification
        """CREATE TABLE IF NOT EXISTS kyc_record (
            id                SERIAL PRIMARY KEY,
            wallet_address    VARCHAR NOT NULL,
            stripe_session_id VARCHAR UNIQUE,
            stripe_vs_id      VARCHAR,
            status            VARCHAR DEFAULT 'pending',
            created_at        TIMESTAMP,
            verified_at       TIMESTAMP,
            return_url        VARCHAR
        )""",
        "CREATE INDEX IF NOT EXISTS kyc_record_wallet_idx ON kyc_record (wallet_address)",
        # trusted issuer registry extended fields
        "ALTER TABLE nft_issuer ADD COLUMN IF NOT EXISTS contact_email VARCHAR",
        "ALTER TABLE nft_issuer ADD COLUMN IF NOT EXISTS lei            VARCHAR",
        "ALTER TABLE nft_issuer ADD COLUMN IF NOT EXISTS nft_types      VARCHAR",
        # v12 — wallet reputation ratings
        """CREATE TABLE IF NOT EXISTS wallet_rating (
            id             SERIAL PRIMARY KEY,
            rated_address  VARCHAR NOT NULL,
            rater_address  VARCHAR NOT NULL,
            escrow_id      VARCHAR NOT NULL,
            rating         INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            comment        TEXT,
            rater_role     VARCHAR NOT NULL,
            created_at     TIMESTAMP
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS wallet_rating_unique ON wallet_rating (escrow_id, rater_address)",
        """CREATE TABLE IF NOT EXISTS free_audit_usage (
            id             SERIAL PRIMARY KEY,
            wallet_address VARCHAR NOT NULL,
            escrow_id      VARCHAR NOT NULL,
            resource       VARCHAR,
            timestamp      TIMESTAMP
        )""",
        "ALTER TABLE job_posting ADD COLUMN IF NOT EXISTS claimable BOOLEAN DEFAULT FALSE",
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
BITHOMP_API_KEY = os.getenv("BITHOMP_API_KEY")  # optional — enables Bithomp domain verification
PROTOCOL_WALLET    = "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR"
MIN_FEE_XRP        = 0.1

# Base chain USDC payment constants
BASE_WALLET_ADDRESS = os.getenv("BASE_WALLET_ADDRESS", "")        # our receiving address on Base
BASE_RPC_URL        = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
USDC_CONTRACT_BASE  = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base mainnet
MIN_FEE_USDC        = 0.10   # USD — 100000 in USDC's 6-decimal units

# Temporary reviewer bypass for directory submissions (e.g. Claude Connectors Directory).
# Lets a reviewer exercise paid endpoints without a funded XRPL wallet. Set
# REVIEWER_BYPASS_TOKEN in the environment only for the duration of a review,
# then unset it — this must NOT be left enabled in production.
REVIEWER_BYPASS_TOKEN = os.getenv("REVIEWER_BYPASS_TOKEN")
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
# x402 v2 / XRPL "exact" scheme support (https://xrpl-x402.t54.ai/docs).
# Lets agents pay via a presigned, unsubmitted XRPL Payment transaction
# (PAYMENT-SIGNATURE header) instead of submitting it themselves and
# proving it after the fact. AgentTrust acts as its own facilitator: it
# decodes, validates, and submits the signed blob itself, since it's
# already the resource server and already holds a submission pipeline.
XRPL_CAIP2_NETWORK    = "xrpl:0"  # mainnet, per x402 CAIP-2 convention
X402_DEFAULT_SOURCE_TAG = 804681468
X402_CHALLENGE_TTL_SECONDS = 600

_x402_challenges: dict = {}  # invoice_id -> {resource, payTo, asset, issuer, currency, amount, created_at}


def _x402_v2_envelope(resource: str, required_xrp: float) -> tuple[dict, str]:
    """Builds a v2 'accepts' entry + invoice id, and registers the challenge.

    Protocol fees are always charged in XRP today; the IOU/RLUSD branch in
    verify_x402_v2_payment exists for forward-compat with RLUSD-denominated
    fees but is not yet reachable from here.
    """
    invoice_id = secrets.token_hex(8)
    amount_drops = str(int(round(required_xrp * 1_000_000)))
    _x402_challenges[invoice_id] = {
        "resource":     resource,
        "payTo":        PROTOCOL_WALLET,
        "asset":        "XRP",
        "issuer":       None,
        "currency":     None,
        "amount_drops": amount_drops,
        "amount_value": None,
        "created_at":   time.time(),
    }
    entry = {
        "scheme":  "exact",
        "network": XRPL_CAIP2_NETWORK,
        "asset":   "XRP",
        "payTo":   PROTOCOL_WALLET,
        "amount":  amount_drops,
        "maxTimeoutSeconds": X402_CHALLENGE_TTL_SECONDS,
        "extra": {
            "sourceTag": X402_DEFAULT_SOURCE_TAG,
            "invoiceId": invoice_id,
        },
    }
    return entry, invoice_id


def _raise_402(resource: str, error: str, min_xrp: float = None) -> None:
    """Raise an x402-compliant 402 Payment Required exception.

    Emits both the legacy v1 X-Payment-Required header (tx-hash-after-the-fact
    flow, used by the Xaman/human checkout) and a proper x402 v2
    PAYMENT-REQUIRED header (presigned-transaction flow, for agents using
    the official x402-XRPL client libraries).

    Spec: https://x402.org, https://xrpl-x402.t54.ai/docs
    """
    required_xrp = min_xrp if min_xrp is not None else MIN_FEE_XRP

    v2_entry, invoice_id = _x402_v2_envelope(resource, required_xrp)

    usdc_accepts = []
    if BASE_WALLET_ADDRESS:
        usdc_accepts = [{
            "scheme":   "exact",
            "network":  "eip155:8453",
            "asset":    "USDC",
            "payTo":    BASE_WALLET_ADDRESS,
            "amount":   str(int(MIN_FEE_USDC * 1_000_000)),  # 6 decimals
            "resource": resource,
            "maxTimeoutSeconds": 300,
            "extra": {
                "contractAddress": USDC_CONTRACT_BASE,
                "instruction": (
                    f"Send ${MIN_FEE_USDC:.2f} USDC to {BASE_WALLET_ADDRESS} on Base (chain 8453), "
                    "then include the transaction hash as the X-PAYMENT header."
                ),
                "headerName": "X-PAYMENT",
            },
        }]

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
                    "then include the transaction hash as the X-PAYMENT header. "
                    "Agents using the x402-XRPL presigned-transaction flow should "
                    "instead use the PAYMENT-REQUIRED header below."
                ),
                "headerName": "X-PAYMENT",
            },
        }] + usdc_accepts,
        "error": error,
    }
    v2_body = {"x402Version": 2, "accepts": [v2_entry] + usdc_accepts}

    encoded    = base64.b64encode(json.dumps(body).encode()).decode()
    encoded_v2 = base64.b64encode(json.dumps(v2_body).encode()).decode()
    raise PaymentRequired(JSONResponse(
        status_code=402,
        content=body,
        headers={"X-Payment-Required": encoded, "PAYMENT-REQUIRED": encoded_v2},
    ))


def _x402_invoice_binding_ok(tx: dict, invoice_id: str) -> bool:
    expected_hex = hashlib.sha256(invoice_id.encode()).hexdigest().upper()
    if str(tx.get("InvoiceID", "")).upper() == expected_hex:
        return True
    expected_memo = invoice_id.encode("utf-8").hex().upper()
    for memo in (tx.get("Memos") or []):
        if str(memo.get("Memo", {}).get("MemoData", "")).upper() == expected_memo:
            return True
    return False


async def verify_x402_v2_payment(payment_signature_b64: str, escrow_id: str, db: Session, resource: str) -> dict:
    """Verifies and settles a presigned XRPL Payment per the x402 v2 'exact' scheme."""
    try:
        envelope = json.loads(base64.b64decode(payment_signature_b64).decode())
        accepted = envelope["accepted"]
        signed_blob = envelope["payload"]["signedTxBlob"]
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_tx_blob: malformed PAYMENT-SIGNATURE payload.")

    if envelope.get("x402Version") != 2 or accepted.get("scheme") != "exact":
        raise HTTPException(status_code=400, detail="payment_requirements_mismatch: expected x402Version 2, scheme 'exact'.")
    if accepted.get("network") != XRPL_CAIP2_NETWORK:
        raise HTTPException(status_code=400, detail=f"invalid_network: expected {XRPL_CAIP2_NETWORK}.")

    invoice_id = (accepted.get("extra") or {}).get("invoiceId")
    challenge  = _x402_challenges.get(invoice_id)
    if not challenge or (time.time() - challenge["created_at"]) > X402_CHALLENGE_TTL_SECONDS:
        _raise_402(resource, "Payment challenge expired or unknown invoiceId. Request a new PAYMENT-REQUIRED challenge.")

    try:
        tx = xrpl_decode_tx_blob(signed_blob)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_tx_blob: could not decode signedTxBlob.")

    if tx.get("TransactionType") != "Payment":
        raise HTTPException(status_code=400, detail="not_payment_tx")
    if str(tx.get("Destination", "")).strip().lower() != challenge["payTo"].lower():
        raise HTTPException(status_code=400, detail="destination_mismatch")
    if not tx.get("LastLedgerSequence"):
        raise HTTPException(status_code=400, detail="missing_last_ledger_sequence")
    if not _x402_invoice_binding_ok(tx, invoice_id):
        raise HTTPException(status_code=400, detail="invoice_binding_missing")

    tx_amount = tx.get("Amount")
    if challenge["asset"] == "XRP":
        if isinstance(tx_amount, dict) or int(tx_amount or 0) < int(challenge["amount_drops"]):
            raise HTTPException(status_code=400, detail="amount_mismatch")
        amount_xrp = round(int(tx_amount) / 1_000_000, 6)
    else:
        if not isinstance(tx_amount, dict):
            raise HTTPException(status_code=400, detail="amount_mismatch")
        currency_ok = str(tx_amount.get("currency", "")).upper() == str(challenge["currency"] or "").upper()
        issuer_ok    = tx_amount.get("issuer") == challenge["issuer"]
        try:
            value_ok = Decimal(str(tx_amount.get("value", "0"))) >= Decimal(str(challenge["amount_value"]))
        except (InvalidOperation, TypeError):
            value_ok = False
        if not (currency_ok and issuer_ok and value_ok):
            raise HTTPException(status_code=400, detail="amount_mismatch")
        amount_xrp = None

    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(XRPL_URL, json={"method": "submit", "params": [{"tx_blob": signed_blob}]})
    result = res.json().get("result", {})
    engine_result = result.get("engine_result", "")
    tx_hash = (result.get("tx_json") or {}).get("hash") or result.get("hash")

    if engine_result != "tesSUCCESS" or not tx_hash:
        raise HTTPException(status_code=400, detail=f"Settlement failed: {engine_result or 'unknown error'}.")

    del _x402_challenges[invoice_id]  # consume — single use, per spec

    sender = tx.get("Account", "unknown")
    db.add(PaymentLog(payment_hash=tx_hash, purpose="x402_v2", sender=sender, amount_xrp=amount_xrp, escrow_id=escrow_id))
    db.commit()

    logger.info(f"✅ x402 v2 SETTLED: {tx_hash} from {sender} for escrow '{escrow_id}'")

    settlement = {"success": True, "transaction": tx_hash, "network": XRPL_CAIP2_NETWORK, "payer": sender}
    return {
        "sender": sender,
        "amount_xrp": amount_xrp,
        "tx_hash": tx_hash,
        "payment_response_header": base64.b64encode(json.dumps(settlement).encode()).decode(),
    }


# ---------------------------------------------------------------------------
# x402 — Base chain USDC payment verification
# ---------------------------------------------------------------------------
# Verifies a USDC transfer on Base mainnet via JSON-RPC eth_getTransactionReceipt.
# We check the ERC-20 Transfer event log: from=payer, to=our wallet, token=USDC,
# amount>=MIN_FEE_USDC. No API key required — uses the public Base RPC.
#
# USDC Transfer event topic:
#   Transfer(address indexed from, address indexed to, uint256 value)
#   keccak256("Transfer(address,address,uint256)")
_USDC_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _pad_address(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").zfill(64)


async def verify_usdc_base_payment(tx_hash: str, escrow_id: str, db: Session, resource: str) -> dict:
    """Verify a USDC payment on Base mainnet and record it in PaymentLog."""
    if not BASE_WALLET_ADDRESS:
        raise HTTPException(status_code=503, detail="USDC payments on Base are not yet configured on this server.")

    already_used = db.query(PaymentLog).filter(PaymentLog.payment_hash == tx_hash).first()
    if already_used:
        raise HTTPException(
            status_code=403,
            detail=f"Payment hash already used for '{already_used.escrow_id}' on {already_used.timestamp.strftime('%Y-%m-%d %H:%M UTC')}.",
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(BASE_RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_getTransactionReceipt",
                "params": [tx_hash],
            })
        receipt = resp.json().get("result")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Base RPC lookup failed: {e}")

    if not receipt:
        raise HTTPException(status_code=400, detail="Transaction not found on Base — it may still be pending. Wait for confirmation and retry.")

    if receipt.get("status") != "0x1":
        raise HTTPException(status_code=400, detail="Transaction failed on Base (status=0x0).")

    our_address_padded = _pad_address(BASE_WALLET_ADDRESS)
    usdc_contract      = USDC_CONTRACT_BASE.lower()
    min_units          = int(MIN_FEE_USDC * 1_000_000)  # USDC has 6 decimals

    transfer_found = False
    payer          = None
    usdc_amount    = 0.0

    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != usdc_contract:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != _USDC_TRANSFER_TOPIC:
            continue
        if topics[2].lower() != our_address_padded:
            continue
        raw_value = int(log.get("data", "0x0"), 16)
        if raw_value < min_units:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient USDC. Required ≥${MIN_FEE_USDC:.2f} ({min_units} units), received {raw_value} units.",
            )
        payer          = "0x" + topics[1][-40:]
        usdc_amount    = raw_value / 1_000_000
        transfer_found = True
        break

    if not transfer_found:
        raise HTTPException(
            status_code=400,
            detail=f"No USDC Transfer to {BASE_WALLET_ADDRESS} found in this transaction. Ensure you sent USDC (not ETH) to the correct address on Base.",
        )

    db.add(PaymentLog(
        payment_hash=tx_hash,
        purpose="usdc_base",
        sender=payer,
        amount_xrp=None,
        escrow_id=escrow_id,
    ))
    db.commit()

    logger.info(f"✅ USDC BASE: ${usdc_amount:.4f} from {payer} for '{escrow_id}' tx={tx_hash[:16]}…")

    import asyncio as _asyncio2
    _asyncio2.create_task(_telegram_notify(
        f"💵 *USDC payment received (Base)*\n"
        f"Amount: `${usdc_amount:.4f} USDC`\n"
        f"From: `{payer}`\n"
        f"Escrow: `{escrow_id}`\n"
        f"Resource: `{resource}`\n"
        f"Tx: `{tx_hash[:20]}…`"
    ))

    settlement = {"success": True, "transaction": tx_hash, "network": "eip155:8453", "payer": payer}
    return {
        "sender":                  payer,
        "amount_usdc":             usdc_amount,
        "tx_hash":                 tx_hash,
        "payment_response_header": base64.b64encode(json.dumps(settlement).encode()).decode(),
    }


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
    # Invoice requirements — buyer can require seller to submit a matching invoice
    invoice_requirements: Optional[dict] = None
    # Expected fields: po_number, supplier_name, services_description,
    # require_date (bool), require_line_items (bool)
    # Amount/currency are always required when this is set (mirrored from escrow amount)

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
async def verify_fee_payment(fee_hash: str, escrow_id: str, db: Session, min_xrp: float = None, resource: str = "/", reviewer_token: str = None, payment_signature: str = None) -> dict:
    required_xrp = min_xrp if min_xrp is not None else MIN_FEE_XRP

    if REVIEWER_BYPASS_TOKEN and REVIEWER_BYPASS_TOKEN in (reviewer_token, fee_hash):
        logger.warning(f"⚠️ REVIEWER BYPASS used for {resource} (escrow_id={escrow_id}) — fee check skipped.")
        return {"bypassed": True, "sender": "reviewer-bypass", "amount_xrp": required_xrp}

    if payment_signature:
        return await verify_x402_v2_payment(payment_signature, escrow_id, db, resource)

    if not fee_hash:
        # ── Free tier: grant up to FREE_AUDIT_LIMIT audits for established wallets ──
        # Extract buyer_address from escrow record if available
        free_wallet = None
        try:
            vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
            if vault and vault.buyer_address:
                free_wallet = vault.buyer_address
        except Exception:
            pass

        if free_wallet:
            used = db.query(FreeAuditUsage).filter(FreeAuditUsage.wallet_address == free_wallet).count()
            if used < FREE_AUDIT_LIMIT:
                # Fetch trust score to gate on wallet quality
                try:
                    score_data = await compute_xrpl_trust_score(free_wallet, db)
                    score = score_data.get("score", 0)
                except Exception:
                    score = 0

                if score >= FREE_AUDIT_MIN_SCORE:
                    db.add(FreeAuditUsage(wallet_address=free_wallet, escrow_id=escrow_id, resource=resource))
                    db.commit()
                    remaining = FREE_AUDIT_LIMIT - used - 1
                    logger.info(f"🎁 FREE AUDIT granted: wallet={free_wallet} score={score} used={used+1}/{FREE_AUDIT_LIMIT}")
                    return {
                        "free_tier": True,
                        "audits_remaining": remaining,
                        "sender": free_wallet,
                        "amount_xrp": 0,
                        "message": f"Free audit applied ({used + 1}/{FREE_AUDIT_LIMIT}). {remaining} free audit{'s' if remaining != 1 else ''} remaining for this wallet.",
                    }

        _raise_402(resource, "Payment required. Provide a fee_hash (x402 v1) or PAYMENT-SIGNATURE header (x402 v2).", min_xrp=required_xrp)

    # Route EVM transaction hashes (0x-prefixed, 66 chars) to Base USDC verification
    if fee_hash.startswith("0x") and len(fee_hash) == 66:
        return await verify_usdc_base_payment(fee_hash, escrow_id, db, resource)

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
    worker_email:         str,
    worker_name:          str,
    escrow_id:            str,
    buyer_name:           str,
    amount:               float,
    currency:             str,
    task_preview:         str,
    deadline:             str,
    invoice_requirements: Optional[dict] = None,
):
    if not RESEND_API_KEY or not worker_email:
        return
    worker_url   = f"{SITE_URL}?worker={escrow_id}"
    preview_safe = task_preview[:300] + ("…" if len(task_preview) > 300 else "")
    amount_str   = f"{amount} {currency}"

    # Build invoice requirements block if present
    inv_block = ""
    if invoice_requirements:
        ir = invoice_requirements
        rows = []
        if ir.get("po_number"):
            rows.append(f"<tr><td>PO / Reference</td><td><strong>{ir['po_number']}</strong></td></tr>")
        if ir.get("supplier_name"):
            rows.append(f"<tr><td>Supplier name</td><td><strong>{ir['supplier_name']}</strong></td></tr>")
        rows.append(f"<tr><td>Amount</td><td><strong>{amount_str}</strong></td></tr>")
        if ir.get("services_description"):
            rows.append(f"<tr><td>Services / goods</td><td><strong>{ir['services_description']}</strong></td></tr>")
        rows.append(f"<tr><td>Invoice number</td><td><strong>Required</strong></td></tr>")
        rows.append(f"<tr><td>Invoice date</td><td><strong>{'Required' if ir.get('require_date') else 'Not required'}</strong></td></tr>")
        rows.append(f"<tr><td>Line items</td><td><strong>{'Itemised breakdown required' if ir.get('require_line_items') else 'Summary total is fine'}</strong></td></tr>")
        inv_block = f"""
  <p style="font-size:.85rem;font-weight:700;margin:20px 0 .4rem;color:#0d0d12;">Invoice required — your invoice must include:</p>
  <table style="width:100%;border-collapse:collapse;font-size:.85rem;margin-bottom:20px;">
    <colgroup><col style="width:40%"><col style="width:60%"></colgroup>
    {''.join(f'<tr style="border-bottom:1px solid #eef0f6;">{r[4:]}'
             .replace('<tr style="border-bottom:1px solid #eef0f6;"><td>', '<tr style="border-bottom:1px solid #eef0f6;"><td style="padding:7px 4px;color:#666;">')
             .replace('</td><td>', '</td><td style="padding:7px 4px;">')
             for r in rows)}
  </table>
  <p style="font-size:.82rem;color:#666;margin-bottom:20px;">
    Submit your proof of work <em>and</em> include an invoice matching every field above.
    The AI referee verifies both before releasing payment.
  </p>"""

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
  {inv_block}
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
# Primary: Bithomp verified-domain API (daily-updated, thousands of verified entries).
# Fallback: our own live TOML check (used when BITHOMP_API_KEY is not set, or Bithomp
#           returns unverified for a domain that may have been verified very recently).
# ---------------------------------------------------------------------------
async def _verify_domain_via_bithomp(wallet_address: str, expected_domain: str = None) -> dict | None:
    """Query Bithomp API for domain verification. Returns result dict or None if unavailable."""
    if not BITHOMP_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(
                f"https://bithomp.com/api/v2/address/{wallet_address}",
                params={"verifiedDomain": "true"},
                headers={"x-bithomp-token": BITHOMP_API_KEY},
            )
        if res.status_code != 200:
            return None
        data = res.json()
        service = data.get("service") or {}
        domain = service.get("domain") or data.get("verifiedDomain")
        verified = bool(domain)
        if not verified:
            return {"verified": False, "detail": f"Wallet {wallet_address} has no verified domain on Bithomp.", "source": "bithomp"}
        if expected_domain and domain.lower() != expected_domain.lower():
            return {"verified": False, "detail": f"Bithomp shows domain '{domain}', expected '{expected_domain}'.", "source": "bithomp"}
        return {"verified": True, "detail": f"Domain verified via Bithomp: {wallet_address} ↔ {domain}", "domain": domain, "source": "bithomp"}
    except Exception:
        return None


async def verify_domain_ownership(wallet_address: str, expected_domain: str = None) -> dict:
    # Try Bithomp first
    bithomp_result = await _verify_domain_via_bithomp(wallet_address, expected_domain)
    if bithomp_result is not None:
        return bithomp_result

    # Fallback: live TOML check
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



@app.get("/domain/preview")
async def domain_preview(domain: str, db: Session = Depends(get_db)):
    """
    Buyer-facing domain preview: given a domain name, return what we know about it.
    1. Check our issuer registry.
    2. Fetch xrp-ledger.toml from the domain (server-side — no CORS constraints).
    3. For each wallet listed in the TOML, check Bithomp for verified-domain confirmation.
    Returns enough for the UI to show a meaningful status without a wallet address.
    """
    import re
    clean = domain.replace("https://", "").replace("http://", "").rstrip("/").lower()

    # 1. Registry lookup
    try:
        row = db.execute(text(
            "SELECT name, wallet_address, verified FROM nft_issuer WHERE LOWER(website) LIKE :d LIMIT 1"
        ), {"d": f"%{clean}%"}).fetchone()
    except Exception:
        row = None
    if row and row[2] == "verified":
        return {"status": "registry_verified", "name": row[0], "domain": clean,
                "detail": f"{row[0]} is a verified issuer in the AgentTrust registry."}
    if row:
        return {"status": "registry_listed", "name": row[0], "domain": clean,
                "detail": f"{row[0]} is listed in the AgentTrust registry (pending verification)."}

    # 2. Fetch xrp-ledger.toml from the domain (try https then http)
    # Note: Bithomp /api/v2/services (reverse domain→wallet lookup) requires a higher plan tier.
    # We can only verify individual wallets via Bithomp, so we need the TOML to get the wallet first.
    toml_content = None
    toml_status = None
    toml_error = None
    for scheme in ("https", "http"):
        toml_url = f"{scheme}://{clean}/.well-known/xrp-ledger.toml"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                         headers={"User-Agent": "AgentTrust/1.0"}) as client:
                r = await client.get(toml_url)
            toml_status = r.status_code
            if r.status_code == 200 and r.text.strip():
                toml_content = r.text
                break
        except Exception as e:
            toml_error = str(e)

    if not toml_content:
        return {"status": "unknown", "domain": clean, "toml_status": toml_status, "toml_error": toml_error,
                "detail": f"No xrp-ledger.toml found at {clean}. The seller must publish one listing their wallet address."}

    # 3. Extract wallet addresses from the TOML (look for r... addresses)
    wallets = list(dict.fromkeys(re.findall(r"\br[1-9A-HJ-NP-Za-km-z]{24,33}\b", toml_content)))[:5]
    if not wallets:
        return {"status": "toml_found", "domain": clean,
                "detail": f"xrp-ledger.toml exists at {clean} but lists no wallet addresses yet."}

    # 4. Check Bithomp for each wallet to confirm domain linkage
    bithomp_verified_wallet = None
    for w in wallets:
        result = await _verify_domain_via_bithomp(w, clean)
        if result and result.get("verified"):
            bithomp_verified_wallet = w
            break

    if bithomp_verified_wallet:
        return {"status": "bithomp_verified", "domain": clean, "wallet": bithomp_verified_wallet,
                "detail": f"{clean} is verified on Bithomp — wallet {bithomp_verified_wallet[:8]}… is confirmed as the domain owner."}

    return {"status": "toml_found", "domain": clean, "wallets": wallets,
            "detail": f"xrp-ledger.toml found at {clean} listing {len(wallets)} wallet(s). Bithomp verification not confirmed — seller's wallet must have its XRPL Domain field set to this domain."}


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
async def _fetch_nft_count(wallet_address: str) -> int:
    """Fetch NFT count from XRPL for use in trust score."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(XRPL_URL, json={
                "method": "account_nfts",
                "params": [{"account": wallet_address, "limit": 10}]
            })
            return len(r.json().get("result", {}).get("account_nfts", []))
    except Exception:
        return 0


async def compute_xrpl_trust_score(wallet_address: str, db: Session = None) -> dict:
    """
    AgentTrust Wallet Trust Score — 11 signals, genuine max 100.

    Signal breakdown:
      Account age              — up to 20 pts (2 pts/month, max 10 months)
      XRP balance              — up to 15 pts (3 pts per 10 XRP, max 50 XRP)
      On-chain activity        — up to 15 pts (owner count as activity proxy)
      Domain verified          — 10 pts flat
      NFTs held                — up to 5 pts
      AgentTrust escrow rate   — up to 20 pts (our own platform data)
      Peer ratings             — up to 15 pts (avg star rating from counterparties)
      Wallet ownership proof   — 8 pts (on-chain AccountSet verification)
      Sanctions clear          — 7 pts; sanctioned wallets score 0 and are blocked from escrow
      Entity reputation        — up to 8 pts (XRPScan: verified entity +5, security flags +1 each)
      Xaman KYC                — 5 pts (Xaman KYC via Veriff, authenticated platform API; human-only signal)
    """
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

        current_ledger = info.get("ledger_current_index", 90_000_000)
        balance_xrp = int(acct.get("Balance", 0)) / 1_000_000
        has_domain  = bool(acct.get("Domain"))
        owner_count = acct.get("OwnerCount", 0)

        # Account age via first transaction's date field (XRPL epoch = seconds since
        # Jan 1 2000). Using ledger arithmetic was unreliable because account_info with
        # ledger_index="validated" returns ledger_index not ledger_current_index, so the
        # current_ledger defaulted to 90_000_000 — only 74k ledgers above the RLUSD wallet.
        # xrplcluster.com supports forward=True and returns the correct first tx.
        from datetime import datetime, timezone
        XRPL_EPOCH = 946684800  # Jan 1 2000 in Unix seconds
        age_days = 0
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                tx_res = await client.post(XRPL_URL, json={
                    "method": "account_tx",
                    "params": [{"account": wallet_address, "limit": 1, "forward": True,
                                "ledger_index_min": 0, "ledger_index_max": -1}]
                })
            txs = tx_res.json().get("result", {}).get("transactions", [])
            if txs:
                tx_entry = txs[0]
                # date is XRPL epoch seconds; present in tx or tx_json depending on node
                xrpl_date = (
                    tx_entry.get("tx", {}).get("date")
                    or tx_entry.get("tx_json", {}).get("date")
                    or tx_entry.get("date")
                )
                if xrpl_date:
                    creation_dt = datetime.fromtimestamp(xrpl_date + XRPL_EPOCH, tz=timezone.utc)
                    age_days = (datetime.now(timezone.utc) - creation_dt).days
        except Exception:
            pass

        # Fetch NFTs, XRPScan entity data, and Xaman KYC concurrently
        import asyncio as _asyncio
        nft_task      = _asyncio.create_task(_fetch_nft_count(wallet_address))
        xrpscan_task  = _asyncio.create_task(_get_xrpscan_account(wallet_address))
        xaman_task    = _asyncio.create_task(_get_xaman_kyc(wallet_address))
        nft_count, xrpscan, xaman_kyc = await _asyncio.gather(nft_task, xrpscan_task, xaman_task)

        # XRPScan entity + security flags signal
        xrpscan_score   = 0
        xrpscan_entity  = None
        xrpscan_flags   = {}
        if xrpscan:
            xrpscan_entity = xrpscan.get("entity_name")
            # Verified known entity (exchange, market maker etc.) — 5 pts
            if xrpscan.get("entity_verified"):
                xrpscan_score += 5
            # Account security hardening — up to 3 pts
            if xrpscan.get("master_key_disabled"):
                xrpscan_score += 1
            if xrpscan.get("require_dest_tag"):
                xrpscan_score += 1
            if xrpscan.get("deposit_auth"):
                xrpscan_score += 1
            xrpscan_flags = {
                "master_key_disabled": xrpscan.get("master_key_disabled", False),
                "require_dest_tag":    xrpscan.get("require_dest_tag", False),
                "deposit_auth":        xrpscan.get("deposit_auth", False),
            }

        # Xaman KYC signal — 5 pts if wallet holder has completed Xaman identity verification
        # AI agent wallets will never have this; it distinguishes verified humans from agents
        xaman_kyc_score = 5 if xaman_kyc else 0

        # AgentTrust KYC signal — 10 pts if operator has completed AgentTrust identity verification
        agentrust_kyc_verified = False
        agentrust_kyc_score = 0
        if db:
            try:
                kyc_row = db.query(KycRecord).filter(
                    KycRecord.wallet_address == wallet_address,
                    KycRecord.status == "verified"
                ).first()
                if kyc_row:
                    agentrust_kyc_verified = True
                    agentrust_kyc_score = 10
            except Exception:
                pass

        # On-chain signals
        age_score      = min(20, int(age_days / 30) * 2)       # 2 pts/month, max 20
        balance_score  = min(15, int(balance_xrp / 10) * 3)    # 3 pts per 10 XRP, max 15
        activity_score = min(15, int(owner_count / 5) * 2)     # 2 pts per 5 items, max 15
        domain_score   = 10 if has_domain else 0
        nft_score      = min(5, nft_count)                      # 1 pt per NFT, max 5

        # AgentTrust platform signals (from our DB)
        completion_score = 0
        peer_score = 0
        completion_stats = {}
        peer_stats = {}

        if db:
            try:
                # Escrow completion history
                total_escrows = db.execute(text(
                    "SELECT COUNT(*) FROM escrow_vault WHERE worker_address = :w"
                ), {"w": wallet_address}).scalar() or 0
                passed_escrows = db.execute(text(
                    "SELECT COUNT(*) FROM escrow_vault WHERE worker_address = :w AND status = 'RELEASED'"
                ), {"w": wallet_address}).scalar() or 0
                if total_escrows > 0:
                    pass_rate = passed_escrows / total_escrows
                    completion_score = min(20, int(pass_rate * 15) + min(5, total_escrows))
                completion_stats = {"total_escrows": total_escrows, "passed_escrows": passed_escrows}

                # Peer ratings
                rating_row = db.execute(text(
                    "SELECT COUNT(*), AVG(rating) FROM wallet_rating WHERE rated_address = :w"
                ), {"w": wallet_address}).fetchone()
                rating_count = rating_row[0] or 0
                avg_rating = float(rating_row[1] or 0)
                if rating_count > 0:
                    peer_score = min(15, int((avg_rating / 5) * 12) + min(3, rating_count // 3))
                peer_stats = {"rating_count": rating_count, "avg_rating": round(avg_rating, 2)}
            except Exception:
                pass

        # Wallet ownership verification (on-chain AccountSet proof)
        wallet_sig_score = 0
        wallet_sig_verified = False
        if db:
            try:
                vrow = db.query(WalletVerification).filter(
                    WalletVerification.wallet_address == wallet_address
                ).first()
                if vrow:
                    wallet_sig_verified = True
                    wallet_sig_score = 8
            except Exception:
                pass

        # Multi-jurisdiction sanctions check — BEI primary, OFAC XML fallback
        # Hard zero if sanctioned; +7 pts if clean
        sanctions_clear = None
        sanctions_score = 0
        sanctions_detail = {}
        try:
            sanctioned, sanctions_detail = await is_wallet_sanctioned(wallet_address)
            sanctions_clear = not sanctioned
            if sanctioned:
                return {
                    "score": 0,
                    "detail": f"Wallet address flagged on sanctions list (source: {sanctions_detail.get('source','unknown')}). Score withheld.",
                    "signals": {"sanctions_clear": False, "sanctions_detail": sanctions_detail},
                    "verified_issuer": None,
                }
            sanctions_score = 7
        except Exception:
            pass  # screening unavailable — skip gracefully

        # Verified issuer badge — check our own registry first, then Bithomp
        verified_issuer = None
        if db:
            try:
                row = db.execute(text(
                    "SELECT name, website, verified FROM nft_issuer WHERE wallet_address = :w LIMIT 1"
                ), {"w": wallet_address}).fetchone()
                if row and row[2] == "verified":
                    verified_issuer = {"name": row[0], "domain": row[1], "source": "agenttrust"}
            except Exception:
                pass
        if not verified_issuer:
            bithomp = await _verify_domain_via_bithomp(wallet_address)
            if bithomp and bithomp.get("verified"):
                verified_issuer = {
                    "name": None,
                    "domain": bithomp.get("domain"),
                    "source": "bithomp",
                }

        total = age_score + balance_score + activity_score + domain_score + nft_score + completion_score + peer_score + wallet_sig_score + sanctions_score + xrpscan_score + xaman_kyc_score + agentrust_kyc_score

        signals = {
            "age_days":              age_days,
            "balance_xrp":           round(balance_xrp, 2),
            "owner_count":           owner_count,
            "has_domain":            has_domain,
            "nft_count":             nft_count,
            "wallet_sig_verified":   wallet_sig_verified,
            "sanctions_clear":       sanctions_clear,
            "risk_score":            sanctions_detail.get("risk_score") if sanctions_detail else None,
            "risk_level":            sanctions_detail.get("risk_level") if sanctions_detail else None,
            "known_entity":          xrpscan_entity or (sanctions_detail.get("entity") if sanctions_detail else None),
            "xrpscan_entity":        xrpscan_entity,
            "xrpscan_flags":         xrpscan_flags,
            "xaman_kyc":             xaman_kyc,
            "kyc_verified":          agentrust_kyc_verified,
            "score_breakdown": {
                "account_age":            age_score,
                "balance":                balance_score,
                "on_chain_activity":      activity_score,
                "domain_verified":        domain_score,
                "nfts_held":              nft_score,
                "escrow_completion":      completion_score,
                "peer_rating":            peer_score,
                "wallet_sig_verified":    wallet_sig_score,
                "sanctions_clear":        sanctions_score,
                "entity_reputation":      xrpscan_score,
                "xaman_kyc":              xaman_kyc_score,
                "agentrust_kyc":          agentrust_kyc_score,
            },
            **completion_stats,
            **peer_stats,
        }

        return {
            "score": min(100, total),
            "detail": f"AgentTrust wallet trust score: {min(100,total)}/100",
            "signals": signals,
            "verified_issuer": verified_issuer,
        }
    except Exception as e:
        return {"score": 0, "detail": f"Could not compute score: {e}", "signals": {}}


async def _score_bid_wallet(bid_id: str, wallet_address: str, session_factory):
    db = session_factory()
    try:
        result = await compute_xrpl_trust_score(wallet_address, db=db)
        bid = db.query(Bid).filter(Bid.id == bid_id).first()
        if bid:
            bid.xrpl_trust_score = result.get("score", 0)
            db.commit()
    finally:
        db.close()


@app.get("/wallet/score/{address}")
async def get_wallet_score(address: str, db: Session = Depends(get_db)):
    """
    AgentTrust Wallet Trust Score — 11 on-chain + platform signals, scored 0–100.
    Combines account age, balance, activity, domain verification, on-chain wallet
    ownership proof, multi-jurisdiction sanctions screening (AnChain.ai BEI),
    entity reputation (XRPScan), Xaman KYC, NFTs held, AgentTrust escrow
    completion history, and peer ratings.
    A sanctioned wallet receives a hard score of 0 regardless of other signals.
    """
    result = await compute_xrpl_trust_score(address, db=db)
    return result


@app.get("/wallet/debug-age/{address}")
async def debug_wallet_age(address: str):
    """Debug endpoint: shows raw result from each age-calculation source."""
    from datetime import datetime, timezone
    results = {}

    # Source 1: data.ripple.com
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"https://data.ripple.com/v2/accounts/{address}",
                                 headers={"Accept": "application/json"})
        results["data_ripple_com"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["data_ripple_com"] = {"error": str(e)}

    # Source 2: s2.ripple.com JSON-RPC
    for url in ["https://s2.ripple.com:51234", "https://s1.ripple.com:51234"]:
        key = url.split("//")[1].split(":")[0]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json={
                    "method": "account_tx",
                    "params": [{"account": address, "limit": 1, "forward": True,
                                "ledger_index_min": 0, "ledger_index_max": -1}]
                })
            txs = r.json().get("result", {}).get("transactions", [])
            results[key] = {"status": r.status_code, "tx_count": len(txs),
                            "first_tx": txs[0] if txs else None}
        except Exception as e:
            results[key] = {"error": str(e)}

    # Source 3: xrplcluster.com (no-forward, just to see what it returns)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(XRPL_URL, json={
                "method": "account_tx",
                "params": [{"account": address, "limit": 1, "forward": True,
                            "ledger_index_min": 0, "ledger_index_max": -1}]
            })
        txs = r.json().get("result", {}).get("transactions", [])
        results["xrplcluster_forward"] = {"status": r.status_code, "tx_count": len(txs),
                                          "first_tx": txs[0] if txs else None}
    except Exception as e:
        results["xrplcluster_forward"] = {"error": str(e)}

    return results


class WalletRatingRequest(BaseModel):
    escrow_id:     str
    rater_address: str
    rating:        int   # 1–5
    comment:       Optional[str] = None


@app.post("/wallet/{address}/rate")
async def rate_wallet(address: str, req: WalletRatingRequest, db: Session = Depends(get_db)):
    """
    Submit a 1–5 star peer rating for a wallet after a completed escrow.
    Auth: the escrow_id must exist and be in RELEASED status, and rater_address
    must be either the buyer or worker on that escrow (one rating per role per escrow).
    """
    if not (1 <= req.rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")

    escrow = db.execute(
        text("SELECT buyer_address, worker_address, status FROM escrow_vault WHERE escrow_id = :id"),
        {"id": req.escrow_id}
    ).fetchone()
    if not escrow:
        raise HTTPException(status_code=404, detail="Escrow not found.")
    if escrow[2] != "RELEASED":
        raise HTTPException(status_code=400, detail="Ratings can only be submitted after an escrow is RELEASED.")

    buyer_address, worker_address = escrow[0], escrow[1]
    if req.rater_address == buyer_address and address == worker_address:
        rater_role = "buyer"
    elif req.rater_address == worker_address and address == buyer_address:
        rater_role = "worker"
    else:
        raise HTTPException(status_code=403, detail="rater_address must be a participant in this escrow rating the other party.")

    try:
        db.execute(text("""
            INSERT INTO wallet_rating (rated_address, rater_address, escrow_id, rating, comment, rater_role, created_at)
            VALUES (:rated, :rater, :escrow_id, :rating, :comment, :role, :now)
        """), {
            "rated": address, "rater": req.rater_address, "escrow_id": req.escrow_id,
            "rating": req.rating, "comment": req.comment, "role": rater_role,
            "now": datetime.now(timezone.utc).isoformat(),
        })
        db.commit()
    except Exception:
        raise HTTPException(status_code=409, detail="You have already rated this wallet for this escrow.")

    return {"status": "rated", "rated_address": address, "rating": req.rating, "role": rater_role}


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
    x_reviewer_token: Optional[str] = Header(None),
    payment_signature: Optional[str] = Header(None, alias="PAYMENT-SIGNATURE"),
    db: Session = Depends(get_db),
    response: Response = None,
):
    fee_hash = (req.fee_hash or x_payment_hash or x_payment or "").strip()
    if not fee_hash and not x_reviewer_token and not payment_signature:
        _raise_402(
            "/audit",
            f"Payment required. Option 1: Send {MIN_FEE_XRP} XRP to {PROTOCOL_WALLET} on the XRPL. "
            f"Option 2: Send ${MIN_FEE_USDC:.2f} USDC to {BASE_WALLET_ADDRESS or '(not configured)'} on Base (chain 8453). "
            "Include the transaction hash as the X-PAYMENT header (or fee_hash body field). "
            "Or provide a PAYMENT-SIGNATURE header (x402 v2 XRPL presigned flow).",
        )

    audit_id = f"audit-{(fee_hash or 'reviewer')[:16].lower()}"
    fee_result = await verify_fee_payment(fee_hash=fee_hash, escrow_id=audit_id, db=db, resource="/audit", reviewer_token=x_reviewer_token, payment_signature=payment_signature)
    if response is not None and fee_result.get("payment_response_header"):
        response.headers["PAYMENT-RESPONSE"] = fee_result["payment_response_header"]

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
async def generate_escrow(req: EscrowSetupRequest, db: Session = Depends(get_db), x_reviewer_token: Optional[str] = Header(None), payment_signature: Optional[str] = Header(None, alias="PAYMENT-SIGNATURE"), response: Response = None):
    existing = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Project ID '{req.escrow_id}' already exists.")

    # OFAC sanctions check — block sanctioned wallets before accepting any funds
    for addr_to_check in [req.buyer_address, req.worker_address]:
        if addr_to_check:
            try:
                sanctioned, s_detail = await is_wallet_sanctioned(addr_to_check, escrow_id=req.escrow_id)
                if sanctioned:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Wallet {addr_to_check} is flagged on a sanctions list (source: {s_detail.get('source','unknown')}). Escrow creation is not permitted."
                    )
            except HTTPException:
                raise
            except Exception:
                pass  # screening unavailable — don't block, log and continue

    # Regulatory threshold check — warn at $1k (Travel Rule), block at $3k (KYC required), $10k (hard cap)
    threshold = await check_value_threshold(
        req.amount_xrp, req.amount_rlusd,
        req.currency.upper() if req.currency else "XRP",
        buyer_address=req.buyer_address,
        db=db,
    )
    if not threshold["ok"]:
        if threshold.get("level") == "kyc_required":
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "kyc_required",
                    "kyc_url": "/kyc/verify",
                    "message": threshold["message"],
                },
            )
        raise HTTPException(
            status_code=451,  # Unavailable For Legal Reasons
            detail=threshold["message"],
        )

    fee_result = await verify_fee_payment(fee_hash=req.fee_hash, escrow_id=req.escrow_id, db=db, resource="/escrow/generate", reviewer_token=x_reviewer_token, payment_signature=payment_signature)
    if response is not None and fee_result.get("payment_response_header"):
        response.headers["PAYMENT-RESPONSE"] = fee_result["payment_response_header"]

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
        if amount_xrp < 1.0:
            # Not a hard block — small amounts are valid — but warn that unfunded worker wallets
            # won't be auto-created by the EscrowFinish if the payout is below the 1 XRP reserve.
            logger.warning(f"escrow amount {amount_xrp} XRP < 1 XRP reserve — worker wallet may need pre-funding")
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
        invoice_requirements   = json.dumps(req.invoice_requirements) if req.invoice_requirements else None,
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
            worker_email         = req.worker_email,
            worker_name          = "",
            escrow_id            = req.escrow_id,
            buyer_name           = req.buyer_name,
            amount               = amount_val,
            currency             = currency,
            task_preview         = req.task_description,
            deadline             = deadline_str,
            invoice_requirements = req.invoice_requirements or None,
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

    response_body = {
        "escrow_id":           req.escrow_id,
        "condition":           final_condition,
        "escrow_amount":       escrow_amount,      # ready for EscrowCreate tx
        "currency":            currency,
        "status":              "LOCKED",
        "cancel_after_ripple": cancel_after_ripple,
        "cancel_after_human":  cancel_after_ts.strftime("%Y-%m-%d %H:%M UTC") if cancel_after_ts else None,
        "worker_email_sent":   bool(req.worker_email),
    }
    if threshold.get("level") == "warn":
        response_body["compliance_warning"] = threshold["compliance_warning"]
    if currency == "XRP" and amount_xrp and amount_xrp < 1.0:
        response_body["worker_reserve_warning"] = (
            f"Escrow amount ({amount_xrp} XRP) is below the 1 XRP XRPL account reserve. "
            "If the worker wallet is unfunded, the EscrowFinish will fail to activate it. "
            "Ensure the worker wallet is already funded, or increase the escrow amount to ≥ 1 XRP."
        )
    return response_body


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


class PrepareEscrowRequest(BaseModel):
    escrow_id:        str
    buyer_address:    str
    worker_address:   str
    amount_xrp:       Optional[float] = None
    amount_rlusd:     Optional[float] = None
    currency:         str = "XRP"
    cancel_after_hrs: int = 168


@app.post("/escrow/prepare")
async def prepare_escrow(req: PrepareEscrowRequest, db: Session = Depends(get_db)):
    """
    Build a ready-to-sign XRPL EscrowCreate transaction without requiring
    the buyer to have any XRPL library installed.

    The caller signs the returned transaction dict with their wallet (e.g. via
    xrpl-py wallet.sign(), Xaman, or any XRPL signer) and submits the blob.
    They then call POST /escrow/{escrow_id}/confirm with the tx hash.

    This endpoint does NOT create the vault record — call POST /escrow/generate
    first (to register the escrow and get the condition), then call this endpoint
    to get the signable transaction.
    """
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == req.escrow_id).first()
    if not vault:
        raise HTTPException(
            status_code=404,
            detail=f"Escrow '{req.escrow_id}' not found. Call POST /escrow/generate first.",
        )

    try:
        client     = AsyncJsonRpcClient(XRPL_URL)
        acct_res   = await client.request(AccountInfo(account=req.buyer_address, ledger_index="current"))
        acct_data  = acct_res.result["account_data"]
        sequence   = acct_data["Sequence"]
        ledger_res = await client.request(Fee())
        base_fee   = int(ledger_res.result.get("drops", {}).get("base_fee", 12))
        current_ledger = ledger_res.result.get("ledger_current_index", 0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch XRPL account info: {e}")

    currency = req.currency.upper()
    if currency == "RLUSD" and req.amount_rlusd:
        amount_field = {"currency": RLUSD_HEX, "issuer": RLUSD_ISSUER, "value": str(req.amount_rlusd)}
    else:
        amount_xrp = req.amount_xrp or (vault.amount_xrp or 0)
        amount_field = str(int(amount_xrp * 1_000_000))

    cancel_after_ripple = None
    if vault.cancel_after_ts:
        cancel_after_ripple = int(vault.cancel_after_ts.timestamp()) - RIPPLE_EPOCH

    tx = {
        "TransactionType":    "EscrowCreate",
        "Account":            req.buyer_address,
        "Destination":        req.worker_address,
        "Amount":             amount_field,
        "Condition":          vault.condition,
        "Fee":                str(base_fee),
        "Sequence":           sequence,
        "LastLedgerSequence": current_ledger + 20,
    }
    if cancel_after_ripple:
        tx["CancelAfter"] = cancel_after_ripple

    return {
        "escrow_id":   req.escrow_id,
        "transaction": tx,
        "instructions": (
            "Sign this transaction with your buyer wallet and submit the signed blob to the XRPL. "
            "Then call POST /escrow/{escrow_id}/confirm with the resulting tx hash."
        ),
        "condition": vault.condition,
    }


@app.post("/escrow/{escrow_id}/submit")
async def submit_escrow_transaction(escrow_id: str, body: dict, db: Session = Depends(get_db)):
    """
    Submit a signed EscrowCreate tx blob and auto-confirm the vault in one step.

    The buyer signs the transaction dict from POST /escrow/prepare locally
    (seed never leaves their environment), then sends the signed blob here.
    We submit it to XRPL and activate the vault automatically — no separate
    confirm step required.

    Body: { "tx_blob": "<hex-encoded signed transaction>" }
    """
    vault = db.query(EscrowVault).filter(EscrowVault.escrow_id == escrow_id).first()
    if not vault:
        raise HTTPException(status_code=404, detail=f"Vault '{escrow_id}' not found.")

    tx_blob = body.get("tx_blob", "").strip()
    if not tx_blob:
        raise HTTPException(status_code=400, detail="tx_blob is required.")

    try:
        from xrpl.models.transactions import Transaction
        client  = AsyncJsonRpcClient(XRPL_URL)
        result  = await client.request(SubmitOnly(tx_blob=tx_blob))
        res     = result.result
        engine  = res.get("engine_result", "")
        tx_hash = res.get("tx_json", {}).get("hash") or res.get("hash", "")

        if engine not in ("tesSUCCESS", "terQUEUED") and not engine.startswith("tes"):
            raise HTTPException(
                status_code=400,
                detail=f"XRPL rejected transaction: {engine} — {res.get('engine_result_message', '')}",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to submit to XRPL: {e}")

    # Auto-confirm the vault
    sequence = res.get("tx_json", {}).get("Sequence")
    vault.escrow_tx_hash  = tx_hash
    vault.escrow_sequence = sequence
    if not vault.escrow_owner:
        vault.escrow_owner = vault.buyer_address
    db.commit()

    logger.info(f"✅ Auto-confirmed via submit: escrow={escrow_id} hash={tx_hash[:16] if tx_hash else '?'}... engine={engine}")

    return {
        "status":    "submitted_and_confirmed",
        "escrow_id": escrow_id,
        "tx_hash":   tx_hash,
        "sequence":  sequence,
        "engine":    engine,
        "next_step": (
            "Vault is active. The worker submits proof via evaluate_escrow_work() "
            "and payment releases automatically on PASS."
        ),
    }


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
async def purchase_extra_attempt(req: PurchaseAttemptRequest, db: Session = Depends(get_db), x_reviewer_token: Optional[str] = Header(None), payment_signature: Optional[str] = Header(None, alias="PAYMENT-SIGNATURE"), response: Response = None):
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
    fee_result = await verify_fee_payment(
        fee_hash  = req.fee_hash,
        escrow_id = f"{req.escrow_id}-attempt",
        db        = db,
        min_xrp   = EXTRA_ATTEMPT_FEE_XRP,
        resource  = "/evaluate/purchase-attempt",
        reviewer_token = x_reviewer_token,
        payment_signature = payment_signature,
    )
    if response is not None and fee_result.get("payment_response_header"):
        response.headers["PAYMENT-RESPONSE"] = fee_result["payment_response_header"]

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


@app.post("/jobs/{job_id}/claim")
async def claim_job(job_id: str, body: dict, db: Session = Depends(get_db)):
    """
    Worker agent claims an open job and self-awards it without a bid/award cycle.

    Only works on jobs that are explicitly marked claimable (posted with
    claimable=True). The job transitions to 'awarded', the caller's wallet
    becomes the worker, and the buyer is notified via webhook if configured.

    Returns the agreed XRP amount and escrow creation instructions so the
    buyer (or agent acting as buyer) can immediately call /escrow/generate
    (or the prepare_escrow endpoint) to lock funds.
    """
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job.status != "open":
        raise HTTPException(status_code=409, detail=f"Job '{job_id}' is not open (status: {job.status}).")
    if not getattr(job, "claimable", False):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Job '{job_id}' is not directly claimable. "
                "Submit a bid via POST /jobs/{job_id}/bid instead."
            ),
        )

    worker_address = (body.get("worker_address") or "").strip()
    if not worker_address or not worker_address.startswith("r"):
        raise HTTPException(status_code=400, detail="worker_address must be a valid XRPL r-address.")

    worker_name  = (body.get("worker_name") or "").strip()
    worker_email = (body.get("worker_email") or "").strip() or None

    import uuid
    bid_id = f"BID-{uuid.uuid4().hex[:8].upper()}"

    bid = Bid(
        id             = bid_id,
        job_id         = job_id,
        worker_address = worker_address,
        worker_name    = worker_name,
        worker_email   = worker_email,
        proposed_xrp   = job.budget_xrp or 0,
        proposal       = "Direct claim",
        status         = "accepted",
    )
    db.add(bid)

    job.status         = "awarded"
    job.awarded_bid_id = bid_id
    db.commit()

    logger.info(f"🎯 JOB CLAIMED: {job_id} → worker={worker_address}")

    import asyncio
    if job.buyer_callback_url:
        asyncio.create_task(fire_bid_awarded_webhook(
            callback_url   = job.buyer_callback_url,
            bid_id         = bid_id,
            job_id         = job_id,
            job_title      = job.title,
            agreed_xrp     = bid.proposed_xrp,
            worker_address = worker_address,
        ))

    return {
        "status":         "claimed",
        "job_id":         job_id,
        "bid_id":         bid_id,
        "worker_address": worker_address,
        "agreed_xrp":     bid.proposed_xrp,
        "next_step": (
            f"Buyer must create the escrow: call prepare_escrow or create_escrow_vault() "
            f"with worker_address='{worker_address}' and amount_xrp={bid.proposed_xrp}. "
            f"Sign the returned EscrowCreate transaction and confirm via confirm_escrow_transaction()."
        ),
    }


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
async def post_skill_listing(req: SkillListingRequest, db: Session = Depends(get_db), x_reviewer_token: Optional[str] = Header(None), payment_signature: Optional[str] = Header(None, alias="PAYMENT-SIGNATURE"), response: Response = None):
    """
    Create a new skill listing. Requires a valid 0.1 XRP fee payment.
    Both humans (via the marketplace UI) and agents (via MCP) can post skills.
    """
    existing = db.query(SkillListing).filter(SkillListing.id == req.id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Skill ID '{req.id}' already exists.")

    fee_result = await verify_fee_payment(fee_hash=req.fee_hash, escrow_id=req.id, db=db, resource="/marketplace/skills", reviewer_token=x_reviewer_token, payment_signature=payment_signature)
    if response is not None and fee_result.get("payment_response_header"):
        response.headers["PAYMENT-RESPONSE"] = fee_result["payment_response_header"]

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

@app.get("/nft/issuers/debug-seed")
async def debug_seed():
    """Manually run the seed and report results — for diagnostics only."""
    db_url = os.getenv("DATABASE_URL", "NOT SET")
    db_url_safe = db_url[:40] + "..." if len(db_url) > 40 else db_url
    results = []
    errors = []
    try:
        db = SessionLocal()
        try:
            for entry in _PUBLIC_ISSUERS:
                try:
                    exists = db.execute(
                        text("SELECT id FROM nft_issuer WHERE wallet_address = :w"),
                        {"w": entry["wallet_address"]}
                    ).fetchone()
                    if exists:
                        results.append({"name": entry["name"], "status": "already exists"})
                        continue
                    db.execute(text("""
                        INSERT INTO nft_issuer
                            (wallet_address, name, category, description, website, verified, created_at)
                        VALUES
                            (:wallet_address, :name, :category, :description, :website, :verified, :created_at)
                    """), {
                        "wallet_address": entry["wallet_address"],
                        "name": entry["name"],
                        "category": entry["category"],
                        "description": entry["description"],
                        "website": entry["website"],
                        "verified": "public",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    db.commit()
                    results.append({"name": entry["name"], "status": "inserted"})
                except Exception as e:
                    db.rollback()
                    errors.append({"name": entry["name"], "error": str(e)})
            total = db.execute(text("SELECT COUNT(*) FROM nft_issuer")).scalar()
        finally:
            db.close()
    except Exception as e:
        return {"fatal_error": str(e), "database_url": db_url_safe}
    return {"seeded": results, "errors": errors, "total_issuers_in_db": total, "database_url": db_url_safe,
            "columns": [r[0] for r in db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='nft_issuer' ORDER BY ordinal_position")).fetchall()]}


@app.get("/nft/issuers")
async def list_nft_issuers(category: str = None, include_pending: bool = False, db: Session = Depends(get_db)):
    if include_pending:
        statuses = ("verified", "public", "pending")
    else:
        statuses = ("verified", "public")
    placeholders = ",".join(f":s{i}" for i in range(len(statuses)))
    params = {f"s{i}": s for i, s in enumerate(statuses)}
    # Only select core columns guaranteed to exist; newer optional columns added safely
    existing_cols = {r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='nft_issuer'"
    )).fetchall()}
    optional = {c: f", {c}" if c in existing_cols else ", NULL" for c in ["wallet_addresses", "verified", "lei", "nft_types"]}
    sql = (
        f"SELECT id, wallet_address{optional['wallet_addresses']}{optional['verified']}"
        f"{optional['lei']}{optional['nft_types']}, name, category, description, website"
        f" FROM nft_issuer WHERE verified IN ({placeholders})"
        if "verified" in existing_cols else
        f"SELECT id, wallet_address{optional['wallet_addresses']}, NULL as verified"
        f"{optional['lei']}{optional['nft_types']}, name, category, description, website"
        f" FROM nft_issuer"
    )
    if "verified" in existing_cols and category:
        sql += " AND category = :category"
        params["category"] = category
    elif category:
        sql += " WHERE category = :category"
        params["category"] = category
    sql += " ORDER BY name"
    rows = db.execute(text(sql), params).fetchall()
    base_url = "https://xrpl-referee.onrender.com"
    keys = list(rows[0]._fields) if rows else []
    def row_val(r, k): return getattr(r, k, None)
    return {
        "issuers": [
            {
                "id":               row_val(r, "id"),
                "wallet_address":   row_val(r, "wallet_address"),
                "wallet_addresses": json.loads(row_val(r, "wallet_addresses") or "[]") or [row_val(r, "wallet_address")],
                "name":             row_val(r, "name"),
                "category":         row_val(r, "category"),
                "description":      row_val(r, "description"),
                "website":          row_val(r, "website"),
                "verified":         row_val(r, "verified") or "public",
                "lei":              row_val(r, "lei"),
                "nft_types":        row_val(r, "nft_types"),
            }
            for r in rows
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

    # Try auto-verification via xrp-ledger.toml before creating the record
    auto_verified = False
    if req.website and req.wallet_address:
        try:
            vr = await verify_domain_ownership(req.wallet_address, req.website)
            auto_verified = vr.get("verified", False)
        except Exception:
            pass

    issuer = NftIssuer(
        wallet_address=all_wallets[0], name=req.name,
        category=req.category, description=req.description,
        website=req.website, verified="verified" if auto_verified else "pending",
        created_at=datetime.now(timezone.utc),
        contact_email=req.contact_email,
        lei=req.lei, nft_types=req.nft_types,
    )
    issuer.set_wallets(all_wallets)
    db.add(issuer); db.commit()
    if RESEND_API_KEY:
        asyncio.create_task(_send_issuer_registration_email(issuer))
    if auto_verified:
        return {"status": "verified", "message": "Registered and verified! Your listing is now live.", "wallets": issuer.all_wallets()}
    return {"status": "pending", "message": "Registration received. Once you publish your xrp-ledger.toml file, return here to complete verification.", "wallets": issuer.all_wallets()}


@app.get("/nft/issuers/feed")
async def issuer_registry_feed(
    page: int = 1,
    per_page: int = 100,
    since: str = None,
    category: str = None,
    verified: str = None,
    db: Session = Depends(get_db),
):
    """
    Paginated, versioned JSON feed of the AgentTrust Issuer Registry.
    Designed for wallets, explorers, and DEXs to consume and cache.

    - page / per_page: standard pagination (max 200 per page)
    - since: ISO 8601 timestamp — return only records created or updated after this time
    - category: filter by category slug
    - verified: filter by status ('verified', 'public', or omit for both)
    """
    per_page = min(per_page, 200)
    offset = (page - 1) * per_page

    where_clauses = ["verified IN ('verified','public')"]
    params: dict = {}

    if verified in ("verified", "public", "pending"):
        where_clauses = [f"verified = :verified"]
        params["verified"] = verified
    if category:
        where_clauses.append("category = :category")
        params["category"] = category
    if since:
        where_clauses.append("created_at > :since")
        params["since"] = since

    where_sql = " AND ".join(where_clauses)

    existing_cols = {r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='nft_issuer'"
    )).fetchall()}

    opt_wa  = ", wallet_addresses" if "wallet_addresses" in existing_cols else ", NULL as wallet_addresses"
    opt_lei = ", lei"              if "lei"              in existing_cols else ", NULL as lei"
    opt_nft = ", nft_types"        if "nft_types"        in existing_cols else ", NULL as nft_types"
    opt_cat = ", created_at"       if "created_at"       in existing_cols else ", NULL as created_at"

    total = db.execute(text(f"SELECT COUNT(*) FROM nft_issuer WHERE {where_sql}"), params).scalar()

    rows = db.execute(text(
        f"SELECT id, wallet_address{opt_wa}, name, category, description, website, verified{opt_lei}{opt_nft}{opt_cat}"
        f" FROM nft_issuer WHERE {where_sql} ORDER BY id LIMIT :limit OFFSET :offset"
    ), {**params, "limit": per_page, "offset": offset}).fetchall()

    def rv(r, k): return getattr(r, k, None)

    return {
        "spec_version": "1.0.0",
        "spec": "https://www.cryptovault.co.uk/docs/issuer-registry-spec.md",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),
            "next": f"https://xrpl-referee.onrender.com/nft/issuers/feed?page={page+1}&per_page={per_page}" if (page * per_page) < total else None,
        },
        "filters": {"category": category, "verified": verified, "since": since},
        "issuers": [
            {
                "id":               rv(r, "id"),
                "wallet_address":   rv(r, "wallet_address"),
                "wallet_addresses": json.loads(rv(r, "wallet_addresses") or "[]") or [rv(r, "wallet_address")],
                "name":             rv(r, "name"),
                "category":         rv(r, "category"),
                "description":      rv(r, "description"),
                "website":          rv(r, "website"),
                "verified":         rv(r, "verified"),
                "lei":              rv(r, "lei"),
                "nft_types":        rv(r, "nft_types"),
                "created_at":       rv(r, "created_at").isoformat() if rv(r, "created_at") else None,
            }
            for r in rows
        ],
    }


class NftIssuerClaimRequest(BaseModel):
    contact_email: str


class WalletVerifyConfirmRequest(BaseModel):
    wallet_address: str
    tx_hash: str
    issuer_id: Optional[int] = None   # if provided, marks the issuer entry as sig-verified too


def _fetch_issuer_row(db: Session, issuer_id: int):
    """Fetch a single issuer via raw SQL, selecting only columns guaranteed to exist
    (mirrors list_nft_issuers — the live table may predate newer optional columns)."""
    existing_cols = {r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='nft_issuer'"
    )).fetchall()}
    cols = ["id", "wallet_address", "name", "category", "description", "website"]
    optional_cols = ["wallet_addresses", "verified", "contact_email"]
    select_cols = cols + [c for c in optional_cols if c in existing_cols]
    sql = f"SELECT {', '.join(select_cols)} FROM nft_issuer WHERE id = :id"
    row = db.execute(text(sql), {"id": issuer_id}).fetchone()
    if not row:
        return None
    data = dict(zip(select_cols, row))
    for c in optional_cols:
        data.setdefault(c, None)
    return data


@app.get("/nft/issuers/by-wallet/{wallet_address}")
async def get_nft_issuer_by_wallet(wallet_address: str, db: Session = Depends(get_db)):
    """Look up a registry entry by wallet address. Returns 404 if not found."""
    existing_cols = {r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='nft_issuer'"
    )).fetchall()}
    cols = ["id", "wallet_address", "name", "category", "description", "website"]
    optional_cols = ["wallet_addresses", "verified", "contact_email"]
    select_cols = cols + [c for c in optional_cols if c in existing_cols]
    row = db.execute(
        text(f"SELECT {', '.join(select_cols)} FROM nft_issuer WHERE wallet_address = :w LIMIT 1"),
        {"w": wallet_address},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Wallet not found in registry.")
    data = dict(zip(select_cols, row))
    for c in optional_cols:
        data.setdefault(c, None)
    return {
        "id": data["id"],
        "name": data["name"],
        "wallet_address": data["wallet_address"],
        "wallet_addresses": json.loads(data["wallet_addresses"] or "[]") or [data["wallet_address"]],
        "category": data["category"],
        "description": data["description"],
        "website": data["website"],
        "verified": data["verified"] or "public",
        "claimed": bool(data["contact_email"]),
    }


@app.get("/nft/issuers/{issuer_id}")
async def get_nft_issuer(issuer_id: int, db: Session = Depends(get_db)):
    issuer = _fetch_issuer_row(db, issuer_id)
    if not issuer:
        raise HTTPException(status_code=404, detail="Issuer not found.")
    return {
        "id": issuer["id"],
        "name": issuer["name"],
        "wallet_address": issuer["wallet_address"],
        "wallet_addresses": json.loads(issuer["wallet_addresses"] or "[]") or [issuer["wallet_address"]],
        "category": issuer["category"],
        "description": issuer["description"],
        "website": issuer["website"],
        "verified": issuer["verified"] or "public",
        "claimed": bool(issuer["contact_email"]),
    }


@app.post("/nft/issuers/{issuer_id}/claim")
async def claim_nft_issuer(issuer_id: int, req: NftIssuerClaimRequest, db: Session = Depends(get_db)):
    """
    Self-serve claim for a seeded ("public") registry entry. The caller proves control of the
    issuer's wallet by publishing it in their own domain's xrp-ledger.toml — the same convention
    XRPL already uses for Domain-field verification. No manual review needed once that check passes.
    """
    issuer = _fetch_issuer_row(db, issuer_id)
    if not issuer:
        raise HTTPException(status_code=404, detail="Issuer not found.")
    if issuer["contact_email"]:
        raise HTTPException(status_code=409, detail="This entry has already been claimed.")
    if not issuer["website"]:
        raise HTTPException(status_code=400, detail="This entry has no domain on file to verify against. Contact support to claim it manually.")

    result = await verify_domain_ownership(issuer["wallet_address"], issuer["website"])
    if not result["verified"]:
        raise HTTPException(status_code=400, detail=result["detail"])

    db.execute(
        text("UPDATE nft_issuer SET contact_email = :email, verified = 'verified' WHERE id = :id"),
        {"email": req.contact_email, "id": issuer_id},
    )
    db.commit()
    return {
        "status": "verified",
        "message": f"Claim confirmed via {issuer['website']}. {issuer['name']} is now marked 'verified' in the registry.",
        "name": issuer["name"],
        "wallet_address": issuer["wallet_address"],
    }


# ---------------------------------------------------------------------------
# 11g. ON-CHAIN WALLET OWNERSHIP VERIFICATION
# ---------------------------------------------------------------------------
@app.get("/verify/challenge")
async def get_wallet_challenge(wallet: str):
    """
    Issue a one-time verification challenge for a wallet.

    The wallet owner must submit an XRPL AccountSet transaction from their wallet
    with a Memo containing the challenge string (as hex). They then POST the tx hash
    to POST /verify/confirm to complete ownership proof.

    No private key or signature is ever sent to AgentTrust — the proof is the
    on-chain tx itself, which only the key-holder could have signed and broadcast.
    Challenge expires in 30 minutes.
    """
    if not wallet or not wallet.startswith("r") or len(wallet) < 25:
        raise HTTPException(400, "Invalid XRPL wallet address.")
    challenge = f"agenttrust-verify-{secrets.token_hex(12)}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    _verify_challenges[wallet] = (challenge, expires_at)
    memo_hex = challenge.encode().hex().upper()
    return {
        "wallet": wallet,
        "challenge": challenge,
        "memo_hex": memo_hex,
        "expires_at": expires_at.isoformat(),
        "instructions": (
            f"Using Xaman or any XRPL wallet, submit an AccountSet transaction from {wallet} "
            f"with one Memo whose MemoData field is exactly: {memo_hex} "
            f"(hex-encoded UTF-8 of the challenge string, no spaces). "
            f"Then POST the resulting tx hash to /verify/confirm within 30 minutes."
        ),
    }


@app.post("/verify/confirm")
async def confirm_wallet_ownership(req: WalletVerifyConfirmRequest, db: Session = Depends(get_db)):
    """
    Complete wallet ownership verification by providing the XRPL AccountSet tx hash.

    Looks up the transaction on-chain, confirms it was submitted by the claimed wallet,
    and verifies the Memo contains the expected challenge string. On success, stores a
    WalletVerification record and optionally marks an NFT issuer entry as sig-verified.
    """
    wallet = req.wallet_address.strip()
    tx_hash = req.tx_hash.strip().upper()

    # Check a challenge exists and is still valid
    if wallet not in _verify_challenges:
        raise HTTPException(400, "No pending challenge for this wallet. Call GET /verify/challenge?wallet=... first.")
    challenge, expires_at = _verify_challenges[wallet]
    if datetime.now(timezone.utc) > expires_at:
        del _verify_challenges[wallet]
        raise HTTPException(400, "Challenge expired. Request a new one via GET /verify/challenge.")

    # Look up the tx on XRPL
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            tx_res = await client.post(XRPL_URL, json={
                "method": "tx",
                "params": [{"transaction": tx_hash}]
            })
        tx_data = tx_res.json().get("result", {})
    except Exception as e:
        raise HTTPException(502, f"Could not reach XRPL node: {e}")

    if tx_data.get("status") == "error" or not tx_data:
        raise HTTPException(400, "Transaction not found on the XRPL ledger. Make sure the tx has been confirmed.")

    # Verify it was submitted by the claimed wallet
    account = (
        tx_data.get("Account")
        or tx_data.get("tx_json", {}).get("Account")
        or tx_data.get("tx", {}).get("Account")
    )
    if account != wallet:
        raise HTTPException(400, f"Transaction account ({account}) does not match claimed wallet ({wallet}).")

    # Verify the Memo contains the challenge
    memos = (
        tx_data.get("Memos")
        or tx_data.get("tx_json", {}).get("Memos")
        or tx_data.get("tx", {}).get("Memos")
        or []
    )
    expected_hex = challenge.encode().hex().upper()
    found = any(
        m.get("Memo", {}).get("MemoData", "").upper() == expected_hex
        for m in memos
    )
    if not found:
        raise HTTPException(400, "Transaction Memo does not contain the expected challenge string. Check that MemoData is set correctly (no extra characters).")

    # Persist the verification
    try:
        Base.metadata.create_all(engine, tables=[WalletVerification.__table__])
    except Exception:
        pass
    try:
        existing = db.query(WalletVerification).filter(WalletVerification.wallet_address == wallet).first()
        if existing:
            existing.verified_at = datetime.now(timezone.utc)
            existing.tx_hash = tx_hash
        else:
            db.add(WalletVerification(wallet_address=wallet, tx_hash=tx_hash))
        db.commit()
    except Exception as e:
        logger.warning(f"Could not persist WalletVerification: {e}")

    # Optionally mark an issuer entry as verified too
    if req.issuer_id:
        try:
            db.execute(
                text("UPDATE nft_issuer SET verified = 'verified' WHERE id = :id AND wallet_address = :w"),
                {"id": req.issuer_id, "w": wallet},
            )
            db.commit()
        except Exception:
            pass

    del _verify_challenges[wallet]
    return {
        "verified": True,
        "wallet": wallet,
        "method": "xrpl_accountset",
        "tx_hash": tx_hash,
        "message": f"Wallet {wallet} ownership confirmed via on-chain AccountSet tx {tx_hash}.",
    }


@app.get("/wallet/sanctions/{address}")
async def check_sanctions(address: str):
    """
    Screen an XRPL wallet address against multi-jurisdiction sanctions lists.
    Primary: AnChain.ai BEI (OFAC + UN + UK + EU + Canada + Australia + AI risk graph).
    Fallback: US Treasury OFAC SDN XML (refreshed every 24 h).
    Returns sanctioned, risk_score, risk_level, entity, category, source.
    Always returns a result — never raises on data unavailability.
    """
    sanctioned, detail = await is_wallet_sanctioned(address)
    return {
        "address":    address,
        "sanctioned": sanctioned,
        **{k: v for k, v in detail.items() if k != "sanctioned"},
        "note": "Not a substitute for professional compliance advice.",
    }


@app.delete("/nft/issuers/{issuer_id}")
async def delete_nft_issuer(issuer_id: int, token: str = None, db: Session = Depends(get_db)):
    """Admin-only removal of a registry entry (e.g. defunct/blackholed issuer). Guarded by REVIEWER_BYPASS_TOKEN."""
    if not REVIEWER_BYPASS_TOKEN or token != REVIEWER_BYPASS_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing admin token.")
    issuer = _fetch_issuer_row(db, issuer_id)
    if not issuer:
        raise HTTPException(status_code=404, detail="Issuer not found.")
    db.execute(text("DELETE FROM nft_issuer WHERE id = :id"), {"id": issuer_id})
    db.commit()
    return {"status": "deleted", "name": issuer["name"], "id": issuer_id}


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
# ISSUER LOOKUP  (AgentTrust registry only)
# Path kept as /gleif/xrpl-lookup for backward compatibility
# ---------------------------------------------------------------------------

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
            "name":          q,
            "source":        "agentrust",
            "xrpl_wallet":   None,
            "xrpl_verified": False,
            "domain":        None,
            "register_url":  "https://www.cryptovault.co.uk/marketplace#issuers",
            "message":       "Not in the AgentTrust registry. Ask the organisation to register their XRPL wallet.",
        })
    return {"results": results}

# ---------------------------------------------------------------------------
# KYC — Xaman KYC verification (powered by Veriff)
# ---------------------------------------------------------------------------

@app.post("/kyc/verify")
async def kyc_verify(wallet_address: str, db: Session = Depends(get_db)):
    """
    Check whether a wallet has Xaman KYC and cache the result.
    Agents call this after the operator completes Xaman KYC to refresh
    their verified status without waiting for the next trust score query.

    Returns {"kyc_verified": true} if Xaman reports the wallet as KYC-approved.
    The result is cached in kyc_record so future escrow requests bypass the
    live Xaman query.
    """
    # Return cached result if already verified
    existing = db.query(KycRecord).filter(
        KycRecord.wallet_address == wallet_address,
        KycRecord.status == "verified",
    ).first()
    if existing:
        return {
            "wallet_address": wallet_address,
            "kyc_verified": True,
            "method": existing.return_url or "xaman",
            "verified_at": existing.verified_at.isoformat() if existing.verified_at else None,
        }

    xaman_ok = await _get_xaman_kyc(wallet_address)
    if xaman_ok:
        try:
            row = KycRecord(
                wallet_address=wallet_address,
                status="verified",
                verified_at=datetime.now(timezone.utc),
                return_url="xaman",
            )
            db.add(row)
            db.commit()
        except Exception:
            db.rollback()
        return {"wallet_address": wallet_address, "kyc_verified": True, "method": "xaman"}

    return {
        "wallet_address": wallet_address,
        "kyc_verified": False,
        "message": "Xaman KYC not detected for this wallet. Complete identity verification via Xaman (powered by Veriff), then call this endpoint again.",
        "xaman_kyc_url": "https://xaman.app/detect/xapp/xumm/kyc",
    }


@app.get("/kyc/status/{wallet_address}")
async def kyc_status(wallet_address: str, db: Session = Depends(get_db)):
    """
    Check KYC verification status for a wallet address.
    Checks the local cache first, then queries Xaman live if no cached record exists.
    """
    row = db.query(KycRecord).filter(
        KycRecord.wallet_address == wallet_address,
        KycRecord.status == "verified",
    ).first()
    if row:
        return {
            "wallet_address": wallet_address,
            "kyc_verified": True,
            "method": row.return_url or "xaman",
            "verified_at": row.verified_at.isoformat() if row.verified_at else None,
        }

    # Live Xaman check
    xaman_ok = await _get_xaman_kyc(wallet_address)
    return {
        "wallet_address": wallet_address,
        "kyc_verified": xaman_ok,
        "method": "xaman" if xaman_ok else None,
    }


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🚀 Starting AgentTrust Referee v7.0 on port {port}")
    uvicorn.run("referee:app", host="0.0.0.0", port=port, reload=False)

