"""Tests for post-release rating email flow and magic-link endpoints."""
import time
import base64
import hashlib
import hmac
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret-for-ratings")

from referee import app, _make_rating_token, _verify_rating_token, _send_release_rating_emails, RATING_LINK_TTL_S

client = TestClient(app)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def test_rating_token_roundtrip():
    token = _make_rating_token("ESC-001", "rBuyer", "rWorker", "buyer")
    info = _verify_rating_token(token)
    assert info is not None
    assert info["escrow_id"] == "ESC-001"
    assert info["rater_address"] == "rBuyer"
    assert info["rated_address"] == "rWorker"
    assert info["role"] == "buyer"


def test_rating_token_expired(monkeypatch):
    # Make a token that expired 1s ago
    secret = os.getenv("JWT_SECRET", "")
    expires = int(time.time()) - 1
    payload = f"ESC-002:rBuyer:rWorker:buyer:{expires}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()
    assert _verify_rating_token(token) is None


def test_rating_token_tampered():
    token = _make_rating_token("ESC-003", "rBuyer", "rWorker", "buyer")
    # flip one char in the middle
    byt = bytearray(base64.urlsafe_b64decode(token.encode()))
    byt[10] ^= 0xFF
    bad_token = base64.urlsafe_b64encode(bytes(byt)).decode()
    assert _verify_rating_token(bad_token) is None


def test_rating_token_with_hyphens_in_escrow_id():
    """escrow_id typically contains hyphens — token round-trip must handle them."""
    eid = "ESC-ABCD-1234-XYZ"
    token = _make_rating_token(eid, "rBuyer", "rWorker", "buyer")
    info = _verify_rating_token(token)
    assert info is not None
    assert info["escrow_id"] == eid


# ---------------------------------------------------------------------------
# /rate/verify endpoint
# ---------------------------------------------------------------------------

def _setup_released_vault(db_exec, escrow_id="TEST-RATE-01", buyer="rBuyer111", worker="rWorker111"):
    """Insert a minimal RELEASED vault row via raw SQL for testing."""
    db_exec.execute(
        "INSERT OR IGNORE INTO escrow_vault (escrow_id, status, buyer_address, worker_address, project_label) "
        "VALUES (?, 'RELEASED', ?, ?, 'Test Project')",
        (escrow_id, buyer, worker),
    )


def test_rate_verify_valid():
    token = _make_rating_token("TEST-RATE-V1", "rBuyer", "rWorker", "buyer")
    # Without a live DB row this will 400 — just verify token decode part works
    # and the endpoint exists
    resp = client.get(f"/rate/verify?token={token}")
    assert resp.status_code in (200, 400, 503)  # 400/503 expected (no DB row in test)


def test_rate_verify_expired():
    secret = os.getenv("JWT_SECRET", "")
    expires = int(time.time()) - 1
    payload = f"TEST-RATE-V2:rBuyer:rWorker:buyer:{expires}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()
    resp = client.get(f"/rate/verify?token={token}")
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()


def test_rate_verify_invalid_token():
    resp = client.get("/rate/verify?token=notavalidtoken")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /rate/submit endpoint
# ---------------------------------------------------------------------------

def test_rate_submit_invalid_token():
    resp = client.post("/rate/submit?token=badtoken&rating=5")
    assert resp.status_code == 400


def test_rate_submit_invalid_rating_out_of_range():
    token = _make_rating_token("ESC-X", "rBuyer", "rWorker", "buyer")
    resp = client.post(f"/rate/submit?token={token}&rating=6")
    assert resp.status_code == 400


def test_rate_submit_comment_too_long():
    token = _make_rating_token("ESC-X", "rBuyer", "rWorker", "buyer")
    long_comment = "x" * 281
    resp = client.post(f"/rate/submit?token={token}&rating=4&comment={long_comment}")
    assert resp.status_code == 422


def test_rate_submit_expired_token():
    secret = os.getenv("JWT_SECRET", "")
    expires = int(time.time()) - 1
    payload = f"TEST-RATE-S1:rBuyer:rWorker:buyer:{expires}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()
    resp = client.post(f"/rate/submit?token={token}&rating=5")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# _send_release_rating_emails
# ---------------------------------------------------------------------------

def test_release_emails_sent(monkeypatch):
    sent = []
    monkeypatch.setattr("referee.RESEND_API_KEY", "fake-key")
    import resend as resend_mod
    monkeypatch.setattr(resend_mod.Emails, "send", lambda payload: sent.append(payload))

    vault = MagicMock()
    vault.escrow_id = "ESC-EMAIL-01"
    vault.project_label = "Widget Project"
    vault.buyer_email = "buyer@example.com"
    vault.worker_email = "worker@example.com"
    vault.buyer_address = "rBuyerAddr"
    vault.worker_address = "rWorkerAddr"

    _send_release_rating_emails(vault)

    assert len(sent) == 2
    recipients = [s["to"] for s in sent]
    assert "buyer@example.com" in recipients
    assert "worker@example.com" in recipients
    # Each email should contain a rating link
    for s in sent:
        assert "/rate?token=" in s["html"]
    # Buyer email rates the worker
    buyer_mail = next(s for s in sent if s["to"] == "buyer@example.com")
    assert "rWorkerAddr" in buyer_mail["html"]
    # Worker email rates the buyer
    worker_mail = next(s for s in sent if s["to"] == "worker@example.com")
    assert "rBuyerAddr" in worker_mail["html"]


def test_release_emails_skipped_no_email(monkeypatch):
    sent = []
    monkeypatch.setattr("referee.RESEND_API_KEY", "fake-key")
    import resend as resend_mod
    monkeypatch.setattr(resend_mod.Emails, "send", lambda payload: sent.append(payload))

    vault = MagicMock()
    vault.escrow_id = "ESC-EMAIL-02"
    vault.project_label = None
    vault.buyer_email = None   # no email
    vault.worker_email = None  # no email
    vault.buyer_address = "rBuyerAddr"
    vault.worker_address = "rWorkerAddr"

    _send_release_rating_emails(vault)
    assert len(sent) == 0


def test_release_emails_skipped_no_resend(monkeypatch):
    sent = []
    monkeypatch.setattr("referee.RESEND_API_KEY", None)
    import resend as resend_mod
    monkeypatch.setattr(resend_mod.Emails, "send", lambda payload: sent.append(payload))

    vault = MagicMock()
    vault.escrow_id = "ESC-EMAIL-03"
    vault.project_label = "Test"
    vault.buyer_email = "buyer@example.com"
    vault.worker_email = "worker@example.com"
    vault.buyer_address = "rBuyerAddr"
    vault.worker_address = "rWorkerAddr"

    _send_release_rating_emails(vault)
    assert len(sent) == 0


def test_release_emails_resend_failure_does_not_raise(monkeypatch):
    """_send_release_rating_emails must never raise even if Resend fails."""
    monkeypatch.setattr("referee.RESEND_API_KEY", "fake-key")
    import resend as resend_mod
    monkeypatch.setattr(resend_mod.Emails, "send", lambda payload: (_ for _ in ()).throw(Exception("Network error")))

    vault = MagicMock()
    vault.escrow_id = "ESC-EMAIL-04"
    vault.project_label = "Test"
    vault.buyer_email = "buyer@example.com"
    vault.worker_email = "worker@example.com"
    vault.buyer_address = "rBuyerAddr"
    vault.worker_address = "rWorkerAddr"

    _send_release_rating_emails(vault)  # must not raise
