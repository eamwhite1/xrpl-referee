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

@pytest.mark.asyncio
async def test_fire_vault_webhook_sends_signed_payload(monkeypatch):
    """_fire_vault_webhook posts body with X-AgentTrust-Signature header when secret is set."""
    import referee as ref
    import hmac, hashlib

    monkeypatch.setattr(ref, "WEBHOOK_SIGNING_SECRET", "test-secret-key")
    monkeypatch.setattr(ref, "_PRIVATE_IP_PREFIXES", ())

    captured = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, content, headers):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return FakeResponse()

    import socket as _socket
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, ("203.0.113.1", 443))])

    with patch("httpx.AsyncClient", return_value=FakeClient()):
        payload = {"event": "escrow.status_changed", "escrow_id": "esc-789", "status": "RELEASED", "ts": 1000}
        await ref._fire_vault_webhook(payload, "https://receiver.example.com/hook")

    assert "url" in captured
    assert captured["url"] == "https://receiver.example.com/hook"
    sig_header = captured["headers"].get("X-AgentTrust-Signature", "")
    assert sig_header.startswith("sha256=")
    # Signing format is "{ts}.{body}" — extract ts from X-AgentTrust-Timestamp header
    ts_header = captured["headers"].get("X-AgentTrust-Timestamp", "")
    msg = f"{ts_header}.{captured['content'].decode()}".encode()
    expected = "sha256=" + hmac.new(b"test-secret-key", msg, hashlib.sha256).hexdigest()
    assert sig_header == expected


@pytest.mark.asyncio
async def test_fire_vault_webhook_no_secret_no_sig(monkeypatch):
    """_fire_vault_webhook posts without signature header when WEBHOOK_SIGNING_SECRET is empty."""
    import referee as ref

    monkeypatch.setattr(ref, "WEBHOOK_SIGNING_SECRET", "")
    monkeypatch.setattr(ref, "_PRIVATE_IP_PREFIXES", ())

    captured = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, content, headers):
            captured["headers"] = headers
            return FakeResponse()

    import socket as _socket
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, ("203.0.113.1", 443))])

    with patch("httpx.AsyncClient", return_value=FakeClient()):
        await ref._fire_vault_webhook({"escrow_id": "x"}, "https://receiver.example.com/hook")

    assert "X-AgentTrust-Signature" not in captured.get("headers", {})


@pytest.mark.asyncio
async def test_fire_vault_webhook_ssrf_blocked(monkeypatch):
    """_fire_vault_webhook blocks private IP destinations."""
    import referee as ref

    monkeypatch.setattr(ref, "WEBHOOK_SIGNING_SECRET", "")

    import socket as _socket
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, ("192.168.1.1", 443))])

    posted = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, *a, **kw):
            posted.append(True)

    with patch("httpx.AsyncClient", return_value=FakeClient()):
        await ref._fire_vault_webhook({"escrow_id": "x"}, "https://internal.example.com/hook")

    assert not posted, "SSRF should have been blocked"
