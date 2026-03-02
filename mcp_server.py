"""
AgentTrust Referee — MCP Server
================================
Exposes the AI audit and escrow tools as MCP-compatible tools via FastMCP.
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

mcp = FastMCP(
    name="AgentTrust Referee",
    instructions=(
        "The AgentTrust Referee is a trustless AI verdict engine. "
        "Use audit_task to verify whether completed work meets a task specification — "
        "requires a 0.1 XRP fee paid to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR on XRPL Mainnet. "
        "Use create_escrow_vault to lock XRPL funds in crypto-condition escrow gated by AI verdict. "
        "Use evaluate_escrow_work to submit proof against an existing vault and receive the "
        "fulfillment key if approved."
    ),
)

REFEREE_BASE = "https://xrpl-referee.onrender.com"


# ---------------------------------------------------------------------------
# TOOL 1 — Standalone AI verdict (no escrow)
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "title": "Audit Task",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def audit_task(
    task: str,
    work: str,
    fee_hash: str,
    task_category: str = "default",
    require_consensus: bool = False,
) -> dict:
    """
    Verify whether completed work meets a task specification using AI.

    Before calling this tool, send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR
    on XRPL Mainnet and provide the transaction hash as fee_hash.
    Each fee_hash can only be used once (anti-replay protection).

    Args:
        task: The original task specification or requirements given to the worker.
        work: The completed work, output, or proof of completion.
        fee_hash: XRPL transaction hash of your 0.1 XRP payment to the protocol wallet.
        task_category: Domain-specific evaluation mode. One of: default, creative, code,
                      data, bug_bounty, legal, supply_chain.
        require_consensus: If True, two AI models must independently agree before
                          returning PASS. Recommended for high-stakes tasks.

    Returns:
        A structured verdict with:
        - status: "approved" or "rejected"
        - verdict: "PASS" or "FAIL"
        - score: 0-100 confidence score
        - summary: One-sentence conclusion
        - details: Full AI reasoning
        - criteria_met: List of requirements that were satisfied
        - criteria_failed: List of requirements that were not satisfied
        - model_used: Which AI model produced the verdict
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


