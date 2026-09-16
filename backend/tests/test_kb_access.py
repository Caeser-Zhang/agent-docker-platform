"""Domain access core (services/kb_access) plus the citation-badge route it guards.

kb_access is the single place where "may this user read this database?" and
"which credential do we inject?" are answered, so its two outcomes are tested
separately: denial is a user-facing 403, a missing credential an operator-facing
500. The unit of authorisation is the DOMAIN — a database name is reverse-looked
-up to its unique domain, public domains grant everyone implicitly, private
domains consult the roster. The badge tests prove routers/fastk.py goes through
the same gate rather than forwarding the browser's request blindly.
"""
from datetime import datetime, timezone

import httpx
import pytest

from app import crypto
from app.config import settings
from app.models import KbDomain, KbDomainDb, KbDomainGrant
from app.routers import fastk as fastk_router
from app.services import kb_access


async def make_domain(
    db, name, *, key_type="private", api_key="sk-real", dbs=(), members=(), revoked=()
):
    """Seed one domain directly — the admin API has its own tests.

    ``members`` are active roster user_ids, ``revoked`` soft-deleted ones.
    ``api_key=None`` leaves the domain without a credential (operator error).
    """
    domain = KbDomain(
        name=name,
        key_type=key_type,
        api_key_enc=crypto.encrypt_secret(api_key) if api_key else "",
    )
    db.add(domain)
    await db.flush()
    for kb_name in dbs:
        db.add(KbDomainDb(domain_id=domain.id, kb_name=kb_name))
    for user_id in members:
        db.add(KbDomainGrant(user_id=user_id, domain_id=domain.id))
    for user_id in revoked:
        db.add(KbDomainGrant(
            user_id=user_id, domain_id=domain.id, revoked_at=datetime.now(timezone.utc)
        ))
    await db.commit()
    return domain


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


async def test_unmanaged_database_is_denied(db_factory):
    """A database no domain claims is unreachable — only managed dbs proxy."""
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
        assert await kb_access.resolve_access(db, "unknown_db", "u1") == kb_access.KbAccess(False, None)


async def test_private_domain_denies_without_a_roster_row(db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"])
        assert await kb_access.resolve_access(db, "fastdb", "u1") == kb_access.KbAccess(False, None)


async def test_one_private_key_covers_every_database_in_the_domain(db_factory):
    """The core concept shift: the key belongs to the DOMAIN, not the database."""
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb", "fastdb_mirror"], api_key="sk-domain", members=["u1"])
        for kb_name in ("fastdb", "fastdb_mirror"):
            access = await kb_access.resolve_access(db, kb_name, "u1")
            assert access == kb_access.KbAccess(granted=True, api_key="sk-domain")


async def test_public_domain_grants_every_user_implicitly(db_factory):
    """Public = no roster consulted; a user nobody ever granted still passes."""
    async with db_factory() as db:
        await make_domain(db, "公告", key_type="public", dbs=["news"], api_key="sk-pub")
        access = await kb_access.resolve_access(db, "news", "literally-anyone")
        assert access == kb_access.KbAccess(granted=True, api_key="sk-pub")


async def test_grant_without_credential_is_an_operator_error(db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", api_key=None, dbs=["fastdb"], members=["u1"])
        access = await kb_access.resolve_access(db, "fastdb", "u1")
        assert access.granted is True
        assert access.api_key is None  # → 500, never a 403 blaming the user


async def test_revoked_roster_row_denies(db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], revoked=["u1"])
        assert (await kb_access.resolve_access(db, "fastdb", "u1")).granted is False


async def test_access_is_scoped_per_user_and_per_domain(db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="k1", members=["u1"])
        await make_domain(db, "人事", dbs=["hr_only"], api_key="k2", members=["u2"])
        assert (await kb_access.resolve_access(db, "hr_only", "u1")).granted is False
        assert (await kb_access.resolve_access(db, "fastdb", "u2")).granted is False
        assert (await kb_access.resolve_access(db, "fastdb", "u1")).api_key == "k1"


# ------------------------------------------------------------------ granted_kbs


async def test_granted_kbs_unions_public_and_private(db_factory):
    async with db_factory() as db:
        await make_domain(db, "公告", key_type="public", dbs=["pub_b", "pub_a"])
        await make_domain(db, "研发", dbs=["sec"], members=["u1"])
        await make_domain(db, "人事", dbs=["other"], members=["u2"])
        # Sorted, and public databases come free for every user.
        assert await kb_access.granted_kbs(db, "u1") == ["pub_a", "pub_b", "sec"]
        assert await kb_access.granted_kbs(db, "nobody") == ["pub_a", "pub_b"]


async def test_granted_kbs_ignores_revoked_rows(db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["sec"], revoked=["u1"])
        assert await kb_access.granted_kbs(db, "u1") == []


def test_denial_message_is_actionable_and_configurable(monkeypatch):
    monkeypatch.setattr(settings, "kb_admin_contact", "李四（工号 000001）")
    message = kb_access.denial_message("fastdb")
    assert "fastdb" in message
    assert "fastk databases" in message  # tells the agent what to run next
    assert "李四（工号 000001）" in message  # contact comes from config, not code


# ---------------------------------------------------------------- citation badge


async def test_chunk_denied_for_a_non_whitelisted_user(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"])  # private, empty roster
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get("/api/fastk/chunk", params={"db": "fastdb", "chunk_id": "abc123"})
    assert r.status_code == 403
    assert "无权限访问知识库 'fastdb'" in r.json()["detail"]
    assert calls == []  # refused before anything reaches the fastk server


async def test_chunk_500_when_the_credential_is_missing(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", api_key=None, dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get("/api/fastk/chunk", params={"db": "fastdb", "chunk_id": "abc123"})
    assert r.status_code == 500
    assert "缺少可用凭据" in r.json()["detail"]
    assert calls == []


async def test_chunk_injects_the_domain_key_for_a_roster_member(
    app_client_factory, mock_httpx, db_factory
):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-real", members=["u1"])
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


async def test_chunk_in_a_public_domain_needs_no_roster_row(
    app_client_factory, mock_httpx, db_factory
):
    async with db_factory() as db:
        await make_domain(db, "公告", key_type="public", dbs=["news"], api_key="sk-pub")
    calls = mock_httpx(
        lambda request: httpx.Response(200, json={"results": [{"chunk_id": "c1", "text": "hi"}]})
    )
    client = app_client_factory([fastk_router.router], user_id="stranger")
    async with client:
        r = await client.get("/api/fastk/chunk", params={"db": "news", "chunk_id": "c1"})
    assert r.status_code == 200
    assert calls[0].headers["x-api-key"] == "sk-pub"


async def test_chunk_image_is_gated_the_same_way(app_client_factory, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, content=b"png", headers={"content-type": "image/png"}))
    client = app_client_factory([fastk_router.router], user_id="u1")
    async with client:
        r = await client.get(
            "/api/fastk/chunk-image", params={"db": "fastdb", "chunk_id": "abc123", "index": 0}
        )
    assert r.status_code == 403
    assert calls == []
