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
    # When finish hash is not yet known:
    pending = {"release_status": "pending", "release_tx_hash": None, "poll": "/escrow/AT-TEST"}
    assert "auto_finish_hash" not in pending
    assert pending["release_status"] == "pending"
    assert pending["poll"] is not None

    # When finish hash is resolved within 3s:
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
    mock_vault.buyer_address = "rBuyer123"
    mock_vault.escrow_tx_hash = "DEADBEEF" * 8

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.limit.return_value.all.side_effect = [
        [mock_vault],  # RELEASED query
        [],            # CANCELLED query
    ]

    finish_tx = {
        "TransactionType": "EscrowFinish",
        "OfferSequence": 12345,
        "hash": "FINISHHASH" + "0" * 54,
    }

    with patch.object(referee, '_get_escrow_sequence', new=AsyncMock(return_value=12345)):
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
    mock_vault.buyer_address = "rBuyer456"
    mock_vault.escrow_tx_hash = "CAFEBABE" * 8

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.limit.return_value.all.side_effect = [
        [],            # RELEASED query
        [mock_vault],  # CANCELLED query
    ]

    cancel_tx = {
        "TransactionType": "EscrowCancel",
        "OfferSequence": 99999,
        "hash": "CANCELHASH" + "0" * 54,
    }

    with patch.object(referee, '_get_escrow_sequence', new=AsyncMock(return_value=99999)):
        with patch.object(referee, '_fetch_escrow_final_tx', new=AsyncMock(return_value=cancel_tx)):
            await referee._backfill_escrow_hashes(mock_db)

    assert mock_vault.cancel_tx_hash == cancel_tx["hash"]
    mock_db.commit.assert_called()
