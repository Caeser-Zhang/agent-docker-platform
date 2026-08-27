"""Config management routes — CRUD for MCP servers, providers, and skills.

All operations read/write the host opencode.json (or skill files on disk).
After any write, the caller should reload config into running containers.

Endpoints:
  GET    /api/config                  — overview: providers + mcp + skills
  --- Providers ---
  GET    /api/config/providers        — list all providers (no secrets)
  POST   /api/config/providers        — create/update a provider
  DELETE /api/config/providers/{id}   — delete a provider
  --- MCP ---
  GET    /api/config/mcp              — list all MCP servers
  POST   /api/config/mcp/{name}       — create/update an MCP server
  PATCH  /api/config/mcp/{name}       — toggle enabled/disabled
  DELETE /api/config/mcp/{name}       — delete an MCP server
  --- Skills ---
  GET    /api/config/skills           — list all skills
  GET    /api/config/skills/{name}    — get skill content
  POST   /api/config/skills/{name}    — create/update a skill
  DELETE /api/config/skills/{name}    — delete a skill
  --- Reload ---
  POST   /api/config/reload           — reload config into running container
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user
from ..models import User
from ..services import host_config, opencode_config
from ..services.agent_controller import agent_controller
from ..services.container_manager import container_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])


# ------------------------------------------------------------------
#  Schemas
# ------------------------------------------------------------------

class ProviderConfig(BaseModel):
    name: str | None = None
    npm: str = "@ai-sdk/openai-compatible"
    options: dict[str, Any] = {}
    models: dict[str, dict] = {}


class McpLocalConfig(BaseModel):
    type: str = "local"
    command: list[str]
    enabled: bool = True
    environment: dict[str, str] | None = None
    cwd: str | None = None
    timeout: int | None = None


class McpRemoteConfig(BaseModel):
    type: str = "remote"
    url: str
    enabled: bool = True
    headers: dict[str, str] | None = None
    timeout: int | None = None


class McpToggle(BaseModel):
    enabled: bool


class SkillCreate(BaseModel):
    content: str


class ReloadResponse(BaseModel):
    reloaded: bool
    message: str = ""


# ------------------------------------------------------------------
#  Overview
# ------------------------------------------------------------------

def _safe_mcp_entry(name: str, cfg: dict, builtin: bool) -> dict:
    """Serialize an MCP server entry without leaking secrets."""
    entry = {
        "type": cfg.get("type"),
        "enabled": cfg.get("enabled", True),
        "builtin": builtin,
    }
    if cfg.get("type") == "remote":
        entry["url"] = cfg.get("url")
        entry["hasHeaders"] = bool(cfg.get("headers"))
    elif cfg.get("type") == "local":
        entry["command"] = cfg.get("command")
        entry["hasEnv"] = bool(cfg.get("environment"))
    return entry


def _all_mcp() -> dict[str, dict]:
    """Merged view of built-in MCP servers + user-declared MCP servers.

    Built-in servers are discovered from /builtin-mcp (with enabled overrides
    from the host config applied); user servers come from the host opencode.json
    ``mcp`` section. Built-in entries are marked ``builtin=True`` so the client
    can restrict edit/delete while still toggling them.
    """
    result: dict[str, dict] = {}
    for name, cfg in opencode_config.builtin_mcp_servers().items():
        result[name] = _safe_mcp_entry(name, cfg, builtin=True)
    for name, cfg in host_config.list_mcp_servers().items():
        result[name] = _safe_mcp_entry(name, cfg, builtin=False)
    return result


@router.get("")
async def config_overview(user: User = Depends(get_current_user)):
    """Get an overview of all config sections."""
    providers = host_config.list_providers_raw()
    skills = host_config.list_skills()

    # Never leak API keys
    safe_providers = {}
    for pid, cfg in providers.items():
        safe_cfg = {k: v for k, v in cfg.items() if k != "options"}
        opts = cfg.get("options") or {}
        safe_cfg["options"] = {
            "baseURL": opts.get("baseURL"),
            "hasApiKey": bool(opts.get("apiKey")),
        }
        safe_providers[pid] = safe_cfg

    return {
        "providers": safe_providers,
        "mcp": _all_mcp(),
        "skills": skills,
    }


# ------------------------------------------------------------------
#  Provider CRUD
# ------------------------------------------------------------------

@router.get("/providers")
async def list_providers(user: User = Depends(get_current_user)):
    """List all configured providers (API keys masked)."""
    providers = host_config.list_providers_raw()
    result = {}
    for pid, cfg in providers.items():
        opts = cfg.get("options") or {}
        result[pid] = {
            "name": cfg.get("name", pid),
            "npm": cfg.get("npm"),
            "baseURL": opts.get("baseURL"),
            "hasApiKey": bool(opts.get("apiKey")),
            "models": list((cfg.get("models") or {}).keys()),
        }
    return {"providers": result}


@router.post("/providers/{provider_id}")
async def upsert_provider(
    provider_id: str,
    body: ProviderConfig,
    user: User = Depends(get_current_user),
):
    """Create or update a provider."""
    try:
        cfg = body.model_dump(exclude_none=True)
        host_config.upsert_provider(provider_id, cfg)
        logger.info("Provider '%s' upserted by %s", provider_id, user.username)
        return {"status": "ok", "provider_id": provider_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    user: User = Depends(get_current_user),
):
    """Delete a provider."""
    if not host_config.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    logger.info("Provider '%s' deleted by %s", provider_id, user.username)
    return {"status": "ok"}


# ------------------------------------------------------------------
#  MCP server CRUD
# ------------------------------------------------------------------

@router.get("/mcp")
async def list_mcp(user: User = Depends(get_current_user)):
    """List all MCP servers (built-in + user-declared; secrets masked)."""
    return {"mcp": _all_mcp()}


@router.post("/mcp/{name}")
async def upsert_mcp(
    name: str,
    body: dict,
    user: User = Depends(get_current_user),
):
    """Create or update an MCP server.

    Accepts either local or remote config. The `type` field determines which
    fields are required. Built-in MCP servers cannot be overwritten.
    """
    if name in opencode_config.builtin_mcp_servers():
        raise HTTPException(status_code=403, detail=f"Built-in MCP server '{name}' cannot be modified")
    try:
        host_config.upsert_mcp_server(name, body)
        logger.info("MCP server '%s' upserted by %s", name, user.username)
        return {"status": "ok", "name": name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/mcp/{name}")
async def toggle_mcp(
    name: str,
    body: McpToggle,
    user: User = Depends(get_current_user),
):
    """Enable or disable an MCP server without removing it."""
    builtin = opencode_config.builtin_mcp_servers()
    if name in builtin:
        host_config.toggle_builtin_mcp(name, body.enabled)
    else:
        result = host_config.toggle_mcp_server(name, body.enabled)
        if result is None:
            raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    logger.info("MCP server '%s' %s by %s", name, "enabled" if body.enabled else "disabled", user.username)
    return {"status": "ok", "name": name, "enabled": body.enabled, "builtin": name in builtin}


@router.delete("/mcp/{name}")
async def delete_mcp(
    name: str,
    user: User = Depends(get_current_user),
):
    """Delete an MCP server."""
    if name in opencode_config.builtin_mcp_servers():
        raise HTTPException(status_code=403, detail=f"Built-in MCP server '{name}' cannot be deleted")
    if not host_config.delete_mcp_server(name):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    logger.info("MCP server '%s' deleted by %s", name, user.username)
    return {"status": "ok"}


# ------------------------------------------------------------------
#  Skill CRUD
# ------------------------------------------------------------------

@router.get("/skills")
async def list_skills(user: User = Depends(get_current_user)):
    """List all skills."""
    return {"skills": host_config.list_skills()}


@router.get("/skills/{name}")
async def get_skill(name: str, user: User = Depends(get_current_user)):
    """Get a skill's content."""
    skill = host_config.get_skill(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return skill


@router.post("/skills/{name}")
async def upsert_skill(
    name: str,
    body: SkillCreate,
    user: User = Depends(get_current_user),
):
    """Create or update a skill."""
    try:
        result = host_config.upsert_skill(name, body.content)
        logger.info("Skill '%s' upserted by %s", name, user.username)
        return {"status": "ok", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/skills/{name}")
async def delete_skill(
    name: str,
    user: User = Depends(get_current_user),
):
    """Delete a skill."""
    if not host_config.delete_skill(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    logger.info("Skill '%s' deleted by %s", name, user.username)
    return {"status": "ok"}


# ------------------------------------------------------------------
#  Reload — push config changes into the running container
# ------------------------------------------------------------------

@router.post("/reload", response_model=ReloadResponse)
async def reload_config(user: User = Depends(get_current_user)):
    """Re-inject config into the user's container and restart it.

    Must actually restart the container (not just reuse the running one) —
    opencode reads the config at boot. Mirrors tunnel.py's /config/reload:
    stop → re-inject → start → rebuild the SSE pump.
    """
    ok = await container_manager.reload_config(user.id)
    if not ok:
        return ReloadResponse(
            reloaded=False,
            message="No container to reload — 请先启动 Agent",
        )
    await agent_controller.restart_pump(user.id)
    return ReloadResponse(reloaded=True, message="Config reloaded into container")
