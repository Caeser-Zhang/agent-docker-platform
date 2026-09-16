"""Admin surface for knowledge domains: CRUD, type switch, rosters, imports.

Properties that get their own tests: credentials are write-only (no endpoint
echoes one, the stored column is encrypted), a database belongs to exactly one
domain (409 names the owner), a type switch requires confirm_name and keeps the
roster rows so switching back restores the old membership, and nothing is
written by an import until the admin commits the preview (single-use token,
bound to one domain). The user side (/api/kb/my-domains) must aggregate only
what the caller may access and fail soft on catalog errors.
"""
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app import crypto
from app.config import settings
from app.models import AuditEvent, KbDomain, KbDomainDb, KbDomainGrant, User
from app.routers import kb_domains
from app.services import audit


async def add_user(db, *, id: str, username: str, uid: str, role: str = "user"):
    db.add(User(id=id, username=username, uid=uid, hashed_password="x", role=role))
    await db.commit()


async def seed_domain(
    db, name, *, key_type="private", api_key="sk-real", dbs=(), members=(), revoked=()
):
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


@pytest.fixture
def admin_client(app_client_factory, db_factory, monkeypatch):
    """Client authenticated as an admin, with audit writes aimed at the test DB."""
    monkeypatch.setattr(audit, "async_session", db_factory)
    # _previews is a module-level dict; swap in a fresh one per test so tokens
    # never leak across tests.
    monkeypatch.setattr(kb_domains, "_previews", {})
    return app_client_factory(
        [kb_domains.router, kb_domains.user_router],
        user_id="admin1", username="admin", role="admin",
    )


