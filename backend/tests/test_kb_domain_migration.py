"""One-shot migration: kb_keys/kb_grants → kb_domains/kb_domain_dbs/kb_domain_grants.

The legacy shape (one credential per physical database) must land as one
PRIVATE domain per credential holding exactly that database, with the roster
carried over — active rows active, revoked rows still revoked — so the
effective whitelist on the first boot after upgrade is bit-for-bit the old one.
The migration is gated on the legacy table existing, which makes re-running it
(startup happens many times) a no-op.
"""
import pytest
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401  (register tables on Base.metadata)
from app import crypto
from app.database import Base, _migrate_kb_domains


@pytest.fixture
def legacy_engine(tmp_path):
    """A sync SQLite database carrying legacy kb_keys/kb_grants rows."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE kb_keys ("
            "kb_name VARCHAR(64) PRIMARY KEY, api_key_enc TEXT, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE kb_grants ("
            "user_id VARCHAR(100), kb_name VARCHAR(64), "
            "created_at DATETIME, revoked_at DATETIME, "
            "PRIMARY KEY (user_id, kb_name))"
        ))
        enc = crypto.encrypt_secret("sk-legacy")
        conn.execute(
            text("INSERT INTO kb_keys (kb_name, api_key_enc) VALUES (:n, :e)"),
            [{"n": "fastdb", "e": enc}, {"n": "hr_only", "e": enc}],
        )
        conn.execute(
            text("INSERT INTO kb_grants (user_id, kb_name, revoked_at) "
                 "VALUES (:u, :k, :r)"),
            [
                {"u": "u1", "k": "fastdb", "r": None},          # active
                {"u": "u2", "k": "fastdb", "r": "2025-01-01"},   # revoked
                {"u": "u1", "k": "hr_only", "r": None},          # active
                {"u": "u9", "k": "ghost_db", "r": None},         # orphan (no key)
            ],
        )
        # New tables — as create_all would build them on startup.
        Base.metadata.create_all(conn)
    yield engine
    engine.dispose()


def test_migration_moves_keys_dbs_and_grants(legacy_engine):
    with legacy_engine.begin() as conn:
        _migrate_kb_domains(conn)

    with legacy_engine.begin() as conn:
        domains = conn.execute(text(
            "SELECT id, name, description, key_type, api_key_enc FROM kb_domains"
        )).fetchall()
        dbs = conn.execute(text(
            "SELECT domain_id, kb_name FROM kb_domain_dbs"
        )).fetchall()
        grants = conn.execute(text(
            "SELECT user_id, domain_id, revoked_at FROM kb_domain_grants"
        )).fetchall()

    # One private domain per legacy credential, named after its database,
    # with the encrypted key carried over verbatim (no re-encryption needed).
    assert {d[1] for d in domains} == {"fastdb", "hr_only"}
    assert all(d[3] == "private" for d in domains)
    assert all(d[2] == "" for d in domains)
    domain_ids = {d[1]: d[0] for d in domains}
    for d in domains:
        assert crypto.decrypt_secret(d[4]) == "sk-legacy"

    # Each domain holds exactly its own database.
    assert {(r[1], r[0]) for r in dbs} == {
        ("fastdb", domain_ids["fastdb"]),
        ("hr_only", domain_ids["hr_only"]),
    }

    # Roster: active stays active (revoked_at NULL), revoked stays revoked.
    by_user_domain = {(r[0], r[1]): r[2] for r in grants}
    assert by_user_domain[("u1", domain_ids["fastdb"])] is None
    assert by_user_domain[("u1", domain_ids["hr_only"])] is None
    assert by_user_domain[("u2", domain_ids["fastdb"])] is not None
    # The orphan grant points at no migrated domain — it is dropped, not invented.
    assert all(uid != "u9" for uid, _ in by_user_domain)


def test_migration_drops_the_legacy_tables(legacy_engine):
    with legacy_engine.begin() as conn:
        _migrate_kb_domains(conn)
    tables = set(inspect(legacy_engine).get_table_names())
    assert "kb_keys" not in tables
    assert "kb_grants" not in tables
    assert {"kb_domains", "kb_domain_dbs", "kb_domain_grants"} <= tables


def test_migration_is_idempotent(legacy_engine):
    with legacy_engine.begin() as conn:
        _migrate_kb_domains(conn)
    # Second startup: the legacy tables are gone, so the gate short-circuits
    # and nothing is duplicated or dropped.
    with legacy_engine.begin() as conn:
        _migrate_kb_domains(conn)
        count = conn.execute(text("SELECT COUNT(*) FROM kb_domains")).scalar_one()
        grant_count = conn.execute(text("SELECT COUNT(*) FROM kb_domain_grants")).scalar_one()
    assert count == 2
    assert grant_count == 3


def test_migration_noop_on_a_fresh_database(tmp_path):
    """A database that never had the legacy tables is left exactly as created."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        _migrate_kb_domains(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM kb_domains")).scalar_one() == 0
    assert "kb_keys" not in inspect(engine).get_table_names()
    engine.dispose()
