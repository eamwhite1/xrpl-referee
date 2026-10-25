"""Test that trust score signals_list points sum to the total score."""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import AsyncMock, patch, MagicMock


def _make_mock_client(mock_tx_age, mock_tx_activity):
    mock_info = {
        "result": {
            "account_data": {
                "Account": "rTest123", "Balance": "50000000", "OwnerCount": 10,
                "Sequence": 100
            },
            "ledger_current_index": 90000000, "status": "success"
        }
    }

    async def fake_post(url, **kwargs):
        method = (kwargs.get("json") or {}).get("method", "")
        resp = MagicMock()
        resp.status_code = 200
        if method == "account_info":
            resp.json.return_value = mock_info
        elif method == "account_tx":
            body = (kwargs.get("json") or {}).get("params", [{}])[0]
            if body.get("forward"):
                resp.json.return_value = mock_tx_age
            else:
                resp.json.return_value = mock_tx_activity
        else:
            resp.json.return_value = {"result": {}}
        return resp

    async def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = ""
        return resp

    mock_client = AsyncMock()
    mock_client.post.side_effect = fake_post
    mock_client.get.side_effect = fake_get
    return mock_client


def test_signals_sum_equals_total():
    """signals_list points must sum to the total score (excluding info-only cards)."""
    import referee as ref

    mock_tx_age = {"result": {"transactions": [{"tx_json": {"date": 700000000}}]}}
    mock_tx_activity = {"result": {"transactions": [], "marker": None}}
    mock_client = _make_mock_client(mock_tx_age, mock_tx_activity)

    async def nft_zero(*a, **kw):
        return 0

    async def xrpscan_none(*a, **kw):
        return None

    async def sanctions_clear(*a, **kw):
        return (False, {})

    async def bithomp_none(*a, **kw):
        return None

    with patch("referee.httpx.AsyncClient") as mock_cls, \
         patch("referee._fetch_nft_count", nft_zero), \
         patch("referee._get_xrpscan_account", xrpscan_none), \
         patch("referee.is_wallet_sanctioned", sanctions_clear), \
         patch("referee._verify_domain_via_bithomp", bithomp_none):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = asyncio.run(ref.compute_xrpl_trust_score("rTest123"))

    assert "signals_list" in result, f"signals_list missing from result: {result}"
    sl = result["signals_list"]
    scored = [s for s in sl if s["label"] != "Ledger objects owned"]
    points_sum = sum(s["points"] for s in scored)
    assert points_sum == result["score"], \
        f"Points sum {points_sum} != score {result['score']}"


def test_signals_list_has_correct_labels():
    """signals_list must contain all 12 expected labels."""
    import referee as ref

    mock_tx_age = {"result": {"transactions": []}}
    mock_tx_activity = {"result": {"transactions": [], "marker": None}}
    mock_info = {
        "result": {
            "account_data": {"Account": "rTest456", "Balance": "10000000", "OwnerCount": 0},
            "ledger_current_index": 90000000, "status": "success"
        }
    }

    async def fake_post(url, **kwargs):
        method = (kwargs.get("json") or {}).get("method", "")
        resp = MagicMock()
        resp.status_code = 200
        if method == "account_info":
            resp.json.return_value = mock_info
        else:
            resp.json.return_value = {"result": {"transactions": []}}
        return resp

    async def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = ""
        return resp

    mock_client = AsyncMock()
    mock_client.post.side_effect = fake_post
    mock_client.get.side_effect = fake_get

    async def nft_zero(*a, **kw):
        return 0

    async def xrpscan_none(*a, **kw):
        return None

    async def sanctions_clear(*a, **kw):
        return (False, {})

    async def bithomp_none(*a, **kw):
        return None

    with patch("referee.httpx.AsyncClient") as mock_cls, \
         patch("referee._fetch_nft_count", nft_zero), \
         patch("referee._get_xrpscan_account", xrpscan_none), \
         patch("referee.is_wallet_sanctioned", sanctions_clear), \
         patch("referee._verify_domain_via_bithomp", bithomp_none):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = asyncio.run(ref.compute_xrpl_trust_score("rTest456"))

    expected_labels = {
        "Wallet age", "Balance", "Activity (90 days)", "Domain verified",
        "NFTs", "Escrow history", "Peer ratings", "Wallet signature verified",
        "Sanctions clear", "Identity (KYC)", "XRPScan entity + security flags",
        "Ledger objects owned",
    }
    actual_labels = {s["label"] for s in result["signals_list"]}
    assert actual_labels == expected_labels, f"Label mismatch: {actual_labels ^ expected_labels}"