async def create_via_api(client, name, **kwargs) -> str:
    r = await client.post("/api/admin/kb-domains", json={"name": name, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ------------------------------------------------------------------- admin gate


async def test_admin_routes_reject_a_normal_user(app_client_factory):
    client = app_client_factory([kb_domains.router], user_id="u1", role="user")
    async with client:
        assert (await client.get("/api/admin/kb-domains")).status_code == 403
        assert (await client.post("/api/admin/kb-domains", json={"name": "x"})).status_code == 403
        assert (await client.get("/api/admin/kb-users")).status_code == 403


# ------------------------------------------------------------------ domain CRUD


async def test_create_and_list_domains(admin_client):
    async with admin_client as client:
        r = await client.post("/api/admin/kb-domains", json={
            "name": "研发领域", "key_type": "private", "api_key": "sk-secret",
            "description": "研发相关库",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "研发领域"
        assert body["key_type"] == "private"
        assert set(body) == {"id", "name", "key_type"}  # the key is never echoed

        await client.post("/api/admin/kb-domains", json={"name": "公共领域", "key_type": "public"})
        items = (await client.get("/api/admin/kb-domains")).json()["items"]
    assert [(d["name"], d["key_type"], d["has_api_key"], d["db_count"], d["member_count"])
            for d in items] == [
        ("研发领域", "private", True, 0, 0),
        ("公共领域", "public", False, 0, 0),
    ]
    assert "sk-secret" not in json.dumps(items, ensure_ascii=False)


async def test_create_rejects_duplicate_name_and_bad_input(admin_client):
    async with admin_client as client:
        await create_via_api(client, "研发")
        r = await client.post("/api/admin/kb-domains", json={"name": "研发"})
        assert r.status_code == 400
        assert "已存在" in r.json()["detail"]

        assert (await client.post("/api/admin/kb-domains", json={
            "name": "x", "key_type": "open"})).status_code == 400
        assert (await client.post("/api/admin/kb-domains", json={
            "name": "x", "description": "长" * 501})).status_code == 400
        assert (await client.post("/api/admin/kb-domains", json={"name": "  "})).status_code == 400


async def test_domain_key_is_write_only_and_encrypted(admin_client, db_factory):
    async with admin_client as client:
        domain_id = await create_via_api(client, "研发", api_key="sk-super-secret")
        r = await client.get(f"/api/admin/kb-domains/{domain_id}")
        assert r.status_code == 200
        assert r.json()["has_api_key"] is True
        assert "sk-super-secret" not in r.text  # a stolen admin session learns nothing

    async with db_factory() as db:
        row = (await db.execute(select(KbDomain).where(KbDomain.id == domain_id))).scalar_one()
    assert row.api_key_enc != "sk-super-secret"
    assert crypto.decrypt_secret(row.api_key_enc) == "sk-super-secret"


async def test_get_domain_detail_shape(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(
            db, "研发", dbs=["fastdb", "devdb"], members=["u1"], revoked=["u2"]
        )
    async with admin_client as client:
        r = await client.get(f"/api/admin/kb-domains/{domain.id}")
        assert (await client.get("/api/admin/kb-domains/nope")).status_code == 404
    body = r.json()
    assert [d["kb_name"] for d in body["databases"]] == ["devdb", "fastdb"]  # sorted
    assert body["members"] == [{
        "user_id": "u1", "username": "alice", "uid": "0001",
        "created_at": body["members"][0]["created_at"],
    }]
    assert body["total_users"] == 2
    assert body["active_member_count"] == 1  # revoked rows do not count


async def test_update_renames_and_re_describes(admin_client, db_factory):
    async with db_factory() as db:
        a = await seed_domain(db, "研发")
        b = await seed_domain(db, "人事")
    async with admin_client as client:
        r = await client.put(f"/api/admin/kb-domains/{a.id}", json={
            "name": "研发中台", "description": "新描述"})
        assert r.status_code == 200
        assert r.json() == {"id": a.id, "name": "研发中台", "key_type": "private"}

        r = await client.put(f"/api/admin/kb-domains/{a.id}", json={"name": "人事"})
        assert r.status_code == 400
        assert "已存在" in r.json()["detail"]

    async with db_factory() as db:
        row = (await db.execute(select(KbDomain).where(KbDomain.id == a.id))).scalar_one()
        assert row.name == "研发中台"
        assert row.description == "新描述"
        assert (await db.execute(select(KbDomain).where(KbDomain.id == b.id))).scalar_one()


async def test_update_rotates_key_and_rejects_empty(admin_client, db_factory):
    async with db_factory() as db:
        domain = await seed_domain(db, "研发", api_key="old")
    async with admin_client as client:
        assert (await client.put(f"/api/admin/kb-domains/{domain.id}",
                                 json={"api_key": ""})).status_code == 400
        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={"api_key": "new"})
        assert r.status_code == 200
    async with db_factory() as db:
        rows = (await db.execute(select(KbDomain))).scalars().all()
    assert len(rows) == 1
    assert crypto.decrypt_secret(rows[0].api_key_enc) == "new"


# ------------------------------------------------------------------ type switch


async def test_type_switch_requires_exact_confirm_name(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        await add_user(db, id="u3", username="carol", uid="0003")
        domain = await seed_domain(db, "研发", key_type="private", members=["u1"])

    async with admin_client as client:
        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={"key_type": "public"})
        assert r.status_code == 400
        assert "confirm_name" in r.json()["detail"]

        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={
            "key_type": "public", "confirm_name": "别的名字"})
        assert r.status_code == 400

        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={
            "key_type": "public", "confirm_name": "研发"})
        assert r.status_code == 200
        assert r.json()["key_type"] == "public"

        # No-op: sending the current type again is not a switch (no confirm needed).
        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={"key_type": "public"})
        assert r.status_code == 200

    async with db_factory() as db:
        events = (await db.execute(
            select(AuditEvent).where(AuditEvent.action == "kbdomain.type_switch")
        )).scalars().all()
    assert len(events) == 1  # the no-op PUT is not a switch and not audited as one
    detail = json.loads(events[0].detail)
    assert detail["from"] == "private" and detail["to"] == "public"
    assert detail["affected_users"] == 3  # private→public opens it to everyone


async def test_switch_to_private_counts_stranded_users(admin_client, db_factory):
    """public→private strands everyone NOT already on the roster."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(db, "公共", key_type="public", members=["u1"])
    async with admin_client as client:
        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={
            "key_type": "private", "confirm_name": "公共"})
        assert r.status_code == 200
    async with db_factory() as db:
        event = (await db.execute(
            select(AuditEvent).where(AuditEvent.action == "kbdomain.type_switch")
        )).scalars().one()
    detail = json.loads(event.detail)
    assert detail["from"] == "public" and detail["to"] == "private"
    assert detail["affected_users"] == 1  # 2 users total, 1 already on the roster


async def test_type_switch_preserves_roster_both_ways(admin_client, db_factory):
    """Roster rows survive both directions, so switching back restores membership."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发", key_type="private", members=["u1"])
    async with admin_client as client:
        await client.put(f"/api/admin/kb-domains/{domain.id}", json={
            "key_type": "public", "confirm_name": "研发"})
        await client.put(f"/api/admin/kb-domains/{domain.id}", json={
            "key_type": "private", "confirm_name": "研发"})
        members = (await client.get(f"/api/admin/kb-domains/{domain.id}/members")).json()["items"]
    assert [m["user_id"] for m in members] == ["u1"]
    async with db_factory() as db:
        rows = (await db.execute(select(KbDomainGrant))).scalars().all()
    assert len(rows) == 1 and rows[0].revoked_at is None  # never duplicated, never revoked


