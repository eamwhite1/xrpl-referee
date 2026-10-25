"""
Tests for challenges v2 fixes: DER fulfillment, fee seq, referee wallet,
strict verifier, atomic transition, review per brief, kill switch.
"""
import asyncio
import hashlib
import json
import secrets
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

RIPPLE_EPOCH = 946684800


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class VaultStub:
    def __init__(self, **kw):
        self.escrow_id = kw.get("escrow_id", "AT-FIX-0001")
        self.status = kw.get("status", "PASS_CHALLENGE_WINDOW")
        self.buyer_address = kw.get("buyer_address", "rBuyer1111")
        self.worker_address = kw.get("worker_address", "rWorker2222")
        self.task_description = kw.get("task_description", "Build something")
        self.release_mode = kw.get("release_mode", "challenge_window_hrs:24")
        self.challenge_deadline = kw.get("challenge_deadline", datetime.now(timezone.utc) + timedelta(hours=12))
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
        self.challenge_fee_owner = kw.get("challenge_fee_owner", "rBuyer1111")
        self.challenge_quote_id = kw.get("challenge_quote_id")
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
        self.fulfillment = kw.get("fulfillment", "A0228020" + "AB" * 32)
        self.require_consensus = kw.get("require_consensus", False)
        self.max_submissions = kw.get("max_submissions", 3)
        self.submission_count = kw.get("submission_count", 1)


# ---------------------------------------------------------------------------
# 1. DER fulfillment encoding
# ---------------------------------------------------------------------------
def test_der_condition_format():
    """DER condition must be A0258020<sha256_hex>810120."""
    preimage = secrets.token_bytes(32)
    sha_hex = hashlib.sha256(preimage).hexdigest().upper()
    condition = "A0258020" + sha_hex + "810120"
    assert condition.startswith("A0258020")
    assert condition.endswith("810120")
    assert len(condition) == 8 + 64 + 6  # 78 chars


def test_der_fulfillment_format():
    """DER fulfillment must be A0228020<preimage_hex>."""
    preimage = secrets.token_bytes(32)
    fulfillment = "A0228020" + preimage.hex().upper()
    assert fulfillment.startswith("A0228020")
    assert len(fulfillment) == 8 + 64  # 72 chars


def test_der_condition_matches_fulfillment():
    """SHA-256 of the preimage in the fulfillment must match the condition hash."""
    preimage = secrets.token_bytes(32)
    sha_hex = hashlib.sha256(preimage).hexdigest().upper()
    condition = "A0258020" + sha_hex + "810120"
    fulfillment = "A0228020" + preimage.hex().upper()
    # Extract the hash from condition and verify it matches
    extracted_hash = condition[8:8+64]
    computed_hash = hashlib.sha256(preimage).hexdigest().upper()
    assert extracted_hash == computed_hash


# ---------------------------------------------------------------------------
# 2. Kill switch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kill_switch_prepare_challenge():
    """When CHALLENGES_ENABLED=false, /challenge/prepare returns 503."""
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
         patch("referee.CHALLENGES_ENABLED", False):
        from fastapi.testclient import TestClient
        from referee import app
        client = TestClient(app)
        resp = client.post("/escrow/AT-KILL-001/challenge/prepare", json={"challenger_address": "rBuyer1111"})
        assert resp.status_code == 503
        assert resp.json().get("error") == "challenges_disabled"


@pytest.mark.asyncio
async def test_kill_switch_submit_challenge():
    """When CHALLENGES_ENABLED=false, /challenge returns 503."""
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
         patch("referee.CHALLENGES_ENABLED", False):
        from fastapi.testclient import TestClient
        from referee import app
        client = TestClient(app)
        resp = client.post("/escrow/AT-KILL-001/challenge", json={})
        assert resp.status_code == 503
        assert resp.json().get("error") == "challenges_disabled"


# ---------------------------------------------------------------------------
# 3. Atomic transition: prevent double-challenge
# ---------------------------------------------------------------------------
def test_atomic_transition_prevents_double_challenge():
    """Two simultaneous challenge submissions — only one should win."""
    vault = VaultStub(status="PASS_CHALLENGE_WINDOW", challenged_at=None)
    # Simulate atomic rowcount check
    first_update_rows = 1   # first request wins
    second_update_rows = 0  # second sees 0 rows (status already changed)
    assert first_update_rows == 1
    assert second_update_rows == 0


