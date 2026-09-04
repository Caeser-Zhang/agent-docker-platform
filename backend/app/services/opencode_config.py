"""Build the opencode configuration that gets injected into a user's container.

The platform does **not** talk to any LLM itself. All agent capability lives in
`opencode serve` running inside the per-user container, so the only thing the
platform has to get right is the configuration that container boots with.

Pipeline:

    host ~/.config/opencode/opencode.json   (mounted read-only into the backend)
        -> strip host-only sections (mcp / plugin — they point at Windows paths
           and would make the container hang on startup)
        -> rewrite loopback base URLs so a local LLM proxy on the developer's
           machine is still reachable from inside the container
        -> merge container defaults (permissions, autoupdate off, ...)
        -> translate the platform's visibility overrides (builtin_skills /
           builtin_mcp "enabled" flags) into permission deny rules while
           forcing every MCP server connected — visibility is enforced by
           opencode's permission system, not by connection state, so it can
           be flipped at runtime via PATCH /global/config (~2s) instead of a
           full stop/inject/start cycle
        -> inject built-in MCP servers and plugins (pre-baked into the
           read-only agent image; users cannot remove them)
        -> written into the per-user config volume as
           $XDG_CONFIG_HOME/opencode/opencode.json

Verified against opencode 1.18.16: the server logs
`message=loading path=<XDG_CONFIG_HOME>/opencode/opencode.json` on boot and
`GET /config` echoes the merged result back.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# User-declared `plugin` entries are stripped — they reference host paths or
# npm specs that trigger an install at boot, which breaks in a hardened,
# read-only container. Built-in plugins (pre-baked into the agent image at
# build time) are re-injected afterwards via _discover_builtin_plugins().
# `mcp` is now managed via the host_config API and filtered
# per-entry: remote MCP servers (URL-based) are kept, local ones (command-based)
# are stripped because they reference host executables.
# `builtin_mcp` / `builtin_skills` are platform-internal visibility override
# maps (built-in MCP servers and built-in plugin skills); they must never be
# injected into the container as opencode config keys — build_container_config
# translates them into permission deny rules instead.
HOST_ONLY_KEYS = ("plugin", "builtin_mcp", "builtin_skills")

# Any of these appearing inside a URL means "the machine running Docker", which
# from the container's point of view is reachable as host.docker.internal.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]")

_URL_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<host>[^/:]+|\[[^\]]+\])(?P<rest>.*)$")

# Applied underneath the user's config. The container is the sandbox, so tools
# run without interactive approval — there is no human at the other end of an
# "ask" prompt in a headless server.
CONTAINER_DEFAULTS: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,
    "share": "disabled",
    "permission": {
        "read": "allow",
        "edit": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": "allow",
        "task": "allow",
        "webfetch": "allow",
        "todowrite": "allow",
        "external_directory": "allow",
        "skill": "allow",
        "web_search*": "allow",
    },
}

# The built-in oh-my-opencode-slim plugin reads its per-deployment config from
# ${XDG_CONFIG_HOME}/opencode/<plugin>.json (seeded by agent-image/entrypoint.sh
# from plugin.default.json on first boot). The platform renders this file
# itself (see render_plugin_config) so the per-agent model mapping and the
# dynamic visibility rules (preset mcps lists, disabled_skills) stay in sync
# with the permission deny rules written into opencode.json.
PLUGIN_CONFIG_FILENAME = "oh-my-opencode-slim.json"

# Agents the entrypoint preset template covers (mirror of entrypoint.sh's
# generated presets.container — keep the two in sync).
_PRESET_AGENTS = ("orchestrator", "oracle", "librarian", "explorer", "designer", "fixer")

# Agents whose model plugin.default.json statically pins. The plugin merges
# config.agents over presets[config.preset] (static keys win), so the render
# keeps the pin structure but with the actually resolved model.
_PINNED_AGENTS = (
    "orchestrator", "explorer", "librarian", "oracle",
    "designer", "fixer", "observer", "council", "councillor",
)


def _rewrite_loopback(value: str, replacement: str) -> str:
    """Rewrite the host part of a URL when it points at loopback.

    Only touches strings that actually look like URLs, so an API key that
    happens to contain "localhost" is left alone.
    """
    match = _URL_RE.match(value)
    if not match:
        return value
    if match.group("host") not in LOOPBACK_HOSTS:
        return value
    return f"{match.group('scheme')}{replacement}{match.group('rest')}"


def _walk(node: Any, replacement: str) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, replacement) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, replacement) for v in node]
    if isinstance(node, str):
        return _rewrite_loopback(node, replacement)
    return node


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins, but nested dicts are merged rather than replaced."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _first_model_of(prov: Any) -> str | None:
    """First model id of a single provider entry (deterministic order)."""
    if not isinstance(prov, dict):
        return None
    models = prov.get("models") or {}
    if not isinstance(models, dict):
        return None
    for model_id in sorted(models):
        return model_id
    return None


def _first_model(provider: dict) -> str | None:
    """Pick a deterministic provider/model pair to use as the default.

    opencode's config file expects model/small_model as "provider/model" strings.
    (The session creation API takes Model.Ref objects, but that's a different
    context — the config file format is string.)
    """
    for provider_id in sorted(provider):
        model_id = _first_model_of(provider[provider_id])
        if model_id:
            return f"{provider_id}/{model_id}"
    return None


def load_source_config() -> tuple[dict, str]:
    """Read the mounted host config. Returns (config, source_description)."""
    path = Path(settings.opencode_config_source)
    if not path.is_file():
        logger.warning(
            "No opencode config mounted at %s — containers will start with the "
            "built-in defaults and no custom providers.",
            path,
        )
        return {}, "none"
    try:
        raw = path.read_text(encoding="utf-8-sig")
        return json.loads(raw), str(path)
    except Exception as exc:  # noqa: BLE001 - config problems must not crash the API
        logger.error("Failed to parse opencode config at %s: %s", path, exc)
        return {}, f"{path} (parse failed: {exc})"


def _strip_incompatible_model_options(config: dict) -> dict:
    """Remove model-level option keys that break @ai-sdk/openai-compatible.

    opencode v1.18.16's ``@ai-sdk/openai-compatible`` provider rejects
    ``options.thinking`` with::

        http.body cannot overlay protocol-owned field(s): thinking

    because ``thinking`` is a protocol-level field that the SDK owns — it
    cannot be forwarded as a raw body parameter.  The host config (written for
    the desktop TUI, which has its own thinking-budget handling) sets it on
    every model, so we strip it transparently for the container.
    """
    provider = config.get("provider") or {}
    for prov in provider.values():
        models = prov.get("models") or {}
        for model in models.values():
            opts = model.get("options")
            if isinstance(opts, dict) and "thinking" in opts:
                opts.pop("thinking", None)
                if not opts:
                    model.pop("options", None)
    return config


def _resolve_placeholders(value: Any) -> Any:
    """Resolve ``${VAR}`` placeholders against platform settings (lowercased).

    ``${SEARXNG_URL}`` -> ``settings.searxng_url``. Unresolvable placeholders
    are kept verbatim so the misconfiguration stays visible in the container
    config instead of silently becoming an empty string. Dicts and lists are
    resolved recursively.
    """
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}"):
            var_name = value[2:-1]
            return getattr(settings, var_name.lower(), value)
        return value
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v) for v in value]
    return value


def _discover_builtin_mcp() -> dict[str, dict]:
    """Auto-discover built-in MCP servers from manifest files.

    Each subdirectory under :attr:`settings.builtin_mcp_dir` is expected to
    contain a manifest.json. Two manifest types are supported:

    Local (spawns a command inside the agent image)::

        {
          "name": "web_search",
          "type": "local",
          "command": ["python3", "/opt/agent/builtin-mcp/web_search/..."],
          "environment": {"SEARXNG_URL": "${SEARXNG_URL}"},
          "enabled": true
        }

    Remote (connects to an MCP server reachable over the network — e.g. a
    platform-managed service such as fastk-mcp on agent-net)::

        {
          "name": "fastk",
          "type": "remote",
          "url": "${FASTK_MCP_URL}",
          "enabled": true
        }

    ``${VAR}`` placeholders in environment values and urls/headers are
    resolved against the platform settings (lowercased):
    ``${SEARXNG_URL}`` -> ``settings.searxng_url``.
    """
    mcp_dir = Path(settings.builtin_mcp_dir)
    if not mcp_dir.is_dir():
        return {}
    result: dict[str, dict] = {}
    for manifest_path in sorted(mcp_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            logger.warning("Skipping unreadable manifest: %s", manifest_path)
            continue
        name = manifest.get("name")
        if not name:
            logger.warning("Manifest missing 'name': %s", manifest_path)
            continue
        mcp_type = manifest.get("type", "local")
        entry: dict[str, Any] = {
            "type": mcp_type,
            "enabled": manifest.get("enabled", True),
        }
        if mcp_type == "remote":
            url = manifest.get("url")
            if not url:
                logger.warning("Remote manifest missing 'url': %s", manifest_path)
                continue
            entry["url"] = _resolve_placeholders(url)
            headers = manifest.get("headers")
            if headers:
                entry["headers"] = _resolve_placeholders(headers)
        else:
            entry["command"] = manifest.get("command", [])
            # Resolve ${VAR} placeholders in environment values
            entry["environment"] = _resolve_placeholders(manifest.get("environment", {}))
        result[name] = entry
    return result


def _apply_builtin_overrides(servers: dict[str, dict], overrides: dict) -> dict[str, dict]:
    """Apply host-config ``builtin_mcp`` enabled overrides to discovered servers.

    The manifolds under /builtin-mcp are read-only, so a user's enable/disable
    toggle is persisted in the host opencode.json under the top-level
    ``builtin_mcp`` key instead. Only the ``enabled`` field is honored.
    """
    for name, override in (overrides or {}).items():
        if name not in servers or not isinstance(override, dict):
            continue
        if "enabled" in override:
            servers[name]["enabled"] = bool(override["enabled"])
    return servers


def builtin_mcp_servers() -> dict[str, dict]:
    """Built-in MCP servers with any host-config enabled overrides applied.

    This is the single source of truth for what the platform considers a
    built-in MCP server, used both by :func:`build_container_config` (to
    inject them) and by the /api/config router (to list / toggle them).
    """
    servers = _discover_builtin_mcp()
    source, _ = load_source_config()
    overrides = (source.get("builtin_mcp") or {}) if isinstance(source, dict) else {}
    return _apply_builtin_overrides(servers, overrides)


def _discover_builtin_plugins() -> list[str]:
    """Auto-discover built-in opencode plugins from manifest files.

    Each subdirectory under :attr:`settings.builtin_plugins_dir` contains a
    manifest.json pointing at the plugin's install path inside the agent
    image. The plugin's full node_modules tree is pre-baked at image build
    time (see agent-image/Dockerfile), so referencing the package directory
    makes opencode load it in place — no npm install, no network, and the
    read-only rootfs keeps users from removing it::

        {
          "name": "oh-my-opencode-slim",
          "path": "/opt/agent/builtin-plugins/oh-my-opencode-slim/node_modules/oh-my-opencode-slim",
          "enabled": true
        }

    Returns the list of plugin paths to inject into the config's ``plugin``
    array.
    """
    plugins_dir = Path(settings.builtin_plugins_dir)
    if not plugins_dir.is_dir():
        return []
    result: list[str] = []
    for manifest_path in sorted(plugins_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            logger.warning("Skipping unreadable plugin manifest: %s", manifest_path)
            continue
        if not manifest.get("enabled", True):
            continue
        path = manifest.get("path")
        if not path:
            logger.warning("Plugin manifest missing 'path': %s", manifest_path)
            continue
        result.append(path)
    return result


# ------------------------------------------------------------------
#  Dynamic visibility — skills / MCP via permission rules
# ------------------------------------------------------------------

def _visibility_overrides(section: str, source: dict | None = None) -> dict[str, bool]:
    """Read ``{name: enabled}`` from a host-config visibility section."""
    if source is None:
        source, _ = load_source_config()
    raw = source.get(section) if isinstance(source, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        name: bool(entry.get("enabled", True))
        for name, entry in raw.items()
        if isinstance(entry, dict)
    }


def hidden_builtin_skills(source: dict | None = None) -> set[str]:
    """Built-in skills currently hidden from agents.

    Enforcement is two-pronged (verified against opencode 1.18.25 +
    oh-my-opencode-slim 2.2.15): the global ``permission.skill`` object gets a
    per-name deny rule (covers native agents like build/plan), and the plugin
    config's ``disabled_skills`` array makes the plugin append a deny entry to
    every plugin agent's skill permission — appended last, so it wins over the
    orchestrator's ``skills: ["*"]`` allow-all.
    """
    return {
        name
        for name, enabled in _visibility_overrides("builtin_skills", source).items()
        if not enabled
    }


def hidden_mcp_servers(source: dict | None = None) -> set[str]:
    """MCP servers currently hidden from agents.

    Built-in servers hide via the ``builtin_mcp`` override (enabled=False);
    host-declared remote servers hide via their own ``enabled`` field. The
    injected config always connects every MCP server (``enabled`` is forced
    on) — hiding is a permission ``deny`` rule on ``<sanitized>_*``, so a
    toggle is a permission flip that PATCH /global/config applies in ~2s
    instead of a 10-20s stop/inject/start cycle.
    """
    if source is None:
        source, _ = load_source_config()
    hidden = {
        name
        for name, enabled in _visibility_overrides("builtin_mcp", source).items()
        if not enabled
    }
    mcp = source.get("mcp") if isinstance(source, dict) else None
    if isinstance(mcp, dict):
        for name, cfg in mcp.items():
            if (
                isinstance(cfg, dict)
                and cfg.get("type") == "remote"
                and cfg.get("enabled") is False
            ):
                hidden.add(name)
    return hidden


def _sanitize_mcp_permission_key(name: str) -> str:
    """Mirror the plugin's MCP permission-key sanitization.

    oh-my-opencode-slim maps an MCP server to the permission key
    ``<sanitized>_*`` with ``sanitized = name.replace(/[^a-zA-Z0-9_-]/g, "_")``
    (index.js config hook); opencode's global permission uses the same shape
    for MCP tool rules.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


