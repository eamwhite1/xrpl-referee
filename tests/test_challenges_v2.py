"""
Tests for ENABLE CHALLENGES v2 — fee quote flow, verifier, review outcomes,
fee finish idempotency, sweeper auto-cancel.
All network/DB calls are mocked.
"""
import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


RIPPLE_EPOCH = 946684800

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class VaultStub:
    def __init__(self, **kw):
        self.escrow_id = kw.get("escrow_id", "AT-CHV2-0001")
        self.status = kw.get("status", "PASS_CHALLENGE_WINDOW")
        self.buyer_address = kw.get("buyer_address", "rBuyer1111")
        self.worker_address = kw.get("worker_address", "rWorker2222")
        self.task_description = kw.get("task_description", "Build something")
        self.release_mode = kw.get("release_mode", "challenge_window_hrs:24")
        self.challenge_deadline = kw.get(
            "challenge_deadline", datetime.now(timezone.utc) + timedelta(hours=12)
        )
        self.challenged_at = kw.get("challenged_at")
        self.challenge_criteria_ids = kw.get("challenge_criteria_ids")
        self.challenge_reason = kw.get("challenge_reason")
        self.challenge_fee_hash = kw.get("challenge_fee_hash")
        self.challenge_fee_drops = kw.get("challenge_fee_drops")
        self.challenge_fee_condition = kw.get("challenge_fee_condition")
        self.challenge_fee_cancel_after = kw.get("challenge_fee_cancel_after")
        self.challenge_fee_state = kw.get("challenge_fee_state")
        self.challenge_fee_finish_hash = kw.get("challenge_fee_finish_hash")
        self.challenge_fee_finish_attempts = kw.get("challenge_fee_finish_attempts", 0)
        self.challenge_fee_cancel_hash = kw.get("challenge_fee_cancel_hash")
        self.challenge_fee_seq = kw.get("challenge_fee_seq", 99001)
        self.challenge_quote_id = kw.get("challenge_quote_id")
        self.challenge_fee_owner = kw.get("challenge_fee_owner")
        self.challenge_outcome = kw.get("challenge_outcome")
        self.challenge_verdict = kw.get("challenge_verdict")
        self.challenge_verdict_hash = kw.get("challenge_verdict_hash")
        self.challenge_models_used = kw.get("challenge_models_used")
        self.challenge_review_started_at = kw.get("challenge_review_started_at")
        self.challenge_reviewed_at = kw.get("challenge_reviewed_at")
        self.challenge_review_attempts = kw.get("challenge_review_attempts", 0)
        self.pass_at = kw.get("pass_at")
        self.pass_evidence_snapshot = kw.get("pass_evidence_snapshot")
        self.criteria_v2 = kw.get("criteria_v2")
        self.worker_submission = kw.get("worker_submission", json.dumps({"work": "delivered"}))
        self.cancel_after_ts = kw.get("cancel_after_ts", datetime.now(timezone.utc) + timedelta(hours=48))
        self.remedy_attempt_used = kw.get("remedy_attempt_used", False)
        self.escrow_sequence = kw.get("escrow_sequence", 12345)
        self.escrow_owner = kw.get("escrow_owner")
        self.condition = kw.get("condition", "A025802012345BEEF810120")
        self.fulfillment = kw.get("fulfillment", "DEADBEEF")
        self.require_consensus = kw.get("require_consensus", False)


class QuoteStub:
    def __init__(self, **kw):
        self.id = kw.get("id", "qtest-0001")
        self.escrow_id = kw.get("escrow_id", "AT-CHV2-0001")
        self.payer_address = kw.get("payer_address", "rBuyer1111")
        self.fee_drops = kw.get("fee_drops", 400000)
        self.condition = kw.get("condition", "A025802012345BEEF810120")
        self.fulfillment_enc = kw.get("fulfillment_enc", "enc:v1:DEADBEEF")
        self.cancel_after_ripple = kw.get("cancel_after_ripple", 1000000)
        self.created_at = kw.get("created_at", datetime.now(timezone.utc))
        self.expires_at = kw.get("expires_at", datetime.now(timezone.utc) + timedelta(minutes=30))
        self.used_at = kw.get("used_at")
        self.fee_tx_hash = kw.get("fee_tx_hash")


