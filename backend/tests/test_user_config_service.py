"""Service-layer tests: encryption-at-rest, validation, and ownership scoping."""
import json

import pytest
from sqlalchemy import select

from app import crypto
from app.models import User, UserLLMProvider, UserMcpServer
from app.services import opencode_config, user_config as uc


async def test_mcp_encrypts_sensitive_fields_at_rest(db_factory):
    async with db_factory() as db:
        server = await uc.create_mcp(
            db,
            "user-1",
            {
                "name": "demo",
                "type": "remote",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer token"},
                "environment": {"NOPE": "should-not-leak"},
            },
        )

    async with db_factory() as db:
        row = (await db.execute(select(UserMcpServer))).scalar_one()
        assert row.name == "demo"
        assert row.type == "remote"
        # config blob is encrypted and contains no plaintext secrets.
        assert "Bearer" not in row.config_enc
        assert "should-not-leak" not in row.config_enc
        decrypted = crypto.decrypt_json(row.config_enc)
        assert decrypted["headers"] == {"Authorization": "Bearer token"}
        assert decrypted["environment"] == {"NOPE": "should-not-leak"}


async def test_llm_encrypts_api_key_at_rest(db_factory):
    async with db_factory() as db:
        await uc.create_llm(
            db,
            "user-1",
            {
                "provider_id": "my-openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-super-secret",
                "models": {"gpt-4o": {}},
            },
        )

    async with db_factory() as db:
        row = (await db.execute(select(UserLLMProvider))).scalar_one()
        assert "sk-super-secret" not in row.api_key_enc
        assert crypto.decrypt_secret(row.api_key_enc) == "sk-super-secret"
        assert "https://api.openai.com/v1" not in row.base_url_enc
        assert crypto.decrypt_secret(row.base_url_enc) == "https://api.openai.com/v1"


async def test_ownership_scoping_returns_none_for_other_user(db_factory):
    async with db_factory() as db:
        mcp = await uc.create_mcp(
            db, "user-1", {"name": "demo", "type": "remote", "url": "https://x"}
        )
        llm = await uc.create_llm(db, "user-1", {"provider_id": "p1", "api_key": "k"})

    async with db_factory() as db:
        assert await uc.get_mcp(db, "user-2", mcp.id) is None
        assert await uc.get_llm(db, "user-2", llm.id) is None
        assert await uc.update_mcp(db, "user-2", mcp.id, {"enabled": False}) is None
        assert await uc.update_llm(db, "user-2", llm.id, {"name": "hacked"}) is None
        assert await uc.delete_mcp(db, "user-2", mcp.id) is False
        assert await uc.delete_llm(db, "user-2", llm.id) is False


async def test_update_preserves_omitted_secrets(db_factory):
    async with db_factory() as db:
        server = await uc.create_mcp(
            db,
            "user-1",
            {
                "name": "demo",
                "type": "remote",
                "url": "https://x",
                "headers": {"Authorization": "Bearer keepme"},
            },
        )
        # Update only `enabled` — headers must be preserved.
        updated = await uc.update_mcp(db, "user-1", server.id, {"enabled": False})
        assert updated is not None
        assert updated.enabled is False

    async with db_factory() as db:
        row = (await db.execute(select(UserMcpServer))).scalar_one()
        assert crypto.decrypt_json(row.config_enc)["headers"] == {
            "Authorization": "Bearer keepme"
        }


async def test_duplicate_name_raises(db_factory):
    async with db_factory() as db:
        await uc.create_mcp(db, "user-1", {"name": "demo", "type": "remote", "url": "https://x"})
        with pytest.raises(uc.DuplicateError):
            await uc.create_mcp(
                db, "user-1", {"name": "demo", "type": "remote", "url": "https://y"}
            )


async def test_same_name_across_users_is_allowed(db_factory):
    async with db_factory() as db:
        a = await uc.create_mcp(
            db, "user-1", {"name": "demo", "type": "remote", "url": "https://x"}
        )
        b = await uc.create_mcp(
            db, "user-2", {"name": "demo", "type": "remote", "url": "https://x"}
        )
    assert a.id != b.id


async def test_invalid_input_raises_value_error(db_factory):
    async with db_factory() as db:
        with pytest.raises(ValueError):
            await uc.create_mcp(db, "user-1", {"name": "Bad Name", "type": "remote", "url": "https://x"})
        with pytest.raises(ValueError):
            await uc.create_mcp(db, "user-1", {"name": "local", "type": "local"})  # missing command
        with pytest.raises(ValueError):
            await uc.create_mcp(db, "user-1", {"name": "remote", "type": "remote"})  # missing url
        with pytest.raises(ValueError):
            await uc.create_llm(db, "user-1", {"provider_id": ""})


# ------------------------------------------------------------------
#  Active LLM selection & container-config merge
# ------------------------------------------------------------------

async def _seed_user(db, user_id: str, username: str = "alice") -> None:
    db.add(User(id=user_id, username=username, hashed_password="x"))
    await db.commit()


async def test_active_llm_set_get_and_clear(db_factory):
    async with db_factory() as db:
        await _seed_user(db, "user-1")
        await uc.create_llm(db, "user-1", {"provider_id": "my-openai", "models": {"gpt-4o": {}}})

        assert await uc.get_active_llm(db, "user-1") is None

        result = await uc.set_active_llm(db, "user-1", "my-openai", "gpt-4o")
        assert result == {"provider_id": "my-openai", "model": "gpt-4o"}

        assert await uc.get_active_llm(db, "user-1") == {
            "provider_id": "my-openai",
            "model": "gpt-4o",
        }

        cleared = await uc.set_active_llm(db, "user-1", None)
        assert cleared == {"provider_id": None, "model": None}
        assert await uc.get_active_llm(db, "user-1") is None


async def test_set_active_llm_requires_owned_provider(db_factory):
    async with db_factory() as db:
        await _seed_user(db, "user-1")
        await _seed_user(db, "user-2", username="bob")
        await uc.create_llm(db, "user-1", {"provider_id": "p1"})

        # Another user's provider id does not resolve for this user.
        with pytest.raises(ValueError):
            await uc.set_active_llm(db, "user-2", "p1")
        # Unknown provider id.
        with pytest.raises(ValueError):
            await uc.set_active_llm(db, "user-1", "missing")


async def test_build_user_config_json_selects_active_model(db_factory, monkeypatch):
    # Isolate from the host opencode.json so the assertion is deterministic.
    monkeypatch.setattr(opencode_config, "load_source_config", lambda: ({}, "test"))

    async with db_factory() as db:
        await _seed_user(db, "user-1")
        await uc.create_llm(
            db,
            "user-1",
            {
                "provider_id": "my-openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-x",
                "models": {"gpt-4o": {}},
            },
        )
        await uc.set_active_llm(db, "user-1", "my-openai", "gpt-4o")

    async with db_factory() as db:
        config = json.loads(await uc.build_user_config_json(db, "user-1"))

    assert config["model"] == "my-openai/gpt-4o"
    assert "my-openai" in config["provider"]
    # User providers route through the user-scoped LLM proxy path.
    assert config["provider"]["my-openai"]["options"]["baseURL"].endswith(
        "/_user/user-1/my-openai"
    )