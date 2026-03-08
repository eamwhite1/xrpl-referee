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
from typing import Annotated

mcp = FastMCP(
    name="AgentTrust Referee",
    instructions=(
        "The AgentTrust Referee is a trustless AI verdict engine built on the XRP Ledger. "
        "Use audit_task to verify whether completed work meets a task specification — "
        "requires a 0.1 XRP fee paid to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on XRPL Mainnet. "
        "Use create_escrow_vault to lock XRP or RLUSD in crypto-condition escrow gated by AI verdict. "
        "Use evaluate_escrow_work to submit proof against an existing vault — payment releases "
        "automatically to the worker's wallet on approval. No EscrowFinish needed. "
        "Use list_marketplace_jobs to browse live XRP bounties agents can claim. "
        "Use get_xrp_price to convert bounty amounts to fiat before deciding whether to take a job."
    ),
)

REFEREE_BASE = "https://xrpl-referee.onrender.com"


@mcp.tool(annotations=ToolAnnotations(
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
        description="64-character hex transaction hash of the 0.1 XRP payment to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR. Each hash is single-use.",
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

    Before calling, send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on XRPL Mainnet.
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