# ------------------------------------------------------------------
#  Plugin config rendering (oh-my-opencode-slim.json)
# ------------------------------------------------------------------

def _apply_list_visibility(items: Any, hidden: set[str]) -> Any:
    """Rewrite a preset ``mcps``/``skills`` list against the current hidden set.

    The plugin parses these lists with ``!name`` exclusion semantics
    (parseList in index.js): ``*`` allows everything minus explicit ``!name``
    exclusions; a plain whitelist is intersected with the available servers.
    The transform is idempotent — stale ``!name`` entries for names that are
    visible again are dropped, still-hidden names get an exclusion appended
    (allow-all lists) or are removed from the whitelist. It must also run
    with an empty hidden set, otherwise stale ``!name`` exclusions from an
    earlier hide would survive an un-hide.
    """
    if not isinstance(items, list):
        return items
    result: list[Any] = []
    for item in items:
        if not isinstance(item, str):
            result.append(item)
        elif item == "*":
            result.append(item)
        elif item.startswith("!") and item[1:] in hidden:
            result.append(item)
        elif not item.startswith("!") and item not in hidden:
            result.append(item)
    if "*" in result:
        for name in sorted(hidden):
            if f"!{name}" not in result:
                result.append(f"!{name}")
    return result


def apply_plugin_visibility(
    plugin_cfg: Any, hidden_skills: set[str], hidden_mcps: set[str]
) -> dict:
    """Apply the current visibility sets to a plugin config document.

    ``hidden_skills`` becomes the plugin's ``disabled_skills`` array — the
    plugin appends a skill-permission deny for each entry to every plugin
    agent (built-in, custom, ACP and councillor alike). ``hidden_mcps``
    rewrites every preset agent's ``mcps`` list, which the plugin translates
    into per-agent ``<mcp>_*`` permission rules.

    Used both when rendering the config from scratch (:func:`render_plugin_config`)
    and when toggling at runtime (read the container's current file,
    transform, write back). The backend owns these keys: they are
    authoritatively replaced, never merged, so toggling can also *un-hide*
    (opencode's mergeDeep PATCHes cannot delete keys).
    """
    cfg = copy.deepcopy(plugin_cfg) if isinstance(plugin_cfg, dict) else {}
    if hidden_skills:
        cfg["disabled_skills"] = sorted(hidden_skills)
    else:
        cfg.pop("disabled_skills", None)
    presets = cfg.get("presets")
    if isinstance(presets, dict):
        for preset in presets.values():
            if not isinstance(preset, dict):
                continue
            for agent_cfg in preset.values():
                if isinstance(agent_cfg, dict) and "mcps" in agent_cfg:
                    agent_cfg["mcps"] = _apply_list_visibility(agent_cfg["mcps"], hidden_mcps)
    return cfg


