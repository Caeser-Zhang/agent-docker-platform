"""Admin surface for KB credentials and the whitelist.

Two properties matter here and get their own tests: credentials are write-only
(no endpoint ever echoes one, and the stored column is encrypted), and deleting
a credential takes its grants with it — SQLite does not honour the FK's
ON DELETE CASCADE without foreign_keys=ON, so the route must do it explicitly.
"""
import pytest
from sqlalchemy import select

from app import crypto
from app.models import AuditEvent, KbGrant, KbKey, User
from app.routers import kb_keys
from app.services import audit


async def add_user(db, *, id: str, username: str, uid: str, role: str = "user"):
    db.add(User(id=id, username=username, uid=uid, hashed_password="x", role=role))
    await db.commit()


@pytest.fixture
def admin_client(app_client_factory, db_factory, monkeypatch):
    """Client authenticated as an admin, with audit writes aimed at the test DB."""
    monkeypatch.setattr(audit, "async_session", db_factory)
    return app_client_factory(
        [kb_keys.router, kb_keys.user_router], user_id="admin1", username="admin", role="admin"
    )


# ------------------------------------------------------------------- admin gate


async def test_admin_routes_reject_a_normal_user(app_client_factory):
    client = app_client_factory([kb_keys.router], user_id="u1", role="user")
    async with client:
        assert (await client.get("/api/admin/kb-keys")).status_code == 403
        assert (await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "sk"})).status_code == 403
        assert (await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb", "uid": "1"})).status_code == 403


# ------------------------------------------------------------------ credentials


async def test_key_is_write_only(admin_client, db_factory):
    async with admin_client as client:
        r = await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "sk-super-secret"})
        assert r.status_code == 200
        assert r.json() == {"kb_name": "fastdb", "action": "kbkey.create"}
        assert "sk-super-secret" not in r.text

        r = await client.get("/api/admin/kb-keys")
    items = r.json()["items"]
    assert items[0]["kb_name"] == "fastdb"
    assert items[0]["has_api_key"] is True
    assert "sk-super-secret" not in r.text  # a stolen admin session learns nothing

    async with db_factory() as db:
        row = (await db.execute(select(KbKey).where(KbKey.kb_name == "fastdb"))).scalar_one()
    assert row.api_key_enc != "sk-super-secret"
    assert crypto.decrypt_secret(row.api_key_enc) == "sk-super-secret"


async def test_key_can_be_rotated_in_place(admin_client, db_factory):
    async with admin_client as client:
        await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "old"})
        r = await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "new"})
        assert r.json()["action"] == "kbkey.rotate"

    async with db_factory() as db:
        rows = (await db.execute(select(KbKey))).scalars().all()
    assert len(rows) == 1
    assert crypto.decrypt_secret(rows[0].api_key_enc) == "new"


async def test_invalid_database_names_are_rejected(admin_client):
    async with admin_client as client:
        assert (await client.put("/api/admin/kb-keys/bad%20name", json={"api_key": "sk"})).status_code == 400
        assert (await client.put("/api/admin/kb-keys/fastdb", json={"api_key": ""})).status_code == 422


# ----------------------------------------------------------------------- grants


async def test_grant_requires_a_recorded_credential(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
    async with admin_client as client:
        r = await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb", "uid": "0001"})
    assert r.status_code == 400
    assert "尚未录入凭据" in r.json()["detail"]  # tells the admin the exact next call


async def test_grant_by_uid_then_listed_with_the_username(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
    async with admin_client as client:
        await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "sk"})
        r = await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb", "uid": "0001"})
        assert r.status_code == 200
        assert r.json() == {"kb_name": "fastdb", "user_id": "u1", "username": "alice"}

        r = await client.get("/api/admin/kb-grants")
    (item,) = r.json()["items"]
    assert item["kb_name"] == "fastdb"
    assert item["user_id"] == "u1"
    assert item["username"] == "alice"
    assert item["uid"] == "0001"
    assert item["created_at"]


async def test_grant_by_username_is_idempotent(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
    async with admin_client as client:
        await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "sk"})
        body = {"kb_name": "fastdb", "username": "alice"}
        assert (await client.post("/api/admin/kb-grants", json=body)).status_code == 200
        assert (await client.post("/api/admin/kb-grants", json=body)).status_code == 200

    async with db_factory() as db:
        rows = (await db.execute(select(KbGrant))).scalars().all()
    assert len(rows) == 1


