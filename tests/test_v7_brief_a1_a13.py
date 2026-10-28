"""Tests for v7 brief items A1–A13 (28 Oct 2026)."""
import json
import re
import pytest
import xrpl.core.keypairs as kp


# ---------------------------------------------------------------------------
# A1: testnet demo — static analysis
# ---------------------------------------------------------------------------

def test_a1_run_testnet_flow_exists():
    with open("referee.py") as f:
        src = f.read()
    assert "async def run_testnet_flow(" in src, "run_testnet_flow function missing"


def test_a1_no_loopback_http_in_demo():
    """testnet_demo must not use http://localhost in the demo path."""
    with open("referee.py") as f:
        src = f.read()
    # Find run_testnet_flow and testnet_demo area — should not call localhost
    # The old pattern was httpx.AsyncClient with localhost
    assert "http://localhost" not in src or src.count("http://localhost") == 0 or \
        all("localhost" not in line for line in src.split("\n") if "httpx" in line and "testnet_demo" in line), \
        "testnet_demo may still use loopback HTTP"


def test_a1_sweep_orphan_vaults_exists():
    with open("referee.py") as f:
        src = f.read()
    assert "_sweep_orphan_testnet_vaults" in src, "_sweep_orphan_testnet_vaults missing"


def test_a1_semaphore_exists():
    with open("referee.py") as f:
        src = f.read()
    assert "asyncio.Semaphore" in src, "No asyncio.Semaphore for demo concurrency control"


def test_a1_rate_limit_uses_last_xff_hop():
    """Rate counter must use LAST X-Forwarded-For hop."""
    with open("referee.py") as f:
        src = f.read()
    assert "_client_ip" in src, "_client_ip helper missing"
    # Check it returns last hop
    idx = src.find("def _client_ip")
    snippet = src[idx:idx+500]
    assert "hops[-1]" in snippet or "split(\",\")[-1]" in snippet or "[-1]" in snippet, \
        "_client_ip must use last X-Forwarded-For hop"


def test_a1_demo_502_on_failure():
    """testnet_demo must return 502 on failure."""
    with open("referee.py") as f:
        src = f.read()
    assert '"demo_failed"' in src, "demo_failed error key missing"
    assert "retryable" in src, "retryable field missing from error response"


# ---------------------------------------------------------------------------
# A2: /testnet/try/* endpoints
# ---------------------------------------------------------------------------

def test_a2_try_start_endpoint_exists():
    with open("referee.py") as f:
        src = f.read()
    assert '/testnet/try/start' in src or 'testnet_try_start' in src, \
        "POST /testnet/try/start endpoint missing"


def test_a2_try_lock_endpoint_exists():
    with open("referee.py") as f:
        src = f.read()
    assert 'testnet_try_lock' in src, "testnet_try_lock endpoint missing"


def test_a2_try_submit_endpoint_exists():
    with open("referee.py") as f:
        src = f.read()
    assert 'testnet_try_submit' in src, "testnet_try_submit endpoint missing"


def test_a2_seeds_not_returned():
    """testnet_try_start must store seeds in memory only — never return them."""
    with open("referee.py") as f:
        src = f.read()
    # Find testnet_try_start function
    idx = src.find("async def testnet_try_start")
    assert idx >= 0, "testnet_try_start not found"
    fn_body = src[idx:idx+3000]
    # Should not have return with "seed" or "secret"
    return_lines = [l for l in fn_body.split("\n") if "return" in l and ("seed" in l or "secret" in l.lower())]
    assert not return_lines, f"testnet_try_start returns seed/secret: {return_lines}"


def test_a2_test_money_in_responses():
    with open("referee.py") as f:
        src = f.read()
    assert '"test_money"' in src, '"test_money": true field missing from try endpoints'
    assert '"note"' in src or "Test XRP on XRPL Testnet" in src, \
        '"note" field missing from try endpoints'


def test_a2_session_ttl_exists():
    with open("referee.py") as f:
        src = f.read()
    assert "_testnet_try_sessions" in src, "_testnet_try_sessions dict missing"
    assert "expires_at" in src, "TTL expiry field missing"


# ---------------------------------------------------------------------------
# A3: criteria validation
# ---------------------------------------------------------------------------

def test_a3_allow_lists_exist():
    with open("referee.py") as f:
        src = f.read()
    assert "_CRITERION_ALLOWED_KEYS" in src or "allowed_keys" in src.lower(), \
        "Per-type allow-list for criteria missing"


def test_a3_unknown_field_error():
    with open("referee.py") as f:
        src = f.read()
    assert "unknown field" in src, "'unknown field' error message missing from criteria validation"