async def test_update_rejects_invalid_key_type(admin_client, db_factory):
    async with db_factory() as db:
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        r = await client.put(f"/api/admin/kb-domains/{domain.id}", json={
            "key_type": "open", "confirm_name": "研发"})
    assert r.status_code == 400


# ----------------------------------------------------------------------- delete


async def test_delete_domain_cascades_explicitly(admin_client, db_factory):
    """Grants and db links must not outlive the domain (SQLite honours no FK cascade)."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(
            db, "研发", dbs=["fastdb"], members=["u1"], revoked=["u2"]
        )
    async with admin_client as client:
        r = await client.delete(f"/api/admin/kb-domains/{domain.id}")
        assert r.status_code == 200
        assert r.json() == {"id": domain.id, "affected_members": 1}  # active only
        assert (await client.delete(f"/api/admin/kb-domains/{domain.id}")).status_code == 404
        assert (await client.get("/api/admin/kb-domains")).json()["items"] == []
    async with db_factory() as db:
        assert (await db.execute(select(KbDomainGrant))).scalars().all() == []
        assert (await db.execute(select(KbDomainDb))).scalars().all() == []


# ----------------------------------------------------------- domain databases


async def test_db_belongs_to_exactly_one_domain(admin_client, db_factory):
    async with db_factory() as db:
        a = await seed_domain(db, "研发")
        b = await seed_domain(db, "人事")
    async with admin_client as client:
        r = await client.post(f"/api/admin/kb-domains/{a.id}/dbs", json={"kb_name": "fastdb"})
        assert r.status_code == 200 and r.json()["added"] is True

        # Idempotent re-attach to the SAME domain.
        r = await client.post(f"/api/admin/kb-domains/{a.id}/dbs", json={"kb_name": "fastdb"})
        assert r.status_code == 200 and r.json()["added"] is False

        # Another domain claiming it → 409 naming the owner.
        r = await client.post(f"/api/admin/kb-domains/{b.id}/dbs", json={"kb_name": "fastdb"})
        assert r.status_code == 409
        assert "研发" in r.json()["detail"]

        assert (await client.post(f"/api/admin/kb-domains/{a.id}/dbs",
                                  json={"kb_name": "bad name"})).status_code == 400

        items = (await client.get(f"/api/admin/kb-domains/{a.id}/dbs")).json()["items"]
        assert items == [{"kb_name": "fastdb"}]

        r = await client.delete(f"/api/admin/kb-domains/{a.id}/dbs/fastdb")
        assert r.status_code == 200
        r = await client.delete(f"/api/admin/kb-domains/{a.id}/dbs/fastdb")
        assert r.status_code == 404
        assert "未关联" in r.json()["detail"]

        # After detaching, domain b may claim it.
        r = await client.post(f"/api/admin/kb-domains/{b.id}/dbs", json={"kb_name": "fastdb"})
        assert r.status_code == 200 and r.json()["added"] is True


# ---------------------------------------------------------------------- members


async def test_grant_by_uid_then_listed(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        r = await client.post(f"/api/admin/kb-domains/{domain.id}/members", json={"uid": "0001"})
        assert r.status_code == 200
        assert r.json() == {"domain_id": domain.id, "user_id": "u1", "username": "alice"}

        items = (await client.get(f"/api/admin/kb-domains/{domain.id}/members")).json()["items"]
    assert [(m["user_id"], m["username"], m["uid"]) for m in items] == [("u1", "alice", "0001")]
    assert items[0]["created_at"]


async def test_grant_by_username_is_idempotent(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        body = {"username": "alice"}
        assert (await client.post(f"/api/admin/kb-domains/{domain.id}/members", json=body)).status_code == 200
        assert (await client.post(f"/api/admin/kb-domains/{domain.id}/members", json=body)).status_code == 200
    async with db_factory() as db:
        rows = (await db.execute(select(KbDomainGrant))).scalars().all()
    assert len(rows) == 1


async def test_grant_needs_a_known_identifier(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        assert (await client.post(f"/api/admin/kb-domains/{domain.id}/members",
                                  json={})).status_code == 400
        assert (await client.post(f"/api/admin/kb-domains/{domain.id}/members",
                                  json={"uid": "9999"})).status_code == 404
        assert (await client.post(f"/api/admin/kb-domains/nope/members",
                                  json={"uid": "0001"})).status_code == 404


async def test_revoke_soft_deletes_then_regrant_revives(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发", members=["u1"])
    async with admin_client as client:
        assert (await client.delete(
            f"/api/admin/kb-domains/{domain.id}/members/u1")).status_code == 200
        assert (await client.delete(
            f"/api/admin/kb-domains/{domain.id}/members/u1")).status_code == 404
        assert (await client.get(
            f"/api/admin/kb-domains/{domain.id}/members")).json()["items"] == []

        # Re-grant revives the surviving row instead of inserting a second one.
        assert (await client.post(f"/api/admin/kb-domains/{domain.id}/members",
                                  json={"username": "alice"})).status_code == 200

    async with db_factory() as db:
        rows = (await db.execute(select(KbDomainGrant))).scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked_at is None  # revived in place


async def test_public_domain_rejects_roster_writes(admin_client, db_factory):
    """A public domain has no roster by definition — writes must explain why."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "公共", key_type="public")
    async with admin_client as client:
        r = await client.post(f"/api/admin/kb-domains/{domain.id}/members", json={"uid": "0001"})
        assert r.status_code == 400
        assert "公共领域" in r.json()["detail"]

        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/preview",
                              data={"text": "0001"})
        assert r.status_code == 400
        assert "公共领域" in r.json()["detail"]

        # Reading the (empty) roster is fine.
        assert (await client.get(
            f"/api/admin/kb-domains/{domain.id}/members")).json()["items"] == []