# ---------------------------------------------------------------------------
# TOOL 2 — Create escrow vault
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "title": "Create Escrow Vault",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def create_escrow_vault(
    escrow_id: str,
    fee_hash: str,
    task_description: str,
    worker_address: str,
    amount_xrp: float,
    buyer_name: str,
    buyer_address: str,
    project_label: str = "",
    cancel_after_hrs: int = 168,
) -> dict:
    """
    Create an AI-gated XRPL escrow vault. Locks funds that release automatically
    when the worker's submission is approved by the AI referee.

    Before calling this tool:
    1. Send 0.1 XRP to rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR (protocol fee)
    2. Use the returned condition in an XRPL EscrowCreate transaction to lock funds

    Args:
        escrow_id: Unique receipt code for this job, e.g. AT-7X9K-2MQ4
        fee_hash: XRPL tx hash of your 0.1 XRP protocol fee payment
        task_description: Detailed task spec the worker must fulfil to be paid
        worker_address: XRPL wallet address of the worker who will receive payment
        amount_xrp: Amount of XRP to lock in escrow for the worker
        buyer_name: Name or identifier of the buyer/employer
        buyer_address: Buyer's XRPL wallet address
        project_label: Optional human-readable job label e.g. "Logo Design Jan 2026"
        cancel_after_hrs: Hours until the escrow expires and buyer can reclaim (default 168 = 7 days)

    Returns:
        - escrow_id: The receipt code to share with the worker
        - condition: SHA-256 crypto-condition hex — use in XRPL EscrowCreate transaction
        - cancel_after_human: Human-readable expiry datetime
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/generate",
            json={
                "escrow_id":        escrow_id,
                "fee_hash":         fee_hash,
                "project_label":    project_label,
                "buyer_name":       buyer_name,
                "buyer_address":    buyer_address,
                "task_description": task_description,
                "worker_address":   worker_address,
                "amount_xrp":       amount_xrp,
                "cancel_after_hrs": cancel_after_hrs,
            },
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# TOOL 3 — Confirm EscrowCreate (store tx hash)
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "title": "Confirm Escrow Transaction",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def confirm_escrow_transaction(
    escrow_id: str,
    tx_hash: str,
) -> dict:
    """
    After submitting the EscrowCreate transaction on XRPL, call this tool
    to store the tx hash. The referee will automatically look up and cache
    the escrow sequence number so the worker never needs to find it manually.

    Args:
        escrow_id: The receipt code from create_escrow_vault
        tx_hash: The XRPL transaction hash of your EscrowCreate transaction

    Returns:
        - status: "confirmed"
        - sequence: The escrow sequence number (cached for worker use)
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/escrow/{escrow_id}/confirm",
            json={"tx_hash": tx_hash},
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# TOOL 4 — Submit work for escrow-linked audit
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "title": "Evaluate Escrow Work",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def evaluate_escrow_work(
    escrow_id: str,
    work: str,
    task_category: str = "default",
    require_consensus: bool = False,
) -> dict:
    """
    Worker submits proof of completed work against an existing escrow vault.
    If the AI approves, the fulfillment key is returned — submit it in an
    XRPL EscrowFinish transaction to claim your payment.

    Args:
        escrow_id: The receipt code provided by the buyer
        work: Completed work, output, or proof of completion
        task_category: Domain hint for the AI evaluator (default, creative, code, etc.)
        require_consensus: Require two models to agree (high-stakes jobs)

    Returns on PASS:
        - status: "approved"
        - fulfillment: Hex string — include in XRPL EscrowFinish transaction
        - condition: Hex string — also required in EscrowFinish
        - buyer_address: Buyer's wallet (use as Owner in EscrowFinish)
        - escrow_sequence: Sequence number (use as OfferSequence in EscrowFinish)
        - amount_xrp: Amount you will receive (minus ~0.005 XRP XRPL network fee)

    Returns on FAIL:
        - status: "rejected"
        - verdict: Detailed feedback on what criteria were not met
    """
    async with httpx.AsyncClient(timeout=90.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/evaluate",
            json={
                "escrow_id":         escrow_id,
                "work":              work,
                "task_category":     task_category,
                "require_consensus": require_consensus,
            },
        )
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# TOOL 5 — Get escrow vault info
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "title": "Get Escrow Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_escrow_info(escrow_id: str) -> dict:
    """
    Retrieve metadata about an existing escrow vault by receipt code.
    Safe to call — never returns the fulfillment key.

    Args:
        escrow_id: The receipt code (e.g. AT-7X9K-2MQ4)

    Returns:
        Task description, buyer name, worker address, amount, deadline,
        escrow sequence number, and current status.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{REFEREE_BASE}/escrow/{escrow_id}")
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------------------
# TOOL 6 — DEX quote (XRP → RLUSD)
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "title": "Get RLUSD Quote",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_rlusd_quote(xrp_amount: float, worker_address: str) -> dict:
    """
    Get a live XRP to RLUSD conversion quote via the XRPL DEX.
    Use this before claiming an escrow if the worker wants RLUSD instead of XRP.

    Args:
        xrp_amount: Amount of XRP to convert
        worker_address: Worker's XRPL wallet address (checks trust line status)

    Returns:
        - estimated_rlusd: How much RLUSD the worker would receive
        - trust_line_ok: Whether the worker has a RLUSD trust line set up
        - slippage_warning: True if liquidity is low
        - trust_line_instructions: Setup instructions if trust line is missing
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{REFEREE_BASE}/dex/quote",
            json={"xrp_amount": xrp_amount, "worker_address": worker_address},
        )
        res.raise_for_status()
        return res.json()
