"""Tests for Grok second-audit (P0-1 through P2-5) fixes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import inspect
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# P0-1: /stats SQL uses ai_verdict, not verdict
# ---------------------------------------------------------------------------

def test_stats_sql_uses_ai_verdict():
    import referee as ref
    src = inspect.getsource(ref.get_platform_stats)
    # Must not contain "verdict IS NOT NULL" (that bare column doesn't exist)
    assert "verdict IS NOT NULL" not in src.replace("ai_verdict IS NOT NULL", ""), \
        "/stats must not query non-existent bare 'verdict' column"
    assert "ai_verdict IS NOT NULL" in src, "/stats must query ai_verdict IS NOT NULL"


def test_stats_tools_returns_generic_error():
    import referee as ref
    src = inspect.getsource(ref.stats_tools)
    assert "str(e)" not in src, "/stats/tools must not return raw exception string"


# ---------------------------------------------------------------------------
# P1-1: _referee_wallet_for_network helper exists and returns correct wallet
# ---------------------------------------------------------------------------

def test_referee_wallet_for_network_helper_exists():
    import referee as ref
    assert hasattr(ref, "_referee_wallet_for_network"), "_referee_wallet_for_network must be defined"


def test_referee_wallet_for_network_mainnet():
    import referee as ref
    if ref.referee_wallet is None:
        pytest.skip("XRPL_SEED not configured in test env")
    wallet = ref._referee_wallet_for_network("mainnet")
    assert wallet is ref.referee_wallet


def test_referee_wallet_for_network_testnet_raises_without_seed():
    import referee as ref
    original = ref.XRPL_TESTNET_SEED
    ref.XRPL_TESTNET_SEED = None
    try:
        with pytest.raises(Exception):
            ref._referee_wallet_for_network("testnet")
    finally:
        ref.XRPL_TESTNET_SEED = original


def test_referee_wallet_for_network_testnet_with_seed():
    import referee as ref
    from xrpl.core.keypairs import generate_seed
    test_seed = generate_seed()
    original = ref.XRPL_TESTNET_SEED
    ref.XRPL_TESTNET_SEED = test_seed
    try:
        wallet = ref._referee_wallet_for_network("testnet")
        assert wallet is not ref.referee_wallet
        assert wallet.address.startswith("r")
    finally:
        ref.XRPL_TESTNET_SEED = original


# ---------------------------------------------------------------------------
# P1-1: auto_finish_escrow uses network-aware wallet in source
# ---------------------------------------------------------------------------

def test_auto_finish_escrow_uses_network_wallet():
    import referee as ref
    src = inspect.getsource(ref.auto_finish_escrow)
    assert "_referee_wallet_for_network" in src, "auto_finish_escrow must use _referee_wallet_for_network"
    assert "referee_wallet.address" not in src or "_af_wallet" in src, \
        "auto_finish_escrow must not hardcode referee_wallet"


# ---------------------------------------------------------------------------
# P1-2: challenge helpers use network-aware URL
# ---------------------------------------------------------------------------

def test_finish_challenge_fee_uses_network_url():
    import referee as ref
    src = inspect.getsource(ref._finish_challenge_fee_escrow)
    assert "_xrpl_url_for_network" in src, "_finish_challenge_fee_escrow must use _xrpl_url_for_network"
    assert "AsyncJsonRpcClient(XRPL_URL)" not in src, "_finish_challenge_fee_escrow must not hardcode XRPL_URL"


def test_cancel_challenge_fee_uses_network_url():
    import referee as ref
    src = inspect.getsource(ref._cancel_challenge_fee_escrow)
    assert "_xrpl_url_for_network" in src, "_cancel_challenge_fee_escrow must use _xrpl_url_for_network"
    assert "AsyncJsonRpcClient(XRPL_URL)" not in src


def test_verify_challenge_fee_uses_network_url():
    import referee as ref
    src = inspect.getsource(ref._verify_challenge_fee_escrow)
    assert "_xrpl_url_for_network" in src, "_verify_challenge_fee_escrow must use _xrpl_url_for_network"
    assert "await client.post(XRPL_URL," not in src


# ---------------------------------------------------------------------------
# P1-3: hire_and_pay uses vault-returned escrow_id for prepare step
# ---------------------------------------------------------------------------

def _read_mcp_server_src():
    """Read mcp_server.py source directly (avoids fastmcp import in test env)."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp_server.py")
    with open(path) as f:
        return f.read()


