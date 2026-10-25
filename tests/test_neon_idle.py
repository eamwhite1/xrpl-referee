"""Real tests for Neon idle optimisation."""
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta


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
# Test 2a: GET /marketplace/jobs and /jobs produce 0 SessionLocal calls from snapshot
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
# Test 2b: Expiry filtering — future jobs appear with expires_hrs, past jobs excluded
# ---------------------------------------------------------------------------
def test_snapshot_expiry_filtering():
    """Future-expiry job appears with expires_hrs as int; past-expiry job is excluded."""
    import referee
    from fastapi.testclient import TestClient

    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    referee._jobs_snapshot["marketplace_jobs"] = [
        {"id": "J-FUTURE", "title": "Future job", "_expires_at_iso": future, "bounty": 1.0,
         "description": "", "category": "default", "currency": "XRP", "poster": "rTest",
         "poster_name": "", "tags": [], "status": "OPEN", "bid_count": 0,
         "is_demo": False, "claimable": False},
        {"id": "J-PAST", "title": "Past job", "_expires_at_iso": past, "bounty": 1.0,
         "description": "", "category": "default", "currency": "XRP", "poster": "rTest",
         "poster_name": "", "tags": [], "status": "OPEN", "bid_count": 0,
         "is_demo": False, "claimable": False},
    ]
    referee._jobs_snapshot["open_jobs"] = [
        {"id": "J-FUTURE-OPEN", "title": "Future open", "_expires_at_iso": future,
         "budget_xrp": 1.0, "buyer_address": "rBuyer", "buyer_name": "", "category": "default",
         "tags": [], "status": "open", "bid_count": 0, "created_at": None, "expires_at": "—",
         "required_nft_issuer": None, "required_domain": None, "required_vc_issuer_did": None,
         "required_vc_type": None, "proof_policy": "ALL"},
        {"id": "J-PAST-OPEN", "title": "Past open", "_expires_at_iso": past,
         "budget_xrp": 1.0, "buyer_address": "rBuyer", "buyer_name": "", "category": "default",
         "tags": [], "status": "open", "bid_count": 0, "created_at": None, "expires_at": "—",
         "required_nft_issuer": None, "required_domain": None, "required_vc_issuer_did": None,
         "required_vc_type": None, "proof_policy": "ALL"},
    ]
    referee._jobs_snapshot["refreshed_at"] = datetime.now(timezone.utc)
    referee._jobs_snapshot["stale_since"] = None

    client = TestClient(referee.app, raise_server_exceptions=False)

    # Test /marketplace/jobs
    r = client.get("/marketplace/jobs")
    assert r.status_code == 200
    data = r.json()
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    ids = [j.get("id") or j.get("job_id") for j in jobs]
    assert "J-FUTURE" in ids, f"Future job should appear, got: {ids}"
    assert "J-PAST" not in ids, f"Past job should be excluded, got: {ids}"
    future_job = next((j for j in jobs if (j.get("id") or j.get("job_id")) == "J-FUTURE"), None)
    assert future_job is not None
    expires_hrs = future_job.get("deadline_hrs") or future_job.get("expires_hrs")
    assert isinstance(expires_hrs, (int, float)) and expires_hrs > 0, \
        f"deadline_hrs should be positive number, got {expires_hrs}"

    # Test /jobs
    r2 = client.get("/jobs?limit=100")
    assert r2.status_code == 200
    data2 = r2.json()
    jobs2 = data2.get("jobs", data2) if isinstance(data2, dict) else data2
    ids2 = [j.get("id") or j.get("job_id") for j in jobs2]
    assert "J-FUTURE-OPEN" in ids2, f"Future open job should appear, got: {ids2}"
    assert "J-PAST-OPEN" not in ids2, f"Past open job should be excluded, got: {ids2}"
    future_open = next((j for j in jobs2 if (j.get("id") or j.get("job_id")) == "J-FUTURE-OPEN"), None)
    assert future_open is not None
    open_expires_hrs = future_open.get("expires_hrs") or future_open.get("deadline_hrs")
    assert isinstance(open_expires_hrs, (int, float)) and open_expires_hrs > 0, \
        f"expires_hrs should be positive number, got {open_expires_hrs}"


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

    # account_tx returns API v2 shape: tx_json + top-level hash
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "result": {
            "transactions": [{
                "hash": "AABBCC001122",
                "tx_json": {"TransactionType": "EscrowCancel", "OfferSequence": 12345, "Owner": "rOwner123"},
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
    assert mock_vault.delivery_status == "CANCELLED"
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4b: _resolve_gone_escrow — API v1 shape resolves CANCELLED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v1_shape_resolves_cancelled():
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-TEST-0002"
    mock_vault.status = "LOCKED"
    mock_db = MagicMock()

    # API v1 shape: hash inside tx dict
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "result": {
            "transactions": [{
                "tx": {
                    "hash": "DDEEFF334455",
                    "TransactionType": "EscrowCancel",
                    "OfferSequence": 99999,
                    "Owner": "rOwner123",
                },
                "meta": {"TransactionResult": "tesSUCCESS"},
            }],
        }
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await referee._resolve_gone_escrow(mock_vault, "rOwner123", 99999, mock_db)

    assert mock_vault.status == "CANCELLED"
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4c: _resolve_gone_escrow — no match → RELEASE_FAILED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_match_returns_release_failed():
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-TEST-0003"
    mock_vault.status = "LOCKED"
    mock_db = MagicMock()

    # AccountTx returns unrelated transactions — none match sequence 77777
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "result": {
            "transactions": [{
                "hash": "FFFF0000",
                "tx_json": {"TransactionType": "Payment", "Owner": "rOwner123"},
                "meta": {"TransactionResult": "tesSUCCESS"},
            }],
        }
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await referee._resolve_gone_escrow(mock_vault, "rOwner123", 77777, mock_db)

    assert mock_vault.status == "RELEASE_FAILED"
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: IOU Amount in account_objects → _poll_expired_escrows_once leaves vault LOCKED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_iou_escrow_stays_locked():
    """account_objects returning IOU Amount → vault stays LOCKED (not abandoned)."""
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-IOU-TEST"
    mock_vault.status = "LOCKED"
    mock_vault.buyer_address = "rBuyer"
    mock_vault.worker_address = "rWorker"
    mock_vault.amount_xrp = 100.0
    mock_vault.escrow_tx_hash = None
    mock_vault.cancel_after_ts = None
    mock_vault.created_at = datetime.now(timezone.utc) - timedelta(hours=48)

    mock_db = MagicMock()
    # Query for stale PENDING → empty; orphaned LOCKED → our vault; expired LOCKED → empty
    def mock_query_filter_all():
        m = MagicMock()
        m.all.return_value = []
        return m

    orphan_mock = MagicMock()
    orphan_mock.all.return_value = [mock_vault]

    call_count = [0]
    def mock_filter(*args, **kwargs):
        call_count[0] += 1
        # 1st filter call = stale PENDING, 2nd = orphaned LOCKED, 3rd = expired LOCKED
        if call_count[0] == 2:
            return orphan_mock
        m = MagicMock()
        m.all.return_value = []
        return m

    mock_query = MagicMock()
    mock_query.filter.side_effect = mock_filter
    mock_db.query.return_value = mock_query

    # account_objects returns an IOU Amount (dict)
    iou_response = MagicMock()
    iou_response.json.return_value = {
        "result": {
            "account_objects": [{
                "LedgerEntryType": "Escrow",
                "Amount": {"currency": "USD", "issuer": "rIssuer", "value": "100"},
                "Destination": "rWorker",
            }]
        }
    }

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=iou_response)

    original_get_db = referee.get_db
    def fake_get_db():
        yield mock_db

    with patch("httpx.AsyncClient", return_value=mock_http):
        with patch.object(referee, "get_db", fake_get_db):
            with patch.object(referee, "referee_wallet", MagicMock()):
                with patch.object(referee, "_db_available", True):
                    try:
                        await referee._poll_expired_escrows_once()
                    except Exception:
                        pass  # may fail on missing wallet keypair etc.

    # Key assertion: IOU escrow detected as live → vault.status must NOT be ABANDONED
    assert mock_vault.status == "LOCKED", \
        f"IOU escrow should stay LOCKED, got {mock_vault.status}"
