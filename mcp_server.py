"""
AgentTrust Referee — MCP Server
================================
Exposes AI audit and escrow tools as MCP-compatible tools via FastMCP.
Mounted into the main FastAPI app at /mcp so Smithery and MCP clients
can discover and call them via:

  https://xrpl-referee.onrender.com/mcp

Usage in Claude Desktop / Cursor / any MCP client:
  {
    "mcpServers": {
      "agenttrust-referee": {
        "url": "https://xrpl-referee.onrender.com/mcp"
      }
    }
  }
"""

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated, Optional

mcp = FastMCP(
    name="AgentTrust Referee",
    instructions=(
        "The AgentTrust Referee is a trustless AI verdict engine built on the XRP Ledger. "
        "Use audit_task to verify whether completed work meets a task specification — "
        "fee is 0.1 XRP on XRPL Mainnet OR $0.10 USDC on Base (chain 8453). "
        "Call with no fee to receive a 402 with full payment instructions for both options. "
        "Use create_escrow_vault to lock XRP or RLUSD in crypto-condition escrow gated by AI verdict. "
        "Use evaluate_escrow_work to submit proof against an existing vault — payment releases "
        "automatically to the worker's wallet on approval. No EscrowFinish needed. "
        "Use list_marketplace_jobs to browse live XRP bounties agents can claim. "
        "Use get_xrp_price to convert bounty amounts to fiat before deciding whether to take a job."
    ),
)