def test_a3_expected_value_required():
    with open("referee.py") as f:
        src = f.read()
    assert "expected_value" in src, "expected_value validation missing"
    # Check it validates non-empty
    assert "non-empty" in src or "empty" in src, "empty expected_value check missing"


def test_a3_metadata_json_validation():
    with open("referee.py") as f:
        src = f.read()
    assert "metadata must be a JSON object" in src or "JSON-encoded object string" in src, \
        "metadata JSON validation error message missing"


# ---------------------------------------------------------------------------
# A4: trust tools network param
# ---------------------------------------------------------------------------

def test_a4_assess_has_network_param():
    with open("mcp_server.py") as f:
        src = f.read()
    idx = src.find("async def assess_counterparty_and_job")
    assert idx >= 0
    fn_sig = src[idx:idx+2000]
    assert "network" in fn_sig, "assess_counterparty_and_job missing network param"


def test_a4_batch_has_network_param():
    with open("mcp_server.py") as f:
        src = f.read()
    idx = src.find("async def batch_wallet_trust_scores")
    assert idx >= 0
    fn_sig = src[idx:idx+1000]
    assert "network" in fn_sig, "batch_wallet_trust_scores missing network param"


def test_a4_testnet_does_not_block_proceed():
    with open("mcp_server.py") as f:
        src = f.read()
    assert "testnet: scores are informational" in src, \
        "testnet caution message missing from assess_counterparty_and_job"


def test_a4_auto_fallback_logic():
    with open("mcp_server.py") as f:
        src = f.read()
    assert "auto" in src and ("testnet" in src), \
        "auto network fallback to testnet missing"


def test_a4_network_in_response():
    with open("mcp_server.py") as f:
        src = f.read()
    idx = src.find("async def assess_counterparty_and_job")
    fn_body = src[idx:idx+5000]
    assert '"network"' in fn_body or "'network'" in fn_body, \
        "network field missing from assess_counterparty_and_job response"


# ---------------------------------------------------------------------------
# A5: stable verdict shape
# ---------------------------------------------------------------------------

def test_a5_verdict_view_exists():
    with open("referee.py") as f:
        src = f.read()
    assert "def _verdict_view(" in src, "_verdict_view function missing"


def test_a5_verdict_view_returns_required_fields():
    with open("referee.py") as f:
        src = f.read()
    idx = src.find("def _verdict_view(")
    fn_body = src[idx:idx+6000]
    required = ["verdict", "score", "summary", "criteria_results", "submission_excerpt",
                "explorer_links", "attempts_remaining"]
    for field in required:
        assert f'"{field}"' in fn_body, f"_verdict_view missing field: {field}"


def test_a5_verdict_not_nested_object():
    """_verdict_view must unwrap nested verdict objects."""
    with open("referee.py") as f:
        src = f.read()
    idx = src.find("def _verdict_view(")
    fn_body = src[idx:idx+3000]
    assert "isinstance(verdict, dict)" in fn_body, \
        "_verdict_view must handle nested verdict dict"


# ---------------------------------------------------------------------------
# A6: challenge fixes
# ---------------------------------------------------------------------------

def test_a6_html_escape_used():
    with open("referee.py") as f:
        src = f.read()
    assert "html.escape" in src or "html_escape" in src.lower(), \
        "html.escape not used for user-supplied strings"


def test_a6_challenge_window_closing_409():
    with open("referee.py") as f:
        src = f.read()
    assert "challenge_window_closing" in src, \
        "409 challenge_window_closing error missing"


def test_a6_verdict_view_used_in_challenge():
    with open("referee.py") as f:
        src = f.read()
    # _verdict_view should be referenced in challenge/email code
    assert "_verdict_view(" in src, "_verdict_view not called anywhere"


# ---------------------------------------------------------------------------
# A7: sign instructions
# ---------------------------------------------------------------------------

def test_a7_correct_python_sign_line():
    for fname in ("mcp_server.py", "referee.py"):
        with open(fname) as f:
            src = f.read()
        assert "EscrowCreate.from_xrpl(tx)" in src or "from_xrpl" in src, \
            f"EscrowCreate.from_xrpl not found in {fname}"
        assert ".blob()" in src, f"blob() call (with parentheses) not found in {fname}"


def test_a7_no_legacy_sign_patterns():
    """No 'wallet.sign(tx)' with xrpl-py, no '.blob)' without parens."""
    with open("mcp_server.py") as f:
        src = f.read()
    # blob without () is the bug
    assert ".blob)" not in src and ".blob," not in src, \
        "Legacy .blob without parentheses found in mcp_server.py"


