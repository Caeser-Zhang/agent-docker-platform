"""Per-user custom MCP server and LLM provider storage.

Implements the ownership isolation layer: every query is scoped by the
authenticated user's ``user_id``, so a user can only read/write/delete rows
they created. Sensitive fields (MCP headers/environment, LLM api key/base URL/
models) are encrypted at rest via :mod:`app.crypto`; the serialization helpers
mask secrets so the HTTP layer never returns plaintext credentials.

All functions take the caller's ``user_id`` plus a request-scoped async
session. Validation errors raise :class:`ValueError`; a unique-constraint
collision raises :class:`DuplicateError`; "row does not exist or is not owned
by this user" returns ``None`` (so the router can return a uniform 404 without
leaking whether another user owns a matching id).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crypto
from ..models import User, UserLLMProvider, UserMcpServer
from . import opencode_config

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class DuplicateError(Exception):
    """Raised when a create/update violates a user-scoped unique constraint."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_name(name: str | None) -> str:
    if not name or len(name) > 100 or not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid name '{name}': must be 1-100 lowercase alphanumeric with single hyphens"
        )
    return name


def _validate_provider_id(provider_id: str | None) -> str:
    if not provider_id or len(provider_id) > 100 or not _NAME_RE.match(provider_id):
        raise ValueError(
            f"Invalid provider_id '{provider_id}': must be 1-100 lowercase alphanumeric with single hyphens"
        )
    return provider_id


# ------------------------------------------------------------------
#  MCP serialization helpers
# ------------------------------------------------------------------

def _mcp_secret_config(data: dict[str, Any]) -> dict[str, Any]:
    """Project a payload down to the encryptable MCP config fields."""
    cfg: dict[str, Any] = {}
    for key in ("command", "url", "headers", "environment", "cwd", "timeout"):
        if key in data:
            value = data[key]
            if key in ("headers", "environment") and not value:
                value = {}
            cfg[key] = value
    return cfg


def _validate_mcp_config(type_: str, cfg: dict[str, Any]) -> None:
    if type_ == "local" and not cfg.get("command"):
        raise ValueError("Local MCP server requires 'command' array")
    if type_ == "remote" and not cfg.get("url"):
        raise ValueError("Remote MCP server requires 'url'")


def serialize_mcp(server: UserMcpServer) -> dict[str, Any]:
    """Masked, JSON-safe view of a user MCP server (no plaintext secrets)."""
    cfg = crypto.decrypt_json(server.config_enc)
    entry: dict[str, Any] = {
        "id": server.id,
        "name": server.name,
        "type": server.type,
        "enabled": server.enabled,
        "created_at": server.created_at.isoformat(),
        "updated_at": server.updated_at.isoformat(),
    }
    if server.type == "remote":
        entry["url"] = cfg.get("url")
        entry["hasHeaders"] = bool(cfg.get("headers"))
        if "timeout" in cfg:
            entry["timeout"] = cfg["timeout"]
    else:
        entry["command"] = cfg.get("command")
        entry["hasEnv"] = bool(cfg.get("environment"))
        if "cwd" in cfg:
            entry["cwd"] = cfg["cwd"]
        if "timeout" in cfg:
            entry["timeout"] = cfg["timeout"]
    return entry


# ------------------------------------------------------------------
#  MCP CRUD (ownership-scoped)
# ------------------------------------------------------------------

async def list_mcp(db: AsyncSession, user_id: str) -> list[UserMcpServer]:
    result = await db.execute(
        select(UserMcpServer)
        .where(UserMcpServer.user_id == user_id)
        .order_by(UserMcpServer.created_at)
    )
    return list(result.scalars().all())


