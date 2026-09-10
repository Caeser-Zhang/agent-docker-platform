# Per-user agent container.
#
# The ONLY process in this image is `opencode serve` — the real headless
# opencode agent runtime. It owns the agent loop, the LLM calls, the tools and
# the session store. The platform never re-implements any of that; it just
# manages this container's lifecycle and reverse-proxies HTTP into it.
#
#   docker build -t agent-demo:1.4.0 ./agent-image
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

# Pre-bake the present-file plugin's single runtime dependency: zod.
# The plugin source itself (index.js + package.json + manifest.json) lives in
# the repo at builtin-plugins/present-file/ and is overlaid onto this tree by
# the runtime stage's `COPY builtin-plugins/`, so node_modules ends up as a
# sibling of index.js and bare `import { z } from "zod"` resolves in place.
#
# zod MUST be v4. opencode's tool registry (tool/registry.ts) recognises plugin
# tool args with `"_zod" in value` — a v4-only internal (v3 schemas carry
# `_def`). A v3 shape is silently treated as non-zod and falls through to
# legacyJsonSchema(), which passes the schema objects straight through as if
# they were JSON Schema, handing the LLM a broken tool signature that it can
# never call successfully. The smoke test asserts both the v4 marker and the
# exact conversion the registry performs (z.object(args) + toJSONSchema), so a
# future downgrade or a zod major bump fails the build rather than the runtime.
RUN mkdir -p /fetch/builtin-plugins/present-file/node_modules \
    && cd /fetch/builtin-plugins/present-file \
    && npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund --omit=dev "zod@^4.1.0" \
    && test -f node_modules/zod/package.json \
    && node -e "const{z}=require('zod');const s=z.string().describe('x');if(!s||typeof s!=='object'||!('_zod' in s))throw new Error('zod is not v4: plugin tool args need the _zod marker');const js=z.toJSONSchema(z.object({path:s}),{io:'input'});if(js.type!=='object'||js.properties.path.type!=='string')throw new Error('zod v4 JSON Schema conversion unusable');console.log('[build] zod '+require('zod/package.json').version+' v4 smoke test OK')"

# Pre-bake the pptx-generator skill's Node dependency: pptxgenjs@4.0.1.
# Pure JS (jszip & friends, no native addons), so the tree built here on
# node:20-slim runs fine on the runtime stage's glibc — same trick as the
# provider-deps above. The runtime stage has no npm; the tree is copied to
# /opt/agent/skill-envs/pptx/ and consumed through the pptx-node wrapper
# (see builtin-tools/pptx/ and builtin-skills/pptx-generator/).
RUN mkdir -p /fetch/skill-envs/pptx/node_modules \
    && cd /fetch/skill-envs/pptx \
    && printf '{"name":"pptx-skill-env","private":true}\n' > package.json \
    && npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund --omit=dev "pptxgenjs@4.0.1" \
    # Fail the build early if the install produced something unusable.
    && node -e "const P=require('pptxgenjs');const p=new P();if(typeof p.addSlide!=='function')throw new Error('pptxgenjs smoke test failed')" \
    && test -f node_modules/pptxgenjs/package.json

