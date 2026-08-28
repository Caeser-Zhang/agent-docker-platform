"""Per-user custom content routes — MCP servers and LLM providers.

Every endpoint is scoped to the authenticated user via ``get_current_user``:
users can only read, create, update and delete the records they own. Secrets
(API keys, MCP headers/environment) are encrypted at rest and masked in
responses — the API never returns plaintext credentials.

Endpoints:
  GET    /api/user-config/mcp                — list my MCP servers
  POST   /api/user-config/mcp                — create an MCP server
  GET    /api/user-config/mcp/{server_id}    — get one MCP server
  PATCH  /api/user-config/mcp/{server_id}    — update one MCP server
  DELETE /api/user-config/mcp/{server_id}    — delete one MCP server
  GET    /api/user-config/llm                — list my LLM providers
  POST   /api/user-config/llm                — create an LLM provider
  GET    /api/user-config/llm/{config_id}    — get one LLM provider
  PATCH  /api/user-config/llm/{config_id}    — update one LLM provider
  DELETE /api/user-config/llm/{config_id}    — delete one LLM provider
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..services import user_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/user-config", tags=["user-config"])


# ------------------------------------------------------------------
#  Schemas
# ------------------------------------------------------------------

class McpCreate(BaseModel):
    name: str
    type: str
    enabled: bool = True
    command: list[str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    environment: dict[str, str] | None = None
    cwd: str | None = None
    timeout: int | None = None


class McpUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    enabled: bool | None = None
    command: list[str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    environment: dict[str, str] | None = None
    cwd: str | None = None
    timeout: int | None = None


class LlmCreate(BaseModel):
    provider_id: str
    name: str | None = None
    npm: str = "@ai-sdk/openai-compatible"
    base_url: str | None = None
    api_key: str | None = None
    models: dict[str, Any] | None = None


class LlmUpdate(BaseModel):
    provider_id: str | None = None
    name: str | None = None
    npm: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: dict[str, Any] | None = None


class ActiveLlmUpdate(BaseModel):
    provider_id: str | None = None
    model: str | None = None


# ------------------------------------------------------------------
#  MCP server CRUD
# ------------------------------------------------------------------

@router.get("/mcp")
async def list_mcp(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    servers = await user_config.list_mcp(db, user.id)
    return {"mcp": [user_config.serialize_mcp(s) for s in servers]}


@router.post("/mcp", status_code=201)
async def create_mcp(
    body: McpCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        server = await user_config.create_mcp(db, user.id, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except user_config.DuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("User %s created MCP server '%s'", user.username, server.name)
    return user_config.serialize_mcp(server)


@router.get("/mcp/{server_id}")
async def get_mcp(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await user_config.get_mcp(db, user.id, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return user_config.serialize_mcp(server)


@router.patch("/mcp/{server_id}")
async def update_mcp(
    server_id: str,
    body: McpUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        server = await user_config.update_mcp(
            db, user.id, server_id, body.model_dump(exclude_unset=True)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except user_config.DuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    logger.info("User %s updated MCP server '%s'", user.username, server.name)
    return user_config.serialize_mcp(server)


@router.delete("/mcp/{server_id}", status_code=204)
async def delete_mcp(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await user_config.delete_mcp(db, user.id, server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="MCP server not found")
    logger.info("User %s deleted MCP server '%s'", user.username, server_id)


# ------------------------------------------------------------------
#  LLM provider CRUD
# ------------------------------------------------------------------

@router.get("/llm")
async def list_llm(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    providers = await user_config.list_llm(db, user.id)
    return {"providers": [user_config.serialize_provider(p) for p in providers]}


@router.post("/llm", status_code=201)
async def create_llm(
    body: LlmCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        provider = await user_config.create_llm(db, user.id, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except user_config.DuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("User %s created LLM provider '%s'", user.username, provider.provider_id)
    return user_config.serialize_provider(provider)


@router.get("/llm/{config_id}")
async def get_llm(
    config_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    provider = await user_config.get_llm(db, user.id, config_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="LLM provider not found")
    return user_config.serialize_provider(provider)


@router.patch("/llm/{config_id}")
async def update_llm(
    config_id: str,
    body: LlmUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        provider = await user_config.update_llm(
            db, user.id, config_id, body.model_dump(exclude_unset=True)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except user_config.DuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if provider is None:
        raise HTTPException(status_code=404, detail="LLM provider not found")
    logger.info("User %s updated LLM provider '%s'", user.username, provider.provider_id)
    return user_config.serialize_provider(provider)


@router.delete("/llm/{config_id}", status_code=204)
async def delete_llm(
    config_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await user_config.delete_llm(db, user.id, config_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="LLM provider not found")
    logger.info("User %s deleted LLM provider '%s'", user.username, config_id)


# ------------------------------------------------------------------
#  Active LLM selection
# ------------------------------------------------------------------

@router.get("/active-llm")
async def get_active_llm(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the user's active LLM selection (or empty when none set)."""
    active = await user_config.get_active_llm(db, user.id)
    if active is None:
        return {"provider_id": None, "model": None}
    return active


@router.put("/active-llm")
async def set_active_llm(
    body: ActiveLlmUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set (or clear, when ``provider_id`` is None) the active LLM selection.

    Ownership is enforced: the provider must be one of the current user's
    rows. The selection is merged into the container config on the next
    start/reload.
    """
    try:
        result = await user_config.set_active_llm(
            db, user.id, body.provider_id, body.model
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(
        "User %s set active LLM to %s/%s",
        user.username, result["provider_id"], result["model"],
    )
    return result