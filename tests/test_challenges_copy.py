"""Tests that mcp_server.py challenge copy is accurate and free of stale wording."""
import os
import inspect


def _mcp_src():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp_server.py")
    with open(path) as f:
        return f.read()


def test_no_14_day_in_mcp_server():
    assert "14-day" not in _mcp_src(), "mcp_server.py must not say '14-day'"


def test_challenge_window_hrs_present():
    assert "challenge_window_hrs" in _mcp_src(), "mcp_server.py must reference challenge_window_hrs"


def test_no_buyer_approve():
    assert "buyer_approve" not in _mcp_src(), "mcp_server.py must not reference buyer_approve"


def test_no_human_arbitration():
    src = _mcp_src()
    assert "human arbitration" not in src.lower(), "mcp_server.py must not claim human arbitration"
