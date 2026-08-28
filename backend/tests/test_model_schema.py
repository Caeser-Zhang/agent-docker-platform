"""Schema-level guard for the user-config data model.

Locks in the "方案 A" contract: the legacy ``user_llm_selection`` table (which
stored plaintext API keys and was never consumed) is removed, and the active
LLM selection is represented by two nullable columns on ``users``.
"""
from app.database import Base, _add_missing_columns, _drop_legacy_tables
import app.models  # noqa: F401  (register all tables on Base.metadata)


def test_legacy_selection_table_removed():
    assert "user_llm_selection" not in Base.metadata.tables


def test_users_has_active_selection_columns():
    cols = {c.name for c in Base.metadata.tables["users"].columns}
    assert {"active_llm_provider_id", "active_model"} <= cols


def test_migration_adds_active_columns_and_drops_legacy(tmp_path):
    """A pre-existing database is upgraded: new columns added, legacy table dropped."""
    import sqlalchemy as sa
    from sqlalchemy import text

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        meta = sa.MetaData()
        sa.Table(
            "users",
            meta,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("username", sa.String(100)),
            sa.Column("hashed_password", sa.String(200)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        sa.Table(
            "user_llm_selection",
            meta,
            sa.Column("user_id", sa.String(36), primary_key=True),
            sa.Column("api_key", sa.Text),
        )
        meta.create_all(engine)

        with engine.begin() as conn:
            _add_missing_columns(conn)
            _drop_legacy_tables(conn)

        with engine.connect() as conn:
            users_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(users)"))}
            assert "active_llm_provider_id" in users_cols
            assert "active_model" in users_cols

            tables = {
                r[0]
                for r in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
            assert "user_llm_selection" not in tables
    finally:
        engine.dispose()