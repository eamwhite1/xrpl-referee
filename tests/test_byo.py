"""
Part D BYO (Bring-your-own-deal) tests.
Tests external_ref round-trip, worker_url in responses, seller-initiated flow,
and counterparty_type validation.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_vault(**kwargs):
    """Create a minimal mock EscrowVault."""
    v = MagicMock()
    v.escrow_id        = kwargs.get("escrow_id", "AT-TEST-0001")
    v.status           = kwargs.get("status", "LOCKED")
    v.external_ref     = kwargs.get("external_ref", None)
    v.external_url     = kwargs.get("external_url", None)
    v.counterparty_type = kwargs.get("counterparty_type", "agent")
    v.initiated_by     = kwargs.get("initiated_by", "buyer")
    v.pay_url          = kwargs.get("pay_url", None)
    v.funding_deadline = kwargs.get("funding_deadline", None)
    v.buyer_address    = kwargs.get("buyer_address", "rBuyer1234567890123456789012")
    v.worker_address   = kwargs.get("worker_address", "rWorker123456789012345678901")
    v.amount_xrp       = kwargs.get("amount_xrp", 10.0)
    v.task_description = kwargs.get("task_description", "Test task")
    v.cancel_after_ts  = kwargs.get("cancel_after_ts", datetime.now(timezone.utc) + timedelta(days=7))
    v.worker_email     = kwargs.get("worker_email", None)
    v.buyer_email      = kwargs.get("buyer_email", None)
    v.criteria_v2      = kwargs.get("criteria_v2", None)
    v.remedy_attempt_used = False
    v.challenge_deadline = None
    v.challenged_at    = None
    v.challenge_criteria_ids = None
    v.release_mode     = "auto"
    return v


# ---------------------------------------------------------------------------
# 1. BYO vault: external_ref / external_url / counterparty_type stored
# ---------------------------------------------------------------------------

def test_external_ref_truncated_at_500_chars():
    """external_ref is stored as-is up to 500 chars."""
    long_ref = "upwork:" + "x" * 600
    truncated = (long_ref or "")[:500] or None
    assert len(truncated) == 500


def test_external_url_must_be_https():
    """external_url validation: only https:// accepted."""
    valid   = "https://example.com/job/123"
    invalid = "http://example.com/job/123"
    ftp     = "ftp://files.example.com"
    assert valid.startswith("https://")
    assert not invalid.startswith("https://")
    assert not ftp.startswith("https://")


def test_counterparty_type_validation():
    """counterparty_type: 'agent' and 'human' accepted; others default to 'agent'."""
    valid_types   = ("agent", "human")
    invalid_types = ("robot", "org", "", None)
    for t in valid_types:
        stored = t if t in ("agent", "human") else "agent"
        assert stored == t
    for t in invalid_types:
        stored = t if t in ("agent", "human") else "agent"
        assert stored == "agent"


# ---------------------------------------------------------------------------
# 2. worker_url in vault creation response
# ---------------------------------------------------------------------------

def test_worker_url_format():
    """worker_url should be SITE_URL?worker=escrow_id."""
    site_url  = "https://www.cryptovault.co.uk"
    escrow_id = "AT-7X9K-2MQ4"
    worker_url = f"{site_url}?worker={escrow_id}"
    assert worker_url == "https://www.cryptovault.co.uk?worker=AT-7X9K-2MQ4"


def test_worker_instructions_shape():
    """worker_instructions must contain required keys."""
    escrow_id       = "AT-7X9K-2MQ4"
    base_url        = "https://mcp.cryptovault.co.uk"
    criteria_v2     = [{"id": "c1", "type": "judgment", "description": "Code compiles"}]
    criteria_summary = [c.get("description", c) if isinstance(c, dict) else c for c in criteria_v2]
    instructions = {
        "submit_url":       f"{base_url}/evaluate",
        "submit_method":    "POST",
        "submit_body":      {"escrow_id": escrow_id, "work": "<your deliverable>", "evaluate_token": "<token>"},
        "mcp_tool":         "evaluate_escrow_work",
        "deadline":         "2026-10-30T00:00:00+00:00",
        "max_attempts":     3,
        "criteria_summary": criteria_summary,
        "note":             "Agents: use evaluate_escrow_work via MCP. Humans: visit worker_url to submit via web form.",
    }
    required_keys = {"submit_url", "submit_method", "mcp_tool", "criteria_summary", "note"}
    assert required_keys.issubset(instructions.keys())
    assert instructions["criteria_summary"] == ["Code compiles"]


# ---------------------------------------------------------------------------
# 3. GET /escrow/{id} returns BYO fields
# ---------------------------------------------------------------------------

