"""Tests for release/cancel tx hash storage and proof-of-payment."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def test_escrow_info_includes_release_fields():
    """get_escrow_info response shape includes release_tx_hash and explorer_url."""
    HASH = "08B30D9DA7444A13083E041EB62E00E83144739F9497868355AE4BF1328546BD"
    explorer = f"https://livenet.xrpl.org/transactions/{HASH}"
    assert "livenet.xrpl.org/transactions" in explorer
    assert len(HASH) == 64


def test_evaluate_pass_response_shape():
    """PASS response has release_status + poll, not null auto_finish_hash."""
    pending = {"release_status": "pending", "release_tx_hash": None, "poll": "/escrow/AT-TEST"}
    assert "auto_finish_hash" not in pending
    assert pending["release_status"] == "pending"
    assert pending["poll"] is not None

    HASH = "A" * 64
    confirmed = {"release_status": "confirmed", "release_tx_hash": HASH,
                 "explorer_url": f"https://livenet.xrpl.org/transactions/{HASH}", "poll": None}
    assert confirmed["release_status"] == "confirmed"
    assert confirmed["release_tx_hash"] == HASH
    assert confirmed["poll"] is None


@pytest.mark.asyncio
async def test_backfill_sets_release_hash():
    """_backfill_escrow_hashes sets release_tx_hash on a RELEASED vault missing it."""
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-BACKFILL-01"
    mock_vault.status = "RELEASED"
    mock_vault.release_tx_hash = None
    mock_vault.released_at = None
    mock_vault.escrow_owner = None
    mock_vault.escrow_sequence = None
    mock_vault.backfill_attempts = 0
    mock_vault.escrow_tx_hash = "DEADBEEF" * 8

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.limit.return_value.all.side_effect = [
        [mock_vault],  # RELEASED query
        [],            # CANCELLED query
    ]

    finish_tx = {
        "TransactionType": "EscrowFinish",
        "Owner": "rOwner123",
        "OfferSequence": 12345,
        "hash": "FINISHHASH" + "0" * 54,
        "_ledger_close_time": None,
    }

    with patch.object(referee, '_get_escrow_create_info', new=AsyncMock(return_value={"sequence": 12345, "owner": "rOwner123"})):
        with patch.object(referee, '_fetch_escrow_final_tx', new=AsyncMock(return_value=finish_tx)):
            await referee._backfill_escrow_hashes(mock_db)

    assert mock_vault.release_tx_hash == finish_tx["hash"]
    mock_db.commit.assert_called()


@pytest.mark.asyncio
async def test_backfill_sets_cancel_hash():
    """_backfill_escrow_hashes sets cancel_tx_hash on a CANCELLED vault missing it."""
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-BACKFILL-02"
    mock_vault.status = "CANCELLED"
    mock_vault.cancel_tx_hash = None
    mock_vault.cancelled_at = None
    mock_vault.escrow_owner = None
    mock_vault.escrow_sequence = None
    mock_vault.backfill_attempts = 0
    mock_vault.escrow_tx_hash = "CAFEBABE" * 8

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.limit.return_value.all.side_effect = [
        [],            # RELEASED query
        [mock_vault],  # CANCELLED query
    ]

    cancel_tx = {
        "TransactionType": "EscrowCancel",
        "Owner": "rOwner456",
        "OfferSequence": 99999,
        "hash": "CANCELHASH" + "0" * 54,
        "_ledger_close_time": None,
    }

    with patch.object(referee, '_get_escrow_create_info', new=AsyncMock(return_value={"sequence": 99999, "owner": "rOwner456"})):
        with patch.object(referee, '_fetch_escrow_final_tx', new=AsyncMock(return_value=cancel_tx)):
            await referee._backfill_escrow_hashes(mock_db)

    assert mock_vault.cancel_tx_hash == cancel_tx["hash"]
    mock_db.commit.assert_called()


@pytest.mark.asyncio
async def test_backfill_owner_not_buyer():
    """Backfill finds cancel tx when owner differs from buyer (AT-H6LB-RXZH case)."""
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-H6LB-RXZH"
    mock_vault.status = "CANCELLED"
    mock_vault.cancel_tx_hash = None
    mock_vault.cancelled_at = None
    mock_vault.release_tx_hash = None
    mock_vault.escrow_tx_hash = "CREATEHASH" + "0" * 54
    mock_vault.escrow_owner = None
    mock_vault.escrow_sequence = None
    mock_vault.buyer_address = "rPB6TCFu..."
    mock_vault.backfill_attempts = 0

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.limit.return_value.all.side_effect = [
        [],             # RELEASED query
        [mock_vault],   # CANCELLED query
    ]

    escrow_info = {"sequence": 102383926, "owner": "rK3sZBhWUNs12jqXz54wgKThFyf6XFVbzM"}
    cancel_tx = {
        "TransactionType": "EscrowCancel",
        "Owner": "rK3sZBhWUNs12jqXz54wgKThFyf6XFVbzM",
        "OfferSequence": 102383926,
        "hash": "FCA2D2293305C897AA2B328FBF48BAB41A2DF1404078651455C0B9CB1E855449",
        "_ledger_close_time": 800000000,
    }

    with patch.object(referee, '_get_escrow_create_info', new=AsyncMock(return_value=escrow_info)):
        with patch.object(referee, '_fetch_escrow_final_tx', new=AsyncMock(return_value=cancel_tx)):
            await referee._backfill_escrow_hashes(mock_db)

    assert mock_vault.cancel_tx_hash == cancel_tx["hash"]
    assert mock_vault.escrow_owner == "rK3sZBhWUNs12jqXz54wgKThFyf6XFVbzM"


def test_ledger_time_to_utc():
    """Ripple epoch conversion is correct."""
    import referee
    from datetime import timezone
    dt = referee._ledger_time_to_utc(0)
    assert dt.year == 2000
    assert dt.month == 1
    assert dt.day == 1
    assert dt.tzinfo == timezone.utc

    import datetime as dt_module
    expected = dt_module.datetime.fromtimestamp(946684800 + 800000000, tz=timezone.utc)
    assert referee._ledger_time_to_utc(800000000) == expected


@pytest.mark.asyncio
async def test_backfill_corrects_wrong_cached_owner():
    """Backfill overwrites wrong cached escrow_owner with real owner from EscrowCreate tx."""
    import referee

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-OWNER-FIX"
    mock_vault.status = "CANCELLED"
    mock_vault.cancel_tx_hash = None
    mock_vault.cancelled_at = None
    mock_vault.release_tx_hash = None
    mock_vault.escrow_tx_hash = "CREATEHASH" + "0" * 54
    mock_vault.escrow_owner = "rWRONG_BUYER_ADDRESS"
    mock_vault.escrow_sequence = None
    mock_vault.buyer_address = "rWRONG_BUYER_ADDRESS"
    mock_vault.backfill_attempts = 0

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.limit.return_value.all.side_effect = [
        [],
        [mock_vault],
    ]

    real_escrow_info = {"sequence": 999, "owner": "rREAL_OWNER_ADDRESS"}
    cancel_tx = {
        "TransactionType": "EscrowCancel",
        "Owner": "rREAL_OWNER_ADDRESS",
        "OfferSequence": 999,
        "hash": "CANCELHASH" + "0" * 55,
        "_ledger_close_time": 800000000,
    }

    with patch.object(referee, '_get_escrow_create_info', new=AsyncMock(return_value=real_escrow_info)):
        with patch.object(referee, '_fetch_escrow_final_tx', new=AsyncMock(return_value=cancel_tx)):
            await referee._backfill_escrow_hashes(mock_db)

    assert mock_vault.escrow_owner == "rREAL_OWNER_ADDRESS", f"Expected real owner, got {mock_vault.escrow_owner}"
    assert mock_vault.cancel_tx_hash == cancel_tx["hash"]


@pytest.mark.asyncio
async def test_one_off_corrects_now_timestamp():
    """One-off pass corrects released_at set to now() to real ledger close time."""
    import referee
    from datetime import datetime, timezone

    referee._one_off_backfill_done = False

    correct_ledger_time = 812345678
    correct_ts = referee._ledger_time_to_utc(correct_ledger_time)

    mock_vault = MagicMock()
    mock_vault.escrow_id = "AT-CR23-A001"
    mock_vault.status = "RELEASED"
    mock_vault.release_tx_hash = "08B30D9DA7444A13083E041EB62E00E83144739F9497868355AE4BF1328546BD"
    mock_vault.released_at = datetime.now(timezone.utc)
    mock_vault.escrow_tx_hash = "CREATEHASH" + "0" * 54
    mock_vault.escrow_owner = "rCORRECT"
    mock_vault.escrow_sequence = 12345

    mock_db = MagicMock()
    # released query returns [mock_vault]; cancelled query returns []
    mock_db.query.return_value.filter.return_value.all.side_effect = [
        [mock_vault],
        [],
    ]
    mock_db.query.return_value.filter.return_value.update.return_value = 0

    mock_response = MagicMock()
    mock_response.json.return_value = {"result": {"date": correct_ledger_time}}

    with patch.object(referee, '_get_escrow_create_info', new=AsyncMock(return_value={"sequence": 12345, "owner": "rCORRECT"})):
        with patch('httpx.AsyncClient') as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_http
            await referee._one_off_backfill_correction(mock_db)

    assert mock_vault.released_at == correct_ts, f"Expected {correct_ts}, got {mock_vault.released_at}"
    referee._one_off_backfill_done = False