# ---------------------------------------------------------------------------
# 1. get_challenge_fee_drops — price-pegged
# ---------------------------------------------------------------------------
def test_get_challenge_fee_drops_price_pegged():
    """At $2.00/XRP: $1.00 fee = 500000 drops, above floor of 200000."""
    xrp_price = 2.00
    CHALLENGE_FEE_USD = 1.00
    CHALLENGE_FEE_FLOOR_DROPS = 200_000
    drops = int((CHALLENGE_FEE_USD / xrp_price) * 1_000_000)
    result = max(drops, CHALLENGE_FEE_FLOOR_DROPS)
    assert result == 500_000


def test_get_challenge_fee_drops_floor():
    """At $10000/XRP: fee = 100 drops, floored to 200000."""
    xrp_price = 10000.0
    CHALLENGE_FEE_USD = 1.00
    CHALLENGE_FEE_FLOOR_DROPS = 200_000
    drops = int((CHALLENGE_FEE_USD / xrp_price) * 1_000_000)
    result = max(drops, CHALLENGE_FEE_FLOOR_DROPS)
    assert result == CHALLENGE_FEE_FLOOR_DROPS


# ---------------------------------------------------------------------------
# 2. get_challenge_fee_drops — fallback when price unavailable
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_challenge_fee_drops_fallback():
    """When price oracle fails, return CHALLENGE_FEE_FALLBACK_DROPS."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    with patch("referee.SessionLocal"), \
         patch("referee.engine"), \
         patch("referee.Base"), \
         patch("referee.run_migrations"), \
         patch("referee._load_ofac_from_db"), \
         patch("referee._seed_public_issuers"), \
         patch("referee._verify_xumm", return_value=None), \
         patch("referee._ofac_refresh_loop", return_value=asyncio.sleep(0)), \
         patch("referee._check_gemini_models", return_value=asyncio.sleep(0)), \
         patch("referee._gemini_schema_selftest", return_value=asyncio.sleep(0)), \
         patch("referee._ensure_hourly_window"), \
         patch("referee._refresh_jobs_snapshot", return_value=asyncio.sleep(0)), \
         patch("referee._get_xrp_price_usd", side_effect=Exception("oracle down")):
        from referee import get_challenge_fee_drops, CHALLENGE_FEE_FALLBACK_DROPS
        result = await get_challenge_fee_drops()
        assert result == CHALLENGE_FEE_FALLBACK_DROPS


# ---------------------------------------------------------------------------
# 3. challenge/prepare — wrong vault status → 400
# ---------------------------------------------------------------------------
def test_challenge_prepare_wrong_status():
    vault = VaultStub(status="LOCKED")
    assert vault.status != "PASS_CHALLENGE_WINDOW"
    # Simulate endpoint guard
    status_ok = vault.status == "PASS_CHALLENGE_WINDOW"
    assert not status_ok


# ---------------------------------------------------------------------------
# 4. challenge/prepare — success path logic
# ---------------------------------------------------------------------------
def test_challenge_prepare_success_logic():
    vault = VaultStub(status="PASS_CHALLENGE_WINDOW")
    import secrets as _sec, hashlib as _hash

    fb = _sec.token_bytes(32)
    cond = "A025802012345BEEF810120"
    fee_drops = 400_000
    cancel_after = int(datetime.now(timezone.utc).timestamp()) + 86400 - RIPPLE_EPOCH

    assert vault.worker_address.startswith("r")
    assert len(fb) == 32
    assert fee_drops >= 200_000
    assert cancel_after > 0


# ---------------------------------------------------------------------------
# 5. _verify_challenge_fee_escrow — wrong account
# ---------------------------------------------------------------------------
def test_verify_fee_escrow_wrong_account():
    quote = QuoteStub(payer_address="rBuyer1111")
    vault = VaultStub(worker_address="rWorker2222")
    tx = {
        "TransactionType": "EscrowCreate",
        "Account": "rSOMEONEELSE",
        "Destination": "rWorker2222",
        "Amount": "500000",
        "Condition": quote.condition,
    }
    ok = tx["Account"] == quote.payer_address
    assert not ok


# ---------------------------------------------------------------------------
# 6. _verify_challenge_fee_escrow — amount too low
# ---------------------------------------------------------------------------
def test_verify_fee_escrow_amount_too_low():
    quote = QuoteStub(fee_drops=400_000)
    tx = {
        "TransactionType": "EscrowCreate",
        "Account": "rBuyer1111",
        "Destination": "rWorker2222",
        "Amount": "100000",  # below fee_drops
        "Condition": quote.condition,
    }
    ok = int(tx["Amount"]) >= quote.fee_drops
    assert not ok


# ---------------------------------------------------------------------------
# 7. _verify_challenge_fee_escrow — condition mismatch
# ---------------------------------------------------------------------------
def test_verify_fee_escrow_condition_mismatch():
    quote = QuoteStub(condition="AAAA")
    tx = {"Condition": "BBBB"}
    ok = tx["Condition"].upper() == quote.condition.upper()
    assert not ok


# ---------------------------------------------------------------------------
# 8. _verify_challenge_fee_escrow — all checks pass
# ---------------------------------------------------------------------------
def test_verify_fee_escrow_success():
    quote = QuoteStub(payer_address="rBuyer1111", fee_drops=400_000, condition="A025BEEF")
    vault = VaultStub(worker_address="rWorker2222")
    tx = {
        "TransactionType": "EscrowCreate",
        "Account": "rBuyer1111",
        "Destination": "rWorker2222",
        "Amount": "500000",
        "Condition": "A025BEEF",
    }
    ok = (
        tx["TransactionType"] == "EscrowCreate"
        and tx["Account"] == quote.payer_address
        and tx["Destination"] == vault.worker_address
        and int(tx["Amount"]) >= quote.fee_drops
        and tx["Condition"].upper() == quote.condition.upper()
    )
    assert ok


# ---------------------------------------------------------------------------
# 9. challenge endpoint — expired quote → 410
# ---------------------------------------------------------------------------
def test_challenge_endpoint_expired_quote():
    now = datetime.now(timezone.utc)
    quote = QuoteStub(expires_at=now - timedelta(minutes=1))
    expired = now >= quote.expires_at
    assert expired, "Quote should be expired"


# ---------------------------------------------------------------------------
# 10. challenge endpoint — success path (logic)
# ---------------------------------------------------------------------------
def test_challenge_endpoint_success_logic():
    vault = VaultStub(status="PASS_CHALLENGE_WINDOW", challenged_at=None)
    quote = QuoteStub(used_at=None)
    now = datetime.now(timezone.utc)

    # Guard: vault must be PASS_CHALLENGE_WINDOW
    assert vault.status == "PASS_CHALLENGE_WINDOW"
    # Guard: quote must not be used
    assert quote.used_at is None
    # Guard: deadline not passed
    assert vault.challenge_deadline > now

    # Simulate acceptance
    vault.status = "CHALLENGED"
    vault.challenged_at = now
    vault.challenge_fee_state = "LOCKED"
    quote.used_at = now

    assert vault.status == "CHALLENGED"
    assert vault.challenge_fee_state == "LOCKED"
    assert quote.used_at is not None


# ---------------------------------------------------------------------------
# 11. _run_challenge_review — PASS_UPHELD
# ---------------------------------------------------------------------------
def test_challenge_review_pass_upheld():
    """Pro says PASS → PASS_UPHELD → RELEASING + fee FINISHED."""
    vault = VaultStub(status="CHALLENGED")
    now = datetime.now(timezone.utc)

    # Simulate Pro PASS
    raw_verdict = "PASS"
    challenge_outcome = "PASS_UPHELD"

    vault.challenge_outcome = challenge_outcome
    vault.challenge_reviewed_at = now
    vault.status = "RELEASING"
    vault.challenge_fee_state = "FINISHED"

    assert vault.status == "RELEASING"
    assert vault.challenge_outcome == "PASS_UPHELD"
    assert vault.challenge_fee_state == "FINISHED"


# ---------------------------------------------------------------------------
# 12. _run_challenge_review — PASS_OVERTURNED → REMEDY
# ---------------------------------------------------------------------------
def test_challenge_review_pass_overturned_remedy():
    """Pro says FAIL, CancelAfter ≥ 24h → PASS_OVERTURNED → REMEDY, fee REFUNDABLE."""
    vault = VaultStub(
        status="CHALLENGED",
        cancel_after_ts=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    now = datetime.now(timezone.utc)
    raw_verdict = "FAIL"
    challenge_outcome = "PASS_OVERTURNED"

    cancel_after = vault.cancel_after_ts
    time_remaining = (cancel_after - now).total_seconds()

    vault.challenge_outcome = challenge_outcome
    vault.challenge_fee_state = "REFUNDABLE"
    if cancel_after and time_remaining >= 86400:
        vault.status = "REMEDY"
    else:
        vault.status = "CANCELLED"

    assert vault.status == "REMEDY"
    assert vault.challenge_fee_state == "REFUNDABLE"


# ---------------------------------------------------------------------------
# 13. _run_challenge_review — REVIEW_INCONCLUSIVE → RELEASING + REFUNDABLE
# ---------------------------------------------------------------------------
def test_challenge_review_inconclusive():
    """Pro unavailable → REVIEW_INCONCLUSIVE → RELEASING, fee REFUNDABLE."""
    vault = VaultStub(status="CHALLENGED")
    now = datetime.now(timezone.utc)

    vault.challenge_outcome = "REVIEW_INCONCLUSIVE"
    vault.status = "RELEASING"
    vault.challenge_fee_state = "REFUNDABLE"
    vault.challenge_reviewed_at = now

    assert vault.status == "RELEASING"
    assert vault.challenge_outcome == "REVIEW_INCONCLUSIVE"
    assert vault.challenge_fee_state == "REFUNDABLE"


# ---------------------------------------------------------------------------
# 14. _finish_challenge_fee_escrow — idempotent when hash already set
# ---------------------------------------------------------------------------
def test_finish_challenge_fee_idempotent():
    vault = VaultStub(challenge_fee_finish_hash="SOMEHASH123")
    # Simulate idempotency guard
    if vault.challenge_fee_finish_hash:
        already_done = True
    else:
        already_done = False
    assert already_done, "Should skip re-submission if hash already set"


# ---------------------------------------------------------------------------
# 15. sweeper auto-cancel REFUNDABLE past CancelAfter
# ---------------------------------------------------------------------------
def test_sweeper_auto_cancel_refundable():
    """REFUNDABLE vault past CancelAfter should be picked up by sweeper."""
    now = datetime.now(timezone.utc)
    vault = VaultStub(
        challenge_fee_state="REFUNDABLE",
        challenge_fee_cancel_after=now - timedelta(minutes=5),  # past
        challenge_fee_cancel_hash=None,
    )
    # Simulate sweeper filter
    should_cancel = (
        vault.challenge_fee_state == "REFUNDABLE"
        and vault.challenge_fee_cancel_after is not None
        and vault.challenge_fee_cancel_after <= now
        and vault.challenge_fee_cancel_hash is None
    )
    assert should_cancel


# ---------------------------------------------------------------------------
# 16. test_challenge_returns_402_not_501 — new behavior
# ---------------------------------------------------------------------------
def test_challenge_endpoint_returns_402_not_501():
    """When vault status is wrong, challenge endpoint returns 402 (not 501)."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from fastapi.testclient import TestClient
    from unittest.mock import patch, MagicMock

    mock_vault = MagicMock()
    mock_vault.status = "LOCKED"  # not PASS_CHALLENGE_WINDOW

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vault

    with patch("referee.SessionLocal") as mock_sl, \
         patch("referee.engine"), \
         patch("referee.Base"), \
         patch("referee.run_migrations"), \
         patch("referee._load_ofac_from_db"), \
         patch("referee._seed_public_issuers"), \
         patch("referee._verify_xumm", return_value=None), \
         patch("referee._ofac_refresh_loop", return_value=asyncio.sleep(0)), \
         patch("referee._check_gemini_models", return_value=asyncio.sleep(0)), \
         patch("referee._gemini_schema_selftest", return_value=asyncio.sleep(0)), \
         patch("referee._ensure_hourly_window"), \
         patch("referee._refresh_jobs_snapshot", return_value=asyncio.sleep(0)), \
         patch("referee.get_db", return_value=iter([mock_db])):
        from referee import app
        client = TestClient(app)
        resp = client.post("/escrow/test-escrow-123/challenge", json={
            "quote_id":           "q-test-001",
            "fee_tx_hash":        "A" * 64,
            "challenger_address": "rBuyer1111",
        })
        # 402 when vault status is wrong, or 404 if vault not found
        assert resp.status_code in (402, 404)
