"""Integration tests for the per-user config HTTP API (ownership isolation)."""
import json

from app.models import User
from app.services import user_config as uc


async def test_mcp_full_crud_flow(client_factory):
    client = await client_factory("u1", "alice")
    async with client:
        # Create
        r = await client.post(
            "/api/user-config/mcp",
            json={
                "name": "demo",
                "type": "remote",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer topsecret"},
            },
        )
        assert r.status_code == 201
        created = r.json()
        assert created["name"] == "demo"
        assert created["type"] == "remote"
        assert created["url"] == "https://example.com/mcp"
        assert created["hasHeaders"] is True
        assert "Authorization" not in r.text

        server_id = created["id"]

        # List
        r = await client.get("/api/user-config/mcp")
        assert r.status_code == 200
        assert [s["id"] for s in r.json()["mcp"]] == [server_id]

        # Get by id
        r = await client.get(f"/api/user-config/mcp/{server_id}")
        assert r.status_code == 200
        assert r.json()["id"] == server_id

        # Update (patch enabled only; secrets preserved)
        r = await client.patch(f"/api/user-config/mcp/{server_id}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["hasHeaders"] is True

        # Delete
        r = await client.delete(f"/api/user-config/mcp/{server_id}")
        assert r.status_code == 204

        r = await client.get("/api/user-config/mcp")
        assert r.json()["mcp"] == []


async def test_llm_full_crud_flow_and_masking(client_factory):
    client = await client_factory("u1", "alice")
    async with client:
        r = await client.post(
            "/api/user-config/llm",
            json={
                "provider_id": "my-openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-very-secret",
                "models": {"gpt-4o": {}},
            },
        )
        assert r.status_code == 201
        created = r.json()
        assert created["provider_id"] == "my-openai"
        assert created["baseURL"] == "https://api.openai.com/v1"
        assert created["hasApiKey"] is True
        assert created["models"] == {"gpt-4o": {}}
        assert "sk-very-secret" not in r.text

        config_id = created["id"]

        r = await client.get("/api/user-config/llm")
        assert r.status_code == 200
        assert len(r.json()["providers"]) == 1

        r = await client.patch(
            f"/api/user-config/llm/{config_id}", json={"api_key": ""}
        )
        assert r.status_code == 200
        assert r.json()["hasApiKey"] is False

        r = await client.delete(f"/api/user-config/llm/{config_id}")
        assert r.status_code == 204


async def test_user_cannot_access_other_users_content(client_factory):
    alice = await client_factory("u1", "alice")
    async with alice:
        r = await alice.post(
            "/api/user-config/mcp",
            json={"name": "priv", "type": "remote", "url": "https://x"},
        )
        mcp_id = r.json()["id"]
        r = await alice.post(
            "/api/user-config/llm", json={"provider_id": "privp", "api_key": "k"}
        )
        llm_id = r.json()["id"]

    bob = await client_factory("u2", "bob")
    async with bob:
        assert (await bob.get("/api/user-config/mcp")).json()["mcp"] == []
        assert (await bob.get("/api/user-config/llm")).json()["providers"] == []
        assert (await bob.get(f"/api/user-config/mcp/{mcp_id}")).status_code == 404
        assert (await bob.get(f"/api/user-config/llm/{llm_id}")).status_code == 404
        assert (
            await bob.patch(f"/api/user-config/mcp/{mcp_id}", json={"enabled": False})
        ).status_code == 404
        assert (
            await bob.delete(f"/api/user-config/mcp/{mcp_id}")
        ).status_code == 404
        assert (await bob.delete(f"/api/user-config/llm/{llm_id}")).status_code == 404


async def test_duplicate_name_returns_409(client_factory):
    client = await client_factory("u1", "alice")
    async with client:
        payload = {"name": "demo", "type": "remote", "url": "https://x"}
        assert (await client.post("/api/user-config/mcp", json=payload)).status_code == 201
        assert (await client.post("/api/user-config/mcp", json=payload)).status_code == 409


async def test_invalid_payload_returns_400(client_factory):
    client = await client_factory("u1", "alice")
    async with client:
        # Bad name
        r = await client.post(
            "/api/user-config/mcp",
            json={"name": "Bad Name", "type": "remote", "url": "https://x"},
        )
        assert r.status_code == 400

        # local MCP without command
        r = await client.post(
            "/api/user-config/mcp", json={"name": "local", "type": "local"}
        )
        assert r.status_code == 400


async def test_missing_required_field_returns_422(client_factory):
    client = await client_factory("u1", "alice")
    async with client:
        r = await client.post("/api/user-config/llm", json={"api_key": "k"})
        assert r.status_code == 422


async def test_active_llm_get_put_flow(client_factory, db_factory):
    # Seed a real User row (set_active_llm resolves the user from the DB) plus
    # a provider owned by that user.
    async with db_factory() as db:
        db.add(User(id="u1", username="alice", hashed_password="x"))
        await db.commit()
        await uc.create_llm(db, "u1", {"provider_id": "p1", "api_key": "k"})

    client = await client_factory("u1", "alice")
    async with client:
        # No selection yet.
        r = await client.get("/api/user-config/active-llm")
        assert r.status_code == 200
        assert r.json() == {"provider_id": None, "model": None}

        r = await client.put(
            "/api/user-config/active-llm", json={"provider_id": "p1", "model": "m1"}
        )
        assert r.status_code == 200
        assert r.json() == {"provider_id": "p1", "model": "m1"}

        r = await client.get("/api/user-config/active-llm")
        assert r.json() == {"provider_id": "p1", "model": "m1"}

        # Selecting a provider the user does not own is rejected.
        r = await client.put(
            "/api/user-config/active-llm", json={"provider_id": "other"}
        )
        assert r.status_code == 400

        # Clearing the selection.
        r = await client.put(
            "/api/user-config/active-llm", json={"provider_id": None}
        )
        assert r.status_code == 200
        assert r.json() == {"provider_id": None, "model": None}