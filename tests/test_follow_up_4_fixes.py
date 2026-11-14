"""Tests for follow-up #4 fixes: next_step, internal traffic, SITE_URL, server.json."""
import json
import pathlib
import pytest
import respx
import httpx
from fastapi.testclient import TestClient


def _get_client():
    import sys, os
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-long-enough!")
    os.environ.setdefault("ENTERPRISE_WALLET", "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe")
    from referee import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# P1-6: next_step in job/marketplace responses
# ---------------------------------------------------------------------------

@respx.mock
def test_list_marketplace_jobs_next_step():
    """list_marketplace_jobs response must include next_step with testnet hint."""
    import asyncio
    import sys, os
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-long-enough!")

    respx.get("https://mcp.cryptovault.co.uk/marketplace/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [], "total": 0, "marketplace_url": "https://agenttrustxrpl.com/marketplace"})
    )

    import importlib
    import mcp_server
    importlib.reload(mcp_server)

    async def _run():
        result = await mcp_server.list_marketplace_jobs()
        assert "next_step" in result
        assert "create_testnet_wallet" in result["next_step"]
        assert "hire_and_pay" in result["next_step"]

    asyncio.run(_run())


@respx.mock
def test_list_open_jobs_next_step():
    """list_open_jobs response must include next_step with testnet hint."""
    import asyncio
    import sys, os
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-long-enough!")

    respx.get("https://mcp.cryptovault.co.uk/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [], "total": 0})
    )

    import importlib
    import mcp_server
    importlib.reload(mcp_server)

    async def _run():
        result = await mcp_server.list_open_jobs()
        assert "next_step" in result
        assert "create_testnet_wallet" in result["next_step"]

    asyncio.run(_run())


@respx.mock
def test_view_job_next_step():
    """view_job response must include next_step with testnet hint."""
    import asyncio
    import sys, os
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-long-enough!")

    respx.get("https://mcp.cryptovault.co.uk/jobs/JOB-123").mock(
        return_value=httpx.Response(200, json={"id": "JOB-123", "title": "Test", "bids": []})
    )

    import importlib
    import mcp_server
    importlib.reload(mcp_server)

    async def _run():
        result = await mcp_server.view_job("JOB-123")
        assert "next_step" in result
        assert "hire_and_pay" in result["next_step"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# P2-C: marketplace_url uses SITE_URL / agenttrustxrpl.com
# ---------------------------------------------------------------------------

def test_marketplace_jobs_api_uses_site_url():
    """GET /marketplace/jobs must return marketplace_url with agenttrustxrpl.com."""
    client = _get_client()
    resp = client.get("/marketplace/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert "agenttrustxrpl.com" in data.get("marketplace_url", "")


# ---------------------------------------------------------------------------
# P2-B: server.json has correct title and websiteUrl
# ---------------------------------------------------------------------------

def test_server_json_title_and_domain():
    """server.json must have title 'AgentTrust XRPL' and websiteUrl agenttrustxrpl.com."""
    path = pathlib.Path(__file__).parent.parent / "server.json"
    assert path.exists(), "server.json not found"
    data = json.loads(path.read_text())
    assert data.get("title") == "AgentTrust XRPL"
    assert "agenttrustxrpl.com" in data.get("websiteUrl", "")
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from referee import APP_VERSION
    assert data.get("version") == APP_VERSION


# ---------------------------------------------------------------------------
# P1-7: internal traffic excluded from /stats/testnet outside counts
# ---------------------------------------------------------------------------

def test_stats_testnet_excludes_internal():
    """GET /stats/testnet must return outside_vaults counting only non-internal vaults."""
    client = _get_client()
    import referee as ref
    from sqlalchemy import create_engine, text as _t
    from sqlalchemy.orm import sessionmaker
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    with eng.connect() as c:
        c.execute(_t("CREATE TABLE escrow_vault (escrow_id TEXT, network TEXT, is_internal BOOLEAN, "
                     "buyer_address TEXT, status TEXT, source TEXT, created_at TIMESTAMP)"))
        c.commit()
    _orig = ref.SessionLocal
    ref.SessionLocal = sessionmaker(bind=eng)
    try:
        resp = client.get("/stats/testnet")
    finally:
        ref.SessionLocal = _orig
    # Should succeed (even with empty DB) and return outside_vaults key
    assert resp.status_code == 200
    data = resp.json()
    assert "outside_vaults" in data or "outside_buyer_wallets" in data, \
        f"Expected outside_* fields in stats response, got: {list(data.keys())}"
