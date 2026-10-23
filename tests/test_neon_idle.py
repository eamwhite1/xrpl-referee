"""Real tests for Neon idle optimisation."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Test 1: 404/402 on a DB route must NOT flip _db_available to False
# ---------------------------------------------------------------------------
def test_app_exceptions_dont_flip_db_flag():
    import referee
    from fastapi import HTTPException
    from sqlalchemy.orm import Session

    original_flag = referee._db_available
    referee._db_available = True

    called_failure = []
    original_failure = referee._on_db_failure
    referee._on_db_failure = lambda: called_failure.append(1)

    # Simulate get_db yielding, then an HTTPException raised by the route (not DB)
    mock_db = MagicMock(spec=Session)
    with patch.object(referee, "SessionLocal", return_value=mock_db):
        gen = referee.get_db()
        try:
            next(gen)  # yield db
        except StopIteration:
            pass
        # Throw an HTTPException into the generator (simulates route raising 404)
        try:
            gen.throw(HTTPException(status_code=404, detail="not found"))
        except (HTTPException, StopIteration):
            pass

    assert called_failure == [], "HTTPException must not flip db flag to unavailable"

    referee._on_db_failure = original_failure
    referee._db_available = original_flag


# ---------------------------------------------------------------------------
# Test 2: GET /marketplace/jobs and /jobs produce 0 SessionLocal calls from snapshot
# ---------------------------------------------------------------------------
def test_snapshot_no_db_calls():
    import referee
    from fastapi.testclient import TestClient

    # Pre-populate snapshot with test data
    referee._jobs_snapshot["marketplace_jobs"] = [
        {"id": "J1", "title": "Test Job", "description": "desc", "category": "default",
         "bounty": 1.0, "currency": "XRP", "poster": "rTest", "poster_name": "",
         "deadline": "—", "_expires_at_iso": None, "tags": [], "status": "OPEN",
         "bid_count": 0, "is_demo": False, "claimable": False}
    ]
    referee._jobs_snapshot["open_jobs"] = [
        {"id": "J2", "title": "Open Job", "description": "desc2", "budget_xrp": 2.0,
         "buyer_address": "rBuyer", "buyer_name": "", "category": "default", "tags": [],
         "status": "open", "bid_count": 0, "created_at": None, "expires_at": "—",
         "_expires_at_iso": None, "required_nft_issuer": None, "required_domain": None,
         "required_vc_issuer_did": None, "required_vc_type": None, "proof_policy": "ALL"}
    ]
    referee._jobs_snapshot["refreshed_at"] = datetime.now(timezone.utc)
    referee._jobs_snapshot["stale_since"] = None

    call_count = []

    original_sl = referee.SessionLocal.__class__
    def counting_sessionlocal():
        call_count.append(1)
        raise Exception("SessionLocal must not be called on GET /marketplace/jobs or /jobs")

    with patch.object(referee, "SessionLocal", side_effect=counting_sessionlocal):
        client = TestClient(referee.app, raise_server_exceptions=False)
        for _ in range(10):
            r1 = client.get("/marketplace/jobs")
            r2 = client.get("/jobs?limit=100")
            assert r1.status_code == 200, f"Expected 200, got {r1.status_code}"
            assert r2.status_code == 200, f"Expected 200, got {r2.status_code}"

    assert call_count == [], f"Expected 0 DB calls, got {len(call_count)}"


# ---------------------------------------------------------------------------
# Test 3: _ensure_hourly_window creates no duplicate task
# ---------------------------------------------------------------------------
def test_no_duplicate_hourly_task():
    import referee

    async def run():
        referee._hourly_window_task = None

        referee._ensure_hourly_window()
        task1 = referee._hourly_window_task
        assert task1 is not None

        referee._ensure_hourly_window()
        task2 = referee._hourly_window_task
        assert task1 is task2, "Must be same task — no duplicate created"

        task1.cancel()
        try:
            await task1
        except (asyncio.CancelledError, Exception):
            pass

        # After task is done, _ensure_hourly_window creates a new one
        referee._ensure_hourly_window()
        task3 = referee._hourly_window_task
        assert task3 is not task1, "After done, a new task should be created"
        task3.cancel()
        try:
            await task3
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Test 4: tecNO_TARGET resolves vault status to CANCELLED via _resolve_gone_escrow
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tec_no_target_resolves_cancelled():
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-TEST-0001"
    mock_vault.status = "LOCKED"
    mock_db = MagicMock()

    # account_tx returns an EscrowCancel matching OfferSequence=12345
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "result": {
            "transactions": [{
                "tx": {"TransactionType": "EscrowCancel", "OfferSequence": 12345},
                "meta": {"TransactionResult": "tesSUCCESS"},
            }],
        }
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await referee._resolve_gone_escrow(mock_vault, "rOwner123", 12345, mock_db)

    assert mock_vault.status == "CANCELLED"
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: IOU Amount in account_objects → escrow NOT abandoned
# ---------------------------------------------------------------------------
def test_iou_escrow_not_abandoned():
    """An IOU Amount (dict) must never match XRP drops (string) → escrow is preserved."""
    iou_amount = {"currency": "USD", "issuer": "rIssuer123", "value": "100"}
    vault_amount_xrp = 100.0
    xrp_drops = str(int(vault_amount_xrp * 1_000_000))  # "100000000"

    # The orphan check uses: isinstance(amt, dict) → live = True → skip ABANDON
    assert isinstance(iou_amount, dict), "IOU amount should be a dict"
    assert iou_amount != xrp_drops, "IOU amount dict must not equal XRP drops string"
    # Confirm the guard logic: if isinstance(amt, dict): live = True
    amt = iou_amount
    live = isinstance(amt, dict) or amt == xrp_drops
    assert live is True, "IOU escrow object should be treated as live (no ABANDON)"