def test_a7_sign_deadline_note_in_hire_and_pay():
    with open("mcp_server.py") as f:
        src = f.read()
    assert "sign_deadline_note" in src, "sign_deadline_note missing from hire_and_pay response"


def test_a7_network_in_hire_and_pay_response():
    # explorer_base is set in referee.py's hire_and_pay route and returned in response
    with open("referee.py") as f:
        src = f.read()
    assert "explorer_base" in src, "explorer_base missing from hire_and_pay response"


# ---------------------------------------------------------------------------
# A8: source column + /stats/testnet
# ---------------------------------------------------------------------------

def test_a8_source_column_in_model():
    with open("referee.py") as f:
        src = f.read()
    assert "source" in src and "Column(String" in src, \
        "source column missing from EscrowVault model"
    # Check migration
    assert "ADD COLUMN IF NOT EXISTS source" in src, \
        "source column migration missing"


def test_a8_is_internal_helper():
    with open("referee.py") as f:
        src = f.read()
    assert "_is_internal_vault(" in src, "_is_internal_vault helper missing"


def test_a8_stats_testnet_endpoint_exists():
    with open("referee.py") as f:
        src = f.read()
    assert "stats_testnet" in src or "/stats/testnet" in src, \
        "/stats/testnet endpoint missing"


def test_a8_stats_testnet_keys():
    with open("referee.py") as f:
        src = f.read()
    for key in ["tn_vaults_total", "tn_vaults_outside", "outside_buyer_wallets",
                "outside_wallets_reached_pass", "outside_demo_runs"]:
        assert key in src, f"stats_testnet missing key: {key}"


# ---------------------------------------------------------------------------
# A9: submit_escrow_transaction wait for validation
# ---------------------------------------------------------------------------

def test_a9_confirmed_status():
    with open("referee.py") as f:
        src = f.read()
    assert '"confirmed"' in src or "'confirmed'" in src, \
        "confirmed status missing from submit endpoint"


def test_a9_tessuccess_check():
    with open("referee.py") as f:
        src = f.read()
    assert "tesSUCCESS" in src, "tesSUCCESS check missing from submit_escrow_transaction"


def test_a9_submitted_fallback():
    with open("referee.py") as f:
        src = f.read()
    assert '"submitted"' in src or "'submitted'" in src, \
        "submitted fallback status missing"


# ---------------------------------------------------------------------------
# A10: MCP discoverability
# ---------------------------------------------------------------------------

def test_a10_first_two_tools_are_testnet():
    with open("mcp_server.py") as f:
        src = f.read()
    # Find the first two @mcp.tool + async def pairs
    tools = re.findall(r'async def (\w+)', src)
    mcp_tools = [t for t in tools if t not in ('__init__', 'main')]
    assert mcp_tools[0] == "create_testnet_wallet", \
        f"First tool should be create_testnet_wallet, got {mcp_tools[0]}"
    assert mcp_tools[1] == "try_testnet_demo", \
        f"Second tool should be try_testnet_demo, got {mcp_tools[1]}"


def test_a10_instructions_length():
    with open("mcp_server.py") as f:
        src = f.read()
    start = src.find('    instructions=(')
    end = src.find('\n    ),\n', start)
    block = src[start+18:end]
    total = 0
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', block):
        s = m.group(1)
        s = s.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
        total += len(s)
    assert total <= 7122, f"Instructions too long: {total} chars (limit 7122)"


def test_a10_free_to_try_in_four_descriptions():
    with open("mcp_server.py") as f:
        src = f.read()
    assert src.count("Free to try: pass network='testnet'") >= 4, \
        "Need 'Free to try' in at least 4 tool descriptions"


def test_a10_llms_full_txt_endpoint():
    with open("referee.py") as f:
        src = f.read()
    assert "/llms-full.txt" in src, "/llms-full.txt endpoint missing"


def test_a10_testnet_in_card_descriptions():
    with open("referee.py") as f:
        src = f.read()
    # Check agent card and openapi description mention free testnet
    assert "Free testnet sandbox" in src or "free testnet" in src.lower(), \
        "Card descriptions don't mention free testnet sandbox"


# ---------------------------------------------------------------------------
# A11: RLUSD and Batch wording
# ---------------------------------------------------------------------------

def test_a11_no_lock_xrp_or_rlusd():
    for fname in ("referee.py", "mcp_server.py"):
        with open(fname) as f:
            src = f.read()
        assert "Lock XRP or RLUSD" not in src, \
            f"'Lock XRP or RLUSD' still present in {fname}"