def test_hire_and_pay_uses_vault_escrow_id():
    src = _read_mcp_server_src()
    assert "actual_escrow_id" in src, "hire_and_pay must use actual_escrow_id from vault response"
    assert 'vault_data.get("escrow_id")' in src, "hire_and_pay must read escrow_id from vault_data"


def test_hire_and_pay_escrow_id_optional():
    src = _read_mcp_server_src()
    # escrow_id parameter must have a default of ""
    assert 'escrow_id' in src
    # Check the parameter default
    import re
    match = re.search(r'escrow_id.*?\]\s*=\s*""', src, re.DOTALL)
    assert match is not None, "escrow_id must default to empty string"


# ---------------------------------------------------------------------------
# P2-1: GET /escrow/{id} includes pass_at
# ---------------------------------------------------------------------------

def test_get_escrow_info_includes_pass_at():
    import referee as ref
    src = inspect.getsource(ref.get_escrow_info)
    assert "pass_at" in src, "get_escrow_info must include pass_at in response"


# ---------------------------------------------------------------------------
# P2-2: /mcp/core-tools returns enriched metadata
# ---------------------------------------------------------------------------

def test_mcp_core_tools_returns_enriched():
    import referee as ref
    src = inspect.getsource(ref.mcp_core_profile)
    assert "full_schema_url" in src, "/mcp/core-tools must include full_schema_url"
    assert "description" in src or "enriched" in src, "/mcp/core-tools must return enriched tool entries"


# ---------------------------------------------------------------------------
# P2-4: SCORE_MAX constant defined as 133
# ---------------------------------------------------------------------------

def test_score_max_constant():
    import referee as ref
    assert hasattr(ref, "SCORE_MAX"), "SCORE_MAX constant must be defined"
    assert ref.SCORE_MAX == 133, "SCORE_MAX must equal 133"


def test_score_100_is_capped():
    import referee as ref
    src = inspect.getsource(ref.compute_xrpl_trust_score)
    assert "score_100" in src, "score_100 must be in compute_xrpl_trust_score"
    # P2-2 changed formula to round(total*100/SCORE_MAX); either formula is acceptable
    assert "SCORE_MAX" in src or "min(100" in src, "score_100 must use SCORE_MAX or min(100, total)"


# ---------------------------------------------------------------------------
# P2-5: JWT startup warning for short secrets
# ---------------------------------------------------------------------------

def test_jwt_startup_warning_in_source():
    import referee as ref
    import inspect as _i
    # Read source of module-level code — check the module source for the warning
    source_path = _i.getfile(ref)
    with open(source_path) as f:
        src = f.read()
    assert "JWT_SECRET" in src and "32" in src and "logger.warning" in src, \
        "Must have startup warning when JWT_SECRET < 32 bytes"


# ---------------------------------------------------------------------------
# Explorer URL uses correct domain per network
# ---------------------------------------------------------------------------

def test_explorer_url_mainnet():
    import referee as ref
    url = ref._explorer_url("ABCD1234", "mainnet")
    assert "livenet.xrpl.org" in url


def test_explorer_url_testnet():
    import referee as ref
    url = ref._explorer_url("ABCD1234", "testnet")
    assert "testnet.xrpl.org" in url


def test_explorer_url_none_hash():
    import referee as ref
    assert ref._explorer_url(None, "mainnet") is None
    assert ref._explorer_url("", "mainnet") is None
