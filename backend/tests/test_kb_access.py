"""Whitelist core (services/kb_access) plus the citation-badge route it guards.

kb_access is the single place where "may this user read this database?" and
"which credential do we inject?" are answered, so its two outcomes are tested
separately: denial is a user-facing 403, a missing credential an operator-facing
500. The badge tests prove routers/fastk.py goes through the same gate rather
than forwarding the browser's request blindly.
"""
import httpx
import pytest

from app import crypto
from app.config import settings
from app.models import KbGrant, KbKey
from app.routers import fastk as fastk_router
from app.services import kb_access


async def seed(db, *, grants=(), keys=()):
    """Insert credentials/keys directly — the admin API has its own tests."""
    for kb_name, api_key in keys:
        db.add(KbKey(kb_name=kb_name, api_key_enc=crypto.encrypt_secret(api_key)))
    for user_id, kb_name in grants:
        db.add(KbGrant(user_id=user_id, kb_name=kb_name))
    await db.commit()


# ------------------------------------------------------------------ proxy token


def test_proxy_token_roundtrips_to_its_owner():
    token = kb_access.issue_proxy_token("user-1")
    assert kb_access.verify_proxy_token(token) == "user-1"
    assert "user-1" not in token  # opaque: the uid cannot be read out of it


@pytest.mark.parametrize(
    "token",
    [None, "", "not-a-fernet-token", crypto.encrypt_secret("user-1")],
    ids=["none", "empty", "garbage", "encrypted-but-unprefixed"],
)
def test_proxy_token_rejected(token):
    # Only tokens this module minted pass — a bare encrypted uid must not.
    assert kb_access.verify_proxy_token(token) is None


def test_proxy_token_cannot_impersonate_another_user():
    assert kb_access.verify_proxy_token(kb_access.issue_proxy_token("u2")) == "u2"
    assert kb_access.verify_proxy_token(kb_access.issue_proxy_token("u1")) != "u2"


# --------------------------------------------------------------- resolve_access


async def test_denied_without_a_grant(db_factory):
    async with db_factory() as db:
        await seed(db, keys=[("fastdb", "sk-real")])
        assert await kb_access.resolve_access(db, "fastdb", "u1") == kb_access.KbAccess(False, None)


async def test_grant_without_credential_is_an_operator_error(db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")])
        access = await kb_access.resolve_access(db, "fastdb", "u1")
        assert access.granted is True
        assert access.api_key is None  # → 500, never a 403 blaming the user


async def test_grant_yields_the_decrypted_platform_key(db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
        access = await kb_access.resolve_access(db, "fastdb", "u1")
        assert access == kb_access.KbAccess(granted=True, api_key="sk-real")


async def test_access_is_scoped_per_user_and_per_database(db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "k1"), ("asset_test", "k2")])
        assert (await kb_access.resolve_access(db, "asset_test", "u1")).granted is False
        assert (await kb_access.resolve_access(db, "fastdb", "u2")).granted is False


async def test_granted_kbs_lists_only_own_databases(db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "vl_test"), ("u1", "asset_test"), ("u2", "fastdb")])
        assert await kb_access.granted_kbs(db, "u1") == ["asset_test", "vl_test"]
        assert await kb_access.granted_kbs(db, "nobody") == []


def test_denial_message_is_actionable_and_configurable(monkeypatch):
    monkeypatch.setattr(settings, "kb_admin_contact", "李四（工号 000001）")
    message = kb_access.denial_message("fastdb")
    assert "fastdb" in message
    assert "fastk databases" in message  # tells the agent what to run next
    assert "李四（工号 000001）" in message  # contact comes from config, not code


# ---------------------------------------------------------------- citation badge


async def test_chunk_denied_for_a_non_whitelisted_user(app_client_factory, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get("/api/fastk/chunk", params={"db": "fastdb", "chunk_id": "abc123"})
    assert r.status_code == 403
    assert "无权限访问知识库 'fastdb'" in r.json()["detail"]
    assert calls == []  # refused before anything reaches the fastk server


async def test_chunk_500_when_the_credential_is_missing(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")])
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get("/api/fastk/chunk", params={"db": "fastdb", "chunk_id": "abc123"})
    assert r.status_code == 500
    assert "缺少可用凭据" in r.json()["detail"]
    assert calls == []


async def test_chunk_injects_the_platform_key_for_a_granted_user(
    app_client_factory, mock_httpx, db_factory
):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
    calls = mock_httpx(
        lambda request: httpx.Response(
            200, json={"results": [{"chunk_id": "abc123", "text": "hello", "image_path": ""}]}
        )
    )
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get("/api/fastk/chunk", params={"db": "fastdb", "chunk_id": "abc123"})
    assert r.status_code == 200
    assert r.json()["chunk_id"] == "abc123"
    assert calls[0].headers["x-api-key"] == "sk-real"
    assert str(calls[0].url).endswith("/fastk/api/databases/fastdb/query")


async def test_chunk_image_is_gated_the_same_way(app_client_factory, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, content=b"png", headers={"content-type": "image/png"}))
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get(
            "/api/fastk/chunk-image", params={"db": "fastdb", "chunk_id": "abc123", "index": 0}
        )
    assert r.status_code == 403
    assert calls == []
