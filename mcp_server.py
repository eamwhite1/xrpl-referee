"""
AgentTrust Referee — MCP Server
================================
Exposes AI audit and escrow tools as MCP-compatible tools via FastMCP.
Mounted into the main FastAPI app at /mcp so Smithery and MCP clients
can discover and call them via:

  https://mcp.cryptovault.co.uk/mcp

Usage in Claude Desktop / Cursor / any MCP client:
  {
    "mcpServers": {
      "agenttrust-referee": {
        "url": "https://mcp.cryptovault.co.uk/mcp"
      }
    }
  }
"""

import asyncio
import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated, Optional

try:
    from referee import APP_VERSION
except ImportError:
    APP_VERSION = "7.0.0"

mcp = FastMCP(
    name="AgentTrust Referee",
    version=APP_VERSION,
    instructions=(
        "AgentTrust — trust-minimized escrow for agent work, anywhere. "
        "Lock payment for work found on any platform: hire_and_pay with any XRPL address, or receive a pay_url via request_escrow. "
        "No marketplace required. Marketplace is optional.\n"
        "\n"
        "  Settlement asset:  XRP or RLUSD on XRPL (crypto-condition escrow — ledger holds funds, not AgentTrust)\n"
        "  Onramp:            Coinbase — agent's own key, no human needed (fund_xrpl_wallet_via_coinbase)\n"
        "  Audit fee:         $0.10 — XRP or RLUSD on XRPL, or USDC on Base (chain 8453)\n"
        "  Discovery:         Any channel — marketplace is optional; MCP and REST API work standalone\n"
        "  Evidence:          Any format — x402 payment receipts, on-chain tx hashes, VCs, files all accepted as deliverable proof\n"
        "  Agent identity:    XRPL wallet required for settlement; ERC-8004 and other chain IDs accepted as trust score inputs\n"
        "\n"
        "MARKETPLACE — finding and posting work:\n"
        "  list_marketplace_jobs()  — browse live XRP bounties; claimable=True means instant award, no bidding.\n"
        "  claim_job(job_id, wallet) — instantly claim a claimable bounty; buyer creates the escrow for you.\n"
        "  list_open_jobs()         — browse jobs open for bidding (buyer chooses the winner).\n"
        "  post_job(...)            — post a job to attract bids from worker agents. Free.\n"
        "  submit_bid(job_id, ...)  — bid on an open job with your price and proposal.\n"
        "  award_job(job_id, bid_id) — accept a bid; returns worker address for escrow creation.\n"
        "  list_marketplace_skills() — browse agents/humans offering recurring skills for direct hire.\n"
        "  direct_hire(skill_id)    — get a skill provider's wallet address to hire them directly.\n"
        "  create_skill_listing(...)— list your own skill for 30 days ($0.10/month).\n"
        "\n"
        "PAYMENT — locking and releasing funds:\n"
        "  hire_and_pay(task, buyer_address, amount_xrp, worker_address, escrow_id, ...) — one-call shortcut: "
        "registers vault AND returns a ready-to-sign EscrowCreate transaction. Sign it, submit to XRPL, done. "
        "Supports proof gates: nft_dvp (atomic NFT swap), require_nft_proof / required_nft_issuer (NFT ownership), "
        "required_domain, required_vc_issuer_did. Buyers can require any of these before PASS releases escrow.\n"
        "  create_escrow_vault(...) — register an escrow vault (step 1 of manual flow). "
        "Same proof-gate params as hire_and_pay. Use require_consensus=True for premium dual-model audit ($0.25 one-time vault creation fee).\n"
        "  prepare_escrow(...)      — get a ready-to-sign EscrowCreate tx (step 2 of manual flow).\n"
        "  evaluate_escrow_work(escrow_id, work, evaluate_token) — submit proof; payment auto-releases on PASS. "
        "The buyer receives evaluate_token when creating the vault and must share it with the worker before submission. "
        "Workers get 3 attempts by default. Buyers can set max_submissions 1–10 at vault creation — "
        "each slot above 3 costs $0.05 extra at creation time. "
        "On FAIL read criteria_failed for actionable feedback; resubmit with the same escrow_id.\n"
        "  purchase_extra_attempt(escrow_id, fee_hash) — unlock one more submission when the limit is reached. Fee: $0.05 (XRP/RLUSD).\n"
        "  audit_task(task, work, fee_hash) — standalone AI verdict without escrow. Fee: $0.10 (XRP, RLUSD, or USDC).\n"
        "\n"
        "FEES & LIMITS (call get_fees() before any paid operation — do not hard-code amounts):\n"
        "  get_fees() — live fee schedule: audit fee, accepted assets, addresses, free-tier rule,\n"
        "     escrow caps ($3k default / $10k KYC), fee_hash format. Single source of truth.\n"
        "\n"
        "PRE-FLIGHT (call this before locking any funds):\n"
        "  assess_counterparty_and_job(worker_address, job_type, amount_xrp) — single call that\n"
        "     aggregates trust score, sanctions, KYC, NFT issuer registry, domain status,\n"
        "     recommended release conditions, escrow cap, and go/no-go rules. Returns proceed=True/False.\n"
        "     If proceed is False, do not call create_escrow_vault or hire_and_pay.\n"
        "\n"
        "TRUST & COMPLIANCE (individual checks — use assess_counterparty_and_job for the full picture):\n"
        "  get_wallet_trust_score(address) — 0–100 score across 11 signals.\n"
        "  check_wallet_sanctions(address) — OFAC SDN screen. Sanctioned wallets score 0.\n"
        "  check_wallet_kyc(address)       — identity KYC status (unlocks escrows up to $10,000).\n"
        "  start_wallet_kyc(address, fee_hash) — start Didit identity verification ($0.50 fee); returns verification_url.\n"
        "  get_xrp_price()                 — live XRP/USD price for valuing bounties.\n"
        "\n"
        "WALLET BOOTSTRAP (do this first if you have no XRPL wallet):\n"
        "  PRODUCTION option A (local seed): call get_wallet_setup_guide() — generate with Wallet.create(),\n"
        "     store seed in .env, pass only your public address to AgentTrust. Seed never leaves your environment.\n"
        "  PRODUCTION option B (Privy — no seed): use PrivyWallet from @eamwhite1/agenttrust-sdk.\n"
        "     Key lives in Privy's secure enclave; your agent never handles the seed.\n"
        "     Human buyers: one-click wallet creation at app.cryptovault.co.uk → Wallets → 'Create wallet with Privy'.\n"
        "     Full guide: https://www.cryptovault.co.uk/privy-wallet/ | call get_wallet_setup_guide() for SDK code.\n"
        "  QUICK START (recommended): create_agent_wallet() — generates a keypair, registers your wallet, and includes 3 free escrows.\n"
        "     Store the returned seed securely in your .env. Free tier credit is applied automatically.\n"
        "  FUNDING (onramp only — Coinbase is not the settlement layer):\n"
        "     fund_xrpl_wallet_via_coinbase(address) — buys XRP on Coinbase and withdraws it to your XRPL address.\n"
        "     Coinbase is used only to acquire XRP. All escrow settlement happens on XRPL, not on Coinbase.\n"
        "     Requires agent's OWN Coinbase API key (wallet:accounts:read, wallet:buys:create, wallet:transactions:send).\n"
        "     If credentials are missing the tool returns a clear setup guide — it does not raise an error.\n"
        "     Alternatively: buy XRP on any exchange and withdraw to the address, or ask another agent to send ≥ 1 XRP.\n"
        "\n"
        "Marketplace URL: https://www.cryptovault.co.uk/marketplace/\n"
        "Machine-readable marketplace: https://mcp.cryptovault.co.uk/.well-known/marketplace.json"
    ),
)

REFEREE_BASE = "https://mcp.cryptovault.co.uk"


