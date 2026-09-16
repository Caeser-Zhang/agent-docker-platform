"""The whitelist enforcement point: ``/fastk/api/{path}`` proxy.

What an agent container can do is decided here, so the tests walk the same order
the route does: authenticate the proxy token, refuse writes, then either filter
the catalog or authorise one database and forward it with the platform's real
key. Authorisation is domain-based: a database inherits its domain's key and
its domain's public/private access rule. The invariant checked in every
forwarding test is that the caller's own token never travels upstream.
"""
import json
from datetime import datetime, timezone

import httpx

from app import crypto
from app.config import settings
from app.models import KbDomain, KbDomainDb, KbDomainGrant
from app.routers import kb_proxy
from app.services import kb_access


async def make_domain(
    db, name, *, key_type="private", api_key="sk-real", dbs=(), members=(), revoked=()
):
    for_domain = KbDomain(
        name=name,
        key_type=key_type,
        api_key_enc=crypto.encrypt_secret(api_key) if api_key else "",
    )
    db.add(for_domain)
    await db.flush()
    for kb_name in dbs:
        db.add(KbDomainDb(domain_id=for_domain.id, kb_name=kb_name))
    for user_id in members:
        db.add(KbDomainGrant(user_id=user_id, domain_id=for_domain.id))
    for user_id in revoked:
        db.add(KbDomainGrant(
            user_id=user_id, domain_id=for_domain.id, revoked_at=datetime.now(timezone.utc)
        ))
    await db.commit()
    return for_domain


CATALOG = [
    {"name": "fastdb", "description": "源码库", "uri": "/data/fastdb", "model": "bge", "dimension": 1024},
    {"name": "hr_only", "description": "人事", "uri": "/data/hr", "model": "bge", "dimension": 1024},
]


def auth(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


# ------------------------------------------------------------------ token gate


async def test_missing_or_forged_token_is_401(app_client_factory, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        assert (await client.get("/fastk/api/databases/")).status_code == 401
        r = await client.get("/fastk/api/databases/", headers=auth("sk-some-real-looking-key"))
    assert r.status_code == 401
    assert r.json()["error"]["message"]  # the CLI prints error.message verbatim
    assert calls == []


async def test_a_real_api_key_is_not_accepted_as_a_proxy_token(app_client_factory, mock_httpx, db_factory):
    """Containers hold no real key, so one arriving here is by definition wrong."""
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases/", headers=auth("sk-real"))
    assert r.status_code == 401


# ----------------------------------------------------------------- read guard


async def test_mutating_post_is_refused_even_on_a_granted_db(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={}))
    token = kb_access.issue_proxy_token("u1")
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/fastdb/documents", json={"path": "x.md"}, headers=auth(token)
        )
    assert r.status_code == 405
    assert "只读" in r.json()["error"]["message"]
    assert calls == []