# ----------------------------------------------------------------------- import


async def test_import_text_preview_then_commit(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/preview",
                              data={"text": "0001, 0002 9999、alice"})
        assert r.status_code == 200
        preview = r.json()
        assert preview["source"] == "text"
        # 工号 first, username fallback; duplicates collapse; unknown stays verbatim.
        assert sorted(m["user_id"] for m in preview["matched"]) == ["u1", "u2"]
        assert preview["unmatched"] == ["9999"]
        assert preview["already"] == []
        assert preview["domain_id"] == domain.id
        assert preview["domain_name"] == "研发"
        assert preview["preview_token"]

        # Nothing was written by the preview.
        assert (await client.get(
            f"/api/admin/kb-domains/{domain.id}/members")).json()["items"] == []

        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/commit",
                              json={"preview_token": preview["preview_token"]})
        assert r.status_code == 200
        assert r.json() == {"domain_id": domain.id, "granted": 2, "skipped": 0}

        # Token is single-use.
        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/commit",
                              json={"preview_token": preview["preview_token"]})
        assert r.status_code == 400
        assert "过期或无效" in r.json()["detail"]

        members = (await client.get(
            f"/api/admin/kb-domains/{domain.id}/members")).json()["items"]
    assert sorted(m["user_id"] for m in members) == ["u1", "u2"]


