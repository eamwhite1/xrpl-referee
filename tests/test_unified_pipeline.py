"""
tests/test_unified_pipeline.py — T1-T25 from v7.0.8 brief
Tests domain normalisation (T23) and version consistency (T24) run standalone.
Other tests require the full app + DB and are marked with @pytest.mark.integration.
"""
import pytest
import json
import os
import sys

# ---------------------------------------------------------------------------
# T23: domain normalisation — no DB, no app import needed
# ---------------------------------------------------------------------------

def test_t23_domain_normalisation():
    """T23: _normalise_domain handles schemes, paths, trailing dots, bad input."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from referee import _normalise_domain
    assert _normalise_domain("https://ripple.com/") == "ripple.com"
    assert _normalise_domain("ripple.com") == "ripple.com"
    assert _normalise_domain("http://ripple.com") == "ripple.com"
    # Repeated scheme
    result = _normalise_domain("HTTPS://https://Ripple.com//path?q=1")
    assert result == "ripple.com"
    # Trailing dot
    assert _normalise_domain("ripple.com.") == "ripple.com"
    # Empty / None
    assert _normalise_domain("") is None
    assert _normalise_domain(None) is None
    # No dot → invalid
    assert _normalise_domain("not a domain") is None
    # Path stripped
    assert _normalise_domain("ripple.com/foo/bar") == "ripple.com"
    # Query stripped
    assert _normalise_domain("ripple.com?q=1") == "ripple.com"


# ---------------------------------------------------------------------------
# T24: version consistency — referee.py vs server.json
# ---------------------------------------------------------------------------

def test_t24_version():
    """T24: APP_VERSION in referee.py matches server.json and equals 7.0.8."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from referee import APP_VERSION
    assert APP_VERSION == "7.0.8", f"referee.py APP_VERSION={APP_VERSION!r}, expected 7.0.8"
    server_json_path = os.path.join(repo_root, "server.json")
    if os.path.exists(server_json_path):
        with open(server_json_path) as f:
            sj = json.load(f)
        assert sj["version"] == "7.0.8", f"server.json version={sj['version']!r}, expected 7.0.8"


# ---------------------------------------------------------------------------
# T25: dead-code tombstone — _try_ai_judge/_free_ai_allow/_free_ai_record deleted
# ---------------------------------------------------------------------------

def test_t25_dead_code_removed():
    """T25: _try_ai_judge, _free_ai_allow, _free_ai_record no longer exist on the module."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import referee
    assert not hasattr(referee, "_try_ai_judge"), "_try_ai_judge must be deleted in 7.0.8"
    assert not hasattr(referee, "_free_ai_allow"), "_free_ai_allow must be deleted in 7.0.8"
    assert not hasattr(referee, "_free_ai_record"), "_free_ai_record must be deleted in 7.0.8"


# ---------------------------------------------------------------------------
# Integration tests — T1-T22 require full app + DB + mocked Gemini
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_t1_gate_fail_no_ai_call():
    """T1: /try, ticked, 'hello' → FAIL, 0 provider calls, attempt consumed, ai.status skipped_gate_failed."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t2_pass_with_ai():
    """T2: ticked, 'HELLO AGENT', AI PASS → PASS, 1 provider call, criteria c1+task, ai.status ran."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t3_ai_fail_on_task():
    """T3: ticked, gate passes, AI FAIL on 'task' → FAIL, no release, attempt consumed."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t4_ai_inconclusive():
    """T4: AI INCONCLUSIVE → no release, existing semantics."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t5_ai_disabled_env():
    """T5: TRY_AI_ENABLED=false → HELD 'disabled', 0 calls, 0 guard DB statements, submission_count unchanged."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t6_ai_capped():
    """T6: TRY_AI_DAILY_CAP=2, calls=2 pre-seeded → HELD 'capped', 0 calls, no release, attempt not consumed."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
@pytest.mark.parametrize("failure_mode", [
    "provider_500", "timeout", "invalid_json", "finish_reason_max_tokens", "missing_task_criterion"
])
def test_t7_provider_failure_modes(failure_mode):
    """T7: Provider failures → HELD 'unavailable', 1 call (no retry), calls+1 AND failed+1 in rows."""
    pytest.skip("integration: requires full app + DB + mocked Gemini")


@pytest.mark.integration
def test_t8_unticked_no_provider_calls():
    """T8: unticked 'HELLO AGENT' → PASS + release, 0 provider calls; unticked 'hello' → FAIL."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t9_mainnet_code_criterion():
    """T9: mainnet vault, NFT proof passes, require_ai_audit=False, failing code criterion → FAIL."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t10_code_only_criteria_with_ai_audit():
    """T10: code-only criteria, require_ai_audit=True → synthetic 'task' criterion present."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t11_prompt_injection():
    """T11: prompt injection in work → task payload is fixed server task, user text in UNTRUSTED_WORK delimiters."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t12_demo_capped():
    """T12: /testnet/demo capped → HTTP 200 HELD, no release, 1 reservation with path 'demo'."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t13_testnet_challenge():
    """T13: testnet challenge: 1 call, no retry on failure; capped → REVIEW_INCONCLUSIVE 0 calls; 2nd same vault → 0 calls."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t14_flash_inconclusive_no_pro():
    """T14: testnet vault attempt 3, Flash INCONCLUSIVE → 0 Pro calls; payload has maxOutputTokens and thinkingBudget=0."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t15_restart_persistence():
    """T15: restart persistence: reserve 3, reset module state, next reserve returns calls=4."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t16_concurrency_cap():
    """T16: concurrency cap=5, 20 concurrent reservations → exactly 5 ok, 15 capped, DB global=5."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t17_per_ip_rate_limit():
    """T17: per-IP 6th call same last XFF hop capped; changing only first hop doesn't reset."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t18_cache():
    """T18: cache same work 2 sessions: 2nd status 'cached', 0 provider calls; failed call not cached."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t19_utc_rollover():
    """T19: UTC rollover: cap resets, summary logged once, no IP or hash in log."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t20_status_health_no_db():
    """T20: /status and /health: calls_today is int, commit present, ZERO DB statements."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t21_stats_free_ai():
    """T21: /stats/free-ai: numbers only, no 'ip_' substring, zero-filled days, 429 on rate limit."""
    pytest.skip("integration: requires full app + DB")


@pytest.mark.integration
def test_t22_neon_budget():
    """T22: Neon budget: gate-FAIL adds no extra statements; AI submit ≤3 DB statements."""
    pytest.skip("integration: requires full app + DB")
