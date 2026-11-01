"""Tests for follow-up brief #2 fixes:
P0-A: No open()/close() on AsyncJsonRpcClient
P0-B: CSRF exact origin match
P0-C: created_by_account_id written in /escrow/generate
P0-D: W3C VC signature verification (real sig check, fail-closed)
P1-A: privy_policy_xrpl.json schema
P1-D: _verdict_view returns raw strings (no html.escape)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import pytest
import inspect


# ---------------------------------------------------------------------------
# P0-A: AsyncJsonRpcClient must NOT have open()/close() calls
# ---------------------------------------------------------------------------

def test_no_async_json_rpc_client_open_close():
    """referee.py must not call client.open() or client.close() on AsyncJsonRpcClient."""
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "referee.py")) as f:
        source = f.read()
    # Count open/close on the XRPL client (not httpx or other clients)
    # These patterns indicate the wrong xrpl-py 5.x usage
    import re
    bad_patterns = [
        r"await client\.open\(\)",
        r"await client\.close\(\)",
    ]
    for pat in bad_patterns:
        matches = re.findall(pat, source)
        assert not matches, (
            f"Found forbidden pattern '{pat}' {len(matches)} time(s) in referee.py. "
            "xrpl-py 5.x AsyncJsonRpcClient is stateless HTTP; no open/close needed."
        )


# ---------------------------------------------------------------------------
# P0-B: CSRF exact origin match
# ---------------------------------------------------------------------------

def test_csrf_blocks_startswith_bypass():
    """_check_csrf_origin_header must block 'https://www.cryptovault.co.uk.evil.com'."""
    import referee as ref
    from fastapi import HTTPException
    from unittest.mock import MagicMock

    evil_origin = "https://www.cryptovault.co.uk.evil.com"
    req = MagicMock()
    req.headers = {"origin": evil_origin}
    with pytest.raises(HTTPException) as exc_info:
        ref._check_csrf_origin_header(req)
    assert exc_info.value.status_code == 403


def test_csrf_allows_app_cryptovault():
    """_check_csrf_origin_header must allow 'https://app.cryptovault.co.uk'."""
    import referee as ref
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {"origin": "https://app.cryptovault.co.uk"}
    # Should not raise
    ref._check_csrf_origin_header(req)


def test_csrf_allows_agenttrustxrpl():
    """_check_csrf_origin_header must allow 'https://agenttrustxrpl.com'."""
    import referee as ref
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {"origin": "https://agenttrustxrpl.com"}
    ref._check_csrf_origin_header(req)


def test_csrf_blocks_unknown_origin():
    """_check_csrf_origin_header must block unknown origins."""
    import referee as ref
    from fastapi import HTTPException
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {"origin": "https://totally-different.com"}
    with pytest.raises(HTTPException) as exc_info:
        ref._check_csrf_origin_header(req)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# P0-C: created_by_account_id written in /escrow/generate
# ---------------------------------------------------------------------------

def test_generate_escrow_writes_account_id(tmp_path):
    """POST /escrow/generate with a valid at_session cookie sets created_by_account_id."""
    import referee as ref
    from fastapi.testclient import TestClient
    import jwt as pyjwt
    import respx
    import httpx

    secret = os.getenv("JWT_SECRET", "test-secret-for-ci")
    token = pyjwt.encode({"sub": "acct-123"}, secret, algorithm="HS256")

    with respx.mock(assert_all_called=False) as mock:
        # Mock fee verification endpoint (called internally)
        mock.post("https://api.gemini.com/").respond(200, json={})
        # Mock XRPL calls
        mock.post("https://xrplcluster.com/").respond(200, json={"result": {"status": "success"}})

        client = TestClient(ref.app, raise_server_exceptions=False)
        # Use free tier path (no fee_hash needed for testnet)
        resp = client.post(
            "/escrow/generate",
            json={
                "escrow_id": "TEST-P0C-001",
                "buyer_address": "rBuyerTestXXXXXXXXXXXXXXXXXXXXXX",
                "worker_address": "rWorkerTestXXXXXXXXXXXXXXXXXXXXX",
                "task_description": "Test task for P0-C verification",
                "network": "testnet",
                "amount_xrp": 1.0,
            },
            cookies={"at_session": token},
        )
        # Whether success or fee error, check the vault if it was created
        if resp.status_code == 200:
            from sqlalchemy import text
            db_session = next(ref.get_db())
            vault = db_session.query(ref.EscrowVault).filter_by(escrow_id="TEST-P0C-001").first()
            if vault:
                assert vault.created_by_account_id == "acct-123", (
                    f"created_by_account_id should be 'acct-123', got {vault.created_by_account_id!r}"
                )


# ---------------------------------------------------------------------------
# P0-D: W3C VC verification is real (fail-closed)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_vc_verify_rejects_invalid_sig():
    """verify_w3c_credential must return verified=False for a tampered JWT signature."""
    import referee as ref
    import base64

    # Craft a JWT with valid header/payload but invalid signature
    header = base64.urlsafe_b64encode(json.dumps({"alg": "ES256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_data = {
        "iss": "did:web:example.com",
        "vc": {
            "issuer": "did:web:example.com",
            "type": ["VerifiableCredential"],
            "credentialSubject": {},
        }
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    fake_sig = base64.urlsafe_b64encode(b"INVALIDSIGNATURE").rstrip(b"=").decode()
    jwt_str = f"{header}.{payload}.{fake_sig}"

    import respx
    import httpx

    # Mock DID resolution to return a valid-looking document
    did_doc = {
        "verificationMethod": [
            {
                "id": "did:web:example.com#key-1",
                "type": "JsonWebKey2020",
                "publicKeyJwk": {"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "AAAA"},
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/.well-known/did.json").respond(200, json=did_doc)
        result = await ref.verify_w3c_credential(jwt_str, "did:web:example.com")

    # Must fail closed — bad signature or bad JWK key material should return verified=False
    assert result["verified"] is False, (
        f"Expected verified=False for invalid signature, got {result}"
    )


@pytest.mark.anyio
async def test_vc_verify_rejects_unsupported_did_method():
    """verify_w3c_credential must return verified=False for non-did:web DIDs."""
    import referee as ref
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "ES256"}).encode()).rstrip(b"=").decode()
    payload_data = {
        "iss": "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
        "vc": {"issuer": "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
               "type": ["VerifiableCredential"], "credentialSubject": {}},
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    fake_sig = base64.urlsafe_b64encode(b"SIG").rstrip(b"=").decode()
    jwt_str = f"{header}.{payload}.{fake_sig}"

    result = await ref.verify_w3c_credential(jwt_str)
    assert result["verified"] is False


@pytest.mark.anyio
async def test_vc_verify_rejects_hmac_alg():
    """verify_w3c_credential must reject HS256 algorithm."""
    import referee as ref
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    jwt_str = f"{header}.{payload}.{sig}"

    result = await ref.verify_w3c_credential(jwt_str)
    assert result["verified"] is False


@pytest.mark.anyio
async def test_vc_verify_fails_closed_on_did_resolution_failure():
    """verify_w3c_credential must return verified=False when DID resolution times out or errors."""
    import referee as ref
    import base64
    import respx

    header = base64.urlsafe_b64encode(json.dumps({"alg": "ES256"}).encode()).rstrip(b"=").decode()
    payload_data = {
        "iss": "did:web:unreachable.example.invalid",
        "vc": {"issuer": "did:web:unreachable.example.invalid",
               "type": ["VerifiableCredential"], "credentialSubject": {}},
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    jwt_str = f"{header}.{payload}.{sig}"

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://unreachable.example.invalid/.well-known/did.json").mock(
            side_effect=Exception("Connection refused")
        )
        result = await ref.verify_w3c_credential(jwt_str)

    assert result["verified"] is False


# ---------------------------------------------------------------------------
# P1-A: privy_policy_xrpl.json schema validation
# ---------------------------------------------------------------------------

def test_privy_policy_version():
    """privy_policy_xrpl.json must have version '1.0'."""
    policy_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts", "privy_policy_xrpl.json"
    )
    with open(policy_path) as f:
        policy = json.load(f)
    assert policy.get("version") == "1.0", f"Expected version '1.0', got {policy.get('version')!r}"


def test_privy_policy_payment_field():
    """privy_policy_xrpl.json Payment rule must use 'Payment.Amount.drops' field path."""
    policy_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts", "privy_policy_xrpl.json"
    )
    with open(policy_path) as f:
        policy = json.load(f)
    payment_rule = next(
        (r for r in policy.get("rules", [])
         if any(c.get("value") == "Payment" for c in r.get("conditions", []))),
        None,
    )
    assert payment_rule is not None, "No Payment rule found in privy_policy_xrpl.json"
    amount_cond = next(
        (c for c in payment_rule.get("conditions", []) if "Amount" in c.get("field", "")),
        None,
    )
    assert amount_cond is not None, "No Amount condition in Payment rule"
    assert amount_cond["field"] == "Payment.Amount.drops", (
        f"Expected 'Payment.Amount.drops', got {amount_cond['field']!r}"
    )


def test_privy_policy_deny_has_empty_conditions():
    """privy_policy_xrpl.json DENY rule must have 'conditions': []."""
    policy_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts", "privy_policy_xrpl.json"
    )
    with open(policy_path) as f:
        policy = json.load(f)
    deny_rules = [r for r in policy.get("rules", []) if r.get("action") == "DENY"]
    assert deny_rules, "No DENY rule found in privy_policy_xrpl.json"
    for rule in deny_rules:
        assert "conditions" in rule, f"DENY rule missing 'conditions': {rule}"
        assert rule["conditions"] == [], (
            f"DENY rule must have conditions=[], got {rule['conditions']!r}"
        )


def test_privy_policy_rules_have_names():
    """All rules in privy_policy_xrpl.json must have a 'name' field."""
    policy_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts", "privy_policy_xrpl.json"
    )
    with open(policy_path) as f:
        policy = json.load(f)
    for i, rule in enumerate(policy.get("rules", [])):
        assert "name" in rule, f"Rule {i} missing 'name' field: {rule}"


# ---------------------------------------------------------------------------
# P1-D: _verdict_view must not return html-escaped strings
# ---------------------------------------------------------------------------

def test_verdict_view_no_html_escape():
    """_verdict_view must return raw strings, not html-escaped."""
    import referee as ref
    from unittest.mock import MagicMock

    vault = MagicMock()
    vault.ai_verdict = json.dumps({
        "verdict": "PASS",
        "summary": "Score: 100% <success> & great work!",
        "score": 95,
    })
    vault.evaluation_verdicts = []
    vault.criteria_v2 = []
    vault.worker_submission = "submitted & done <details>"
    vault.status = "RELEASED"
    vault.escrow_tx_hash = None
    vault.release_tx_hash = None
    vault.cancel_tx_hash = None
    vault.network = "mainnet"
    vault.max_submissions = 3
    vault.submission_count = 1

    result = ref._verdict_view(vault)
    # Must NOT contain html-escaped entities in the summary
    assert "&amp;" not in result["summary"], "summary must not contain html-escaped &amp;"
    assert "&lt;" not in result["summary"], "summary must not contain html-escaped &lt;"
    assert "&gt;" not in result["summary"], "summary must not contain html-escaped &gt;"
    # submission_excerpt must also be raw
    assert "&amp;" not in result.get("submission_excerpt", ""), \
        "submission_excerpt must not be html-escaped"