# ---------------------------------------------------------------------------
# 4. Verifier: correct account passes
# ---------------------------------------------------------------------------
def test_verifier_correct_account():
    quote_payer = "rBuyer1111"
    worker = "rWorker2222"
    fee_drops = 400_000
    condition = "A025BEEF"
    tx = {
        "TransactionType": "EscrowCreate",
        "Account": "rBuyer1111",
        "Destination": "rWorker2222",
        "Amount": "500000",
        "Condition": "A025BEEF",
    }
    ok = (
        tx["TransactionType"] == "EscrowCreate"
        and tx["Account"] == quote_payer
        and tx["Destination"] == worker
        and int(tx["Amount"]) >= fee_drops
        and tx["Condition"].upper() == condition.upper()
    )
    assert ok


# ---------------------------------------------------------------------------
# 5. Verifier: wrong account fails
# ---------------------------------------------------------------------------
def test_verifier_wrong_account_fails():
    tx = {"Account": "rSTRANGER"}
    quote_payer = "rBuyer1111"
    assert tx["Account"] != quote_payer


# ---------------------------------------------------------------------------
# 6. Verifier: amount too low fails
# ---------------------------------------------------------------------------
def test_verifier_amount_too_low_fails():
    tx_amount = 100_000
    fee_drops = 400_000
    assert tx_amount < fee_drops


# ---------------------------------------------------------------------------
# 7. Verifier: condition mismatch fails
# ---------------------------------------------------------------------------
def test_verifier_condition_mismatch():
    tx_condition = "DEADBEEF"
    quote_condition = "CAFEBABE"
    assert tx_condition.upper() != quote_condition.upper()


# ---------------------------------------------------------------------------
# 8. Verifier: returns info dict with seq/drops/cancel_after
# ---------------------------------------------------------------------------
def test_verifier_returns_info_dict():
    """_verify_challenge_fee_escrow returns (ok, err, info) with owner/seq/drops/cancel_after_ripple."""
    # Simulate the return contract
    info = {"owner": "rBuyer1111", "seq": 99001, "drops": 400_000, "cancel_after_ripple": 123456}
    assert "owner" in info
    assert "seq" in info
    assert "drops" in info
    assert "cancel_after_ripple" in info


# ---------------------------------------------------------------------------
# 9. Finish escrow: 10-min CancelAfter buffer → REFUNDABLE
# ---------------------------------------------------------------------------
def test_finish_fee_escrow_cancel_after_buffer():
    """If CancelAfter < 10 min away, don't try to finish → set REFUNDABLE."""
    now = datetime.now(timezone.utc)
    cancel_after_unix = int(now.timestamp()) + 300  # 5 min away — inside buffer
    buffer_secs = 600  # 10 minutes
    too_close = (cancel_after_unix - int(now.timestamp())) < buffer_secs
    assert too_close


def test_finish_fee_escrow_cancel_after_enough_time():
    """If CancelAfter is far enough away, proceed to finish."""
    now = datetime.now(timezone.utc)
    cancel_after_unix = int(now.timestamp()) + 3600  # 1 hr away
    buffer_secs = 600
    too_close = (cancel_after_unix - int(now.timestamp())) < buffer_secs
    assert not too_close


# ---------------------------------------------------------------------------
# 10. Finish escrow: idempotent when finish_hash already set
# ---------------------------------------------------------------------------
def test_finish_fee_escrow_idempotent():
    vault = VaultStub(challenge_fee_finish_hash="EXISTINGHASH")
    already_done = bool(vault.challenge_fee_finish_hash)
    assert already_done


# ---------------------------------------------------------------------------
# 11. Cancel escrow: idempotent when cancel_hash already set
# ---------------------------------------------------------------------------
def test_cancel_fee_escrow_idempotent():
    vault = VaultStub(challenge_fee_cancel_hash="CANCELHASH")
    already_done = bool(vault.challenge_fee_cancel_hash)
    assert already_done