def render_plugin_config(
    model: str | None,
    hidden_skills: set[str] | None = None,
    hidden_mcps: set[str] | None = None,
) -> dict | None:
    """Render the full oh-my-opencode-slim config for a deployment.

    Mirrors agent-image/entrypoint.sh's preset generation (same agents, same
    allow-lists) plus the static ``agents.*.model`` pins from
    plugin.default.json — with the actually resolved model, because the static
    pins override the preset models in the plugin's deepMerge. Returns None
    when no model is resolved (the entrypoint skips generation in that case
    too; the image-seeded plugin.default.json remains in place).
    """
    if not model:
        return None
    preset: dict[str, Any] = {}
    for agent in _PRESET_AGENTS:
        entry: dict[str, Any] = {"model": model, "skills": [], "mcps": []}
        if agent == "orchestrator":
            entry["skills"] = ["*"]
            entry["mcps"] = ["*"]
        elif agent == "librarian":
            entry["mcps"] = ["web_search"]
        preset[agent] = entry
    cfg: dict[str, Any] = {
        "autoUpdate": False,
        "agents": {name: {"model": model} for name in _PINNED_AGENTS},
        "preset": "container",
        "presets": {"container": preset},
    }
    return apply_plugin_visibility(cfg, hidden_skills or set(), hidden_mcps or set())


