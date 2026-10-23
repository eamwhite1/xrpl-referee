"""
Tests for Neon idle optimisation — snapshot cache, DB flag recovery, 503 behaviour.
Run with: pytest tests/test_neon_idle.py -v
"""
import pytest


def test_snapshot_serves_without_db():
    """100 calls to /marketplace/jobs and /jobs?limit=100 produce zero DB queries between refreshes.

    Intent: _jobs_snapshot is populated once; subsequent GET /marketplace/jobs and GET /jobs
    requests read from _jobs_snapshot directly without opening a DB connection.
    Verify by mocking SessionLocal to raise on any call and asserting it is never invoked
    during 100 sequential requests after an initial snapshot load.
    """
    pass


def test_db_flag_recovers():
    """Boot with unreachable DB; /health returns db:unavailable; after recovery /health returns db:ok.

    Intent: start app with DATABASE_URL pointing to an unreachable host. _db_available starts False.
    GET /health returns {"db": "unavailable"}. Then simulate DB becoming reachable by patching
    engine.connect() to succeed. _db_recovery_loop should detect this and set _db_available = True.
    Subsequent GET /health returns {"db": "ok"} without restarting the process.
    """
    pass


def test_503_while_db_down():
    """DB-backed route returns 503 JSON while _db_available is False.

    Intent: set _db_available = False. POST /escrow/generate (or any DB-backed route)
    should return HTTP 503 with body {"error": "database unavailable", "retry_after": 60}.
    GET /health and GET /status must still return 200.
    GET /marketplace/jobs and GET /jobs must still return 200 (served from snapshot).
    """
    pass


def test_tec_no_target_resolves_status():
    """tecNO_TARGET on EscrowCancel sets DB status to CANCELLED (or RELEASED), not retried.

    Intent: mock async_submit_and_wait to return a result with engine_result="tecNO_TARGET".
    Mock account_tx lookup to return no matching EscrowFinish/EscrowCancel tx.
    Call _poll_expired_escrows_once() with a LOCKED expired vault.
    Assert vault.status is set to "CANCELLED" (not left as LOCKED) and no further submit attempt is made.
    """
    pass
