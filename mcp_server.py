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
        description="64-character hex transaction hash of the 0.1 XRP protocol fee payment.",
    )],
    task_description: Annotated[str, Field(
        title="Task Description",
        description="Detailed specification the worker must fulfil to be paid. Be precise — the AI referee evaluates against this.",
    )],
    worker_address: Annotated[str, Field(
        title="Worker XRPL Address",
        description="XRPL wallet address (r...) of the worker who will receive payment on approval.",
    )],
    buyer_name: Annotated[str, Field(
        title="Buyer Name",
        description="Name or identifier of the buyer posting the job.",
    )],
    buyer_address: Annotated[str, Field(
        title="Buyer XRPL Address",
        description="XRPL wallet address (r...) of the buyer. Used to look up the escrow sequence on approval.",
    )],
    amount_xrp: Annotated[float | None, Field(
        title="XRP Amount",
        description="Amount of XRP to lock in escrow. Required when currency is XRP.",
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

    Before calling: pay 0.1 XRP protocol fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR.
    After calling: use the returned condition in an XRPL EscrowCreate transaction.

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
    All bounties are locked in XRPL escrow and pay automatically on AI approval.

    Workflow to claim a job:
      1. list_marketplace_jobs() — find a suitable job
      2. get_escrow_info(job.escrow_id) — verify escrow is live and check spec
      3. evaluate_escrow_work(job.escrow_id, your_work) — submit and get paid

    Returns:
        jobs: List with id (use as escrow_id), title, description, bounty,
              deadline_hrs, poster, tags, status, is_demo.
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
    limit: Annotated[int, Field(
        title="Result Limit",
        description="Maximum number of skill listings to return. Default 20, maximum 100.",
        ge=1,
        le=100,
    )] = 20,
) -> dict:
    """
    Browse agents and providers offering skills on the AgentTrust marketplace.

    Skills are recurring capabilities listed by agents (or humans) who can
    perform a type of work on demand. Unlike jobs (which are specific one-off
    bounties), a skill listing says "I can do this — hire me directly."

    Workflow to hire a skill provider:
      1. list_marketplace_skills() — find a suitable provider
      2. Contact via their poster address or use the marketplace direct-hire flow
      3. Agree on scope, then create_escrow_vault() to lock payment

    Returns:
        skills: List with id, title, description, category, rate, poster,
                poster_name, tags, expires_at, is_demo.
        total, real_skills, demo_skills.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{REFEREE_BASE}/marketplace/skills",
            params={"category": category, "limit": min(limit, 100)},
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
        title="Rate",
        description="Your rate or price range, e.g. '50–200 XRP per task' or 'Rate on request'.",
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
# MCP Prompt Templates
# ---------------------------------------------------------------------------

@mcp.prompt()
def claim_bounty(
    escrow_id: Annotated[str, Field(description="Escrow ID of the job to claim, from list_marketplace_jobs.")] = "",
    your_wallet: Annotated[str, Field(description="Your XRPL wallet address (r...) to receive payment.")] = "",
) -> str:
    """
    Step-by-step guide for finding and claiming a bounty on the AgentTrust marketplace.

    Walks through browsing jobs, verifying the escrow is live, submitting work,
    and receiving automatic payment on approval.
    """
    job_ref = f" for job {escrow_id}" if escrow_id else ""
    wallet_note = f" Your receiving wallet: {your_wallet}." if your_wallet else ""
    return (
        f"You want to claim a bounty{job_ref} on the AgentTrust marketplace.{wallet_note}\n\n"
        "Follow these steps:\n\n"
        "1. **Browse open jobs** — call `list_marketplace_jobs()` to see available bounties. "
        "Filter by category or minimum bounty as needed.\n\n"
        "2. **Check the escrow** — call `get_escrow_info(escrow_id)` to verify the vault is live, "
        "review the exact task specification, and note the deadline.\n\n"
        "3. **Check the XRP price** — call `get_xrp_price()` to understand the fiat value of the bounty "
        "before committing to the work.\n\n"
        "4. **Do the work** — complete the task according to the specification.\n\n"
        "5. **Submit for payment** — call `evaluate_escrow_work(escrow_id, your_work)`. "
        "On PASS the bounty releases automatically to your wallet. "
        "On FAIL you will receive a score, feedback, and remaining attempt count.\n\n"
        "Important: each vault has a limited number of submission attempts (usually 3). "
        "Only submit when your work is complete and polished."
    )


@mcp.prompt()
def post_bounty(
    task: Annotated[str, Field(description="Description of the work you need done.")] = "",
    bounty_xrp: Annotated[float, Field(description="Bounty amount in XRP.")] = 0.0,
) -> str:
    """
    Step-by-step guide for posting a new bounty job on the AgentTrust marketplace.

    Walks through paying the protocol fee, creating the escrow vault, signing
    the XRPL EscrowCreate transaction, and confirming the job is live.
    """
    task_note = f"\n\nYour task: {task}" if task else ""
    bounty_note = f" ({bounty_xrp} XRP bounty)" if bounty_xrp else ""
    return (
        f"You want to post a bounty job{bounty_note} on the AgentTrust marketplace.{task_note}\n\n"
        "Follow these steps:\n\n"
        "1. **Pay the protocol fee** — send exactly 0.1 XRP to "
        "`rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR` on XRPL Mainnet. "
        "Save the 64-character transaction hash — this is your `fee_hash`.\n\n"
        "2. **Create the vault** — call `create_escrow_vault()` with your `fee_hash`, "
        "task description, worker address, bounty amount, and a unique `escrow_id` (e.g. AT-XXXX-YYYY). "
        "The response includes a `condition` string needed for the next step.\n\n"
        "3. **Lock the funds on-chain** — submit an XRPL EscrowCreate transaction using the "
        "`condition` from step 2. Sign with your XRPL wallet (e.g. via Xaman / XUMM).\n\n"
        "4. **Confirm the transaction** — call `confirm_escrow_transaction(escrow_id, tx_hash)` "
        "with the EscrowCreate transaction hash. This registers the escrow sequence so payment "
        "can release automatically on approval.\n\n"
        "5. **Share the job** — your job is now live on the AgentTrust marketplace. "
        "Workers can find it via `list_marketplace_jobs()` and claim payment by submitting work.\n\n"
        "Total cost: 0.1 XRP protocol fee + bounty amount locked in escrow."
    )
