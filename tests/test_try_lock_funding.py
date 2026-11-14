"""/try lock must wait for faucet-funded wallets to be validated before building EscrowCreate."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import referee  # noqa: E402


class _Resp:
    def __init__(self, ok):
        self._ok = ok

    def is_successful(self):
        return self._ok


class _FakeClient:
    """Each address becomes 'funded' after N lookups."""

    def __init__(self, ready_after):
        self.ready_after = dict(ready_after)
        self.calls = []

    async def request(self, req):
        self.calls.append((req.account, req.ledger_index))
        n = sum(1 for a, _ in self.calls if a == req.account)
        return _Resp(n > self.ready_after.get(req.account, 0))


@pytest.mark.asyncio
async def test_l1_wait_returns_once_accounts_validated():
    c = _FakeClient({"rBuyer": 2, "rWorker": 0})
    assert await referee._wait_accounts_funded(c, ["rBuyer", "rWorker"], timeout=5, interval=0) is True
    assert all(li == "validated" for _, li in c.calls)
    assert sum(1 for a, _ in c.calls if a == "rWorker") == 1


@pytest.mark.asyncio
async def test_l2_wait_times_out_when_never_funded():
    c = _FakeClient({"rBuyer": 10**9})
    assert await referee._wait_accounts_funded(c, ["rBuyer"], timeout=0.05, interval=0.01) is False


@pytest.mark.asyncio
async def test_l3_lock_returns_503_and_submits_nothing_when_wallets_not_ready(monkeypatch):
    from fastapi import HTTPException
    import xrpl.core.keypairs as kp

    seed = kp.generate_seed()
    addr = kp.derive_classic_address(kp.derive_keypair(seed)[0])
    sid = "test-sid-lock"
    referee._testnet_try_sessions[sid] = {
        "buyer_seed": seed, "worker_seed": seed, "buyer_address": addr, "worker_address": addr,
        "escrow_id": None, "step": "wallets_ready", "timeline": [],
        "expires_at": 10**12,
    }

    async def _never(*a, **k):
        return False

    submitted = []

    async def _no_autofill(*a, **k):
        submitted.append(1)
        raise AssertionError("autofill must not run before wallets are funded")

    monkeypatch.setattr(referee, "_wait_accounts_funded", _never)
    monkeypatch.setattr(referee, "autofill", _no_autofill)
    try:
        with pytest.raises(HTTPException) as ei:
            await referee.testnet_try_lock(sid)
        assert ei.value.status_code == 503
        assert ei.value.detail["error"] == "wallets_not_ready"
        assert submitted == []
        assert referee._testnet_try_sessions[sid]["step"] == "wallets_ready"
    finally:
        referee._testnet_try_sessions.pop(sid, None)
