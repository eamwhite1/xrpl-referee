"""Tests for _make_challenge_token / _verify_challenge_token."""
import base64
import hashlib
import hmac as _hmac
import os
import time

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-secret-for-challenge-tokens")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from referee import _make_challenge_token, _verify_challenge_token, app


# ---------------------------------------------------------------------------
# Unit tests — token structure and HMAC
# ---------------------------------------------------------------------------

def _decode_token(token: str) -> str:
    return base64.urlsafe_b64decode(token.encode() + b"==").decode()


def test_token_is_base64_encoded():
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-TEST1", "rBuyer1", expires)
    decoded = _decode_token(token)
    assert decoded.startswith("challenge:AT-TEST1:rBuyer1:")


def test_token_contains_hmac_sig():
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-TEST2", "rBuyer2", expires)
    decoded = _decode_token(token)
    parts = decoded.rsplit(":", 1)
    assert len(parts) == 2
    payload, sig = parts
    secret = os.getenv("JWT_SECRET", "")
    expected = _hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert sig == expected


def test_token_verify_returns_fields():
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-TEST3", "rBuyer3", expires)

    mock_vault = MagicMock()
    mock_vault.buyer_address = "rBuyer3"
    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = mock_vault

    result = _verify_challenge_token(token, mock_db)
    assert result["escrow_id"] == "AT-TEST3"
    assert result["buyer_address"] == "rBuyer3"
    assert result["expires"] == expires


def test_token_handles_hyphen_in_escrow_id():
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-ABCD-EFGH", "rBuyer4", expires)

    mock_vault = MagicMock()
    mock_vault.buyer_address = "rBuyer4"
    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = mock_vault

    result = _verify_challenge_token(token, mock_db)
    assert result["escrow_id"] == "AT-ABCD-EFGH"


def test_expired_token_raises_401():
    from fastapi import HTTPException
    expires = int(time.time()) - 1  # already expired
    token = _make_challenge_token("AT-EXP1", "rBuyer5", expires)

    mock_db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _verify_challenge_token(token, mock_db)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


def test_tampered_sig_raises_401():
    from fastapi import HTTPException
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-TAMP1", "rBuyer6", expires)
    # Flip one character in the token
    token_bytes = list(token)
    token_bytes[-3] = "X" if token_bytes[-3] != "X" else "Y"
    bad_token = "".join(token_bytes)

    mock_db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _verify_challenge_token(bad_token, mock_db)
    assert exc.value.status_code == 401


def test_vault_not_found_raises_401():
    from fastapi import HTTPException
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-MISSING", "rBuyer7", expires)

    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        _verify_challenge_token(token, mock_db)
    assert exc.value.status_code == 401
    assert "vault_not_found" in exc.value.detail


def test_buyer_mismatch_raises_401():
    from fastapi import HTTPException
    expires = int(time.time()) + 3600
    token = _make_challenge_token("AT-MISM1", "rRealBuyer", expires)

    mock_vault = MagicMock()
    mock_vault.buyer_address = "rDifferentBuyer"
    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = mock_vault

    with pytest.raises(HTTPException) as exc:
        _verify_challenge_token(token, mock_db)
    assert exc.value.status_code == 401
    assert "mismatch" in exc.value.detail


def test_garbage_input_raises_401():
    from fastapi import HTTPException
    mock_db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _verify_challenge_token("not-a-valid-token!!!", mock_db)
    assert exc.value.status_code == 401


def test_different_secrets_produce_different_tokens():
    expires = int(time.time()) + 3600
    with patch.dict(os.environ, {"JWT_SECRET": "secret-A"}):
        token_a = _make_challenge_token("AT-SEC1", "rBuyer8", expires)
    with patch.dict(os.environ, {"JWT_SECRET": "secret-B"}):
        token_b = _make_challenge_token("AT-SEC1", "rBuyer8", expires)
    assert token_a != token_b


def test_get_challenge_session_missing_token_returns_401():
    client = TestClient(app)
    resp = client.get("/challenge/session")
    assert resp.status_code == 401