async def test_import_preview_buckets_already_members(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(db, "研发", members=["u1"])
    async with admin_client as client:
        preview = (await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            data={"text": "0001\n0002"})).json()
        assert [m["user_id"] for m in preview["already"]] == ["u1"]
        assert [m["user_id"] for m in preview["matched"]] == ["u2"]

        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/commit",
                              json={"preview_token": preview["preview_token"]})
    # Incremental append: the existing member is not re-granted or counted.
    assert r.json() == {"domain_id": domain.id, "granted": 1, "skipped": 0}
    async with db_factory() as db:
        rows = (await db.execute(select(KbDomainGrant))).scalars().all()
    assert len(rows) == 2


async def test_import_commit_rejects_cross_domain_token(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        a = await seed_domain(db, "研发")
        b = await seed_domain(db, "人事")
    async with admin_client as client:
        preview = (await client.post(
            f"/api/admin/kb-domains/{a.id}/import/preview",
            data={"text": "0001"})).json()
        r = await client.post(f"/api/admin/kb-domains/{b.id}/import/commit",
                              json={"preview_token": preview["preview_token"]})
    assert r.status_code == 400
    assert "与目标领域不一致" in r.json()["detail"]
    async with db_factory() as db:
        assert (await db.execute(select(KbDomainGrant))).scalars().all() == []


async def test_import_commit_rejects_expired_token(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        preview = (await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            data={"text": "0001"})).json()
        # Age the entry past the TTL directly in the (test-local) preview store.
        kb_domains._previews[preview["preview_token"]]["created_at"] = (
            time.time() - kb_domains._PREVIEW_TTL_SECONDS - 1
        )
        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/commit",
                              json={"preview_token": preview["preview_token"]})
    assert r.status_code == 400
    assert "过期" in r.json()["detail"]


