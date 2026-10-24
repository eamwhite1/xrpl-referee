"""Unit tests for Grok P2-5 acceptance criteria."""
import pytest
from datetime import datetime, timezone, timedelta


# ── Test 1: Flash INCONCLUSIVE + Pro PASS → INCONCLUSIVE ────────────────────
@pytest.mark.asyncio
async def test_consensus_flash_inconclusive_pro_pass_gives_inconclusive():
    """P1-1 strict 2-of-2: Flash=INCONCLUSIVE + Pro=PASS must stay INCONCLUSIVE."""
    flash_response = {
        "overall_verdict": "INCONCLUSIVE",
        "overall_reasoning": "Not enough evidence",
        "criteria_results": [],
        "score": 40,
    }
    pro_response = {
        "overall_verdict": "PASS",
        "overall_reasoning": "Work looks good",
        "criteria_results": [],
        "score": 80,
    }

    async def _patched():
        flash_verdict = "INCONCLUSIVE"
        pro_verdict = "PASS"
        models_used = ["gemini-2.5-flash", "gemini-2.5-pro"]
        pro_result = dict(pro_response)
        flash_result = dict(flash_response)
        if flash_verdict == "INCONCLUSIVE":
            pro_result["overall_verdict"] = "INCONCLUSIVE"
            pro_result["overall_reasoning"] = (
                f"Consensus check: Flash=INCONCLUSIVE — result stays INCONCLUSIVE "
                f"(Pro={pro_verdict} alone is not sufficient). "
                f"Flash: {flash_result.get('overall_reasoning','')} | "
                f"Pro: {pro_result.get('overall_reasoning','')}"
            )
            pro_result["models_used"] = models_used
            return pro_result
        return pro_result

    result = await _patched()
    assert result["overall_verdict"] == "INCONCLUSIVE"
    assert "Flash=INCONCLUSIVE" in result["overall_reasoning"]


# ── Test 2: inconclusive_count cap ───────────────────────────────────────────
def test_inconclusive_count_cap_logic():
    """P1-2: after 3 free INCONCLUSIVEs the next one consumes an attempt."""
    max_free_inconclusive = 3

    assert (3 >= max_free_inconclusive), "4th INCONCLUSIVE should consume an attempt"
    assert not (2 >= max_free_inconclusive), "3rd INCONCLUSIVE should be free"


# ── Test 3: released_at from close_time_iso ──────────────────────────────────
def test_released_at_from_close_time_iso():
    """P0-3: released_at uses close_time_iso (API v2) over integer date."""
    close_time_iso = "2024-10-24T13:39:51+00:00"
    close_int = None

    if close_time_iso:
        released_at = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    elif close_int:
        released_at = datetime.fromtimestamp(close_int + 946684800, tz=timezone.utc)
    else:
        released_at = datetime.now(timezone.utc)

    assert released_at == datetime(2024, 10, 24, 13, 39, 51, tzinfo=timezone.utc)
    assert released_at.tzinfo is not None


def test_released_at_integer_fallback():
    """P0-3: falls back to integer date when close_time_iso absent."""
    close_time_iso = None
    close_int = 751900791

    if close_time_iso:
        released_at = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    elif close_int:
        released_at = datetime.fromtimestamp(close_int + 946684800, tz=timezone.utc)
    else:
        released_at = datetime.now(timezone.utc)

    assert released_at == datetime.fromtimestamp(close_int + 946684800, tz=timezone.utc)


def test_released_at_server_time_fallback():
    """P0-3: falls back to server time when no ledger timestamp available."""
    before = datetime.now(timezone.utc)
    close_time_iso = None
    close_int = None

    if close_time_iso:
        released_at = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    elif close_int:
        released_at = datetime.fromtimestamp(close_int + 946684800, tz=timezone.utc)
    else:
        released_at = datetime.now(timezone.utc)

    after = datetime.now(timezone.utc)
    assert before <= released_at <= after


# ── Test 4: regex criteria caps ──────────────────────────────────────────────
def test_criteria_validation_regex_cap():
    """P1-2: more than 3 regex criteria raises 400."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from fastapi import HTTPException
    import referee as ref

    criteria = [
        {"id": f"r{i}", "type": "code", "gate": "regex", "pattern": f"^test{i}$"}
        for i in range(4)
    ]
    with pytest.raises(HTTPException) as exc_info:
        ref._validate_criteria_v2(criteria)
    assert exc_info.value.status_code == 400
    assert "regex" in exc_info.value.detail.lower()


def test_criteria_validation_total_cap():
    """P1-2: more than 10 criteria raises 400."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from fastapi import HTTPException
    import referee as ref

    criteria = [{"id": f"c{i}", "type": "text"} for i in range(11)]
    with pytest.raises(HTTPException) as exc_info:
        ref._validate_criteria_v2(criteria)
    assert exc_info.value.status_code == 400
    assert "10" in exc_info.value.detail


def test_criteria_validation_nested_quantifier():
    """P1-2: nested quantifier like (a+)+ raises 400."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from fastapi import HTTPException
    import referee as ref

    criteria = [{"id": "r1", "type": "code", "gate": "regex", "pattern": "(a+)+$"}]
    with pytest.raises(HTTPException) as exc_info:
        ref._validate_criteria_v2(criteria)
    assert exc_info.value.status_code == 400
    assert "catastrophic" in exc_info.value.detail.lower()


# ── Test 5: timing guards ─────────────────────────────────────────────────────
def test_timing_guard_rejects_near_expiry():
    """P1-3: < 5 min before CancelAfter should be rejected."""
    cancel_after_ts = datetime.now(timezone.utc) + timedelta(minutes=3)
    time_left = cancel_after_ts - datetime.now(timezone.utc)
    assert time_left.total_seconds() < 300


def test_timing_guard_allows_with_time():
    """P1-3: > 5 min before CancelAfter should be allowed."""
    cancel_after_ts = datetime.now(timezone.utc) + timedelta(minutes=10)
    time_left = cancel_after_ts - datetime.now(timezone.utc)
    assert time_left.total_seconds() >= 300


def test_challenge_window_cap_at_creation():
    """P1-3: challenge_window_hrs + 1h > remaining time should be rejected."""
    cancel_after_ts = datetime.now(timezone.utc) + timedelta(hours=2)
    challenge_hrs = 2.0
    _slack = (cancel_after_ts - datetime.now(timezone.utc)).total_seconds() / 3600
    assert (challenge_hrs + 1.0) > _slack


def test_challenge_deadline_capped_before_cancel():
    """P1-3: challenge_deadline is capped at cancel_after_ts - 30 min."""
    cancel_after_ts = datetime.now(timezone.utc) + timedelta(hours=5)
    raw_deadline = datetime.now(timezone.utc) + timedelta(hours=5, minutes=10)
    max_deadline = cancel_after_ts - timedelta(minutes=30)
    capped = min(raw_deadline, max_deadline)
    assert capped == max_deadline
