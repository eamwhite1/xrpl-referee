"""Tests for P0/P1/P2 fixes: callback_url validation, metadata persist/echo, signed webhooks."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app():
    import referee as ref
    return ref.app


def _generate_client():
    from fastapi.testclient import TestClient
    import referee as ref
    return TestClient(ref.app)


# ---------------------------------------------------------------------------
# P1 — callback_url validation in Pydantic models
# ---------------------------------------------------------------------------

def test_escrow_setup_request_http_callback_rejected():
    """EscrowSetupRequest: http:// callback_url raises ValidationError."""
    from pydantic import ValidationError
    import referee as ref
    with pytest.raises((ValidationError, Exception)):
        ref.EscrowSetupRequest(
            escrow_id="test-id",
            buyer_name="Alice",
            buyer_address="rBuyerAddr",
            task_description="do something",
            worker_address="rWorkerAddr",
            callback_url="http://evil.com/hook",
        )


def test_escrow_request_body_http_callback_rejected():
    """EscrowRequestBody: http:// callback_url raises ValidationError."""
    from pydantic import ValidationError
    import referee as ref
    with pytest.raises((ValidationError, Exception)):
        ref.EscrowRequestBody(
            seller_address="rSeller1",
            buyer_address="rBuyer1",
            amount_xrp=5.0,
            task="do work",
            callback_url="http://notssl.example.com",
        )


def test_escrow_request_body_https_callback_accepted():
    """EscrowRequestBody: https:// callback_url is accepted."""
    import referee as ref
    body = ref.EscrowRequestBody(
        seller_address="rSeller1",
        buyer_address="rBuyer1",
        amount_xrp=5.0,
        task="do work",
        callback_url="https://valid.example.com/webhook",
    )
    assert body.callback_url == "https://valid.example.com/webhook"


def test_escrow_setup_request_callback_too_long_rejected():
    """EscrowSetupRequest: callback_url > 2048 chars raises ValidationError."""
    from pydantic import ValidationError
    import referee as ref
    long_url = "https://example.com/" + "a" * 2050
    with pytest.raises((ValidationError, Exception)):
        ref.EscrowSetupRequest(
            escrow_id="test-id",
            buyer_name="Alice",
            buyer_address="rBuyerAddr",
            task_description="task",
            worker_address="rWorkerAddr",
            callback_url=long_url,
        )


def test_escrow_request_body_none_callback_accepted():
    """EscrowRequestBody: None callback_url is accepted."""
    import referee as ref
    body = ref.EscrowRequestBody(
        seller_address="rSeller1",
        buyer_address="rBuyer1",
        amount_xrp=5.0,
        task="do work",
        callback_url=None,
    )
    assert body.callback_url is None


# ---------------------------------------------------------------------------
# P1 — metadata 4 KB cap
# ---------------------------------------------------------------------------

def test_oversize_metadata_rejected_on_model():
    """metadata dict that serializes > 4096 bytes should be rejected by the model validator."""
    import referee as ref
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="4 KB"):
        ref.EscrowRequestBody(
            seller_address="rSeller1",
            buyer_address="rBuyer1",
            amount_xrp=5.0,
            task="do work",
            metadata={"key": "x" * 5000},
        )


# ---------------------------------------------------------------------------
# P2 — _build_webhook_payload builds correct dict before commit
# ---------------------------------------------------------------------------

def test_build_webhook_payload_fields():
    """_build_webhook_payload returns correct dict and callback_url."""
    import referee as ref

    vault = MagicMock()
    vault.escrow_id = "esc-123"
    vault.status = "RELEASED"
    vault.release_tx_hash = "TXHASH1"
    vault.cancel_tx_hash = None
    vault.callback_url = "https://buyer.example.com/hook"
    vault.metadata_json = json.dumps({"order": "42"})

    payload, cb_url = ref._build_webhook_payload(vault, "RELEASING", "TXHASH1")

    assert payload["escrow_id"] == "esc-123"
    assert payload["status"] == "RELEASED"
    assert payload["previous_status"] == "RELEASING"
    assert payload["tx_hash"] == "TXHASH1"
    assert payload["metadata"] == {"order": "42"}
    assert cb_url == "https://buyer.example.com/hook"
    assert "callback_url" not in payload


