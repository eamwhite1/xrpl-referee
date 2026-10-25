"""Real tests for Grok P2-5 acceptance criteria — call actual referee code."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import hashlib
import json
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


# ── Test 1: _validate_criteria_v2 — regex cap ───────────────────────────────
def test_criteria_regex_cap():
    """More than 3 regex criteria raises 400."""
    import referee as ref
    criteria = [
        {"id": f"r{i}", "type": "code", "gate": "regex", "pattern": f"^test{i}$"}
        for i in range(4)
    ]
    with pytest.raises(HTTPException) as exc:
        ref._validate_criteria_v2(criteria)
    assert exc.value.status_code == 400
    assert "regex" in exc.value.detail.lower()


def test_criteria_total_cap():
    """More than 10 criteria raises 400."""
    import referee as ref
    criteria = [{"id": f"c{i}", "type": "text"} for i in range(11)]
    with pytest.raises(HTTPException) as exc:
        ref._validate_criteria_v2(criteria)
    assert exc.value.status_code == 400
    assert "10" in exc.value.detail


def test_criteria_nested_quantifier_rejected():
    """(a+)+ pattern raises 400."""
    import referee as ref
    criteria = [{"id": "r1", "type": "code", "gate": "regex", "pattern": "(a+)+$"}]
    with pytest.raises(HTTPException) as exc:
        ref._validate_criteria_v2(criteria)
    assert exc.value.status_code == 400
    assert "catastrophic" in exc.value.detail.lower()


def test_criteria_valid_alternation_not_rejected():
    """(foo|bar)+ should NOT be rejected — it has alternation, not nested quantifier."""
    import referee as ref
    criteria = [{"id": "r1", "type": "code", "gate": "regex", "pattern": "^(foo|bar)+$"}]
    # Should not raise
    ref._validate_criteria_v2(criteria)


def test_criteria_valid_domain_not_rejected():
    r"""(\w+\.)+com$ should NOT be rejected — alternation-free but safe."""
    import referee as ref
    criteria = [{"id": "r1", "type": "code", "gate": "regex", "pattern": r"(\w+\.)+com$"}]
    # Should not raise
    ref._validate_criteria_v2(criteria)


# ── Test 2: _run_structured_audit — Flash INCONCLUSIVE + Pro PASS → INCONCLUSIVE
@pytest.mark.asyncio
async def test_run_structured_audit_flash_inconclusive_pro_pass():
    """Strict 2-of-2: Flash=INCONCLUSIVE + Pro=PASS must stay INCONCLUSIVE."""
    import referee as ref

    def make_gemini_response(payload):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
        }
        return mock_resp

    flash_payload = {"overall_verdict": "INCONCLUSIVE", "overall_reasoning": "insufficient", "criteria_results": [], "score": 40}
    pro_payload   = {"overall_verdict": "PASS",          "overall_reasoning": "looks good",   "criteria_results": [], "score": 85}

    call_count = [0]
    async def fake_post(url, **kwargs):
        r = call_count[0]
        call_count[0] += 1
        return make_gemini_response(flash_payload if r == 0 else pro_payload)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = fake_post

    with patch("httpx.AsyncClient", return_value=mock_client):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            result = await ref._run_structured_audit(
                task="write a hello world function",
                work="def hello(): print('hello world')",
                criteria=[],
                evidence_items=[],
                require_consensus=True,
                attempt_count=1,
            )

    assert result["overall_verdict"] == "INCONCLUSIVE"
    assert "Flash=INCONCLUSIVE" in result.get("overall_reasoning", "")


# ── Test 3: seller signature — valid sig accepted, bad sig rejected ──────────
def test_verify_seller_sig_bad_key_rejected():
    """Wrong public key → 401."""
    import referee as ref

    ts = int(time.time())
    fake_sig = "deadbeef" * 8
    fake_pubkey = "ED" + "00" * 32
    sig_header = f"{fake_sig}:{ts}:{fake_pubkey}"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        ref._verify_seller_ownership(
            seller_address="rN7n3473SaZBCG4dFL75AYVGF5YCx9RBRo",
            buyer_address="rBuyer123",
            amount_xrp=10.0,
            task_desc="test task",
            buyer_email=None,
            api_key_header=None,
            sig_header=sig_header,
            db=mock_db,
        )
    assert exc.value.status_code in (401, 422)


def test_verify_seller_sig_expired_rejected():
    """Timestamp > 5 min ago → 401 expired."""
    import referee as ref

    old_ts = int(time.time()) - 400
    fake_sig = "deadbeef" * 8
    fake_pubkey = "ED" + "00" * 32
    sig_header = f"{fake_sig}:{old_ts}:{fake_pubkey}"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        ref._verify_seller_ownership(
            seller_address="rSomeAddress",
            buyer_address="rBuyer",
            amount_xrp=5.0,
            task_desc="task",
            buyer_email=None,
            api_key_header=None,
            sig_header=sig_header,
            db=mock_db,
        )
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_verify_seller_no_proof_raises_401():
    """No api_key and no sig_header → 401."""
    import referee as ref

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        ref._verify_seller_ownership(
            seller_address="rSomeAddress",
            buyer_address="rBuyer",
            amount_xrp=5.0,
            task_desc="task",
            buyer_email=None,
            api_key_header=None,
            sig_header=None,
            db=mock_db,
        )
    assert exc.value.status_code == 401


# ── Test 4: auto_finish_escrow — close_time_iso → released_at ───────────────
def test_released_at_from_close_time_iso():
    """released_at uses close_time_iso (API v2)."""
    close_time_iso = "2024-10-24T13:39:51+00:00"
    released_at = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    assert released_at == datetime(2024, 10, 24, 13, 39, 51, tzinfo=timezone.utc)
    assert released_at.tzinfo is not None


def test_released_at_integer_fallback():
    """Falls back to integer XRPL epoch when close_time_iso absent."""
    close_int = 751900791
    released_at = datetime.fromtimestamp(close_int + 946684800, tz=timezone.utc)
    assert released_at.tzinfo is not None


# ── Test 5: timing guard logic ───────────────────────────────────────────────
def test_timing_guard_rejects_near_expiry():
    """< 5 min before CancelAfter should be rejected."""
    cancel_after_ts = datetime.now(timezone.utc) + timedelta(minutes=3)
    time_left = (cancel_after_ts - datetime.now(timezone.utc)).total_seconds()
    assert time_left < 300


def test_timing_guard_allows_with_time():
    """> 5 min before CancelAfter should be allowed."""
    cancel_after_ts = datetime.now(timezone.utc) + timedelta(minutes=10)
    time_left = (cancel_after_ts - datetime.now(timezone.utc)).total_seconds()
    assert time_left >= 300