async def get_mcp(db: AsyncSession, user_id: str, server_id: str) -> UserMcpServer | None:
    result = await db.execute(
        select(UserMcpServer).where(
            UserMcpServer.id == server_id, UserMcpServer.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def create_mcp(db: AsyncSession, user_id: str, data: dict[str, Any]) -> UserMcpServer:
    name = _validate_name(data.get("name"))
    type_ = data.get("type")
    if type_ not in ("local", "remote"):
        raise ValueError("MCP type must be 'local' or 'remote'")

    cfg = _mcp_secret_config(data)
    _validate_mcp_config(type_, cfg)

    server = UserMcpServer(
        user_id=user_id,
        name=name,
        type=type_,
        enabled=bool(data.get("enabled", True)),
        config_enc=crypto.encrypt_json(cfg),
    )
    db.add(server)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError(f"MCP server '{name}' already exists") from exc
    await db.refresh(server)
    return server


async def update_mcp(
    db: AsyncSession, user_id: str, server_id: str, data: dict[str, Any]
) -> UserMcpServer | None:
    server = await get_mcp(db, user_id, server_id)
    if server is None:
        return None

    if "name" in data and data["name"] != server.name:
        server.name = _validate_name(data["name"])
    if "type" in data:
        if data["type"] not in ("local", "remote"):
            raise ValueError("MCP type must be 'local' or 'remote'")
        server.type = data["type"]
    if "enabled" in data:
        server.enabled = bool(data["enabled"])

    if any(k in data for k in ("command", "url", "headers", "environment", "cwd", "timeout")):
        merged = {**crypto.decrypt_json(server.config_enc), **_mcp_secret_config(data)}
        _validate_mcp_config(server.type, merged)
        server.config_enc = crypto.encrypt_json(merged)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError(f"MCP server '{server.name}' already exists") from exc
    await db.refresh(server)
    return server


async def delete_mcp(db: AsyncSession, user_id: str, server_id: str) -> bool:
    server = await get_mcp(db, user_id, server_id)
    if server is None:
        return False
    await db.delete(server)
    await db.commit()
    return True


# ------------------------------------------------------------------
#  LLM provider CRUD (ownership-scoped)
# ------------------------------------------------------------------

def serialize_provider(provider: UserLLMProvider) -> dict[str, Any]:
    """Masked, JSON-safe view of a user LLM provider (no plaintext api key)."""
    models = crypto.decrypt_json(provider.models_enc)
    return {
        "id": provider.id,
        "provider_id": provider.provider_id,
        "name": provider.name,
        "npm": provider.npm,
        "baseURL": crypto.decrypt_secret(provider.base_url_enc),
        "hasApiKey": bool(provider.api_key_enc),
        "models": models,
        "created_at": provider.created_at.isoformat(),
        "updated_at": provider.updated_at.isoformat(),
    }


async def list_llm(db: AsyncSession, user_id: str) -> list[UserLLMProvider]:
    result = await db.execute(
        select(UserLLMProvider)
        .where(UserLLMProvider.user_id == user_id)
        .order_by(UserLLMProvider.created_at)
    )
    return list(result.scalars().all())


async def get_llm(db: AsyncSession, user_id: str, config_id: str) -> UserLLMProvider | None:
    result = await db.execute(
        select(UserLLMProvider).where(
            UserLLMProvider.id == config_id, UserLLMProvider.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def create_llm(db: AsyncSession, user_id: str, data: dict[str, Any]) -> UserLLMProvider:
    provider_id = _validate_provider_id(data.get("provider_id"))

    models = data.get("models")
    models_enc = crypto.encrypt_json(models) if isinstance(models, dict) else ""

    provider = UserLLMProvider(
        user_id=user_id,
        provider_id=provider_id,
        name=data.get("name"),
        npm=data.get("npm") or "@ai-sdk/openai-compatible",
        base_url_enc=crypto.encrypt_secret(data.get("base_url")),
        api_key_enc=crypto.encrypt_secret(data.get("api_key")),
        models_enc=models_enc,
    )
    db.add(provider)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError(
            f"LLM provider '{provider_id}' already exists"
        ) from exc
    await db.refresh(provider)
    return provider


async def update_llm(
    db: AsyncSession, user_id: str, config_id: str, data: dict[str, Any]
) -> UserLLMProvider | None:
    provider = await get_llm(db, user_id, config_id)
    if provider is None:
        return None

    if "provider_id" in data and data["provider_id"] != provider.provider_id:
        provider.provider_id = _validate_provider_id(data["provider_id"])
    if "name" in data:
        provider.name = data["name"]
    if "npm" in data:
        provider.npm = data["npm"] or "@ai-sdk/openai-compatible"
    if "base_url" in data:
        provider.base_url_enc = crypto.encrypt_secret(data["base_url"])
    if "api_key" in data:
        # An explicit empty api_key clears the stored credential.
        provider.api_key_enc = crypto.encrypt_secret(data["api_key"])
    if "models" in data:
        provider.models_enc = (
            crypto.encrypt_json(data["models"])
            if isinstance(data["models"], dict)
            else ""
        )

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError(
            f"LLM provider '{provider.provider_id}' already exists"
        ) from exc
    await db.refresh(provider)
    return provider


async def delete_llm(db: AsyncSession, user_id: str, config_id: str) -> bool:
    provider = await get_llm(db, user_id, config_id)
    if provider is None:
        return False
    await db.delete(provider)
    await db.commit()
    return True


# ------------------------------------------------------------------
#  Active LLM selection (users.active_llm_provider_id / active_model)
# ------------------------------------------------------------------

async def get_active_llm(db: AsyncSession, user_id: str) -> dict[str, str | None] | None:
    """Return the user's active selection, or None when none is set."""
    user = await db.get(User, user_id)
    if user is None or not user.active_llm_provider_id:
        return None
    return {"provider_id": user.active_llm_provider_id, "model": user.active_model}


async def set_active_llm(
    db: AsyncSession,
    user_id: str,
    provider_id: str | None,
    model: str | None = None,
) -> dict[str, str | None]:
    """Set (or clear, when ``provider_id`` is None) the active LLM selection.

    Ownership is enforced: the provider must be one of this user's rows. A
    ``ValueError`` is raised when the provider is missing or not owned, or
    when the model/amount of data is invalid.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError("User not found")

    if provider_id is None:
        user.active_llm_provider_id = None
        user.active_model = None
        await db.commit()
        return {"provider_id": None, "model": None}

    provider_id = _validate_provider_id(provider_id)
    result = await db.execute(
        select(UserLLMProvider).where(
            UserLLMProvider.user_id == user_id,
            UserLLMProvider.provider_id == provider_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ValueError(f"LLM provider '{provider_id}' not found")

    if model is not None and model != "":
        if len(model) > 100:
            raise ValueError("model must be at most 100 characters")
    else:
        model = None

    user.active_llm_provider_id = provider_id
    user.active_model = model
    await db.commit()
    return {"provider_id": provider_id, "model": model}


# ------------------------------------------------------------------
#  Decrypt user content into the opencode container-config shape
# ------------------------------------------------------------------

async def resolve_user_provider_base(
    db: AsyncSession, user_id: str, provider_id: str
) -> str | None:
    """Return a user-owned provider's decrypted upstream base URL, or None.

    The query is scoped by both ``user_id`` and ``provider_id``, so a caller
    can only resolve a provider it owns. Used by the LLM proxy's user-scoped
    route to forward requests to the real upstream.
    """
    result = await db.execute(
        select(UserLLMProvider).where(
            UserLLMProvider.user_id == user_id,
            UserLLMProvider.provider_id == provider_id,
        )
    )
    provider = result.scalar_one_or_none()
    if provider is None:
        return None
    return crypto.decrypt_secret(provider.base_url_enc)


async def build_user_provider_map(db: AsyncSession, user_id: str) -> dict[str, Any]:
    """Decrypt a user's LLM providers into opencode ``provider`` entries.

    Each entry mirrors the host-config provider shape (``npm`` / ``options`` /
    ``models``). Secrets are decrypted here because the result is written into
    the user's own container; the HTTP API never uses this function.
    """
    result: dict[str, Any] = {}
    for provider in await list_llm(db, user_id):
        entry: dict[str, Any] = {
            "npm": provider.npm or "@ai-sdk/openai-compatible",
            "options": {},
            "models": crypto.decrypt_json(provider.models_enc),
        }
        if provider.name:
            entry["name"] = provider.name
        base_url = crypto.decrypt_secret(provider.base_url_enc)
        if base_url:
            entry["options"]["baseURL"] = base_url
        api_key = crypto.decrypt_secret(provider.api_key_enc)
        if api_key:
            entry["options"]["apiKey"] = api_key
        result[provider.provider_id] = entry
    return result


async def build_user_mcp_map(db: AsyncSession, user_id: str) -> dict[str, Any]:
    """Decrypt a user's enabled MCP servers into opencode ``mcp`` entries."""
    result: dict[str, Any] = {}
    for server in await list_mcp(db, user_id):
        if not server.enabled:
            continue
        cfg = crypto.decrypt_json(server.config_enc)
        entry: dict[str, Any] = {"type": server.type, "enabled": True}
        if server.type == "remote":
            entry["url"] = cfg.get("url")
            if cfg.get("headers"):
                entry["headers"] = cfg["headers"]
        else:
            entry["command"] = cfg.get("command") or []
            if cfg.get("environment"):
                entry["environment"] = cfg["environment"]
        if cfg.get("cwd"):
            entry["cwd"] = cfg["cwd"]
        if cfg.get("timeout") is not None:
            entry["timeout"] = cfg["timeout"]
        result[server.name] = entry
    return result


async def build_user_config_json(db: AsyncSession, user_id: str) -> str:
    """Build the container opencode.json for a user from DB-backed content.

    This is the single async entry point the container endpoints call before
    delegating to the synchronous Docker thread: it loads the user's providers,
    MCP servers and active selection, then serializes the merged config.
    """
    active = await get_active_llm(db, user_id)
    return opencode_config.build_container_config_json(
        user_provider=await build_user_provider_map(db, user_id),
        user_mcp=await build_user_mcp_map(db, user_id),
        active_provider_id=(active or {}).get("provider_id"),
        active_model=(active or {}).get("model"),
        user_id=user_id,
    )