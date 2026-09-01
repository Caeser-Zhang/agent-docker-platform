# Per-user agent container.
#
# The ONLY process in this image is `opencode serve` — the real headless
# opencode agent runtime. It owns the agent loop, the LLM calls, the tools and
# the session store. The platform never re-implements any of that; it just
# manages this container's lifecycle and reverse-proxies HTTP into it.
#
#   docker build -t agent-demo:1.2.0 ./agent-image
#
# Layout inside the container:
#   /workspace          project root opencode operates on   (volume, rw)
#   /data/config        XDG_CONFIG_HOME -> opencode/opencode.json injected
#   /data/share         XDG_DATA_HOME   -> sessions, messages, tool output
#   /data/cache         XDG_CACHE_HOME
#   /data/state         XDG_STATE_HOME
# Everything else is a read-only rootfs.

# ---------------------------------------------------------------- fetch stage
# Base images are ARGs so a constrained network can point them at whatever is
# already in the local cache (`--build-arg NODE_IMAGE=node:20-slim`).
ARG NODE_IMAGE=node:20-slim
ARG RUNTIME_IMAGE=debian:bookworm-slim

FROM ${NODE_IMAGE} AS fetch

ARG OPENCODE_VERSION=1.18.25
# Pinned so builds are reproducible — bump to upgrade the pre-baked plugin.
ARG OMOS_PLUGIN_VERSION=2.2.15

WORKDIR /fetch
# The npm package resolves an optional platform dependency (opencode-linux-x64)
# and its postinstall drops the standalone binary at bin/opencode.exe. Despite
# the extension it is a plain Linux ELF executable — the provider SDKs are
# compiled into it, so no further npm installs are needed at runtime.
#
# We also pre-install @ai-sdk/openai-compatible into /fetch/provider-deps so it
# can be copied into the container's opencode config dir at runtime. opencode's
# background dependency installer tries to npm install provider packages at boot
# but the runtime stage has no npm/node, and when it did work it hit ENOSPC on
# the small tmpfs. Pre-baking them eliminates that failure mode entirely.
RUN npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund --omit=dev "opencode-ai@${OPENCODE_VERSION}" \
    && install -m 0755 node_modules/opencode-ai/bin/opencode.exe /opencode \
    && /opencode --version \
    && mkdir -p /fetch/provider-deps/node_modules \
    && cd /fetch/provider-deps \
    && npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund --omit=dev @ai-sdk/openai-compatible

# Pre-download the oh-my-opencode-slim plugin with its FULL dependency tree.
# Doing this at build time means:
#   - no npm install when a user container boots (fast starts, no network)
#   - the ast-grep native binary ships inside the tree (npm pulls the
#     platform-specific @ast-grep/cli-* package AND runs the @ast-grep/cli
#     postinstall here, where node is available), so the plugin never falls
#     back to downloading from GitHub releases at runtime
#   - the tree lives in the read-only image — users cannot remove or tamper
#     with the plugin
# The runtime stage copies it to /opt/agent/builtin-plugins/oh-my-opencode-slim
# and the backend injects the package path into every user's opencode.json
# (opencode loads bare-path plugins in place; dependencies resolve from the
# sibling node_modules directories).
# node_modules is pre-created because npm walks up the tree looking for the
# nearest package.json OR node_modules to decide its install root. /fetch has
# a package.json (from the opencode-ai install above) — without a local
# anchor the plugin tree would silently land in /fetch/node_modules.
RUN mkdir -p /fetch/builtin-plugins/oh-my-opencode-slim/node_modules \
    && cd /fetch/builtin-plugins/oh-my-opencode-slim \
    && npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund --omit=dev "oh-my-opencode-slim@${OMOS_PLUGIN_VERSION}" \
    # Fail the build early if the plugin entry point or the ast-grep platform
    # package didn't make it into the tree — without either, the plugin would
    # try to reach GitHub from inside the (offline) user container.
    && test -f node_modules/oh-my-opencode-slim/dist/index.js \
    && ls node_modules/@ast-grep/ | grep -q '^cli-linux'

# -------------------------------------------------------------- runtime stage
FROM ${RUNTIME_IMAGE}

