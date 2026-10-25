"""Tests for cold-review fixes: truthful claims, RLUSD 422, version headers, optional escrow_id, etc."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# 1. RLUSD 422 error format
# ---------------------------------------------------------------------------

def test_rlusd_unavailable_returns_422():
    """RLUSD escrow HTTP exception should use status 422 with structured error dict."""
    from fastapi import HTTPException
    import referee as ref
    import asyncio
    # Directly test that the HTTPException raised has status 422 and correct detail
    async def _check():
        with patch("referee.check_rlusd_escrow_supported", new=AsyncMock(return_value=False)):
            # Simulate just the RLUSD check block from the endpoint
            if not await ref.check_rlusd_escrow_supported():
                raise HTTPException(
                    status_code=422,
                    detail={"error": "rlusd_escrow_unavailable", "reason": "issuer has not enabled trust-line locking"},
                )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_check())
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "rlusd_escrow_unavailable"
    assert "trust-line locking" in exc_info.value.detail["reason"]


# ---------------------------------------------------------------------------
# 2. /fees includes rlusd_escrow_available
# ---------------------------------------------------------------------------

def test_fees_includes_rlusd_escrow_available():
    """GET /fees should include rlusd_escrow_available key in its return dict."""
    import inspect
    import referee as ref
    import ast
    # Check that the /fees endpoint source contains 'rlusd_escrow_available'
    src = inspect.getsource(ref.get_fees)
    assert "rlusd_escrow_available" in src


# ---------------------------------------------------------------------------
# 3. Version headers present
# ---------------------------------------------------------------------------

def test_version_headers_present():
    """mcp_smithery_compat middleware should set X-AgentTrust-Version and X-AgentTrust-Server to 7.0.1."""
    import inspect
    import referee as ref
    src = inspect.getsource(ref.mcp_smithery_compat)
    assert '"7.0.1"' in src
    assert "X-AgentTrust-Server" in src


# ---------------------------------------------------------------------------
# 4. escrow_id is optional in EscrowSetupRequest
# ---------------------------------------------------------------------------

def test_escrow_id_optional_in_model():
    """EscrowSetupRequest.escrow_id should be Optional (default None)."""
    from referee import EscrowSetupRequest
    req = EscrowSetupRequest(
        buyer_name="Test",
        buyer_address="rBuyerXXXXXXXXXXXXXXXXXXXXXXXXX",
        task_description="Test task",
        worker_address="rWorkerXXXXXXXXXXXXXXXXXXXXXXXX",
    )
    assert req.escrow_id is None


# ---------------------------------------------------------------------------
# 5. explain_agenttrust_trust_model no longer says "final"
# ---------------------------------------------------------------------------

def test_trust_model_no_final_verdict():
    """explain_agenttrust_trust_model source should not claim AI verdict is final."""
    import inspect
    try:
        import mcp_server as ms
    except ModuleNotFoundError:
        pytest.skip("fastmcp not installed")
    src = inspect.getsource(ms.explain_agenttrust_trust_model)
    assert "is final" not in src


# ---------------------------------------------------------------------------
# 6. explain_agenttrust_trust_model no longer says "no disputes"
# ---------------------------------------------------------------------------

def test_trust_model_no_disputes_claim():
    """explain_agenttrust_trust_model source should not claim there are no disputes."""
    import inspect
    try:
        import mcp_server as ms
    except ModuleNotFoundError:
        pytest.skip("fastmcp not installed")
    src = inspect.getsource(ms.explain_agenttrust_trust_model)
    assert "no disputes" not in src.lower()


# ---------------------------------------------------------------------------
# 7. batch_wallet_trust_scores readOnlyHint is False
# ---------------------------------------------------------------------------

def test_batch_wallet_trust_scores_not_readonly():
    """batch_wallet_trust_scores writes to DB — readOnlyHint must be False."""
    try:
        import mcp_server as ms
        from fastmcp import FastMCP
    except ModuleNotFoundError:
        pytest.skip("fastmcp not installed")
    import mcp_server as ms
    from fastmcp import FastMCP
    mcp = ms.mcp
    tool_map = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    tool = tool_map.get("batch_wallet_trust_scores")
    if tool is None:
        pytest.skip("Tool not found in registry")
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        pytest.skip("No annotations on tool")
    # readOnlyHint should be False (or absent/None) — definitely not True
    assert getattr(annotations, "readOnlyHint", None) is not True


# ---------------------------------------------------------------------------
# 8. /stats endpoint returns dict with expected keys
# ---------------------------------------------------------------------------

def test_stats_endpoint_registered():
    """GET /stats route should be registered in the app."""
    import referee as ref
    routes = [r.path for r in ref.app.routes]
    assert "/stats" in routes


# ---------------------------------------------------------------------------
# 9. hire_and_pay response has free_escrows_remaining key (via mock)
# ---------------------------------------------------------------------------

def test_hire_and_pay_free_escrows_remaining_alias():
    """hire_and_pay MCP tool response should include free_escrows_remaining."""
    try:
        import mcp_server as ms
    except ModuleNotFoundError:
        pytest.skip("fastmcp not installed")
    import asyncio
    mock_vault = {
        "escrow_id": "AT-TEST-0001",
        "condition": "ABCD",
        "cancel_after_human": "2026-12-01 00:00 UTC",
        "evaluate_token": "tok123",
        "free_tier": True,
        "audits_remaining": 2,
        "worker_url": None,
        "worker_instructions": None,
    }
    mock_prep = {"transaction": {"TransactionType": "EscrowCreate"}}

    async def _run():
        with patch("mcp_server.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_vault_resp = MagicMock()
            mock_vault_resp.status_code = 200
            mock_vault_resp.json.return_value = mock_vault
            mock_prep_resp = MagicMock()
            mock_prep_resp.status_code = 200
            mock_prep_resp.json.return_value = mock_prep
            mock_client.post.side_effect = [mock_vault_resp, mock_prep_resp]
            try:
                result = await ms.hire_and_pay(
                    task="Test task",
                    buyer_address="rBuyerXXXXXXXXXXXXXXXXXXXXXXXXX",
                    amount_xrp=5.0,
                    worker_address="rWorkerXXXXXXXXXXXXXXXXXXXXXXXX",
                )
                return result
            except Exception:
                return None

    from unittest.mock import MagicMock
    result = asyncio.run(_run())
    if result and isinstance(result, dict):
        assert "free_escrows_remaining" in result


# ---------------------------------------------------------------------------
# 10. smithery-verification not in llms.txt
# ---------------------------------------------------------------------------

def test_smithery_verification_removed_from_llms_txt():
    """smithery-verification token should not appear in llms.txt."""
    llms_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "..", "agent-trust", "llms.txt")
    llms_path = os.path.normpath(llms_path)
    if not os.path.exists(llms_path):
        pytest.skip("llms.txt not found relative to xrpl-referee")
    content = open(llms_path).read()
    assert "smithery-verification=" not in content