# Pre-bake the pretty-mermaid skill's Node dependencies:
# beautiful-mermaid@1.1.3 (pure JS) + @resvg/resvg-js@2.6.2 (native addon —
# npm pulls the linux binary matching THIS build platform via optionalDependencies,
# e.g. resvg-js-linux-x64-gnu on amd64; NAPI so it is Node-version agnostic).
# Versions match builtin-skills/pretty-mermaid/package-lock.json. Consumed
# through the mermaid-node wrapper (see builtin-tools/pretty-mermaid/); the
# node binary itself is shared with the pptx env to avoid a second ~110MB copy.
RUN mkdir -p /fetch/skill-envs/pretty-mermaid \
    && cd /fetch/skill-envs/pretty-mermaid \
    && printf '{"name":"pretty-mermaid-skill-env","private":true}\n' > package.json \
    && npm install --no-audit --no-fund --omit=dev "beautiful-mermaid@1.1.3" "@resvg/resvg-js@2.6.2" \
    # Fail the build early if either package is unusable (ESM entry resolves,
    # native resvg binding directory present).
    && node -e "import('beautiful-mermaid').then(m=>{if(!m||typeof m!=='object')throw new Error('beautiful-mermaid smoke test failed');console.log('[build] beautiful-mermaid smoke test OK')}).catch(e=>{console.error(e);process.exit(1)})" \
    && node -e "const{Resvg}=require('@resvg/resvg-js');const r=new Resvg('<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"8\" height=\"8\"></svg>').render();if(r.asPng().length===0)throw new Error('resvg render smoke test failed');console.log('[build] @resvg/resvg-js native smoke test OK')" \
    && test -d node_modules/@resvg/resvg-js \
    && test -f node_modules/beautiful-mermaid/package.json

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
# package per line, # for comments) and appended here at build time. `tr -d '\r'`
# guards against Windows checkouts flipping the file to CRLF — with \r still in
# the lines, the anchored grep below matches NOTHING and every listed package
# is silently skipped (that's exactly how python3-venv went missing once).
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
        $(tr -d '\r' < /tmp/builtin-packages.list | grep -E '^[a-z0-9][a-z0-9+.-]*$' | tr '\n' ' ') \
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
# Platform-level global instructions (the present_file delivery rules). The
# entrypoint seeds this to ${XDG_CONFIG_HOME}/opencode/AGENTS.md on first boot;
# opencode's Instruction service reads that path as its *global* instruction
# file and appends it to every system prompt, additive with any project-level
# AGENTS.md under /workspace (both are injected, each prefixed with
# "Instructions from: <path>").
#
# The repo-side file is deliberately NOT named AGENTS.md: opencode discovers
# instruction files by name (AGENTS.md / CLAUDE.md / CONTEXT.md) walking up from
# the working directory, so an AGENTS.md sitting in this repo would be loaded as
# project instructions by every agent working *on* agent-docker-demo itself —
# telling it to call a present_file tool that does not exist outside a user
# container. Renaming the source isolates the two; the name inside the image is
# what matters.
COPY platform-instructions.md /opt/agent/AGENTS.md
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
# Isolated Node runtime + pre-baked pptxgenjs@4.0.1 for the pptx-generator
# skill (builtin-skills/pptx-generator/). Node deliberately stays OFF the
# global PATH — the runtime image must not grow a general Node stack; the
# pptx-node wrapper is the only supported entry point. node:20-slim and
# bookworm-slim share glibc, so the fetch-stage binary runs here with just
# libstdc++6 (added to builtin-tools/packages.list).
COPY --from=fetch /usr/local/bin/node /opt/agent/skill-envs/pptx/bin/node
COPY --from=fetch /fetch/skill-envs/pptx/node_modules /opt/agent/skill-envs/pptx/node_modules
COPY --from=fetch /fetch/skill-envs/pptx/package.json /opt/agent/skill-envs/pptx/package.json
# Pre-baked pretty-mermaid deps (beautiful-mermaid + native @resvg/resvg-js).
# Reuses the pptx env's node binary — see builtin-tools/pretty-mermaid/mermaid-node.
# entrypoint.sh symlinks this tree into the seeded skill dir at every boot so
# the scripts' ESM bare imports resolve (NODE_PATH does not apply to ESM).
COPY --from=fetch /fetch/skill-envs/pretty-mermaid/node_modules /opt/agent/skill-envs/pretty-mermaid/node_modules
COPY --from=fetch /fetch/skill-envs/pretty-mermaid/package.json /opt/agent/skill-envs/pretty-mermaid/package.json
# markitdown[pptx] (PPTX text extraction) in a dedicated venv. The venv is
# created HERE, not on the fetch stage: venv bin/ scripts hard-code the
# absolute interpreter path, so a venv built around the node image's
# /usr/local/bin/python would be dead on arrival. PIP_USER /
# PIP_BREAK_SYSTEM_PACKAGES (ENV below) do not affect this RUN — an ENV
# directive only takes effect for later instructions — so pip installs
# straight into the venv as intended.
RUN python3 -m venv /opt/agent/skill-envs/pptx/venv \
    && /opt/agent/skill-envs/pptx/venv/bin/pip install --no-cache-dir \
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple "markitdown[pptx]" \
    && /opt/agent/skill-envs/pptx/venv/bin/python -c "import markitdown, pptx" \
    && /opt/agent/skill-envs/pptx/bin/node --version
