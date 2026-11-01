"""Tests for unified wallet verification and signed ratings (P1-1, P1-2)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import hashlib
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from xrpl.core.keypairs import generate_seed, derive_keypair, derive_classic_address, sign


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_db():
    """In-memory SQLite DB with the required tables."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wallet_verification (
                wallet_address VARCHAR PRIMARY KEY,
                verified_at    DATETIME,
                method         VARCHAR,
                tx_hash        VARCHAR
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS linked_wallet (
                id              INTEGER PRIMARY KEY,
                account_id      VARCHAR NOT NULL,
                xrpl_address    VARCHAR NOT NULL,
                label           VARCHAR,
                verified        BOOLEAN DEFAULT 0,
                verify_challenge VARCHAR,
                privy_wallet_id VARCHAR,
                privy_public_key VARCHAR,
                created_at      DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS enterprise_api_key (
                id          VARCHAR PRIMARY KEY,
                account_id  VARCHAR NOT NULL,
                key_hash    VARCHAR UNIQUE NOT NULL,
                label       VARCHAR,
                created_at  DATETIME,
                last_used_at DATETIME,
                revoked     BOOLEAN DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wallet_rating (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                rated_address  VARCHAR NOT NULL,
                rater_address  VARCHAR NOT NULL,
                escrow_id      VARCHAR NOT NULL,
                rating         INTEGER NOT NULL,
                comment        TEXT,
                rater_role     VARCHAR NOT NULL,
                created_at     DATETIME,
                UNIQUE(escrow_id, rater_address)
            )
        """))
        conn.commit()
    return Session


def _make_sig(escrow_id, rated_address, rating, private_key, public_key, ts=None):
    """Build x-rater-signature header."""
    if ts is None:
        ts = int(time.time())
    message = f"agenttrust-rate:{escrow_id}:{rated_address}:{rating}:{ts}"
    sig_hex = sign(message.encode(), private_key)
    return f"{sig_hex}:{ts}:{public_key}", ts


# ---------------------------------------------------------------------------
# P1-1 Tests
# ---------------------------------------------------------------------------

def test_record_wallet_verification_creates_row():
    """record_wallet_verification upserts a WalletVerification row."""
    import referee as ref
    Session = _make_db()
    db = Session()
    result = ref.record_wallet_verification(db, "rTestAddr1", "AABBCC", method="xrpl_memo")
    assert result is not None
    row = db.execute(text("SELECT * FROM wallet_verification WHERE wallet_address='rTestAddr1'")).fetchone()
    assert row is not None
    assert row[2] == "xrpl_memo"  # method column
    assert row[3] == "AABBCC"    # tx_hash column


def test_record_wallet_verification_idempotent():
    """Second call with same address is a no-op (returns existing row)."""
    import referee as ref
    Session = _make_db()
    db = Session()
    ref.record_wallet_verification(db, "rTestAddr2", "HASH1", method="xrpl_memo")
    ref.record_wallet_verification(db, "rTestAddr2", "HASH2", method="enterprise_accountset")
    rows = db.execute(text("SELECT * FROM wallet_verification WHERE wallet_address='rTestAddr2'")).fetchall()
    assert len(rows) == 1
    # First write wins — tx_hash stays HASH1
    assert rows[0][3] == "HASH1"


def test_backfill_creates_rows_for_verified_wallets():
    """_backfill_wallet_verifications creates WalletVerification for verified LinkedWallets."""
    import referee as ref

    Session = _make_db()
    # Patch SessionLocal
    with patch.object(ref, "SessionLocal", Session):
        db = Session()
        db.execute(text(
            "INSERT INTO linked_wallet (account_id, xrpl_address, verified) VALUES ('acc1', 'rWallet1', 1)"
        ))
        db.execute(text(
            "INSERT INTO linked_wallet (account_id, xrpl_address, verified) VALUES ('acc1', 'rWallet2', 0)"
        ))
        db.commit()
        db.close()

        ref._backfill_wallet_verifications()

        db2 = Session()
        rows = db2.execute(text("SELECT wallet_address, method FROM wallet_verification")).fetchall()
        addrs = {r[0] for r in rows}
        assert "rWallet1" in addrs
        assert "rWallet2" not in addrs
        assert all(r[1] == "enterprise_backfill" for r in rows)

        # Idempotent: second call adds no new rows
        ref._backfill_wallet_verifications()
        rows2 = db2.execute(text("SELECT wallet_address FROM wallet_verification")).fetchall()
        assert len(rows2) == 1


def test_backfill_idempotent():
    """Running _backfill_wallet_verifications twice does not duplicate rows."""
    import referee as ref
    Session = _make_db()
    with patch.object(ref, "SessionLocal", Session):
        db = Session()
        db.execute(text(
            "INSERT INTO linked_wallet (account_id, xrpl_address, verified) VALUES ('acc2', 'rWallet3', 1)"
        ))
        db.commit()
        db.close()
        ref._backfill_wallet_verifications()
        ref._backfill_wallet_verifications()
        db2 = Session()
        rows = db2.execute(text("SELECT * FROM wallet_verification WHERE wallet_address='rWallet3'")).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# P1-2 Tests: signed ratings
# ---------------------------------------------------------------------------

def _mock_db_with_escrow(buyer="rBuyer", worker="rWorker", status="RELEASED"):
    """Return a mock db where escrow_vault lookup returns the given row."""
    db = MagicMock()

    # Provide fetchone for escrow lookup
    escrow_row = MagicMock()
    escrow_row.__getitem__ = lambda self, i: [buyer, worker, status][i]

    # Provide fetchone for WalletVerification lookup
    vrow = MagicMock()
    vrow.wallet_address = worker

    # Chain: db.query(WalletVerification).filter(...).first() -> vrow
    def _query_side_effect(model):
        mock_q = MagicMock()
        mock_q.filter.return_value.first.return_value = vrow
        mock_q.filter_by.return_value.first.return_value = None
        return mock_q

    db.query.side_effect = _query_side_effect

    # db.execute(...).fetchone() for escrow
    exec_mock = MagicMock()
    exec_mock.fetchone.return_value = escrow_row
    db.execute.return_value = exec_mock

    return db


def test_rate_no_signature_returns_401():
    """POST /wallet/{address}/rate without any auth → 401."""
    import referee as ref
    from fastapi.testclient import TestClient

    client = TestClient(ref.app)
    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    rater = derive_classic_address(pub)

    resp = client.post(
        f"/wallet/rWorker123/rate",
        json={
            "escrow_id": "esc1",
            "rater_address": rater,
            "rating": 5,
        },
    )
    assert resp.status_code == 401


def test_rate_valid_signature_200(monkeypatch):
    """Valid x-rater-signature over the correct message → 200."""
    import referee as ref

    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    rater_addr = derive_classic_address(pub)  # rater is the buyer
    rated_addr = "rWorkerABCD"               # rated is the worker (URL address)
    escrow_id = "esc-valid-1"
    rating = 4

    ts = int(time.time())
    # message uses rated_addr (the address in the URL path)
    message = f"agenttrust-rate:{escrow_id}:{rated_addr}:{rating}:{ts}"
    sig_hex = sign(message.encode(), priv)
    sig_header = f"{sig_hex}:{ts}:{pub}"

    from fastapi.testclient import TestClient

    def _mock_get_db():
        mock = MagicMock()
        # escrow: buyer=rater_addr, worker=rated_addr, status=RELEASED
        escrow_row = MagicMock()
        escrow_row.__getitem__ = lambda s, i: [rater_addr, rated_addr, "RELEASED"][i]
        exec_mock = MagicMock()
        exec_mock.fetchone.return_value = escrow_row
        mock.execute.return_value = exec_mock

        # WalletVerification lookup for rater
        vrow_obj = MagicMock()
        vrow_obj.wallet_address = rater_addr

        def _q(model):
            qm = MagicMock()
            qm.filter.return_value.first.return_value = vrow_obj
            qm.filter_by.return_value.first.return_value = None
            return qm

        mock.query.side_effect = _q
        mock.commit = MagicMock()
        return mock

    from referee import get_db
    ref.app.dependency_overrides[get_db] = _mock_get_db
    try:
        ref._rater_sig_replay.clear()
        client = TestClient(ref.app)
        resp = client.post(
            f"/wallet/{rated_addr}/rate",
            json={"escrow_id": escrow_id, "rater_address": rater_addr, "rating": rating},
            headers={"x-rater-signature": sig_header},
        )
        assert resp.status_code == 200, resp.text
    finally:
        ref.app.dependency_overrides.pop(get_db, None)


def test_rate_replay_rejected(monkeypatch):
    """Same signature used twice → 409."""
    import referee as ref

    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    rater_addr = derive_classic_address(pub)  # rater = buyer
    rated_addr = "rWorkerReplay1"             # rated = worker (URL address)
    escrow_id = "esc-replay-1"
    rating = 3

    ts = int(time.time())
    sig_hex = sign(f"agenttrust-rate:{escrow_id}:{rated_addr}:{rating}:{ts}".encode(), priv)
    sig_header = f"{sig_hex}:{ts}:{pub}"

    vrow_obj = MagicMock()
    vrow_obj.wallet_address = rater_addr

    def _mock_get_db():
        mock = MagicMock()
        escrow_row = MagicMock()
        # buyer=rater_addr, worker=rated_addr so rater is buyer rating worker
        escrow_row.__getitem__ = lambda s, i: [rater_addr, rated_addr, "RELEASED"][i]
        exec_mock = MagicMock()
        exec_mock.fetchone.return_value = escrow_row
        mock.execute.return_value = exec_mock

        def _q(model):
            qm = MagicMock()
            qm.filter.return_value.first.return_value = vrow_obj
            qm.filter_by.return_value.first.return_value = None
            return qm
        mock.query.side_effect = _q
        mock.commit = MagicMock()
        return mock

    from referee import get_db
    ref.app.dependency_overrides[get_db] = _mock_get_db
    ref._rater_sig_replay.clear()
    try:
        client = TestClient(ref.app)
        r1 = client.post(
            f"/wallet/{rated_addr}/rate",
            json={"escrow_id": escrow_id, "rater_address": rater_addr, "rating": rating},
            headers={"x-rater-signature": sig_header},
        )
        assert r1.status_code == 200, r1.text

        # Second call with same sig — should replay-reject
        r2 = client.post(
            f"/wallet/{rated_addr}/rate",
            json={"escrow_id": escrow_id, "rater_address": rater_addr, "rating": rating},
            headers={"x-rater-signature": sig_header},
        )
        assert r2.status_code == 409, r2.text
    finally:
        ref.app.dependency_overrides.pop(get_db, None)


def test_rate_wrong_key_rejected():
    """Signature where pubkey derives to different address than rater_address → 401."""
    import referee as ref

    seed1 = generate_seed()
    pub1, priv1 = derive_keypair(seed1)
    addr1 = derive_classic_address(pub1)

    seed2 = generate_seed()
    pub2, priv2 = derive_keypair(seed2)
    addr2 = derive_classic_address(pub2)

    escrow_id = "esc-wrongkey-1"
    rated_addr = "rWorkerWrongKey"  # worker = rated address in URL
    rating = 2
    ts = int(time.time())

    # Sign with key1 but claim rater is addr2 (different key); addr2 = buyer
    sig_hex = sign(f"agenttrust-rate:{escrow_id}:{rated_addr}:{rating}:{ts}".encode(), priv1)
    sig_header = f"{sig_hex}:{ts}:{pub1}"  # pub1 → addr1, but we claim addr2

    def _mock_get_db():
        mock = MagicMock()
        escrow_row = MagicMock()
        # buyer=addr2, worker=rated_addr so addr2 rating worker is valid role
        escrow_row.__getitem__ = lambda s, i: [addr2, rated_addr, "RELEASED"][i]
        exec_mock = MagicMock()
        exec_mock.fetchone.return_value = escrow_row
        mock.execute.return_value = exec_mock

        def _q(model):
            qm = MagicMock()
            qm.filter.return_value.first.return_value = None
            qm.filter_by.return_value.first.return_value = None
            return qm
        mock.query.side_effect = _q
        mock.commit = MagicMock()
        return mock

    from referee import get_db
    ref.app.dependency_overrides[get_db] = _mock_get_db
    ref._rater_sig_replay.clear()
    try:
        client = TestClient(ref.app)
        resp = client.post(
            f"/wallet/{rated_addr}/rate",
            json={"escrow_id": escrow_id, "rater_address": addr2, "rating": rating},
            headers={"x-rater-signature": sig_header},
        )
        assert resp.status_code == 401, resp.text
    finally:
        ref.app.dependency_overrides.pop(get_db, None)


def test_rate_comment_too_long_422():
    """Comment > 280 chars → 422 from Pydantic validator."""
    import referee as ref
    from fastapi.testclient import TestClient

    client = TestClient(ref.app)
    long_comment = "x" * 281

    resp = client.post(
        "/wallet/rSomebody/rate",
        json={
            "escrow_id": "esc1",
            "rater_address": "rRater123",
            "rating": 3,
            "comment": long_comment,
        },
    )
    assert resp.status_code == 422


def test_rate_enterprise_api_key_200():
    """Enterprise x-api-key linked to verified rater wallet → 200."""
    import referee as ref

    seed = generate_seed()
    pub, priv = derive_keypair(seed)
    rater_addr = derive_classic_address(pub)   # rater = buyer
    rated_addr = "rWorkerEnterprise"           # rated = worker (URL address)
    api_key = "test-enterprise-key-xyz"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    account_id = "acc-enterprise-1"

    key_rec = MagicMock()
    key_rec.account_id = account_id
    key_rec.revoked = False

    lw = MagicMock()
    lw.xrpl_address = rater_addr
    lw.verified = True
    lw.account_id = account_id

    def _mock_get_db():
        mock = MagicMock()
        escrow_row = MagicMock()
        escrow_row.__getitem__ = lambda s, i: [rater_addr, rated_addr, "RELEASED"][i]
        exec_mock = MagicMock()
        exec_mock.fetchone.return_value = escrow_row
        mock.execute.return_value = exec_mock

        def _q(model):
            qm = MagicMock()
            # EnterpriseApiKey lookup
            if hasattr(model, "__tablename__") and model.__tablename__ == "enterprise_api_key":
                qm.filter.return_value.first.return_value = key_rec
            # LinkedWallet lookup
            elif hasattr(model, "__tablename__") and model.__tablename__ == "linked_wallet":
                qm.filter.return_value.first.return_value = lw
            else:
                qm.filter.return_value.first.return_value = None
            qm.filter_by.return_value.first.return_value = None
            return qm
        mock.query.side_effect = _q
        mock.commit = MagicMock()
        return mock

    from referee import get_db
    ref.app.dependency_overrides[get_db] = _mock_get_db
    try:
        client = TestClient(ref.app)
        resp = client.post(
            f"/wallet/{rated_addr}/rate",
            json={"escrow_id": "esc-ent-1", "rater_address": rater_addr, "rating": 5},
            headers={"x-api-key": api_key},
        )
        assert resp.status_code == 200, resp.text
    finally:
        ref.app.dependency_overrides.pop(get_db, None)
