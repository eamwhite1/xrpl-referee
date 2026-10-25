"""Tests for POST /escrow/request auth: Option 3 — at_session cookie."""
import os
import time
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-request-auth")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from referee import _verify_seller_ownership


def _make_jwt(account_id: str, secret: str = None) -> str:
    import jwt as pyjwt
    secret = secret or os.getenv("JWT_SECRET", "test-jwt-secret-for-request-auth")
    return pyjwt.encode({"sub": account_id, "exp": int(time.time()) + 3600}, secret, algorithm="HS256")


def _mock_request(cookie_value=None):
    req = MagicMock()
    req.cookies = {}
    if cookie_value:
        req.cookies["at_session"] = cookie_value
    return req


def _mock_db_with_linked_wallet(account_id: str, xrpl_address: str, verified: bool = True):
    from referee import LinkedWallet
    linked = MagicMock(spec=LinkedWallet)
    linked.xrpl_address = xrpl_address
    linked.verified = verified
    linked.account_id = account_id

    mock_db = MagicMock()
    # Build a filter chain that returns linked when all conditions match
    query_chain = MagicMock()
    query_chain.filter.return_value = query_chain
    query_chain.first.return_value = linked
    mock_db.query.return_value = query_chain
    return mock_db


def test_option3_valid_cookie_grants_access():
    account_id = "acc_test_001"
    seller = "rSellerVerified123"
    token = _make_jwt(account_id)
    req = _mock_request(cookie_value=token)
    db = _mock_db_with_linked_wallet(account_id, seller, verified=True)

    result = _verify_seller_ownership(
        seller_address=seller,
        buyer_address="rBuyer123",
        amount_xrp=5.0,
        task_desc="test task",
        buyer_email="buyer@test.com",
        api_key_header=None,
        sig_header=None,
        db=db,
        request=req,
    )
    assert result is True


def test_option3_no_cookie_falls_through_to_401():
    from fastapi import HTTPException
    seller = "rSeller999"
    req = _mock_request(cookie_value=None)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        _verify_seller_ownership(
            seller_address=seller,
            buyer_address="rBuyer999",
            amount_xrp=5.0,
            task_desc="test",
            buyer_email="b@test.com",
            api_key_header=None,
            sig_header=None,
            db=mock_db,
            request=req,
        )
    assert exc.value.status_code == 401


def test_option3_unverified_wallet_falls_through_to_401():
    from fastapi import HTTPException
    account_id = "acc_test_003"
    seller = "rSellerUnverified"
    token = _make_jwt(account_id)
    req = _mock_request(cookie_value=token)

    # DB returns None (unverified wallet filtered out)
    mock_db = MagicMock()
    query_chain = MagicMock()
    query_chain.filter.return_value = query_chain
    query_chain.first.return_value = None
    mock_db.query.return_value = query_chain

    with pytest.raises(HTTPException) as exc:
        _verify_seller_ownership(
            seller_address=seller,
            buyer_address="rBuyer003",
            amount_xrp=5.0,
            task_desc="test",
            buyer_email="b@test.com",
            api_key_header=None,
            sig_header=None,
            db=mock_db,
            request=req,
        )
    assert exc.value.status_code == 401
