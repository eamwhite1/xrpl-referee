"""Tests for real testnet sandbox (B1-B9)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
import re


def test_testnet_unavailable_without_seed():
    """_xrpl_url_for_network('testnet') raises 503 if XRPL_TESTNET_SEED unset."""
    import referee as ref
    from fastapi import HTTPException
    original = ref.XRPL_TESTNET_SEED
    try:
        ref.XRPL_TESTNET_SEED = None
        with pytest.raises(HTTPException) as exc:
            ref._xrpl_url_for_network("testnet")
        assert exc.value.status_code == 503
        assert exc.value.detail["error"] == "testnet_unavailable"
    finally:
        ref.XRPL_TESTNET_SEED = original


def test_xrpl_url_for_mainnet():
    """_xrpl_url_for_network('mainnet') returns XRPL_URL."""
    import referee as ref
    assert ref._xrpl_url_for_network("mainnet") == ref.XRPL_URL


def test_xrpl_url_for_testnet_with_seed():
    """_xrpl_url_for_network('testnet') returns XRPL_TESTNET_URL when seed is set."""
    import referee as ref
    original = ref.XRPL_TESTNET_SEED
    try:
        ref.XRPL_TESTNET_SEED = "sEdTFakeTestSeed123"
        result = ref._xrpl_url_for_network("testnet")
        assert result == ref.XRPL_TESTNET_URL
    finally:
        ref.XRPL_TESTNET_SEED = original


def test_testnet_vault_id_prefix():
    """Testnet vault IDs start with TN-."""
    assert re.match(r"TN-[A-Z0-9]{4}-[A-Z0-9]{4}", "TN-ABCD-1234")
    assert not re.match(r"TN-[A-Z0-9]{4}-[A-Z0-9]{4}", "AT-ABCD-1234")
    assert not re.match(r"TN-[A-Z0-9]{4}-[A-Z0-9]{4}", "TN-ABC-1234")


def test_explorer_url_mainnet():
    """_explorer_url returns livenet URL for mainnet."""
    import referee as ref
    url = ref._explorer_url("ABCDEF1234", "mainnet")
    assert "xrpl.org" in url
    assert "testnet" not in url
    assert "ABCDEF1234" in url


def test_explorer_url_testnet():
    """_explorer_url returns testnet URL for testnet."""
    import referee as ref
    url = ref._explorer_url("ABCDEF1234", "testnet")
    assert "testnet.xrpl.org" in url
    assert "ABCDEF1234" in url


def test_network_column_on_escrow_vault():
    """EscrowVault model has network column with default 'mainnet'."""
    import referee as ref
    col = ref.EscrowVault.__table__.c.get("network")
    assert col is not None, "network column not found on EscrowVault"


def test_testnet_audit_counter_exists():
    """_testnet_audit_counter module-level dict exists."""
    import referee as ref
    assert hasattr(ref, "_testnet_audit_counter")
    assert isinstance(ref._testnet_audit_counter, dict)


def test_domain_toml_cache_exists():
    """_domain_toml_cache module-level dict exists."""
    import referee as ref
    assert hasattr(ref, "_domain_toml_cache")
    assert isinstance(ref._domain_toml_cache, dict)
