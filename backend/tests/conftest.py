"""Shared pytest fixtures for the user-content permission-isolation tests.

Builds an isolated SQLite database per test (so tests never touch the
production database) and a FastAPI app exposing only the user-config router
with ``get_db`` / ``get_current_user`` overridden to a throwaway session and a
synthetic user, respectively.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (register tables on Base.metadata)
from app.auth import get_current_user
from app.database import Base, get_db
from app.routers import user_config


@pytest_asyncio.fixture
async def db_factory(tmp_path):
    """An async session factory bound to a fresh temp-file SQLite database."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client_factory(db_factory):
    """Return an async factory building an httpx client for a synthetic user."""

    async def make_client(user_id: str, username: str) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(user_config.router)

        async def override_get_db():
            async with db_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=user_id, username=username, role="user"
        )

        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return make_client