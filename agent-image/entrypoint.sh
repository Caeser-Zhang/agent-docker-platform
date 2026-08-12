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