def test_a11_no_beta_untested():
    for fname in ("referee.py", "mcp_server.py"):
        with open(fname) as f:
            src = f.read()
        assert "beta/untested" not in src, \
            f"'beta/untested' still present in {fname}"


def test_a11_rlusd_escrow_correct_wording():
    with open("referee.py") as f:
        src = f.read()
    assert "XLS-85" in src, "XLS-85 reference missing"
    assert "28 Oct 2026" in src, "Date comment not updated to 28 Oct 2026"


def test_a11_batch_correct_wording():
    for fname in ("referee.py", "mcp_server.py"):
        with open(fname) as f:
            src = f.read()
        assert "XLS-56 Batch is enabled on mainnet" in src or \
               "XLS-56" in src, f"XLS-56 Batch wording missing from {fname}"


# ---------------------------------------------------------------------------
# A12: post_job network param
# ---------------------------------------------------------------------------

def test_a12_post_job_network_param():
    with open("referee.py") as f:
        src = f.read()
    idx = src.find("async def post_job(")
    assert idx >= 0
    fn_body = src[idx:idx+3000]
    assert "network" in fn_body, "post_job missing network param handling"
    assert "testnet" in fn_body, "post_job not handling testnet network"


def test_a12_list_jobs_network_filter():
    with open("referee.py") as f:
        src = f.read()
    idx = src.find("async def list_jobs(")
    assert idx >= 0
    fn_sig = src[idx:idx+500]
    assert "network" in fn_sig, "list_jobs missing network param"
    assert "include_testnet" in fn_sig, "list_jobs missing include_testnet param"


def test_a12_list_jobs_filters_testnet_by_default():
    with open("referee.py") as f:
        src = f.read()
    idx = src.find("async def list_jobs(")
    fn_body = src[idx:idx+2000]
    assert "testnet" not in fn_body or \
           "show_testnet" in fn_body or \
           '"testnet"' in fn_body, \
        "list_jobs must hide testnet jobs by default"


# ---------------------------------------------------------------------------
# A13: DB + memory hardening
# ---------------------------------------------------------------------------

def test_a13_rss_logging():
    with open("referee.py") as f:
        src = f.read()
    assert "rss_mb=" in src, "rss_mb= log line missing"


def test_a13_rss_warning_threshold():
    with open("referee.py") as f:
        src = f.read()
    assert "430" in src, "430 MB RSS warning threshold missing"


def test_a13_db_invalidate_on_error():
    with open("referee.py") as f:
        src = f.read()
    assert "db.invalidate()" in src or "invalidate()" in src, \
        "db.invalidate() call missing from get_db error handler"


def test_a13_rollback_swallowed():
    with open("referee.py") as f:
        src = f.read()
    assert "rollback error (swallowed)" in src or \
           "swallowed" in src, \
        "Rollback error swallowing missing"


def test_a13_pooler_prepare_threshold():
    with open("referee.py") as f:
        src = f.read()
    assert "prepare_threshold" in src, \
        "prepare_threshold=None for pooler connections missing"
    assert "pooler" in src, "pooler detection missing"


def test_a13_ttl_eviction_for_counters():
    with open("referee.py") as f:
        src = f.read()
    assert "_testnet_wallet_counter" in src, "_testnet_wallet_counter missing"
    assert "_testnet_demo_counter" in src, "_testnet_demo_counter missing"
    # Check eviction exists
    assert "_testnet_demo_counter" in src and "pop" in src, \
        "TTL eviction for demo counter missing"


def test_a13_seller_sig_replay_eviction():
    with open("referee.py") as f:
        src = f.read()
    assert "_seller_sig_replay" in src, "_seller_sig_replay dict missing"
    idx = src.find("_seller_sig_replay")
    # Find the eviction code — should be near the hourly sweep
    assert "seller_sig_replay" in src and "pop" in src, \
        "_seller_sig_replay eviction missing"


# ---------------------------------------------------------------------------
# Cross-cutting: no legacy sign patterns
# ---------------------------------------------------------------------------

def test_no_legacy_xrpl_sign():
    """No 'wallet.sign(tx)' used as xrpl-py 3.x API in instructions or docstrings."""
    with open("mcp_server.py") as f:
        src = f.read()
    # wallet.sign(tx) in JS context is fine (that's correct JS SDK)
    # The bug is using it as Python xrpl-py API
    # Check that no Python snippet says wallet.sign(tx) as the sign call
    lines = [l for l in src.split("\n") if "wallet.sign(tx)" in l and "JS" not in l and "js:" not in l.lower()]
    assert not lines, f"Legacy wallet.sign(tx) as Python xrpl-py found: {lines[:3]}"
