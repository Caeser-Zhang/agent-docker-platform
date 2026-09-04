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
  PATCH  /api/config/mcp/{name}       — toggle enabled/disabled (runtime ~2s)
  DELETE /api/config/mcp/{name}       — delete an MCP server
  --- Skills ---
  GET    /api/config/skills           — list all skills
  GET    /api/config/skills/{name}    — get skill content
  POST   /api/config/skills/{name}    — create/update a skill
  DELETE /api/config/skills/{name}    — delete a skill
  --- Built-in skill visibility (dynamic) ---
  GET    /api/config/builtin-skills          — list plugin skills + visibility
  PATCH  /api/config/builtin-skills/{name}   — show/hide (runtime ~2s)
  --- Reload ---
  POST   /api/config/reload           — reload config into running container
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user, require_admin
from ..database import get_db
from ..models import User
from ..services import host_config, opencode_config, user_config, visibility
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


class BuiltinSkillToggle(BaseModel):
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
        "source": "builtin" if builtin else "user",
    }
    if cfg.get("type") == "remote":
        entry["url"] = cfg.get("url")
        entry["hasHeaders"] = bool(cfg.get("headers"))
    elif cfg.get("type") == "local":
        entry["command"] = cfg.get("command")
        entry["hasEnv"] = bool(cfg.get("environment"))
    return entry


def _all_mcp(include_host: bool = True) -> dict[str, dict]:
    """Merged view of built-in MCP servers + host-declared MCP servers.

    Built-in servers are discovered from /builtin-mcp (with enabled overrides
    from the host config applied); host servers come from the host opencode.json
    ``mcp`` section. Built-in entries are marked ``builtin=True`` so the client
    can restrict edit/delete while still toggling them.

    ``include_host=False`` hides the host (platform-wide) servers — those are
    injected into EVERY user's container, so they are admin-managed only and
    must not leak to regular users browsing the config panel. Per-user MCP
    servers live in /api/user-config/mcp instead.
    """
    result: dict[str, dict] = {}
    for name, cfg in opencode_config.builtin_mcp_servers().items():
        result[name] = _safe_mcp_entry(name, cfg, builtin=True)
    if include_host:
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
        # Host MCP servers are platform-wide (admin-managed): regular users
        # only see the built-in ones here, their own servers live under
        # /api/user-config/mcp.
        "mcp": _all_mcp(include_host=user.role == "admin"),
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
    """List MCP servers (secrets masked).

    Regular users see only the built-in servers; host-declared servers are
    platform-wide and admin-managed (they would otherwise leak every user's
    additions to everyone). Per-user servers live at /api/user-config/mcp.
    """
    return {"mcp": _all_mcp(include_host=user.role == "admin")}


