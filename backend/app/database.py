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
        UserLLMProvider,
        AuditEvent,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # No Alembic in this project — add columns that older databases
        # (created before the field existed) are missing.
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_drop_legacy_tables)


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
