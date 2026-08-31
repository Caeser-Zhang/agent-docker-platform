#!/usr/bin/env bash
# Prepare the XDG tree on the (initially empty) /data volume, then hand the
# process over to `opencode serve`. Nothing else runs in this container.
set -euo pipefail

: "${XDG_CONFIG_HOME:=/data/config}"
: "${XDG_DATA_HOME:=/data/share}"
: "${XDG_CACHE_HOME:=/data/cache}"
: "${XDG_STATE_HOME:=/data/state}"
: "${AGENT_WORKDIR:=/workspace}"
: "${OPENCODE_PORT:=4096}"

mkdir -p \
    "${XDG_CONFIG_HOME}/opencode" \
    "${XDG_DATA_HOME}" \
    "${XDG_CACHE_HOME}" \
    "${XDG_STATE_HOME}" \
    "${AGENT_WORKDIR}"

CONFIG_FILE="${XDG_CONFIG_HOME}/opencode/opencode.json"
PROVIDER_DEPS_DIR="${XDG_CONFIG_HOME}/opencode/node_modules"

# The platform injects the sanitized host config before starting us. If that
# did not happen (standalone `docker run`, first boot, injection failure) fall
# back to the config baked into the image so the server still comes up.
if [ ! -s "${CONFIG_FILE}" ]; then
    echo "[entrypoint] no injected config, using image default" >&2
    cp /opt/agent/opencode.default.json "${CONFIG_FILE}"
fi

# Pre-baked provider SDK packages (e.g. @ai-sdk/openai-compatible) are copied
# into the image at build time. opencode looks for them in
# ${XDG_CONFIG_HOME}/opencode/node_modules — symlink the pre-baked tree there
# so it doesn't try to npm install at boot (which would fail: no npm in the
# runtime image, and the tmpfs used to be too small).
if [ ! -d "${PROVIDER_DEPS_DIR}" ] && [ -d /opt/agent/provider-deps/node_modules ]; then
    echo "[entrypoint] linking pre-baked provider SDKs" >&2
    mkdir -p "${XDG_CONFIG_HOME}/opencode"
    cp -a /opt/agent/provider-deps/node_modules "${PROVIDER_DEPS_DIR}"
    cp -a /opt/agent/provider-deps/package.json "${XDG_CONFIG_HOME}/opencode/package.json" 2>/dev/null || true
    cp -a /opt/agent/provider-deps/package-lock.json "${XDG_CONFIG_HOME}/opencode/package-lock.json" 2>/dev/null || true
fi