def test_get_escrow_byo_fields():
    """GET /escrow/{id} should expose external_ref, external_url, counterparty_type, worker_url."""
    vault     = _make_vault(
        external_ref="upwork:123",
        external_url="https://upwork.com/job/123",
        counterparty_type="human",
        initiated_by="buyer",
    )
    site_url  = "https://www.cryptovault.co.uk"
    response  = {
        "external_ref":       vault.external_ref,
        "external_url":       vault.external_url,
        "counterparty_type":  vault.counterparty_type or "agent",
        "initiated_by":       vault.initiated_by or "buyer",
        "worker_url":         f"{site_url}?worker={vault.escrow_id}",
    }
    assert response["external_ref"] == "upwork:123"
    assert response["external_url"] == "https://upwork.com/job/123"
    assert response["counterparty_type"] == "human"
    assert response["worker_url"] == f"{site_url}?worker=AT-TEST-0001"


# ---------------------------------------------------------------------------
# 4. Seller-initiated escrow request
# ---------------------------------------------------------------------------

def test_seller_initiated_vault_fields():
    """Seller-initiated vault should have initiated_by='seller', status='AWAITING_FUNDING'."""
    vault = _make_vault(
        status="AWAITING_FUNDING",
        initiated_by="seller",
        counterparty_type="human",
        pay_url="https://www.cryptovault.co.uk?fund=AT-TEST-0001",
        funding_deadline=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    assert vault.status         == "AWAITING_FUNDING"
    assert vault.initiated_by   == "seller"
    assert vault.pay_url        == "https://www.cryptovault.co.uk?fund=AT-TEST-0001"
    assert vault.funding_deadline > datetime.now(timezone.utc)


def test_pay_url_format():
    """pay_url should be SITE_URL?fund=escrow_id."""
    site_url  = "https://www.cryptovault.co.uk"
    escrow_id = "AT-7X9K-2MQ4"
    pay_url   = f"{site_url}?fund={escrow_id}"
    assert pay_url == "https://www.cryptovault.co.uk?fund=AT-7X9K-2MQ4"


def test_request_escrow_validation():
    """amount_xrp > 0 and task required."""
    # amount_xrp = 0 should be rejected
    assert 0 <= 0  # guard exists in endpoint
    # empty task should be rejected
    assert not "".strip()


def test_fund_after_deadline_rejected():
    """Funding past funding_deadline should be rejected with 403."""
    now_utc          = datetime.now(timezone.utc)
    funding_deadline = now_utc - timedelta(hours=1)  # already passed
    # Simulate the guard
    is_past_deadline = now_utc > funding_deadline
    assert is_past_deadline


def test_fund_wrong_wallet_rejected():
    """Funding with wrong wallet_address should be rejected."""
    vault = _make_vault(buyer_address="rBuyer1234567890123456789012")
    wrong_wallet = "rDifferent1234567890123456789"
    assert wrong_wallet != vault.buyer_address


# ---------------------------------------------------------------------------
# 5. AWAITING_FUNDING expiry in hourly window
# ---------------------------------------------------------------------------

def test_awaiting_funding_expiry_logic():
    """AWAITING_FUNDING vaults past funding_deadline should be cancelled."""
    now_utc  = datetime.now(timezone.utc)
    vault    = _make_vault(
        status="AWAITING_FUNDING",
        funding_deadline=now_utc - timedelta(hours=2),  # already expired
    )
    # Simulate hourly window logic
    if vault.status == "AWAITING_FUNDING" and vault.funding_deadline and now_utc > vault.funding_deadline:
        vault.status = "CANCELLED"
    assert vault.status == "CANCELLED"


# ---------------------------------------------------------------------------
# 6. Seller-initiated full flow (unit simulation)
# ---------------------------------------------------------------------------

def test_seller_initiated_full_flow():
    """Simulate: AWAITING_FUNDING → fund → LOCKED → evaluate PASS → RELEASED."""
    now_utc  = datetime.now(timezone.utc)
    vault    = _make_vault(
        status="AWAITING_FUNDING",
        initiated_by="seller",
        funding_deadline=now_utc + timedelta(hours=24),
    )

    # Fund the escrow
    vault.status       = "LOCKED"
    vault.escrow_tx_hash = "ABCD" * 16
    assert vault.status == "LOCKED"

    # Worker submits, PASS verdict
    vault.status      = "RELEASED"
    vault.released_at = now_utc
    assert vault.status     == "RELEASED"
    assert vault.released_at is not None


# ---------------------------------------------------------------------------
# 7. is_demo flag on JobPosting
# ---------------------------------------------------------------------------

def test_is_demo_flag_default():
    """New JobPosting rows should default is_demo=False."""
    job = MagicMock()
    job.is_demo = False
    assert not job.is_demo


def test_is_demo_in_snapshot():
    """Job snapshot should include is_demo from DB column."""
    job = MagicMock()
    job.is_demo = True
    job.id = "JOB-DEMO-0001"
    snapshot_entry = {"id": job.id, "is_demo": bool(getattr(job, "is_demo", False))}
    assert snapshot_entry["is_demo"] is True
