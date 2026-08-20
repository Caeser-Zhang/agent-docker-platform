"""Database engine and session management."""
from pathlib import Path

from sqlalchemy import bindparam, text
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


class Base(DeclarativeBase):
    pass


async def init_db():
    """Create all tables and run lightweight migrations — called on startup."""
    from .models import User, AgentContainer  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # No Alembic in this project — add columns that older databases
        # (created before the field existed) are missing.
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(sync_conn) -> None:
    """ALTER TABLE for columns introduced after initial deployment (SQLite).

    PRAGMA introspection is SQLite-only; on other backends (PostgreSQL)
    create_all already produced the current schema, so skip.
    """
    if sync_conn.dialect.name != "sqlite":
        return
    user_cols = {row[1] for row in sync_conn.execute(text("PRAGMA table_info(users)"))}
    if "role" not in user_cols:
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")
        )


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