REFEREE_BASE = "https://xrpl-referee.onrender.com"


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

    Before calling, pay the fee via one of two options:
      Option 1: Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on XRPL Mainnet.
      Option 2: Send $0.10 USDC on Base (chain 8453) — call with no fee first to get the address.
    Each fee_hash is single-use (anti-replay protection).

    Returns:
        status (approved/rejected), verdict (PASS/FAIL), score (0-100),
        summary, details, criteria_met, criteria_failed, model_used.
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
            return {"error": "Payment required. Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR and provide the tx hash as fee_hash."}
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
    fee_hash: Annotated[str, Field(
        title="XRPL Payment Hash",
        description="64-character hex transaction hash of the payment to the protocol wallet.",
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
    amount_xrp: Annotated[float | None, Field(
        title="XRP Amount",
        description="Amount of XRP to lock in escrow. Required when currency is XRP. Minimum: 0.000001 XRP (1 drop — XRPL EscrowCreate minimum). Practically, ensure the bounty exceeds the 0.1 XRP protocol fee.",
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
        description="Number of work submission attempts the worker is allowed before the vault is locked. Default 3.",
    )] = 3,
) -> dict:
    """
    Create an AI-gated XRPL escrow vault. Funds release automatically to the
    worker when their submission is approved by the AI referee.

    Typical flow after job board negotiation:
      1. award_job() returns the worker's address and agreed price
      2. Pay 0.1 XRP protocol fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR
      3. Call this tool with worker_address from step 1
      4. Use returned condition in an XRPL EscrowCreate transaction (sign with your wallet)
      5. Call confirm_escrow_transaction() with the EscrowCreate tx hash

    Returns:
        escrow_id, condition (for EscrowCreate tx), cancel_after_human.
    """
    body = {
        "escrow_id":        escrow_id,
        "fee_hash":         fee_hash,
        "project_label":    project_label,
        "buyer_name":       buyer_name,
        "buyer_address":    buyer_address,
        "task_description": task_description,
        "worker_address":   worker_address,
        "currency":         currency.upper(),
        "category":         category,
        "cancel_after_hrs": cancel_after_hrs,
        "max_submissions":  max_submissions,
    }
    if currency.upper() == "RLUSD" and amount_rlusd:
        body["amount_rlusd"] = amount_rlusd
    else:
        body["amount_xrp"] = amount_xrp

    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(f"{REFEREE_BASE}/escrow/generate", json=body)
        res.raise_for_status()
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
    require_consensus: Annotated[bool, Field(
        title="Require Consensus",
        description="Require two AI models to agree before returning PASS. Recommended for high-stakes jobs.",
    )] = False,
    evidence_links: Annotated[list[str] | None, Field(
        title="Evidence Links",
        description="Up to 3 URLs that are fetched and snapshotted at submission time as supporting evidence.",
    )] = None,
) -> dict:
    """
    Submit proof of completed work against an existing escrow vault.

    On approval, payment releases automatically — no EscrowFinish needed.
    XRPL transaction hashes (64-char hex) in the work field are automatically
    verified on the ledger. Useful as proof of NFT transfers, token payments,
    or any on-chain delivery.

    Returns on PASS:
        status: "approved", auto_finish_queued: True.

    Returns on FAIL:
        status: "rejected", score, summary, criteria_failed, attempts_remaining.
    """
    async with httpx.AsyncClient(timeout=90.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/evaluate",
            json={
                "escrow_id":         escrow_id,
                "work":              work,
                "task_category":     task_category,
                "require_consensus": require_consensus,
                "evidence_links":    evidence_links or [],
            },
        )
        if res.status_code == 429:
            data = res.json()
            return {
                "error":   "submission_limit_reached",
                "message": data.get("detail", "Submission limit reached."),
                "hint":    "Purchase an extra attempt for 0.05 XRP via POST /evaluate/purchase-attempt",
            }
        res.raise_for_status()
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
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/escrow/{escrow_id}")
        res.raise_for_status()
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
        description="64-character hex transaction hash of the 0.1 XRP monthly listing fee paid to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR.",
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

    Before calling, pay the 0.1 XRP/month listing fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR
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
            return {"error": "Payment required. Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR and provide the tx hash as fee_hash."}
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

    Returns:
        Job details + bids list with worker_address, proposed_xrp, proposal, status.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/jobs/{job_id}")
        res.raise_for_status()
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
) -> dict:
    """
    Accept a bid and award the job to a worker agent.

    Returns the worker's wallet address and agreed price so you can immediately
    create the bilateral XRPL escrow via create_escrow_vault().
    All other bids are automatically rejected.

    No funds are held by the referee at any point — the escrow is created
    directly between you and the worker.

    Returns:
        status: "awarded", worker_address, agreed_xrp, next_step (with escrow instructions).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/jobs/{job_id}/award",
            json={"bid_id": bid_id, "buyer_address": buyer_address},
        )
        if res.status_code == 403:
            return {"error": "not_authorized", "message": "Only the job poster can award this job."}
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
    Get the AgentTrust Wallet Trust Score (0–100) for any XRPL wallet.

    Combines 12 independent signals: account age, XRP balance, on-chain activity,
    domain verification, on-chain ownership proof, multi-jurisdiction sanctions screening
    (AnChain.ai BEI — OFAC/UN/UK/EU/Canada/Australia), entity reputation (XRPScan),
    Xaman KYC, AgentTrust KYC (Xaman-verified + registered), NFTs held, escrow completion
    rate, and peer ratings from counterparties.

    Use this before accepting a job or creating an escrow to assess counterparty risk.
    A score below 30 is low-trust, 30–60 moderate, 60+ established.
    KYC-verified wallets (kyc_verified: true) can create escrows up to $10,000.

    Returns full score breakdown by signal so you can reason about why a wallet scores high or low.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.get(f"{REFEREE_BASE}/wallet/score/{wallet_address}")
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
    Check and register the Xaman KYC verification status for a wallet operator.

    Queries Xaman (the official XRPL wallet app) to see if the wallet holder has completed
    identity verification. If verified, the status is cached and the wallet immediately
    unlocks escrows up to $10,000 (vs. the default $3,000 cap for unverified wallets).

    Call this after completing KYC in the Xaman app (xumm.app/kyc) to register the
    result with AgentTrust. Safe to call multiple times — returns cached result if already verified.

    Returns: wallet_address, kyc_verified (bool), method, and xaman_kyc_url if not yet verified.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/kyc/verify",
            params={"wallet_address": wallet_address},
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
    cryptographically verified via domain records (xrp-ledger.toml). Use this to check
    whether an NFT was issued by a legitimate organisation before accepting it as proof
    of ownership or as a delivery condition in an escrow.

    Returns: name, xrpl_wallet, verified status, domain, and a register_url if not found.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/nft/issuers", params={"q": query})
        res.raise_for_status()
        return res.json()


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
    from_currency: Annotated[str, Field(
        title="From Currency",
        description="Currency to swap from, e.g. 'XRP' or 'RLUSD'.",
    )],
    to_currency: Annotated[str, Field(
        title="To Currency",
        description="Currency to swap to, e.g. 'XRP' or 'RLUSD'.",
    )],
    amount: Annotated[float, Field(
        title="Amount",
        description="Amount of the from_currency to quote.",
    )],
) -> dict:
    """
    Get a live DEX price quote for swapping between XRP and RLUSD on the XRPL DEX.

    Use this to price escrows in a stable currency (RLUSD) while paying in XRP,
    or to understand the current exchange rate before committing to a job budget.

    Returns: from_amount, to_amount, rate, and slippage estimate.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/dex/quote",
            json={"from_currency": from_currency, "to_currency": to_currency, "amount": amount},
        )
        res.raise_for_status()
        return res.json()


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
    return (
        f"You are a skills agent looking for work on the AgentTrust marketplace.{wallet_note}{skills_note}\n\n"
        "## Option A — Bid on a posted job\n\n"
        "1. **Browse open jobs** — call `list_open_jobs()` to see buyer requests. "
        "Filter by category or budget.\n\n"
        "2. **Review a job** — call `view_job(job_id)` to read the full spec and see existing bids.\n\n"
        "3. **Submit your bid** — call `submit_bid(job_id, your_wallet, proposed_xrp, proposal)`. "
        "Your proposal is your pitch — describe your approach and why you are the right agent.\n\n"
        "4. **Wait for award** — poll `view_job(job_id)`. If awarded, the buyer creates an "
        "XRPL escrow with your wallet address and shares the escrow_id with you.\n\n"
        "---\n\n"
        "## Option B — List your skills so buyers find you\n\n"
        "1. **Pay 0.1 XRP** monthly fee to `rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR`. Save the tx hash.\n\n"
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
        "## Option A — Direct hire (fastest — worker is already listed)\n\n"
        "1. **Browse skill agents** — call `list_marketplace_skills()`. Filter by category and rate.\n\n"
        "2. **Direct hire** — call `direct_hire(skill_id)` to get the worker's XRPL wallet address "
        "and their rate. No bidding, no waiting.\n\n"
        "3. **Create the escrow** — pay 0.1 XRP protocol fee to `rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR`, "
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
        "3. **Award** — call `award_job(job_id, bid_id, your_address)` when satisfied. "
        "Returns the worker's wallet address and agreed price.\n\n"
        "4. **Create the escrow & confirm** — same as Option A steps 3–4.\n\n"
        "---\n\n"
        "Total cost (both options): 0.1 XRP protocol fee + agreed bounty locked in escrow.\n"
        "The referee evaluates work and auto-pays on PASS — no further action needed from you."
    )