def build_container_config(
    user_provider: dict | None = None,
    user_mcp: dict | None = None,
    active_provider_id: str | None = None,
    active_model: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Produce the config document to write into a user's container.

    Host config supplies the platform baseline (providers, remote MCP servers,
    defaults); the three ``user_*`` arguments layer per-user content on top:
    ``user_provider`` / ``user_mcp`` are the decrypted user LLM providers and
    MCP servers, and ``active_provider_id`` / ``active_model`` decide the
    default ``model``. ``user_id`` scopes user-provider proxy routing.
    """
    source, origin = load_source_config()

    sanitized = copy.deepcopy(source)
    dropped = [key for key in HOST_ONLY_KEYS if key in sanitized]
    for key in dropped:
        sanitized.pop(key, None)

    # Filter MCP servers: keep remote (URL-based), drop local (command-based).
    # Local MCP servers reference host executables that don't exist in the
    # container; remote ones are reachable over the network.
    mcp = sanitized.get("mcp")
    if isinstance(mcp, dict):
        sanitized["mcp"] = {
            name: cfg for name, cfg in mcp.items()
            if isinstance(cfg, dict) and cfg.get("type") == "remote"
        }
        if not sanitized["mcp"]:
            sanitized.pop("mcp", None)

    # Inject built-in MCP servers discovered from manifest files under
    # builtin_mcp_dir. These run inside the agent image itself (the command
    # paths reference /opt/agent/builtin-mcp/...), so they cannot be removed
    # by the user. Environment variables with ${VAR} placeholders are resolved
    # against platform settings at discovery time.
    builtin_mcp = builtin_mcp_servers()
    if builtin_mcp:
        sanitized.setdefault("mcp", {}).update(builtin_mcp)

    # Inject built-in plugins (pre-baked into the read-only agent image at
    # build time, so users cannot remove them). The user's own plugin entries
    # were stripped above (HOST_ONLY_KEYS) — they reference host paths or npm
    # specs that would need an install at boot. opencode loads bare-path
    # plugin specs in place; dependencies resolve from the sibling
    # node_modules directories of the pre-baked tree.
    builtin_plugins = _discover_builtin_plugins()
    if builtin_plugins:
        user_plugins = sanitized.get("plugin")
        plugins = [p for p in user_plugins if isinstance(p, str)] if isinstance(user_plugins, list) else []
        sanitized["plugin"] = builtin_plugins + plugins

    sanitized = _walk(sanitized, settings.container_host_alias)

    merged = _deep_merge(CONTAINER_DEFAULTS, sanitized)

    # Layer per-user MCP servers and LLM providers on top of the host baseline.
    if user_mcp:
        merged.setdefault("mcp", {}).update(user_mcp)
    if user_provider:
        merged.setdefault("provider", {}).update(user_provider)

    # opencode needs a default model or the first prompt has nothing to run on.
    provider = merged.get("provider") or {}

    # An explicit active selection wins over the deterministic fallback.
    if active_provider_id and active_provider_id in provider:
        model_id = active_model or _first_model_of(provider[active_provider_id])
        if model_id:
            merged["model"] = f"{active_provider_id}/{model_id}"

    if not merged.get("model"):
        fallback = _first_model(provider)
        if fallback:
            merged["model"] = fallback

    # opencode tries to pick a cheaper model from the provider for lightweight
    # tasks (title generation etc.). If none exists it falls back to the main
    # model, but the fallback resolution has a bug where it reports
    # "Model unavailable" and poisons the session runner. Setting small_model
    # explicitly to the main model avoids the faulty resolution path entirely.
    if merged.get("model") and not merged.get("small_model"):
        merged["small_model"] = merged["model"]

    # Rewrite agent model references to point at the configured provider.
    # The host config may reference models like "gpt-4o-mini" that belong to
    # providers not available in the container; opencode silently falls back to
    # its built-in OpenCode Zen provider (https://opencode.ai/zen/v1), which
    # returns 403 for restricted regions.
    default_model = merged.get("model")
    if default_model:
        agents = merged.get("agents") or {}
        for agent_id, agent_cfg in agents.items():
            if not isinstance(agent_cfg, dict):
                continue
            # Force every agent to use the configured provider's model.
            agent_cfg["model"] = default_model

    # Remove option keys that the container's @ai-sdk/openai-compatible runtime
    # considers protocol-owned (thinking budget etc.) — they cause a hard 400
    # at the LLM call boundary.
    merged = _strip_incompatible_model_options(merged)

    # Route every configured provider through the platform LLM proxy
    # (backend /llm-proxy router). The proxy forwards to the real upstream
    # while normalizing SSE tool-call deltas: some gateways (deepseek-v4-pro
    # behind volces) send id:""/name:"" on continuation chunks, which
    # @ai-sdk/openai-compatible treats as a hard stream error. Empty-string
    # fields are stripped so ai-sdk's nullish-merge falls back to the values
    # already accumulated from the tool call's first chunk.
    provider = merged.get("provider") or {}
    for provider_id, prov in provider.items():
        opts = prov.get("options") if isinstance(prov, dict) else None
        if (
            isinstance(opts, dict)
            and isinstance(opts.get("baseURL"), str)
            and opts["baseURL"]
        ):
            if user_id and user_provider and provider_id in user_provider:
                # User providers are routed through a user-scoped proxy path so
                # the backend can resolve them independently of the host config.
                # The ``_user`` segment disambiguates from the host route
                # (``/{provider_id}/{path}``); underscores are not valid in a
                # provider slug, so it can never collide with a real id.
                opts["baseURL"] = f"{settings.llm_proxy_base.rstrip('/')}/_user/{user_id}/{provider_id}"
            else:
                opts["baseURL"] = f"{settings.llm_proxy_base.rstrip('/')}/{provider_id}"

    # Only allow providers defined in the user's config. opencode ships with a
    # global model catalogue (wandb, nvidia, opencode Zen, etc.) that it
    # downloads on boot. Without this whitelist, opencode may silently route
    # requests to those built-in providers — which return 403 for restricted
    # regions — instead of the user's configured provider.
    configured_providers = sorted((merged.get("provider") or {}).keys())
    if configured_providers:
        merged["enabled_providers"] = configured_providers

    # ------------------------------------------------------------------
    # Dynamic visibility: hidden skills / MCP servers become permission deny
    # rules instead of being excluded from the config. Every MCP server stays
    # connected (``enabled`` forced on); hiding is enforced by opencode's
    # permission evaluation, which a runtime PATCH /global/config can flip
    # without restarting the container.
    # ------------------------------------------------------------------
    hidden_skills = hidden_builtin_skills(source)
    hidden_mcps = hidden_mcp_servers(source)

    for entry in (merged.get("mcp") or {}).values():
        if isinstance(entry, dict):
            entry["enabled"] = True

    if hidden_skills or hidden_mcps:
        permission = merged.setdefault("permission", {})
        if hidden_skills:
            # permission.skill accepts either an action string (shorthand for
            # {"*": action}) or a {name: action} object where specific keys
            # win over "*" (opencode PermissionRuleSchema union). Normalize to
            # the object form and deny each hidden skill by name.
            skill_rules = permission.get("skill")
            if not isinstance(skill_rules, dict):
                skill_rules = {
                    "*": skill_rules if isinstance(skill_rules, str) else "allow"
                }
            for name in hidden_skills:
                skill_rules[name] = "deny"
            permission["skill"] = skill_rules
        for name in hidden_mcps:
            permission[f"{_sanitize_mcp_permission_key(name)}_*"] = "deny"

    logger.info(
        "Built container opencode config from %s (providers=%s, dropped=%s, "
        "model=%s, builtin_plugins=%s, hidden_skills=%d, hidden_mcp=%d)",
        origin,
        ",".join(sorted(provider)) or "-",
        ",".join(dropped) or "-",
        merged.get("model", "-"),
        len(merged.get("plugin") or []),
        len(hidden_skills),
        len(hidden_mcps),
    )
    return merged


def build_container_config_json(
    user_provider: dict | None = None,
    user_mcp: dict | None = None,
    active_provider_id: str | None = None,
    active_model: str | None = None,
    user_id: str | None = None,
) -> str:
    """Serialize :func:`build_container_config` to the injected JSON string."""
    return json.dumps(
        build_container_config(
            user_provider=user_provider,
            user_mcp=user_mcp,
            active_provider_id=active_provider_id,
            active_model=active_model,
            user_id=user_id,
        ),
        ensure_ascii=False,
        indent=2,
    )


def describe_source() -> dict:
    """Diagnostics for the /api/agent/status panel — never leaks secrets."""
    source, origin = load_source_config()
    provider = (source.get("provider") or {}) if isinstance(source, dict) else {}
    mcp = (source.get("mcp") or {}) if isinstance(source, dict) else {}
    return {
        "source": origin,
        "mounted": origin not in ("none",) and not origin.endswith(")"),
        "providers": sorted(provider),
        "mcp_servers": sorted(mcp),
        "stripped": [key for key in HOST_ONLY_KEYS if key in source],
        "builtin_plugins": _discover_builtin_plugins(),
        "host_alias": settings.container_host_alias,
    }