@router.post("/mcp/{name}")
async def upsert_mcp(
    name: str,
    body: dict,
    user: User = Depends(require_admin),
):
    """Create or update a host (platform-wide) MCP server. Admin only.

    Writes the host opencode.json, which is injected into EVERY user's
    container — regular users must use /api/user-config/mcp instead, which
    is scoped (and encrypted) per user.

    Accepts either local or remote config. The `type` field determines which
    fields are required. Built-in MCP servers cannot be overwritten.
    """
    if name in opencode_config.builtin_mcp_servers():
        raise HTTPException(status_code=403, detail=f"Built-in MCP server '{name}' cannot be modified")
    try:
        host_config.upsert_mcp_server(name, body)
        logger.info("MCP server '%s' upserted by admin %s", name, user.username)
        return {"status": "ok", "name": name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/mcp/{name}")
async def toggle_mcp(
    name: str,
    body: McpToggle,
    user: User = Depends(require_admin),
):
    """Enable or disable an MCP server without removing it. Admin only.

    Toggling rewrites the host config (affecting every user's container,
    including built-in server overrides) and then pushes a runtime
    permission flip to all running agents via PATCH /global/config, so the
    change takes effect in ~2s instead of requiring a container restart.

    Host-defined local servers (command-based, running on the host) are
    never injected into containers, so there is nothing to push at runtime
    for them. Built-in local servers (e.g. web_search) DO run inside the
    containers and are injected, so they get the runtime push like remote
    ones.
    """
    builtin = opencode_config.builtin_mcp_servers()
    if name in builtin:
        host_config.toggle_builtin_mcp(name, body.enabled)
    else:
        result = host_config.toggle_mcp_server(name, body.enabled)
        if result is None:
            raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    entry = builtin.get(name) or host_config.get_mcp_server(name) or {}
    if entry.get("type") == "local" and name not in builtin:
        runtime = {"applied": 0, "failed": [], "skipped": "local"}
    else:
        runtime = await visibility.broadcast_visibility_change("mcp", name, hidden=not body.enabled)
    logger.info(
        "MCP server '%s' %s by admin %s (runtime applied=%d failed=%d)",
        name, "enabled" if body.enabled else "disabled", user.username,
        runtime.get("applied", 0), len(runtime.get("failed", [])),
    )
    return {"status": "ok", "name": name, "enabled": body.enabled, "builtin": name in builtin, "runtime": runtime}


@router.delete("/mcp/{name}")
async def delete_mcp(
    name: str,
    user: User = Depends(require_admin),
):
    """Delete a host (platform-wide) MCP server. Admin only."""
    if name in opencode_config.builtin_mcp_servers():
        raise HTTPException(status_code=403, detail=f"Built-in MCP server '{name}' cannot be deleted")
    if not host_config.delete_mcp_server(name):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    logger.info("MCP server '%s' deleted by admin %s", name, user.username)
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
#  Built-in skill visibility (dynamic, runtime ~2s)
# ------------------------------------------------------------------

@router.get("/builtin-skills")
async def get_builtin_skills(user: User = Depends(require_admin)):
    """List built-in (plugin-provided) skills with their visibility state.

    Enumerated live from a running agent when possible; falls back to the
    persisted overrides when no container is up.
    """
    return await visibility.builtin_skills_overview()


@router.patch("/builtin-skills/{name}")
async def toggle_builtin_skill(
    name: str,
    body: BuiltinSkillToggle,
    user: User = Depends(require_admin),
):
    """Show or hide a built-in skill for all agents. Admin only.

    Persists the override (re-rendered into every container on next
    start/reload) and pushes a runtime permission flip to all running
    agents via PATCH /global/config — no container restart needed.
    """
    host_config.toggle_builtin_skill(name, body.enabled)
    runtime = await visibility.broadcast_visibility_change("skill", name, hidden=not body.enabled)
    logger.info(
        "Builtin skill '%s' %s by admin %s (runtime applied=%d failed=%d)",
        name, "enabled" if body.enabled else "disabled", user.username,
        runtime.get("applied", 0), len(runtime.get("failed", [])),
    )
    return {"status": "ok", "name": name, "enabled": body.enabled, "runtime": runtime}


# ------------------------------------------------------------------
#  Reload — push config changes into the running container
# ------------------------------------------------------------------

@router.post("/reload", response_model=ReloadResponse)
async def reload_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-inject config into the user's container and restart it.

    Must actually restart the container (not just reuse the running one) —
    opencode reads the config at boot. Mirrors tunnel.py's /config/reload:
    stop → re-inject → start → rebuild the SSE pump.

    The user's own providers / MCP servers / active LLM selection are merged
    in, so a change to those takes effect here too.
    """
    config_json = await user_config.build_user_config_json(db, user.id)
    ok = await container_manager.reload_config(user.id, config_json)
    if not ok:
        return ReloadResponse(
            reloaded=False,
            message="No container to reload — 请先启动 Agent",
        )
    await agent_controller.restart_pump(user.id)
    return ReloadResponse(reloaded=True, message="Config reloaded into container")
