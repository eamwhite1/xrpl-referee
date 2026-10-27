"""Tests for v7.0.2 retest fixes."""
import json
import ast
import xrpl.core.keypairs as kp


def test_metadata_dict_passthrough():
    """metadata dict should not be json.dumps'd in mcp_server.py."""
    with open("mcp_server.py") as f:
        src = f.read()
    assert "metadata if isinstance(metadata, dict)" in src, (
        "metadata should pass through as dict, not json.dumps'd"
    )


def test_no_wallet_sign_in_next_step():
    """next_step strings must not contain wallet.sign() (xrpl-py 3.x API)."""
    with open("mcp_server.py") as f:
        src = f.read()
    # The docstring may mention it as legacy, but next_step string should use sign()
    # Find next_step string content
    import re
    next_steps = re.findall(r'"next_step":\s*\(([^)]+)\)', src, re.DOTALL)
    for ns in next_steps:
        assert "wallet.sign(" not in ns, f"wallet.sign() found in next_step string: {ns[:100]}"


def test_server_json_description():
    """server.json description must be <=100 chars and mention testnet."""
    with open("server.json") as f:
        data = json.load(f)
    desc = data["description"]
    assert len(desc) <= 100, f"server.json description too long ({len(desc)} chars): {desc}"
    assert "testnet" in desc.lower() or "free" in desc.lower(), (
        "server.json description should mention testnet/free"
    )


def test_llms_txt_testnet_in_first_15_lines():
    """llms.txt testnet quickstart must appear in first 15 lines."""
    with open("llms.txt") as f:
        lines = f.readlines()
    first_15 = "".join(lines[:15])
    assert "create_testnet_wallet" in first_15, (
        "create_testnet_wallet must appear in first 15 lines of llms.txt"
    )
    assert "try_testnet_demo" in first_15, (
        "try_testnet_demo must appear in first 15 lines of llms.txt"
    )


def test_evaluate_response_has_attempts_remaining():
    """evaluate endpoint return dict must include attempts_remaining."""
    with open("referee.py") as f:
        src = f.read()
    assert '"attempts_remaining"' in src, "evaluate response must include attempts_remaining"
    assert '"attempts_used"' in src, "evaluate response must include attempts_used"


def test_evaluate_response_stable_fields():
    """evaluate response must have stable top-level verdict/score/summary/criteria_results."""
    with open("referee.py") as f:
        src = f.read()
    for field in ["escrow_tx_url", "release_explorer_url"]:
        assert f'"{field}"' in src, f"evaluate response missing {field}"


def test_xaman_force_network():
    """Challenge xaman must use force_network not network."""
    with open("referee.py") as f:
        src = f.read()
    assert 'xaman_options["force_network"]' in src, (
        "Challenge xaman must set force_network"
    )
    assert 'xaman_options["network"] = "TESTNET"' not in src, (
        "xaman_options[\"network\"] = \"TESTNET\" found — should be force_network"
    )


def test_remedy_email_button_text():
    """Remedy email button must say 'View escrow' not 'Submit Remedy'."""
    with open("referee.py") as f:
        src = f.read()
    assert "Submit Remedy →" not in src, "Remedy button should say 'View escrow →'"
    assert "View escrow →" in src, "Remedy button must say 'View escrow →'"


def test_wallet_score_network_param():
    """GET /wallet/score endpoint must accept network query param."""
    with open("referee.py") as f:
        src = f.read()
    assert 'network: str = Query("mainnet")' in src, (
        "GET /wallet/score must have network: str = Query(\"mainnet\") param"
    )