def test_build_webhook_payload_no_metadata():
    """_build_webhook_payload handles missing metadata_json."""
    import referee as ref

    vault = MagicMock()
    vault.escrow_id = "esc-456"
    vault.status = "CANCELLED"
    vault.release_tx_hash = None
    vault.cancel_tx_hash = None
    vault.callback_url = None
    vault.metadata_json = None

    payload, cb_url = ref._build_webhook_payload(vault, "LOCKED")

    assert payload["metadata"] is None
    assert cb_url is None


# ---------------------------------------------------------------------------
# P2 — _fire_vault_webhook sends signed payload
# ---------------------------------------------------------------------------

def _public_resolver(monkeypatch, ref, addr="93.184.216.34"):
    import ipaddress
    monkeypatch.setattr(ref, "_resolve_public_ips", lambda host: [ipaddress.ip_address(addr)])


@pytest.mark.asyncio
async def test_fire_vault_webhook_sends_signed_payload(monkeypatch):
    """_fire_vault_webhook posts to the resolved public IP (Host header kept) with a valid signature."""
    import referee as ref
    import hmac, hashlib
    import httpx, respx

    monkeypatch.setattr(ref, "WEBHOOK_SIGNING_SECRET", "test-secret-key")
    _public_resolver(monkeypatch, ref)
    with respx.mock(assert_all_mocked=True) as m:
        route = m.post("https://93.184.216.34/hook").mock(return_value=httpx.Response(200))
        payload = {"event": "escrow.status_changed", "escrow_id": "esc-789", "status": "RELEASED", "ts": 1000}
        await ref._fire_vault_webhook(payload, "https://receiver.example.com/hook")
        assert route.call_count == 1
        sent = route.calls.last.request
    assert sent.headers["host"] == "receiver.example.com"
    sig_header = sent.headers.get("X-AgentTrust-Signature", "")
    assert sig_header.startswith("sha256=")
    # Signing format is "{ts}.{body}" — ts comes from the X-AgentTrust-Timestamp header
    msg = f"{sent.headers['X-AgentTrust-Timestamp']}.{sent.content.decode()}".encode()
    assert sig_header == "sha256=" + hmac.new(b"test-secret-key", msg, hashlib.sha256).hexdigest()
    assert json.loads(sent.content)["escrow_id"] == "esc-789"


@pytest.mark.asyncio
async def test_fire_vault_webhook_no_secret_no_sig(monkeypatch):
    """_fire_vault_webhook posts without a signature header when WEBHOOK_SIGNING_SECRET is empty."""
    import referee as ref
    import httpx, respx

    monkeypatch.setattr(ref, "WEBHOOK_SIGNING_SECRET", "")
    _public_resolver(monkeypatch, ref)
    with respx.mock(assert_all_mocked=True) as m:
        route = m.post("https://93.184.216.34/hook").mock(return_value=httpx.Response(200))
        await ref._fire_vault_webhook({"escrow_id": "x"}, "https://receiver.example.com/hook")
        assert route.call_count == 1
        sent = route.calls.last.request
    assert "X-AgentTrust-Signature" not in sent.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://192.168.1.1/hook",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/hook",
    "http://localhost/hook",
    "http://2130706433/hook",
])
async def test_fire_vault_webhook_ssrf_blocked(monkeypatch, url):
    """_fire_vault_webhook never connects to private, loopback or metadata destinations."""
    import referee as ref
    import httpx, respx

    monkeypatch.setattr(ref, "WEBHOOK_SIGNING_SECRET", "")
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as m:
        any_call = m.route().mock(return_value=httpx.Response(200))
        await ref._fire_vault_webhook({"escrow_id": "x"}, url)
        assert any_call.call_count == 0, "SSRF should have been blocked"