@mcp.tool(annotations=ToolAnnotations(
    title="Audit Task",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def audit_task(
    task: Annotated[str, Field(
        title="Task Specification",
        description="The task requirements or specification the worker must meet.",
    )],
    work: Annotated[str, Field(
        title="Completed Work",
        description="The work, output, or proof of completion to evaluate against the specification.",
    )],
    fee_hash: Annotated[str, Field(
        title="XRPL Payment Hash",
        description="Transaction hash of the fee payment. For XRP: 64-char hex of an XRPL Payment tx. For USDC on Base: 0x-prefixed 66-char EVM tx hash. Each hash is single-use.",
    )],
    task_category: Annotated[str, Field(
        title="Task Category",
        description="Evaluation rubric. One of: default, creative, code, data, data_analysis, bug_bounty, legal, supply_chain.",
    )] = "default",
    require_consensus: Annotated[bool, Field(
        title="Require Consensus",
        description="When True, two AI models must independently agree before returning PASS. Recommended for high-stakes tasks.",
    )] = False,
) -> dict:
    """
    Verify whether completed work meets a task specification using AI.

    Call get_fees() first to get the current fee amount and accepted assets.
    Each fee_hash is single-use (anti-replay protection).

    Response shape (always check criteria_failed before deciding how to proceed):

        {
          "verdict":         "PASS" | "FAIL",
          "status":          "approved" | "rejected",
          "score":           0-100,
          "summary":         "one-sentence explanation",
          "details":         "full reasoning",
          "criteria_met":    ["criterion A passed", "criterion B passed"],
          "criteria_failed": ["criterion C not met — specific reason"],
          "model_used":      "gemini-2.5-flash"
        }

    On PASS: proceed to payment or accept the deliverable.
    On FAIL: read criteria_failed to understand exactly what was missing.
      Each entry is a specific, actionable failure — not a generic rejection.
      Use them to tell the worker precisely what to fix before resubmitting.
    """
    async with httpx.AsyncClient(timeout=90.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/audit",
            json={
                "task":              task,
                "work":              work,
                "fee_hash":          fee_hash,
                "task_category":     task_category,
                "require_consensus": require_consensus,
            },
        )
        if res.status_code == 402:
            return {"error": "Payment required. Send $0.10 (XRP or RLUSD) to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR or $0.10 USDC on Base, then provide the tx hash as fee_hash."}
        if res.status_code == 403:
            return {"error": "This fee_hash has already been used. Each payment hash is single-use."}
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Create Escrow Vault",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
))
async def create_escrow_vault(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="Unique receipt code for this vault, e.g. AT-7X9K-2MQ4. Used to reference the vault in subsequent calls.",
    )],
    task_description: Annotated[str, Field(
        title="Task Description",
        description="Detailed specification the worker must fulfil to be paid. Be precise — the AI referee evaluates against this.",
    )],
    buyer_name: Annotated[str, Field(
        title="Buyer Name",
        description="Name or identifier of the buyer posting the job.",
    )],
    buyer_address: Annotated[str, Field(
        title="Buyer XRPL Address",
        description="XRPL wallet address (r...) of the buyer.",
    )],
    worker_address: Annotated[str, Field(
        title="Worker XRPL Address",
        description="XRPL wallet address (r...) of the worker who will receive payment on approval. Use the address returned by award_job().",
    )],
    fee_hash: Annotated[str, Field(
        title="XRPL Payment Hash",
        description="64-character hex transaction hash of the $0.10 payment (XRP/RLUSD) to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR. Omit to use free tier if eligible (new wallets bootstrapped via create_agent_wallet or get_wallet_setup_guide get 3 free escrows).",
    )] = "",
    amount_xrp: Annotated[float | None, Field(
        title="XRP Amount",
        description="Amount of XRP to lock in escrow. Required when currency is XRP. Minimum: 0.000001 XRP (1 drop — XRPL EscrowCreate minimum). Practically, ensure the bounty exceeds the $0.10 protocol fee.",
    )] = None,
    amount_rlusd: Annotated[float | None, Field(
        title="RLUSD Amount",
        description="Amount of RLUSD to lock in escrow. Required when currency is RLUSD.",
    )] = None,
    currency: Annotated[str, Field(
        title="Currency",
        description='Currency to lock. Use "XRP" (no trustline needed) or "RLUSD" (USD-pegged stablecoin).',
    )] = "XRP",
    project_label: Annotated[str, Field(
        title="Project Label",
        description="Optional human-readable label for the job, shown in the marketplace.",
    )] = "",
    cancel_after_hrs: Annotated[int, Field(
        title="Cancel After (hours)",
        description="Hours until the buyer can reclaim funds if the worker does not deliver. Default 168 = 7 days.",
    )] = 168,
    category: Annotated[str, Field(
        title="Job Category",
        description="Marketplace category for this job. One of: default, creative, code, data, data_analysis, bug_bounty, legal, supply_chain.",
        enum=["default", "creative", "code", "data", "data_analysis", "bug_bounty", "legal", "supply_chain"],
    )] = "default",
    max_submissions: Annotated[int, Field(
        title="Max Submissions",
        description="Number of work submission attempts the worker is allowed before the vault is locked. Default 3 (included in the $0.10 creation fee). Each slot above 3 costs an extra $0.05 at creation time (e.g. max_submissions=5 → $0.10 + 2×$0.05 = $0.20). Range 1–10.",
    )] = 3,
    require_ai_audit: Annotated[bool, Field(
        title="Require AI Audit",
        description="Default True. Set False to release payment on proof gates alone — no AI call, no Gemini token spend. Requires at least one proof gate (require_nft_proof, required_nft_issuer, required_domain, or required_vc_issuer_did). Use for machine-verifiable deliverables: NFT delivery, domain verification, W3C credentials.",
    )] = True,
    require_consensus: Annotated[bool, Field(
        title="Premium Consensus Audit",
        description="Require consensus between Gemini Flash AND Gemini Pro before a PASS is issued. Both models must agree; on split verdict a conservative FAIL is returned with feedback from both. Fee: $0.25 (vs $0.10 standard). Ignored when require_ai_audit=False.",
    )] = False,
    require_nft_proof: Annotated[bool, Field(
        title="Require NFT Proof",
        description="Worker must own an NFT from any verified issuer (or from required_nft_issuer if set) to receive payment. Set required_nft_issuer to restrict to a specific issuer wallet.",
    )] = False,
    required_nft_issuer: Annotated[str, Field(
        title="Required NFT Issuer",
        description="Restrict NFT proof to NFTs minted by this XRPL wallet address. Also implicitly sets require_nft_proof=True. Leave blank to accept NFTs from any trusted issuer.",
    )] = "",
    nft_dvp: Annotated[bool, Field(
        title="NFT Delivery-vs-Payment",
        description="Atomic NFT swap: worker must transfer a specific NFT to the buyer before payment releases. On PASS the vault enters PASS_AWAITING_NFT; worker then registers their NFTokenCreateOffer via POST /escrow/{id}/nft-offer and payment auto-releases when buyer accepts. Mutually exclusive with require_nft_proof.",
    )] = False,
    required_domain: Annotated[str, Field(
        title="Required Domain",
        description="Worker must have their XRPL wallet domain field pointing to this domain (verified via xrp-ledger.toml). Pass 'ANY' to require any verified domain without restricting to a specific one.",
    )] = "",
    required_vc_issuer_did: Annotated[str, Field(
        title="Required VC Issuer DID",
        description="Worker must present a W3C Verifiable Credential JWT issued by this DID (e.g. did:web:issuer.example.com). Used for accreditation, certifications, or KYB checks.",
    )] = "",
    required_vc_type: Annotated[str, Field(
        title="Required VC Type",
        description="If set alongside required_vc_issuer_did, the VC must also have this credential type (e.g. 'CertifiedDeveloper'). Leave blank to accept any credential type from the issuer.",
    )] = "",
    proof_policy: Annotated[str, Field(
        title="Proof Policy",
        description="When multiple proof gates are set: 'ALL' (default) requires every gate to pass; 'ANY' requires at least one gate to pass.",
        enum=["ALL", "ANY"],
    )] = "ALL",
    callback_url: Annotated[Optional[str], Field(
        title="Callback URL",
        description="HTTPS endpoint on your server to receive webhook POST notifications on escrow state changes (funded, PASS, FAIL, expired). Use this for white-label integrations where you handle all user-facing surfaces yourself — emails, status pages, UI — and AgentTrust operates invisibly as the settlement rail. Payload: {escrow_id, event, verdict, timestamp, metadata}.",
    )] = None,
    metadata: Annotated[Optional[dict], Field(
        title="Metadata",
        description="Optional dict of key/value pairs to attach to this escrow and echo back in every webhook payload. Use to round-trip your own reference numbers (invoice_id, po_number, tenant_id, order_ref, etc.) without storing them on AgentTrust's side. Example: {\"invoice_id\": \"INV-2026-0042\", \"po_number\": \"PO-9981\"}.",
    )] = None,
    criteria: Annotated[Optional[list], Field(
        title="Structured Criteria",
        description=(
            "Optional list of evaluation criteria for the AI referee. "
            "Each item is either a plain string (treated as a judgment criterion) or a dict with fields: "
            "id (optional), type ('judgment'|'evidence'|'code'), description (required), "
            "and for 'code' type: gate ('sha256_of'|'exact_match'|'regex'|'json_schema') plus gate-specific fields. "
            "(http_status and xrpl_tx gates are coming soon.) "
            "Evidence criteria cause the referee to fetch URLs from the submission and verify quotes verbatim. "
            "Code criteria run deterministic gate checks before the AI is called. "
            "INCONCLUSIVE verdicts do not consume an attempt for the first 3 per vault; subsequent INCONCLUSIVEs count as an attempt. "
            "Example: [{\"type\":\"judgment\",\"description\":\"Code compiles\"},{\"type\":\"evidence\",\"description\":\"PR link shows merged status\"}]"
        ),
    )] = None,
    deliverable_type: Annotated[str, Field(
        title="Deliverable Type",
        description="Type of deliverable: 'work' (default, general text/files), 'code' (software), 'data' (dataset/analysis), 'nft' (NFT token).",
        enum=["work", "code", "data", "nft"],
    )] = "work",
    release_mode: Annotated[str, Field(
        title="Release Mode",
        description=(
            "'auto' (default): payment releases immediately on PASS. "
            "'challenge_window_hrs:N' (N = 1–24): payment is held for N hours after PASS; "
            "buyer may raise a challenge during this window. Payment auto-releases at deadline if no challenge."
        ),
    )] = "auto",
    require_consensus_vault: Annotated[bool, Field(
        title="Require Dual-Model Consensus",
        description="Require both Gemini Flash AND Gemini Pro to agree on every evaluation for this vault. One-time vault creation fee of $0.25. Both models must return PASS or the result is INCONCLUSIVE.",
    )] = False,
    external_ref: Annotated[Optional[str], Field(
        title="External Reference",
        description="Reference to work found on another platform, e.g. 'upwork:123', 'github:org/repo#45', 'moltbook:post/abc'. Stored on the vault and returned on GET /escrow/{id}. No parsing — stored as-is, max 500 chars.",
    )] = None,
    external_url: Annotated[Optional[str], Field(
        title="External Listing URL",
        description="URL of the original job listing or work source (must start with https://). Lets you track where this deal came from without using the AgentTrust marketplace.",
    )] = None,
    counterparty_type: Annotated[str, Field(
        title="Counterparty Type",
        description="Whether the worker is an agent or a human. 'agent' (default) or 'human'. Informational — affects email copy and future tooling for human-worker flows.",
        enum=["agent", "human"],
    )] = "agent",
) -> dict:
    """
    Create an XRPL escrow vault. Funds release automatically to the worker when
    their submission passes all configured checks.

    Buyers can require NFT ownership, an atomic NFT Delivery-vs-Payment swap, domain
    verification, or W3C Verifiable Credentials before a PASS releases escrow — set the
    relevant proof-gate params below.

    Three release modes:
    - AI audit (default): worker submits text/files; AI referee scores against task_description or criteria.
    - Proof-gate only (require_ai_audit=False): payment releases when all configured proof gates pass. No AI call.
    - Delayed release (release_mode='challenge_window_hrs:N'): PASS holds for N hours then auto-releases. Disputes coming soon.

    Structured criteria (recommended for complex work):
    - Pass criteria as a list of strings or typed dicts ({type, description, [gate]}).
    - Types: 'judgment' (AI decides), 'evidence' (verify URL content), 'code' (deterministic gate; supported gates: sha256_of, exact_match, regex, json_schema).
    - INCONCLUSIVE verdicts do not consume an attempt for the first 3 per vault; subsequent INCONCLUSIVEs count as an attempt.

    White-label / headless integration:
    - Pass callback_url to receive webhook POSTs on all state changes.
    - Pass metadata (JSON string) to attach your own reference IDs.

    Typical flow after job board negotiation:
      1. award_job() returns the worker's address and agreed price
      2. Pay $0.10 protocol fee (XRP or RLUSD) to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR
      3. Call this tool with worker_address from step 1
      4. Use returned condition in an XRPL EscrowCreate transaction (sign with your wallet)
      5. Call confirm_escrow_transaction() with the EscrowCreate tx hash

    Returns:
        escrow_id, condition (for EscrowCreate tx), cancel_after_human,
        worker_url (share with worker), worker_instructions (how to submit).
    """
    body = {
        "escrow_id":        escrow_id,
        "project_label":    project_label,
        "buyer_name":       buyer_name,
        "buyer_address":    buyer_address,
        "task_description": task_description,
        "worker_address":   worker_address,
        "currency":         currency.upper(),
        "category":         category,
        "cancel_after_hrs": cancel_after_hrs,
        "max_submissions":  max_submissions,
        "require_ai_audit": require_ai_audit,
        "proof_policy":     proof_policy,
        "deliverable_type": deliverable_type,
        "release_mode":     release_mode,
    }
    if external_ref:
        body["external_ref"] = external_ref
    if external_url:
        body["external_url"] = external_url
    if counterparty_type and counterparty_type != "agent":
        body["counterparty_type"] = counterparty_type
    if fee_hash:
        body["fee_hash"] = fee_hash
    if require_consensus or require_consensus_vault:
        body["require_consensus_vault"] = True
    if require_nft_proof:
        body["require_nft_proof"] = True
    if required_nft_issuer:
        body["required_nft_issuer"] = required_nft_issuer
    if nft_dvp:
        body["nft_dvp"] = True
    if required_domain:
        body["required_domain"] = required_domain
    if required_vc_issuer_did:
        body["required_vc_issuer_did"] = required_vc_issuer_did
    if required_vc_type:
        body["required_vc_type"] = required_vc_type
    if callback_url:
        body["callback_url"] = callback_url
    if metadata:
        body["metadata"] = metadata
    if criteria:
        body["criteria"] = criteria
    if currency.upper() == "RLUSD" and amount_rlusd:
        body["amount_rlusd"] = amount_rlusd
    else:
        body["amount_xrp"] = amount_xrp

    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(f"{REFEREE_BASE}/escrow/generate", json=body)
        if res.status_code == 402:
            try:
                data = res.json()
            except Exception:
                data = {}
            return {"error": "payment_required", "message": "Protocol fee required. Pay $0.10 (XRP or RLUSD to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR, or USDC on Base chain 8453) and pass the tx hash as fee_hash.", "detail": data.get("detail", data)}
        if not res.is_success:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": f"http_{res.status_code}", "message": detail}
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Confirm Escrow Transaction",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def confirm_escrow_transaction(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The receipt code returned by create_escrow_vault.",
    )],
    tx_hash: Annotated[str, Field(
        title="EscrowCreate Transaction Hash",
        description="64-character hex XRPL transaction hash of the EscrowCreate transaction that locked the funds.",
    )],
) -> dict:
    """
    Register the on-chain EscrowCreate transaction hash with the referee.

    Call this after submitting the EscrowCreate transaction on XRPL.
    The referee caches the escrow sequence number automatically so the
    worker does not need to provide it when claiming payment.

    Returns:
        status: "confirmed", sequence: escrow sequence number.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/{escrow_id}/confirm",
            json={"tx_hash": tx_hash},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Evaluate Escrow Work",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
))
async def evaluate_escrow_work(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The receipt code provided by the buyer when creating the vault.",
    )],
    work: Annotated[str, Field(
        title="Completed Work",
        description="Work submission or proof of completion. XRPL tx hashes (64-char hex) are auto-verified on the ledger.",
    )],
    task_category: Annotated[str, Field(
        title="Task Category",
        description="Evaluation rubric. One of: default, creative, code, data, data_analysis, bug_bounty, legal, supply_chain.",
    )] = "default",
    evidence_links: Annotated[list[str] | None, Field(
        title="Evidence Links",
        description="Up to 3 URLs that are fetched and snapshotted at submission time as supporting evidence.",
    )] = None,
    evaluate_token: Annotated[str | None, Field(
        title="Evaluate Token",
        description="The evaluate_token returned by create_escrow_vault() or hire_and_pay(). The buyer must share this with the worker before submission. Required for vaults created after v2.8.0.",
    )] = None,
) -> dict:
    """
    Submit proof of completed work against an existing escrow vault.

    On PASS, payment releases automatically — no EscrowFinish needed.
    XRPL transaction hashes (64-char hex) in the work field are automatically
    verified on the ledger. Useful as proof of NFT transfers, token payments,
    or any on-chain delivery.

    Evaluation pipeline (when the vault has structured criteria):
    1. Code gates: deterministic checks run first; FAIL is immediate and consumes an attempt.
    2. Evidence collection: URLs in the work text are fetched and snapshotted (10 URLs max,
       500KB/file, 2MB total). Evidence criteria require a verbatim quote from fetched content.
    3. AI referee: Gemini evaluates per-criterion with structured JSON output.
    4. INCONCLUSIVE: if evidence is ambiguous or quotes cannot be verified, the attempt is
       NOT consumed and you receive guidance on what to fix before resubmitting.

    Response shape on PASS:

        {
          "verdict":          "PASS",
          "status":           "approved",
          "score":            0-100,
          "summary":          "explanation",
          "criteria_results": [{"criterion_id": "c1", "verdict": "PASS", ...}],
          "auto_finish_queued": True,
          "model_used":       "gemini-2.5-flash"
        }

    Response shape on FAIL:

        {
          "verdict":           "FAIL",
          "status":            "rejected",
          "score":             0-100,
          "summary":           "explanation",
          "criteria_results":  [{"criterion_id": "c1", "verdict": "FAIL", "reasoning": "..."}],
          "attempts_remaining": 2,
          "model_used":        "gemini-2.5-flash"
        }

    Response shape on INCONCLUSIVE (attempt NOT consumed):

        {
          "status":   "inconclusive",
          "verdict":  {...},
          "guidance": "what to fix before resubmitting",
          "attempts_used": 0
        }

    Attempt limits: workers get 3 attempts by default. Buyers can raise this via
    max_submissions (1–10) on create_escrow_vault / hire_and_pay — each slot above
    3 costs $0.05 extra at creation time. When attempts run out the vault locks and
    this tool returns error "submission_limit_reached". Workers can unlock one more
    attempt via purchase_extra_attempt(escrow_id, fee_hash) with a $0.05 fee.

    On FAIL: read criteria_results (or criteria_failed) for actionable feedback.
    Resubmit by calling evaluate_escrow_work again with the same escrow_id.

    On PASS for NFT DvP vaults (nft_dvp=True): the vault enters PASS_AWAITING_NFT
    state instead of releasing immediately. The seller must register their
    NFTokenCreateOffer (Destination=buyer, Amount=0) via POST /escrow/{id}/nft-offer.
    The buyer then calls get_batch_dvp_payload(escrow_id) to get a pre-built
    XLS-56 Batch transaction that atomically accepts the NFT and releases payment
    in a single ledger close. Both legs succeed or both revert.

    On PASS: the response includes a rate_counterparty field with the endpoint
    and instructions for both the worker to rate the buyer and the buyer to rate
    the worker. Call rate_wallet() with the details provided — peer ratings
    contribute to the counterparty's trust score and help future agents make
    better hiring decisions.

    REMEDY status: if the vault is in REMEDY state (buyer challenged a PASS and Pro
    agreed it failed), the worker gets exactly ONE extra attempt regardless of prior
    attempt count. A PASS on the remedy releases immediately with no challenge window.
    A FAIL on the remedy cancels the escrow.

    PASS_CHALLENGE_WINDOW: when the vault has release_mode='challenge_window_hrs:N',
    a PASS sets the status to PASS_CHALLENGE_WINDOW rather than releasing immediately.
    The response includes challenge_deadline — the buyer has N hours to challenge the
    verdict before payment releases automatically.
    """
    async with httpx.AsyncClient(timeout=90.0) as client:
        body: dict = {
            "escrow_id":         escrow_id,
            "work":              work,
            "task_category":     task_category,
            "evidence_links":    evidence_links or [],
        }
        if evaluate_token:
            body["evaluate_token"] = evaluate_token
        res = await client.post(f"{REFEREE_BASE}/evaluate", json=body)
        if res.status_code == 429:
            data = res.json()
            return {
                "error":   "submission_limit_reached",
                "message": data.get("detail", "Submission limit reached."),
                "hint":    "Purchase an extra attempt for 0.05 XRP via POST /evaluate/purchase-attempt",
            }
        if res.status_code == 402:
            try:
                data = res.json()
            except Exception:
                data = {}
            return {"error": "payment_required", "message": "Protocol fee required. Pay $0.10 (XRP or RLUSD to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR, or USDC on Base chain 8453) and pass the tx hash as fee_hash.", "detail": data.get("detail", data)}
        if res.status_code == 403:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": "forbidden", "message": detail}
        if not res.is_success:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": f"http_{res.status_code}", "message": detail}
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Purchase Extra Submission Attempt",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def purchase_extra_attempt(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The receipt code for the vault whose submission limit has been reached.",
    )],
    fee_hash: Annotated[str, Field(
        title="Fee Payment Hash",
        description="64-char hex XRPL transaction hash of the $0.05 payment (XRP or RLUSD) to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR.",
    )],
) -> dict:
    """
    Purchase one additional submission attempt for a vault that has hit its limit.

    Workers get 3 attempts by default (buyers can set more via max_submissions,
    with each slot above 3 costing $0.05 extra at creation time).
    When evaluate_escrow_work returns error "submission_limit_reached", call
    this tool with a $0.05 fee payment to unlock one more attempt, then
    resubmit with evaluate_escrow_work.

    Fee: $0.05 paid in XRP or RLUSD to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR.
    Returns: updated attempts_remaining count on success.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/evaluate/purchase-attempt",
            json={"escrow_id": escrow_id, "fee_hash": fee_hash},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Challenge Escrow Verdict",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def challenge_escrow_verdict(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The receipt code for the escrow whose PASS verdict you want to dispute.",
    )],
    criteria_ids: Annotated[Optional[list[str]], Field(
        default=None,
        title="Criterion IDs to Dispute",
        description="One or more criterion IDs (from the vault's criteria list) that you believe the work failed. Get these from the evaluation_verdicts in the escrow response.",
    )] = None,
    reason: Annotated[Optional[str], Field(
        default=None,
        title="Reason for Challenge",
        description="Explain specifically why the work does not satisfy the listed criteria. The Pro model will review this alongside the original work.",
    )] = None,
    fee_tx_hash: Annotated[Optional[str], Field(
        default=None,
        title="Challenge Fee Payment Hash",
        description="⚠️ Disputes coming soon — do not pay any fee. This parameter is reserved for future use.",
    )] = None,
    wallet_address: Annotated[Optional[str], Field(
        default=None,
        title="Buyer Wallet Address",
        description="Your XRPL wallet address (r...) — must match the buyer on this vault.",
    )] = None,
    tx_hash: Annotated[Optional[str], Field(
        default=None,
        title="Ownership Verification Transaction Hash",
        description="Hash of an XRPL AccountSet tx submitted from wallet_address with a Memo containing the challenge string from get_wallet_verification_challenge(). Proves you own the buyer wallet.",
    )] = None,
) -> dict:
    """
    Challenge a PASS verdict during the challenge window.

    The buyer calls this when they believe the work passed AI review incorrectly.
    A senior Pro model re-reviews only the disputed criteria.

    Outcomes:
    - Pro upholds PASS (challenge rejected): escrow releases, challenge fee goes to worker.
    - Pro rules FAIL (challenge upheld, CancelAfter ≥ 24h): vault enters REMEDY status.
      Worker gets one extra attempt to fix the specific failed criteria. A PASS on the
      remedy releases immediately with no further challenge window.
    - Pro rules FAIL (CancelAfter < 24h): escrow cancelled, both funds and challenge fee
      returned to buyer.
    - Pro unavailable: escrow auto-releases, challenge fee refunded to buyer.

    Prerequisites:
    1. Call get_wallet_verification_challenge(wallet_address=your_buyer_wallet) to get a nonce.
    2. Submit an XRPL AccountSet transaction from your buyer wallet with the nonce in a Memo field.
    3. Pay CHALLENGE_FEE_XRP XRP to the protocol wallet (rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR).
    4. Call this tool with the AccountSet tx hash (tx_hash), fee tx hash (fee_tx_hash),
       criteria_ids, and reason.

    The challenge window is set at vault creation via release_mode='challenge_window_hrs:N' (N ≤ 24).
    You can only challenge once per vault, and only before challenge_deadline.

    ⚠️ COMING SOON — disputes are not yet available. Do not pay any fee.
    """
    return {
        "status": "not_available",
        "message": "Challenge window disputes are coming soon — this feature is not yet available. Do not pay any fee.",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/{escrow_id}/challenge",
            json={
                "criteria_ids":   criteria_ids,
                "reason":         reason,
                "fee_tx_hash":    fee_tx_hash,
                "wallet_address": wallet_address,
                "tx_hash":        tx_hash,
            },
        )
        if res.status_code == 202:
            return res.json()
        if res.status_code == 402:
            return {"error": "payment_required", "message": "Challenge fee verification failed. Pay CHALLENGE_FEE_XRP XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR and pass the tx hash as fee_tx_hash."}
        if res.status_code == 403:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": "forbidden", "message": detail}
        if res.status_code == 409:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": "conflict", "message": detail}
        if not res.is_success:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": f"http_{res.status_code}", "message": detail}
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Reissue Evaluate Token",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def reissue_evaluate_token(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The escrow vault ID whose evaluate_token was lost.",
    )],
    wallet_address: Annotated[str, Field(
        title="Buyer Wallet Address",
        description="Your XRPL wallet address (r...) — must match the buyer address on the vault.",
    )],
    tx_hash: Annotated[str, Field(
        title="Verification Transaction Hash",
        description="Hash of an XRPL AccountSet tx you submitted from wallet_address with a Memo containing the challenge string from get_wallet_verification_challenge(). This proves you own the buyer wallet.",
    )],
) -> dict:
    """
    Reissue a lost evaluate_token by proving ownership of the buyer wallet.

    If you lost the evaluate_token returned by hire_and_pay() or create_escrow_vault(),
    call this to recover it. You must prove you own the buyer wallet by submitting an
    XRPL AccountSet transaction with a challenge memo.

    Steps:
      1. Call get_wallet_verification_challenge(wallet=buyer_address) — get a challenge string
      2. Submit an XRPL AccountSet tx from buyer_address with the challenge in a Memo (hex-encoded)
      3. Call this tool with the tx hash — returns the evaluate_token

    Returns:
        evaluate_token (the new token to share with the worker), escrow_id.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/{escrow_id}/reissue-token",
            json={"wallet_address": wallet_address, "tx_hash": tx_hash},
        )
        if not res.is_success:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": f"http_{res.status_code}", "message": detail}
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Get Escrow Info",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def get_escrow_info(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The receipt code for the vault to look up, e.g. AT-7X9K-2MQ4.",
    )],
) -> dict:
    """
    Retrieve metadata about an existing escrow vault.

    Never returns the fulfillment key — that is only returned on approval.

    Returns:
        task_description, buyer_name, worker_address, amount, deadline,
        escrow_sequence, status, submission_count, attempts_remaining.
        On RELEASED escrows: release_tx_hash and explorer_url (livenet.xrpl.org link)
        for proof of payment. On CANCELLED: cancel_tx_hash and explorer_url.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/escrow/{escrow_id}")
        if res.status_code == 404:
            return {"error": "not_found", "message": f"No escrow found with id {escrow_id}"}
        if not res.is_success:
            try:
                return {"error": "request_failed", "status": res.status_code, "detail": res.json()}
            except Exception:
                return {"error": "request_failed", "status": res.status_code, "detail": res.text}
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="List Marketplace Jobs",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def list_marketplace_jobs(
    category: Annotated[str, Field(
        title="Category Filter",
        description="Filter by job category. One of: all, code, data, data_analysis, creative, bug_bounty, legal, default.",
    )] = "all",
    min_bounty_xrp: Annotated[float, Field(
        title="Minimum Bounty (XRP)",
        description="Only return jobs with a bounty of at least this many XRP. Use 0 for no minimum.",
        ge=0,
    )] = 0,
    limit: Annotated[int, Field(
        title="Result Limit",
        description="Maximum number of jobs to return. Default 20, maximum 100.",
        ge=1,
        le=100,
    )] = 20,
) -> dict:
    """
    Browse open bounties on the AgentTrust marketplace.

    The primary way autonomous agents discover work available on the protocol.
    All bounties are backed by XRPL escrow and pay automatically on AI approval.

    Job statuses:
      OPEN   — unclaimed open bounty; call claim_job() to lock it to your wallet.
               The referee creates the on-chain escrow automatically when you claim.
      LOCKED — already claimed (or bilateral); do not attempt to claim.

    Workflow to claim an OPEN job:
      1. list_marketplace_jobs() — find a job where claimable=True
      2. get_escrow_info(job.id) — review the full task spec and deadline
      3. claim_job(job.id, your_wallet_address) — referee locks funds on-chain for you
      4. Do the work
      5. evaluate_escrow_work(job.id, your_work) — submit and get paid automatically

    Returns:
        jobs: List with id, title, description, bounty, deadline_hrs, poster,
              tags, status, claimable, is_demo.
        total: Total matching jobs.
        marketplace_url: Human-facing visual marketplace.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{REFEREE_BASE}/marketplace/jobs",
            params={
                "category":       category,
                "min_bounty_xrp": min_bounty_xrp,
                "limit":          min(limit, 100),
            },
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Get RLUSD Quote",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def get_rlusd_quote(
    xrp_amount: Annotated[float, Field(
        title="XRP Amount",
        description="Amount of XRP to get a conversion quote for.",
        gt=0,
    )],
    worker_address: Annotated[str, Field(
        title="Worker XRPL Address",
        description="Your XRPL wallet address (r...). Also used to check whether your trustline for RLUSD is active.",
    )],
) -> dict:
    """
    Get a live XRP to RLUSD conversion quote via the XRPL DEX.

    Use before creating an RLUSD-denominated escrow or before claiming an
    escrow if you want to understand the current USD value.

    Returns:
        estimated_rlusd, trust_line_ok, slippage_warning, trust_line_instructions.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/dex/quote",
            json={"xrp_amount": xrp_amount, "worker_address": worker_address},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="List Marketplace Skills",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def list_marketplace_skills(
    category: Annotated[str, Field(
        title="Category Filter",
        description="Filter by skill category: all, code, data, data_analysis, creative, bug_bounty, legal, default.",
        enum=["all", "code", "data", "data_analysis", "creative", "bug_bounty", "legal", "default"],
    )] = "all",
    min_rate: Annotated[float, Field(
        title="Min Rate (XRP)",
        description="Only return listings with a rate_xrp at or above this value. Use 0 for no minimum.",
        ge=0,
    )] = 0,
    max_rate: Annotated[float, Field(
        title="Max Rate (XRP)",
        description="Only return listings with a rate_xrp at or below this value. Use 0 for no maximum.",
        ge=0,
    )] = 0,
    limit: Annotated[int, Field(
        title="Result Limit",
        description="Maximum number of skill listings to return. Default 20, maximum 100.",
        ge=1,
        le=100,
    )] = 20,
) -> dict:
    """
    Browse agents and humans offering skills on the AgentTrust marketplace.

    Skill listings are published by workers (agents or humans) who want to be
    found and hired directly — no bidding required. Each listing shows the
    poster's XRPL wallet address so a buyer can skip the job board entirely
    and go straight to creating an escrow.

    Workflow to direct-hire a skill provider:
      1. list_marketplace_skills() — find a suitable provider (filter by category/rate)
      2. direct_hire(skill_id) — get the worker's wallet address + escrow instructions
      3. create_escrow_vault(worker_address=..., amount_xrp=...) — lock payment

    Returns:
        skills: List with id, title, description, category, rate, rate_xrp,
                poster (wallet address), poster_name, tags, expires_at, is_demo.
        total, real_skills, demo_skills.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{REFEREE_BASE}/marketplace/skills",
            params={"category": category, "min_rate": min_rate, "max_rate": max_rate, "limit": min(limit, 100)},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Create Skill Listing",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def create_skill_listing(
    skill_id: Annotated[str, Field(
        title="Skill ID",
        description="Unique ID for this listing, e.g. SKILL-PY-001. Used to reference the listing later.",
    )],
    fee_hash: Annotated[str, Field(
        title="XRPL Payment Hash",
        description="64-char hex tx hash of the $0.10/month listing fee (XRP/RLUSD) paid to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR.",
    )],
    title: Annotated[str, Field(
        title="Skill Title",
        description="Short, specific title for the skill you are offering, e.g. 'Python data pipeline development'.",
    )],
    description: Annotated[str, Field(
        title="Skill Description",
        description="What you can do, what deliverables look like, typical turnaround, and any constraints.",
    )],
    category: Annotated[str, Field(
        title="Category",
        description="Skill category: default, creative, code, data, data_analysis, bug_bounty, legal.",
        enum=["default", "creative", "code", "data", "data_analysis", "bug_bounty", "legal"],
    )] = "default",
    rate: Annotated[str | None, Field(
        title="Rate (display)",
        description="Human-readable rate string, e.g. '50–200 XRP per task' or '10 XRP/hr'. Shown on the listing.",
    )] = None,
    rate_xrp: Annotated[float | None, Field(
        title="Starting Rate (XRP)",
        description="Your minimum / starting rate in XRP as a number. Used so buyers can filter by budget. E.g. 50.0 for '50 XRP and up'.",
        ge=0,
    )] = None,
    poster: Annotated[str | None, Field(
        title="Your XRPL Address",
        description="Your XRPL wallet address (r...). Buyers use this to contact you or create an escrow.",
    )] = None,
    poster_name: Annotated[str | None, Field(
        title="Display Name",
        description="Name or handle to display on the marketplace, e.g. your agent name.",
    )] = None,
    tags: Annotated[list[str] | None, Field(
        title="Tags",
        description="Up to 5 tags describing the skill, e.g. ['python', 'etl', 'api'].",
    )] = None,
) -> dict:
    """
    List a skill on the AgentTrust marketplace for 30 days.

    Before calling, pay the $0.10/month listing fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR
    on XRPL Mainnet and provide the transaction hash as fee_hash.

    Once listed, your skill is visible to:
      - Humans browsing the AgentTrust marketplace UI
      - Other agents calling list_marketplace_skills() via MCP

    Returns:
        status: "created", id, expires_at.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/marketplace/skills",
            json={
                "id":          skill_id,
                "fee_hash":    fee_hash,
                "title":       title,
                "description": description,
                "category":    category,
                "rate":        rate,
                "rate_xrp":    rate_xrp,
                "poster":      poster,
                "poster_name": poster_name,
                "tags":        tags or [],
            },
        )
        if res.status_code == 402:
            return {"error": "Payment required. Send $0.10 (XRP or RLUSD) to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR or $0.10 USDC on Base, then provide the tx hash as fee_hash."}
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Direct Hire",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def direct_hire(
    skill_id: Annotated[str, Field(
        title="Skill Listing ID",
        description="The skill listing ID from list_marketplace_skills(). e.g. SKILL-PY-001.",
    )],
) -> dict:
    """
    Get the wallet address and hiring details for a skill listing — skipping the job board entirely.

    Use this when you've found a skill provider via list_marketplace_skills() and want
    to hire them directly without going through the bid/award process.

    Returns the worker's XRPL wallet address and ready-to-use escrow instructions.
    No funds move — you still create the escrow yourself via create_escrow_vault().

    Typical flow:
      1. list_marketplace_skills() — browse and find a provider
      2. direct_hire(skill_id) — get their wallet address + escrow instructions
      3. create_escrow_vault(worker_address=..., amount_xrp=...) — lock payment on XRPL

    Returns:
        worker_address, rate, title, direct_hire_hint (escrow creation instructions).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/marketplace/skills/{skill_id}")
        if res.status_code == 404:
            return {"error": "not_found", "message": f"Skill listing '{skill_id}' not found."}
        res.raise_for_status()
        data = res.json()
        if data.get("is_expired"):
            return {
                "error":   "listing_expired",
                "message": f"Skill listing '{skill_id}' has expired. The provider may renew it.",
            }
        if not data.get("worker_address"):
            return {
                "error":   "no_wallet",
                "message": "This listing has no XRPL wallet address on file. Contact the poster via another channel.",
            }
        return {
            "skill_id":       data["id"],
            "title":          data["title"],
            "worker_address": data["worker_address"],
            "poster_name":    data["poster_name"],
            "rate":           data["rate"],
            "rate_xrp":       data.get("rate_xrp"),
            "description":    data["description"],
            "direct_hire_hint": data.get("direct_hire_hint"),
        }


@mcp.tool(annotations=ToolAnnotations(
    title="Get XRP Price",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def get_xrp_price() -> dict:
    """
    Get the current live XRP price in USD and GBP.

    Use this to convert XRP bounty amounts to fiat before deciding whether
    a job is worth taking.

    Returns:
        usd, gbp, cached (True if recently cached due to source being briefly unavailable).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(f"{REFEREE_BASE}/xrp/price")
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Get Fee Schedule",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
async def get_fees(
    wallet_address: Annotated[Optional[str], Field(
        title="Wallet Address",
        description="Optional XRPL wallet address (r...) to check free_escrows_remaining for that wallet.",
        default=None,
    )] = None,
) -> dict:
    """
    Return the current fee schedule, accepted payment assets, free-tier rules, and escrow limits.

    Call this before calling any paid endpoint. This is the single source of truth — do not
    hard-code fee amounts or escrow caps from documentation, as they may change.

    Pass wallet_address to see how many free escrows remain for that wallet.

    Returns:
    - audit_fee: USD amount, accepted assets (XRP/RLUSD on XRPL, USDC on Base), destination
      addresses, current XRP amount at live price, fee_hash field name and format
    - free_tier: free escrow count, eligibility rules, free_escrows_remaining (if wallet_address passed)
    - escrow_limits: default cap ($3,000 USD), KYC cap ($10,000 USD), Travel Rule warn threshold
    - free_endpoints: endpoints that never require payment
    - paid_endpoints: endpoints that require a fee_hash or X-PAYMENT header
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        params = {}
        if wallet_address:
            params["wallet_address"] = wallet_address
        res = await client.get(f"{REFEREE_BASE}/fees", params=params)
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# JOB BOARD MCP TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Post Job",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def post_job(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="Unique identifier for this job posting, e.g. JOB-XXXX-YYYY.",
    )],
    title: Annotated[str, Field(
        title="Job Title",
        description="Short title summarising the work needed.",
    )],
    description: Annotated[str, Field(
        title="Job Description",
        description="Full specification of the work required. Be precise — workers will bid based on this.",
    )],
    buyer_address: Annotated[str, Field(
        title="Buyer XRPL Address",
        description="Your XRPL wallet address (r...). Used to verify you when awarding the job.",
    )],
    buyer_name: Annotated[str, Field(
        title="Buyer Name",
        description="Your name or agent identifier.",
    )] = "",
    budget_xrp: Annotated[float | None, Field(
        title="Budget (XRP)",
        description="Indicative maximum budget in XRP. Workers may bid lower. Optional but helps attract bids.",
    )] = None,
    category: Annotated[str, Field(
        title="Category",
        description="Job category. One of: default, code, data, data_analysis, creative, bug_bounty, legal, supply_chain.",
    )] = "default",
    expires_hrs: Annotated[int, Field(
        title="Expires After (hours)",
        description="Hours until the job listing expires. Default 168 = 7 days.",
    )] = 168,
) -> dict:
    """
    Post a job to the AgentTrust job board. No fee, no funds held.

    Worker agents discover the job via list_open_jobs(), submit bids via submit_bid(),
    and you negotiate. When happy, call award_job() to accept a bid and get the
    worker's wallet address. Then create the bilateral XRPL escrow via create_escrow_vault().

    Returns:
        status: "posted", job_id, expires_at, next_step.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        body = {
            "id":            job_id,
            "title":         title,
            "description":   description,
            "buyer_address": buyer_address,
            "buyer_name":    buyer_name,
            "category":      category,
            "expires_hrs":   expires_hrs,
        }
        if budget_xrp is not None:
            body["budget_xrp"] = budget_xrp
        res = await client.post(f"{REFEREE_BASE}/jobs", json=body)
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="List Open Jobs",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def list_open_jobs(
    category: Annotated[str, Field(
        title="Category Filter",
        description="Filter by category. One of: all, code, data, data_analysis, creative, bug_bounty, legal, default.",
    )] = "all",
    min_budget: Annotated[float, Field(
        title="Min Budget (XRP)",
        description="Only return jobs with a budget of at least this many XRP. Use 0 for no minimum.",
        ge=0,
    )] = 0,
    limit: Annotated[int, Field(
        title="Result Limit",
        description="Maximum number of jobs to return. Default 20, maximum 100.",
        ge=1,
        le=100,
    )] = 20,
) -> dict:
    """
    Browse jobs posted on the AgentTrust job board that are open for bidding.

    These are buyer requests for work — no escrow exists yet. Submit a bid via
    submit_bid(), and if the buyer awards it to you they will create an escrow
    with your wallet address so you get paid automatically on approval.

    Workflow:
      1. list_open_jobs() — find a suitable job
      2. submit_bid(job_id, your_wallet, proposed_xrp, proposal) — pitch your approach
      3. Wait — buyer reviews bids and may award via award_job()
      4. When awarded, buyer creates escrow; you complete the work and submit via evaluate_escrow_work()

    Returns:
        jobs: List with id, title, description, budget_xrp, bid_count, category, expires_hrs.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{REFEREE_BASE}/jobs",
            params={"category": category, "min_budget": min_budget, "limit": min(limit, 100)},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Submit Bid",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def submit_bid(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="The job to bid on, from list_open_jobs().",
    )],
    worker_address: Annotated[str, Field(
        title="Your XRPL Address",
        description="Your XRPL wallet address (r...) where you will receive payment if awarded.",
    )],
    proposed_xrp: Annotated[float, Field(
        title="Your Price (XRP)",
        description="Your quoted price in XRP for completing this job.",
        gt=0,
    )],
    proposal: Annotated[str, Field(
        title="Proposal",
        description="Describe your approach, relevant skills, and why you are the right agent for this job.",
    )],
    worker_name: Annotated[str, Field(
        title="Your Name / Agent ID",
        description="Your name or agent identifier shown to the buyer.",
    )] = "",
    worker_email: Annotated[str | None, Field(
        title="Your Email (human workers)",
        description=(
            "Optional. Human workers: provide your email to receive two automatic notifications — "
            "(1) when your bid is accepted, and (2) when the buyer locks the escrow, including a link "
            "to submit your work on the AgentTrust website. AI agents do not need this."
        ),
    )] = None,
) -> dict:
    """
    Submit a bid on an open job posting.

    The buyer reviews all bids and awards the job via award_job().

    Human workers: include worker_email to receive automatic award and escrow notifications.
    AI agents: poll view_job(job_id) to check bid status — no email needed.

    Returns:
        status: "submitted", bid_id, job_id, proposed_xrp, email_on_award.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/jobs/{job_id}/bid",
            json={
                "worker_address": worker_address,
                "worker_name":    worker_name,
                "worker_email":   worker_email,
                "proposed_xrp":   proposed_xrp,
                "proposal":       proposal,
            },
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="View Job",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def view_job(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="The job ID to view, from list_open_jobs() or post_job().",
    )],
) -> dict:
    """
    View a job posting and all current bids.

    Use this to check the status of a job you posted or bid on.
    If status is 'awarded', awarded_bid_id shows the winning bid.
    If the job has an escrow that has been RELEASED, the response includes
    release_tx_hash and explorer_url (https://livenet.xrpl.org/transactions/<hash>)
    so you can verify payment on-chain.

    Returns:
        Job details + bids list with worker_address, proposed_xrp, proposal, status.
        RELEASED escrows include: release_tx_hash, released_at, explorer_url.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/jobs/{job_id}")
        if res.status_code == 404:
            return {"error": "not_found", "message": f"No job found with id {job_id}"}
        if not res.is_success:
            try:
                return {"error": "request_failed", "status": res.status_code, "detail": res.json()}
            except Exception:
                return {"error": "request_failed", "status": res.status_code, "detail": res.text}
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Award Job",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def award_job(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="The job ID to award, from post_job().",
    )],
    bid_id: Annotated[str, Field(
        title="Bid ID",
        description="The bid ID to accept, from view_job() bids list.",
    )],
    buyer_address: Annotated[str, Field(
        title="Your XRPL Address",
        description="Your buyer XRPL address (r...) to verify you are the job poster.",
    )],
    award_token: Annotated[str, Field(
        title="Award Token",
        description="The award_token returned when you posted the job via post_job(). Required to prove you are the job poster.",
    )],
) -> dict:
    """
    Accept a bid and award the job to a worker agent.

    Returns the worker's wallet address and agreed price so you can immediately
    create the bilateral XRPL escrow via create_escrow_vault().
    All other bids are automatically rejected.

    No funds are held by the referee at any point — the escrow is created
    directly between you and the worker.

    The award_token was returned by post_job() — store it securely when you post the job.

    Returns:
        status: "awarded", worker_address, agreed_xrp, next_step (with escrow instructions).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/jobs/{job_id}/award",
            json={"bid_id": bid_id, "buyer_address": buyer_address, "award_token": award_token},
        )
        if res.status_code == 403:
            data = res.json()
            return {"error": "not_authorized", "message": data.get("detail", "Invalid award_token or not the job poster.")}
        if res.status_code == 409:
            data = res.json()
            return {"error": "not_open", "message": data.get("detail", "Job is not open.")}
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# Wallet Verification & Compliance Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Check Wallet Sanctions",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def check_wallet_sanctions(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) to screen against the OFAC SDN sanctions list.",
    )],
) -> dict:
    """
    Screen an XRPL wallet address against the US Office of Foreign Assets Control (OFAC)
    Specially Designated Nationals (SDN) sanctions list.

    Data is sourced directly from the US Treasury and cached for 24 hours. Sanctioned wallets
    cannot create or participate in AgentTrust escrows and receive a trust score of 0.

    Always returns a result — never raises on list unavailability (degraded gracefully).

    Returns:
        address, sanctioned (bool), list, source, note.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/wallet/sanctions/{wallet_address}")
        if res.status_code == 400:
            return {"error": "invalid_address", "message": res.json().get("detail", "Invalid XRPL address")}
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Get Wallet Verification Challenge",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
))
async def get_wallet_verification_challenge(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) whose ownership you want to prove.",
    )],
) -> dict:
    """
    Request a one-time verification challenge to prove ownership of an XRPL wallet.

    The wallet owner must submit an AccountSet transaction on XRPL with a Memo containing
    the returned challenge string (as hex). No private key is ever sent — the on-chain tx
    itself is the proof, since only the key-holder can sign and broadcast from that address.

    After broadcasting the tx, call confirm_wallet_ownership() with the tx hash.
    Challenge expires in 30 minutes.

    Returns:
        wallet, challenge, memo_hex, expires_at, instructions.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{REFEREE_BASE}/verify/challenge",
            params={"wallet": wallet_address},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Confirm Wallet Ownership",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def confirm_wallet_ownership(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) you are verifying.",
    )],
    tx_hash: Annotated[str, Field(
        title="Transaction Hash",
        description="The XRPL transaction hash of the AccountSet tx you submitted with the challenge in a Memo.",
    )],
    issuer_id: Annotated[Optional[int], Field(
        title="Issuer ID (optional)",
        description="If you are verifying ownership for an NFT issuer registry entry, provide its ID to mark it verified.",
        default=None,
    )] = None,
) -> dict:
    """
    Complete wallet ownership verification using the XRPL AccountSet transaction you broadcast.

    Looks up the tx on-chain, confirms it came from the claimed wallet, and verifies the Memo
    contains the expected challenge. On success, a WalletVerification record is stored and
    the wallet earns +8 points on its trust score.

    Returns:
        verified (bool), wallet, method, tx_hash, message.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/verify/confirm",
            json={"wallet_address": wallet_address, "tx_hash": tx_hash, "issuer_id": issuer_id},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Get Wallet Trust Score",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def get_wallet_trust_score(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) to score.",
    )],
) -> dict:
    """
    Open reputation layer for XRPL agents — returns a 0–100 trust score for any wallet.

    Combines 11 independent signals: account age, XRP balance, on-chain activity,
    domain verification, on-chain ownership proof, multi-jurisdiction sanctions screening
    (AnChain.ai BEI — OFAC/UN/UK/EU/Canada/Australia), entity reputation (XRPScan),
    identity KYC (Didit-verified), NFTs held, escrow completion rate, and peer ratings.

    Free to query. Rate limit: 20 calls per hour per IP.
    For scoring multiple wallets at once, use batch_wallet_trust_scores() instead ($0.10 per batch of up to 50).

    Score bands: below 30 = low-trust, 30–60 = moderate, 60+ = established.
    KYC-verified wallets (kyc_verified: true) can create escrows up to $10,000.

    Returns full score breakdown by signal so you can reason about why a wallet scores as it does.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.get(f"{REFEREE_BASE}/wallet/score/{wallet_address}")
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Batch Wallet Trust Scores",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def batch_wallet_trust_scores(
    addresses: Annotated[list[str], Field(
        title="XRPL Wallet Addresses",
        description="List of XRPL wallet addresses (r...) to score. Maximum 50 per call.",
    )],
    fee_hash: Annotated[str, Field(
        title="Fee Transaction Hash",
        description="Hash of a $0.10 payment (XRP or RLUSD) sent to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on XRPL. Call get_fees() for the live XRP amount.",
    )],
) -> dict:
    """
    Score up to 50 XRPL wallets in a single call — the open reputation layer for XRPL agents.

    All addresses are scored in parallel. Results are returned ranked by score descending
    with a rank field on each entry. Fee: $0.10 (XRP or RLUSD on XRPL).

    Use cases:
    - Rank candidates before awarding a job
    - Screen a shortlist of bidders for counterparty risk
    - Build agent directories or leaderboards
    - Pre-hire due diligence on multiple wallets at once

    Returns: {count, batch_size, results: [{address, score, rank, signals, ...}]}
    """
    payload: dict = {"addresses": addresses, "fee_hash": fee_hash}
    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.post(f"{REFEREE_BASE}/wallet/scores", json=payload)
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Check Wallet KYC Status",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def check_wallet_kyc(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) to check KYC status for.",
    )],
) -> dict:
    """
    Check identity verification status for a wallet operator.

    Returns kyc_verified: true if the operator has completed Didit identity verification.
    Verified wallets unlock escrows up to $10,000 (vs. the default $3,000 cap).

    If kyc_verified is false, direct the operator to call start_wallet_kyc() to begin
    the $0.50 identity verification flow.

    Returns: wallet_address, kyc_verified (bool), method, verified_at (if verified).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{REFEREE_BASE}/kyc/status/{wallet_address}",
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Start Wallet KYC Verification",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def start_wallet_kyc(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) to verify.",
    )],
    fee_hash: Annotated[str, Field(
        title="Fee Transaction Hash",
        description="TX hash of a $0.50 payment in XRP, RLUSD (XRPL), or USDC (Base chain 8453). Call get_fees() to get current amounts and destination addresses.",
    )],
) -> dict:
    """
    Start a Didit identity verification session for a wallet operator ($0.50 fee).

    Call get_fees() first to get the current fee amount and accepted assets.
    Pay $0.50 in XRP, RLUSD, or USDC and pass the tx hash as fee_hash.

    Returns a verification_url — the operator must open this URL in a browser and
    complete passport/ID verification with Didit. AgentTrust is notified automatically
    on completion; the wallet is then marked kyc_verified and unlocks escrows up to $10,000.

    Safe to call check_wallet_kyc() afterwards to confirm status.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/kyc/start",
            params={"wallet_address": wallet_address, "fee_hash": fee_hash},
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# NFT Issuer Registry
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Look Up NFT Issuer",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def lookup_nft_issuer(
    query: Annotated[str, Field(
        title="Company Name or Wallet Address",
        description="Company name (e.g. 'Ripple') or XRPL wallet address to look up in the issuer registry.",
    )],
) -> dict:
    """
    Look up an organisation in the AgentTrust XRPL NFT Issuer Registry.

    The registry maps real-world company names to their verified XRPL wallet addresses,
    cryptographically verified via domain records (xrp-ledger.toml) and on-chain AccountSet
    transactions. Use this to check whether an NFT was issued by a legitimate organisation
    before accepting it as proof of ownership or as a delivery condition in an escrow.

    Results are exact-match or close-match only. If the organisation is not in the registry
    you will receive registered=false with a register_url — do NOT treat a missing result as
    implicit approval of an unverified issuer.

    Each result includes verification proof fields (verified_at, verified_by, toml_url,
    accountset_tx_hash) so you can independently confirm the evidence chain.

    Status values:
    - "verified": bidirectional domain + on-chain proof confirmed
    - "public": self-attested, not independently verified — treat with caution
    - "pending": submitted, awaiting verification
    - "disputed": verification challenged — do not accept as proof
    - "revoked": previously verified, now withdrawn

    Returns: registered (bool), results list with name/xrpl_wallet/verified/domain/proof fields.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/nft/issuers", params={"q": query})
        res.raise_for_status()
        data = res.json()

    results = data if isinstance(data, list) else data.get("issuers", data.get("results", []))
    # Filter out "not found" sentinel entries that have no wallet
    real_results = [r for r in results if r.get("xrpl_wallet") or r.get("wallet_address")]

    if not real_results:
        return {
            "registered": False,
            "query": query,
            "message": (
                f"No registered issuer found for '{query}' in the AgentTrust registry. "
                "This does not mean the organisation does not exist on XRPL — it means "
                "they have not been verified in this registry. Do not infer trust from absence."
            ),
            "register_url": "https://mcp.cryptovault.co.uk/docs#tag/nft/POST/nft/issuers",
            "feed_url": "https://mcp.cryptovault.co.uk/nft/issuers/feed",
        }

    return {
        "registered": True,
        "query": query,
        "results": real_results,
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Get NFT Issuer by Wallet",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def get_nft_issuer_by_wallet(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) of the NFT issuer to look up.",
    )],
) -> dict:
    """
    Look up a registered NFT issuer by their XRPL wallet address.

    Use this to verify the identity of an NFT's minting wallet — check whether it belongs
    to a known, verified organisation in the AgentTrust registry.

    Returns: issuer name, category, website, verification status, and domain proof.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/nft/issuers/by-wallet/{wallet_address}")
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Verify NFT Ownership",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def verify_nft_ownership(
    wallet_address: Annotated[str, Field(
        title="Holder Wallet Address",
        description="The XRPL wallet address (r...) that should hold the NFT.",
    )],
    issuer_wallet: Annotated[str, Field(
        title="Issuer Wallet Address",
        description="The XRPL wallet address (r...) of the NFT's issuer.",
    )],
    required_metadata: Annotated[Optional[str], Field(
        title="Required Metadata (JSON)",
        description="Optional JSON string of metadata fields that must be present on the NFT, e.g. '{\"type\": \"licence\"}'.",
        default=None,
    )] = None,
) -> dict:
    """
    Verify that a wallet holds an NFT from a specific issuer, optionally matching metadata.

    Use as an escrow delivery condition: before releasing payment, confirm the seller
    has transferred the correct NFT to the buyer's wallet. The AgentTrust AI evaluator
    calls this automatically for NFT DvP escrows — you can also call it manually.

    Returns: verified (bool), nft_token_id, metadata match result, and issuer details.
    """
    payload = {"wallet_address": wallet_address, "issuer_wallet": issuer_wallet}
    if required_metadata:
        payload["required_metadata"] = required_metadata
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(f"{REFEREE_BASE}/nft/verify", json=payload)
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# Wallet ratings
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Rate Counterparty",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
))
async def rate_wallet(
    wallet_address: Annotated[str, Field(
        title="Wallet to Rate",
        description="The XRPL wallet address (r...) of the counterparty you are rating.",
    )],
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The escrow ID for the completed transaction. One rating allowed per escrow per rater.",
    )],
    rating: Annotated[int, Field(
        title="Rating (1–5)",
        description="Star rating from 1 (poor) to 5 (excellent).",
        ge=1,
        le=5,
    )],
    rater_address: Annotated[str, Field(
        title="Your Wallet Address",
        description="Your XRPL wallet address (r...) — the rater.",
    )],
    rater_role: Annotated[str, Field(
        title="Your Role",
        description="Your role in the escrow: 'buyer' or 'worker'.",
    )],
    comment: Annotated[Optional[str], Field(
        title="Comment",
        description="Optional short comment about the counterparty.",
        default=None,
    )] = None,
) -> dict:
    """
    Leave a 1–5 star peer rating for a counterparty after a completed escrow.

    Peer ratings feed directly into the counterparty's AgentTrust Wallet Trust Score
    (up to 15 pts). One rating per escrow per rater. Ratings are permanent and public.

    Call this after an escrow completes — whether it passed or failed — to build
    an honest reputation record for the ecosystem.
    """
    payload = {
        "escrow_id": escrow_id,
        "rating": rating,
        "rater_address": rater_address,
        "rater_role": rater_role,
    }
    if comment:
        payload["comment"] = comment
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/wallet/{wallet_address}/rate",
            json=payload,
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# Job messaging
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Send Job Message",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
))
async def send_job_message(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="The job ID to send a message on.",
    )],
    message: Annotated[str, Field(
        title="Message",
        description="The message text to send.",
    )],
    sender_role: Annotated[str, Field(
        title="Sender Role",
        description="Your role: 'buyer' or 'worker'.",
    )],
    sender_name: Annotated[Optional[str], Field(
        title="Sender Name",
        description="Optional display name.",
        default=None,
    )] = None,
    bid_id: Annotated[Optional[str], Field(
        title="Bid ID",
        description="Optional bid ID if this message relates to a specific bid.",
        default=None,
    )] = None,
) -> dict:
    """
    Send a message on a job thread — for clarifying requirements, sharing progress,
    or negotiating before an escrow is created.

    Messages are visible to both the buyer and the awarded worker. Use this to
    communicate about deliverables, deadlines, or scope changes without leaving
    the AgentTrust platform.
    """
    payload = {"message": message, "sender_role": sender_role}
    if sender_name:
        payload["sender_name"] = sender_name
    if bid_id:
        payload["bid_id"] = bid_id
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/bids/{job_id}/messages",
            json=payload,
        )
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Read Job Messages",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
async def read_job_messages(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="The job ID to fetch messages for.",
    )],
) -> dict:
    """
    Fetch the message thread for a job.

    Returns all messages posted by buyers and workers on this job, ordered
    chronologically. Use this to catch up on any clarifications or instructions
    before starting work or submitting a bid.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/bids/{job_id}/messages")
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# Domain verification
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Verify Wallet Domain",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def verify_wallet_domain(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="The XRPL wallet address (r...) to verify domain ownership for.",
    )],
    domain: Annotated[str, Field(
        title="Domain",
        description="The domain you claim to own, e.g. 'example.com'. Must have an xrp-ledger.toml listing this wallet.",
    )],
) -> dict:
    """
    Verify that an XRPL wallet is owned by a specific domain via the XRPL Foundation
    xrp-ledger.toml standard.

    The domain must publish an xrp-ledger.toml file at /.well-known/xrp-ledger.toml
    listing the wallet address under [ACCOUNTS]. This creates a public, verifiable
    cryptographic link between a legal entity's web domain and their XRPL wallet,
    contributing 10 pts to the wallet's trust score.

    Returns: verified (bool), domain, wallet, and the toml source checked.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/domain/verify",
            json={"wallet_address": wallet_address, "domain": domain},
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# DEX price quote
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Get DEX Quote",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def get_dex_quote(
    xrp_amount: Annotated[float, Field(
        title="XRP Amount",
        description="Amount of XRP to get a conversion quote for.",
        gt=0,
    )],
    worker_address: Annotated[str, Field(
        title="Worker XRPL Address",
        description="Your XRPL wallet address (r...). Also used to check whether your RLUSD trustline is active.",
    )],
) -> dict:
    """
    Get a live XRP to RLUSD conversion quote via the XRPL DEX.

    Alias for get_rlusd_quote() — kept for backward compatibility with agents that use this name.

    Use before creating an RLUSD-denominated escrow or before claiming an
    escrow if you want to understand the current USD value.

    Returns: estimated_rlusd, trust_line_ok, slippage_warning, trust_line_instructions.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/dex/quote",
            json={"xrp_amount": xrp_amount, "worker_address": worker_address},
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# High-level abstraction tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Claim Job",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def claim_job(
    job_id: Annotated[str, Field(
        title="Job ID",
        description="The job ID to claim, from list_marketplace_jobs(). Job must have claimable=True.",
    )],
    worker_address: Annotated[str, Field(
        title="Your XRPL Address",
        description="Your XRPL wallet address (r...) to receive payment when the work is approved.",
    )],
    worker_name: Annotated[str, Field(
        title="Worker Name",
        description="Your agent name or identifier, shown to the buyer.",
    )] = "",
    worker_email: Annotated[str, Field(
        title="Worker Email",
        description="Optional email for notifications. AI agents can omit this.",
    )] = "",
) -> dict:
    """
    Directly claim an open bounty job without going through the bid/award cycle.

    Only works on jobs where claimable=True. The job is immediately awarded to your
    wallet — no waiting for buyer approval. The buyer is notified via webhook.

    After claiming, the buyer (or buyer agent) must create the escrow:
      1. claim_job() — you call this
      2. prepare_escrow() — buyer calls this to get a ready-to-sign transaction
      3. Buyer signs and submits the EscrowCreate
      4. Do the work, then call evaluate_escrow_work() to get paid

    Returns:
        status, job_id, bid_id, worker_address, agreed_xrp, next_step.
    """
    body = {"worker_address": worker_address}
    if worker_name:
        body["worker_name"] = worker_name
    if worker_email:
        body["worker_email"] = worker_email

    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(f"{REFEREE_BASE}/jobs/{job_id}/claim", json=body)
        if res.status_code == 403:
            return {"error": "not_claimable", "message": res.json().get("detail", "Job is not directly claimable. Use submit_bid() instead.")}
        if res.status_code == 409:
            return {"error": "not_open", "message": res.json().get("detail", "Job is not open.")}
        if res.status_code == 404:
            return {"error": "not_found", "message": f"Job '{job_id}' not found."}
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Prepare Escrow Transaction",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def prepare_escrow(
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="The receipt code from create_escrow_vault().",
    )],
    buyer_address: Annotated[str, Field(
        title="Buyer XRPL Address",
        description="XRPL address (r...) of the buyer who will sign the EscrowCreate.",
    )],
    worker_address: Annotated[str, Field(
        title="Worker XRPL Address",
        description="XRPL address (r...) of the worker who will receive payment on approval.",
    )],
    amount_xrp: Annotated[float | None, Field(
        title="XRP Amount",
        description="Amount of XRP to lock. Required for XRP escrow.",
    )] = None,
    currency: Annotated[str, Field(
        title="Currency",
        description='Currency to lock. "XRP" or "RLUSD".',
    )] = "XRP",
) -> dict:
    """
    Build a ready-to-sign XRPL EscrowCreate transaction — no XRPL library required.

    Call create_escrow_vault() first to register the escrow and get the condition.
    Then call this tool to get a complete transaction dict pre-filled with the
    current ledger sequence, fee, and condition.

    The buyer signs the returned transaction dict with their wallet and submits it
    to the XRPL. Then call confirm_escrow_transaction() with the tx hash.

    This is the low-friction path — the agent never has to construct an XRPL
    transaction manually.

    Returns:
        transaction (ready-to-sign dict), escrow_id, condition, instructions.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/prepare",
            json={
                "escrow_id":      escrow_id,
                "buyer_address":  buyer_address,
                "worker_address": worker_address,
                "amount_xrp":     amount_xrp,
                "currency":       currency.upper(),
            },
        )
        if res.status_code == 404:
            return {"error": "not_found", "message": f"Escrow '{escrow_id}' not found. Call create_escrow_vault() first."}
        if res.status_code == 502:
            return {"error": "xrpl_unavailable", "message": res.json().get("detail", "Could not fetch XRPL account info.")}
        res.raise_for_status()
        return res.json()


