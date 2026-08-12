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

# `plugin` entries trigger an npm install at boot — that breaks in a hardened,
# read-only container. `mcp` is now managed via the host_config API and filtered
# per-entry: remote MCP servers (URL-based) are kept, local ones (command-based)
# are stripped because they reference host executables.
HOST_ONLY_KEYS = ("plugin",)

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
    },
}


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


def _first_model(provider: dict) -> str | None:
    """Pick a deterministic provider/model pair to use as the default.

    opencode's config file expects model/small_model as "provider/model" strings.
    (The session creation API takes Model.Ref objects, but that's a different
    context — the config file format is string.)
    """
    for provider_id in sorted(provider):
        models = provider[provider_id].get("models") or {}
        for model_id in sorted(models):
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


def build_container_config() -> dict:
    """Produce the config document to write into a user's container."""
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

    sanitized = _walk(sanitized, settings.container_host_alias)

    merged = _deep_merge(CONTAINER_DEFAULTS, sanitized)

    # opencode needs a default model or the first prompt has nothing to run on.
    provider = merged.get("provider") or {}
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

    # Only allow providers defined in the user's config. opencode ships with a
    # global model catalogue (wandb, nvidia, opencode Zen, etc.) that it
    # downloads on boot. Without this whitelist, opencode may silently route
    # requests to those built-in providers — which return 403 for restricted
    # regions — instead of the user's configured provider.
    configured_providers = sorted((merged.get("provider") or {}).keys())
    if configured_providers:
        merged["enabled_providers"] = configured_providers

    logger.info(
        "Built container opencode config from %s (providers=%s, dropped=%s, model=%s)",
        origin,
        ",".join(sorted(provider)) or "-",
        ",".join(dropped) or "-",
        merged.get("model", "-"),
    )
    return merged


def build_container_config_json() -> str:
    return json.dumps(build_container_config(), ensure_ascii=False, indent=2)


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
        "host_alias": settings.container_host_alias,
    }
