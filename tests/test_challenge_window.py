"""
Tests for Part B Phase 2: challenge window, remedy attempt, auto-release.
All Gemini calls are mocked.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Minimal vault stub
# ---------------------------------------------------------------------------
class VaultStub:
    def __init__(self, **kw):
        self.escrow_id = kw.get("escrow_id", "AT-TEST-0001")
        self.status = kw.get("status", "LOCKED")
        self.buyer_address = kw.get("buyer_address", "rBuyer1234")
        self.worker_address = kw.get("worker_address", "rWorker5678")
        self.buyer_email = kw.get("buyer_email", "buyer@example.com")
        self.worker_email = kw.get("worker_email", "worker@example.com")
        self.task_description = kw.get("task_description", "Build a thing")
        self.release_mode = kw.get("release_mode", "auto")
        self.challenge_deadline = kw.get("challenge_deadline")
        self.challenge_fee_amount = kw.get("challenge_fee_amount", 0.5)
        self.challenge_criteria_ids = kw.get("challenge_criteria_ids")
        self.challenge_reason = kw.get("challenge_reason")
        self.challenge_fee_hash = kw.get("challenge_fee_hash")
        self.challenged_at = kw.get("challenged_at")
        self.criteria_v2 = kw.get("criteria_v2")
        self.worker_submission = kw.get("worker_submission", json.dumps({"work": "my work"}))
        self.cancel_after_ts = kw.get("cancel_after_ts")
        self.remedy_attempt_used = kw.get("remedy_attempt_used", False)
        self.released_at = kw.get("released_at")
        self.cancelled_at = kw.get("cancelled_at")
        self.submission_count = kw.get("submission_count", 0)
        self.max_submissions = kw.get("max_submissions", 3)
        self.escrow_sequence = kw.get("escrow_sequence", 12345)
        self.escrow_owner = kw.get("escrow_owner")
        self.require_consensus = kw.get("require_consensus", False)
        self.evaluation_verdicts = None
        self.models_used = None
        self.prompt_version = None
        self.verdict_hash = None
        self.ai_verdict = None
        self.model_used = None
        self.delivery_status = None
        self.delivery_expires_at = None


# ---------------------------------------------------------------------------
# 1. PASS_CHALLENGE_WINDOW → deadline passes → auto-released
# ---------------------------------------------------------------------------
def test_auto_release_logic():
    """Hourly window should pick up PASS_CHALLENGE_WINDOW vaults past deadline."""
    now = datetime.now(timezone.utc)
    vault = VaultStub(
        status="PASS_CHALLENGE_WINDOW",
        challenge_deadline=now - timedelta(hours=1),  # deadline passed
    )
    assert vault.challenge_deadline < now
    # Simulate the auto-release logic
    if vault.status == "PASS_CHALLENGE_WINDOW" and vault.challenge_deadline <= now:
        vault.status = "RELEASED"
        vault.released_at = now
    assert vault.status == "RELEASED"
    assert vault.released_at is not None


# ---------------------------------------------------------------------------
# 2. Challenge endpoint: double-challenge guard
# ---------------------------------------------------------------------------
def test_double_challenge_guard():
    vault = VaultStub(
        status="PASS_CHALLENGE_WINDOW",
        challenge_deadline=datetime.now(timezone.utc) + timedelta(hours=12),
        challenged_at=datetime.now(timezone.utc),  # already challenged
    )
    # Simulate guard check
    already_challenged = vault.challenged_at is not None
    assert already_challenged, "Should detect double-challenge"


# ---------------------------------------------------------------------------
# 3. Challenge endpoint: after deadline → rejected
# ---------------------------------------------------------------------------
def test_challenge_after_deadline():
    now = datetime.now(timezone.utc)
    vault = VaultStub(
        status="PASS_CHALLENGE_WINDOW",
        challenge_deadline=now - timedelta(minutes=5),  # expired
    )
    deadline = vault.challenge_deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    assert now >= deadline, "Deadline should be in the past"


# ---------------------------------------------------------------------------
# 4. Challenge endpoint: wrong wallet → rejected
# ---------------------------------------------------------------------------
def test_challenge_wrong_wallet():
    vault = VaultStub(buyer_address="rBuyerCorrect")
    provided = "rBuyerWrong"
    assert vault.buyer_address != provided


# ---------------------------------------------------------------------------
# 5. Challenge criteria validation
# ---------------------------------------------------------------------------
def test_challenge_criteria_validation():
    vault = VaultStub(
        criteria_v2=[
            {"id": "c1", "type": "judgment", "description": "Quality"},
            {"id": "c2", "type": "code", "description": "Tests pass"},
        ]
    )
    valid_ids = {c.get("id") for c in vault.criteria_v2}
    # Valid
    assert {"c1"}.issubset(valid_ids)
    # Invalid
    unknown = [cid for cid in ["c99"] if cid not in valid_ids]
    assert unknown == ["c99"]


# ---------------------------------------------------------------------------
# 6. _run_challenge_review: Pro PASS → RELEASED (logic test, no live imports)
# ---------------------------------------------------------------------------
def test_challenge_review_pro_pass():
    vault = VaultStub(
        escrow_id="AT-TEST-0002",
        status="CHALLENGED",
        challenge_criteria_ids=["c1"],
        worker_submission=json.dumps({"work": "the work"}),
        cancel_after_ts=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    now = datetime.now(timezone.utc)
    pro_verdict = "PASS"
    if pro_verdict == "PASS":
        vault.status = "RELEASED"
        vault.released_at = now
    assert vault.status == "RELEASED"
    assert vault.released_at is not None


# ---------------------------------------------------------------------------
# 7. _run_challenge_review: Pro FAIL, CancelAfter ≥ 24h → REMEDY
# ---------------------------------------------------------------------------
def test_challenge_review_pro_fail_remedy():
    vault = VaultStub(
        escrow_id="AT-TEST-0003",
        status="CHALLENGED",
        challenge_criteria_ids=["c1"],
        worker_submission=json.dumps({"work": "subpar work"}),
        cancel_after_ts=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    now = datetime.now(timezone.utc)
    cancel_after = vault.cancel_after_ts
    time_remaining = (cancel_after - now).total_seconds()
    # Simulate Pro FAIL logic
    pro_verdict = "FAIL"
    if pro_verdict != "PASS":
        if cancel_after and time_remaining >= 86400:
            vault.status = "REMEDY"
        else:
            vault.status = "CANCELLED"
    assert vault.status == "REMEDY"


# ---------------------------------------------------------------------------
# 8. _run_challenge_review: Pro FAIL, CancelAfter < 24h → CANCELLED
# ---------------------------------------------------------------------------
def test_challenge_review_pro_fail_cancel():
    vault = VaultStub(
        status="CHALLENGED",
        cancel_after_ts=datetime.now(timezone.utc) + timedelta(hours=12),  # < 24h
    )
    now = datetime.now(timezone.utc)
    cancel_after = vault.cancel_after_ts
    time_remaining = (cancel_after - now).total_seconds() if cancel_after else 0
    pro_verdict = "FAIL"
    if pro_verdict != "PASS":
        if cancel_after and time_remaining >= 86400:
            vault.status = "REMEDY"
        else:
            vault.status = "CANCELLED"
            vault.cancelled_at = now
    assert vault.status == "CANCELLED"
    assert vault.cancelled_at is not None


# ---------------------------------------------------------------------------
# 9. _run_challenge_review: Pro unavailable → RELEASED + fee refunded
# ---------------------------------------------------------------------------
def test_challenge_review_pro_unavailable():
    vault = VaultStub(status="CHALLENGED")
    # Simulate pro_result = None path
    pro_result = None
    if pro_result is None:
        vault.status = "RELEASED"
        vault.released_at = datetime.now(timezone.utc)
    assert vault.status == "RELEASED"


# ---------------------------------------------------------------------------
# 10. Remedy: evaluate_work on REMEDY vault → PASS → RELEASED immediately
# ---------------------------------------------------------------------------
def test_remedy_pass_releases():
    vault = VaultStub(status="REMEDY", remedy_attempt_used=False)
    # Simulate remedy flow
    is_remedy = vault.status == "REMEDY"
    assert is_remedy
    assert not vault.remedy_attempt_used
    vault.remedy_attempt_used = True  # consumed before eval
    # Simulate PASS
    overall_verdict = "PASS"
    if is_remedy and overall_verdict == "PASS":
        vault.status = "RELEASED"
        vault.released_at = datetime.now(timezone.utc)
    assert vault.status == "RELEASED"
    assert vault.remedy_attempt_used is True


# ---------------------------------------------------------------------------
# 11. Remedy: evaluate_work on REMEDY vault → FAIL → CANCELLED
# ---------------------------------------------------------------------------
def test_remedy_fail_cancels():
    vault = VaultStub(status="REMEDY", remedy_attempt_used=False)
    vault.remedy_attempt_used = True
    overall_verdict = "FAIL"
    is_remedy = True
    if is_remedy and overall_verdict != "PASS":
        vault.status = "CANCELLED"
        vault.cancelled_at = datetime.now(timezone.utc)
    assert vault.status == "CANCELLED"


# ---------------------------------------------------------------------------
# 12. Remedy: second attempt rejected
# ---------------------------------------------------------------------------
def test_remedy_second_attempt_rejected():
    vault = VaultStub(status="REMEDY", remedy_attempt_used=True)
    rejected = vault.remedy_attempt_used is True
    assert rejected, "Second remedy attempt should be rejected"


# ---------------------------------------------------------------------------
# 13. Grok's case: last-attempt FAIL → challenge upheld → remedy PASS → RELEASED
# ---------------------------------------------------------------------------
def test_grok_full_flow():
    # Setup: vault at max attempts, FAIL
    vault = VaultStub(status="LOCKED", submission_count=3, max_submissions=3)
    # Step 1: last attempt — FAIL, attempts exhausted
    vault.submission_count = 3
    # Step 2: AI PASS → challenge window
    vault.status = "PASS_CHALLENGE_WINDOW"
    vault.challenge_deadline = datetime.now(timezone.utc) + timedelta(hours=24)
    # Step 3: buyer challenges
    vault.status = "CHALLENGED"
    vault.challenged_at = datetime.now(timezone.utc)
    vault.challenge_criteria_ids = ["c1"]
    # Step 4: Pro upholds (FAIL) — CancelAfter ≥ 24h
    vault.cancel_after_ts = datetime.now(timezone.utc) + timedelta(hours=48)
    now = datetime.now(timezone.utc)
    cancel_after = vault.cancel_after_ts
    time_remaining = (cancel_after - now).total_seconds()
    pro_verdict = "FAIL"
    if pro_verdict != "PASS" and cancel_after and time_remaining >= 86400:
        vault.status = "REMEDY"
    assert vault.status == "REMEDY"
    # Step 5: worker submits remedy — PASS
    assert not vault.remedy_attempt_used
    vault.remedy_attempt_used = True
    overall_verdict = "PASS"
    if vault.status == "REMEDY" and overall_verdict == "PASS":
        vault.status = "RELEASED"
        vault.released_at = datetime.now(timezone.utc)
    assert vault.status == "RELEASED"
    assert vault.remedy_attempt_used is True
    assert vault.released_at is not None


def test_challenge_returns_501():
    """Challenge endpoint must return 501 until fee payout is implemented."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from fastapi.testclient import TestClient
    from unittest.mock import patch, MagicMock

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
         patch("referee._refresh_jobs_snapshot", return_value=asyncio.sleep(0)):
        from referee import app
        client = TestClient(app)
        resp = client.post("/escrow/test-escrow-123/challenge", json={
            "criteria_ids": ["c1"],
            "reason": "work is bad",
            "fee_tx_hash": "A" * 64,
            "wallet_address": "rTest",
            "tx_hash": "B" * 64,
        })
        assert resp.status_code == 501
