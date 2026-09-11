"""The whitelist enforcement point: ``/fastk/api/{path}`` proxy.

What an agent container can do is decided here, so the tests walk the same order
the route does: authenticate the proxy token, refuse writes, then either filter
the catalog or authorise one database and forward it with the platform's real
key. The invariant checked in every forwarding test is that the caller's own
token never travels upstream.
"""
import json

import httpx

from app.config import settings
from app.models import KbGrant, KbKey
from app.routers import kb_proxy
from app.services import kb_access
from app import crypto


async def seed(db, *, grants=(), keys=()):
    for kb_name, api_key in keys:
        db.add(KbKey(kb_name=kb_name, api_key_enc=crypto.encrypt_secret(api_key)))
    for user_id, kb_name in grants:
        db.add(KbGrant(user_id=user_id, kb_name=kb_name))
    await db.commit()


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
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
    mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/databases/", headers=auth("sk-real"))
    assert r.status_code == 401


# ----------------------------------------------------------------- read guard


async def test_mutating_post_is_refused_even_on_a_granted_db(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
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


async def test_catalog_is_filtered_to_grants_and_strips_uri(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real"), ("hr_only", "sk-hr")])
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
    assert "x-api-key" not in calls[0].headers  # listing needs no key, and none is sent


async def test_catalog_without_trailing_slash_behaves_the_same(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
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


async def test_search_forwards_the_real_key_and_drops_the_token(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real"), ("hr_only", "sk-hr")])
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


async def test_each_database_gets_its_own_key(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(
            db,
            grants=[("u1", "fastdb"), ("u1", "hr_only")],
            keys=[("fastdb", "sk-fastdb"), ("hr_only", "sk-hr")],
        )
    calls = mock_httpx(lambda request: httpx.Response(200, json={"results": []}))
    token = kb_access.issue_proxy_token("u1")
    client = app_client_factory([kb_proxy.router])
    async with client:
        await client.post("/fastk/api/databases/fastdb/search", json={"query": "a"}, headers=auth(token))
        await client.post("/fastk/api/databases/hr_only/search", json={"query": "b"}, headers=auth(token))
    assert [c.headers["x-api-key"] for c in calls] == ["sk-fastdb", "sk-hr"]


async def test_ungranted_database_is_403_with_guidance(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real"), ("hr_only", "sk-hr")])
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


async def test_database_detail_is_gated_too(app_client_factory, mock_httpx, db_factory):
    """The CLI's `instructions` command hits GET /databases/<db> — same gate."""
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real"), ("hr_only", "sk-hr")])
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
        await seed(db, grants=[("u1", "fastdb")])  # grant recorded, key never entered
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
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
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
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
    calls = mock_httpx(lambda request: httpx.Response(200, json={}))
    client = app_client_factory([kb_proxy.router])
    async with client:
        r = await client.get("/fastk/api/health", headers=auth(kb_access.issue_proxy_token("u1")))
    assert r.status_code == 404
    assert calls == []


async def test_database_names_outside_the_allowlist_are_404(app_client_factory, mock_httpx, db_factory):
    async with db_factory() as db:
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
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
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])

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
        await seed(db, grants=[("u1", "fastdb")], keys=[("fastdb", "sk-real")])
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