ARG AGENT_UID=1000
ARG AGENT_GID=1000

# Swap deb.debian.org for a mirror that has IPv4 AAAA records, since WSL2's
# DNS often returns IPv6-only addresses and buildkit's runc containers have no
# IPv6 default route — apt-get hangs with zero network throughput.
RUN sed -i 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null; \
    sed -i 's|http://security.debian.org|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|http://security.debian.org|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list 2>/dev/null; \
    true

# ripgrep / git / fd are what opencode's built-in grep, glob and vcs tools shell
# out to. curl is used by the container HEALTHCHECK.
# Additional CLI tools are declared in builtin-tools/packages.list (one apt
# package per line, # for comments) and appended here at build time.
COPY builtin-tools/packages.list /tmp/builtin-packages.list
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fd-find \
        git \
        less \
        python3 \
        ripgrep \
        $(grep -E '^[a-z0-9][a-z0-9+.-]*$' /tmp/builtin-packages.list | tr '\n' ' ') \
    && rm -rf /var/lib/apt/lists/* /tmp/builtin-packages.list \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    # Some bookworm-derived bases (node:*-slim) already ship a uid/gid 1000
    # account. Reuse it instead of failing, so RUNTIME_IMAGE can be swapped for
    # whatever the local cache happens to hold.
    && (getent group "${AGENT_GID}" >/dev/null || groupadd -g "${AGENT_GID}" agent) \
    && (getent passwd "${AGENT_UID}" >/dev/null \
        || useradd -m -u "${AGENT_UID}" -g "${AGENT_GID}" -s /bin/bash agent) \
    # HOME is pinned to /home/agent below, so create it explicitly rather than
    # relying on whatever home dir the reused account came with.
    && install -d -o "${AGENT_UID}" -g "${AGENT_GID}" /workspace /data /home/agent

COPY --from=fetch /opencode /usr/local/bin/opencode
# Pre-baked provider SDK packages so opencode doesn't need npm at runtime.
COPY --from=fetch /fetch/provider-deps /opt/agent/provider-deps
COPY opencode.default.json /opt/agent/opencode.default.json
# Built-in MCP servers — each subdirectory of builtin-mcp/ declares one server
# via a manifest.json (mcp config) plus its executable(s). The backend discovers
# the manifests and injects the servers into every user container. The scripts
# themselves live in the read-only image and cannot be removed by users.
COPY builtin-mcp/ /opt/agent/builtin-mcp/
# Built-in opencode plugins — pre-installed node_modules trees fetched above.
# The source builtin-plugins/ dir (manifest.json + plugin.default.json per
# plugin) is copied OVER the fetched tree: the manifest drives backend-side
# discovery/injection, plugin.default.json seeds the plugin's own config file
# on first boot (see entrypoint.sh).
COPY --from=fetch /fetch/builtin-plugins /opt/agent/builtin-plugins
COPY builtin-plugins/ /opt/agent/builtin-plugins/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# fastk CLI — stdlib-only client for the FastDB knowledge-base REST API,
# the tool the bundled fastk-search / fastk-analyze skills instruct on.
COPY builtin-tools/fastk-cli/fastk /usr/local/bin/fastk
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/opencode /usr/local/bin/fastk

# WORKAROUND（oven-sh/bun#32178）：Bun 1.3.14 的 parallel-GC 存在确定性竞态，
# bash 工具 spawn 子进程后 drain microtask 会 segment fault（Bun 1.3.15 已修复）。
# opencode 内置的 Bun 仍停在 1.3.14，故用单线程 GC 绕过；等 opencode 内嵌
# Bun >= 1.3.15 后可移除下面这行。
ENV HOME=/home/agent \
    XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/share \
    XDG_CACHE_HOME=/data/cache \
    XDG_STATE_HOME=/data/state \
    AGENT_WORKDIR=/workspace \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_PORT=4096 \
    BUN_JSC_numberOfGCMarkers=1

WORKDIR /workspace
USER 1000:1000
EXPOSE 4096

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=4 \
    CMD curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" \
        "http://127.0.0.1:${OPENCODE_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve", "--hostname", "0.0.0.0"]
