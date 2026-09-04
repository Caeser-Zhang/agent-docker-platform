"""Runtime visibility — flip skill/MCP visibility in live agent containers.

The host opencode.json (``builtin_skills`` / ``builtin_mcp`` sections) is the
persisted source of truth; :func:`opencode_config.build_container_config`
renders it into permission rules whenever a container (re)starts. This module
is the runtime fast path for a toggle: instead of the 10-20s stop → inject →
start cycle it rewrites the plugin config file inside the container's config
volume and PATCHes opencode's ``/global/config`` with the permission delta —
opencode mergeDeep-persists the patch, invalidates every instance and
re-evaluates in ~2s (verified against opencode 1.18.25).

Enforcement is two-pronged, mirroring build_container_config:

* ``opencode.json`` permission rules (native agents + the native tool
  surface): a hidden skill becomes ``permission.skill["<name>"] = "deny"`` and
  a hidden MCP server becomes ``permission["<sanitized>_*"] = "deny"``.
  mergeDeep PATCHes cannot delete keys, so *un-hiding* flips the rule back to
  ``"allow"``; the injected file is re-rendered authoritatively on the next
  container restart, which cleans the accumulated keys up.
* the oh-my-opencode-slim plugin config (``disabled_skills`` + the preset
  ``mcps`` lists). The backend owns these keys, so the file is read,
  transformed with :func:`opencode_config.apply_plugin_visibility` and written
  back — an authoritative replace that, unlike a merge, can also *un-hide*.

Visibility toggles are platform-wide admin actions, so a change is broadcast
to every running agent container. A container that is down picks the change
up from the injected config on its next start; a runtime push failure is
therefore non-fatal — the persisted state stays correct and self-heals.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .agent_controller import agent_controller
from .container_manager import container_manager
from .opencode_config import (
    PLUGIN_CONFIG_FILENAME,
    _discover_builtin_plugins,
    _sanitize_mcp_permission_key,
    apply_plugin_visibility,
    hidden_builtin_skills,
    hidden_mcp_servers,
)
from .tunnel_relay import tunnel_relay
from . import host_config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
#  Built-in skill enumeration (runtime, via a live container)
# ------------------------------------------------------------------

async def list_builtin_skills(user_id: str) -> list[dict]:
    """List plugin-registered skills from the running container's opencode.

    Built-in plugin skills live inside the plugin's pre-baked node_modules
    tree in the read-only agent image, so the backend cannot see them on the
    host — only the opencode server inside the container knows them. Query
    its native GET /skill through the relay and keep the entries whose
    ``location`` falls under a built-in plugin path. Returns [] whenever the
    container is not running or the call fails.
    """
    running, password = await agent_controller.get_agent_gate(user_id)
    if not running or not password:
        return []
    plugin_prefixes = [p.rstrip("/") + "/" for p in _discover_builtin_plugins()]
    if not plugin_prefixes:
        return []
    try:
        resp = await tunnel_relay.http_request(
            user_id, "GET", "/skill", password=password, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Native skill listing via relay failed: %s", exc)
        return []
    if resp.get("status") != 200 or not isinstance(resp.get("body"), list):
        return []
    skills: list[dict] = []
    for s in resp["body"]:
        location = s.get("location") or ""
        if not any(location.startswith(p) for p in plugin_prefixes):
            continue
        if s.get("name"):
            skills.append({
                "name": s["name"],
                "description": s.get("description", ""),
                "dir": location,
                "scope": "builtin",
            })
    return skills


async def _running_agent_user_ids() -> list[str]:
    """User IDs of every running agent container (visibility is platform-wide)."""
    try:
        rows = await asyncio.to_thread(container_manager.list_all_containers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agent container listing failed: %s", exc)
        return []
    return [
        row["user_id"]
        for row in rows
        if row.get("status") == "running" and row.get("user_id")
    ]


async def builtin_skills_overview() -> dict:
    """Enumerate the built-in plugin skills with their visibility state.

    The skill list itself lives inside the plugin's compiled bundle in the
    read-only image, so enumeration asks a running container's opencode —
    any one of them works, the plugin set is platform-wide. When none is
    running (or none answers) the listing falls back to the persisted
    override keys, so the admin panel can still see — and flip — everything
    that was ever toggled.
    """
    overrides = host_config.list_builtin_skill_overrides()
    skills: list[dict] = []
    for uid in await _running_agent_user_ids():
        skills = await list_builtin_skills(uid)
        if skills:
            break
    reachable = bool(skills)
    if not skills:
        skills = [
            {"name": name, "description": "", "dir": "", "scope": "builtin"}
            for name in sorted(overrides)
        ]
    return {
        "reachable": reachable,
        "skills": [
            {
                "name": s["name"],
                "description": s.get("description", ""),
                "dir": s.get("dir", ""),
                "enabled": bool((overrides.get(s["name"]) or {}).get("enabled", True)),
            }
            for s in skills
        ],
    }


# ------------------------------------------------------------------
#  Runtime toggle — permission patch + plugin config rewrite
# ------------------------------------------------------------------

def _visibility_patch(kind: str, name: str, hidden: bool) -> dict:
    """Build the ``PATCH /global/config`` body that flips one visibility rule.

    ``kind`` is "skill" or "mcp"; ``hidden`` selects deny vs allow. The skill
    patch always carries ``"*": "allow"`` — opencode's PermissionRuleSchema
    union means an object PATCH *replaces* a plain-string ``skill`` value,
    and without the wildcard every other skill would fall back to the
    interactive-approval default, which a headless server cannot answer.
    """
    action = "deny" if hidden else "allow"
    if kind == "skill":
        return {"permission": {"skill": {"*": "allow", name: action}}}
    return {"permission": {f"{_sanitize_mcp_permission_key(name)}_*": action}}


async def _sync_plugin_config(user_id: str) -> bool:
    """Re-apply the current visibility sets to the container's plugin config.

    Read-modify-write: the platform authoritatively owns ``disabled_skills``
    and the preset ``mcps`` lists, so a full re-render both applies a new
    hide and clears a previous one — unlike a mergeDeep PATCH, a replace can
    delete keys. Returns True when there is no plugin config file to rewrite
    (the plugin then runs on its defaults; only the permission rules apply).
    """
    raw = await asyncio.to_thread(
        container_manager.read_config_file, user_id, PLUGIN_CONFIG_FILENAME
    )
    if raw is None:
        return True
    try:
        cfg = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Plugin config in agent-%s is unreadable: %s", user_id, exc)
        return False
    if not isinstance(cfg, dict):
        return False
    updated = apply_plugin_visibility(
        cfg, hidden_builtin_skills(), hidden_mcp_servers()
    )
    payload = json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")
    return await asyncio.to_thread(
        container_manager.write_config_file, user_id, PLUGIN_CONFIG_FILENAME, payload
    )


async def _push_to_container(user_id: str, patch: dict) -> bool:
    """Apply one visibility patch to a single container (file first, PATCH second)."""
    running, password = await agent_controller.get_agent_gate(user_id)
    if not running or not password:
        return False
    # The file must land before the PATCH: the PATCH invalidates every
    # opencode instance, and the rebuilt ones read the freshly written
    # plugin config (disabled_skills / preset mcps lists).
    if not await _sync_plugin_config(user_id):
        logger.warning("Plugin config sync failed for agent-%s", user_id)
    try:
        resp = await tunnel_relay.http_request(
            user_id,
            "PATCH",
            "/global/config",
            raw_body=json.dumps(patch).encode("utf-8"),
            password=password,
            timeout=15,
            headers={"content-type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Visibility PATCH for agent-%s failed: %s", user_id, exc)
        return False
    if resp.get("status") != 200:
        logger.warning(
            "Visibility PATCH for agent-%s returned HTTP %s: %s",
            user_id, resp.get("status"), resp.get("body"),
        )
        return False
    return True


async def broadcast_visibility_change(kind: str, name: str, hidden: bool) -> dict:
    """Push one visibility toggle to every running agent container.

    ``kind`` is "skill" or "mcp". Returns a summary the caller can surface:
    ``applied`` counts containers that took the runtime patch, ``failed``
    lists the ones that did not (down containers included — they pick the
    persisted state up from the injected config on their next start).
    """
    patch = _visibility_patch(kind, name, hidden)
    user_ids = await _running_agent_user_ids()
    if not user_ids:
        return {"applied": 0, "failed": []}

    results = await asyncio.gather(
        *(_push_to_container(uid, patch) for uid in user_ids)
    )
    failed = [uid for uid, ok in zip(user_ids, results) if not ok]
    if failed:
        logger.warning(
            "Runtime visibility push (%s '%s' -> %s) failed on %d container(s): %s",
            kind, name, "hidden" if hidden else "visible",
            len(failed), ",".join(failed),
        )
    return {"applied": len(user_ids) - len(failed), "failed": failed}
