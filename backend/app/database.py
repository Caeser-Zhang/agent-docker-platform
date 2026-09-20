"""Database engine and session management."""
from pathlib import Path

from sqlalchemy import bindparam, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


def _ensure_sqlite_parent_dir() -> None:
    """Create the SQLite file's parent directory if it does not exist.

    SQLite creates the database file on connect but never its parent
    directory, so an absolute URL (sqlite+aiosqlite:////app/data/x.db)
    would fail on a freshly created volume. No-op for non-SQLite URLs
    and for relative paths whose parent is the CWD.
    """
    url = make_url(settings.database_url)
    if url.drivername.startswith("sqlite") and url.database:
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent_dir()
engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI dependency yielding one request-scoped async session."""
    async with async_session() as session:
        yield session


class Base(DeclarativeBase):
    pass


async def init_db():
    """Create all tables and run lightweight migrations — called on startup."""
    from .models import (  # noqa: F401
        User,
        AgentContainer,
        UserMcpServer,
        UserBuiltinMcpToggle,
        UserLLMProvider,
        AuditEvent,
        RequestLog,
        KbDomain,
        KbDomainDb,
        KbDomainGrant,
        MessageFeedback,
        AgentRoundMetrics,
        ToolCallMetrics,
        LLMProxyMetrics,
        OpinionFeedback,
        OpinionAttachment,
        Wish,
        WishAction,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # No Alembic in this project — add columns that older databases
        # (created before the field existed) are missing.
        await conn.run_sync(_add_missing_columns)
        # One-shot reshape: per-database credentials → knowledge domains.
        await conn.run_sync(_migrate_kb_domains)
        await conn.run_sync(_drop_legacy_tables)


def _table_columns(sync_conn, table: str) -> set[str]:
    """Existing column names for ``table`` (empty set if the table is absent)."""
    if sync_conn.dialect.name == "sqlite":
        return {row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))}
    return {
        row[0] for row in sync_conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": table},
        )
    }


def _ensure_column(sync_conn, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN if missing; no-op when the table doesn't exist yet.

    An empty introspection result means create_all will build the table with the
    column already present, so there is nothing to migrate.
    """
    cols = _table_columns(sync_conn, table)
    if cols and column not in cols:
        sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _add_missing_columns(sync_conn) -> None:
    """ALTER TABLE for columns introduced after initial deployment.

    create_all only creates missing TABLES — it never alters existing ones,
    so any database created before a column existed (SQLite or PostgreSQL)
    needs these additive migrations. Introspection is dialect-specific:
    PRAGMA for SQLite, information_schema for PostgreSQL.
    """
    if sync_conn.dialect.name == "sqlite":
        user_cols = {row[1] for row in sync_conn.execute(text("PRAGMA table_info(users)"))}
    else:
        user_cols = {
            row[0] for row in sync_conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
            )
        }
    if "role" not in user_cols:
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")
        )
    if "uid" not in user_cols:
        # ALTER TABLE ADD COLUMN cannot add UNIQUE on either backend —
        # uniqueness is enforced in the register route for migrated databases.
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN uid VARCHAR(50)"))
    if "active_llm_provider_id" not in user_cols:
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN active_llm_provider_id VARCHAR(100)")
        )
    if "active_model" not in user_cols:
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN active_model VARCHAR(100)")
        )

    # 运维监测增强（P0）：回合错误分类 + token 拆分，工具单次耗时。
    _ensure_column(sync_conn, "agent_round_metrics", "error_name", "VARCHAR(64)")
    _ensure_column(sync_conn, "agent_round_metrics", "error_status_code", "INTEGER")
    for col in (
        "input_tokens", "output_tokens", "reasoning_tokens",
        "cache_read_tokens", "cache_write_tokens",
    ):
        _ensure_column(sync_conn, "agent_round_metrics", col, "INTEGER")
    _ensure_column(sync_conn, "tool_call_metrics", "duration_ms", "INTEGER")


