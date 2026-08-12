# Per-user agent container.
#
# The ONLY process in this image is `opencode serve` — the real headless
# opencode agent runtime. It owns the agent loop, the LLM calls, the tools and
# the session store. The platform never re-implements any of that; it just
# manages this container's lifecycle and reverse-proxies HTTP into it.
#
#   docker build -t agent-demo:1.0.0 ./agent-image
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

ARG OPENCODE_VERSION=1.18.16

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
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fd-find \
        git \
        less \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
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
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/opencode

ENV HOME=/home/agent \
    XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/share \
    XDG_CACHE_HOME=/data/cache \
    XDG_STATE_HOME=/data/state \
    AGENT_WORKDIR=/workspace \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_PORT=4096

WORKDIR /workspace
USER 1000:1000
EXPOSE 4096

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=4 \
    CMD curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" \
        "http://127.0.0.1:${OPENCODE_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve", "--hostname", "0.0.0.0"]