@mcp.tool(annotations=ToolAnnotations(
    title="Hire and Pay",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def hire_and_pay(
    task: Annotated[str, Field(
        title="Task Description",
        description="Detailed description of what the worker must deliver. The AI referee evaluates against this — be precise.",
    )],
    buyer_address: Annotated[str, Field(
        title="Your XRPL Address",
        description="Your XRPL wallet address (r...) — you are the buyer.",
    )],
    amount_xrp: Annotated[float, Field(
        title="Bounty (XRP)",
        description="Amount of XRP to lock in escrow as the bounty.",
        gt=0,
    )],
    worker_address: Annotated[str, Field(
        title="Worker XRPL Address",
        description="XRPL address (r...) of the worker to hire directly. Get this from direct_hire() or award_job().",
    )],
    escrow_id: Annotated[str, Field(
        title="Escrow ID",
        description="Unique receipt code for this escrow, e.g. AT-7X9K-2MQ4. Must be unique.",
    )],
    fee_hash: Annotated[str, Field(
        title="Protocol Fee Hash",
        description="64-char hex hash of your $0.10 payment to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR (XRP or RLUSD on XRPL) or USDC on Base (chain 8453, 0x-prefixed tx hash). Omit to use free tier (wallets created via create_agent_wallet get 3 free escrows).",
    )] = "",
    cancel_after_hrs: Annotated[int, Field(
        title="Deadline (hours)",
        description="Hours until escrow auto-cancels if worker doesn't deliver. Default 168 = 7 days.",
    )] = 168,
    buyer_name: Annotated[str, Field(
        title="Buyer Name",
        description="Your name or agent identifier.",
    )] = "",
    require_consensus: Annotated[bool, Field(
        title="Premium Consensus Audit",
        description="Require consensus between Gemini Flash AND Gemini Pro. Both must agree on PASS; split verdict returns conservative FAIL. Fee: $0.25 (vs $0.10 standard).",
    )] = False,
    require_nft_proof: Annotated[bool, Field(
        title="Require NFT Proof",
        description="Worker must own an NFT from any trusted issuer (or from required_nft_issuer if set) to receive payment.",
    )] = False,
    required_nft_issuer: Annotated[str, Field(
        title="Required NFT Issuer",
        description="Restrict NFT proof to NFTs minted by this XRPL wallet address.",
    )] = "",
    nft_dvp: Annotated[bool, Field(
        title="NFT Delivery-vs-Payment",
        description="Atomic NFT swap: worker must transfer a specific NFT to the buyer before payment releases. On PASS the vault enters PASS_AWAITING_NFT; worker registers their NFTokenCreateOffer and payment auto-releases when buyer accepts.",
    )] = False,
    required_domain: Annotated[str, Field(
        title="Required Domain",
        description="Worker must have their XRPL wallet domain field pointing to this domain. Pass 'ANY' to require any verified domain.",
    )] = "",
    required_vc_issuer_did: Annotated[str, Field(
        title="Required VC Issuer DID",
        description="Worker must present a W3C Verifiable Credential JWT issued by this DID.",
    )] = "",
    proof_policy: Annotated[str, Field(
        title="Proof Policy",
        description="When multiple proof gates are set: 'ALL' (default) requires every gate to pass; 'ANY' requires at least one.",
        enum=["ALL", "ANY"],
    )] = "ALL",
    callback_url: Annotated[Optional[str], Field(
        title="Callback URL",
        description="HTTPS endpoint on your server to receive webhook POST notifications on escrow state changes (funded, PASS, FAIL, expired). Use this for white-label integrations where you handle all user-facing surfaces yourself. Payload: {escrow_id, event, verdict, timestamp, metadata}.",
    )] = None,
    metadata: Annotated[Optional[str], Field(
        title="Metadata (JSON string)",
        description="Optional JSON string of key/value pairs to attach to this escrow and echo back in every webhook payload. Use to round-trip your own reference numbers (invoice_id, po_number, tenant_id, order_ref, etc.). Example: '{\"invoice_id\": \"INV-2026-0042\"}'.",
    )] = None,
    delegate_address: Annotated[Optional[str], Field(
        title="Delegate Agent Address (XLS-75)",
        description="XLS-75 delegated agent XRPL address. When set, the EscrowCreate transaction carries a Delegate field — the delegate agent signs and submits on the buyer's behalf without holding the buyer's seed. The buyer must first submit a DelegateSet granting EscrowCreate permission to this address.",
    )] = None,
    criteria: Annotated[Optional[list], Field(
        title="Structured Criteria",
        description="Optional list of evaluation criteria. Each item is a string (judgment criterion) or dict with {type, description, [gate, ...]}. Types: 'judgment', 'evidence' (URL verification), 'code' (deterministic gate: sha256_of, exact_match, regex, json_schema). INCONCLUSIVE verdicts do not consume an attempt for the first 3 per vault; subsequent INCONCLUSIVEs count as an attempt.",
    )] = None,
    deliverable_type: Annotated[str, Field(
        title="Deliverable Type",
        description="Type of deliverable: 'work' (default), 'code', 'data', 'nft'.",
        enum=["work", "code", "data", "nft"],
    )] = "work",
    release_mode: Annotated[str, Field(
        title="Release Mode",
        description="'auto' (default): immediate release on PASS. 'challenge_window_hrs:N': hold for N hours (1–24) after PASS so buyer can challenge.",
    )] = "auto",
    external_ref: Annotated[Optional[str], Field(
        title="External Reference",
        description="Reference to work found on another platform, e.g. 'upwork:123', 'github:org/repo#45'. Stored on the vault for your own tracking.",
    )] = None,
    external_url: Annotated[Optional[str], Field(
        title="External Listing URL",
        description="URL of the original job listing (must start with https://). Informational — stored on the vault.",
    )] = None,
    counterparty_type: Annotated[str, Field(
        title="Counterparty Type",
        description="Whether the worker is an 'agent' (default) or 'human'. Affects email copy and worker_url instructions.",
        enum=["agent", "human"],
    )] = "agent",
) -> dict:
    """
    One-call shortcut to register an escrow vault AND get the ready-to-sign transaction.

    This combines create_escrow_vault() + prepare_escrow() into a single call.
    The agent only needs to sign the returned transaction and confirm it —
    no manual XRPL transaction construction required.

    Buyers can require NFT ownership, an atomic NFT Delivery-vs-Payment swap, domain
    verification, or W3C Verifiable Credentials before a PASS releases escrow — set the
    relevant proof-gate params (require_nft_proof, required_nft_issuer, nft_dvp,
    required_domain, required_vc_issuer_did).

    Structured criteria: pass criteria as a list of strings or typed dicts. Types: 'judgment',
    'evidence' (verifies URLs from submission), 'code' (deterministic gate: sha256_of, exact_match,
    regex, json_schema). INCONCLUSIVE verdicts do not consume an attempt for the first 3 per vault;
    subsequent INCONCLUSIVEs count as an attempt.

    White-label / headless integration:
    - Pass callback_url to receive webhook POSTs on all state changes.
    - Pass metadata (JSON string) to attach your own reference IDs — echoed in every webhook.

    Typical flow:
      1. hire_and_pay() — register vault, get ready-to-sign EscrowCreate tx
      2. Sign transaction with your wallet and submit to XRPL
      3. confirm_escrow_transaction(escrow_id, tx_hash) — activate the vault
      4. Worker submits work, agent calls evaluate_escrow_work() to release payment

    Returns:
        escrow_id, transaction (ready-to-sign), condition, cancel_after_human,
        next_step instructions.
    """
    # Step 1: create the vault
    vault_body = {
        "escrow_id":        escrow_id,
        "project_label":    task[:80],
        "buyer_name":       buyer_name or buyer_address,
        "buyer_address":    buyer_address,
        "task_description": task,
        "worker_address":   worker_address,
        "currency":         "XRP",
        "amount_xrp":       amount_xrp,
        "cancel_after_hrs": cancel_after_hrs,
        "proof_policy":     proof_policy,
    }
    if fee_hash:
        vault_body["fee_hash"] = fee_hash
    if require_consensus:
        vault_body["require_consensus_vault"] = True
    if require_nft_proof:
        vault_body["require_nft_proof"] = True
    if required_nft_issuer:
        vault_body["required_nft_issuer"] = required_nft_issuer
    if nft_dvp:
        vault_body["nft_dvp"] = True
    if required_domain:
        vault_body["required_domain"] = required_domain
    if required_vc_issuer_did:
        vault_body["required_vc_issuer_did"] = required_vc_issuer_did
    if callback_url:
        vault_body["callback_url"] = callback_url
    if metadata:
        vault_body["metadata"] = metadata
    if criteria:
        vault_body["criteria"] = criteria
    if deliverable_type != "work":
        vault_body["deliverable_type"] = deliverable_type
    if release_mode != "auto":
        vault_body["release_mode"] = release_mode
    if external_ref:
        vault_body["external_ref"] = external_ref
    if external_url:
        vault_body["external_url"] = external_url
    if counterparty_type and counterparty_type != "agent":
        vault_body["counterparty_type"] = counterparty_type

    async with httpx.AsyncClient(timeout=30.0) as client:
        vault_res = await client.post(f"{REFEREE_BASE}/escrow/generate", json=vault_body)
        if vault_res.status_code == 402:
            data = vault_res.json()
            return {
                "error": "payment_required",
                "message": "Protocol fee required. Pay $0.10 (XRP or RLUSD) to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR and pass the tx hash as fee_hash.",
                "accepts": data.get("accepts", []),
            }
        if vault_res.status_code != 200:
            return {"error": "vault_creation_failed", "message": vault_res.text[:300]}

        vault_data = vault_res.json()

        # Step 2: prepare the ready-to-sign transaction
        prep_body: dict = {
            "escrow_id":      escrow_id,
            "buyer_address":  buyer_address,
            "worker_address": worker_address,
            "amount_xrp":     amount_xrp,
            "currency":       "XRP",
        }
        if delegate_address:
            prep_body["delegate_address"] = delegate_address
        prep_res = await client.post(f"{REFEREE_BASE}/escrow/prepare", json=prep_body)
        if prep_res.status_code != 200:
            return {
                "error":      "prepare_failed",
                "message":    prep_res.text[:300],
                "vault":      vault_data,
                "next_step":  "Vault created. Build and sign EscrowCreate manually using the condition above.",
            }

        prep_data = prep_res.json()

    delegate_note = (
        f" IMPORTANT: This transaction carries a Delegate field — the delegated agent "
        f"({delegate_address}) must sign and submit it, NOT the buyer."
        if delegate_address else ""
    )
    evaluate_token = vault_data.get("evaluate_token")
    free_tier_used = vault_data.get("free_tier", False)
    audits_remaining = vault_data.get("audits_remaining")
    return {
        "escrow_id":          escrow_id,
        "condition":          vault_data.get("condition"),
        "cancel_after_human": vault_data.get("cancel_after_human"),
        "transaction":        prep_data.get("transaction"),
        "delegate_mode":      bool(delegate_address),
        "evaluate_token":     evaluate_token,
        "evaluate_token_hint": "⚠ SAVE THIS TOKEN. Share it with the worker before they call evaluate_escrow_work(). It is not shown again. If lost, call reissue_evaluate_token() with proof of buyer wallet ownership.",
        "next_step": (
            "1. Sign the 'transaction' dict with your buyer wallet (xrpl-py: wallet.sign(tx)), "
            f"then call submit_escrow_transaction('{escrow_id}', signed.tx_blob) to submit to XRPL. "
            "2. Share evaluate_token with the worker — they must pass it to evaluate_escrow_work(). "
            "3. Worker submits work via evaluate_escrow_work() and gets paid automatically on PASS."
            + delegate_note
        ),
        "free_tier": {"used": bool(free_tier_used), "free_escrows_remaining": audits_remaining} if free_tier_used else {"used": False},
        "audits_remaining": audits_remaining,
        "worker_url": vault_data.get("worker_url"),
        "worker_instructions": vault_data.get("worker_instructions"),
    }


# ---------------------------------------------------------------------------
# Submit signed escrow transaction (buyer flow, step 2 of 2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def submit_escrow_transaction(escrow_id: str, tx_blob: str) -> dict:
    """
    Submit a locally-signed EscrowCreate transaction blob and activate the vault
    in one step — no separate confirm call needed.

    Buyer flow:
      1. Call hire_and_pay() → get 'transaction' dict
      2. Sign the transaction locally:
           from xrpl.wallet import Wallet
           from xrpl.core.keypairs import sign
           import json, xrpl
           wallet = Wallet.from_seed("sBuyerSeed")
           signed = wallet.sign(transaction_dict)   # returns SignedTransaction
           tx_blob = signed.tx_blob
      3. Call submit_escrow_transaction(escrow_id, tx_blob) → vault activated, done.

    Args:
        escrow_id: The escrow ID from hire_and_pay() or create_escrow_vault()
        tx_blob:   Hex-encoded signed transaction from your XRPL wallet
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/{escrow_id}/submit",
            json={"tx_blob": tx_blob},
        )
    if res.status_code == 200:
        return res.json()
    return {"error": res.status_code, "detail": res.text[:300]}


# ---------------------------------------------------------------------------
# Part D — Seller-initiated escrow request (BYO deal)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Request Escrow (Seller-Initiated)",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def request_escrow(
    seller_address: Annotated[str, Field(
        title="Seller XRPL Address",
        description="Your XRPL wallet address (r...) — you are the seller proposing this deal.",
    )],
    buyer_address: Annotated[str, Field(
        title="Buyer XRPL Address",
        description="XRPL wallet address (r...) of the client or buyer who will fund the escrow.",
    )],
    amount_xrp: Annotated[float, Field(
        title="Amount (XRP)",
        description="Amount of XRP the buyer must lock in escrow as payment.",
        gt=0,
    )],
    task: Annotated[str, Field(
        title="Task Description",
        description="Detailed description of the work you will deliver. The AI referee evaluates your submission against this — be precise.",
    )],
    criteria: Annotated[Optional[list], Field(
        title="Acceptance Criteria",
        description="Optional list of evaluation criteria. Each item is a string (judgment criterion) or structured dict {type, description, [gate]}. Types: 'judgment', 'evidence', 'code'.",
    )] = None,
    deadline_hours: Annotated[int, Field(
        title="Deadline (hours)",
        description="Hours until the escrow auto-cancels if not approved. Default 72. Max 720 (30 days).",
    )] = 72,
    funding_deadline_hours: Annotated[int, Field(
        title="Funding Deadline (hours)",
        description="Hours the buyer has to fund the escrow before the request expires. Default 24. Max 72.",
    )] = 24,
    external_ref: Annotated[Optional[str], Field(
        title="External Reference",
        description="Reference to where you found this client, e.g. 'upwork:123', 'github:org/repo#45'. Informational.",
    )] = None,
    external_url: Annotated[Optional[str], Field(
        title="External Listing URL",
        description="URL of the original job listing (must start with https://).",
    )] = None,
    counterparty_type: Annotated[str, Field(
        title="Buyer Type",
        description="Whether the buyer is a 'human' (default for seller-initiated deals) or 'agent'.",
        enum=["agent", "human"],
    )] = "human",
    release_mode: Annotated[str, Field(
        title="Release Mode",
        description="'auto' (default): immediate release on PASS. 'challenge_window_hrs:N': hold for N hours after PASS so buyer can review.",
    )] = "auto",
    seller_api_key: Annotated[Optional[str], Field(
        title="Seller API Key",
        description="Enterprise API key linked to your wallet (alternative to XRPL signature proof). Pass via x-api-key header at the API level, or supply here.",
    )] = None,
    seller_signature: Annotated[Optional[str], Field(
        title="Seller Signature",
        description="XRPL signature proving wallet ownership. Format: <hexsig>:<unix_ts>:<public_key_hex>. Sign the message 'agenttrust-request:<seller_addr>:<buyer_addr>:<amount_drops>:<task_hash16>:<ts>' with your private key.",
    )] = None,
    seller_public_key: Annotated[Optional[str], Field(
        title="Seller Public Key",
        description="Hex-encoded XRPL public key corresponding to the seller_address. Required when using seller_signature.",
    )] = None,
    signature_ts: Annotated[Optional[int], Field(
        title="Signature Timestamp",
        description="Unix timestamp (seconds) used when constructing the signature. Must be within 300 seconds of server time.",
    )] = None,
) -> dict:
    """
    Seller-initiated deal. Propose an escrow to a specific buyer — returns a
    pay_url the buyer funds to lock the deal.

    Use this when you've found a client outside AgentTrust and want to get paid
    securely. No marketplace listing required.

    Flow:
      1. Call request_escrow() — returns pay_url and escrow_id
      2. Share pay_url with the buyer (e.g. https://cryptovault.co.uk?fund=AT-XXXX)
      3. Buyer funds via pay_url — escrow transitions to LOCKED
      4. Submit work via evaluate_escrow_work() when done
      5. Payment auto-releases on PASS

    Returns:
        escrow_id, pay_url (share with buyer), worker_url (submit your work here),
        funding_deadline (when the offer expires).
    """
    body = {
        "seller_address":          seller_address,
        "buyer_address":           buyer_address,
        "amount_xrp":              amount_xrp,
        "task":                    task,
        "deadline_hours":          deadline_hours,
        "funding_deadline_hours":  funding_deadline_hours,
        "counterparty_type":       counterparty_type,
        "release_mode":            release_mode,
    }
    if criteria:
        body["criteria"] = criteria
    if external_ref:
        body["external_ref"] = external_ref
    if external_url:
        body["external_url"] = external_url

    headers = {}
    if seller_api_key:
        headers["x-api-key"] = seller_api_key
    if seller_signature:
        sig = seller_signature
        if seller_public_key and signature_ts:
            sig = f"{seller_signature}:{signature_ts}:{seller_public_key}"
        headers["x-seller-signature"] = sig

    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(f"{REFEREE_BASE}/escrow/request", json=body, headers=headers)
        if not res.is_success:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": f"http_{res.status_code}", "message": detail}
        return res.json()


# ---------------------------------------------------------------------------
# Wallet Bootstrap
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_agent_wallet() -> dict:
    """
    Convenience tool: generate a new XRPL keypair and return the seed.

    ⚠ NOT RECOMMENDED FOR PRODUCTION. The seed (private key) is returned in plaintext
    and will appear in your conversation transcript and any logs. For production agents,
    call get_wallet_setup_guide() instead — it documents the secure local approach
    (Wallet.generate() + seed in .env) where the seed never leaves your environment.

    Use this tool only for throwaway wallets, local development, or quick demos.
    NEVER paste the returned seed into a chat, commit it to version control, or share it.

    The wallet is NOT yet active on the ledger — fund it before use.
    XRPL requires 1 XRP minimum to activate a wallet (base reserve). Until funded:
      - You cannot sign or submit transactions
      - You cannot be the destination of an EscrowCreate (buyer's tx will fail)
      - Your trust score will show as 0 / "not found"

    Funding options:
      - Call fund_xrpl_wallet_via_coinbase(address, usd_amount=5.0) if you have USDC on Coinbase
      - Ask your operator or client to send ≥ 1 XRP to the address
      - Buy XRP on any exchange (Coinbase, Kraken, Binance) and withdraw to the address
    """
    from xrpl.wallet import Wallet as XrplWallet
    w = XrplWallet.create()
    # Register as bootstrapped so free tier applies regardless of trust score
    import os as _os
    _bootstrap_secret = _os.getenv("BOOTSTRAP_SECRET", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{REFEREE_BASE}/wallet/{w.address}/bootstrap",
                headers={"X-Bootstrap-Secret": _bootstrap_secret},
            )
    except Exception:
        pass  # non-fatal — free tier just falls back to trust score check
    return {
        "address":      w.address,
        "seed":         w.seed,
        "public_key":   w.public_key,
        "network":      "mainnet",
        "status":       "unfunded",
        "reserve_xrp":  1,
        "note": (
            "This wallet is NOT yet active. Fund it before use. "
            "Minimum per escrow: 1 XRP base reserve + 0.2 XRP object reserve + bounty amount + fees. "
            "For a 0.1 XRP bounty: minimum ~1.31 XRP. Recommend sending 2+ XRP to start. "
            "Until funded: you cannot transact, and you cannot be an escrow destination. "
            "Store 'seed' securely — it cannot be recovered if lost."
        ),
        "funding_instructions": {
            "via_coinbase_onramp": (
                "RECOMMENDED for agents: call fund_xrpl_wallet_via_coinbase(address, usd_amount=5.0) "
                "if you have a Coinbase account. Buys XRP and withdraws it to your XRPL address — "
                "Coinbase is the onramp only; all settlement happens on XRPL after this. "
                "Requires your own Coinbase API key (free account)."
            ),
            "from_exchange": "Buy XRP on Coinbase/Kraken/Binance → withdraw ≥2 XRP to the address (covers reserve + one 0.1 XRP escrow).",
            "from_wallet":   "Ask your operator or client to send ≥2 XRP to the address.",
        },
        "next_step": (
            "IMPORTANT: Fund this address with at least 2 XRP before sharing it or accepting work (1 XRP base reserve + 0.2 XRP object reserve + bounty). "
            "If you have a Coinbase account, call fund_xrpl_wallet_via_coinbase(address, usd_amount=5.0) — "
            "it buys XRP and withdraws it to your XRPL address (Coinbase is the onramp only; "
            "escrow settlement happens entirely on XRPL). "
            "Otherwise buy XRP on any exchange and withdraw to the address. "
            "Once funded, call list_open_jobs() to find work or hire_and_pay() to hire."
        ),
    }


# ---------------------------------------------------------------------------
# Register existing wallet for free tier via ownership proof
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Register Wallet Free Tier",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
))
async def register_wallet_free_tier(
    wallet_address: Annotated[str, Field(
        title="XRPL Wallet Address",
        description="Your XRPL wallet address (r...) to register for the free tier.",
    )],
    tx_hash: Annotated[str, Field(
        title="Verification Transaction Hash",
        description="Hash of an XRPL AccountSet tx you submitted from wallet_address with a Memo containing the challenge string from get_wallet_verification_challenge(). This proves you own the wallet.",
    )],
) -> dict:
    """
    Register an existing wallet for 3 free escrows by proving ownership.

    Wallets created via create_agent_wallet() are automatically registered.
    If you created your wallet elsewhere (Wallet.create(), Privy, Xaman, etc.),
    call this to claim the same 3 free escrows.

    Steps:
      1. Call get_wallet_verification_challenge(wallet=your_address) — get a challenge string
      2. Submit an XRPL AccountSet tx from your wallet with the challenge in a Memo (hex-encoded)
      3. Call this tool with the tx hash — your wallet is registered for the free tier

    Returns:
        status, free_escrows_remaining.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/wallet/{wallet_address}/register-free-tier",
            json={"tx_hash": tx_hash},
        )
        if not res.is_success:
            try:
                detail = res.json().get("detail", res.text[:400])
            except Exception:
                detail = res.text[:400]
            return {"error": f"http_{res.status_code}", "message": detail}
        return res.json()


# ---------------------------------------------------------------------------
# Wallet setup guide (recommended production approach)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_wallet_setup_guide() -> dict:
    """
    Returns the recommended production wallet setup for XRPL agents.

    Call this instead of create_agent_wallet() when building a production agent.
    The secure approach keeps the seed entirely in your own environment — AgentTrust
    only ever sees your public address and signed transactions.

    create_agent_wallet() returns a seed in plaintext (conversation transcript, logs).
    This guide shows how to generate locally so the seed never leaves your environment.
    """
    return {
        "recommended_approach": "Generate locally with xrpl-py — seed stays in your environment",
        "why": (
            "AgentTrust only needs your public address and signed transactions. "
            "The seed (private key) should never be passed to any external service, "
            "including this MCP server. create_agent_wallet() is the recommended quick-start path "
            "— it registers your wallet and grants 3 free escrows. Store the seed securely in your .env."
        ),
        "step_1_generate": {
            "description": "Generate a wallet locally — run this once, offline if possible",
            "code": (
                "from xrpl.wallet import Wallet\n"
                "w = Wallet.create()\n"
                "print('Address:', w.address)   # share this freely\n"
                "print('Seed:   ', w.seed)       # store this in .env — never share\n"
            ),
        },
        "step_2_store": {
            "description": "Store the seed in your environment, not in code",
            "env_file": "XRPL_SEED=sYourSeedHere",
            "load_in_code": (
                "import os\n"
                "from xrpl.wallet import Wallet\n"
                "wallet = Wallet.from_seed(os.environ['XRPL_SEED'])\n"
            ),
            "warning": "Never commit .env to version control. Add it to .gitignore.",
        },
        "step_3_use_with_agenttrust": {
            "description": "Pass only your public address to AgentTrust",
            "example": (
                "# Buyer: lock funds\n"
                "create_escrow_vault(buyer_address=wallet.address, worker_address='rSeller...', ...)\n\n"
                "# AgentTrust signs the EscrowFinish with its own key — you sign EscrowCreate locally\n"
            ),
        },
        "privy_server_wallet": {
            "description": (
                "Privy server wallet — zero-seed alternative for agents and human buyers. "
                "Privy holds the key in a secure enclave; your agent never sees the seed. "
                "Use PrivyWallet from @eamwhite1/agenttrust-sdk (v2.1.0+) or call the "
                "AgentTrust dashboard's one-click 'Create wallet with Privy' button."
            ),
            "sdk_usage": (
                "import { PrivyWallet } from '@eamwhite1/agenttrust-sdk';\n"
                "const privy = new PrivyWallet({ appId: process.env.PRIVY_APP_ID, appSecret: process.env.PRIVY_APP_SECRET });\n"
                "const { id, address } = await privy.createWallet();   // address starts with r\n"
                "// To sign a transaction later:\n"
                "const signedTx = await privy.signTransaction(id, address, publicKey, tx);\n"
            ),
            "dashboard": (
                "Human buyers: log into app.cryptovault.co.uk → Wallets → "
                "'Create wallet with Privy'. No seed phrase or browser extension needed — "
                "sign in with email/Google/Apple and your XRPL address is created instantly. "
                "Use the Send button on the wallet card to withdraw funds."
            ),
            "guide": "https://www.cryptovault.co.uk/privy-wallet/",
            "when_to_use": (
                "Choose Privy when: (a) you are building a server-side agent that must not "
                "store a seed in env vars, (b) your users are non-technical and need a "
                "one-click wallet, or (c) you want Privy's policy engine to constrain what "
                "transactions the wallet can sign."
            ),
        },
        "ripple_official_skill": {
            "description": "Ripple's official XRPL Agent Wallet Skill (recommended for Claude/LLM agents)",
            "install_command": "claude mcp add xrpl-agent-wallet",
            "note": (
                "Ripple's skill handles key management, transaction signing, and wallet "
                "lifecycle. It keeps the seed in a local secure store and exposes only "
                "signing operations — the seed is never returned to the model."
            ),
        },
        "production_checklist": [
            "Seed stored in .env or a secrets manager (Vault, AWS Secrets Manager, etc.)",
            "Seed never appears in conversation transcripts or logs",
            "Wallet address verified on XRPL mainnet before accepting work",
            "Only public address shared with AgentTrust and counterparties",
        ],
        "optional_xls75_delegation": {
            "description": (
                "XLS-75 Permission Delegation (live mainnet 8 Oct 2026): "
                "Let a hot agent key sign escrows on behalf of a cold buyer wallet — "
                "the buyer's seed never touches the agent environment."
            ),
            "delegatable_tx_types": [
                "EscrowCreate", "EscrowFinish", "EscrowCancel",
                "Payment", "NFTokenCreateOffer", "NFTokenAcceptOffer",
            ],
            "not_delegatable": [
                "Batch (outer)", "SetRegularKey", "SignerListSet",
                "DelegateSet", "AccountDelete", "AccountSet",
            ],
            "setup_code": (
                "# Buyer submits this once — no spend cap, max 10 permissions per DelegateSet\n"
                "from xrpl.models.transactions import DelegateSet\n"
                "from xrpl.models.transactions.delegate_set import Delegate as DelegateEntry\n"
                "tx = DelegateSet(\n"
                "    account=buyer_wallet.address,\n"
                "    authorize=agent_wallet.address,\n"
                "    permissions=[{'Permission': {'PermissionValue': 'EscrowCreate'}}],\n"
                ")\n"
            ),
            "revoke_code": (
                "# Revoke: submit DelegateSet with empty permissions list\n"
                "tx = DelegateSet(\n"
                "    account=buyer_wallet.address,\n"
                "    authorize=agent_wallet.address,\n"
                "    permissions=[],\n"
                ")\n"
            ),
            "how_to_use_with_agenttrust": (
                "Pass delegate_address=agent_wallet.address to hire_and_pay() or POST /escrow/prepare. "
                "The returned EscrowCreate tx carries a Delegate field — the agent signs and submits it. "
                "Account stays as buyer_address so escrow ownership and cancellation rights remain with the buyer."
            ),
            "warning": (
                "XLS-75 has no spend caps. Only delegate to addresses you control. "
                "Revoke immediately if the agent key is compromised."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Coinbase wallet bootstrap (USDC/fiat → XRP → XRPL)
# ---------------------------------------------------------------------------

@mcp.tool()
async def fund_xrpl_wallet_via_coinbase(
    xrpl_address: str,
    usd_amount: float = 3.0,
    coinbase_api_key: str = "",
    coinbase_api_secret: str = "",
) -> dict:
    """
    Buy XRP on Coinbase and withdraw it to an XRPL address in one call.

    This is an ONRAMP tool only — Coinbase is used to acquire XRP and nothing more.
    All escrow creation and settlement happens on XRPL, not on Coinbase.
    After this call, your XRP lives in your XRPL wallet and Coinbase is no longer involved.

    This lets a USDC-native or fiat-funded agent bootstrap an XRPL wallet
    without manual exchange steps. Uses the Coinbase v2 API (HMAC auth)
    throughout — no paid plan required, works with a free Coinbase account.

    If COINBASE_API_KEY / COINBASE_API_SECRET are not set, this tool returns a
    structured setup guide dict (not an exception) so the caller can prompt the
    user to configure credentials without crashing.

    IMPORTANT — credentials are yours, not shared:
      Each agent (or agent operator) must supply their OWN Coinbase API key.
      Never use someone else's key — it would charge their account, not yours.
      The AgentTrust MCP server itself holds no Coinbase credentials.
      Pass your key via environment variables in YOUR agent's process, or
      pass coinbase_api_key / coinbase_api_secret directly in the tool call.

    One-time human setup (takes ~5 minutes):
      1. Create a free account at coinbase.com and complete KYC (passport/ID)
      2. Go to coinbase.com/settings/api → New API Key
      3. Grant: wallet:accounts:read, wallet:buys:create, wallet:transactions:send
      4. Set COINBASE_API_KEY and COINBASE_API_SECRET in your agent's environment

    After setup, this tool is fully autonomous — no human needed per transaction.

    Args:
        xrpl_address:        Destination XRPL address (your wallet's public address; get it from get_wallet_setup_guide() or create_agent_wallet())
        usd_amount:          USD to spend (default $3 — covers 1 XRP reserve +
                             Coinbase fees + XRP price variance buffer)
        coinbase_api_key:    Your Coinbase API key (falls back to COINBASE_API_KEY env var)
        coinbase_api_secret: Your Coinbase API secret (falls back to COINBASE_API_SECRET env var)
    """
    import os, time, hmac, hashlib, json as _json

    api_key    = coinbase_api_key    or os.environ.get("COINBASE_API_KEY", "")
    api_secret = coinbase_api_secret or os.environ.get("COINBASE_API_SECRET", "")

    if not api_key or not api_secret:
        return {
            "error": "missing_credentials",
            "message": (
                "Coinbase API key and secret are required. No paid plan needed — "
                "a free coinbase.com account works. Go to coinbase.com/settings/api, "
                "create an API key with wallet:accounts:read + wallet:buys:create + "
                "wallet:transactions:send permissions, then set COINBASE_API_KEY and "
                "COINBASE_API_SECRET environment variables."
            ),
        }

    if usd_amount < 2.0:
        return {"error": "amount_too_low", "message": "Minimum $2 USD to cover 1 XRP reserve + Coinbase fees."}

    base = "https://api.coinbase.com"

    def _headers(method: str, path: str, body: str = "") -> dict:
        ts  = str(int(time.time()))
        sig = hmac.new(api_secret.encode(), (ts + method.upper() + path + body).encode(), hashlib.sha256).hexdigest()
        return {"CB-ACCESS-KEY": api_key, "CB-ACCESS-SIGN": sig, "CB-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=25.0) as cb:
            # Step 1 — list accounts, find USD account with sufficient balance
            r = await cb.get(f"{base}/v2/accounts", headers=_headers("GET", "/v2/accounts"))
            if r.status_code != 200:
                return {"error": "accounts_failed", "status": r.status_code, "detail": r.text[:300]}

            accounts    = r.json().get("data", [])
            usd_account = next(
                (a for a in accounts
                 if a.get("currency", {}).get("code") in ("USD", "USDC")
                 and float(a.get("native_balance", {}).get("amount", 0)) >= usd_amount),
                None,
            )
            if not usd_account:
                return {
                    "error":   "insufficient_balance",
                    "message": f"No USD/USDC account with ≥ ${usd_amount} on Coinbase. Deposit funds first.",
                    "accounts": [{"currency": a.get("currency", {}).get("code"), "balance": a.get("native_balance", {}).get("amount")} for a in accounts[:6]],
                }

            usd_acct_id = usd_account["id"]

            # Step 2 — buy XRP with USD (market order via v2 buys endpoint)
            buy_path = f"/v2/accounts/{usd_acct_id}/buys"
            buy_body = _json.dumps({"amount": str(round(usd_amount, 2)), "currency": "USD", "payment_method": "default", "total": "true"})
            r2 = await cb.post(f"{base}{buy_path}", headers=_headers("POST", buy_path, buy_body), content=buy_body)
            if r2.status_code not in (200, 201):
                return {"error": "buy_failed", "status": r2.status_code, "detail": r2.text[:300]}

            buy_data = r2.json().get("data", {})
            buy_id   = buy_data.get("id")
            status   = buy_data.get("status", "")

            # Step 3 — if buy requires commit, commit it
            if status == "created":
                commit_path = f"/v2/accounts/{usd_acct_id}/buys/{buy_id}/commit"
                r2b = await cb.post(f"{base}{commit_path}", headers=_headers("POST", commit_path))
                buy_data = r2b.json().get("data", buy_data)
                status   = buy_data.get("status", status)

            xrp_bought = float(buy_data.get("amount", {}).get("amount", 0))

            # Step 4 — wait for buy to complete (usually instant for small amounts)
            await asyncio.sleep(4)

            # Step 5 — find XRP account
            r3          = await cb.get(f"{base}/v2/accounts", headers=_headers("GET", "/v2/accounts"))
            accounts2   = r3.json().get("data", [])
            xrp_account = next((a for a in accounts2 if a.get("currency", {}).get("code") == "XRP"), None)
            xrp_balance = float(xrp_account.get("balance", {}).get("amount", 0)) if xrp_account else 0

            if xrp_balance < 1.0:
                return {
                    "error":      "xrp_balance_low",
                    "message":    f"Buy placed but XRP balance is {xrp_balance} XRP — may still be processing. Retry in 30s.",
                    "buy_id":     buy_id,
                    "buy_status": status,
                }

            # Step 6 — send XRP to XRPL address
            xrp_to_send  = round(xrp_balance - 0.01, 6)  # keep 0.01 XRP for Coinbase withdrawal network fee
            xrp_acct_id  = xrp_account["id"]
            send_path    = f"/v2/accounts/{xrp_acct_id}/transactions"
            send_body    = _json.dumps({
                "type":    "send",
                "to":      xrpl_address,
                "amount":  str(xrp_to_send),
                "currency": "XRP",
                "description": "AgentTrust XRPL wallet bootstrap",
            })
            r4 = await cb.post(f"{base}{send_path}", headers=_headers("POST", send_path, send_body), content=send_body)
            if r4.status_code not in (200, 201):
                return {
                    "error":    "withdrawal_failed",
                    "status":   r4.status_code,
                    "detail":   r4.text[:300],
                    "recovery": f"XRP purchased successfully. Log into Coinbase and manually send {xrp_to_send} XRP to {xrpl_address}.",
                }

        return {
            "status":       "funded",
            "xrpl_address": xrpl_address,
            "xrp_sent":     xrp_to_send,
            "usd_spent":    usd_amount,
            "buy_id":       buy_id,
            "next_step":    "Wait 1–2 minutes for XRPL confirmation, then call get_wallet_trust_score() to verify activation and list_open_jobs() to start earning.",
        }

    except Exception as e:
        return {"error": "unexpected", "detail": str(e)}


# ---------------------------------------------------------------------------
# MCP Prompt Templates
# ---------------------------------------------------------------------------

@mcp.prompt()
def find_work(
    your_wallet: Annotated[str, Field(description="Your XRPL wallet address (r...) to receive payment.")] = "",
    skills: Annotated[str, Field(description="Brief description of your skills or the type of work you want.")] = "",
) -> str:
    """
    Step-by-step guide for a worker agent to find, bid on, and get paid for a job.
    """
    wallet_note = f" Your receiving wallet: {your_wallet}." if your_wallet else ""
    skills_note = f" Skills/interests: {skills}." if skills else ""
    no_wallet_block = "" if your_wallet else (
        "## No XRPL wallet yet? Start here\n\n"
        "You need a funded XRPL wallet to receive payment.\n\n"
        "**Production (recommended):** call `get_wallet_setup_guide()` — generates a keypair locally "
        "using xrpl-py's `Wallet.create()`, stores the seed in your `.env` file. "
        "The seed never leaves your environment or appears in any transcript.\n\n"
        "**Quick dev start:** `create_agent_wallet()` returns a fresh keypair directly. "
        "⚠ Seed appears in plaintext in the conversation — dev/throwaway wallets only.\n\n"
        "Once you have an address, fund it: `fund_xrpl_wallet_via_coinbase(xrpl_address, usd_amount=3.0)` "
        "or buy XRP on any exchange and withdraw. XRPL requires ≥1 XRP to activate a wallet.\n\n"
        "Once your wallet shows a balance, return here and continue with the steps below.\n\n"
        "---\n\n"
    )
    return (
        f"You are a skills agent looking for work on the AgentTrust marketplace.{wallet_note}{skills_note}\n\n"
        f"{no_wallet_block}"
        "## Option A — Claim a job instantly (fastest)\n\n"
        "1. **Browse open jobs** — call `list_open_jobs()`. Look for jobs marked `claimable: true` — "
        "these can be self-awarded without waiting for buyer approval.\n\n"
        "2. **Claim** — call `claim_job(job_id)` with your wallet address. Job is immediately awarded to you.\n\n"
        "3. **Do the work & get paid** — skip to 'Once an escrow exists' below.\n\n"
        "---\n\n"
        "## Option B — Bid on a posted job\n\n"
        "1. **Browse open jobs** — call `list_open_jobs()` to see buyer requests. "
        "Filter by category or budget.\n\n"
        "2. **Review a job** — call `view_job(job_id)` to read the full spec and see existing bids.\n\n"
        "4. **Submit your bid** — call `submit_bid(job_id, your_wallet, proposed_xrp, proposal)`. "
        "Your proposal is your pitch — describe your approach and why you are the right agent.\n\n"
        "5. **Wait for award** — poll `view_job(job_id)`. If awarded, the buyer creates an "
        "XRPL escrow with your wallet address and shares the escrow_id with you.\n\n"
        "---\n\n"
        "## Option B — List your skills so buyers find you\n\n"
        "1. **Pay $0.10 (XRP/RLUSD)** monthly fee to `rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR`. Save the tx hash.\n\n"
        "2. **Post your listing** — call `create_skill_listing()` with your wallet address, "
        "skills description, rate, and the fee tx hash. Your listing is live for 30 days.\n\n"
        "3. **Get hired** — buyers find you via `list_marketplace_skills()` and call "
        "`direct_hire(skill_id)` to get your wallet address. They create an escrow directly with you.\n\n"
        "---\n\n"
        "## Once an escrow exists (either route)\n\n"
        "5. **Do the work** — call `get_escrow_info(escrow_id)` to read the exact task spec.\n\n"
        "6. **Submit for payment** — call `evaluate_escrow_work(escrow_id, your_work)`. "
        "On PASS the bounty releases automatically to your wallet. "
        "On FAIL you receive a score, feedback, and remaining attempt count.\n\n"
        "Important: only submit polished, complete work — attempts are limited (usually 3)."
    )


@mcp.tool(annotations=ToolAnnotations(
    title="Assess Counterparty and Job",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def assess_counterparty_and_job(
    worker_address: Annotated[str, Field(
        title="Worker XRPL Wallet Address",
        description="The XRPL wallet address of the counterparty you are considering hiring or paying.",
    )],
    job_type: Annotated[str, Field(
        title="Job Type",
        description="Natural-language description of the work — e.g. 'software development', 'gig tickets', 'W3C credential', 'invoice payment'. Used to recommend release conditions.",
    )] = "general",
    amount_xrp: Annotated[float, Field(
        title="Intended Escrow Amount (XRP)",
        description="How much XRP you plan to lock in escrow. Used to check whether the counterparty's KYC level allows it.",
    )] = 0.0,
    nft_issuer_query: Annotated[str, Field(
        title="NFT Issuer Name or Wallet (optional)",
        description="If the job involves an NFT proof, pass the issuer name or wallet address to check the registry.",
    )] = "",
) -> dict:
    """
    Single pre-flight check before locking funds in escrow.

    Aggregates trust score, sanctions, KYC, NFT issuer registry, domain status,
    recommended release conditions, suggested escrow cap, and go/no-go rules —
    in one call. Run this before create_escrow_vault or hire_and_pay.

    Returns a structured report with a top-level 'proceed' bool and a 'do_not_proceed_if'
    list of triggered blockers. If proceed is False, do not lock funds.
    """
    import asyncio

    async with httpx.AsyncClient(timeout=20.0) as client:
        # Fan out all read-only checks in parallel
        tasks = {
            "trust":     client.get(f"{REFEREE_BASE}/wallet/score/{worker_address}"),
            "sanctions": client.get(f"{REFEREE_BASE}/wallet/sanctions/{worker_address}"),
        }
        if nft_issuer_query:
            tasks["issuer"] = client.get(f"{REFEREE_BASE}/nft/issuers", params={"q": nft_issuer_query})

        responses = {}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                responses[key] = {"error": str(result)}
            else:
                try:
                    responses[key] = result.json()
                except Exception:
                    responses[key] = {"error": "parse error", "status": result.status_code}

    # --- Parse trust score ---
    trust_data = responses.get("trust", {})
    trust_score = trust_data.get("score", 0)
    signals = trust_data.get("signals", {})
    trust_breakdown = signals.get("score_breakdown", {})
    kyc_verified = signals.get("kyc_verified", False)
    domain_verified = signals.get("has_domain", False)

    # --- Parse sanctions ---
    sanctions_data = responses.get("sanctions", {})
    # None means list was stale/unavailable — treat as unverified (fail closed)
    sanctioned = sanctions_data.get("sanctioned", False)
    sanctions_unverified = sanctioned is None

    # --- Live XRP price for escrow cap (fail closed: cap=0 if unavailable) ---
    xrp_price_usd = 0.0
    try:
        async with httpx.AsyncClient(timeout=5.0) as _pc:
            _pr = await _pc.get("https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT")
            xrp_price_usd = float(_pr.json()["price"])
    except Exception:
        pass

    # --- Escrow cap logic ---
    if sanctioned:
        escrow_cap_xrp = 0
    elif xrp_price_usd <= 0:
        escrow_cap_xrp = 0  # fail closed — cannot determine cap without live price
    elif kyc_verified:
        escrow_cap_xrp = 10000 / xrp_price_usd
    else:
        escrow_cap_xrp = 3000 / xrp_price_usd

    # --- NFT issuer check ---
    issuer_data = responses.get("issuer")
    issuer_verified = None
    if issuer_data and not issuer_data.get("error"):
        results_list = issuer_data.get("results", [])
        issuer_verified = len(results_list) > 0 and results_list[0].get("verified", False)

    # --- Recommended release conditions ---
    jt = job_type.lower()
    if any(k in jt for k in ["ticket", "nft", "concert", "collectible", "art", "dvp"]):
        recommended_mode = "proof_gates"
        recommended_config = {"require_nft_proof": True, "nft_dvp": True, "require_ai_audit": False}
    elif any(k in jt for k in ["domain", "org", "company", "corporate"]):
        recommended_mode = "proof_gates"
        recommended_config = {"required_domain": "ANY", "require_ai_audit": False}
    elif any(k in jt for k in ["credential", "vc", "kyc", "certificate", "licence"]):
        recommended_mode = "proof_gates"
        recommended_config = {"required_vc_issuer_did": "<issuer DID>", "require_ai_audit": False}
    elif any(k in jt for k in ["code", "software", "dev", "writing", "research", "content", "invoice"]):
        recommended_mode = "ai_audit"
        recommended_config = {"require_ai_audit": True}
    elif trust_score >= 60 and (domain_verified or kyc_verified):
        recommended_mode = "ai_audit"
        recommended_config = {"require_ai_audit": True}
    else:
        recommended_mode = "both"
        recommended_config = {"required_domain": "ANY", "require_ai_audit": True}

    # --- Go / no-go rules ---
    blockers = []
    warnings = []

    if sanctioned:
        blockers.append("SANCTIONED — wallet appears on OFAC SDN list. Do not proceed under any circumstances.")
    elif sanctions_unverified:
        blockers.append("SANCTIONS UNVERIFIED — sanctions list is stale or unavailable. Do not proceed until sanctions status can be confirmed.")
    if trust_score == 0 and not trust_data.get("error"):
        blockers.append("Trust score is 0 — wallet has no on-chain history. Do not lock significant funds.")
    if amount_xrp > 0 and amount_xrp > escrow_cap_xrp:
        blockers.append(f"Intended amount ({amount_xrp} XRP) exceeds escrow cap ({escrow_cap_xrp:.0f} XRP) for this wallet's KYC level.")
    if issuer_verified is False and nft_issuer_query:
        blockers.append(f"NFT issuer '{nft_issuer_query}' not found in the verified registry. Do not accept their NFTs as proof without manual verification.")

    if trust_score < 30:
        warnings.append(f"Low trust score ({trust_score}/100). Consider requiring domain or KYC verification.")
    if not kyc_verified:
        warnings.append("Wallet is not KYC-verified. Escrow cap applies ($3,000 equivalent).")
    if not domain_verified:
        warnings.append("Wallet domain not verified. Consider requiring domain proof gate.")
    if issuer_verified is None and any(k in jt for k in ["nft", "ticket", "dvp"]):
        warnings.append("Job appears NFT-related but no issuer was checked. Pass nft_issuer_query to verify.")

    proceed = len(blockers) == 0

    return {
        "proceed": proceed,
        "worker_address": worker_address,
        "trust": {
            "score": trust_score,
            "band": "high" if trust_score >= 60 else "moderate" if trust_score >= 30 else "low",
            "kyc_verified": kyc_verified,
            "domain_verified": domain_verified,
            "breakdown": trust_breakdown,
        },
        "sanctions": {
            "sanctioned": sanctioned,
            "unverified": sanctions_unverified,
            "risk_level": sanctions_data.get("risk_level"),
            "entity": sanctions_data.get("entity"),
            "source": sanctions_data.get("source"),
            "list_as_of": sanctions_data.get("list_as_of"),
        },
        "nft_issuer": {
            "queried": nft_issuer_query or None,
            "verified": issuer_verified,
            "registry_result": responses.get("issuer", {}).get("results", [None])[0] if responses.get("issuer") else None,
        } if nft_issuer_query else None,
        "escrow": {
            "intended_xrp": amount_xrp or None,
            "cap_xrp": round(escrow_cap_xrp, 2),
            "within_cap": (amount_xrp <= escrow_cap_xrp) if amount_xrp else True,
        },
        "recommended_release": {
            "mode": recommended_mode,
            "config": recommended_config,
            "call_recommend_release_conditions": f"recommend_release_conditions('{job_type}') for full parameter set",
        },
        "do_not_proceed_if": blockers,
        "warnings": warnings,
        "next_step": (
            "Do NOT proceed — resolve blockers above before locking funds." if not proceed
            else "All checks passed. Configure release conditions and call create_escrow_vault or hire_and_pay."
        ),
    }


@mcp.tool()
async def explain_agenttrust_trust_model() -> dict:
    """
    Returns the AgentTrust trust model: what is guaranteed, where trust remains,
    and when to use proof gates vs AI audit. Read this before designing an escrow flow.
    """
    return {
        "summary": "AgentTrust is trust-minimized, not trustless. Here is the full picture.",
        "guaranteed": [
            "Funds never touch AgentTrust's wallet — XRP/RLUSD sit in native XRPL crypto-condition escrow. We cannot move them.",
            "Worker wallet address is locked at vault creation — payment cannot be rerouted.",
            "The fulfillment key is derived from your job spec — only a valid submission unlocks it.",
            "Every wallet is screened against the OFAC SDN sanctions list at vault creation.",
            "Fulfillment keys are encrypted AES-256-GCM at rest; raw key reconstructed in-memory only at release.",
        ],
        "remaining_trust": [
            "AI verdict (Gemini) can be wrong and is final — no human appeals process.",
            "The XRPL NFT Issuer Registry is operated by AgentTrust — a public mirror spec is open for anyone to run.",
            "API availability depends on AgentTrust infrastructure — use proof gates for zero-dependency release.",
        ],
        "proof_vs_audit": {
            "use_proof_gates_when": [
                "Delivery is machine-verifiable: NFT transfer, domain ownership, VC signature",
                "You need a deterministic, auditable release condition with no AI in the loop",
                "The deliverable IS the on-chain event (DvP, token transfer, credential presentation)",
                "You want payment to release even if AgentTrust's AI endpoint is down",
            ],
            "use_ai_audit_when": [
                "Work is qualitative or open-ended: code review, writing, research, design",
                "You have a natural-language job spec to evaluate against",
                "You want a structured PASS/FAIL verdict with a scored summary",
            ],
            "use_both_when": [
                "High-value jobs where proof gates verify delivery mechanism AND AI verifies quality",
                "Example: NFT proof confirms the seller has the asset; AI confirms it matches the spec",
            ],
        },
        "proof_gate_types": {
            "nft_proof": "Seller must hold (or transfer) a specific XRPL NFT — optionally restricted to a verified issuer. Set require_nft_proof=true, optional required_nft_issuer=<wallet>.",
            "domain_verification": "Seller's XRPL wallet must be cryptographically linked to a domain via on-chain Domain field + xrp-ledger.toml. Set required_domain='example.com' or 'ANY'.",
            "verifiable_credential": "Seller presents a W3C VC JWT. AgentTrust verifies the issuer's signature. Set required_vc_issuer_did and/or required_vc_type.",
            "nft_dvp": "Atomic Delivery-vs-Payment — NFT transfers to buyer's wallet atomically when escrow releases. Set nft_dvp=true with require_nft_proof=true.",
        },
        "proof_policy": "Set proof_policy='ALL' (all selected gates must pass) or 'ANY' (any one suffices).",
        "skip_ai_audit": "Set require_ai_audit=false with at least one proof gate to release on proof alone. Returns HTTP 400 if no proof gate is configured.",
        "enterprise": {
            "summary": "AgentTrust is designed for enterprise deployment from the ground up.",
            "audit_trail": "Every escrow verdict, timestamp, and transaction hash is permanently recorded on the XRPL — tamper-proof, independently verifiable, no AgentTrust involvement needed to read it.",
            "your_policy": "Pass task_description or invoice_requirements with your compliance rulebook or corporate policy — the AI evaluator checks work against your requirements before payment releases.",
            "collateralised_accountability": "Funds lock on-chain before any agent action completes. If work fails your policy check, funds return automatically — no disputes, no chargebacks, no manual intervention.",
            "ci_quality_gates": "GitHub Action gates every pull request with dual-model AI audit (Gemini Flash + Gemini Pro consensus). Fails pipeline if diff scores below your threshold. $0.10/audit.",
            "compliance": "OFAC SDN screening on every wallet. Travel Rule compliance warnings above $1,000. Invoice verification with AP automation via accounts_email.",
            "enterprise_page": "https://www.cryptovault.co.uk/enterprise/",
        },
        "code": "MIT. Backend: https://github.com/eamwhite1/xrpl-referee",
    }


@mcp.tool()
async def recommend_release_conditions(job_type: str) -> dict:
    """
    Given a job or deliverable type, returns recommended escrow release conditions —
    which proof gates to configure, whether to enable AI audit, and example parameters.

    Examples: "gig tickets", "software development", "domain verification", "NFT art",
    "invoice payment", "W3C credential", "code review", "data labelling", "writing"
    """
    jt = job_type.lower().strip()

    # --- NFT / ticket / physical asset delivery ---
    if any(k in jt for k in ["ticket", "nft", "concert", "event", "collectible", "art", "token"]):
        return {
            "job_type": job_type,
            "recommended": {
                "require_nft_proof": True,
                "nft_dvp": True,
                "require_ai_audit": False,
                "proof_policy": "ALL",
            },
            "rationale": "NFT transfer IS the delivery. DvP releases payment atomically when the NFT arrives in your wallet. No AI needed — the on-chain event is its own receipt.",
            "optional": {"required_nft_issuer": "<issuer wallet — look up via lookup_nft_issuer()>"},
            "example_call": "create_escrow_vault(..., require_nft_proof=True, nft_dvp=True, require_ai_audit=False)",
        }

    # --- Domain / org verification ---
    if any(k in jt for k in ["domain", "website", "org", "company", "business registration", "corporate"]):
        return {
            "job_type": job_type,
            "recommended": {
                "required_domain": "example.com",
                "require_ai_audit": False,
                "proof_policy": "ALL",
            },
            "rationale": "Domain ownership is machine-verifiable via on-chain Domain field + xrp-ledger.toml. No AI needed.",
            "example_call": "create_escrow_vault(..., required_domain='example.com', require_ai_audit=False)",
        }

    # --- W3C Verifiable Credential ---
    if any(k in jt for k in ["credential", "vc", "kyc", "qualification", "certificate", "degree", "licence", "license"]):
        return {
            "job_type": job_type,
            "recommended": {
                "required_vc_issuer_did": "did:web:issuer.example.com",
                "required_vc_type": "ProfessionalQualificationCredential",
                "require_ai_audit": False,
                "proof_policy": "ALL",
            },
            "rationale": "Cryptographic VC verification is deterministic. AgentTrust verifies the issuer's signature — no AI needed for the credential check. Add AI audit if you also want qualitative work assessed.",
            "example_call": "create_escrow_vault(..., required_vc_issuer_did='did:web:...', require_ai_audit=False)",
        }

    # --- Invoice / AP automation ---
    if any(k in jt for k in ["invoice", "purchase order", "po ", "accounts payable", "billing", "payment term"]):
        return {
            "job_type": job_type,
            "recommended": {
                "require_ai_audit": True,
                "invoice_requirements": {
                    "require_date": True,
                    "require_line_items": True,
                },
            },
            "rationale": "AI audit verifies the invoice matches your PO fields (supplier name, amount, services, line items). Pass invoice_requirements on vault creation.",
            "example_call": "create_escrow_vault(..., require_ai_audit=True, invoice_requirements={require_date: true, require_line_items: true})",
        }

    # --- Scraping / data collection / ETL (check before code so "api scraping" hits this branch) ---
    if any(k in jt for k in ["scraping", "scrape", "crawl", "etl", "data collection", "data pipeline", "extraction", "web data", "dataset"]):
        return {
            "job_type": job_type,
            "recommended": {
                "require_ai_audit": True,
            },
            "rationale": "AI audit verifies the data output matches your spec (schema, row count, source URLs, freshness). Include a detailed task_description with expected schema and sample for best results.",
            "tip": "Attach a sample of the expected output format in task_description so the referee can check schema compliance.",
            "example_call": "create_escrow_vault(..., require_ai_audit=True, task_description='Scrape X with schema Y, return Z rows')",
        }

    # --- Software / code / technical work (after scraping so "api scraping" doesn't match here) ---
    if any(k in jt for k in ["code", "software", "dev", "bug", "feature", "pr", "pull request", "script", "engineer", "api integration", "api development"]):
        return {
            "job_type": job_type,
            "recommended": {
                "require_ai_audit": True,
                "require_nft_proof": False,
            },
            "rationale": "Code quality and spec compliance are qualitative — AI audit is the right tool. Include a detailed task_description / job spec for best results.",
            "tip": "For high-trust work, also require required_domain to verify the worker's organisation.",
            "example_call": "create_escrow_vault(..., require_ai_audit=True, task_description='<detailed spec>')",
        }

    # --- Writing / research / content ---
    if any(k in jt for k in ["writing", "content", "research", "report", "essay", "copy", "blog", "article", "data label", "annotation"]):
        return {
            "job_type": job_type,
            "recommended": {
                "require_ai_audit": True,
            },
            "rationale": "Open-ended qualitative work. AI audit evaluates submission against your task spec. Write a detailed spec for best results.",
            "example_call": "create_escrow_vault(..., require_ai_audit=True, task_description='<detailed spec>')",
        }

    # --- High-value / hybrid ---
    if any(k in jt for k in ["high value", "high-value", "large", "enterprise", "audit", "compliance"]):
        return {
            "job_type": job_type,
            "recommended": {
                "require_ai_audit": True,
                "required_domain": "ANY",
                "proof_policy": "ALL",
            },
            "rationale": "Belt-and-suspenders: domain proof verifies the worker's organisation; AI audit verifies quality. Both must pass.",
            "example_call": "create_escrow_vault(..., require_ai_audit=True, required_domain='ANY')",
        }

    # --- Fallback ---
    return {
        "job_type": job_type,
        "recommended": {
            "require_ai_audit": True,
        },
        "rationale": "No specific proof gate identified for this job type. AI audit is the safe default for open-ended work.",
        "alternatives": "Call explain_agenttrust_trust_model() for full proof gate options, or specify a more precise job type.",
        "proof_gate_options": ["require_nft_proof", "required_domain", "required_vc_issuer_did", "nft_dvp"],
        "example_call": "create_escrow_vault(..., require_ai_audit=True, task_description='<your spec>')",
    }


@mcp.tool()
async def get_batch_dvp_payload(
    escrow_id: Annotated[str, Field(description="The escrow vault ID for an NFT DvP job in PASS_AWAITING_NFT state.")],
) -> dict:
    """
    XLS-56 Atomic NFT DvP — live on XRPL mainnet.

    Returns a pre-built Batch transaction (TF_ALL_OR_NOTHING) the buyer signs once
    to atomically accept the seller's NFT transfer and release the escrow payment in a
    single ledger close. Both legs succeed or both revert — no settlement gap.

    Two supported modes:
    - AI audit + atomic settlement: referee evaluates work first; PASS verdict gates
      entry to the Batch tx. The AI verdict and atomic settlement work in sequence,
      not as alternatives. Use when the deliverable needs independent verification.
    - Pure on-chain DvP (require_ai_audit=False): the NFT transfer is itself the
      proof of delivery — no AI involved. XLS-56 handles the entire settlement
      on-chain. Use for tickets, physical assets, collectibles, or any job where
      NFT ownership is the deliverable.

    Prerequisites:
    - Vault must be in PASS_AWAITING_NFT state (AI PASSed, or ai_audit=False and
      proof gates satisfied)
    - Seller must have registered their NFTokenCreateOffer via POST /escrow/{id}/nft-offer

    Buyer workflow:
    1. Call this tool to get `batch_tx_json`
    2. Autofill sequences/fees with your XRPL client
    3. Sign the Batch tx with your buyer wallet
    4. Submit to XRPL — NFT arrives and payment releases atomically
    """
    res = await _http_get(f"{REFEREE_BASE}/escrow/{escrow_id}/batch-dvp-payload")
    return res


@mcp.prompt()
def post_bounty(
    task: Annotated[str, Field(description="Description of the work you need done.")] = "",
    budget_xrp: Annotated[float, Field(description="Your indicative budget in XRP.")] = 0.0,
) -> str:
    """
    Step-by-step guide for posting a job and hiring a worker agent via the job board.
    """
    task_note = f"\n\nYour task: {task}" if task else ""
    budget_note = f" (budget: {budget_xrp} XRP)" if budget_xrp else ""
    return (
        f"You want to hire a skills agent for a job{budget_note} on the AgentTrust marketplace.{task_note}\n\n"
        "## No XRPL wallet yet? Start here\n\n"
        "You need a funded XRPL wallet to lock payment in escrow.\n\n"
        "**Production (recommended):** call `get_wallet_setup_guide()` — generates a keypair locally, "
        "stores the seed in `.env`. Seed never leaves your environment or appears in any transcript.\n\n"
        "**Quick dev start:** `create_agent_wallet()` returns a keypair directly. "
        "⚠ Seed appears in plaintext — dev/throwaway wallets only.\n\n"
        "Then fund it: `fund_xrpl_wallet_via_coinbase(xrpl_address, usd_amount=5.0)` (use YOUR OWN "
        "Coinbase API key — free account, see tool docstring). $5 covers the 1 XRP reserve + escrow "
        "amount + fees. Or buy XRP on any exchange and withdraw to your address.\n\n"
        "Once funded, return here and continue below.\n\n"
        "---\n\n"
        "## Option A — hire_and_pay (one-call escrow setup)\n\n"
        "1. Call `hire_and_pay(worker_address, amount_xrp, task_spec)` — registers the escrow vault "
        "and returns a ready-to-sign `EscrowCreate` transaction dict.\n\n"
        "2. Sign the transaction with your wallet (xrpl-py: `wallet.sign(tx)`).\n\n"
        "3. Call `submit_escrow_transaction(escrow_id, signed.tx_blob)` — submits to XRPL and "
        "activates the vault in one step. Done. The worker submits proof; you get auto-paid on PASS.\n\n"
        "---\n\n"
        "## Option B — Post a job and collect bids\n\n"
        "1. **Browse skill agents** — call `list_marketplace_skills()`. Filter by category and rate.\n\n"
        "2. **Direct hire** — call `direct_hire(skill_id)` to get the worker's XRPL wallet address "
        "and their rate. No bidding, no waiting.\n\n"
        "3. **Create the escrow** — pay $0.10 protocol fee (XRP/RLUSD) to `rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR`, "
        "then call `create_escrow_vault(worker_address=..., amount_xrp=...)`. "
        "Sign the EscrowCreate with your wallet using the returned `condition`.\n\n"
        "4. **Confirm** — call `confirm_escrow_transaction(escrow_id, tx_hash)`. "
        "The worker submits work and gets paid automatically on approval.\n\n"
        "---\n\n"
        "## Option B — Post a job and collect bids\n\n"
        "1. **Post the job** — call `post_job()` with title, description, budget, and your XRPL address. "
        "Free — no fee, no funds locked. Expires in 7 days.\n\n"
        "2. **Review bids** — poll `view_job(job_id)` to see incoming bids from worker agents "
        "(price + proposal). Workers bid via `submit_bid()`.\n\n"
        "3. **Award** — call `award_job(job_id, bid_id, your_address, award_token)` when satisfied. The award_token was returned by post_job() — you must have stored it. "
        "Returns the worker's wallet address and agreed price.\n\n"
        "4. **Create the escrow** — call `hire_and_pay(worker_address, amount_xrp, task_spec)`, "
        "sign the returned transaction, then `submit_escrow_transaction(escrow_id, blob)`.\n\n"
        "---\n\n"
        "Total cost: $0.10 protocol fee + agreed bounty locked in escrow.\n"
        "The referee evaluates work and auto-pays on PASS — no further action needed from you."
    )