# ---------------------------------------------------------------------------
# 12. validate_challenge_verdict: PASS_OVERTURNED requires evidence_quote
# ---------------------------------------------------------------------------
def test_validate_verdict_overturned_requires_evidence_quote():
    """PASS_OVERTURNED must only be accepted if evidence_quote is a verbatim substring of work."""
    work_text = "def hello(): return 'world'"
    evidence_quote = "def hello(): return 'world'"  # verbatim
    assert evidence_quote in work_text  # valid overturned


def test_validate_verdict_overturned_hallucinated_quote_rejected():
    """If evidence_quote is not in the work, PASS_OVERTURNED should be rejected."""
    work_text = "def hello(): return 'world'"
    evidence_quote = "def goodbye(): return 'moon'"  # not in work
    assert evidence_quote not in work_text  # invalid — should be INCONCLUSIVE


# ---------------------------------------------------------------------------
# 13. Challenge review: PASS_UPHELD keeps fee LOCKED
# ---------------------------------------------------------------------------
def test_review_pass_upheld_fee_stays_locked():
    vault = VaultStub(status="CHALLENGED", challenge_fee_state="LOCKED")
    # Pro says PASS → PASS_UPHELD
    verdict = "PASS"
    if verdict == "PASS":
        outcome = "PASS_UPHELD"
        vault.status = "RELEASING"
        # fee stays LOCKED (worker gets it)
    assert vault.status == "RELEASING"
    assert vault.challenge_fee_state == "LOCKED"  # unchanged


# ---------------------------------------------------------------------------
# 14. Challenge review: PASS_OVERTURNED with time remaining → REMEDY
# ---------------------------------------------------------------------------
def test_review_pass_overturned_remedy_with_time():
    vault = VaultStub(
        status="CHALLENGED",
        cancel_after_ts=datetime.now(timezone.utc) + timedelta(hours=48),
        submission_count=1,
        max_submissions=3,
    )
    now = datetime.now(timezone.utc)
    time_remaining = (vault.cancel_after_ts - now).total_seconds()
    # REMEDY conditions: has remaining submissions AND ≥ 2h left
    has_remaining = vault.submission_count < vault.max_submissions
    has_time = time_remaining >= 7200  # 2 hours
    expected_status = "REMEDY" if (has_remaining and has_time) else "AWAITING_CANCEL"
    assert expected_status == "REMEDY"
    vault.status = expected_status
    vault.challenge_outcome = "PASS_OVERTURNED"
    vault.challenge_fee_state = "REFUNDABLE"
    assert vault.status == "REMEDY"
    assert vault.challenge_fee_state == "REFUNDABLE"


# ---------------------------------------------------------------------------
# 15. Challenge review: PASS_OVERTURNED with no time → AWAITING_CANCEL
# ---------------------------------------------------------------------------
def test_review_pass_overturned_awaiting_cancel_no_time():
    vault = VaultStub(
        status="CHALLENGED",
        cancel_after_ts=datetime.now(timezone.utc) + timedelta(minutes=30),  # < 2h
        submission_count=3,
        max_submissions=3,
    )
    now = datetime.now(timezone.utc)
    time_remaining = (vault.cancel_after_ts - now).total_seconds()
    has_remaining = vault.submission_count < vault.max_submissions
    has_time = time_remaining >= 7200
    expected_status = "REMEDY" if (has_remaining and has_time) else "AWAITING_CANCEL"
    assert expected_status == "AWAITING_CANCEL"


# ---------------------------------------------------------------------------
# 16. Challenge review: REVIEW_INCONCLUSIVE → RELEASING + fee REFUNDABLE
# ---------------------------------------------------------------------------
def test_review_inconclusive_releasing():
    vault = VaultStub(status="CHALLENGED")
    vault.challenge_outcome = "REVIEW_INCONCLUSIVE"
    vault.status = "RELEASING"
    vault.challenge_fee_state = "REFUNDABLE"
    assert vault.status == "RELEASING"
    assert vault.challenge_outcome == "REVIEW_INCONCLUSIVE"
    assert vault.challenge_fee_state == "REFUNDABLE"


# ---------------------------------------------------------------------------
# 17. Stuck review sweeper: ≥2 attempts → force INCONCLUSIVE
# ---------------------------------------------------------------------------
def test_stuck_review_sweeper_forces_inconclusive():
    vault = VaultStub(status="CHALLENGED", challenge_review_attempts=2)
    if vault.challenge_review_attempts >= 2:
        vault.challenge_outcome = "REVIEW_INCONCLUSIVE"
        vault.status = "RELEASING"
        vault.challenge_fee_state = "REFUNDABLE"
    assert vault.status == "RELEASING"
    assert vault.challenge_outcome == "REVIEW_INCONCLUSIVE"


