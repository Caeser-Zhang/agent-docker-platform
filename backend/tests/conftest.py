"""Shared pytest fixtures for the permission-isolation tests.

Builds an isolated SQLite database per test (so tests never touch the
production database) and FastAPI apps exposing the routers under test, with
``get_db`` / ``get_current_user`` overridden to a throwaway session and a
synthetic user, respectively. ``mock_httpx`` keeps outbound calls in-process.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
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


@pytest_asyncio.fixture
async def app_client_factory(db_factory):
    """Build a client over an app exposing ANY routers, for one synthetic user.

    ``client_factory`` above is pinned to the user-config router; the KB
    whitelist spans three routers (proxy, admin, citation badge) that all need
    the same throwaway database and dependency overrides. ``role="admin"`` is
    enough to satisfy ``require_admin``, which reads the role off the user.
    """

    def make(routers, *, user_id: str = "u1", username: str = "alice", role: str = "user",
             uid: str = "00899219") -> httpx.AsyncClient:
        app = FastAPI()
        for router in routers:
            app.include_router(router)

        async def override_get_db():
            async with db_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=user_id, username=username, role=role, uid=uid
        )

        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return make


@pytest.fixture
def mock_httpx(monkeypatch):
    """Intercept outbound ``httpx.AsyncClient`` calls with an in-memory handler.

    ``install(handler)`` patches the client class for the rest of the test and
    returns the list of requests the code under test actually sent upstream, so
    tests can assert on the injected ``X-API-Key`` and the forwarded URL/body.
    Clients that bring their own transport — the ASGI test clients built above —
    are passed through untouched.
    """
    real_client = httpx.AsyncClient
    recorded: list[httpx.Request] = []

    def install(handler):
        def _handler(request: httpx.Request):
            recorded.append(request)
            return handler(request)  # MockTransport awaits coroutine results too

        def factory(**kwargs):
            if "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(_handler)
            return real_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return recorded

    return install