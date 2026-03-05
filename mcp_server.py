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

Note: annotations= kwarg removed — not supported in FastMCP < 2.0.
Tool descriptions are conveyed through docstrings, which all MCP clients read.
"""

import httpx
from fastmcp import FastMCP

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


@mcp.tool()
async def audit_task(
    task: str,
    work: str,
    fee_hash: str,
    task_category: str = "default",
    require_consensus: bool = False,
) -> dict:
    """
    Verify whether completed work meets a task specification using AI.

    Before calling, send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on XRPL Mainnet.
    Each fee_hash is single-use (anti-replay protection).

    Args:
        task: The task specification or requirements given to the worker.
        work: The completed work, output, or proof of completion.
        fee_hash: XRPL transaction hash of the 0.1 XRP payment to the protocol wallet.
        task_category: One of: default, creative, code, data, data_analysis,
                       bug_bounty, legal, supply_chain.
        require_consensus: If True, two models must agree. For high-stakes tasks.

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


@mcp.tool()
async def create_escrow_vault(
    escrow_id: str,
    fee_hash: str,
    task_description: str,
    worker_address: str,
    buyer_name: str,
    buyer_address: str,
    amount_xrp: float = None,
    amount_rlusd: float = None,
    currency: str = "XRP",
    project_label: str = "",
    cancel_after_hrs: int = 168,
    max_submissions: int = 3,
) -> dict:
    """
    Create an AI-gated XRPL escrow vault. Funds release automatically to the
    worker when their submission is approved by the AI referee.

    Before calling: pay 0.1 XRP protocol fee to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR.
    After calling: use the returned condition in an XRPL EscrowCreate transaction.

    Args:
        escrow_id: Unique receipt code, e.g. AT-7X9K-2MQ4
        fee_hash: XRPL tx hash of the 0.1 XRP protocol fee payment
        task_description: Detailed spec the worker must fulfil to be paid
        worker_address: XRPL wallet address of the worker
        buyer_name: Name or identifier of the buyer
        buyer_address: Buyer's XRPL wallet address
        amount_xrp: Amount of XRP to lock (when currency=XRP)
        amount_rlusd: Amount of RLUSD to lock (when currency=RLUSD)
        currency: "XRP" (default, no trustline needed) or "RLUSD" (USD-pegged stable)
        project_label: Optional human-readable label
        cancel_after_hrs: Hours until buyer can reclaim (default 168 = 7 days)
        max_submissions: How many attempts the worker gets (default 3)

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


@mcp.tool()
async def confirm_escrow_transaction(escrow_id: str, tx_hash: str) -> dict:
    """
    After submitting the EscrowCreate transaction on XRPL, call this to register
    the tx hash. The referee caches the escrow sequence number automatically.

    Args:
        escrow_id: The receipt code from create_escrow_vault
        tx_hash: XRPL transaction hash of the EscrowCreate transaction

    Returns:
        status: "confirmed", sequence: escrow sequence number
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/{escrow_id}/confirm",
            json={"tx_hash": tx_hash},
        )
        res.raise_for_status()
        return res.json()


@mcp.tool()
async def evaluate_escrow_work(
    escrow_id: str,
    work: str,
    task_category: str = "default",
    require_consensus: bool = False,
    evidence_links: list = None,
) -> dict:
    """
    Submit proof of completed work against an existing escrow vault.
    On approval, payment releases automatically — no EscrowFinish needed.

    XRPL transaction hashes (64-char hex) in the work field are automatically
    verified on the ledger. Useful as proof of NFT transfers, token payments,
    or any on-chain delivery — just paste the tx hash.

    Args:
        escrow_id: The receipt code provided by the buyer
        work: Completed work or proof of completion. Include XRPL tx hashes
              directly — they are auto-verified on the ledger.
        task_category: default, creative, code, data, data_analysis,
                       bug_bounty, legal, supply_chain
        require_consensus: Require two models to agree (high-stakes jobs)
        evidence_links: Up to 3 URLs fetched and snapshotted at submission time.

    Returns on PASS:
        status: "approved", auto_finish_queued: True

    Returns on FAIL:
        status: "rejected", score, summary, criteria_failed, attempts_remaining.

    Returns HTTP 429 if submission limit reached — purchase extra attempt for
    0.05 XRP via POST /evaluate/purchase-attempt.
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


@mcp.tool()
async def get_escrow_info(escrow_id: str) -> dict:
    """
    Retrieve metadata about an existing escrow vault. Never returns the fulfillment key.

    Args:
        escrow_id: The receipt code (e.g. AT-7X9K-2MQ4)

    Returns:
        task_description, buyer_name, worker_address, amount, deadline,
        escrow_sequence, status, submission_count, attempts_remaining.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/escrow/{escrow_id}")
        res.raise_for_status()
        return res.json()


@mcp.tool()
async def list_marketplace_jobs(
    category: str = "all",
    min_bounty_xrp: float = 0,
    limit: int = 20,
) -> dict:
    """
    Browse open bounties on the AgentTrust marketplace. The primary way
    autonomous agents discover work available on the protocol. All bounties
    are locked in XRPL escrow and pay in XRP automatically on AI approval.

    Workflow to claim a job:
      1. list_marketplace_jobs() — find a suitable job
      2. get_escrow_info(job.escrow_id) — verify escrow is live and check spec
      3. evaluate_escrow_work(job.escrow_id, your_work) — submit and get paid

    Args:
        category: all, code, data, data_analysis, creative, bug_bounty, legal, default
        min_bounty_xrp: Minimum bounty in XRP (e.g. 100 to see jobs worth >= 100 XRP)
        limit: Max jobs to return (default 20, max 100)

    Returns:
        jobs: List with id (use as escrow_id), title, description, bounty,
              deadline_hrs, poster, tags, status, is_demo.
        total: Total matching jobs.
        marketplace_url: Human-facing visual marketplace.
        note: Demo jobs (is_demo=true) are illustrative — no live escrow to claim.
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


@mcp.tool()
async def get_rlusd_quote(xrp_amount: float, worker_address: str) -> dict:
    """
    Get a live XRP to RLUSD conversion quote via the XRPL DEX.
    Use before claiming an escrow if you want RLUSD instead of XRP.

    Args:
        xrp_amount: Amount of XRP to convert
        worker_address: Your XRPL wallet address (also checks trustline status)

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


@mcp.tool()
async def get_xrp_price() -> dict:
    """
    Get the current live XRP price in USD and GBP. Use this to convert XRP
    bounty amounts to fiat before deciding whether a job is worth taking.

    Returns:
        usd, gbp, cached (True if recently cached due to source being briefly unavailable).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(f"{REFEREE_BASE}/xrp/price")
        res.raise_for_status()
        return res.json()