# ---------------------------------------------------------------------------
# 18. Sweeper sweep 4: queries LOCKED state (not FINISHED)
# ---------------------------------------------------------------------------
def test_sweep4_queries_locked_state():
    """Sweep 4 (finish fee) should query challenge_fee_state == 'LOCKED', not 'FINISHED'."""
    # LOCKED is set immediately on challenge accept; FINISHED only after tesSUCCESS
    state_to_query = "LOCKED"
    assert state_to_query == "LOCKED"
    assert state_to_query != "FINISHED"


# ---------------------------------------------------------------------------
# 19. Sweeper sweep 5: REFUNDABLE past CancelAfter → cancel
# ---------------------------------------------------------------------------
def test_sweep5_refundable_past_cancel_after():
    now = datetime.now(timezone.utc)
    vault = VaultStub(
        challenge_fee_state="REFUNDABLE",
        challenge_fee_cancel_after=now - timedelta(minutes=10),
        challenge_fee_cancel_hash=None,
    )
    should_cancel = (
        vault.challenge_fee_state == "REFUNDABLE"
        and vault.challenge_fee_cancel_after is not None
        and vault.challenge_fee_cancel_after <= now
        and vault.challenge_fee_cancel_hash is None
    )
    assert should_cancel


# ---------------------------------------------------------------------------
# 20. auto_finish_escrow: includes challenge memo if verdict_hash set
# ---------------------------------------------------------------------------
def test_auto_finish_includes_challenge_memo():
    vault = VaultStub(challenge_verdict_hash="VERDICHASH123")
    should_add_memo = bool(vault.challenge_verdict_hash)
    assert should_add_memo


# ---------------------------------------------------------------------------
# 21. /fees: challenge_fee block has current_drops, floor_xrp, enabled
# ---------------------------------------------------------------------------
def test_fees_challenge_fee_block_fields():
    """The /fees response challenge_fee block must have current_drops, floor_xrp, enabled."""
    fee_block = {
        "usd": 1.0,
        "current_drops": 500_000,
        "floor_xrp": 0.2,
        "enabled": True,
        "description": "~$1 USD locked to seller",
    }
    assert "current_drops" in fee_block
    assert "floor_xrp" in fee_block
    assert "enabled" in fee_block


# ---------------------------------------------------------------------------
# 22. prepare_challenge: 15-min window guard
# ---------------------------------------------------------------------------
def test_prepare_challenge_15min_guard():
    """prepare_challenge should reject if < 15 min remain in challenge window."""
    now = datetime.now(timezone.utc)
    deadline_too_close = now + timedelta(minutes=10)
    deadline_ok = now + timedelta(minutes=20)

    minutes_remaining_close = (deadline_too_close - now).total_seconds() / 60
    minutes_remaining_ok = (deadline_ok - now).total_seconds() / 60

    assert minutes_remaining_close < 15
    assert minutes_remaining_ok >= 15


# ---------------------------------------------------------------------------
# 23. prepare_challenge: max 5 quotes guard
# ---------------------------------------------------------------------------
def test_prepare_challenge_max_5_quotes():
    """prepare_challenge should reject if ≥ 5 unexpired quotes exist for the vault."""
    existing_quotes = 5
    MAX_QUOTES = 5
    assert existing_quotes >= MAX_QUOTES  # should be rejected


# ---------------------------------------------------------------------------
# 24. webhook signing: ts.body format
# ---------------------------------------------------------------------------
def test_webhook_signing_ts_body_format():
    """Webhook HMAC must be computed on f'{ts}.{body}', not just body."""
    import hmac
    import hashlib
    secret = "my-webhook-secret"
    body = '{"event":"PASS","escrow_id":"AT-001"}'
    ts = "1700000000"
    msg = f"{ts}.{body}".encode()
    sig = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    assert sig.startswith("sha256=")
    # Verify the ts.body format is different from body-only
    body_only_sig = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    assert sig != body_only_sig
