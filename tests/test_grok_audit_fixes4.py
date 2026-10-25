"""Tests for Grok fourth-audit copy/consistency fixes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import inspect
import pytest


def _read_mcp_server_src():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp_server.py")
    with open(path) as f:
        return f.read()


def _read_referee_src():
    import referee as ref
    return open(inspect.getfile(ref)).read()


# ---------------------------------------------------------------------------
# 1. explorer_url in challenge-window PASS
# ---------------------------------------------------------------------------

def test_pass_challenge_window_returns_explorer_url():
    import referee as ref
    src = inspect.getsource(ref.evaluate_work)
    # The pass_challenge_window return block must include explorer_url
    assert "pass_challenge_window" in src
    assert "explorer_url" in src, "evaluate_work must return explorer_url in pass_challenge_window block"
    # Must use escrow_tx_hash as fallback when finish_hash is absent
    assert "escrow_tx_hash" in src


# ---------------------------------------------------------------------------
# 2. Email badge shows /133 not /100
# ---------------------------------------------------------------------------

def test_email_badge_uses_133_scale():
    src = _read_referee_src()
    assert "trust_score}/100" not in src, "Email badge must not say /100 (raw max is 133)"
    assert "trust_score}/133" in src, "Email badge must say /133"


# ---------------------------------------------------------------------------
# 3. Sanctions wording — OFAC SDN everywhere in user-facing text
# ---------------------------------------------------------------------------

def test_mcp_get_wallet_trust_score_no_anchain():
    src = _read_mcp_server_src()
    # The tool docstring must not mention AnChain.ai BEI in the user-visible description
    assert "AnChain.ai BEI — OFAC/UN/UK/EU/Canada/Australia" not in src
    assert "multi-jurisdiction sanctions screening\n    (AnChain.ai BEI" not in src


def test_mcp_get_wallet_trust_score_says_ofac_sdn():
    src = _read_mcp_server_src()
    assert "OFAC SDN" in src, "get_wallet_trust_score description must say OFAC SDN screening"


def test_get_wallet_score_endpoint_no_anchain():
    """The REST endpoint docstring (which becomes OpenAPI) must not say AnChain.ai BEI."""
    import referee as ref
    src = inspect.getsource(ref.get_wallet_score)
    assert "AnChain.ai BEI" not in src, "get_wallet_score docstring must not say AnChain.ai BEI"
    assert "OFAC SDN" in src, "get_wallet_score docstring must say OFAC SDN"


# ---------------------------------------------------------------------------
# 4. post_job testnet line removed from xrpl-referee llms.txt
# ---------------------------------------------------------------------------

def test_llms_txt_no_post_job_testnet():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "llms.txt")
    with open(path) as f:
        src = f.read()
    assert 'Post a job with network="testnet"' not in src, \
        "xrpl-referee/llms.txt must not say 'Post a job with network=testnet'"


# ---------------------------------------------------------------------------
# 5. server-card / agent-card testnet note includes monetary value disclaimer
# ---------------------------------------------------------------------------

def test_server_card_testnet_disclaimer():
    import referee as ref
    src = inspect.getsource(ref.serve_mcp_server_card)
    assert "no monetary value" in src or "Test XRP has no monetary value" in src, \
        "server-card testnet note must mention monetary value"


def test_agent_card_testnet_disclaimer():
    import referee as ref
    src = inspect.getsource(ref._build_agent_card)
    assert "no monetary value" in src or "Test XRP has no monetary value" in src, \
        "agent-card testnet note must mention monetary value"


# ---------------------------------------------------------------------------
# 6. openapi.json info description — no Enterprise-grade / tamper-proof
# ---------------------------------------------------------------------------

def test_openapi_no_enterprise_grade():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "openapi.json")
    with open(path) as f:
        d = json.load(f)
    desc = d.get("info", {}).get("description", "")
    assert "Enterprise-grade" not in desc, "openapi.json info.description must not say Enterprise-grade"
    assert "tamper-proof" not in desc, "openapi.json info.description must not say tamper-proof"


# ---------------------------------------------------------------------------
# 8. FastAPI app description — no Enterprise-grade / tamper-proof
# ---------------------------------------------------------------------------

def test_fastapi_app_description_factual():
    import referee as ref
    src = inspect.getsource(ref)
    # Check app description string
    assert "Enterprise-grade AI task verification" not in src, \
        "FastAPI app description must not say Enterprise-grade"


# ---------------------------------------------------------------------------
# EscrowSetupRequest network field has description
# ---------------------------------------------------------------------------

def test_escrow_setup_request_network_description():
    import referee as ref
    import json as _json
    # Get the OpenAPI schema from the FastAPI app
    from fastapi.testclient import TestClient
    client = TestClient(ref.app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    schemas = spec.get("components", {}).get("schemas", {})
    # Find EscrowSetupRequest or the inline schema for /escrow/generate
    found = False
    for name, schema in schemas.items():
        if "EscrowSetup" in name or "escrow" in name.lower():
            props = schema.get("properties", {})
            if "network" in props and props["network"].get("description"):
                found = True
                break
    if not found:
        # Check inline in paths
        paths = spec.get("paths", {})
        gen = paths.get("/escrow/generate", {})
        props = gen.get("post", {}).get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("properties", {})
        if "network" in props and props["network"].get("description"):
            found = True
    assert found, "EscrowSetupRequest network field must have a description in the OpenAPI schema"