# pptx-node / pptx-markitdown — the skill's supported entry points (the
# commands baked into builtin-skills/pptx-generator/SKILL.md).
# mermaid-node — same role for builtin-skills/pretty-mermaid/SKILL.md.
COPY builtin-tools/pptx/pptx-node builtin-tools/pptx/pptx-markitdown /usr/local/bin/
COPY builtin-tools/pretty-mermaid/mermaid-node /usr/local/bin/
# Built-in skills, seeded into the user's XDG skills dir on first boot by
# entrypoint.sh (same first-boot-only semantics as plugin.default.json).
COPY builtin-skills/ /opt/agent/builtin-skills/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# fastk CLI — stdlib-only client for the FastDB knowledge-base REST API,
# the tool the bundled fastk-search / fastk-analyze skills instruct on.
COPY builtin-tools/fastk-cli/fastk /usr/local/bin/fastk
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/opencode /usr/local/bin/fastk /usr/local/bin/pptx-node /usr/local/bin/pptx-markitdown /usr/local/bin/mermaid-node

# Build gate for the present-file plugin. Two things can break silently here:
#   1. the runtime `COPY builtin-plugins/` overlay clobbering (or failing to
#      land beside) the pre-baked zod tree fetched in the build stage;
#   2. a plugin shape that opencode's loader/registry rejects at boot — which
#      surfaces only as a missing tool, long after the image shipped.
# The check replays both sides of the contract: PluginLoader's readV1Plugin
# (default export with `id` + `server()`) and tool/registry.ts's fromPlugin
# (z.object(args) -> toJSONSchema). Runs on the pptx skill's Node, the only
# Node in the runtime image and deliberately kept off PATH.
RUN cd /opt/agent/builtin-plugins/present-file \
    && test -f index.js \
    && test -f package.json \
    && test -f manifest.json \
    && test -f node_modules/zod/package.json \
    && grep -q '/opt/agent/builtin-plugins/present-file' manifest.json \
    && /opt/agent/skill-envs/pptx/bin/node -e "Promise.all([import('./index.js'),import('zod')]).then(async([m,{z}])=>{const p=m.default;if(!p||p.id!=='present-file'||typeof p.server!=='function')throw new Error('bad plugin shape: need default export {id,server}');const h=await p.server({worktree:'/tmp',directory:'/tmp'});const d=h.tool&&h.tool.present_file;if(!d||typeof d.execute!=='function'||!d.description)throw new Error('present_file tool missing from hooks.tool');for(const[k,v]of Object.entries(d.args)){if(!v||typeof v!=='object'||!('_zod' in v))throw new Error('arg '+k+' is not a zod v4 schema (registry requires the _zod marker)')}const js=z.toJSONSchema(z.object(d.args),{io:'input'});if(js.type!=='object'||js.properties.path.type!=='string')throw new Error('JSON Schema conversion produced an unusable tool signature');if(!js.required.includes('path'))throw new Error('path must be the only required arg');console.log('[build] present-file plugin OK, args: '+Object.keys(js.properties).join(','))}).catch(e=>{console.error('[build] present-file smoke test failed:',(e&&e.message)||e);process.exit(1)})"

# WORKAROUND（oven-sh/bun#32178）：Bun 1.3.14 的 parallel-GC 存在确定性竞态，
# bash 工具 spawn 子进程后 drain microtask 会 segment fault（Bun 1.3.15 已修复）。
# opencode 内置的 Bun 仍停在 1.3.14，故用单线程 GC 绕过；等 opencode 内嵌
# Bun >= 1.3.15 后可移除下面这行。
#
# pip 相关：容器 rootfs 只读、HOME(/home/agent) 是 tmpfs，Debian bookworm 的
# Python 又被 PEP 668 标记为 externally-managed，裸 `pip install` 会直接报错。
#   PIP_USER=1                  默认装到 /home/agent/.local（tmpfs，可写）
#   PIP_BREAK_SYSTEM_PACKAGES=1 绕过 PEP 668（容器一次性，无系统完整性顾虑）
#   PIP_INDEX_URL               与 apt/npm 一致使用清华镜像
ENV HOME=/home/agent \
    XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/share \
    XDG_CACHE_HOME=/data/cache \
    XDG_STATE_HOME=/data/state \
    AGENT_WORKDIR=/workspace \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_PORT=4096 \
    BUN_JSC_numberOfGCMarkers=1 \
    PIP_USER=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /workspace
USER 1000:1000
EXPOSE 4096

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=4 \
    CMD curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" \
        "http://127.0.0.1:${OPENCODE_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve", "--hostname", "0.0.0.0"]