# Built-in plugins ship a default config next to their pre-baked node_modules
# tree: /opt/agent/builtin-plugins/<name>/plugin.default.json seeds
# ${XDG_CONFIG_HOME}/opencode/<name>.json. First boot only — afterwards the
# user may tune the plugin's config freely. Defaults keep autoUpdate off
# because the plugin tree lives in the read-only image and must never try to
# replace itself at runtime.
for default_cfg in /opt/agent/builtin-plugins/*/plugin.default.json; do
    [ -f "${default_cfg}" ] || continue
    plugin_name="$(basename "$(dirname "${default_cfg}")")"
    target="${XDG_CONFIG_HOME}/opencode/${plugin_name}.json"
    if [ ! -s "${target}" ]; then
        echo "[entrypoint] seeding plugin config: ${target}" >&2
        cp "${default_cfg}" "${target}"
    fi
done

# NOTE: oh-my-opencode-slim's plugin.default.json pins agents.*.model to the
# platform gateway model. At runtime the plugin merges
# config.agents = deepMerge(preset, config.agents) — the static agents keys in
# the seeded oh-my-opencode-slim.json override the deployment-specific preset
# models generated below. Keep the pin in sync when switching providers, or
# drop the agents.*.model entries from that file to fall back to the dynamic
# presets here.

# oh-my-opencode-slim reads per-agent prompt overrides from
# ${XDG_CONFIG_HOME}/opencode/oh-my-opencode-slim/<agent>.md (plugin's
# loadAgentPrompt: file prompt replaces the built-in fallback entirely).
# The image ships a verbatim snapshot of every built-in subagent prompt under
# /opt/agent/builtin-plugins/oh-my-opencode-slim/prompts/ — seed those files on
# first boot so operators can tune prompts without rebuilding the image. Per-file
# "not exists" check: user-edited prompts survive reboots; files added in newer
# images get seeded. Only the 8 agent prompt files are seeded — README.md and
# tools-permissions.md are documentation, agents.manifest.json is metadata.
OMO_PROMPTS_SRC="/opt/agent/builtin-plugins/oh-my-opencode-slim/prompts"
OMO_PROMPTS_DIR="${XDG_CONFIG_HOME}/opencode/oh-my-opencode-slim"
if [ -d "${OMO_PROMPTS_SRC}" ]; then
    mkdir -p "${OMO_PROMPTS_DIR}"
    for prompt_file in "${OMO_PROMPTS_SRC}"/*.md; do
        [ -f "${prompt_file}" ] || continue
        base="$(basename "${prompt_file}")"
        case "${base}" in
            README.md|tools-permissions.md) continue ;;
        esac
        prompt_target="${OMO_PROMPTS_DIR}/${base}"
        if [ ! -s "${prompt_target}" ]; then
            echo "[entrypoint] seeding agent prompt: ${prompt_target}" >&2
            cp "${prompt_file}" "${prompt_target}"
        fi
    done
fi

# oh-my-opencode-slim's official installer generates a config that maps every
# subagent (orchestrator, oracle, ...) to a model via preset/presets. Without
# that mapping the plugin's agents come up with model=null and every tool call
# fails with an opaque "unknown" error. The model is deployment-specific (it
# comes from the injected opencode.json), so generate the preset config at
# boot from the resolved default model instead of shipping a static file.
# Existing configs that already define presets are left untouched.
OMO_CFG="${XDG_CONFIG_HOME}/opencode/oh-my-opencode-slim.json"
OMO_MODEL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("model",""))' "${CONFIG_FILE}" 2>/dev/null || true)"
if [ -n "${OMO_MODEL}" ]; then
    python3 - "${OMO_CFG}" "${OMO_MODEL}" <<'PYEOF'
import json, os, sys

path, model = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except Exception:
        cfg = {}

if not cfg.get("presets"):
    agents = {}
    for agent in ("orchestrator", "oracle", "librarian", "explorer", "designer", "fixer"):
        entry = {"model": model, "skills": [], "mcps": []}
        if agent == "orchestrator":
            entry["skills"] = ["*"]
            entry["mcps"] = ["*"]
        elif agent == "librarian":
            entry["mcps"] = ["web_search"]
        agents[agent] = entry
    cfg["preset"] = "container"
    cfg["presets"] = {"container": agents}
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"[entrypoint] generated oh-my-opencode-slim preset config (model={model})", file=sys.stderr)
PYEOF
fi

# opencode shells out to git for its vcs tools; the workspace volume is owned by
# a different uid than the one that created it in some Docker setups.
git config --global --add safe.directory "${AGENT_WORKDIR}" 2>/dev/null || true
git config --global user.email "agent@localhost" 2>/dev/null || true
git config --global user.name "opencode agent" 2>/dev/null || true

# opencode caches a global model catalogue (including built-in providers like
# nvidia, wandb, etc.) at /data/cache/opencode/models.json. If this cache exists
# from a previous run, opencode may use cached models instead of the configured
# provider, causing 403 errors when the cached provider doesn't support the
# current region. Remove it so every boot starts fresh.
rm -f "${XDG_CACHE_HOME}/opencode/models.json" 2>/dev/null || true

cd "${AGENT_WORKDIR}"

echo "[entrypoint] opencode $(opencode --version) serving on 0.0.0.0:${OPENCODE_PORT}" >&2
echo "[entrypoint] config: ${CONFIG_FILE}" >&2

# `--port` comes last so it always wins over anything passed in CMD.
exec opencode "$@" --port "${OPENCODE_PORT}"