async def test_grant_needs_a_known_identifier(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
    async with admin_client as client:
        await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "sk"})
        assert (await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb"})).status_code == 400
        r = await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb", "uid": "9999"})
    assert r.status_code == 404


async def test_grants_can_be_filtered(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        db.add_all([
            KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk1")),
            KbKey(kb_name="hr_only", api_key_enc=crypto.encrypt_secret("sk2")),
            KbGrant(user_id="u1", kb_name="fastdb"),
            KbGrant(user_id="u2", kb_name="hr_only"),
        ])
        await db.commit()
    async with admin_client as client:
        all_rows = (await client.get("/api/admin/kb-grants")).json()["items"]
        by_db = (await client.get("/api/admin/kb-grants", params={"kb_name": "hr_only"})).json()["items"]
        by_user = (await client.get("/api/admin/kb-grants", params={"user_id": "u1"})).json()["items"]
    assert [(r["username"], r["kb_name"]) for r in all_rows] == [("alice", "fastdb"), ("bob", "hr_only")]
    assert [r["username"] for r in by_db] == ["bob"]
    assert [r["kb_name"] for r in by_user] == ["fastdb"]


async def test_revoke_removes_the_grant(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        db.add(KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk")))
        db.add(KbGrant(user_id="u1", kb_name="fastdb"))
        await db.commit()
    async with admin_client as client:
        assert (await client.delete("/api/admin/kb-grants/u1/fastdb")).status_code == 200
        assert (await client.delete("/api/admin/kb-grants/u1/fastdb")).status_code == 404
        # Soft delete: the row survives with revoked_at stamped, and it no longer
        # shows up in the active whitelist.
        listed = (await client.get("/api/admin/kb-grants")).json()["items"]

    async with db_factory() as db:
        rows = (await db.execute(select(KbGrant))).scalars().all()
        assert len(rows) == 1
        assert rows[0].revoked_at is not None
    assert listed == []


async def test_regrant_revives_the_soft_deleted_row(admin_client, db_factory):
    """Re-granting a revoked database updates the surviving row, never duplicates it."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        db.add(KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk")))
        db.add(KbGrant(user_id="u1", kb_name="fastdb"))
        await db.commit()
    async with admin_client as client:
        assert (await client.delete("/api/admin/kb-grants/u1/fastdb")).status_code == 200
        assert (await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb", "username": "alice"})).status_code == 200
        assert [r["kb_name"] for r in (await client.get("/api/admin/kb-grants")).json()["items"]] == ["fastdb"]

    async with db_factory() as db:
        rows = (await db.execute(select(KbGrant))).scalars().all()
    assert len(rows) == 1  # revived in place, not re-inserted
    assert rows[0].revoked_at is None


async def test_deleting_a_credential_revokes_its_grants(admin_client, db_factory):
    """No grant may outlive the credential it depended on."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        db.add(KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk")))
        db.add(KbGrant(user_id="u1", kb_name="fastdb"))
        db.add(KbGrant(user_id="u2", kb_name="fastdb"))
        await db.commit()

    async with admin_client as client:
        r = await client.delete("/api/admin/kb-keys/fastdb")
        assert r.status_code == 200
        assert r.json()["revoked_users"] == ["u1", "u2"]
        assert (await client.get("/api/admin/kb-keys")).json()["items"] == []
        assert (await client.get("/api/admin/kb-grants")).json()["items"] == []
        assert (await client.delete("/api/admin/kb-keys/fastdb")).status_code == 404

    async with db_factory() as db:
        assert (await db.execute(select(KbGrant))).scalars().all() == []


# ------------------------------------------------------------- permission matrix


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


async def test_kb_user_access_splits_granted_from_available(admin_client, db_factory):
    """Granted = active rows only; available = every other recorded credential."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        db.add_all([
            KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk1")),
            KbKey(kb_name="hr_only", api_key_enc=crypto.encrypt_secret("sk2")),
            KbGrant(user_id="u1", kb_name="fastdb"),
        ])
        await db.commit()
    async with admin_client as client:
        r = await client.get("/api/admin/kb-user-access", params={"user_id": "u1"})
        assert r.status_code == 200
        body = r.json()
        assert (await client.get("/api/admin/kb-user-access", params={"user_id": "nope"})).status_code == 404

    assert body["user_id"] == "u1"
    assert body["username"] == "alice"
    assert [g["kb_name"] for g in body["granted"]] == ["fastdb"]
    assert body["available"] == [{"kb_name": "hr_only", "has_api_key": True}]


async def test_kb_user_access_hides_revoked_from_granted(admin_client, db_factory):
    """A revoked row drops out of granted and re-appears as available."""
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        db.add(KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk")))
        db.add(KbGrant(user_id="u1", kb_name="fastdb"))
        await db.commit()
    async with admin_client as client:
        await client.delete("/api/admin/kb-grants/u1/fastdb")
        body = (await client.get("/api/admin/kb-user-access", params={"user_id": "u1"})).json()
    assert body["granted"] == []
    assert [a["kb_name"] for a in body["available"]] == ["fastdb"]


# --------------------------------------------------------------------- user side


async def test_my_databases_lists_only_own_grants(app_client_factory, db_factory, monkeypatch):
    monkeypatch.setattr(audit, "async_session", db_factory)
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
        await add_user(db, id="u2", username="bob", uid="0002")
        db.add_all([
            KbKey(kb_name="fastdb", api_key_enc=crypto.encrypt_secret("sk1")),
            KbKey(kb_name="vl_test", api_key_enc=crypto.encrypt_secret("sk2")),
            KbGrant(user_id="u1", kb_name="vl_test"),
            KbGrant(user_id="u1", kb_name="fastdb"),
            KbGrant(user_id="u2", kb_name="fastdb"),  # someone else's grant
        ])
        await db.commit()

    client = app_client_factory([kb_keys.user_router], user_id="u1")
    async with client:
        r = await client.get("/api/kb/my-databases")
    assert r.status_code == 200
    assert r.json() == {"databases": ["fastdb", "vl_test"]}  # names only, sorted


# ------------------------------------------------------------------------ audit


async def test_writes_are_audited_without_leaking_the_key(admin_client, db_factory):
    async with db_factory() as db:
        await add_user(db, id="u1", username="alice", uid="0001")
    async with admin_client as client:
        await client.put("/api/admin/kb-keys/fastdb", json={"api_key": "sk-super-secret"})
        await client.post("/api/admin/kb-grants", json={"kb_name": "fastdb", "uid": "0001"})
        await client.delete("/api/admin/kb-grants/u1/fastdb")

    async with db_factory() as db:
        events = (await db.execute(select(AuditEvent))).scalars().all()
    assert {e.action for e in events} == {"kbkey.create", "kbgrant.grant", "kbgrant.revoke"}
    assert all("sk-super-secret" not in e.detail for e in events)
    assert all(e.user_id == "admin1" for e in events)