async def test_import_requires_a_parsable_source(admin_client, db_factory):
    async with db_factory() as db:
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        assert (await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview", data={})).status_code == 400
        r = await client.post(f"/api/admin/kb-domains/{domain.id}/import/preview",
                              data={"text": "  , ; 、 "})
        assert r.status_code == 400
        assert "未解析到任何标识符" in r.json()["detail"]


async def test_import_csv_detects_the_uid_column(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(db, "研发")
    csv_bytes = "姓名,部门,工号\n张三,研发,0001\n李四,测试,0002\n".encode("utf-8-sig")
    async with admin_client as client:
        r = await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            files={"file": ("roster.csv", csv_bytes, "text/csv")},
        )
        assert r.status_code == 200
        preview = r.json()
        assert preview["source"] == "csv"
        meta = preview["source_meta"]
        assert meta["filename"] == "roster.csv"
        assert meta["header_detected"] is True
        assert meta["column_index"] == 2  # the 工号 column, not column 0
        assert sorted(m["user_id"] for m in preview["matched"]) == ["u1", "u2"]
        assert preview["unmatched"] == []


async def test_import_csv_admin_can_repick_the_column(admin_client, db_factory):
    """Explicit column overrides detection — and a wrong pick shows in the preview."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发")
    csv_bytes = "姓名,工号\n张三,0001\n".encode("utf-8")
    async with admin_client as client:
        # Re-pick column 0 (the name column): the header row is read as data,
        # nobody matches — exactly what the preview dialog must surface.
        r = await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            data={"column": "0"},
            files={"file": ("roster.csv", csv_bytes, "text/csv")},
        )
        preview = r.json()
        assert preview["source_meta"]["column_index"] == 0
        assert preview["matched"] == []
        assert preview["unmatched"] == ["姓名", "张三"]

        # Explicit column == detected column still skips the header row.
        r = await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            data={"column": "1"},
            files={"file": ("roster.csv", csv_bytes, "text/csv")},
        )
        preview = r.json()
        assert preview["source_meta"]["header_detected"] is True
        assert [m["uid"] for m in preview["matched"]] == ["0001"]


async def test_import_csv_reads_gbk_encoding(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        domain = await seed_domain(db, "研发")
    csv_bytes = "姓名,工号\n张三,0001\n".encode("gbk")
    async with admin_client as client:
        r = await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            files={"file": ("名单.csv", csv_bytes, "text/csv")},
        )
    assert r.status_code == 200
    assert [m["uid"] for m in r.json()["matched"]] == ["0001"]


async def test_import_xlsx(admin_client, db_factory):
    openpyxl = pytest.importorskip("openpyxl")
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        domain = await seed_domain(db, "研发")
    import io
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["姓名", "工号"])
    ws.append(["张三", "0001"])
    ws.append(["李四", "0002"])
    buf = io.BytesIO()
    wb.save(buf)
    async with admin_client as client:
        r = await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            files={"file": ("roster.xlsx", buf.getvalue(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 200
        preview = r.json()
        assert preview["source"] == "xlsx"
        assert preview["source_meta"]["header_detected"] is True
        assert preview["source_meta"]["column_index"] == 1
        assert sorted(m["user_id"] for m in preview["matched"]) == ["u1", "u2"]


async def test_import_rejects_other_file_types(admin_client, db_factory):
    async with db_factory() as db:
        domain = await seed_domain(db, "研发")
    async with admin_client as client:
        r = await client.post(
            f"/api/admin/kb-domains/{domain.id}/import/preview",
            files={"file": ("roster.txt", b"0001", "text/plain")},
        )
    assert r.status_code == 400
    assert ".csv" in r.json()["detail"]


# ---------------------------------------------------------------- permission UI


async def test_kb_users_lists_every_user(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002", role="admin")
    async with admin_client as client:
        items = (await client.get("/api/admin/kb-users")).json()["items"]
    assert items == [
        {"user_id": "u1", "username": "alice", "uid": "0001", "role": "user"},
        {"user_id": "u2", "username": "bob", "uid": "0002", "role": "admin"},
    ]


# -------------------------------------------------------------------- user side


CATALOG = [
    {"name": "fastdb", "description": "源码库", "uri": "/data/fastdb", "dimension": 1024},
    {"name": "devdb", "description": "研发文档", "uri": "/data/devdb", "dimension": 1024},
    {"name": "hrdb", "description": "人事", "uri": "/data/hrdb", "dimension": 1024},
]


async def test_my_domains_aggregates_only_accessible_domains(
    app_client_factory, db_factory, mock_httpx, monkeypatch
):
    """Public domains + granted private domains; empty and foreign ones hidden."""
    monkeypatch.setattr(settings, "kb_catalog_key", "sk-admin")
    async with db_factory() as db:
        await seed_domain(db, "公共", key_type="public", dbs=["fastdb"])
        await seed_domain(db, "研发", key_type="private", dbs=["devdb"], members=["u1"])
        await seed_domain(db, "人事", key_type="private", dbs=["hrdb"], members=["u2"])
        await seed_domain(db, "空领域", key_type="private")  # no dbs → hidden

    calls = mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_domains.user_router], user_id="u1")
    async with client:
        r = await client.get("/api/kb/my-domains")
    assert r.status_code == 200
    domains = {d["name"]: d for d in r.json()["domains"]}
    assert set(domains) == {"公共", "研发"}  # 人事 belongs to u2, 空领域 has no dbs
    assert domains["公共"]["key_type"] == "public"
    assert domains["公共"]["databases"] == [{"name": "fastdb", "description": "源码库"}]
    assert domains["研发"]["databases"] == [{"name": "devdb", "description": "研发文档"}]
    # The catalog listing is read with the platform key — it buys descriptions only.
    assert calls[0].headers["x-api-key"] == "sk-admin"


async def test_my_domains_without_access_skips_the_catalog_call(
    app_client_factory, db_factory, mock_httpx
):
    async with db_factory() as db:
        await seed_domain(db, "人事", key_type="private", dbs=["hrdb"], members=["u2"])
    calls = mock_httpx(lambda request: httpx.Response(200, json=CATALOG))
    client = app_client_factory([kb_domains.user_router], user_id="u1")
    async with client:
        r = await client.get("/api/kb/my-domains")
    assert r.json() == {"domains": []}
    assert calls == []  # nothing allowed → no upstream call at all


async def test_my_domains_fails_soft_when_catalog_denied(
    app_client_factory, db_factory, mock_httpx, monkeypatch
):
    """A stale AGENT_KB_CATALOG_KEY blanks descriptions, not the panel."""
    monkeypatch.setattr(settings, "kb_catalog_key", "sk-stale")
    async with db_factory() as db:
        await seed_domain(db, "公共", key_type="public", dbs=["fastdb"])
    mock_httpx(lambda request: httpx.Response(401, json={"detail": "invalid api key"}))
    client = app_client_factory([kb_domains.user_router], user_id="u1")
    async with client:
        r = await client.get("/api/kb/my-domains")
    assert r.status_code == 200
    assert r.json()["domains"][0]["databases"] == [{"name": "fastdb", "description": ""}]


async def test_my_domains_fails_soft_on_connection_error(
    app_client_factory, db_factory, mock_httpx
):
    async with db_factory() as db:
        await seed_domain(db, "公共", key_type="public", dbs=["fastdb"])

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    mock_httpx(handler)
    client = app_client_factory([kb_domains.user_router], user_id="u1")
    async with client:
        r = await client.get("/api/kb/my-domains")
    assert r.status_code == 200
    assert r.json()["domains"][0]["databases"] == [{"name": "fastdb", "description": ""}]


# ------------------------------------------------------------------------ audit


async def test_writes_are_audited_without_leaking_the_key(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
    async with admin_client as client:
        domain_id = await create_via_api(client, "研发", api_key="sk-super-secret")
        await client.post(f"/api/admin/kb-domains/{domain_id}/dbs", json={"kb_name": "fastdb"})
        await client.post(f"/api/admin/kb-domains/{domain_id}/members", json={"uid": "0001"})
        preview = (await client.post(
            f"/api/admin/kb-domains/{domain_id}/import/preview",
            data={"text": "0001"})).json()
        await client.post(f"/api/admin/kb-domains/{domain_id}/import/commit",
                          json={"preview_token": preview["preview_token"]})
        await client.delete(f"/api/admin/kb-domains/{domain_id}/members/u1")
        await client.put(f"/api/admin/kb-domains/{domain_id}", json={
            "key_type": "public", "confirm_name": "研发"})
        await client.delete(f"/api/admin/kb-domains/{domain_id}")

    async with db_factory() as db:
        events = (await db.execute(select(AuditEvent))).scalars().all()
    assert {e.action for e in events} == {
        "kbdomain.create", "kbdomain.db_add", "kbdomain.member_grant",
        "kbdomain.member_import", "kbdomain.member_revoke",
        "kbdomain.type_switch", "kbdomain.delete",
    }
    assert all(e.action.startswith("kbdomain.") for e in events)
    assert all("sk-super-secret" not in e.detail for e in events)
    assert all(e.user_id == "admin1" for e in events)
    import_detail = json.loads(
        next(e.detail for e in events if e.action == "kbdomain.member_import")
    )
    assert import_detail["source"] == "text"
    # The preview already bucketed u1 as a member, so the token carried nobody.
    assert import_detail["granted"] == 0 and import_detail["skipped"] == 0
