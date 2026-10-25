"""Tests confirming AnChain BEI has been removed; sanctions = OFAC SDN XML only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import inspect
import referee as ref


def test_no_anchain_in_is_wallet_sanctioned():
    src = inspect.getsource(ref.is_wallet_sanctioned)
    assert "anchain" not in src.lower(), "is_wallet_sanctioned must not reference AnChain"
    assert "bei" not in src.lower(), "is_wallet_sanctioned must not reference BEI"


def test_check_bei_risk_removed():
    assert not hasattr(ref, "_check_bei_risk"), "_check_bei_risk must be removed"


def test_no_anchain_api_key_constant():
    assert not hasattr(ref, "ANCHAIN_API_KEY"), "ANCHAIN_API_KEY must be removed"
    assert not hasattr(ref, "ANCHAIN_BASE"), "ANCHAIN_BASE must be removed"


def test_health_includes_sanctions_source():
    from fastapi.testclient import TestClient
    client = TestClient(ref.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("sanctions_source") == "ofac_sdn_xml"