def _migrate_kb_domains(sync_conn) -> None:
    """One-shot migration: kb_keys/kb_grants → kb_domains/kb_domain_dbs/kb_domain_grants.

    Runs only when the legacy ``kb_keys`` table still exists; a fresh database
    (or a second startup after migration) skips it entirely, which makes the
    whole function idempotent. Each legacy credential becomes one PRIVATE
    domain named after its database, holding exactly that database — so the
    effective whitelist is bit-for-bit the old one on the first boot after
    upgrade, and admins then merge/rename/switch types from the UI.

    Timestamps are not carried over: raw SQLite storage can come back as a
    string, and only ``revoked_at``'s NULL/not-NULL state carries meaning
    (active vs. revoked). ``created_at`` is reset to now; the soft-delete
    stamp is re-derived from the old row's state.

    The legacy tables are dropped child-first in the same transaction — the
    migration must never leave a half-migrated state behind.
    """
    import uuid
    from datetime import datetime, timezone

    if not _table_columns(sync_conn, "kb_keys"):
        return

    now = datetime.now(timezone.utc)
    keys = sync_conn.execute(text("SELECT kb_name, api_key_enc FROM kb_keys")).fetchall()
    domain_of: dict[str, str] = {}
    for kb_name, api_key_enc in keys:
        domain_id = str(uuid.uuid4())
        domain_of[kb_name] = domain_id
        sync_conn.execute(
            text(
                "INSERT INTO kb_domains (id, name, description, key_type, api_key_enc, created_at, updated_at) "
                "VALUES (:id, :name, '', 'private', :enc, :ts, :ts)"
            ),
            {"id": domain_id, "name": kb_name, "enc": api_key_enc or "", "ts": now},
        )
        sync_conn.execute(
            text("INSERT INTO kb_domain_dbs (domain_id, kb_name, created_at) VALUES (:d, :k, :ts)"),
            {"d": domain_id, "k": kb_name, "ts": now},
        )

    grants = sync_conn.execute(
        text("SELECT user_id, kb_name, revoked_at FROM kb_grants")
    ).fetchall() if _table_columns(sync_conn, "kb_grants") else []
    for user_id, kb_name, revoked_at in grants:
        domain_id = domain_of.get(kb_name)
        if domain_id is None:
            continue  # orphan grant (shouldn't exist — FK) — nothing to point at
        sync_conn.execute(
            text(
                "INSERT INTO kb_domain_grants (user_id, domain_id, created_at, revoked_at) "
                "VALUES (:u, :d, :ts, :rev)"
            ),
            {"u": user_id, "d": domain_id, "ts": now,
             "rev": None if revoked_at is None else now},
        )

    sync_conn.execute(text("DROP TABLE IF EXISTS kb_grants"))
    sync_conn.execute(text("DROP TABLE IF EXISTS kb_keys"))


def _drop_legacy_tables(sync_conn) -> None:
    """Drop tables superseded by a cleaner data model.

    ``user_llm_selection`` was an unused 1:1 table that stored the LLM API key
    in plaintext. It is replaced by two nullable ``users`` columns
    (``active_llm_provider_id`` / ``active_model``); drop any pre-existing copy.
    """
    sync_conn.execute(text("DROP TABLE IF EXISTS user_llm_selection"))


def _next_uid(existing: list[str | None]) -> str:
    """Next sequential 工号, starting at 10001 above any numeric uid present."""
    highest = 10000
    for value in existing:
        if value and value.isdigit():
            highest = max(highest, int(value))
    return str(highest + 1)


async def backfill_uids() -> int:
    """Assign sequential 工号 to users created before the uid column existed."""
    from .models import User  # local import — models depends on this module

    async with async_session() as db:
        users = (await db.execute(select(User))).scalars().all()
        missing = [u for u in users if not u.uid]
        if not missing:
            return 0
        taken = [u.uid for u in users if u.uid]
        for user in missing:
            uid = _next_uid(taken)
            user.uid = uid
            taken.append(uid)
        await db.commit()
        return len(missing)


async def promote_admins() -> int:
    """Promote usernames from AGENT_ADMIN_USERNAMES to role='admin'.

    Runs at startup so existing deployments can bootstrap an admin without
    touching the database manually. Returns the number of promoted users.
    """
    names = settings.admin_username_set
    if not names:
        return 0
    stmt = text(
        "UPDATE users SET role = 'admin' WHERE username IN :names AND role != 'admin'"
    ).bindparams(bindparam("names", expanding=True))
    async with engine.begin() as conn:
        result = await conn.execute(stmt, {"names": sorted(names)})
        return result.rowcount
