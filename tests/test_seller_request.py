"""Tests for _verify_seller_ownership in referee.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import hashlib
import time
import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException

from xrpl.core.keypairs import generate_seed, derive_keypair, derive_classic_address, sign


def _make_sig_header(seller_address, buyer_address, amount_xrp, task_desc, private_key, public_key, ts=None):
    if ts is None:
        ts = int(time.time())
    task_hash = hashlib.sha256(task_desc.encode()).hexdigest()[:16]
    amount_drops = int(round(float(amount_xrp) * 1_000_000))
    message = f"agenttrust-request:{seller_address}:{buyer_address}:{amount_drops}:{task_hash}:{ts}"
    sig_hex = sign(message.encode(), private_key)
    return f"{sig_hex}:{ts}:{public_key}", ts


def _mock_db():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def test_valid_signature_returns_true():
    """Valid xrpl signature returns True."""
    import referee as ref

    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    address = derive_classic_address(pub)

    sig_header, _ = _make_sig_header(address, "rBuyer123", 10.0, "write a script", priv, pub)

    result = ref._verify_seller_ownership(
        seller_address=address,
        buyer_address="rBuyer123",
        amount_xrp=10.0,
        task_desc="write a script",
        buyer_email=None,
        api_key_header=None,
        sig_header=sig_header,
        db=_mock_db(),
    )
    assert result is True


def test_tampered_amount_rejected():
    """Signing with amount=10 but calling with amount=50 → 401."""
    import referee as ref

    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    address = derive_classic_address(pub)

    # Sign for amount=10.0
    sig_header, _ = _make_sig_header(address, "rBuyer123", 10.0, "write a script", priv, pub)

    with pytest.raises(HTTPException) as exc:
        ref._verify_seller_ownership(
            seller_address=address,
            buyer_address="rBuyer123",
            amount_xrp=50.0,  # different amount
            task_desc="write a script",
            buyer_email=None,
            api_key_header=None,
            sig_header=sig_header,
            db=_mock_db(),
        )
    assert exc.value.status_code == 401


def test_missing_header_rejected():
    """No api_key and no sig_header → raises 401."""
    import referee as ref

    with pytest.raises(HTTPException) as exc:
        ref._verify_seller_ownership(
            seller_address="rSomeAddress",
            buyer_address="rBuyer",
            amount_xrp=5.0,
            task_desc="task",
            buyer_email=None,
            api_key_header=None,
            sig_header=None,
            db=_mock_db(),
        )
    assert exc.value.status_code == 401


def test_replay_rejected():
    """Submitting the same signature twice → second raises 409."""
    import referee as ref

    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    address = derive_classic_address(pub)

    # Clear replay cache to avoid cross-test interference
    ref._seller_sig_replay.clear()

    sig_header, _ = _make_sig_header(address, "rBuyer999", 25.0, "build an API", priv, pub)

    # First call succeeds
    result = ref._verify_seller_ownership(
        seller_address=address,
        buyer_address="rBuyer999",
        amount_xrp=25.0,
        task_desc="build an API",
        buyer_email=None,
        api_key_header=None,
        sig_header=sig_header,
        db=_mock_db(),
    )
    assert result is True

    # Second call with same sig → 409
    with pytest.raises(HTTPException) as exc:
        ref._verify_seller_ownership(
            seller_address=address,
            buyer_address="rBuyer999",
            amount_xrp=25.0,
            task_desc="build an API",
            buyer_email=None,
            api_key_header=None,
            sig_header=sig_header,
            db=_mock_db(),
        )
    assert exc.value.status_code == 409