async def test_write_post_is_refused_before_authorisation(app_client_factory, mock_httpx):
    """A user with no grants still gets 405, not a hint about the database."""
    calls = mock_httpx(lambda request: httpx.Response(200, json={}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/hr_only/index", json={}, headers=auth(kb_access.issue_proxy_token("u1"))
        )
    assert r.status_code == 405
    assert calls == []


# -------------------------------------------------------------------- catalog


async def test_catalog_is_filtered_to_grants_and_strips_uri(
    app_client_factory, mock_httpx, db_factory, monkeypatch
):
    monkeypatch.setattr(settings, "kb_catalog_key", "sk-admin")
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-real", members=["u1"])
        await make_domain(db, "人事", dbs=["hr_only"], api_key="sk-hr")  # u1 not on roster
    calls = mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases/", headers=auth(kb_access.issue_proxy_token("u1")))
    assert r.status_code == 200
    entries = r.json()
    assert [e["name"] for e in entries] == ["fastdb"]  # hr_only hidden entirely
    assert "uri" not in entries[0]  # host storage path stays on the host
    assert entries[0]["description"] == "源码库"  # server metadata survives filtering
    assert entries[0]["dimension"] == 1024
    # The listing is server-wide: it goes upstream with the platform catalog
    # key — not a per-domain key, and never the caller's own proxy token.
    assert calls[0].headers["x-api-key"] == "sk-admin"


async def test_catalog_includes_public_databases_for_everyone(
    app_client_factory, mock_httpx, db_factory, monkeypatch
):
    monkeypatch.setattr(settings, "kb_catalog_key", "sk-admin")
    async with db_factory() as db:
        await make_domain(db, "公告", key_type="public", dbs=["hr_only"], api_key="sk-hr")
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-real")  # private, no roster
    mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases/", headers=auth(kb_access.issue_proxy_token("u9")))
    assert [e["name"] for e in r.json()] == ["hr_only"]


async def test_catalog_sends_no_key_when_none_is_configured(
    app_client_factory, mock_httpx, db_factory, monkeypatch
):
    """Servers that leave the listing open must not be handed a stray header."""
    monkeypatch.setattr(settings, "kb_catalog_key", "")
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases/", headers=auth(kb_access.issue_proxy_token("u1")))
    assert [e["name"] for e in r.json()] == ["fastdb"]
    assert "x-api-key" not in calls[0].headers


async def test_catalog_without_trailing_slash_behaves_the_same(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases", headers=auth(kb_access.issue_proxy_token("u1")))
    assert [e["name"] for e in r.json()] == ["fastdb"]


async def test_catalog_is_empty_for_a_user_with_no_grants(app_client_factory, mock_httpx):
    mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases/", headers=auth(kb_access.issue_proxy_token("u9")))
    assert r.status_code == 200
    assert r.json() == []


# ------------------------------------------------------------- per-db forwards


async def test_search_forwards_the_domain_key_and_drops_the_token(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-real", members=["u1"])
        await make_domain(db, "人事", dbs=["hr_only"], api_key="sk-hr")
    results = {"results": [{"chunk_id": "abc", "text": "hit"}], "total": 1}
    calls = mock_httpx(lambda request: httpx.Response(200, json=results))
    token = kb_access.issue_proxy_token("u1")
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/fastdb/search",
            json={"query": "hybrid search", "topk": 5},
            headers=auth(token),
        )
    assert r.status_code == 200
    assert r.json() == results
    sent = calls[0]
    assert sent.method == "POST"
    assert str(sent.url).endswith("/fastk/api/databases/fastdb/search")
    assert json.loads(sent.content) == {"query": "hybrid search", "topk": 5}
    assert sent.headers["x-api-key"] == "sk-real"
    assert token not in sent.headers.values()  # the platform token never leaves the backend


async def test_each_domain_gets_its_own_key(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-fastdb", members=["u1"])
        await make_domain(db, "人事", dbs=["hr_only"], api_key="sk-hr", members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    token = kb_access.issue_proxy_token("u1")
    client = app_client_factory([kb_proxy.router])
    async with client:
        await client.post("/fastk/api/databases/fastdb/search", json={"query": "a"}, headers=auth(token))
        await client.post("/fastk/api/databases/hr_only/search", json={"query": "b"}, headers=auth(token))
    assert [c.headers["x-api-key"] for c in calls] == ["sk-fastdb", "sk-hr"]


async def test_sibling_databases_share_their_domain_key(app_client_factory, mock_httpx, db_factory):
    """One key, many databases — every sibling forwards with the same credential."""
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb", "fastdb_mirror"], api_key="sk-domain", members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    token = kb_access.issue_proxy_token("u1")
    client = app_client_factory([kb_proxy.router])
    async with client:
        await client.post("/fastk/api/databases/fastdb/search", json={"query": "a"}, headers=auth(token))
        await client.post("/fastk/api/databases/fastdb_mirror/search", json={"query": "b"}, headers=auth(token))
    assert [c.headers["x-api-key"] for c in calls] == ["sk-domain", "sk-domain"]


async def test_public_domain_forwards_for_any_container(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "公告", key_type="public", dbs=["news"], api_key="sk-pub")
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/news/search",
            json={"query": "x"},
            headers=auth(kb_access.issue_proxy_token("u9")),  # never granted anything
        )
    assert r.status_code == 200
    assert calls[0].headers["x-api-key"] == "sk-pub"


async def test_ungranted_database_is_403_with_guidance(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-real", members=["u1"])
        await make_domain(db, "人事", dbs=["hr_only"], api_key="sk-hr")
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/hr_only/search",
            json={"query": "salary"},
            headers=auth(kb_access.issue_proxy_token("u1")),
        )
    assert r.status_code == 403
    message = r.json()["error"]["message"]
    assert "hr_only" in message
    assert "fastk databases" in message  # the agent can recover from this
    assert settings.kb_admin_contact in message
    assert calls == []


async def test_revoked_member_loses_access_on_the_next_request(app_client_factory, mock_httpx, db_factory):
    """Enforcement re-reads the roster per request — no container recreate needed."""
    async with db_factory() as db:
        domain = await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    client = app_client_factory([kb_proxy.router])
    token = kb_access.issue_proxy_token("u1")
    async with client:
        ok = await client.post("/fastk/api/databases/fastdb/search", json={"query": "x"}, headers=auth(token))
        async with db_factory() as db:
            row = (await db.execute(
                KbDomainGrant.__table__.select().where(KbDomainGrant.domain_id == domain.id)
            )).first()
            await db.execute(
                KbDomainGrant.__table__.update()
                .where(KbDomainGrant.user_id == "u1", KbDomainGrant.domain_id == domain.id)
                .values(revoked_at=datetime.now(timezone.utc))
            )
            await db.commit()
        denied = await client.post("/fastk/api/databases/fastdb/search", json={"query": "x"}, headers=auth(token))
    assert ok.status_code == 200
    assert denied.status_code == 403
    assert len(calls) == 1
    assert row is not None


async def test_database_detail_is_gated_too(app_client_factory, mock_httpx, db_factory):
    """The CLI's `instructions` command hits GET /databases/<db> — same gate."""
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], api_key="sk-real", members=["u1"])
        await make_domain(db, "人事", dbs=["hr_only"], api_key="sk-hr")
    calls = mock_httpx(
        lambda request: httpx.Response(200, json={"name": "fastdb", "instructions": "use it"})
    )
    token = kb_access.issue_proxy_token("u1")
    client = app_client_factory([kb_proxy.router])
    async with client:
        denied = await client.get("/fastk/api/databases/hr_only", headers=auth(token))
        allowed = await client.get("/fastk/api/databases/fastdb", headers=auth(token))
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["instructions"] == "use it"
    assert len(calls) == 1 and calls[0].headers["x-api-key"] == "sk-real"


async def test_granted_db_without_credential_is_500(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", api_key=None, dbs=["fastdb"], members=["u1"])  # key never entered
    calls = mock_httpx(lambda request: httpx.Response(200, json={}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/fastdb/search",
            json={"query": "x"},
            headers=auth(kb_access.issue_proxy_token("u1")),
        )
    assert r.status_code == 500
    assert "缺少可用凭据" in r.json()["error"]["message"]
    assert calls == []  # forwarding without a key would only fail upstream


async def test_query_string_is_forwarded_verbatim(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={"count": 42}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get(
            "/fastk/api/databases/fastdb/stats?refresh=1",
            headers=auth(kb_access.issue_proxy_token("u1")),
        )
    assert r.status_code == 200
    assert calls[0].url.query == b"refresh=1"


async def test_paths_outside_databases_are_404(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/health", headers=auth(kb_access.issue_proxy_token("u1")))
    assert r.status_code == 404
    assert calls == []


async def test_database_names_outside_the_allowlist_are_404(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    calls = mock_httpx(lambda request: httpx.Response(200, json={}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get(
            "/fastk/api/databases/fast%20db", headers=auth(kb_access.issue_proxy_token("u1"))
        )
    assert r.status_code == 404
    assert calls == []


async def test_unreachable_upstream_is_502(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])

    def boom(request):
        raise httpx.ConnectError("no route to host")

    mock_httpx(boom)
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/fastdb/search",
            json={"query": "x"},
            headers=auth(kb_access.issue_proxy_token("u1")),
        )
    assert r.status_code == 502
    assert "fastk 服务不可达" in r.json()["error"]["message"]


async def test_upstream_status_and_body_pass_through(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await make_domain(db, "研发", dbs=["fastdb"], members=["u1"])
    mock_httpx(lambda request: httpx.Response(422, json={"detail": "topk must be positive"}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.post(
            "/fastk/api/databases/fastdb/search",
            json={"query": "x", "topk": -1},
            headers=auth(kb_access.issue_proxy_token("u1")),
        )
    assert r.status_code == 422
    assert r.json() == {"detail": "topk must be positive"}
