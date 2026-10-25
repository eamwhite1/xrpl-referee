"""Tests for Grok third-audit P1-P2 fixes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
import inspect


def test_poll_expired_uses_network_url():
    import referee as ref
    src = inspect.getsource(ref._poll_expired_escrows_once)
    assert "_xrpl_url_for_network" in src, "_poll_expired_escrows_once must use _xrpl_url_for_network"
    # Must not have bare XRPL_URL as a standalone literal (allow substring in larger names)
    import re
    bare_uses = re.findall(r'\bXRPL_URL\b', src)
    assert len(bare_uses) == 0, f"_poll_expired_escrows_once still uses bare XRPL_URL: {bare_uses}"


def test_poll_expired_uses_network_wallet():
    import referee as ref
    src = inspect.getsource(ref._poll_expired_escrows_once)
    assert "_referee_wallet_for_network" in src, "_poll_expired_escrows_once must use _referee_wallet_for_network"


def test_submit_lock_defined():
    import referee as ref
    assert hasattr(ref, "_get_submit_lock"), "_get_submit_lock must be defined"
    import asyncio
    lock = ref._get_submit_lock("mainnet")
    assert isinstance(lock, asyncio.Lock)


def test_submit_lock_per_network():
    import referee as ref
    lock_main = ref._get_submit_lock("mainnet")
    lock_test = ref._get_submit_lock("testnet")
    assert lock_main is not lock_test, "mainnet and testnet must have separate locks"


def test_auto_finish_uses_submit_lock():
    import referee as ref
    src = inspect.getsource(ref.auto_finish_escrow)
    assert "_get_submit_lock" in src, "auto_finish_escrow must use _get_submit_lock"


def test_finish_challenge_uses_submit_lock():
    import referee as ref
    src = inspect.getsource(ref._finish_challenge_fee_escrow)
    assert "_get_submit_lock" in src, "_finish_challenge_fee_escrow must use _get_submit_lock"


def test_cancel_challenge_uses_submit_lock():
    import referee as ref
    src = inspect.getsource(ref._cancel_challenge_fee_escrow)
    assert "_get_submit_lock" in src, "_cancel_challenge_fee_escrow must use _get_submit_lock"


def test_poll_expired_cancel_uses_submit_lock():
    import referee as ref
    src = inspect.getsource(ref._poll_expired_escrows_once)
    assert "_get_submit_lock" in src, "_poll_expired_escrows_once must use _get_submit_lock for cancel"


def test_rating_email_accepts_dict():
    import referee as ref
    src = inspect.getsource(ref._send_release_rating_emails)
    assert '.get(' in src, "_send_release_rating_emails must use .get() for dict access"


def test_rating_email_no_direct_attr():
    import referee as ref
    src = inspect.getsource(ref._send_release_rating_emails)
    # Must not directly access vault.escrow_id or vault.project_label
    assert "vault.escrow_id" not in src
    assert "vault.project_label" not in src


def test_evaluate_explorer_url_fallback():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "referee.py")
    with open(path) as f:
        src = f.read()
    assert "escrow_tx_hash" in src, "evaluate response must fall back to escrow_tx_hash for explorer_url"


def test_score_uses_score_max():
    import referee as ref
    src = inspect.getsource(ref.compute_xrpl_trust_score)
    assert "SCORE_MAX" in src
    assert "max_score" in src


def test_score_100_formula():
    import referee as ref
    src = inspect.getsource(ref.compute_xrpl_trust_score)
    assert "round(total * 100 / SCORE_MAX)" in src


def test_mcp_instructions_mention_testnet():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp_server.py")
    with open(path) as f:
        src = f.read()
    assert "testnet" in src.lower()
    assert "create_testnet_wallet" in src


def test_server_card_has_testnet():
    import referee as ref
    src = inspect.getsource(ref.serve_mcp_server_card)
    assert "testnet" in src


def test_agent_card_has_testnet():
    import referee as ref
    src = inspect.getsource(ref._build_agent_card)
    assert "testnet" in src
