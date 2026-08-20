"""Database engine and session management."""
from pathlib import Path

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
    """Create all tables — called on startup."""
    from .models import User, AgentContainer  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
