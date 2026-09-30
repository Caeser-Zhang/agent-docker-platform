# OpenViking 记忆服务集成测试方案

> 状态：**Phase 0 已完成**（接入改造全部落盘，见 §10；Phase 1–5 待执行）
> 目标读者：平台维护者
> 关联文档：[`docs/memory-service-solution/README.md`](./memory-service-solution/README.md)（自建 Mem0+pgvector 竞争方案）、[`docs/FASTK_APIKEY_WHITELIST_DESIGN.md`](./FASTK_APIKEY_WHITELIST_DESIGN.md)（per-user 代理令牌先例）

---

## 1. 目标

验证 OpenViking（`http://localhost:1933/`，v0.4.21）能否作为**平台内置记忆服务**注入每个用户容器，并针对**中文语言环境**达到可用精度与延迟。

验收覆盖用户点名的五个环节：服务容器化配置、跨容器网络连接、中文内容记忆功能、性能基准、兼容性。

### 1.1 与自建方案的关系

`docs/memory-service-solution/` 描述的是自建 memory-service（Mem0 OSS + pgvector，新增容器 `:8100`）。本方案走 **OpenViking 现成服务**路线。两者**互斥选一**，不并行实施。选型对比：

| 维度 | OpenViking（本方案） | 自建 memory-service |
| --- | --- | --- |
| 交付物 | 已开源、已本地部署、126 个 HTTP 路径 + 16 个 MCP 工具 | 需从零实现 11 条决策 |
| 官方 opencode 插件 | 有（`@openviking/opencode-plugin` `2026.9.25-2`，**零依赖**纯 ESM） | 无，需自研 |
| 中文能力 | embedding `qwen3.7-text-embedding`（1024 维）+ 提取模型 `glm-5.3-flash`，均中文强；插件内含 **CJK 感知 token 估算** | 取决于自选 embedding |
| 存储 | AGFS + 向量库双层（Rust binding 进程内） | pgvector 单层 |
| 运维 | 单容器，bind mount `~/.openviking` | 需自维护迁移/备份 |
| 风险 | 上游版本演进、`rerank` 未配置、多租户需自建 Key 治理 | 全部风险自持 |

**结论：本方案优先。** 理由是自研成本主要落在"已经存在且有测试"的部分（opencode 插件、CJK 预算、会话压缩），自建方案的边际收益不足以抵消实现与长期维护成本。

---

## 2. 事实基线（只读调研结果）

### 2.1 OpenViking 侧

| 项 | 实测值 |
| --- | --- |
| 版本 | `v0.4.21` |
| 部署形态 | WSL Docker 单容器，已健康运行 |
| 启动命令 | `openviking-server --host 0.0.0.0 --port 1933 --with-bot` + `vikingbot gateway 127.0.0.1:18790` |
| 存储 | bind `~/.openviking` → `/app/.openviking` (rw) |
| `/ready` 子项 | `agfs` ok / `vectordb` ok / `api_key_manager` ok / `embedding` ok / **`ollama` not_configured** / **无 `rerank` 项** |
| 认证 | `auth_mode: api_key`；三级角色 ROOT / ADMIN / USER |
| embedding | `qwen3.7-text-embedding`，阿里云 MaaS，1024 维，`input: "text"` |
| vlm / 记忆提取 | `glm-5.3-flash`（火山引擎网关），`temperature 0.0`，`reasoning_effort low` |
| 活体基线 | `initialized=true`，`user=zhangzhixiao`，`total_memories=0`，`by_category` 全 0，向量条数 **48** |
| MCP 端点 | `/mcp`（GET/POST/DELETE），16 个工具 |
| 认证头 | `X-API-Key` 或 `Authorization: Bearer`；租户头 `X-OpenViking-Account`、`X-OpenViking-Actor-Peer` |
| Web UI | **有** —— `GET /` → `302 /studio/`（OpenViking Studio，Vite SPA，静态挂载故不出现在 `/openapi.json`）。另有 `/docs`、`/redoc`。详见 §11.6 |

16 个 MCP 工具（`/openapi.json` + `tools/list` 双向核对）：`find` `search` `read` `write` `edit` `list` `tree` `remember` `add_resource` `add_skill` `list_watches` `cancel_watch` `grep` `glob` `forget` `health`。

### 2.2 平台侧

| 项 | 实测值 |
| --- | --- |
| 双网络 | `platform-net`（frontend/backend/postgres）、`agent-net`（backend/searxng/demo-mcp/用户容器） |
| Agent 运行时 | 唯一：opencode 1.18.25（Bun 编译单体 ELF，`opencode serve`，端口 4096） |
| MCP 注入 | `agent-image/builtin-mcp/<name>/manifest.json`，`type: local`（command+environment）或 `type: remote`（url+headers） |
| 插件注入 | `agent-image/builtin-plugins/<name>/manifest.json`（name + path + enabled） |
| 占位符解析 | `_resolve_placeholders`：`${VAR}` → `settings.<var.lower()>`，**只读全局 settings** |
| 用户开关 | host `opencode.json` 顶层 `builtin_mcp` 键，只认 `enabled` |
| per-user env 注入点 | `container_manager.py:428-457`（`OPENCODE_SERVER_PASSWORD`、`FASTK_API_KEY` 均在此） |
| env 漂移重建 | `container_manager.py:494-518` `_fastk_env_stale()`（成熟可复用模式） |
| 容器硬约束 | 只读 rootfs；`USER 1000:1000`；`/home/agent` 是 tmpfs；**node 二进制不在 PATH**（仅 `/opt/agent/skill-envs/pptx/bin/node`）；无 npm/pip 持久化 |

### 2.3 两个必须先说清的结论

1. **"内置到用户容器"目前并不存在。** 全仓 Grep `openviking` 只命中 `docs/`，**零集成代码**。所谓"集成测试"必须先有集成。→ Phase 0 是接入改造，用户已批准。
2. **OpenViking 当前不在 `agent-net` 上。** 容器只挂 `openviking-repo_default` 网络，用户容器无法通过 DNS 名 `openviking` 解析。→ 跨容器连通性**当前必然失败**，这是 Phase 0 的第一项改造。

---

## 3. 架构决策

### 3.1 部署拓扑：共享服务 + 容器内客户端（已确认）

OpenViking **单实例**挂 `agent-net`，照 `searxng` 先例办理：**不发布 host 端口**，仅容器网络内可达。客户端能力烘进 agent 镜像。

排除"每容器内嵌 openviking-server"的理由（硬约束直接否决）：只读 rootfs 无法写 AGFS workspace；UID 1000 非 root；`/home/agent` 是 tmpfs 重启即失；镜像明确拒绝通用 Node 栈（Dockerfile 注释："the runtime image must not grow a general Node stack"）。

### 3.2 per-user 凭据下发：**backend 代理**（推荐 C）

`_resolve_placeholders` 只读全局 settings，`${OPENVIKING_API_KEY}` 只能得到**一个共享 Key**，与"每用户独立 Key + account 隔离"目标冲突。三条候选路径：

| 方案 | 做法 | 评价 |
| --- | --- | --- |
| **A** manifest 占位符 | `${OPENVIKING_API_KEY}` 走全局 settings | ✗ 所有用户共用一个 Key，无隔离，直接否决 |
| **B** 容器持真 Key | backend 用 Admin API 铸造 per-user User Key，经 `container_manager` env 注入容器，容器直连 `openviking:1933` | △ 隔离达标，但**违反平台既有安全不变量** |
| **C** backend 代理（推荐） | 容器只拿**不透明代理令牌**，backend 侧 `/ov-proxy` 校验令牌→查出该用户真实 User Key→注入 `X-API-Key` 转发 | ✓ 真 Key 永不进容器；与 fastk 白名单代理**完全同构** |

**选 C。** 决定性依据是 `backend/app/services/kb_access.py` 里写死的设计不变量：

> The proxy token handed to agent containers is `encrypt_secret("kbproxy:<uid>")` — stateless, unforgeable without `AGENT_SECRET_KEY`, and worth nothing beyond the bearer's own grants. **Real keys never enter a container.**

以及同文件对 catalog key 的说明：*"this key must never reach a container — one that holds it could enumerate and read every database straight off the host gateway"*。OpenViking 的 User Key 具备同等的"越权即读全量记忆"性质（`viking://user/{uid}/...`），适用同一条不变量。

**C 的额外收益**：代理层是唯一的**中文优化策略注入点**——`scoreThreshold`、`minQueryLength`、`recallTokenBudget` 等参数可在转发时按用户/场景覆写，无需重建容器；同时天然承接审计与限流。

**C 的成本**：MCP over streamable-HTTP 需要 SSE 透传。平台 `/llm-proxy` 已实现同类 SSE 转发，`kb_proxy.py` 已有 `_DROP_HEADERS` / `_RESPONSE_DROP_HEADERS` 头部治理范式，两者可直接复用。

> **待执行时确认**：Admin API 铸造 User Key 的精确端点路径需从 `openapi.json` 与 `examples/multi_tenant/admin_workflow.py` 核对，本方案不预设。User Key 格式已确认为 `base64(account).base64(user).hex_secret`。

### 3.3 MCP 接入类型：`type: remote`

`type: local` 需要 stdio 子进程（插件的 `servers/mcp-proxy.mjs`）→ 需要 `node` → 但镜像**故意**不提供 PATH 上的 node。故选 `type: remote` 直连 backend 代理，规避整条依赖链。

### 3.4 官方插件：纳入，`type: builtin-plugins`

`@openviking/opencode-plugin` `2026.9.25-2` 是零依赖纯 ESM，可由 opencode 内嵌 Bun 直接加载，**无需外部 node**。它提供 manifest 之外的三项关键能力：`autoRecall`（自动召回）、`autoCapture`（自动写入）、`profile-inject`（CJK 感知预算裁剪）。这三项正是"记忆服务"区别于"普通 MCP 工具"的部分。

> **实施期发现的硬约束（详见 §10 结论 3）**：插件默认还会**自己注册一个 MCP server**（`lib/mcp-config.mjs` 的 `injectOpenVikingMcpConfig` 覆写 `config.mcp.openviking` 为 `node servers/mcp-proxy.mjs`）。这与 §3.3 选的 `type: remote` 直接冲突，且因镜像无 PATH 上的 node 会**静默**失效。必须用 `mcpEnabled: false` 关掉，插件以 hook-only 模式运行。

`HARNESS_KEYS` 已含 `opencode`（也有 `trae` / `trae_cn`），`timeoutMs` 对 opencode 有专属 harness 默认值 **30000ms**（全局默认 15000ms）——说明上游已适配本运行时。

> **版本号形态更正**：npm 上不存在 `v0.4.0`。该包用日期版本号（`2026.9.25-2` 为钉住时的 `dist-tags.latest`），**无法用 caret range**，每次升级都是 Dockerfile 里 `ARG OPENVIKING_PLUGIN_VERSION` 的显式编辑。另：发布包的 `files` 白名单**不含 `tests/`**，故原计划的 `node --test` 回归门禁在发布包上跑不了（见 §10 结论 6）。

### 3.5 Rerank：作为对照实验变量

`/ready` 无 rerank 项、`ov.conf` 无 rerank 段。官方文档称 rerank 提升检索精度，而 L1 层（~2000 tokens 概览）的设计意图正是"Rerank 精排/导航"。**当前部署缺失该层能力，是中文语义理解的主要缺口。**

处理方式：不假设它必需，而是把 rerank 开/关设为 Phase 3 的**对照变量**，用同一评测集量化其对 Recall@K、MRR 的增益，再据数据决定是否补配。

> **已定案（P3-13，2026-09-28）：rerank 永久关闭，不补配。** A/B 结果是**强负增益**——`recall@5 0.892→0.517`、`mrr 0.933→0.578`、`p50 136ms→3075ms（22.6×）`、阈值判定 `5 PASS / 2 FAIL → 0 PASS / 7 FAIL`。且开启 rerank 会把检索器切进 `THINKING` 模式并**没有 per-request 退出口**，导致四个分类整棵子树不可见（§11.12.4）。落地形态：`.env` 里注释掉 `OV_RERANK_API_KEY`（key 值原地保留以便复现实验），compose 注释块写入实测依据。本节原判断"L1 层缺失是中文语义理解的主要缺口"**被数据推翻**：缺的不是精排，是目录 overview（见 R18）。

---

## 4. Phase 0 — 接入改造清单

用户已批准"接入改造 + 测试执行"。以下为改造项，**每项均须在 Phase 1 之前完成**。

### 4.1 `docker-compose.yml`：新增 openviking 服务

照 `searxng` 先例（共享服务、挂 `agent-net`、无 host 端口）：

```yaml
  openviking:
    image: openviking-cn-beijing.cr.volces.com/volcengine/openviking:latest
    restart: unless-stopped
    volumes:
      - ./openviking/ov.conf:/app/.openviking/ov.conf:ro   # 密钥经 env 注入，不落盘明文
      - openviking-data:/app/.openviking/data
    environment:
      OV_ROOT_API_KEY: ${OPENVIKING_ROOT_API_KEY}
      # embedding / vlm / rerank 凭据同理由 .env 注入
    networks:
      - agent-net          # 关键：当前部署缺这条
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:1933/ready"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
```

要点：
- **不发布 host 端口**——仅 `agent-net` 内可达，与 `searxng` 一致
- **配置从 `~/.openviking` 迁入仓库**，密钥全部走 `.env`（见 §7 风险 R1）
- 现有 WSL 里的 `openviking-repo_default` 容器须停掉，避免双实例争抢同一数据卷

### 4.2 backend 配置

`backend/app/config.py` 新增（命名遵循 `AGENT_*` → `settings.*` 既有约定）：

```python
    openviking_enabled: bool = False          # 平台级总开关，默认关
    # backend 经 agent-net 直连上游（用于 Admin API 铸造 per-user Key）
    openviking_url: str = "http://openviking:1933"
    # 容器侧的两个 URL，都指向 backend 的 /ov 代理，绝不指向 openviking_url
    openviking_mcp_url: str = "http://backend:8000/ov/mcp"
    openviking_rest_url: str = "http://backend:8000/ov"
    openviking_root_api_key: str = ""         # 仅 backend 使用，绝不下发
    openviking_account_id: str = "agent-platform"
    openviking_admin_user_id: str = "platform-admin"
```

> **实施期更正**：字段名从草案的 `openviking_base` / `openviking_proxy_base` / `openviking_account` 改为上表；代理前缀是 `/ov` 而非 `/ov-proxy`（与 `/fastk/api` 同风格）。`openviking_enabled` 默认 **False**，即不改 `.env` 的平台不会注入任何记忆能力。`admin_user_id` 是建 account 时的必填项（`CreateAccountRequest.required = ["account_id", "admin_user_id"]`），草案漏了。

`backend/.env` 对应新增 `AGENT_OPENVIKING_*`。

### 4.3 backend：`services/ov_access.py`（新增，仿 `kb_access.py`）

#### 4.3.0 身份派生机制调研结论（决定本节实现形态）

读 OpenViking `server/auth/plugins/api_key.py` 后确认两条硬约束：

1. **`X-OpenViking-Account` / `X-OpenViking-User` 在 `api_key` 模式下被静默忽略**。`ApiKeyAuthPlugin.resolve_identity`（:97-103）显式调用 `_remove_header(request, b"x-openviking-account")` / `b"x-openviking-user"`，注释写明 *"Silently ignore identity assertion headers in api_key mode"*。身份完全由 `api_key_manager.resolve(api_key)` 从 Key 自身解出（新格式 Key 内嵌 `account_id.user_id`）。
2. **ROOT Key 被禁止访问租户数据 API**。`get_request_context_checks`（:268-281）对 `Role.ROOT` 且非 OAuth 的请求做路径白名单校验，越界即 `PermissionDeniedError("ROOT API keys cannot access tenant-scoped data APIs in api_key mode")`。白名单仅 3 个具体路径（`/api/v1/system/status`、`/api/v1/system/wait`、`/api/v1/debug/health`）+ 6 个前缀（`/api/v1/admin`、`/observer`、`/console`、`/tasks`、`/system/backend`、`/system/sync`）。

**推论**：`root key + 租户头` 方案不可行，**必须铸造 per-user User Key**。

**正面收益（写入 §6 验收）**：ROOT Key 在 backend 手里也只能做 admin 操作，**即使从 backend 泄漏也读不到任何用户记忆**——天然纵深防御，比 fastk 的 catalog key 更强。

#### 4.3.1 Admin API 契约（已核对源码）

| 用途 | 方法 + 路径 | 守卫 | body | 返回 |
|---|---|---|---|---|
| 建 account（一次性） | `POST /api/v1/admin/accounts` | `require_auth_root` | `{account_id, admin_user_id, seed?, user_config?, settings?}` | `result` 含 admin key |
| 铸 User Key（每用户一次） | `POST /api/v1/admin/accounts/{account_id}/users` | `require_auth_root_or_admin` | `{user_id, role="user", seed?, user_config?}` | `result={account_id, user_id, user_key}` |
| 轮换 | `POST /api/v1/admin/accounts/{account_id}/users/{user_id}/key` | `require_auth_root_or_admin` | `{seed?}` | `result={user_key}`，旧 Key 立即失效 |
| 探测是否已注册 | `GET /api/v1/admin/accounts/{account_id}/users?include_credentials=false&name=<uid>` | `require_auth_root_or_admin` | — | `[{user_id, role, api_key_available}]` |
| 注销 | `DELETE /api/v1/admin/accounts/{account_id}/users/{user_id}` | `require_auth_root_or_admin` | — | 202，异步清理 |

`user_key` 仅在非 `trusted` 模式暴露（`_should_expose_user_key`，:209-213）。响应统一包在 `{status, result}` 信封内。

#### 4.3.2 **无需新增 DB 表** —— 确定性 seed 派生（本节最大简化）

`api_keys/legacy.py:62-65` 的 seed 派生是**纯函数、无服务端盐**：

```python
def derive_seeded_api_key_secret(user_id: str, seed: str) -> str:
    return hashlib.sha256(f"{user_id}\0{seed}".encode("utf-8")).hexdigest()
```

配合 `api_keys/new.py:70-80` `generate_api_key` 与 `:36-39` `_encode_segment`（urlsafe b64 去 padding），User Key 可在 backend 本地**无状态重算**：

```python
def derive_user_key(account_id: str, user_id: str) -> str:
    """Recompute the OpenViking User Key locally — no DB row, no upstream call."""
    seed = hmac.new(settings.secret_key.encode(), f"ovuser:{user_id}".encode(), sha256).hexdigest()
    secret = sha256(f"{user_id}\0{seed}".encode()).hexdigest()
    return ".".join(_seg(account_id), _seg(user_id), _seg(secret))
```

这与平台既有的 `kb_access.issue_proxy_token`（`encrypt_secret("kbproxy:<uid>")`，纯函数、无状态、不落库）**完全同构**。省掉：新表、migration、每请求 DB 查询、缓存与失效逻辑。

服务端仍需一次性 `register_user` 调用，把 Argon2id 哈希写进 keystore（`/local/{account_id}/_system/users.json`）。因此 `ensure_user_registered()` 只在**首次**或**探测到 `api_key_available=false`** 时打 Admin API。

**id 兼容性已验证**：平台 `User.id = str(uuid.uuid4())`（36 字符含连字符），OpenViking `core/identifiers.py:8` 的 `^[a-zA-Z0-9_.@-]+$` 接受连字符 → **UUID 可直接作 OpenViking `user_id`，无需映射**。`account_id` 同理（唯一额外限制：不得以 `_` 开头）。

**轮换语义**：换 `AGENT_SECRET_KEY` 即换全部 seed → 须调 `POST .../users/{uid}/key` 携新 seed 重铸，并由 `_ov_env_stale()` 触发容器重建。这与 `crypto.py` docstring 已声明的"轮换 secret_key 会使既有加密字段失效"是同一类运维约束。

#### 4.3.3 模块接口

```python
PROXY_TOKEN_PREFIX = "ovproxy:"

def issue_proxy_token(user_id: str) -> str: ...        # encrypt_secret(f"ovproxy:{uid}")
def verify_proxy_token(token) -> str | None: ...       # 解出 user_id
def derive_user_key(account_id, user_id) -> str: ...   # 纯函数，见 4.3.2
async def ensure_user_registered(user_id) -> None: ... # 幂等，打 Admin API（用 root key）
```

### 4.4 backend：`routers/ov_proxy.py`（新增，仿 `kb_proxy.py`）

- 校验代理令牌 → `user_id`
- **丢弃调用方传入的 `X-API-Key`**（与 kb_proxy 同一不变量）
- 注入该用户真实 Key 后转发至 `openviking:1933`
- SSE / chunked 透传（参照 `/llm-proxy`），`read=None`——MCP SSE 流在两次服务端通知之间会无限空闲
- 复用 `_DROP_HEADERS` / `_RESPONSE_DROP_HEADERS`
- **写操作放行策略**：与 fastk 的"只读"不同，记忆服务必须能写。Admin API 路径**一律拒绝**

> **实施期更正（重要）**：白名单的粒度不是"MCP 工具名"而是 **HTTP 路径**，且是**allow-list 而非 deny-list**。原因是接入插件后 REST 通道由插件进程自己发起，工具名白名单根本拦不到它。最终 `_ALLOWED_REST` 收录 8 条精确路径 + 1 条 session 正则，恰好等于钉住版本插件会发出的 13 条唯一路径（16 条字面量归一化后），一条不多：
>
> ```
> /health  /api/v1/system/status  /api/v1/fs/ls  /api/v1/content/read
> /api/v1/skills  /api/v1/search/search  /api/v1/search/find  /api/v1/search/recall
> ^/api/v1/sessions/[^/]+(?:/(?:context|commit|messages(?:/batch)?))?$
> ```
>
> Admin API、console、debug 端点因此是**构造上不可达**，不依赖一份需要持续同步的黑名单。路由只挂 GET/POST（插件不用其他方法），`/mcp` 挂 GET/POST/DELETE。
>
> 代价：白名单与插件版本**强耦合**。升级插件必须重新枚举其 REST 面，否则新路径会 404 并被记为 `_ALLOWED_REST may be stale` 警告——这是刻意设计的可观测失效，不是静默降级。
>
> 另一处实现细节：`_caller_user_id` 接受**两种**凭据头拼写（`X-API-Key` 与 `Authorization: Bearer`），因为 opencode 内建 MCP 客户端发前者、插件 `buildOvHeaders` 刻意只发后者。一个派生令牌服务两条通道靠的就是这里。401 时调 `ov_access.reauthorize` 自愈一次（复用 kb_proxy 范式）。

### 4.5 `container_manager.py`：per-user env 注入

在 `environment` 字典（当前 line 428-457）内新增：

```python
                # OpenViking 记忆服务。同 FASTK_API_KEY：不透明代理令牌，
                # 编码用户自身 id，真实 User Key 由 backend 在转发时注入。
                "OPENVIKING_URL": settings.openviking_rest_url,
                "OPENVIKING_MCP_URL": settings.openviking_mcp_url,
                "OPENVIKING_API_KEY": ov_access.issue_proxy_token(user_id),
                "OPENVIKING_AUTH_MODE": "api_key",
                "OPENVIKING_CLI_CONFIG_FILE": OPENVIKING_PLUGIN_CONFIG,
```

> **实施期更正**：env 变量名不是自拟的 `OPENVIKING_BASE_URL`，而是插件 `lib/shared/credentials.mjs` 的 `CREDENTIAL_ENV_VARS` 里**已存在**的名字（见 §10 结论 4）。REST 与 MCP 是**两个**变量，因为插件给 REST 调用拼 `/api/v1/...`、给 MCP 拼 `/mcp`，两者共用同一个 `/ov` 代理前缀但后缀不同。`OPENVIKING_CLI_CONFIG_FILE` 是第 5 项，指向烘进镜像的 knob 文件。

并新增 `_ov_env_stale()`，与 `_fastk_env_stale()`（line 494-518）同构：逐项比较上述 5 个变量，并用 `verify_proxy_token` 校验令牌有效性，触发容器重建以完成 Key 轮换。feature off 时直接返回 False，避免无谓 churn。

### 4.6 agent 镜像：两个 manifest

`agent-image/builtin-mcp/openviking/manifest.json`（**已落盘的最终形态**）：

```json
{
  "name": "openviking",
  "type": "remote",
  "url": "${OPENVIKING_MCP_URL}",
  "enabled": true
}
```

> **与原草案的两处差异**：① 占位符名是 `OPENVIKING_MCP_URL`（对应 `settings.openviking_mcp_url`），不是自拟的 `OPENVIKING_PROXY_BASE`；② **没有 `headers` 键**——per-user 令牌由 backend 在 `build_container_config` 里 `setdefault("headers", {})["X-API-Key"] = ...` 盖章，manifest 保持全用户同形，理由见下面的定案。

> **定案（原未决项）**：`${VAR}` 只能解析**全局** settings（`opencode_config._resolve_placeholders` 只 `getattr(settings, ...)`），无法每用户差异化。因此 header 令牌**不走占位符**，改由 backend 在 `build_container_config` 内按 `user_id` 盖章。
>
> 落点已核实干净：`build_container_config`（`opencode_config.py:612-619`）**已有 `user_id` 形参**，且唯一异步入口 `user_config.build_user_config_json(db, user_id)` 总是传它。在 `:654-656` 的 `sanitized.setdefault("mcp", {}).update(builtin_mcp)` **之后**插入一步覆写即可，无需改签名、无需依赖插件 `credentials.mjs` 的 env 变量名：
>
> ```python
>     # builtin_mcp 注入之后、_walk 之前
>     if user_id and settings.openviking_enabled:
>         entry = sanitized.get("mcp", {}).get("openviking")
>         if entry:
>             entry.setdefault("headers", {})["X-API-Key"] = ov_access.issue_proxy_token(user_id)
> ```
>
> 不选"插件从容器 env 读取"：那条路径要读插件 `credentials.mjs` 的未文档化 env 名，且 env 注入与 header 盖章会形成两处真相源。
>
> **注**：`container_manager.py:186` 的 `build_container_config_json()` 无参 fallback 是 legacy 路径，拿不到 `user_id`。该路径下 openviking 条目保持占位符原样（`_resolve_placeholders` 对无法解析的占位符**保留字面值**），MCP 握手会以 401 失败——可接受的降级，且 4 处实际调用（:656/:674/:702/:747）均传 `config_json`。Phase 5 验证。


`agent-image/builtin-plugins/openviking/manifest.json`（**已落盘**）：

```json
{
  "name": "openviking",
  "path": "/opt/agent/builtin-plugins/openviking/node_modules/@openviking/opencode-plugin",
  "enabled": true
}
```

> `path` 必须指到 **npm 包目录本身**（`node_modules/@openviking/opencode-plugin`），不是 `builtin-plugins/openviking`——opencode 从该路径加载入口模块，而 fetch 阶段 `npm install` 会把包装进 `node_modules/`。

**第三个 artefact（原方案没有，实施期新增）** `agent-image/builtin-plugins/openviking/ovcli.conf`：

```json
{"plugin": {"opencode": {"mcpEnabled": false, "repoContext": false, "dataDir": "/data/state/openviking", "minQueryLength": 2}}}
```

四个 knob 各自解决一个具体失效模式，前三个详见 §10 结论 3，第四个（`minQueryLength: 2`，中文两字查询）是 P3-G 的产出，详见 §11.12.10 A/F。**顶层只能有 `plugin` 一个键**：插件 `credentials.mjs` 的 `hasCredentialFields` 只看顶层 8 个键（`url` / `api_key` / `account` / `account_id` / `user` / `user_id` / `actor_peer_id` / `peer_id`），一旦命中就会把整条凭据链 pin 到这个文件上，导致**所有用户共享同一个记忆空间**。已用负控制实测：文件带 `url`+`api_key` 且 env 也有凭据时，`credentialSource`/`apiKeySource` 仍解析为 `env`，即 env 赢、文件不 pin。

插件源码经 Dockerfile 的 fetch 阶段（npm 源已是 `registry.npmmirror.com`）用 `npm install --omit=dev "@openviking/opencode-plugin@${OPENVIKING_PLUGIN_VERSION}"` 拉取。零依赖，故不存在跨 stage 的平台相关二进制，构建门禁可安全地用 runtime 镜像自己的 node（`/opt/agent/skill-envs/pptx/bin/node`）跑。门禁仿 present-file 形态，断言 6 项（见 §10 结论 3）。

### 4.7 rerank 补配（条件项）→ **已结案：不补配**

Phase 3 对照实验若证明增益显著，则在 `ov.conf` 补 `rerank` 段并使 `/ready` 出现 rerank 子项。**Phase 0 不预先补配**，以免污染基线。

**结案（P3-13）**：增益为**显著负**，不补配。三点须记住，防止后来者"顺手打开"：

1. **`/ready` 永远不会出现 rerank 子项**，无论配没配。两态的 `checks` 都是 `['agfs','api_key_manager','embedding','ollama','vectordb']`；`/api/v1/debug/health` 只回 `{"status":"ok","result":{"healthy":true}}`。**唯一可靠的观测面是 `/metrics` 的 `openviking_model_usage_available{model_type="rerank",valid="1"}`**（OFF=0.0 / ON=1.0）。`rerank_call_duration_seconds` 全落 `le="0.01"` bucket，分辨率不足，不可用。
2. **旋钮只有一个**：`RerankConfig._effective_provider()` 的第二分支是 `if self.api_key: return "cohere"` ⇒ **唯一安全的 off 态是让 `api_key` 为空**。填任何非空值都会激活 rerank，没有 `enabled: false` 可用。
3. **`default_search_mode` 是死配置**（⚠️ 它是 `OpenVikingConfig` 的**顶层键**，不在 `retrieval` 段下），改它不影响检索模式；实际模式由 `hierarchical_retriever.py:127` 根据"有没有 rerank client"自动决定。

---

## 5. 测试设计（Phase 1–5）

每个用例给出 **ID / 前置 / 步骤 / 期望 / 判定方式**，可脚本化。

### Phase 1 — 服务容器化配置验证

| ID | 用例 | 期望 |
| --- | --- | --- |
| P1-01 | `docker compose up -d openviking` 后轮询 `/ready` | 60s 内 `status: ok` |
| P1-02 | 检查 `/ready` 子项 | `agfs` `vectordb` `api_key_manager` `embedding` 全 ok；记录 `rerank`、`ollama` 状态作为基线 |
| P1-03 | `/health` 版本一致性 | 返回 `v0.4.21`，`auth_mode: api_key` |
| P1-04 | 容器 healthcheck | `docker inspect` → `healthy`。**判定方式必须修正**：单次采样会在重启窗口内取到假阳性，须「多点采样比对 `.State.StartedAt` 是否漂移」+「容器内 `curl 127.0.0.1:1933/health` 直接验端口绑定」。见 §11.2 |
| P1-05 | **数据持久化** | 写入一条记忆 → `docker compose down` → `up` → 记忆仍在，向量条数不减 |
| P1-06 | **重启恢复** | `docker restart openviking` → 90s 内 `/ready` ok，AGFS 无损坏日志 |
| P1-07 | 密钥不落盘 | 仓库内 `ov.conf` 与 `.env` 无明文密钥；`grep` 全仓无 64-hex 泄漏 — **已通过**（`.env` 被 gitignore 且未被 git 跟踪、`ov.conf` 全 `${VAR}`、`.env.example` 三键为空）。**副作用**：验证过程把 root key 明文打进了会话记录 —— **已于测试收尾轮换（§11.19 D，2026-09-29），旧 key 实测 401** |
| P1-08 | host 端口未暴露 | **判定已改写**（原期望 `docker port` 为空，与 §11.6 故意发布 `127.0.0.1:1933` 冲突）→ 改为验 loopback-only：`1933/tcp -> 127.0.0.1:1933`、`HostIp:"127.0.0.1"`、`LISTEN 127.0.0.1:1933` — **已通过** |

Phase 1 八项**全部通过**，实测细节见 §11.7。两个原先没写进期望、但实测出来的关键数字：P1-05 卷重建后 `/ready` 14s、`vector/count` 36→37→**39**（embedding 是异步的，写入返回后计数仍会涨）；P1-06 `docker restart` 后 `/ready` **11s**。

### Phase 2 — 跨容器网络连接验证

| ID | 用例 | 期望 |
| --- | --- | --- |
| P2-01 | 网络归属 | `docker inspect` → `Networks` **仅** `agent-net` — **已通过**（`172.19.0.4`，aliases `[agent-docker-demo-openviking-1, openviking]`） |
| P2-02 | DNS 解析 | backend 容器内 `getent hosts openviking` 有结果 — **已通过**（`172.19.0.4`；§11.1 记的 `172.19.0.5` 是重建前旧值，现已过时） |
| P2-03 | backend → openviking | `curl -fsS http://openviking:1933/ready` 200 — **已通过**（`/health` 亦 200 `v0.4.21`） |
| P2-04 | **用户容器 → 代理** | 容器内 `curl -H "X-API-Key: $OPENVIKING_API_KEY" $OPENVIKING_MCP_URL` 完成 MCP `initialize` — **已通过**（`/ov/health` 回 `account_id:agent-platform, user_id:p2probe, role:user`） |
| P2-05 | MCP `tools/list` | ~~返回 **16** 个工具~~ → **实测 15 个**，`add_skill` 上游不存在（`ov_proxy._ALLOWED_TOOLS` 里那条是陈旧项）— **已通过，基线须改** |
| P2-06 | 用户容器**不能**直连 | 容器内 `curl http://openviking:1933/ready` → 应被拒绝或无路由 — **实测可达（200，0.54s），判定为风险而非致命**：容器只持有 proxy token，直连一切数据面端点均 `401`。量化暴露面见 §11.7 |
| P2-07 | `platform-net` 隔离 | frontend 容器内无法解析 `openviking` — **已通过**（`NO_DNS` + `wget: bad address 'openviking:1933'`） |
| P2-08 | 代理丢弃调用方 Key | 容器传入伪造 `X-API-Key` → backend 不转发该值 — **已通过**（伪造 admin key → 401；同一把 key 直连 OV → 200 `user=platform-admin`，证明 401 来自代理不转发而非 key 无效） |
| P2-09 | SSE 透传 | 长响应不超时、不截断 — **已通过**（145312 UTF-8 字节 / 51702 CJK 字符经 MCP `write` 写入，代理读回 `identical=True`，代理开销 ~14ms） |

**§11.6 末尾那条 ⚠ Phase 5 待查项已在 Phase 2 重验中解决**：命名空间确实是 account 级共享，但 per-user 隔离发生在 `viking://user/{uid}` 这一层且是**强隔离**（跨用户访问一律 403）。平台"每用户独立 User Key 即隔离"的前提**成立**，无需改为每用户一个 account。完整作用域模型见 §11.8。

### Phase 3 — 中文内容记忆功能测试

**评测集自建**（官方 `benchmark/locomo`、`longmemeval` 均为英文，不适用）。放置于 `benchmark/ov-zh/`。

**规模已按"先抽样跑通方法论，再扩全量"（用户决策）收敛为抽样集**，全量目标保留为后续项：

| | 原计划（全量） | **已落地（抽样）** |
| --- | --- | --- |
| 语料 | 200 条 / 8 类各 ≥ 20 | **40 条 / 8 类各 5**（`corpus.jsonl`） |
| 查询 | 150 条（40/40/40/30） | **30 条**（G1 8 / G2 8 / G3 8 / **G4 6**） |
| 分组语义 | G1 原文复现、G2 同义改写、G3 口语化省略、G4 跨类干扰 | 同左，每条标注 ground-truth `expected` 语料 id 集合 |

> **抽样集的方法论代价必须记住**：G4 只有 6 条查询 × 每条 4 个 expected = 24 个 expected，单个 miss 就让 `precision@5` 掉 0.083。因此 **G4 的阈值判定在小样本下抖动极大**，任何结论都应在扩到 ≥ 30 条后复核。

语料 schema（实测）：`corpus.jsonl` 每行只有 **`category` / `slug` / `id` / `content`** 四个字段——没有 `text`、`title`、`tags`。URI 由 `corpus_uri()` 拼成 `viking://user/{uid}/memories/{category}/{slug}.md`。

| ID | 用例 | 期望 / 阈值 |
| --- | --- | --- |
| P3-01 | 写入→提取→召回全链路 | `remember` 写入中文对话 → 记忆出现在 `viking://user/{uid}/memories/` → `find` 可召回 — **PASS**（`remember` 返回 265 ms；`system/wait` + `stats/memories` 交叉确认 12 轮 / 35.8 s 后 `total 43→45`、`preferences 8→10`；产出 `preferences/user/后端开发与代码风格.md` 与同目录 `.overview.md`；改述中文查询 #1/#2 命中 `0.6539 / 0.6362` 与 `0.5533 / 0.4538`，全部 > `scoreThreshold=0.35`）。**注意 round0 的 `wait=504` 是积压导致，必须重试**，见 §11.12.6 的完成判定四步 |
| P3-02 | **分类正确性** | 8 类各 ≥ 20 条，分类准确率 **≥ 85%** — **FAIL（`landed-in-expected = 5/9 = 55.6%`；只算 Part C 的 8 条为 `4/8 = 50%`）**。`markers resolved = 9/9`，即**没有丢记忆，只是投错了类目**。详见 §11.12.8。**三项必须一起读的限定**：① 抽取器实测只会路由进 **4 个类目**（`preferences` / `entities` / `events` / `profile`），评测集自造的 `tools` / `skills` / `patterns` **自然对话永远不会产出** ⇒ 8 类设计本身失真；② mis-filing **不影响召回**（rerank OFF 下错误类目的文档照样以 `0.8567` 排 #1）；③ R18 的兜底手段 `target_uri` 定向在分类器不产出该类目时是**负收益**（0 hits 或指向错误文档）。原判据「灌语料走 `content/write` 会绕过分类（`semantic_status:"skipped"`）」**表述有误**，真信号是 `overview=complete|skipped`，见 §11.12.6 |
| P3-03 | **G1 原文复现** | Recall@5 **≥ 0.95**，MRR **≥ 0.90** — **PASS**（实测 `1.000 / 1.000`，rerank OFF） |
| P3-04 | **G2 同义改写** | Recall@5 **≥ 0.80**，MRR **≥ 0.70** ← 中文语义理解核心指标 — **PASS**（实测 `1.000 / 1.000`，**超出阈值 0.20**；注意这是 8 条小样本，且语料仅 40 条，同义改写几乎没有干扰项可混） |
| P3-05 | G3 口语化 | Recall@5 **≥ 0.70** — **PASS**（实测 `0.750`，MRR `0.750`，**余量仅 0.05**，扩样本后极可能翻面） |
| P3-06 | G4 干扰项 | Precision@5 **≥ 0.75** — **FAIL（0.633）**，但**该阈值本身有缺陷**：`precision@5 = found/5`、`found = recall@5 × \|expected\|`，G4 每查询标 4 个 expected ⇒ 阈值 0.75 等价于要求 `recall@5 ≥ 0.9375`。**这是一个伪装成 precision 的 recall 阈值**，实测 24 expected × 0.7917 = 19 found ⇒ 算术天花板恰为 0.6333。改用 `precision@returned` 则为 **0.792（PASS）**。详见 §11.12.5 |
| P3-07 | **编码完整性** | 中文写入后 `read` 回读，逐字符相等；无乱码、无截断、无 `?`/`\ufffd` 替换 — **PASS**（40 条逐字符回读 **0 mismatch**；判定按 §11.3 第 7 条走码点） |
| P3-08 | 混合中英/数字/标点 | 中英混排、全角半角标点、emoji、代码片段均无损 — **PASS**（与 P3-07 同批验证） |
| P3-09 | **CJK token 预算准确度** | 见下 — **已完成，判定"无需改配置"**：服务端 `estimate_text_tokens` 本就是 CJK-aware（1.5 tokens/汉字），context face 主路径按真实 token 强制预算（活体：`max_tokens` 缺省 → `used_tokens 440`；`=300` → `288`，tier 优雅降级）；插件 `chars/4` 的 6× 低估**只作用于 fallback 路径**。**另记录一个配置陷阱**：`recallMaxTokens` clamp 上限 200000 > 服务端 `le=32000`，越界会 422 + 静默降级。详见 §11.12.10 A |
| P3-10 | `minQueryLength=3` 中文适配 | 2 字中文查询（如「报错」）是否被拒；评估是否需下调至 2 — **已完成，判定"下调至 2"并已落地**：服务端完全支持两字查询，损失 100% 在插件门禁 `lib/memory-recall.mjs:10`。端到端 B6：「故障」经代理 `http=200`，4 条高相关命中。详见 §11.12.10 A/E |
| P3-11 | `scoreThreshold=0.35` 中文适配 | 统计 G1–G4 实际分数分布，确认 0.35 不误杀 G2/G3 — **已改判**：脚手架噪声问题量化完毕，结论是**滤除脚手架对 precision 无收益**（delta 恒 +0.000），R15 的真实性质是 **token 预算浪费**而非精度损失。~~建议落地点是插件侧按 URI 过滤~~ → **已落地在代理层**（插件侧 `recallExcludeUris` 是死旋钮），实测回收 **38.5% token**，见 §11.12.10。**不调阈值**这一条仍然成立。数据见 §11.12.5 |
| P3-12 | CJK 密度启发式边界 | `capture-utils.mjs:570` 的 `cjk >= 4 \|\| alnum >= 6 \|\| text.length >= 12`：3 字中文应不达标、4 字应达标 — **已完成，判定"无需改"**：行号实为 `:614-616`，判据**本就 CJK-aware**；`rankItem`/`dedupeItems`/`formatFallback` 的 `lexicalOverlapBoost` 对中文近乎失效，但**只在非 context-face 的 fallback 路径执行**，context 模式排序全在服务端 ⇒ 实质 moot。详见 §11.12.10 A |
| P3-13 | **rerank 对照实验** | 同一评测集跑 rerank 开/关两组，量化 G2 Recall@5 与 MRR 增益 — **已完成，结论为强负增益**：G2 `recall@5 1.000→0.625`、`mrr 1.000→0.562`；全局 `0.892→0.517` / `0.933→0.578`；`p50 136→3075ms`。**决策：永久关闭**（§3.5、§4.7、R3） |

**已跑部分的完整数据、工装与踩坑见 §11.12。** 一句话结论：**rerank OFF 态下 7 项阈值判定 5 PASS / 2 FAIL**，两项 FAIL 分别是 G4 precision@5（阈值缺陷，实为 recall 天花板）与 G4 precision@returned 在未滤除态的连带失败；中文语义召回本身（G1/G2/G3）全部达标。


**P3-09 CJK token 预算验证**（中文专项最有价值靶点）

插件 `lib/shared/profile-inject.mjs` 使用 CJK 感知估算：

```javascript
// codepoint >= 0x3000 → CJK / Hiragana / Katakana / Hangul
let cjk = 0;
  if (text.charCodeAt(i) >= 0x3000) cjk++;
const other = text.length - cjk;
return Math.ceil(cjk * 1.5 + other / 4);
```

源码注释明确指出通用 `chars/4` 启发式会把中文**低估 4–6 倍**（"a 5000 token budget really worth ~1k real tokens for Chinese text"）。测试：

1. 构造纯中文长文本，分别用该公式与真实 tokenizer（`tiktoken` cl100k_base）计数
2. **误差要求**：估算值 ≥ 真实值的 **0.9×**（宁可高估），且 ≤ 真实值的 **1.3×**（源码自述最坏高估 10–20%）
3. 构造混合文本（中文 + 英文 + 代码 + 数字），验证按 CJK 密度换算字符预算的逻辑（`profile-inject.mjs:90-101, 161-163`）
4. 验证注入内容**不超**预算：`profileTokenBudget=10000`、`recallTokenBudget=2000`、`recallMaxContentChars=500`、`skillCatalogTokenBudget=1200`
5. 验证超预算时**裁剪而非报错**，且裁剪后仍是合法 UTF-8（不在多字节字符中间切断）

> **实测结论（P3-G）**：上面这套方法的**前提被推翻了两次**。① `tiktoken` 在服务端**未安装**，"真实 tokenizer 对照"无从做——但也**不需要**做：服务端自己的 `utils/token_estimation.py:11-44 estimate_text_tokens` 就是 CJK-aware 的（ASCII `(len+3)//4`；CJK/Kana/Hangul/全角/Ext-A/Ext-B 按 6 个四分之一单位；其他 astral 按 8），与插件公式同值且覆盖更宽，**预算强制发生在服务端而不是插件**。② 这里点名的插件估算器（`profile-inject.mjs`）走的是 **profile 注入路径**；召回主路径的预算是 `render.py:49-51` → `budget.py:68` → `per_entry_cap`，活体已验证「超预算裁剪而非报错、tier 优雅降级」。**判定：无需改任何配置**，但记录一个陷阱——`recallMaxTokens` 的 clamp 上限（200000）远大于服务端 `SearchRequest.max_tokens` 的 `le=32000`，越界会**每次 422 且静默降级**。完整推导见 §11.12.10 A。

### Phase 4 — 性能基准

| ID | 指标 | 阈值 | 方法 |
| --- | --- | --- | --- |
| P4-01 | 检索延迟 P50 | **≤ 300 ms** | `search` 冷/热各 200 次，剔除首次冷启动 |
| P4-02 | 检索延迟 P95 | **≤ 800 ms** | 同上 |
| P4-03 | 检索延迟 P99 | ≤ 1500 ms | 同上 |
| P4-04 | `find`（L0 摘要）P95 | **≤ 500 ms** | L0 ~100 tokens，应显著快于 L1/L2 |
| P4-05 | 写入吞吐 | ≥ 20 条/s | 批量 `remember` |
| P4-06 | 端到端记忆提交延迟 | ≤ 30 s | 触发 commit（`commitTurnThreshold=8` 或 `commitTokenThreshold=20000`）到记忆可召回 |
| P4-07 | 代理层附加开销 | **≤ 50 ms** | 直连 vs 经 `/ov-proxy` 同请求对比 P50 差值 |
| P4-08 | 并发多用户 | 10 并发下 P95 退化 **≤ 2×** | 参考 `benchmark/custom/session_contention_benchmark.py` |
| P4-09 | 向量规模增长 | 记录基线 48 → 评测后条数；确认延迟不随规模显著劣化 | 每 500 条采样一次 P95 |
| P4-10 | 容器内存占用 | openviking 稳态 RSS 记录并设告警线 | `docker stats` |
| P4-11 | 超时不炸会话 | 人为制造慢响应，确认 `timeoutMs=30000`（opencode harness 默认）生效且**降级而非崩溃** | 插件设计原则："a hook that dies ... would take the host's session down with it" |

> 中文文本比等长英文的向量输入 token 更多，P4-01/02 阈值已按中文场景放宽。若 rerank 开启，P4-02 阈值相应上调至 ≤ 1200 ms 并单独记录。

> **实测结论（Phase 4，rerank OFF，2026-09-29）**：11 项全部出数。**P4-01/02/03/06/07/08/11 达标**（P50 139.8 ms、P95 314.8 ms、P99 335.0 ms；commit 可召回 20.8 s；代理开销 **+12.0 ms**；10 并发退化 **1.38×**；降级 **34/34 PASS**）。四项须修正口径或重跑：**P4-04** 数值达标但"L0 摘要应显著快于 L1/L2"的前提不成立（`find` 实测返回三级混合 42/215/743，p95 382.6 ms **反而慢于** `search`(list) 314.8 ms）；**P4-05** 单一阈值无法描述两个语义不同的通道（`wait=false` 20.7 条/s ✅ / `wait=true` 1.76 条/s ❌，后者 ≈ 一次 DashScope embedding 往返，属上游 SLA）；**P4-09** 因套件第 14 秒撞穿 embedding 配额（`429 insufficient_quota` × 99、索引冻结）而**整体失效**，见 **R24**；**P4-10** 测到 757.9 MiB（cgroup 峰值 964.5 MiB）但**容器无内存上限**（`HostConfig.Memory=0`），"告警线"无配置载体。⇒ **没有任何一项指向 OpenViking 的检索或中文能力缺陷。** 完整数据、工装约束与复现命令见 §11.13。

### Phase 5 — 兼容性与多租户隔离

| ID | 用例 | 期望 |
| --- | --- | --- |
| P5-01 | **跨用户越权（关键）** | 用户 A 的令牌请求 `viking://user/{B}/memories/...` → **必须 403/空结果**，绝不返回 B 的内容 |
| P5-02 | 令牌伪造 | 篡改代理令牌 → `verify_proxy_token` 返回 None → 401 |
| P5-03 | 令牌重放跨用户 | A 的令牌无法通过任何头部组合（`X-OpenViking-Account`、`X-OpenViking-Actor-Peer`）提升为 B 或 ADMIN |
| P5-04 | Admin API 不可达 | 经代理请求 Admin 路径 → 一律拒绝 |
| P5-05 | ROOT Key 不下发 | 容器内 `env` 与文件系统搜不到 root key；只有代理令牌 |
| P5-06 | 角色边界 | ROOT / ADMIN / USER 三级作用域各自可做的操作符合官方定义 |
| P5-07 | **容器重建后记忆存续** | 删除并重建用户容器 → 记忆完整（记忆在服务端，不在容器内）。**实测 PASS**：`docker rm -f` 后第三次 `ensure_container` 拉起新容器（identity `7940a10b`→`3779a848`），命名卷原样保留；直读 + agent 通道 MCP read 均返回完整中文内容，重建前后 `fs/tree` **12 叶逐项全等**，见 §11.18 A |
| P5-08 | Key 轮换触发重建 | `_ov_env_stale()` 返回 True → 容器重建 → 新令牌生效，记忆不丢。**实测 PASS**：轮换 `AGENT_SECRET_KEY` 后 stale 四证据全中（旧 token 不可解 / `ov_env_stale=true` / seed 已变 / 新 key 直测 401）→ R12 重铸（key 与本地派生逐字符一致）→ 容器**自动**重建 → **轮回场景端到端自愈**（agent MCP read 200 in 0.094s）→ 终态 `.env` 字节级还原、tree 17 叶全等，见 §11.18 B |
| P5-09 | 版本兼容 | opencode 1.18.25 × 插件 `2026.9.25-2` × 服务端 digest `sha256:569193ef…` 三方握手成功。**判据须指明"哪一面"**：服务端 REST `/health` 报 **`v0.4.21`**，而 MCP `serverInfo.version` 报 **`1.27.0`** —— 两者不同源，用其中任一个去断言另一个必然失败 |
| P5-10 | **既有能力共存** | `web_search`（builtin-mcp）、`oh-my-opencode-slim`、`present-file`（builtin-plugins）全部仍正常工作，无工具名冲突、无 MCP 列表覆盖。**冲突判定必须按 opencode 的实际前缀规则**：裸名 `search` 在 `web_search` 与 `openviking` 下**都注册**（重叠），但 agent 看到的是 `web_search_search` / `openviking_search` ⇒ **前缀后零冲突** |
| P5-11 | 用户开关 | host `opencode.json` 的 `builtin_mcp.openviking.enabled=false` → 该用户容器内记忆工具消失，其余不受影响；取消隐藏后权限键 `openviking_*` 的 allow/deny 对称生效。**判据须分两层**：Layer 1 = 渲染层纯函数矩阵（`build_container_config` / `apply_plugin_visibility` / `hidden_mcps_from_config` 幂等性）；Layer 2 = **活体推送到运行中容器**（`_push_to_container` → `write_config_file` + `PATCH /global/config`），并在**每一步之后从容器内部独立 `cat` 回读**，不能只信 backend 的 Docker-archive 读回 |
| P5-12 | ~~插件自带测试门禁~~ **不可执行，改为构建门禁** | 发布包 `files` 白名单不含 `tests/`，`node --test tests/*.test.mjs` 在镜像内无文件可跑。替代物是 Dockerfile 里的构建门禁：真实 `loadConfig()` 解析 + 6 项断言（见 §10 结论 3），失败即构建失败 |
| P5-13 | ~~插件语法门禁~~ **已被 P5-12 覆盖** | `npm run check` 同样依赖未发布的脚本。构建门禁的 `import('./index.mjs')` + `import('./lib/config.mjs')` 已隐含语法与形状校验 |
| P5-14 | 只读 rootfs 兼容 | 插件不尝试写 `/opt/agent`；~~`writePathAsync=true` 的落盘路径落在 `/data` 或 `/workspace`~~ **判据陈旧须改写**：`writePathAsync` 只在 `config-schema.mjs:155` 被**声明**，全包**无任何读取点**（死旋钮，同 R15 的 `recallExcludeUris`）。真实判据是**逐条枚举插件的写入路径**并归入 `/data`(volume) vs `/home/agent`(tmpfs)，见 §11.16 F |
| P5-15 | 服务不可用时降级 | 停掉 openviking → agent 会话**仍能正常工作**，仅记忆功能静默失效，不阻塞对话。**实测 PASS**：`docker stop` OV 后代理快速失败（`/ov/health` 502 in 1.09s、MCP 502 in 0.083s，均返回中文错误文案，不挂起）；**宕机期间 2/2 轮完整会话全部 OK**（5.70s / 3.88s）；容器日志 15 分钟窗口 0 条 openviking 行（静默失效）；`docker start` 后 0.029s 回绿。⚠️ 驱动契约：opencode 1.18.25 的 `POST /session/:id/message` **只在根路径且同步返回 JSON**，见 §11.18 C/D |
| P5-16 | 配置越界钳制 | 传入越界 knob 值（如 `recallLimit=999`）→ 钳制到 1–50 而非报错。**须附带枚举例外**：`authMode` 的越界值（含大小写不符的 `API_KEY`）**不是钳制而是静默降级为空串**，即丢掉平台在 `container_manager.py:519-522` 钉死的 `api_key`，见 §11.16 G |
| P5-17 | 多模态范围确认 | embedding `input: "text"`，确认图片/音频记忆路径是否在范围内；不在则显式记录为 out-of-scope。**须分读/写两面取证**：REST 面所有非文本端点与 MCP 写入工具的载荷类型分别验证，不能只看 embedding 声明 |
| P5-18 | **派生 Key 一致性**（R11） | `ensure_user_registered` 注册后回读服务端返回的 `user_key`，与 `derive_user_key()` 本地重算值**逐字符相等**；不等则记 ERROR 并回落"存服务端返回值"。**实测 PASS**：4 个新用户（`p5gc-a/b`、`p5gd-a/b`）注册回读全 `returned_equals_derived=true`（len 115）；409 幂等、realign（`POST …/users/{uid}/key`）回读亦全等；**R11 失败回落机制直测生效**（ERROR 日志 + override 仅本进程 + 真实用户零 override）。**架构确认**：容器 env 的 `OPENVIKING_API_KEY`（len=100，head=`gAAAAA`）是 **Fernet 加密的 proxy token**（解码 = `ovproxy:p2probe`），**不是** OV user key —— 真实派生 key（len 115/117）只存在于 backend 进程内，从不进容器（`config.py:107` 注释明示），见 §11.17 A |
| P5-19 | **计数端点不得作为隔离证据**（R19） | 断言 `/api/v1/stats/memories` 与 `/api/v1/debug/vector/count` 是 **account 级聚合**（实测灌 5 条 preferences 后显示 8，含另一用户的 3 条）。越权断言一律改用内容面端点（`read` / `search` 期望 403）。若需 per-user 计量，只能按 `viking://user/{uid}` 前缀 `ls` 后自行计数。**实测 PASS（判据坐实）**：三个 user key（含 **0 记忆的新用户**）的 `stats/memories` 返回**逐字符相同**（651 条聚合）⇒ account 级实锤；`vector/count`=837；内容面反证 b 跨读 `viking://user/ovzh-eval/…/慢查询案例.md` → **403**。⚠️ 新发现：**ROOT key 对租户数据面 API 全部 403**（stats / vector / fs / MCP 全拒）⇒ 取证必须用 user/admin key，见 §11.17 B |
| P5-20 | **删除通道**（R20） | ~~REST 面无任何删除端点（126 paths 逐一验证）~~ **该表述已实测推翻**：`openapi.json`（root key 可读，126 paths）实有 **16 条 DELETE 路径**（`/api/v1/fs`、`/api/v1/sessions/{session_id}`、`/api/v1/skills/{skill_name}`、admin 面账号/用户/模板、`/webdav/resources`、`/api/v1/watches` 等）。**但「agent 可达面无删除」仍成立（三重防线）**：① `_ALLOWED_REST` 8 条白名单全 GET/POST 语义、无 DELETE（8 条 DELETE 探针实测 405/404）；② `_ALLOWED_TOOLS` 不含 `forget`/`cancel_watch`（proxy 复核 `-32601`「平台未开放记忆工具 forget。」）；③ 容器只有 proxy token 无真实 key。**服务端隔离实测全绿**：跨用户 `forget`（含 `recursive:true`）均 `isError:true "Access denied"`、own 删除成功 + 404 复核；`DELETE /api/v1/fs`（`summary:"Rm"`，query 形 `uri`/`recursive`）own 生效（`estimated_deleted_count:1`）、跨用户 **403**。**产品决策（定案）：不放开 `forget`**，理由与最小放开路径见 §11.17 C |
| P5-21 | **写入路径不落共享作用域**（R16） | 平台自动写入产生的 URI **一律**在 `viking://user/{uid}/` 下；`viking://agent` 与 `viking://resources` 下 0 条平台写入（后者还有"写进去 search 搜不到"的连带问题）。**实测 FAIL ⇒ R29（高危）**：通过的一半 —— 平台自动写入确实全落 `viking://user/{uid}/`（p2probe tree 全量核验）；**失败的一半 —— user key 写 `viking://resources/*` 与 `viking://agent/*` 均 200，且 agent 容器经 proxy `write` 工具同样能写**；写后**另一用户能搜到（score 0.78）、读到全文、ls 列出**（泄漏四面全通），且注入内容 `semantic_status:complete` 进语义索引 ⇒ 跨用户注入通道完整成立。唯一守住的是 user scope 跨读/跨写/跨删全 403。见 §11.17 D |

> **实测结论（Phase 5 Group A = P5-01…P5-06，2026-09-29）**：**6 项全部 PASS，两个硬性项（越权 0、真 Key 0）零例外。**
> · **P5-01 跨用户越权 = 0 次泄露**：A(`p2probe`) → B(`ovzh-eval`) 的 **12 条路径全部 403 `PERMISSION_DENIED`** —— REST `content/read`（`memories/identity.md`、`preferences/.overview.md`）、`fs/ls`（memories 子树 / user root / **percent-encoded URI**）、`search/search` + `target_uri`、`search/find` + `target_uri`，MCP `list` / `tree` / `grep` / `glob` / **`read`（正确参数名 `uris[]`，见下）**。对照组 A→A 全 200。**两条判据须修正**：① `search` 的 `mode=context` **不接受 `target_uri`** → 400 `INVALID_ARGUMENT`，该组合无法作为越权探针；② 原设计的"B 独有 marker 语义搜索"命中 **0.8368**，但 URI 是 `viking://user/p2probe/memories/preferences/user/日志保留策略.md` ⇒ **是 A 自己的记忆**（Phase 2 给 `p2probe` 灌过同一句），marker 并非 B 独有，**不构成泄露**。另：`~` 家目录别名解析到 `viking://user/p2probe/...`（200），与 `core/namespace.resolve_current_user_uri` 的说法一致。
> · **P5-02 令牌伪造 = 全 401**：空令牌 / 末位翻转 / 首位翻转 / 截断 32 字符 / 字面量 `ovproxy:p2probe` / `ovproxy:platform-admin` / 纯垃圾串，一律 `{"error":{"message":"无效的记忆服务访问凭据，请联系管理员重建容器。"}}`。附带两条：**令牌放 query string（`&api_key=`）不认** → 401；`X-API-Key: bogus` + `Authorization: Bearer <valid>` → **401**（`_caller_user_id` 里 `x-api-key` 优先，一旦存在就不再回落 Authorization）。
> · **P5-03 头部提权 = 全 403**：H1–H6 伪造 identity 头（`X-OpenViking-Account: ovzh-eval` / `-User: ovzh-eval` / `-Actor-Peer: ovzh-eval` / 三者齐发 / `-User: platform-admin` / `-Account: agent-platform`）全部 403 ⇒ **这些头不在 `_REQUEST_DROP_HEADERS` 里、确实被转发，但上游在 `api_key` 模式下完全无视它们**（与 `container_manager.py:519-522` 的设计意图双向印证）。H7–H11 把**真实外域 key** 塞进第二个头（B 的 user key / ROOT / admin，分别走 `X-API-Key` 与 `Authorization`）→ 全 403 ⇒ 代理丢弃调用方凭据、只注入 A 自己的派生 key。**直连对照坐实这些 key 本身有效**：B 的 key 直打 `openviking:1933` → `fs/ls B/memories` **200**；而**代理令牌直连 → 401 `UNAUTHENTICATED`「Invalid API Key」** ⇒ 它确实不是 OV key。ROOT key 直打租户数据面 → 403「ROOT API keys cannot access tenant-scoped data APIs in api_key mode」。
> · **P5-04 Admin/debug/console 不可达 = PASS（但见 R13）**：经代理 8 条 admin/debug/stats/forget/content-write 路径（含 `..` 穿越两种写法）全 **404 `{"error":{"message":"记忆服务不提供该接口。"}}`**；`/ov/console`、`/ov/metrics`、`/ov/openapi.json`、`/ov/docs` → **404 `{"detail":"Not Found"}`**（FastAPI 层连路由都没有）。白名单内对照全通：`/ov/api/v1/system/status` → `{"initialized":true,"user":"p2probe"}`、`/ov/health` → `{"version":"v0.4.21","auth_mode":"api_key","account_id":"agent-platform","user_id":"p2probe","role":"user"}`。MCP 工具级拒绝（`forget`/`cancel_watch`/`add_account`/`delete_resource`）→ **HTTP 200 + JSON-RPC `-32601`「平台未开放记忆工具 X。」**（agent 看见的是工具失败而非传输错误，符合 `ov_proxy.py:82-83` 的设计）。⚠️ **`tools/list` 不过滤**，实测 **15 个工具**且**无 `add_skill`** ⇒ **R14 的陈旧项坐实**。
> · **P5-05 ROOT key 不下发 = PASS（净测）**：容器 env 全量 25 项中凭据形状只有 3 个 —— `FASTK_API_KEY`(len=100, head=`gAAAAA`) / `OPENVIKING_API_KEY`(len=100, head=`gAAAAA`, **与前者不相同**) / `OPENCODE_SERVER_PASSWORD`(len=43)；9 个目录文件系统扫描 **0 命中**。**三把真凭据（ROOT OV key len=64 / `AGENT_SECRET_KEY` len=64 / B 的 OV key len=119）在 A 的 env 与 fs 中命中数均为 `env_hits=0 fs_files=0`**。⚠️ 首轮跑出的 `env_hits=1` 是**自伤假阳性**（用 `docker exec -e KROOT=…` 把待检 key 注进了同一个 exec 的 env），净测改为**经 stdin 落 `/tmp`（不在扫描目录内、用后即删）**，永不进 env/argv。附带辨明：`opencode.json` 里的 `apiKey`(len=36, head=`21a57a`) 是**宿主 LLM 供应商 `volces-gateway` 的 key**（`opencode_config.build_container_config` 只改写 `baseURL`、`apiKey` 原样透传，见 `text_tools.py:9-11`），**与 OpenViking 无关**；`ovcli.conf` 全文 106 B = `{"plugin":{"opencode":{"mcpEnabled":false,"repoContext":false,"dataDir":"/data/state/openviking"}}}`，**不含任何凭据**。
> · **P5-06 角色边界矩阵 = PASS**（ROOT / ADMIN(`platform-admin`) / USER(`p2probe`)，直连 `openviking:1933`，11 个探针）：ROOT 是**纯管理面** —— `admin/accounts` 200、`admin/accounts/{id}/users` 200，但**一切租户数据面一律 403**（`stats/memories`、`debug/vector/count`、`search/search`、`content/read`、`fs/ls`）；ADMIN 可列账号内用户 + 可读 account 级计数，但**读不了任何用户空间**（连自己 account 下的 `p2probe`/`ovzh-eval` 都 403），可读共享作用域 `viking://agent`(200) 与 `viking://resources`(200, `[]`)；USER 只能读自己的空间。两个契约细节：`GET /api/v1/admin/accounts/{id}` 对**所有角色都 405**（该路径只接受 POST/PUT）；**R19 第 4 次实证** —— ADMIN 与 USER 的 `stats/memories` 返回**逐字符相同**（`total_memories 651`、`cases 598`…）⇒ 计数端点是 account 级聚合，**绝不能当 per-user 计量或隔离证据**。
> · **两个新发现（见 R13 / R26）**：**R13 复核 + 决策输入** —— A 容器**无 key 即可直连 `openviking:1933`**（`/health` → 200、`/api/v1/system/status` → 401、`/api/v1/admin/accounts` → 401），说明 agent 容器与 openviking 同在 `agent-net`，`ov_proxy` 的白名单**不是网络边界**，`ov_proxy.py:27-29` 宣称的"unreachable by construction"只在**经代理**这一条路径上成立；唯一防线是"容器内不存在有效 OV key"（P5-05 已验证成立，Group A 亦未找到任何绕过）。**R26（新）** —— MCP 的拒绝形状**不一致**：`search`/`list`/`tree`/`grep` 返回 `isError:true`，而 `read`/`glob` 返回 **`isError:false`** 且正文只是 `"Access denied for …"` ⇒ 只看 `isError` 的 agent 会把一次越权拒绝当成成功。
> · **工装教训（编码）**：Group A 首跑输出里的中文乱码（`鏃ュ織淇濈暀绛栫暐`）**不是服务端缺陷** —— 落盘字节是正确 UTF-8（`\xe6\x97\xa5\xe5\xbf\x97` = 日志），按 UTF-8 解码即得「日志保留策略」，按 GBK 解码才得到那串乱码 ⇒ **纯属 PowerShell 管道以 codepage 936 解码的显示层产物**。（`iconv` 报的 offset 6853 无效字节则是探针自身 `head -c 400` 把一个多字节字符切成两半，同样非服务端问题。）
> · **本组数据有 3 次被 R25 作废后重跑**：`05:41:06` / `05:44:11`（dockerd 重生）/ `05:53:09` / `06:08:55` 四次栈级重启全部落在工具调用间隙，表现为 **503「记忆服务当前不可用」而非 403**，极易误读为权限缺陷 ⇒ 所有跨分钟脚本**必须带存活门禁 + 分段 `VALID=` 标记**。完整数据与复现命令见 §11.15。

> **实测结论（Phase 5 Group B = P5-09/10/11/12/13/14/16/17，2026-09-29）**：**8 项全部 PASS**，但暴露 **2 个真实缺陷（R27 / R28）**，其中 R27 是"用户开关不可逆"级别的功能损伤。
> · **P5-09 版本三方握手 = PASS**：opencode **1.18.25** × 插件 **`2026.9.25-2`**（`openvikingSourceCommit=3e02c6ac29842be214fa1560dfaf6ea39a69322c`，`dependencies={}`）× 服务端镜像 `sha256:569193efd49a…757d05`。**关键契约修正**：REST `/ov/health` 报 **`version:"v0.4.21"`**，MCP `serverInfo` 报 **`{"name":"openviking","version":"1.27.0"}`** ⇒ **两个面不同源**，任何"版本一致"断言必须先指明测的是哪一面。
> · **P5-10 既有能力共存 = PASS（零冲突，9 段证据）**：容器内实渲染的 `/data/config/opencode/opencode.json` 里 **3 个 MCP 并存且全 `enabled=true`** —— `demo-mcp`(remote) / `openviking`(remote，`http://backend:8000/ov/mcp` + 已盖章的 100 字符 `X-API-Key`) / `web_search`(local，`python3 /opt/agent/builtin-mcp/web_search/web_search_mcp.py`)；**3 个插件并存** —— `oh-my-opencode-slim@2.2.15` / `@openviking/opencode-plugin@2026.9.25-2` / `opencode-present-file@1.0.0`。**冲突判定的正确口径**：裸名重叠 **`["search"]`**（两边都注册），但加前缀后 `web_search_search` vs `openviking_search` ⇒ **`prefixed_overlap=[]`，零冲突**。**真握手验证**：web_search stdio `initialize` → `serverInfo={"name":"web-search-mcp","version":"1.0.0"}`、`protocolVersion="2024-11-05"`、`tools=["search"]`、`stderr=""`；openviking → 200、`serverInfo={"name":"openviking","version":"1.27.0"}`、**15 工具**；searxng `healthz=200`。**node 动态 import 形状**（rc=0）：present-file exports=`["default"]`、oh-my-opencode-slim exports=`["OhMyOpenCodeLite","default"]` arity=1、openviking exports=`["OpenVikingPlugin","default"]` arity=0 ⇒ 三者形状互不干扰。⚠️ 一条**取证陷阱**：opencode server 的 `/api/config`、`/api/mcp`、`/api/tool`、`/api/permission` 全返回 **200 len=2884 = SPA HTML 兜底**（不是真 API），**不能用它查 MCP 运行态**；真 API 只有 `/api/health`、`/api/agent`、`/api/provider`、`/api/session`（len 16/91/91/50）。另：`/opt/agent/builtin-mcp` 下有 **3 个目录**（`fastk` / `openviking` / `web_search`），但 `fastk` **无 `manifest.json`** ⇒ `_discover_builtin_mcp()` 发现不了它，宿主 `builtin_mcp.fastk.enabled=true` 是**死控制**。
> · **P5-11 用户开关 = PASS（两层全绿），但见 R27**。**Layer 1（渲染层纯函数矩阵，C0–C9 + 幂等性）**：9 个用例的 `mcp_perm_rules` / `orchestrator.mcps` / `librarian.mcps` / `hidden_mcps_from_config` 往返**全部与源码预期逐字段一致**；**所有用例 `mcp_enabled` 恒为 `{demo-mcp:true,openviking:true,web_search:true}`、`ov_entry_present=true`、`ov_header_stamped=true`**（即使被隐藏，per-user 令牌仍照常盖章）⇒ 印证"开关只作用在 permission 层，条目从不被摘除"。C8/C9 补出一条契约：host **remote** MCP `enabled:false` → 进 hidden；host **local** MCP `enabled:false` → **`hidden=[]`**（local 根本不注入，无可隐藏）。**Layer 2（活体推送，6 步 `push_ok=true` 全绿）**：真实走 `_sync_plugin_config`（Docker archive `get_archive`/`put_archive`）+ `PATCH /global/config`（`tunnel_relay`），**只 stub 了 `agent_controller.get_agent_gate` 的查库这一步**（`p2probe` 无 DB 记录 ⇒ 真实门禁 `{"running":false,"password_len":0}`，口令经 **stdin** 注入不进 argv/env）。**每一步都从容器内部独立 `cat` 回读，与 backend 的 Docker-archive 读回逐字段相同** ⇒ 写入真的落在 `/data/config/opencode/`（volume），**独立再证 P5-14 的 `/data` WRITE_OK**，且 `put_archive` 能穿透只读 rootfs。六步 `roundtrip_hidden` 每一步都**精确等于** effective hidden 集 ⇒ `hidden_mcps_from_config` 是忠实逆函数，opencode 重启后状态可正确复原。**开关 openviking 完全不干扰其他能力**：`librarian.mcps` 全程 `["web_search"]`、`plugin_list_len=3`、`mcp_enabled` 三项、`disabled_skills=null` 均不变。
> · **P5-12 / P5-13 构建门禁 = PASS**：镜像构建期 `extracted_gate_count=2`，gate1 → `[build] present-file plugin OK, args: path,title,mode,focus,note`，gate2 → `[build] openviking plugin OK: hook-only, env credentials, dataDir /data/state/openviking`，`GATES_RC=0`。发布包 `files` 白名单 = `["index.mjs","lib/","servers/","scripts/","README.md","INSTALL.md","INSTALL-ZH.md"]`，**`tests_dir=ABSENT`** ⇒ 原设计（跑插件自带测试）在镜像内确实**无文件可跑**，改为构建门禁的决策成立。
> · **P5-14 只读 rootfs = 主判据 PASS + 发现真实缺口**：`ReadonlyRootfs=true`，`Tmpfs={"/home/agent":"size=512m,…","/tmp":"size=256m"}`。**WRITE_DENY** = `/opt/agent`、`/opt/agent/builtin-plugins/openviking`、`/usr/local/bin`、`/library/pptx`；**WRITE_OK** = `/data`、`/data/state`、`/data/state/openviking`、`/workspace`、`/home/agent`、`/home/agent/.openviking`、`/tmp`。⚠️ **但"落在 `/data`"只对 1/5 条写入路径成立**：`initLogger(resolveDataDir())` → `/data/state/openviking/openviking-memory.log`（volume，实测写入 115 B ✅）；而 `workspace-identity.mjs:33-39` → `/home/agent/.openviking/state`、`pending-queue.mjs:57-59` → `/home/agent/.openviking/pending`（**离线补投队列**）、`recall-core.mjs:415-418` → `OPENVIKING_STATE_DIR` 否则 `~/.openviking/state`、`plugin-config.mjs:265-266` debug log ⇒ **4 条全在 tmpfs，容器重启即丢**。容器 env 全量 **27 项**里这 4 个 env **一个都没设**（`XDG_STATE_HOME=/data/state` 虽已设但**插件不读 XDG**）⇒ **`Dockerfile:310-313` 的 dataDir 注释有误**。重定位**已端到端验证有效**：注入 `OPENVIKING_STATE_DIR=/data/state/openviking/state` 后 `/data/state/openviking/state/ws-identity-c52ddf65534b.json` 真实落盘；完整搬迁需 **3 个 env**（`OPENVIKING_STATE_DIR` + `OPENVIKING_PENDING_DIR` + `OPENVIKING_DEBUG_LOG`）。
> · **P5-16 配置越界钳制 = PASS（33 用例全钳制、无一抛错）**：`recallLimit 999→50 / -5→1 / 0→1 / "abc"→10 / 7.6→8 / 1e9→50`；`scoreThreshold 7→1 / -1→0 / nan→0.35`；`minQueryLength 0→1 / 9999→64`；`recallTokenBudget 1→200 / 99999999→50000`；`recallMaxContentChars 10→100 / 999999→5000`；`timeoutMs 10→1000 / 999999999→300000`；`captureMaxLength 1→200 / 99999999→100000`；`recallMaxTokens 1→64`；`recallCompressMaxBullets 0→1`；`enabled "off"→false / "maybe"→true`；`recallQueryFilters "a, b ,c"→["a","b","c"]`；`captureMode bogus→semantic`；`logLevel verbose→error`；`recallPeerScope root→all`。层序正确（**env > workspace-file**）。端到端 `loadConfig` 一致（`TIMEOUT_MS=1` → `timeoutMs:1000, captureTimeoutMs:30000, recallCompressTimeoutMs:110000`）。⚠️ **唯一例外是 `authMode`**：`bogus→""`、**`API_KEY→""`**（enum 大小写敏感）⇒ **静默降级为空串而不是保持平台钉死的 `api_key`**，是 P5-16 里唯一的真实隐患。
> · **P5-17 多模态范围 = 已定案（写侧 out-of-scope，读侧有能力）**：REST 面 **10 条非文本候选端点全 404**（`/ov/api/v1/{resources,resources/upload,upload,files,images,audio,embed,embeddings,models,multimodal}`）；MCP **15 个工具的 `inputSchema` 中多模态关键字仅命中 1 处** —— `add_resource.temp_file_id`；而 `read` 的描述原文含「**Raster images and supported audio return native MCP content blocks.**」⇒ **服务端读侧有多模态能力**。但所有**写入**工具（`write` / `edit` / `remember`）的载荷类型都是 `string` / `array<object>` ⇒ **平台自动写入面是纯文本**。`list viking://user/p2probe/resources` → `[dir] p2`。
> · **两个新发现（见 R27 / R28）**：**R27（中危，功能损伤）** —— 白名单形 preset mcps 的**取消隐藏不可逆**：`librarian.mcps=["web_search"]` → 隐藏 web_search → `[]` → 取消隐藏 → **仍是 `[]`**，librarian 子代理**永久失去 web_search**。根因在 `opencode_config.py:563`（白名单项被隐藏即**从文件里删除**）与 `:565-568`（**只有含 `*` 的 allow-all 形才用 `!name` 标记**，因此可逆）。openviking 自身走 allow-all 形 ⇒ **完全可逆**，只有"被当成白名单项的其它 builtin MCP"受损。自愈路径已证：`render_plugin_config:628` 从零重建 `mcps=["web_search"]`，还原脚本实测 `rendered_equals_restored=true` ⇒ **容器 start/recreate 会自愈，只有"运行中热开关"这一条路径受损**。**R28（低危，配置残留）** —— 一次开关循环后 `permission` 从 **14 键变 15 键**，多出的 `web_search_*` **取消隐藏后不删**；且宿主基线自带的是 **`web_search*`（无下划线）**，而平台 `_sanitize_mcp_permission_key` 产出 **`web_search_*`** ⇒ **两条不同名键并存**，一 allow 一 deny 时优先级未定义/未测。
> · **测试残留已精确还原**：Layer 2 结束后容器**未回到基线**（`librarian.mcps=[]`、`perm=15` 键），已用还原脚本删掉 `web_search_*` 键 + 补回 `librarian.mcps=["web_search"]`，容器内独立回读确认 `perm_count=14`（键集精确等于基线）、`orchestrator.mcps=["*"]`、`agents_count=9`、`roundtrip_hidden=[]`、`mcp_enabled` 三项全 true ⇒ **`RESTORE_OK=true`**，且平台自己的 `render_plugin_config` 产出与还原后的文件**结构完全相等**。
> · **R25 第 15 次复现**：本组两次跑分别撞上 `07:42:07` 与 `07:45:33` 的栈级重启（A/OV/BE/frontend 同时 `started`），门禁第 1 轮 `local_ov=000 via_proxy=503`、第 2 轮 `200/200`；`kernel_uptime_sec=106643`（VM 未重启），四个容器 `created` 全部未变、`RestartCount` 全 0 ⇒ **再次坐实"整栈被停止后重新启动，容器对象未重建"**。完整数据与复现命令见 §11.16。

> **实测结论（Phase 5 Group C = P5-18/19/20/21，2026-09-29）**：**3 PASS + 1 FAIL**，FAIL 项立 **R29（高危）**。两轮探针（`benchmark/ov-zh/out-p5gc.txt` 241 行 + `out-p5gd.txt` 249 行，全段 `VALID`）。
> · **P5-18 派生 Key 一致性（R11）= PASS**：4 个新用户（`p5gc-a/b`、`p5gd-a/b`）注册回读全部 `returned_equals_derived=true`（len 115）；409 重复注册幂等（不回读 key，`_register_user:237` 注释明示）、realign 回读亦全等；**R11 失败回落机制直测生效**（monkeypatch `_admin_post` 强制抛错 → `fallback_error_logged=true`、回落值照常落库，真实用户零污染 `overrides_for_real_users=[]`）。**架构确认**：容器 env 的 `OPENVIKING_API_KEY`（len 100，head `gAAAAA`）是 **Fernet 加密的 proxy token**（解码 = `ovproxy:p2probe`；`ov_access.py:76-79` `issue_proxy_token=encrypt_secret("ovproxy:"+user_id)`），**不是 OV user key** —— 真实派生 key 永不出 backend（`config.py:107` 注释"never at openviking_url: agent containers must not be able"）。首轮 stage 1b 的 `P5_18_LIVE_KEY=MISMATCH live_len=100 derived_len=117` 是设计使然（正是 P5-05「真 Key 0」的机制解释：容器那把 key 直连 OV 一切数据面 401）。见 §11.17 A。
> · **P5-19 计数端点非隔离证据（R19）= PASS（判据坐实）**：三个 user key（含 **0 记忆的新用户**）的 `stats/memories` 返回**逐字符相同**（`total_memories 651, cases 598, profile 5, preferences 15, entities 7, events 10, patterns 5, tools 6, skills 5, warm 651`）⇒ account 级聚合实锤；`debug/vector/count`=837。隔离反证改用内容面：第一轮的 `ovzh-eval_sample_uri=null` 是探针缺陷（根级 recursive `fs/ls` 深度截断，R21），跟进轮用 `fs/tree level_limit=3` 定位真实文件补测 —— b 跨读 `viking://user/ovzh-eval/memories/cases/慢查询案例.md` → **403**。⚠️ **新发现（契约）**：**ROOT key 对租户数据面 API 全部 403**（报文「ROOT API keys cannot access tenant-scoped data APIs in api_key mode」）⇒ 跨用户取证必须用 user/admin key。见 §11.17 B。
> · **P5-20 删除通道（R20）= PASS（判据修正 + 产品决策定案）**：原判「REST 面没有任何删除端点」**被 openapi.json 实测推翻** —— 126 paths 实有 **16 条 DELETE**（含 `/api/v1/fs`、`/api/v1/sessions/{session_id}`、`/api/v1/skills/{skill_name}`、admin 面 `DELETE …/users/{uid}`、`/webdav/resources`、`/api/v1/watches`）。**但「agent 可达面无删除」仍成立（三重防线）**：① `_ALLOWED_REST` 8 条无 DELETE（`content/write,read`、`fs/ls`、`skills` → 405；`fs/rm`、`content/delete`、`memories`、`sessions/{x}` → 404）；② `_ALLOWED_TOOLS` 无 `forget`/`cancel_watch`（proxy `tools/call forget` → `-32601`「平台未开放记忆工具 forget。」）；③ 容器无真实 OV key。服务端隔离实测全绿：跨用户/递归 `forget` 拒（`isError:true "Access denied"`、目标仍在），`DELETE /api/v1/fs` own 生效（200 `estimated_deleted_count:1` + read 404 复核）、跨用户 403 PERMISSION_DENIED。**产品决策（定案）：不放开 `forget`** —— ① proxy `_dispatch` 对 MCP 工具参数无 URI 前缀校验（源码确认），放开即无防线；② 须先加 `viking://user/{caller_uid}/` 前缀守卫 + 修 R26 的 isError 归一化；③ 当前删除走管理员通道已够用（admin 面 `DELETE …/users/{uid}` → 202）。最小放开路径 = `_ALLOWED_TOOLS` 加 forget + URI 守卫 + 拒绝形状归一化（与 R14 的 tools/list 过滤可合并同一补丁）。见 §11.17 C。
> · **P5-21 写入路径作用域（R16）= FAIL ⇒ R29（高危）**：**通过的一半** —— 平台自动写入全落 user scope（p2probe 全量 tree 复核，无一落共享作用域）；**失败的一半** —— user key `write` 写 `viking://resources/p5gd-res-marker.md`（102 B「杭州湾跨海大桥全长三十六公里…」）与 `viking://agent/p5gd-agent-marker.md`（86 B「舟山群岛拥有大小岛屿一千三百九十个…」）均 200（`mode:create, context_type:resource, semantic_status:complete`，进语义索引并生成中文摘要）；**agent 真实通道** —— A 容器（p2probe）经 proxy `/ov/mcp` `tools/call write` 写 `viking://resources/p5gd-proxy-marker.md` → "Wrote 116 bytes"；**泄漏四面全通** —— 另一用户 b 搜「杭州湾跨海大桥」命中注入文档 **score 0.7778**（与写入者自搜**同分** = account 级共享索引）、搜「港珠澳大桥」命中 proxy 标记 **0.7356**，b `content/read` 两个精确 URI → 200 全文，b `fs/ls` 两作用域 200，第三方（studio-debug，role=user）ls 亦 200 ⇒ **跨用户注入通道完整坐实**（0.78 ≫ 插件 `scoreThreshold` 0.35，召回链路必命中）。**守住的边界**：user scope 跨读/跨写/跨删全 403（a 写 `viking://user/p5gd-b/…` → 403 等）⇒ 硬性项「越权 0」仍成立，R29 属"合法通道被滥用"。根因两层：OV 服务端共享作用域无写 ACL；proxy 放行 `write` 且无 URI 校验。另：proxy 脚手架排除是 **per-node** 的（`_SCAFFOLD_URIS` 只排除 7 节点本身，子节点可被召回）。见 §11.17 D。
> · **残留清理完毕**：p5gc 两处共享作用域孤儿标记（写入者用户已删）由 admin key 补删（200×2）；p5gd 全部自清理（a 删两标记 200×2、p2probe 删 proxy 标记 200）；终态 `viking://resources`=`[]`、`viking://agent` 只剩平台目录（endpoints/memories）；四个探针用户 `admin DELETE …/users/{uid}` → 202×4。见 §11.17 E。
> · **R25 第 16/17 次复现**：p5gc 轮 `08:30` / p5gd 轮 `08:42` 各一次，均为门禁第 1 轮 `local_ov=000 via_proxy=503`、第 2 轮 `200/200` —— 两段存活门禁 + 分段 `VALID` 标记持续生效，无一组数据被污染。

> **实测结论（Phase 5 Group D = P5-07/08/15，2026-09-29）**：**3 项全部 PASS**。三段破坏性探针（`benchmark/ov-zh/out-p5ge.txt` / `out-p5gf.txt` / `out-p5gg.txt`，`DONE_P5GE/P5GF/P5GG` 09:58–10:09Z），全部在 `p2probe` 单用户上完成，终态自洽。
> · **P5-07 容器重建后记忆存续 = PASS**：`docker rm -f agent-p2probe` → 第三次 `ensure_container` 拉起新容器（identity `7940a10b`→`3779a848`），命名卷 `agent-data-p2probe` / `agent-ws-p2probe` 原样保留；口令可用、token 解码 `ovproxy:p2probe`；直读 + **agent 通道 MCP read 均返回完整中文内容**；重建前后 `fs/tree` **12 叶逐项全等**；删除复核 200→404。
> · **P5-08 Key 轮换触发重建 = PASS（R12 runbook 实测有效 + 轮回自愈链成立）**：轮换 `.env` 的 `AGENT_SECRET_KEY` 并重建 backend → **stale 四证据全中**（旧 token 不可解、`ov_env_stale=true`、seed 已轮换、新派生 key 直测 401）→ R12 重铸（`reauthorize` 返回 key 与本地派生**逐字符一致**，read 200）→ 容器**自动**重建（新 token/env 落位，agent MCP read 中文全文）→ `.env` 字节级还原 + backend 重建 → **轮回自愈**：还原后的旧 K1 再度 stale（直测 401）→ 又一次自动重建 → agent MCP read **200 in 0.094s**、K1 恢复 200 且 identical → 终态 `.env` 与原始逐字节一致、tree 17 叶与基线全等。⚠️ stage 1 的 `identical=false` 是探针未修的尾随换行比较（OV `content/read` 剥离 `\n`），cosmetic，stage 5/8 的 rstrip 判定均 true。
> · **P5-15 服务宕机降级 = PASS（R9 判据满足）**：基线会话 OK（8.36s）→ `docker stop` OV → 降级取证：`/ov/health` 经代理 **502 in 1.09s** + 中文文案「记忆服务不可达，请稍后重试。」、MCP read 经代理 **502 in 0.083s**（快速失败不挂起）→ **宕机期间 2/2 轮完整会话全部 OK**（5.70s / 3.88s）→ 容器日志 15 分钟窗口 **0 条 openviking 行**（静默失效，不阻塞对话）→ `docker start` 回绿后 MCP read **200 in 0.029s**、marker 中文命中「都江堰水利工程由李冰父子主持修建」、恢复会话 OK（2.31s）。
> · **两个取证契约（新发现，见 §11.18 D）**：① opencode 1.18.25 的 `POST /session/{id}/message` **只挂在根路径**（`/api` 前缀下 POST 落到 SPA catch-all，200 `text/html` len 2884），且**同步返回 `{info, parts}` JSON**（非 SSE；首调 30.56s 冷，热后 2–8s）；② OV `content/read` **剥离尾随换行** ⇒ 全等判定必须 `rstrip("\n")`，且 MCP `read` 参数是 `uris` 数组。
> · **R25 第 18–20 次复现**：三个探针的门禁各撞上第 1 轮 `local_ov=000 via_proxy=503`、第 2 轮 `200/200`（前序 09:42–09:47 曾每 ~2 分钟整栈重启 5 次、OV 日志 `LockAcquisitionError`，09:47:22 后 12/12 轮稳定）—— 存活门禁 + `DONE_*` 终标记持续生效。完整数据与复现命令见 §11.18。

---

## 6. 量化验收汇总

| 类别 | 指标 | 阈值 | **实测（2026-09-28）** |
| --- | --- | --- | --- |
| 中文召回 | G1 Recall@5 | ≥ 0.95 | **1.000 ✅** |
| 中文召回 | **G2 同义改写 Recall@5** | **≥ 0.80** | **1.000 ✅**（MRR 1.000） |
| 中文召回 | G2 MRR | ≥ 0.70 | **1.000 ✅** |
| 中文召回 | G3 口语化 Recall@5 | ≥ 0.70 | **0.750 ✅**（余量 0.05，小样本） |
| 中文精度 | G4 Precision@5 | ≥ 0.75 | **0.633 ❌**（阈值缺陷：等价于要求 recall@5 ≥ 0.9375，见 §11.12.5） |
| 中文精度 | G4 Precision@returned（**建议替换上一行**） | ≥ 0.75 | 未滤除 **0.633 ❌** / 滤除脚手架 **0.792 ✅** |
| 分类 | 8 类分类准确率 | ≥ 85% | **55.6%（5/9）❌** —— 但**判据本身失真**：抽取器实测只路由进 **4 个类目**（`preferences`/`entities`/`events`/`profile`），评测集自造的 `tools`/`skills`/`patterns` 自然对话永远产不出来；且 mis-filing **不影响召回**（错误类目照样 #1 命中 0.8567）。归因见 §11.12.8 |
| 分类 | **落点召回可用性（建议替换上一行）** | 100% | **100% ✅** —— 9/9 marker 全部 resolved（**没有丢记忆，只是投错类目**），5 组查询在 rerank OFF 下**不传 `target_uri`** 全部命中正确文档 |
| 中文内容 | 记忆正文 CJK ratio | ≥ 0.55 | **0.589 – 1.000 ✅**（8 个文件；ratio < 1 的部分全是结构性英文如 `# Summary`、`**user**:`，非 LLM 英文化）。**残留英文只在 `.overview.md` 脚手架**（ratio 0.000 – 0.220），**无受支持的配置通道可改**，见 §11.12.9 |
| CJK 预算 | 估算 / 真实 token 比值 | 0.9 – 1.3 | **判据失效**（`tiktoken` 未安装，无"真实值"可比）→ **改判为源码审查 + 活体预算验证**：服务端 `estimate_text_tokens` 本就 CJK-aware（1.5/汉字），`max_tokens=300` → `used_tokens 288`（0.96×，**在 0.9–1.3 内**），tier 优雅降级。§11.12.10 A |
| CJK 预算 | 注入超预算次数 | 0 | **0 ✅**（`used_tokens` 恒 ≤ `max_tokens`：缺省 1600→440、300→288） |
| CJK 预算 | **召回块 token 回收率（新增，建议纳入）** | ≥ 25% | **38.5% ✅**（同一 query：直连 893 tokens / 9 条 → 走代理 549 tokens / 4 条；渲染字符 −57.8%）。§11.12.10 E |
| CJK 预算 | **召回块 CJK 密度（新增，建议纳入）** | 提升且 CJK 绝对量不减 | **0.094 → 0.224（+138%）✅**，且 **CJK 字符数 221 → 221 完全不变 ⇒ 中文内容零损失**。§11.12.10 E |
| 中文查询 | **两字查询可用（新增，建议纳入）** | `minQueryLength` 不吞掉高频两字词 | **已修复 ✅**：`ovcli.conf` 设 `minQueryLength: 2`；B6「故障」→ `http=200`、4 条高相关命中（改前被插件门禁整段吞掉）。§11.12.10 A/E |
| 编码 | 中文回读逐字符一致率 | 100% | **100%（40/40，0 mismatch）✅** |
| 延迟 | 检索 P50 / P95 | ≤ 300 / ≤ 800 ms | **139.8 / 314.8 ms ✅**（list，n=200）；**172.5 / 399.7 ms ✅**（context，n=200）；P99 335.0 / 556.2 ms ✅（rerank OFF；ON 态为 3075 / 3784 ms ❌）。§11.13 B |
| 延迟 | `find` P95 | ≤ 500 ms | **382.6 ms ✅**（p50 145.0 / p99 514.0）—— 但**判据前提失真**：`find` 返回 L0/L1/L2 **三级混合**（42/215/743），不是「廉价的 L0 端点」，且 p95 反而**慢于** `search`(list)。§11.13 C.1 |
| 延迟 | 代理附加开销 | ≤ 50 ms | **+12.0 ms ✅**（P50 差值，n=100 交错采样，mean +17.3 ms）；与 P2-09 单发 145 KB 往返的 ~14 ms 吻合。冒烟的 −139.1 ms 已排除（n=5 噪声）。§11.13 H |
| 延迟 | 端到端记忆提交 | ≤ 30 s | **两个口径并列**：`remember` → **可召回 20.8 s ✅**（P4-06，polls=9 × 2 s；调用本身 83.7 ms）／ **抽取落盘完成 35.8 s ❌**（P3-01，12 轮 `system/wait`，`total_memories 43→45`，超阈值 19%）。它是**异步**的，不阻塞会话。`content/write` p50 **332 ms** / max 789 ms ✅。**建议判据拆成"调用返回 ≤ 1 s"与"抽取落盘 ≤ 60 s"**，见 §11.12.6 |
| 吞吐 | 写入吞吐 | ≥ 20 条/s | **判据需拆分**：`wait=false`（API 接受）**20.7 条/s ✅** / `wait=true`（含 embedding 落盘）**1.76 条/s ❌**。后者 567.5 ms 均值≈一次到阿里云 Model Studio 的 embedding 往返，**是上游 SLA 不是 OV 缺陷**。§11.13 C.2 |
| 并发 | 10 并发 P95 退化 | ≤ 2× | **1.38× ✅**（435.5 / 314.8 ms，n=200，workers=10，45.8 rps，wall 4.4 s）。§11.13 B |
| 规模 | 向量规模增长下延迟不劣化 | 无显著劣化 | ❌ **测量无效**：scale 套件第 14 秒撞穿 **embedding 配额**（`429 insufficient_quota` × 99），断路器打开、索引冻结（cp200 的 `vector_count` 与 cp100 同为 390；500 docs 只换来 +151 向量）。**P4-01…P4-08 全部早于该时刻，不受影响**。归因与重跑前置条件见 §11.13 E / **R24** |
| 内存 | 容器稳态 RSS + 告警线 | 记录并设线 | 负载后 **757.9 MiB**（cgroup 峰值 **964.5 MiB**，含 R24 积压压力）；冷启 395.3 MiB。⚠️ **`HostConfig.Memory=0` / `memory.max=max` ⇒ 容器无任何内存上限**，"告警线"目前无配置载体。建议 `mem_limit: 1.5g` + 告警线 1.0 GiB。§11.13 F |
| 韧性 | 超时降级不炸会话 | `timeoutMs=30000` 生效且降级 | **34/34 PASS ✅**（真实 HTTP 服务复现两种故障：ECONNREFUSED 9/9 + 挂起 18/18 + 配置 7/7）。**判据表述须改写**：30000 只是 opencode harness 的连接预算 override，**自动召回 hook 主动压到 5 s/腿**；召回阶梯 3 stage（`search`→`recall`→`find`×2 并发）**各自 5 s、无共享 deadline**，实测最坏 **15.0 s**、理论上界 **20 s < 30 s**。无定时器泄漏。§11.13 C.3 / D |

| 隔离 | 跨用户越权成功次数 | **0**（硬性） | **0 ✅**（P5-01 **12 条路径全部 403**：REST `content/read`×2 / `fs/ls`×3（含 percent-encoded）/ `search`+`target_uri` / `find`+`target_uri`，MCP `list`/`tree`/`grep`/`glob`/`read(uris[])`；对照组 A→A 全 200。§11.15 A）。**但**计数类端点 account 级聚合，见 R19（P5-06 第 4 次实证：ADMIN 与 USER 的 `stats/memories` 逐字符相同） |
| 隔离 | 真 Key 进容器次数 | **0**（硬性） | **0 ✅**（P5-05 **净测**：ROOT OV key / `AGENT_SECRET_KEY` / 外域用户 key 三者在容器 A 的 env 与 9 个目录 fs 中命中数**均为 0**；容器内只有 3 个凭据形状变量，全是代理令牌或 opencode BasicAuth 口令。首轮的 `env_hits=1` 已查明为探针自伤假阳性。§11.15 D） |
| 隔离 | **令牌伪造 / 头部提权被拒率（新增）** | **100%** | **100% ✅**（P5-02：7 种伪造令牌 + query-string 传参 + 双凭据头冲突 → **全 401**；P5-03：6 种伪造 identity 头 + 5 种"第二个头塞真外域 key" → **全 403**。§11.15 B/C） |
| 隔离 | **管理面经代理不可达（新增）** | **100%** | **100% ✅**（P5-04：8 条 admin/debug/stats/forget/write 路径含 `..` 穿越 → 全 404；console/metrics/openapi/docs → FastAPI 层 404；4 个未开放 MCP 工具 → JSON-RPC `-32601`。⚠️ 但**网络层未隔离**，容器可直连 `openviking:1933` ⇒ **R13**（Phase 5 已复核并给出决策输入）。§11.15 E） |
| 兼容 | 插件构建门禁（6 项断言） | **6/6 绿**（不绿则镜像构建失败） | **6/6 绿 ✅**（agent 镜像构建 EXIT=0，§11.11）。P3-G 改 `ovcli.conf` 后**复跑仍 6/6 绿**，新镜像 `sha256:93d984254d3e`（§11.12.10 G） |
| 兼容 | 既有 MCP/插件回归 | 全部通过 | **全部通过 ✅**（P5-10：容器内 3 MCP + 3 插件并存，全部 `enabled=true`；裸名 `search` 重叠但**前缀后零冲突** `prefixed_overlap=[]`；web_search stdio 与 openviking 双真握手成功，`searxng healthz=200`；node 动态 import 三插件形状互不干扰。§11.16 B） |
| 兼容 | **版本三方握手（新增）** | 三方一致且可断言 | **握手成功 ✅ 但判据须改口径**：opencode **1.18.25** × 插件 **2026.9.25-2**（commit `3e02c6ac`）× 镜像 `sha256:569193ef…`。⚠️ 服务端**两个面版本不同源** —— REST `/ov/health` = **`v0.4.21`**，MCP `serverInfo` = **`1.27.0`**。§11.16 A |
| 兼容 | **用户开关可逆性（新增）** | hide/unhide 完全对称 | **openviking 完全可逆 ✅ / 白名单形 MCP 不可逆 ❌**：`openviking_*` allow→deny→allow、`orchestrator.mcps` `["*"]`→`["*","!openviking"]`→`["*"]` 全对称；但 `librarian.mcps=["web_search"]` 隐藏后变 `[]`，**取消隐藏仍是 `[]`** ⇒ **R27**（容器 recreate 自愈）。Layer 1 九用例 + Layer 2 六步 `push_ok=true` 全绿。§11.16 C |
| 兼容 | **只读 rootfs 下插件写入路径归属（新增）** | 全部落 volume | **1/5 落 volume ❌**：`ReadonlyRootfs=true`，WRITE_DENY 覆盖 `/opt/agent` 全部 ✅；但插件 5 条写入路径中**只有 logger 落 `/data/state/openviking/`**，其余 4 条（ws-identity / **pending 离线补投队列** / recall state / debug log）全落 **tmpfs `/home/agent`**，容器重启即丢 ⇒ 需 3 个 env 重定位（已验证有效）。§11.16 F |
| 韧性 | **配置越界钳制（新增）** | 钳制而非报错 | **33/33 全钳制、零抛错 ✅**（`recallLimit 999→50`、`scoreThreshold nan→0.35`、`timeoutMs 999999999→300000` …，层序 env > ws-file）。⚠️ **唯一例外 `authMode`**：`bogus→""` 且 **`API_KEY→""`**（大小写敏感）⇒ 静默丢掉平台钉死的 `api_key`。§11.16 G |
| 范围 | **多模态（新增）** | 显式记录 in/out-of-scope | **写侧 out-of-scope（已定案）**：REST 10 条非文本端点**全 404**；MCP 15 工具 schema 中多模态关键字**仅 1 处** `add_resource.temp_file_id`；所有写入工具载荷均 `string`/`array<object>` ⇒ **平台自动写入面纯文本**。**读侧有能力**：`read` 描述含「Raster images and supported audio return native MCP content blocks.」。§11.16 H |
| 隔离 | **派生 Key 一致性（新增，P5-18）** | 注册回读逐字符相等 | **全等 ✅**：4 新用户（`p5gc-a/b`、`p5gd-a/b`）注册 + realign 回读全 equal（len 115）；R11 失败回落机制直测生效；容器 env 持有的是 proxy token（解码 `ovproxy:p2probe`，len 100）而非 OV key —— 真实派生 key 永不出 backend。§11.17 A |
| 隔离 | **计数端点口径（新增，P5-19）** | 计数端点不得作为隔离证据 | **坐实 ✅**：三用户（含 0 记忆新用户）stats/memories 逐字符相同（651 条 account 级聚合）；vector/count=837。隔离证据改用内容面（user scope 跨读 403）。⚠️ ROOT key 对租户数据面全 403 ⇒ 取证须用 user/admin key。§11.17 B |
| 隔离 | **agent 可达面删除通道（新增，P5-20）** | 0 条 | **0 ✅**：REST 白名单 8 条无 DELETE、MCP 白名单无 forget/cancel_watch（-32601）、容器无真实 key（三重防线）；服务端 forget 与 `DELETE /api/v1/fs` 用户隔离实测全绿。**产品决策：不放开 forget**。§11.17 C |
| 隔离 | **共享作用域写入（新增，P5-21）** | 平台写入 0 条落共享作用域 | **平台自动写入 ✅ 全落 user scope；但 agent/user 可写共享作用域 ❌**：write 写 `viking://resources/*` + `viking://agent/*` 全 200（含 A 容器经 proxy 真实通道），另一用户搜到（0.78）+ 读全文 + ls ⇒ 跨用户注入通道完整 ⇒ **R29（高危）**；user scope 跨读/写/删全 403 ✅。§11.17 D |
| 韧性 | 服务宕机时会话可用性 | 100% | **100% ✅（2/2）**（P5-15：`docker stop` OV 后降级期两轮完整会话全部 OK（5.70s / 3.88s）；代理快速失败 502（1.09s / 0.083s）不挂起；容器日志 15 分钟窗口 0 条 openviking 行 = 静默失效；恢复后 0.029s 回绿。§11.18 C） |
| 韧性 | **容器重建后记忆存续（新增，P5-07）** | 记忆完整 | **完整 ✅**：`docker rm -f` → 第三次 `ensure_container` 拉起新容器（identity 变化、命名卷保留），直读 + agent 通道 MCP read 中文全文，重建前后 tree **12 叶逐项全等**。§11.18 A |
| 韧性 | **Key 轮换触发重建（新增，P5-08）** | stale→重建→新令牌生效，记忆不丢 | **全链 ✅**：stale 四证据全中 → R12 重铸（key 与派生逐字符一致）→ 容器自动重建 → 轮回场景端到端自愈（agent MCP read 200 in 0.094s）→ 终态 `.env` 字节级还原、tree 17 叶全等。§11.18 B |
| 韧性 | **跨分钟套件须带存活门禁（新增工装约束）** | 每段有 `VALID=` 标记 | **本组 3 段被 R25 作废后重跑**（栈级重启 `05:41` / `05:44` / `05:53` / `06:08` 全落在调用间隙，表象是 **503 而非 403**）。加门禁后重跑三段全 `VALID=yes`。§11.15 G / R25 |
| 编码 | **测试输出的中文乱码归因（新增）** | 非服务端缺陷 | **已定性 ✅**：落盘字节是正确 UTF-8（`\xe6\x97\xa5\xe5\xbf\x97`=日志），乱码 `鏃ュ織…` 只在按 **GBK** 解码时出现 ⇒ **PowerShell 管道 codepage 936 的显示层产物**，服务端存取无误。§11.15 H |

**任一硬性项（越权 0、真 Key 0、编码 100%、构建门禁 6/6）不达标即整体不通过**，其余指标不达标则记录为待优化项并给出数据。

> **当前状态**：四个硬性项**全部达标**（其中「越权 0」与「真 Key 0」已由 Phase 5 Group A 在**新镜像 + 真实双用户**上净测复核，不再只依赖 Phase 2 的旧镜像结论，见 §11.15）。未达标的量化项有三个，**均已查明不是检索/中文能力问题**：
> 1. **G4 Precision@5 = 0.633** —— 阈值定义缺陷 + 小样本 recall 天花板；同一批数据下 `precision@returned` 滤除脚手架后为 **0.792**。建议把验收口径从 `precision@5` 改为 `precision@returned`（§11.12.5），评测集扩到 G4 ≥ 30 条后复核。
> 2. **8 类分类准确率 = 55.6%** —— 判据失真：抽取器只产出 4 个类目，且 mis-filing 不影响召回（§11.12.8）。建议把验收口径改为"marker resolved 率 + 落点召回可用性"。
> 3. **`remember` 抽取落盘 35.8 s > 30 s** —— 异步路径，不阻塞会话；建议判据拆分（§11.12.6）。
>
> **中文专项结论：内容层面已达标**（正文 CJK ratio 0.589–1.000、文件名中文 slug、召回/同义改写全绿），**残留英文只在 `.overview.md` / `.abstract.md` 结构性脚手架**，且**不存在任何受支持的配置通道可改**（`output_language_override` 是伪杠杆，`TZ` 也修不了日期分桶）⇒ **决定不改 `ov.conf`、不加 `TZ`**，改由插件侧把脚手架当噪声滤除（与 R15 同一措施）。完整定案见 §11.12.9。
>
> **Phase 5 Group B 追加结论（2026-09-29）**：兼容性/韧性面 **8 项全 PASS**，但**不影响上面四个硬性项**，同时暴露 **2 个平台侧真实缺陷** —— **R27（中危，功能损伤）** 白名单形 preset `mcps` 取消隐藏不可逆（librarian 会永久丢掉 `web_search`，容器 recreate 可自愈）、**R28（低危）** permission 键只增不减 + `web_search*` / `web_search_*` 键名分裂。另有 **2 处判据须改口径**：① P5-14 的 `writePathAsync` 是**死旋钮**（全包无读取点），真实判据应改为"逐条枚举写入路径的 volume/tmpfs 归属"—— 实测 **5 条路径只有 1 条落 volume**，其余 4 条（含**离线补投队列**）落 tmpfs，重启即丢，需 3 个 env 重定位；② P5-16 的越界钳制 **33/33 全绿**，但 `authMode` 是**唯一例外**（`bogus→""`、`API_KEY→""` ⇒ 静默丢掉平台钉死的 `api_key`）。详见 §11.16。
>
> **Phase 5 Group C 追加结论（2026-09-29）**：P5-18/19/20 三项 PASS、**P5-21 FAIL ⇒ R29（高危，跨用户注入通道）** —— 该项直接关联硬性项「越权 0」的口径：**user scope 的读/写/删三面 403 全绿（越权仍为 0），但 `viking://resources` / `viking://agent` 两个共享作用域对全部 user 读写开放，agent 经 proxy `write` 即可注入，且注入内容会被其他用户的召回命中（score 0.78 ≫ 0.35 阈值）** —— 属"合法通道被滥用"而非"越权"，须按 R29 处置（proxy 写类工具加 `viking://user/{caller_uid}/` 前缀守卫）。另有 1 处判据修正：P5-20 的"REST 无删除端点"表述已实测推翻（16 条 DELETE 路径存在），agent 可达面无删除仍成立；**产品决策定案：不放开 `forget`**。详见 §11.17。


---

## 7. 风险登记

| ID | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| **R1** | ~~`~/.openviking/ov.conf` 含**明文** root_api_key、embedding api_key、vlm api_key（权限 600）~~ **root key 已轮换结案；embedding/vlm 待用户在 DashScope 侧自行轮换** | 密钥泄漏 | Phase 0 迁移配置进仓库 + 全部改走 `.env`；`.env` 确认在 `.gitignore`（P1-07 实测通过）。**root key 已于收尾轮换（2026-09-29，§11.19 D）**：根 `.env` 一处替换 → compose 双容器 recreate → 新 key admin 200 / 旧 key 401（双形式）、backend 进程内 `is_new=True`、`/ready` 200 全验证。**剩余（用户手动）**：`OV_EMBEDDING_API_KEY` / `OV_VLM_API_KEY` 为外部 DashScope 凭据，平台无法代轮换 |
| **R2** | ~~`_resolve_placeholders` 无法每用户差异化~~ **已消解** | — | 已定案为在 `build_container_config`（已有 `user_id` 形参）内盖章 header，见 §4.6；Phase 5 P5-16 验证 |
| **R3** | ~~rerank 未配置~~ **已结案：不是缺口，配了反而更糟** | ~~中文检索精度可能不达标~~ 实测**若配置则精度与延迟双双崩**：`recall@5 0.892→0.517`、`mrr 0.933→0.578`、`p50 136→3075ms（22.6×）`、`0 PASS / 7 FAIL`；成本 ~9.9 次 rerank 调用 + ~3300 DashScope tokens / 查询 | **决策：永久关闭**。`.env` 里 `OV_RERANK_API_KEY` 注释掉（值原地保留以便复现），compose 注释块写入实测依据与复现警告。前置疑问"v0.4.21 是否支持该配置项"**已确认支持**：`RerankConfig` 挂 `ov.conf` 顶层 `rerank` 键，`extra:"forbid"`；`/ready` 的 `checks` 里**永远不含 rerank**，唯一观测面是 `/metrics` 的 `openviking_model_usage_available{model_type="rerank"}`。**注意旋钮陷阱**：`if self.api_key: return "cohere"` ⇒ 只有"key 为空"是安全 off 态。详见 §4.7、§11.12.3 |
| **R4** | ~~MCP over streamable-HTTP 的 SSE 经代理透传 → 长响应截断/超时~~ **已消解** | — | P2-09 实测通过：145312 UTF-8 字节 / 51702 CJK 字符往返 `identical=True`，代理开销 ~14ms。附带钉死传输形态：streamable HTTP **POST**，无 `mcp-session-id`（无状态），裸 `GET /ov/mcp` 会挂死 |
| **R5** | ~~双实例争抢数据卷~~ **已消解** | — | 旧 WSL 容器已停；P1-05（卷重建后数据完好）+ P1-06（`docker restart` 后 11s 恢复、无损坏日志）实测通过 |
| **R6** | 上游版本演进（服务端按 digest 钉、插件按日期版本号钉，两者互不同步） | 升级破坏兼容 | 服务端钉 `sha256:569193ef…`（registry `latest` 已前移到 `632d24fd…`，**故意不跟**）；插件钉 `ARG OPENVIKING_PLUGIN_VERSION=2026.9.25-2`；构建门禁把"knob 名被上游改名"这类静默失效变成构建失败；P5-09 三方握手纳入 CI |
| **R7** | 中文评测集自建工作量 | 进度 | 先做 60 条最小集（G1/G2/G4 各 20）跑通链路，再扩到全量 |
| **R8** | embedding 为 `input: "text"` | 多模态记忆不可用 | P5-17 显式确认范围，必要时声明 out-of-scope |
| **R9** | 记忆服务成为所有会话的同步依赖 | 单点故障拖垮全部 agent | **P5-15 已实测（2026-09-29）：判据满足** —— `docker stop` OV 后降级期 **2/2 轮完整会话全部 OK**（5.70s / 3.88s），代理快速失败（`/ov/health` 502 in 1.09s、MCP 502 in 0.083s，均带中文文案），容器日志 15 分钟窗口 0 条 openviking 行（静默失效不阻塞），恢复后 0.029s 回绿。另：`timeoutMs` 兜底与插件"越界钳制而非拒绝"设计哲学见 §11.13 C.3/D。§11.18 C |
| **R10** | 共享单实例的容量上限 | 用户数增长后延迟劣化 | P4-09 记录规模-延迟曲线，给出扩容触发线 |
| **R11** | §4.3.2 的本地重算依赖上游 `derive_seeded_api_key_secret` 的实现细节（sha256 + `\0` 分隔、urlsafe b64 去 padding） | 上游改动即全体 Key 失效 | 不硬编码信任：`ensure_user_registered` 注册后**回读服务端返回的 `user_key` 并与本地重算值比对**，不一致则记 ERROR 并回落到"存服务端返回值"的路径；P5-18 专项断言 |
| **R12** | 轮换 `AGENT_SECRET_KEY` 会同时失效代理令牌与全部 User Key seed | 全体容器需重建 | **P5-08 已实测（2026-09-29）：runbook 有效 + 轮回自愈链成立** —— 轮换 → stale 四证据全中（旧 token 不可解 / `ov_env_stale=true` / seed 已变 / 新派生 key 直测 401）→ `POST .../users/{uid}/key` 携新 seed 重铸（返回 key 与本地派生逐字符一致，read 200）→ 容器**自动**重建（新 token/env 落位）；**轮回场景**（rotate→restore 后旧 K1 再度 stale）同样端到端自愈：直测 401 → 自动重建 → agent MCP read 200 in 0.094s → K1 恢复 200。与 `crypto.py` 既有约束同类。§11.18 B |
| **R13** | **（P2-06 实测；Phase 5 Group A 复核）** agent 容器可直连 `http://openviking:1933`，`/ov` 代理**不是**唯一网络路径 | 无 key 即可读 `/metrics`（~132 KB，含 embedding 模型名/调用计数）与 `/openapi.json`（~311 KB，完整 API 全貌）+ `/docs` `/redoc` `/studio/` `/health` `/ready`；并多出 DoS 面。**无租户数据泄漏** —— 一切数据面端点对无 key 请求返回 401 | **Phase 5 复核（新镜像 `93d984254d3e` + 真实双用户）**：A 容器**不带任何 key** 直连 `openviking:1933` → `/health` **200**、`/api/v1/system/status` **401**、`/api/v1/admin/accounts` **401**，与 P2-06 结论一致；且 Group A 的 12 条越权路径 + 11 组伪造凭据**没有一条**能借直连绕过代理拿到 B 的内容。**⇒ 结论收窄为：`ov_proxy` 的 `_ALLOWED_REST` 白名单不是网络边界，`ov_proxy.py:27-29` 宣称的 admin/console/debug「unreachable from a container by construction, not by a deny-list」只在「经代理」这一条路径上成立**；真实防线只有一条 —— **容器内不存在有效 OV key**（P5-05 已净测成立：ROOT / `AGENT_SECRET_KEY` / 外域 user key 在 env 与 9 个目录 fs 中命中数均为 0）。**决策输入（原"三选一"现已可判）**：①把 openviking 移到只与 backend 相连的独立网络 —— **推荐**，代价最小（compose 改 network 归属即可，agent 容器本就只经 `backend:8000/ov` 访问），且能一并消掉 `/metrics`、`/openapi.json`、`/docs` 的信息泄漏面与 DoS 面；②网络策略仅放行 backend IP —— 同等效果但引入 iptables/网络插件依赖，WSL2 下可维护性差；③接受现状并写进威胁模型 —— 仅当"容器内无 key"被当作**唯一且足够**的防线时才可选，须同时接受"任一容器被 RCE 后即可探测 OV 全部 API 面"。compose 注释已改为陈述事实而非宣称 "exclusively"。§11.15 E |
| **R14** | **（P2-05 实测）** `forget` / `cancel_watch` 被 MCP `tools/list` 广告出去，却在 `tools/call` 时被代理白名单拒（`-32601`） | agent 看见工具却调不动，浪费一轮往返并可能反复重试 | 在 `ov_proxy` 的 `tools/list` 响应里同样按 `_ALLOWED_TOOLS` 过滤，而不是只在 `tools/call` 拦。同时删掉 `_ALLOWED_TOOLS` 里的 `add_skill`（上游 15 个工具中**不存在**该项，是陈旧配置）。**Phase 5 P5-04 已 dump 全部 `inputSchema` 坐实**：实测 `tools/list` 恰 15 个（`add_resource` `cancel_watch` `edit` `find` `forget` `glob` `grep` `health` `list` `list_watches` `read` `remember` `search` `tree` `write`），**无 `add_skill`**；且 `forget` / `cancel_watch` 确实在列表里却在 `tools/call` 被拒（`-32601`）。§11.15 E |
| **R15** | **（实测分数分布）** 插件默认 `scoreThreshold: 0.35` 与噪声底（无关脚手架 `.abstract.md` 得 0.25–0.33）**余量仅 0.02**；更糟的是**同用户作用域下的 `.overview.md` 脚手架实测得 0.61**，远高于阈值 | **量化后已改判**：脚手架占 top-5 槽位 **25.3%**（OFF 态）/ 28.0%（ON 态），但**滤除它对 `precision@5` 的 delta 恒为 +0.000**（所有分组）——因为脚手架从来不是 expected 文档，滤掉只是留空槽，而 `precision@5` 分母钉死在 `limit`。真实损失是 **token 预算：平均每查询少 1.27 个有效槽（5.00→3.73，−25%）被噪声占掉** | **不要调 `scoreThreshold`**（0.61 与真实命中 0.63–0.89 重叠，无可用间隔）。**正确做法是在插件侧按 URI 过滤 `.abstract.md` / `.overview.md`**，收益是 token 预算而非精度。**已实测排除的错误做法**：超量拉取补位（`--overfetch 3/5`）——`nofilt-of3` 与 baseline 指标逐项相同，`filt-of3/of5` 的 G4 p@5 仍精确等于 0.633，深度 6–8 补进来的文档同样不是 expected。数据见 §11.12.5 |
| **R16** | **（实测作用域语义）** `viking://agent` 与 `viking://resources` 是 **account 级共享**，同账户任何 user key 都能 `ls` + `read`；且写入 `viking://agent` 的内容**默认 search 搜不到** | 平台若把自动写入落到这两处，会同时造成**跨租户可见**与**召回失败** | 写入路径铁律：一律落 `viking://user/{uid}/`。Phase 5 增一条专项断言"平台写入路径不落 `viking://agent` / `viking://resources`"。详见 §11.8。**Phase 5 P5-21 实测更新**：本条的「任何 user key 都能 ls + read」**升级为「读写都开放」** —— user key 写 `viking://resources/*` / `viking://agent/*` 均 200，agent 经 proxy `write` 同样可写；且「写入 `viking://agent` 的内容默认 search 搜不到」**被推翻** —— 注入内容不仅可被写入者搜到（0.78），**同账户其他用户也能搜到 + 读全文** ⇒ 跨用户注入通道，立 **R29**。§11.17 D |
| **R17** | **（Phase 0 收尾实测；严重度已下调）** 镜像重建后，**运行中**的 agent 容器不会自动 recreate：`ensure_container` 的 running fast path（`container_manager.py:715-740`）只查 `_fastk_env_stale` / `_ov_env_stale`，`_image_stale` 仅在停止路径（`:753` 经 `_needs_recreate`）生效。**已停止**的容器相反 —— `_needs_recreate()` 第一项就是 `_image_stale()`，下次启动即自动迁移 | 只对"重建当时正在跑"的容器成立：它们会一直在旧 rootfs 上跑，OpenViking 插件根本不在容器里、`opencode.json` 也无 `mcp.openviking`，表现为"记忆功能静默不存在"，且 `config_ok=True` 会掩盖它。**当前实测：6 个用户容器全部 `exited`，`_needs_recreate` 一律返回 `True / stale image (want agent-demo:1.4.0)` ⇒ 无待办，下次启动自愈**；自愈路径已用一个真实用户容器实跑验证 | 设计使然（避免打断会话），非 bug。运维手册：每次 `scripts/build-agent.sh` 之后，只需对**当时正在运行**的容器执行 admin "update image"（`agent_controller.recreate_for_user`）；已停止的不必处理，也不应为了"刷新"去主动 start/stop 它们。详见 §11.11 发现 A |
| **R18** | **（P3-13 实测 + P3-C 根因定位；机制已修正）** `THINKING` 检索模式存在**目录级盲区**：它按 URI 层级下降，靠给每个目录的 `.overview.md` 打分决定要不要进这个子树。**真因不是写入通道，而是 registry / `overview_template` 守卫**（`memory_updater.py:1570-1572`）：`memory_type_from_uri()`（`:845-853`，纯词法取 `memories/` 的下一段）→ `registry.get(memory_type)` → `if not schema or not schema.overview_template: return False`。live 默认 registry **只注册了 9 个 memory type**，其中**只有 4 个带 `overview_template`**：`preferences`(324B) / `entities`(249B) / `cases`(156B) / `events`(144B)；`soul` `trajectories` `identity` `experiences` `profile` 注册了但**无模板**；`tools.yaml` `skills.yaml` **磁盘上存在却未注册**；`patterns` 连 YAML 都没有。2×2 实测：MCP `write` 落 `cases` → `overview=complete`，落 `tools` → `overview=skipped` ⇒ **与写入通道无关，只与类目是否在 registry 里有模板有关** | 本评测集 8 个分类里 **`tools` / `patterns` / `skills` / `profile` 四类整棵子树消失**，30 条查询中 **10 条**（Q05 Q06 Q13 Q14 Q15 Q19 Q21 Q22 Q23 Q24）失去唯一命中。**这不是 rerank 分数不准，是检索器根本不下探**。第二独立机制：THINKING 下默认 `score_threshold` 会截断结果（rerank 把分数压到 0.08–0.18 ⇒ 默认 `total=1`，显式传 `score_threshold:0` 才 `total=5`）。**关键量化：R18 与 R3 是同一个杠杆** —— rerank OFF 下未注册类目照样 #1 命中（`tools/probe-tools.md` `0.8567`，**无需 `target_uri`**）⇒ 盲区**只在 THINKING 下显现**，这是 rerank 永久关闭的第二个独立理由 | ①**不要开 rerank**（等于不要 THINKING）——已按此结案；②`target_uri` 定向**不再是可靠兜底**：P3-C 实测当分类器根本不产出该类目时它是**负收益**（`memories/skills` → `hits=0`；`memories/tools` → #1 `0.3023` 指向错误文档且低于 `scoreThreshold`；`memories/profile`/`identity` → `hits=0`，因为 `profile` `identity` `soul` 是根下**单文件**而非目录）。Q05 那个 `0.8967` 是评测集**人为灌进 `tools/`** 才成立的，真实 `remember` 路径不会产出该类目；③**已实测无效的对策**：`content/reindex`（`mode:"vectors_only"`，不生成 overview，跑完仍 `total=3`）、`level=2`（更糟，`total=0`）、`node_limit=40`（无变化）、`mode=thinking\|quick` / `query_expansion=off` / `peer_scope=user` / `quotas`（全 HTTP 400，`SearchRequest.mode` 只接受 `list`/`context`，那是**响应形状**不是检索模式）、`content/overview` GET（返回 `[Directory overview is not ready]`，**不触发生成**）；④account 级模板覆盖**救不了**：`overview_template` 属 locked 字段（见 R22），且 `cases` `trajectories` `experiences` 根本不在 `EDITABLE_MEMORY_TEMPLATE_FIELDS` 里。另注：`MAX_PARALLEL_CHILD_SEARCHES=4` 而分类有 8 个，是 THINKING 的第二个架构上限。详见 §11.12.4 / §11.12.8 |
| **R19** | **（Phase 3 实测）** `/api/v1/stats/memories` 与 `/api/v1/debug/vector/count` 是 **account 级聚合，不按 user 隔离**：灌入 5 条 preferences 后统计显示 `preferences=8`（5 新 + 3 来自另一探针用户 `p2probe`） | **泄漏计数，不泄漏内容**（内容面 403 强隔离仍成立，见 §11.8）。但足以推断"同账户还有别的用户在用"及其活动量级，属侧信道 | Phase 5 P5-01 增一条专项断言：跨用户断言**不能**用计数类端点做证据，必须用内容面端点。**原兜底方案「自己按 `viking://user/{uid}` 前缀 `ls` 后计数」已被证伪**（见 R21：根级 recursive `fs/ls` 有深度截断），per-user 计量只能用 MCP `glob`（`MEM/events/**/*` → 5 files ✓）或 MCP `tree`（`level_limit=6` → 22 entries ✓），或逐子目录 `fs/ls`。§6 的"越权成功次数 0"判定须据此收窄口径 |
| **R20** | **（Phase 3 实测）** **REST 面没有任何删除端点**：`openapi.json` 的 126 个 path 里无 delete/forget/remove 语义项（8 个候选路径逐一 404 验证过）。唯一删除通道是 **MCP `forget`** | 清理测试残留、用户行使"删除我的记忆"（合规诉求）都**只能走 MCP**；而 `ov_proxy._ALLOWED_TOOLS` 当前把 `forget` 拦在外面（R14）⇒ **平台目前无法代用户删除记忆** | ①测试清理直接打 `http://127.0.0.1:1933/mcp`（须带 dual `Accept`，见 §11.10）；②**产品侧决策项**：若要支持用户自助删除，必须把 `forget` 加进 `_ALLOWED_TOOLS` 白名单，并配套作用域校验（`forget` 只接受 `uri`，`recursive` 可选 ⇒ 一旦放开，越权删除是唯一防线，必须验 `viking://user/{uid}` 前缀）。实测删除一条后 `total_memories 44→43`、`cases 6→5`、`vector/count 141→140`，确认生效。**Phase 5 P5-20 实测更新**：原判「REST 面没有任何删除端点」**被 openapi.json 实测推翻**（126 paths 实有 **16 条 DELETE**，含 `/api/v1/fs`、`/api/v1/sessions/{id}`、`/api/v1/skills/{name}`、admin 面、`/webdav/resources`）；但 agent 可达面无删除仍成立（`_ALLOWED_REST` 无 DELETE + `_ALLOWED_TOOLS` 无 forget + 容器无真实 key，三重防线），服务端隔离实测全绿（跨用户/递归 forget 拒、DELETE fs 跨用户 403、own 生效）。**产品决策定案：不放开 forget**（R26 归一化是前置）。详见 §11.17 C |
| **R21** | **（P3-C 实测，两个此前未知的服务端契约缺陷）** **① URI 形态不一致**：真实规范路径是嵌套的 `events/2026/09/28/星槎计划启动会.md`（`fs/ls` / `tree` / `glob` / `content/read` 都用这个），而 `search` 与 `grep` 返回的是**折叠形式** `events/2026-09-28/星槎计划启动会.md`；拿折叠形式打 REST `content/read` → **404 `NOT_FOUND: File not found`**，MCP `read` 两种都接受（容错）。**② 根级 recursive `fs/ls` 深度截断（约 3 段），所有 limit 旋钮无效**：`fs/ls MEM?recursive=true&show_all_hidden=true` 恒返回 **24 nodes**，漏掉 `events/2026/09/28` 目录及其 3 个文件；叠加 `node_limit=5000` / `abs_limit=5000` / `limit=500` **均无效**；规律是"从查询根算起，文件只在 ≤3 段深度内出现"（`events/2026/09/28/x.md` 是第 4 段 ⇒ 被丢）。从 `MEM/events/2026` 起查则 5 nodes 全在 | **①** `ov_proxy._ALLOWED_REST` 同时放行了 `search/search` 与 `content/read`，但**容器内"search 拿 URI → REST 读正文"这条链对 `events` 记忆是断的**，只有走 MCP `read` 才通 ⇒ 平台侧任何自动召回后读正文的实现都必须走 MCP，或自己做 URI 反折叠。**②** 任何"列举用户全部记忆再计数/遍历"的运维与测试脚本都会**静默少数**，且不会报错 | ①读正文一律走 MCP `read`；若必须走 REST，先把折叠形式按 `events/YYYY-MM-DD/` → `events/YYYY/MM/DD/` 反展开（只对 `events` 类目成立，属脆弱耦合，**建议直接禁用这条链**）。②列举一律用 MCP `glob` / `tree`，或逐子目录 `fs/ls`；**不要**用根级 recursive `fs/ls` 做任何完整性断言。`fs/ls` 正确契约见 §11.12.7 |
| **R22** | **（P3-C 源码定位，两处上游缺陷，我方无受支持的修法）** **① `overview_template` 是 locked 字段**：`account_templates._apply_editable_values` 对非白名单字段 `raise ValueError(f"{location} is locked and must match the default template")`，`_complete_template` 的 `editable = {"description"}`；`EDITABLE_MEMORY_TEMPLATE_FIELDS` 只含 6 类（`profile` `events` `preferences` `entities` `soul` `identity`），且可编辑项**全是字段 description**，`cases` `trajectories` `experiences` **完全不可 account 级覆盖**。另：根 `memories/.overview.md` 的英文来自 **`core/directories.py:62` 的 Python 字符串字面量**，既不是模板也不是 prompt。**② `cases.yaml:51` 是四个 overview 模板里唯一没有 `|default(...)` 的**（其余三个都有 `item.file_content.X|default(item.file_name, true)`），任何缺 `case_name`/`task_signature` front-matter 的 cases 文件会让 `.overview.md` **原样吐出 `{{ no such element: dict object['case_name'] }}`**（P3-C X4 已实测到此垃圾） | **①** 中文专项**无法**把分类 overview 的英文标题（`# Preferences Overview` 等，全是 YAML 英文字面量）改成中文；渲染上下文里**根本没有 `language` 变量**（`overview_context` 只有 `memory_type` / `directory_name` / `items`），且 `grep output_language memory_updater.py` → **零命中**。**②** 那段 Jinja 垃圾随后成为 **THINKING 模式的目录打分信号**，属**主动投毒**，而 `cases` 又不可 account 级覆盖 ⇒ 无法绕过 | ①**放弃**改 overview 标题语言，改为在插件侧把 `.overview.md` / `.abstract.md` 当脚手架滤除（与 R15 同一措施，一石二鸟）。②只能等上游修；短期规避是**不要经 MCP `write` 直接往 `cases/` 落无 front-matter 的文件**，让 `cases` 只由抽取路径生成。③**一个尚未实测的正向杠杆**：`EDITABLE_MEMORY_TEMPLATE_FIELDS` 里的字段 description **是抽取 prompt 的一部分且 account 级可改**（`PUT /api/v1/admin/accounts/{account_id}/memory-templates/{memory_type}`）⇒ 可用来强化中文抽取指令（例如约束 `preferences.topic` 必须是简洁中文名词短语）。详见 §11.12.9 |
| **R23** | **（P3-C 实测，中文专项）** **`events` 记忆按 UTC 日历日分桶**，且**无法通过 `TZ` 修正**。机制链已完整验证：`session.py:1395` `created_at = spec.get("created_at") or datetime.now(timezone.utc).isoformat()`（**UTC，TZ 无关**）→ `MessageRange._first_message_time()`（`memory_updater.py:682-689`）`parse_iso_datetime(msg.created_at).strftime("%Y-%m-%d")`（**保留字符串自带的偏移，不做任何本地化转换**）→ `get_year/get_month/get_day` 对该日期串做 `.split("-")[0/1/2]`。`grep astimezone|localtime|tzset|ZoneInfo` 扫遍 `session/` `utils/` `storage/` **无任何本地化转换**。实测同一瞬间 `TZ=UTC` → `events/2026-09-28` + `# 2026-09-28 (Monday)`，`TZ=Asia/Shanghai` → `events/2026-09-29` + `# 2026-09-29 (Tuesday)`，**但那只是 naive `datetime.now()` fallback 分支被翻转**，真实路径走的是 UTC `created_at` | 北京时间 **00:00–08:00** 产生的 `events` 记忆落进**前一天**的桶，日期与星期**同时错**。我们自己的 P3-C 探针数据就是受害者：`remember` 全部跑在 `18:0x–18:2x UTC` = 北京时间 `09-29` 凌晨 `02:0x`，却落进 `events/2026-09-28/`。**唯一受支持的杠杆是 caller 传 `created_at`**（`AddMessageRequest.created_at: Optional[str]` 存在，`+08:00` 偏移串会被 `_first_message_time` 如实格式化 ⇒ 桶变 `09-29`，而 `format_iso8601` 仍归一化成同一 UTC 瞬间写进向量库，**两者不矛盾**）；但官方插件 `@openviking/opencode-plugin@2026.9.25-2` **从不发送 `created_at`**（全包 `grep created_at` → **零命中**），且走 `/api/v1/sessions/{id}/commit` 批量提交 | **不要在 compose 里加 `TZ=Asia/Shanghai`** —— 已实测推翻：它动不了主路径，只会把 `get_year/get_month/get_day/get_timestamp_from_ranges` 与 ChatLog else 分支的 naive `datetime.now()` fallback 翻成北京时间，**造成主路径 UTC、fallback 本地的双重日期基准**（比现状更糟）。同理 `LANG`/`LC_ALL`/`LANGUAGE`/`TZ` 四类注入对语言的收益也≈0（只影响**空文本**的 fallback，`'ok'` 与 `"print('x')"` 在 fallback 翻成 `zh-CN` 后仍判 `en`）。**正确处置**：①接受为上游限制并记录；②若平台将来自己实现写入 hook（不经官方插件），则在 `POST /api/v1/sessions/{id}/messages` 显式带上 `created_at=<本地 ISO+08:00>`（该路径已在 `ov_proxy._SESSION_PATH` 白名单内）；③**不要**用 `output_language_override`（会硬强制全租户中文，收益仅覆盖空文本 fallback）。详见 §11.12.9 |
| **R24** | **（Phase 4 实测，高优先级）** **embedding 配额耗尽后写入侧无背压**：共享 OV 的 embedding 上游是**计量制**的阿里云 Model Studio / DashScope。P4-09 灌 500 docs 的第 14 秒即撞穿配额 —— 全日志 **99 次 `Error code: 429 insufficient_quota`，零个其他错误码**，`uri` 全部指向 `viking://user/p4-scale/memories/cases`。OV 的处置是打开断路器并 **`re-enqueueing messages`**，而队列落在**命名卷**上（`_system/queue/{queue.db,-shm,-wal}` 共 6.9 MiB、`p4-scale` 残留 **523 个文件**），⇒ **跨容器重启无限重试**。实测两次重启后（04:03）日志仍在 re-enqueue，OV CPU 长期 **99.5%–105.6%** | ① **索引静默滞后**：`content/write` 返回 200 但检索不到 —— cp200 的 `vector_count` 与 cp100 **完全相同（都是 390）**，500 docs 只换来 **+151** 向量（而 `wait=true` 通道是 2 向量/篇，500 篇本应 ≈ +1000）⇒ **P4-09 规模曲线整体失效**。② **重试风暴波及全租户**：CPU 打满期间 `/health` 探针连续 `exit=7`（curl 连不上），故障表象与"隔离失效"高度相似，**极易误诊**。③ 单实例共享 ⇒ 任一用户批量写入即可耗尽**全局**配额 | ① 写入侧限速 / 按 account 分摊配额；② 对 429 设**重试上限 + 死信队列**，而非无限 re-enqueue；③ 把 `insufficient_quota` 与 `Embedding circuit breaker is open` 计入告警；④ 批量导入一律走**独立 account/key**（本轮已遵守：`p4-scale` 等抛弃型用户，`ovzh-eval` 语料保持 pristine）。**运行态已自愈**（配额恢复、积压消化完毕、`vector_count 463→813`、OV CPU 99.5 %→2.07 %、门禁 429/breaker/embed_fail/re-enqueue 四项全 0，`queue.db`/`-wal` mtime 冻在 04:06 不再增长）⇒ **Phase 5 写入类用例可以开跑**；但**代码缺陷一个都没修**，配额再被撞穿会原样复现。⚠️ 原记录里"04:03–04:13 整个栈反复重启"**已改判为 R25**（宿主 WSL 用户态回收），不是重试风暴。重跑 P4-09 的前置条件见 §11.13 H |
| **R25** | **（宿主环境，非 OpenViking 缺陷；Phase 4 期间反复出现，一度被误诊为 R24）** **WSL2 发行版的用户态被反复拆除并重建**，导致 dockerd 被杀、所有 `unless-stopped` 容器同步重启。决定性证据是 `who -b` 与 `/proc/uptime` 相互矛盾：`uptime=97633 s`（1 day 2:57，内核/VM 未重启）而 `who -b` 显示 **30 秒前才"开机"**；`journalctl --list-boots` 的 **BOOT ID 始终未变**（boot_id 是内核属性 ⇒ 内核确实没重启）。`dockerd` PID 在 20 分钟内循环 **305 → 287 → 304 → 303**，但 `systemctl show docker.service -p NRestarts` 恒为 **0**（containerd 同）⇒ **不是 systemd 的 `Restart=` 策略，是整个单元被重新拉起**。journal 头部残留着上一次关机的原因：`systemd-logind: The system will power off now!` + `systemd[1]: Stopping session-279.scope - Session 279 of User zhangzhixiao`，并伴随 WSL 9P/drvfs 层异常 `unknown: Operation canceled @p9io.cpp:258 (AcceptAsync)`（`p9io.cpp` 承载 `/mnt/d`）与 `systemd-journald: Time jumped backwards, rotating`。拆除→重建一轮的实测代价：`13:01:08 exit.target` → `13:02:05 sysinit` → `13:02:24 graphical.target`，其中 dockerd 自身 boot 耗 **18 s**（`12:57:26 Starting up` → `12:57:44 Daemon has completed initialization`），并打出 `error unmounting container … layer not mounted` ×2、`Deleting nftables … No such file or directory`、`Removing stale sandbox` ×2 | **① 测量窗被宿主寿命截断**：任何长于发行版空闲寿命的采样都会跨越一次全栈重启（本轮实测重启间隔 2–3 min 到 14 min 不等，**与命令调用间隙高度相关**），`docker events` 里看不到任何容器级 die/start，因为**是 daemon 整体消失**。② **证据易失**：journal 是 volatile 的（`--list-boots` 恒只有 1 条，`Time jumped backwards, rotating` 会丢历史），`journalctl -u docker --since 04:30 --until 04:45` 返回 **"-- No entries --"** 不是没事件而是**记录已被轮转掉** ⇒ 必须**在飞行中**取证。③ **极易误诊为服务缺陷**：表象与"OV 崩溃循环""healthcheck 杀容器""双 compose config 抖动"高度相似，本轮曾据此得出错误假设（见下方"已排除"）。④ `p9io.cpp` 异常说明 `/mnt/d`（9P 挂载）在拆除时 I/O 被取消 ⇒ **仓库在 `/mnt/d` 上时，宿主回收可能损坏未落盘的写入** | ①**长采样必须持有一个存活的 WSL 会话**（keepalive），或接受"窗口会被截断"并在结论里声明；②每个 Phase 开跑前**重新过一次门禁**而不是复用上一 Phase 的结论（本轮 R24 门禁即因此复跑）；③`journalctl` 取证要**立刻落盘到 `/mnt/d`**，不要指望事后回查；④容器重启后 `RestartCount` 仍为 **0**、`manualRestart=false`、`restartPolicy="{unless-stopped 0}"` ⇒ **不要用 `RestartCount` 判断是否发生过重启**，要用 `State.StartedAt` + `dockerd` 的 `etimes` 交叉验证；⑤根治需宿主侧动作（`%USERPROFILE%\.wslconfig` **不存在** ⇒ 全部走 WSL 默认值；`/etc/wsl.conf` 只有 `[boot] systemd=true` + `[user] default=zhangzhixiao`，**无 `[boot] command=`**），不在本测试范围。**与 OpenViking 无关，不改 compose、不改 ov.conf。****复发记录（Phase 5 Group C/D，第 16–20 次复现）**：p5gc/p5gd 轮 `08:30`/`08:42`、Group D 前的 `09:42–09:47`（每 ~2 分钟整栈重启 5 次、OV 日志伴 `LockAcquisitionError`，09:47:22 后 12/12 轮稳定自愈），以及 p5ge/p5gf/p5gg 三段探针的门禁第 1 轮 `local_ov=000 via_proxy=503` → 第 2 轮 `200/200` —— 存活门禁 + 分段标记持续生效，无一组数据被污染。**收尾清理期第 21–23 次（§11.19 E）**：`10:24:11Z`、`10:25:47Z`（间隔 96s）、~`10:37Z` 三次全栈拉起，撞掉三个无门禁首跑（A0 / A2 / A6.1）；补门禁后重跑全部成功。完整取证见 §11.14 |
| **R26** | **（Phase 5 P5-01 实测，上游缺陷，低危）** **MCP 越权拒绝的 `isError` 形状不一致**：同一类 `Access denied for viking://user/ovzh-eval/...` 错误，`search` / `list` / `tree` / `grep` 返回 **`isError:true`**（正文形如 `"Error executing tool search: Access denied for …"`），而 **`read` / `glob` 返回 `isError:false`**（正文只是裸的 `"Access denied for …"`，外层是正常 `result.content[0].text`） | **不是数据泄漏**（内容确实一个字都没吐），但**会误导 agent 的控制流**：只看 `isError` 判定成败的调用方会把一次越权拒绝当成成功，进而把 `"Access denied for …"` 这段英文当成正文喂进上下文，或触发无意义的重试。对 `read(uris[])` 这种**批量**接口尤其糟 —— 一批 URI 里混一个越权项时，整批会被标为成功 | ①平台侧（若将来自己实现召回 hook）判定 MCP 成败**不能只看 `isError`**，须同时匹配正文里的 `Access denied for` / `PermissionDeniedError` 前缀；②`ov_proxy` 可考虑在 MCP 响应回程做一次归一化（把含 `Access denied` 的 `content[].text` 强制置 `isError:true`），属**一行改动**，与 R14 的 `tools/list` 过滤可以合并到同一个补丁里；③向上游报缺陷。注意 `grep` 的文案还多一层包装（`"grep failed for every pattern:\n  .: PermissionDeniedError: …"`），归一化规则要按前缀而非全文匹配。§11.15 A |
| **R27** | **（Phase 5 P5-11 Layer 2 实测，平台侧真实缺陷，中危 —— 功能损伤）** **白名单形 preset `mcps` 的"取消隐藏"不可逆**。`oh-my-opencode-slim.json` 的 `presets.container` 里，`orchestrator.mcps=["*"]`（allow-all 形）而 **`librarian.mcps=["web_search"]`**（白名单形）。隐藏 `web_search` → `librarian.mcps` 变成 **`[]`**；取消隐藏 → **仍是 `[]`**，librarian 子代理**永久失去 web_search**，且 `permission.web_search_*` 已改回 `allow` ⇒ 表面看一切正常、实际能力已丢。**根因（源码级）**：`opencode_config.py:541-569` `_apply_list_visibility` —— `:563` `elif not item.startswith("!") and item not in hidden: result.append(item)`，白名单项一旦被隐藏就是**从文件里删除**（不是标记）；`:565-568` `if "*" in result: for name in sorted(hidden): result.append(f"!{name}")` —— **只有 allow-all 形才用 `!name` 标记**，所以可逆；白名单形没有留下任何可反推的痕迹。**openviking 自身不受影响**（它在 `orchestrator` 的 allow-all 形里 ⇒ P5-11 六步 `["*"]`→`["*","!openviking"]`→`["*"]` 完全对称），受损的是**被当成白名单项列举的其它 builtin MCP** | ①用户/管理员在 UI 上关掉再打开 `web_search`（或任何被 preset 白名单点名的 MCP），librarian 的能力**回不来**；②`_sync_plugin_config`(:194-221) 是 **read-modify-write**（因为 `PATCH /global/config` 走 mergeDeep，删不掉键），所以**运行态没有任何补救通道**；③故障是**静默**的 —— 无报错、无日志、`permission` 看起来正确。**已证的自愈路径**：`render_plugin_config`(:605-636) 的 `:628` `entry["mcps"] = ["web_search"]` 是**从零重建**，还原脚本实测 `rendered_equals_restored=true` ⇒ **容器 start / recreate 会重渲染并完全自愈**，只有"容器运行中热开关"这一条路径受损 | ①**首选修法（一行）**：`_apply_list_visibility` 对白名单形也走标记语义 —— 被隐藏的白名单项**保留**为 `!name` 而非删除，取消隐藏时剥掉 `!`；需先确认 oh-my-opencode-slim 能解析 `!` 前缀（`orchestrator` 已在用，**大概率可以**）。②**次选（不改上游语义）**：`_sync_plugin_config` 在 hidden 集合缩小时，对受影响的 preset **调用 `render_plugin_config` 重建该 entry** 而不是 read-modify-write。③**规避（立即可用）**：管理员开关 builtin MCP 后，对该用户的容器执行一次 recreate（`agent_controller.recreate_for_user`）；或**不要把非 `*` 的 MCP 名写进 preset 白名单**。④测试纪律：**任何可见性用例都必须跑完整的 hide→unhide→与基线逐字段 diff**（本轮 `restored_to_baseline=false` 就是这么抓到的），只测 hide 一侧会完全漏掉本缺陷。§11.16 C/E |
| **R28** | **（Phase 5 P5-11 Layer 2 实测，平台侧真实缺陷，低危 —— 配置残留 + 键名不一致）** **① permission 键只增不减**：一次 `web_search` hide→unhide 循环后，容器内 `opencode.json` 的 `permission` 从 **14 键变 15 键**，多出 `web_search_*`；取消隐藏只把**值**改回 `allow`，**键不会被删除**（同样因为 `_sync_plugin_config` 是 read-modify-write + mergeDeep 删不掉键）。**② 同名不同形的两条键并存**：宿主基线自带的是 **`web_search*`（无下划线，来自 host `opencode.json` 原样透传）**，而平台 `_sanitize_mcp_permission_key`(:526-534, `re.sub(r"[^a-zA-Z0-9_-]","_",name)`) 产出的是 **`web_search_*`（有下划线）** ⇒ **两条键指向同一个 MCP 却不同名**。实测 sanitize 行为：`{"a b"→"a_b", "a-b"→"a-b", "a.b"→"a_b", "openviking"→"openviking", "web_search"→"web_search", "中文"→"__"}` | ①配置文件随开关次数**单调膨胀**（每个被开关过的 MCP 永久留一个键）；②`web_search*: allow` 对 `web_search_*: deny` **没有任何覆盖作用**（不同键），所以"宿主已放行"与"平台已拒绝"可以同时成立；③一旦将来出现**一 allow 一 deny** 的组合，opencode 的**优先级未定义且未测** ⇒ 可能表现为"平台想关但关不掉"或反之。④`中文→"__"` 说明 sanitize 会把**任何纯非 ASCII 的 MCP 名压成同一个键** ⇒ 多个中文名 MCP 会**互相串权限**（当前无此类 MCP，属潜在） | ①**统一键形**：宿主透传与平台生成必须走**同一个** sanitize 函数，消除 `web_search*` / `web_search_*` 并存（这是**根因**，比删残留键更重要）；②**开关归零时删键**：`_sync_plugin_config` 在某个 MCP 从 hidden 移出且宿主原本没有该键时，应**删掉**这个键而不是留 `allow` —— 若 mergeDeep 删不掉，则整份 `permission` 段**整体替换**（本轮还原脚本就是这么做的，实测 `perm_keys_removed=["web_search_*"]` 后精确回到 14 键基线）；③**补一条断言**：可见性用例结束后 `perm_key_count` 必须**等于基线**（本轮 14→15 即告警）；④给 sanitize 加**非 ASCII 名拒绝或转写规则**，避免 `中文→"__"` 这类碰撞。§11.16 C/D/E |
| **R29** | **（Phase 5 P5-21 实测，高危 —— 跨用户数据注入通道）** **共享作用域 `viking://resources` 与 `viking://agent` 对所有 user key 读写开放**：任一 user key 可 `write` 这两个作用域（200），写后**同账户任何其他用户**都能 `search` 命中（score 0.78）、`content/read` 读到全文、`fs/ls` 列出；且 `write` 在 `ov_proxy._ALLOWED_TOOLS` 白名单内 ⇒ **agent 容器经 `/ov/mcp` 即可完成注入**（实测：A 容器写 `viking://resources/p5gd-proxy-marker.md` 成功，用户 b 搜到 + 读到全文）。另：proxy 的脚手架排除是 **per-node** 的（`_SCAFFOLD_URIS` 只排除 7 个节点本身，不含子节点）⇒ 注入到 `viking://resources/xxx.md` 的内容**会被其他用户的召回链路正常召回** | ①**跨用户数据通道**：用户 X 的 agent 可向用户 Y 的上下文注入任意内容（Y 的召回命中 X 注入的文档，score 0.78 远超 `scoreThreshold` 0.35）；②**prompt 注入面**：注入内容带指令时，Y 的 agent 会把它当作记忆执行；③ 污染共享检索池（R16 原判只覆盖"平台自动写入"，未覆盖"agent 主动写共享作用域"） | **proxy 侧最小修复（推荐，一处改动）**：`ov_proxy._dispatch` 对**写类工具**（`write`/`edit`/`remember`/`mkdir`/`add_resource`）校验 `args.uri`（及批量载荷里的每个 uri）**必须以 `viking://user/{caller_uid}/` 开头**——caller 已知（`verify_proxy_token` 解出 user_id），一行前缀守卫即可同时封掉 REST 与 MCP 两个面；**服务端长线**：共享作用域引入 ACL（写需 admin，读按需放开）。**测试侧**：本风险由 P5-21 的完整证据链坐实（写→搜→读→ls 四面），不是推测。§11.17 D |


---

## 8. 执行顺序与退出准则

```
Phase 0  接入改造（compose / config / ov_access / ov_proxy /     ✅ 全部落地并验证
                   container_manager / 三个 artefact / Dockerfile）
   ↓     退出准则：docker compose up 全绿，backend 启动无报错，
         镜像构建通过 present-file 式契约门禁 ← **backend 已重建验证；
         agent 镜像已实跑构建（EXIT=0）+ 容器内端到端门禁全绿，
         见 §11.11。R17 已实测降级：6 个用户容器全为 exited，下次启动
         自愈（已用 caesar789 实跑验证），无待办运维项**
Phase 1  服务容器化配置 ── 退出准则：P1-01..08 全通过        ✅ 已通过（§11.7）
   ↓
Phase 2  跨容器网络 ────── 退出准则：P2-01..09 全通过，      ✅ 8 通过 + P2-06 判为
         tools/list 返回 15 工具（原写 16，实测 add_skill 不存在）  风险 R13（§11.7）
   ↓     ⚠ P2-04 在**旧镜像**（无插件）上测得：网络/代理/env 结论有效，
         插件运行时行为未测，归 Phase 5（§11.11 发现 B）
   ↓
Phase 3  中文功能 ──────── 退出准则：评测集建成，P3-01..13 出数，硬性项达标
   ↓     前置铁律：写入一律落 viking://user/{uid}/（§11.8）；
         CJK 判定一律用码点（§11.3 第 7 条）
   ↓     ◐ 部分完成（§11.12）：评测集抽样集建成（40 语料 / 30 查询）；
         P3-03/04/05/07/08/13 已出数并达标或定案；P3-06 判为阈值缺陷；
         P3-11 改判为 token 预算问题。硬性项「编码 100%」已达标。
         **P3-C 已结案**：P3-01 **PASS**（remember 265ms → 抽取落盘 35.8s，
         完成判定四步见 §11.12.6）；P3-02 **FAIL（5/9）** 但判据失真 ——
         抽取器只产 4 个类目且 mis-filing 不影响召回，`target_uri` 定向是
         **负收益**（§11.12.8）。中文专项**定案：不改 ov.conf、不加 TZ**（§11.12.9）
         **P3-G 已结案**：P3-09 **无需改配置**（服务端 token 估算本就 CJK-aware）、
         P3-10 **已落地** `minQueryLength: 2`、P3-12 **无需改**（判据本就 CJK-aware
         且只在 fallback 路径生效）；§11.12.5 的脚手架滤除**已落地在代理层**
         （插件 `recallExcludeUris` 是死旋钮），实测 **token −38.5% / CJK 密度
         +138% / 中文内容零损失**（§11.12.10）
         **待跑**：评测集扩量与类目重设计
Phase 4  性能基准 ──────── 退出准则：P4-01..11 出数，延迟阈值达标或有明确归因
   ↓     前置：延迟必须按 processing_mode / wait 分层统计（§11.8，实测 269ms vs 15324ms）
   ↓     前置：**按 rerank OFF 态跑**（ON 态 p50=3075ms 会让 P4-01/02 全线失败，
         且该配置已永久关闭）；已知基线 p50=136ms / p95=248ms
   ↓     ◐ 已完成（§11.13）：**11 项全部出数，延迟/并发/韧性阈值全线达标**
         （P50 139.8ms、P95 314.8ms、P99 335.0ms；代理开销 +12.0ms；
         10 并发退化 1.38×；P4-11 降级 **34/34 PASS**，最坏 15.0s / 上界 20s）。
         四项须修正口径或重跑：
         · **P4-04 `find` P95 382.6ms 达标但判据失真** —— `find` 返回 L0/L1/L2
           三级混合（42/215/743），不是"廉价 L0 端点"，p95 反而慢于 `search`(list)
         · **P4-05 写入吞吐判据须拆两条** —— `wait=false` **20.7/s ✅** /
           `wait=true` **1.76/s ❌**（567.5ms ≈ 一次 DashScope embedding 往返，
           属上游 SLA 而非 OV 缺陷）
         · **P4-09 规模曲线整体失效** —— 套件第 14 秒撞穿 embedding 配额
           （`429 insufficient_quota` × 99），断路器打开、索引冻结 ⇒ **R24**；
           P4-01…P4-08 全部早于该时刻，数据干净
         · **P4-10 内存有数无线** —— 负载后 757.9 MiB / cgroup 峰值 964.5 MiB，
           但 `HostConfig.Memory=0` + `memory.max=max` ⇒ **容器无内存上限**，
           阈值表要求的"告警线"当前无配置载体（建议 `mem_limit: 1.5g`）
         **结论：没有任何一项指向 OpenViking 的检索或中文能力缺陷。**
Phase 5  兼容与隔离 ────── 退出准则：P5-01..21 全通过，越权 0 次
   ↓     新增断言：平台写入路径不落 viking://agent / viking://resources（R16）
   ↓     新增断言：跨用户越权**不得**用 stats/vector-count 做证据（account 级聚合，R19）
   ↓     新增断言：**容器内"search 拿 URI → REST content/read 读正文"这条链对
         events 记忆是断的**（URI 折叠形式 → 404），读正文必须走 MCP read（R21）
   ↓     新增断言：**列举记忆树不得用根级 recursive `fs/ls`**（深度截断且 limit
         旋钮全无效，会静默少数），必须用 MCP `glob` / `tree`（R21）
   ↓     新增断言：确认服务端接受 `search/recall` 的 `peer_scope` 字段 ——
         插件在 400/422 时会**静默降级**为不带 peer_scope 重试，语义是把召回
         范围从本 peer 放宽到整个 user root（隔离失效），并落 memo 文件
         `peer-scope.json`（§11.12.9 C）⇒ P5 须检查该 memo 不存在
   ↓     新增前置检查（R24）：**任何写入类用例开跑前先探 embedding 健康度** ——
         `docker logs --since 5m openviking | grep -cE "429|circuit breaker"` 须为 0，
         且 `_system/queue` 无积压；否则该轮写入数据一律标记为无效（断路器打开时
         写入仍返回 200 但索引静默滞后）。批量灌数据一律走抛弃型 account
         **✅ 该门禁已复绿（§11.13 E 结案 / §11.14）**：429/breaker/embed_fail/
         re-enqueue 四项全 0，`queue.db`/`-wal` mtime 冻在 04:06 不再增长，
         `vector_count 463→813`，OV CPU 回落 2.07 %，`health=200 ready=200`，
         embedding/vlm `valid="1"`→1.0（rerank 仍按 R3 决策为 0.0）
   ↓     新增前置检查（**R25，宿主环境**）：开跑前记录 `dockerd` 的 `etimes`，
         若小于预计套件时长，则该轮数据会被一次 WSL 用户态回收作废 ⇒ 重跑。
         **不要**用 `RestartCount` 判断是否发生过重启（daemon 重生时它恒为 0），
         要用 `State.StartedAt` + `dockerd etimes` 交叉验证。journal 是 volatile 的，
         取证必须**在飞行中落盘到 `/mnt/d`**（§11.14 G/I）
   ↓     ◐ **Group A（P5-01…P5-06）已完成（§11.15）**：**6 项全 PASS**，
         两个硬性项零例外 —— 跨用户越权 **0 次泄露**（12 条路径全 403，
         含 percent-encoded URI、`target_uri`、MCP `read(uris[])`）、
         真 Key 进容器 **0 次**（ROOT / `AGENT_SECRET_KEY` / 外域 user key
         在 env + 9 目录 fs 净测命中数全 0）。令牌伪造 **全 401**、
         头部提权 **全 403**、管理面经代理 **全 404 / `-32601`**、
         角色边界矩阵 11 探针符合预期（ROOT 纯管理面、ADMIN 读不了任何用户空间）。
         **四项判据/结论须修正**：
         · `search` 的 `mode=context` **不接受 `target_uri`** → 400 `INVALID_ARGUMENT`，
           该组合不能当越权探针（原设计失效，改用 `mode=list`）
         · "B 独有 marker" 语义搜索命中 0.8368 **不是泄露** —— URI 落在
           `viking://user/p2probe/...`，是 A 自己的记忆（Phase 2 灌过同一句）
         · `opencode.json` 里的 `apiKey`(len=36, head=`21a57a`) 是**宿主 LLM
           供应商 key**（`build_container_config` 只改写 `baseURL`、`apiKey`
           原样透传），**与 OpenViking 无关**，不计入"真 Key 进容器"
         · `ovcli.conf` 的 `mcpEnabled:false` **不是"关 MCP"**，而是阻止插件用
           `node servers/mcp-proxy.mjs` **替换**远程入口（镜像 PATH 上无 node）
           ⇒ 远程 MCP `backend:8000/ov/mcp` 是**活跃生产通道**，本组 MCP 探测
           打在真实通道上（`container_manager.py:56-61 / 510-517`）
         **两个风险更新**：**R13 复核并给出决策输入**（容器无 key 仍可直连
         `openviking:1933` ⇒ 白名单不是网络边界，推荐①移独立网络）；
         **R26 新增**（MCP 拒绝形状不一致：`read`/`glob` 越权时 `isError:false`）。
         **R14 陈旧项坐实**（`tools/list` 恰 15 个、无 `add_skill`，已 dump 全部
         `inputSchema`）。**乱码问题结案**：属 PowerShell codepage 936 显示层产物，
         服务端 UTF-8 正确（§11.15 H）。
   ↓     ✅ **Group B（P5-09/10/11/12/13/14/16/17）已完成（§11.16）**：**8 项全 PASS**，
         但暴露 **2 个平台侧真实缺陷（R27 中危 / R28 低危）**
         · **P5-09** 三方握手成功，但**判据须改口径** —— 服务端 REST `v0.4.21`
           与 MCP `serverInfo` `1.27.0` **不同源**，版本断言必须指明哪一面
         · **P5-10 既有能力零冲突** —— 容器内 3 MCP + 3 插件并存全 `enabled=true`；
           裸名 `search` 重叠但**前缀后 `prefixed_overlap=[]`**；web_search stdio /
           openviking / searxng **三处真握手全通**
         · **P5-11 两层全绿** —— Layer 1 九用例（C0–C9）渲染矩阵 + 幂等性；
           Layer 2 **六步 `push_ok=true` 活体推送**，容器内 `cat` 回读与 backend 的
           Docker-archive 读回**逐字段相同**，`roundtrip_hidden` 每步精确等于
           effective hidden ⇒ 开关只作用在 permission 层，条目/插件从不被摘除
         · **P5-12/13** `GATES_RC=0`、`tests_dir=ABSENT` ⇒ 改构建门禁的决策成立
         · **P5-14** WRITE_DENY 覆盖 `/opt/agent` 全部 ✅，但插件 **5 条写入路径只有
           1 条落 volume**，其余 4 条（含 **pending 离线补投队列**）落 tmpfs ⇒ 重启即丢；
           需 3 个 env 重定位（`OPENVIKING_STATE_DIR`/`_PENDING_DIR`/`_DEBUG_LOG`），
           **已端到端验证有效**；`Dockerfile:310-313` 注释有误
         · **P5-16** 33/33 全钳制零抛错，**唯一例外 `authMode`**（`API_KEY→""`，
           大小写敏感 ⇒ 静默丢掉平台钉死的 `api_key`）
         · **P5-17** 写侧 **out-of-scope 已定案**（REST 10 端点全 404、写入工具载荷
           全 `string`/`array<object>`）；读侧有能力（`read` 支持 raster/audio）
         **两个风险新增**：
         · **R27（中危，功能损伤）** 白名单形 preset `mcps` **取消隐藏不可逆** ——
           `librarian.mcps=["web_search"]` → 隐藏 → `[]` → 取消隐藏 → **仍是 `[]`**，
           子代理永久失去该能力且**无任何报错**。根因 `opencode_config.py:563`（白名单项
           被隐藏即删除）vs `:565-568`（只有含 `*` 的 allow-all 形才用 `!name` 标记）
           ⇒ **openviking 自身完全可逆**，受损的是被 preset 白名单点名的其它 MCP。
           **自愈已证**：`render_plugin_config:628` 从零重建，`rendered_equals_restored=true`
           ⇒ 容器 start/recreate 自愈，**只有运行中热开关受损**
         · **R28（低危）** permission 键**只增不减**（14→15，`web_search_*` 取消隐藏后不删）；
           且宿主自带 **`web_search*`（无下划线）** 与平台生成的 **`web_search_*`** 是
           **两条不同名键并存**，一 allow 一 deny 时优先级未定义/未测
         **测试残留已精确还原**：`RESTORE_OK=true`，容器内独立回读 `perm_count=14`
         （键集等于基线）、`librarian.mcps=["web_search"]`、`orchestrator.mcps=["*"]`、
         `roundtrip_hidden=[]` ⇒ 与 `render_plugin_config` 产出**结构完全相等**
         **R25 第 15 次复现**（`07:42:07` / `07:45:33` 两次栈级重启，四容器 `created`
         未变、`RestartCount` 全 0 ⇒ 整栈停止后重启，容器对象未重建）
   ↓     ✅ **Group C（P5-18/19/20/21）已完成（§11.17）**：**3 PASS + 1 FAIL**
         （P5-21 ⇒ **R29 高危：跨用户注入通道**）
         · **P5-18** 派生 key 一致性 PASS —— 4 新用户注册/realign 回读全等（len 115）；
           R11 失败回落直测生效；容器 env 持有的是 **proxy token**（解码
           `ovproxy:p2probe`）而非 OV key ⇒ 真实派生 key 永不出 backend
         · **P5-19** 计数端点口径 PASS —— 三用户（含 0 记忆新用户）stats 逐字符相同
           （651 聚合）坐实；**新发现：ROOT key 对租户数据面全 403** ⇒ 取证须用
           user/admin key
         · **P5-20** 删除通道 PASS —— 判据修正（REST 实有 16 条 DELETE，原"无删除
           端点"表述推翻）但 agent 可达面无删除仍成立（三重防线）；
           **产品决策定案：不放开 forget**
         · **P5-21** 写入路径作用域 **FAIL ⇒ R29（高危）** —— 平台自动写入全落
           user scope ✅，但 user/agent 可写共享作用域且**泄漏四面全通**
           （b 搜 0.78 + 读全文 + ls + 第三方可见）⇒ 跨用户注入通道
         · 残留已清理（`viking://resources` 归零、agent 只剩平台目录、四探针用户 202）
         **Group D 已完成（2026-09-29）**：P5-07/08/15 全 PASS —— 容器重建后
         记忆存续（tree 12 叶全等）、key 轮换 stale→重铸→自动重建→轮回自愈
         （R12 runbook 实测有效）、OV 宕机期 2/2 会话 OK + 代理 502 快速失败
         （R9 判据满足）。§11.18
汇总     验收报告 + ~~rerank 决策（先解 R3 前置）~~ **rerank 已决策：永久关闭（R3 结案）**
         + 参数调优建议（~~minQueryLength~~ **已定案 = 2** / ~~scoreThreshold~~ → **已落地：代理层按 URI 滤脚手架，R15 + §11.12.10**）
         + **测试收尾已完成（2026-09-29）**：探针用户/语料/容器/卷/`_scratch_*` 全清，
           root key 已轮换结案（R1），环境终态 4 容器 running / 仅剩 platform-admin 与
           root 两用户 —— 详见 §11.19
```

Phase 1 与 Phase 2 之间、Phase 4 与 Phase 5 之间无强依赖，可并行。Phase 3 依赖 Phase 2 打通。

---

## 9. 附录：关键参数速查（中文调优靶点）

| 参数 | 默认 | opencode harness | 中文相关性 |
| --- | --- | --- | --- |
| `timeoutMs` | 15000 | **30000** | 中文写入/提取更慢，需较大超时 |
| `scoreThreshold` | 0.35 | — | **已量化定案：不要动它**。近似原句 0.89 / 原句 0.68 / 同义改写 0.63；噪声：`.overview.md` 脚手架 **0.61**、无关 `.abstract.md` 0.25–0.33。脚手架 0.61 与真实命中区间重叠 ⇒ 无可用间隔。且**滤除脚手架对 precision 零收益**（delta +0.000），R15 的真实性质是 token 预算浪费（25.3% 槽位被噪声占）。对策是**按 URI 过滤**，不是调阈值（§11.12.5）—— **该对策已落地**，但落点是**代理层**（插件 `recallExcludeUris` 是死旋钮），实测回收 **38.5% token**（§11.12.10） |
| `minQueryLength` | 3 | — | **已定案 = 2**（`ovcli.conf` 的 `plugin.opencode.minQueryLength`）。中文 2 字即有意义，损失 100% 在插件门禁 `lib/memory-recall.mjs:10`，服务端两字查询完全正常（B6「故障」→ 4 条高相关命中）。env `OPENVIKING_MIN_QUERY_LENGTH` 优先级更高、alias `recallMinQueryLength` 同效（§11.12.10 A） |
| `recallLimit` | 10（1–50） | — | 中文单条记忆 token 更多，可能需下调。**注意：上调 + 客户端滤除（overfetch）已实测无效**——深度 6–8 补位的文档同样不是 expected，`precision@5` 分毫不变（§11.12.5）。所以 `recallLimit` 只能按 token 预算调，不能指望它提精度 |
| `recallTokenBudget` | 2000（200–50000） | — | 直接决定注入上下文量，须配合 CJK 估算 |
| `recallMaxContentChars` | 500（100–5000） | — | **字符**上限对中文比英文更紧（1 字 ≈ 1.5 token） |
| `profileTokenBudget` | 10000（500–50000） | — | profile 注入总预算 |
| `captureMode` | `semantic` | — | 语义模式依赖 vlm，中文提取质量由此决定 |
| `commitTurnThreshold` | 8 | — | 触发记忆提交 |
| `commitTokenThreshold` | 20000 | — | 中文更早触发（同 token 数对应更少字符） |
| `captureMaxLength` | 24000 | — | 单轮捕获上限 |
| `autoRecall` / `autoCapture` | true / true | — | 记忆服务的核心价值所在 |

插件源码仓库有 7 个测试文件（`tests/*.test.mjs`：`config`、`mcp-config`、`memory-recall`、`memory-session`、`runtime`、`utils-logger`、`viking-uri-guard`），但**发布包不含它们**（`files` 白名单排除），故不能作为 CI 门禁（P5-12 已改写）。上表中 `mcpEnabled` / `repoContext` / `dataDir` 三项**没有对应 env 变量**，只能靠 `ovcli.conf` 配置——这是 §10 结论 3 的直接来源。

**服务端侧旋钮**（`ov.conf`，Phase 3 新增；与插件 knob 是两套东西，别混）：

| 参数 | 默认 | 中文相关性 / 实测结论 |
| --- | --- | --- |
| `rerank.api_key` | 空 | **唯一旋钮，必须保持为空**。非空即激活 rerank 并连带切进 THINKING 模式（R3 结案 / R18）。没有 `enabled: false` |
| `default_search_mode` | `"thinking"` | **死配置**，改它无效。实际模式由 `hierarchical_retriever.py:127` 按"有没有 rerank client"决定。⚠️ **它是 `OpenVikingConfig` 的顶层键，不在 `retrieval` 段下**（原表写成 `retrieval.default_search_mode` 是错的） |
| `auto_generate_l1` | `True` | L1 = 目录 `.overview.md`。⚠️ **顶层键，不是 `retrieval.*`**。**原判「只有 `remember` 抽取路径会触发生成，`content/write` 不会」已被推翻**：两条写入路径都会调 `generate_overview`（`memory_updater.py:1043` 抽取路径 / `:799` `refresh_schema_overview` classmethod，后者由 `content_write.py:536`、`:1270`、`fs_service.py:425`、`:433` 调用）。**真正决定生成与否的是 registry 里该 memory type 有没有 `overview_template`**（R18），与本旋钮无关 |
| `output_language_override` | `""` | 源码注释："bypasses content-based language detection … forces this language instead … (e.g., 'en','zh-CN','ja')"。⚠️ **顶层键，不是 `retrieval.*`**。**已实测，作为中文专项杠杆被推翻**：它确实能硬强制 `semantic_processor.resolve_output_language`（影响记忆正文语言、文件名 slug、资源摘要、VLM 目录 overview），但**影响不到任何 overview 标题** —— `generate_overview` 的渲染上下文 `overview_context` 只有 `{memory_type, directory_name, items}`，**没有 `language` 键**，且 `grep output_language memory_updater.py` → **零命中**；标题全是 YAML 英文字面量，模板本身又是 locked（R22）。**副作用大于收益**：它是**全租户硬强制**，会把英文用户的记忆也写成中文 ⇒ **决定不启用**。完整可改性矩阵见 §11.12.9 |
| `default_search_limit` | `3` | 与插件的 `recallLimit=10` 不是同一个；请求里显式传 `limit` 会覆盖它。⚠️ **顶层键，不是 `retrieval.*`** |
| `language_fallback` | `"en"` | **deprecated**，不要依赖。⚠️ **顶层键，不是 `retrieval.*`**。已被 `language.py` 的系统回退链取代（`LC_ALL`→`LC_MESSAGES`→`LANGUAGE`→`LANG`→`locale.getlocale()`→`TZ`→`/etc/localtime`→`time.tzname`）；**实测注入这四类 env 只能翻转"空文本"的 fallback，`'ok'` 与 `"print('x')"` 在 fallback=`zh-CN` 时仍判 `en`** ⇒ 收益≈0（R23） |
| `retrieval.enable_intent` / `hotness_alpha` / `score_propagation_alpha` / `recall_intent_timeout_s` / `recall_rewrite_timeout_s` | — | **这五个才是真正在 `RetrievalConfig` 里的字段**；`score_propagation_alpha` 与目录分数向下传播有关，是 R18 的第二个可调点（未测） |
| `TZ`（compose env，非 `ov.conf`） | 未设（容器内 `time.tzname=('UTC','UTC')`） | **不要加 `TZ=Asia/Shanghai`** —— 已实测推翻（R23）。`events` 日期分桶走的是服务端 UTC 打戳的 `created_at`，TZ 动不了主路径，只会翻转 naive `datetime.now()` fallback，造成**双重日期基准** |

---

## 10. Phase 0 实施结论（回写）

Phase 0 期间读插件源码 + 用真实 npm 包实跑 `loadConfig()` 得到的六条结论。每条都改动了原方案的某个假设，前文相应位置已就地更正。

### 结论 1：服务端**不需要**升级，钉 digest 而非跟 `latest`

registry 的 `latest` 已从本地 digest 前移（`sha256:569193ef…` → `632d24fd…`）。原以为要跟，实测后决定**不跟**：插件的召回是**双路径降级**（`lib/shared/recall-core.mjs`），先试 context-face `POST /api/v1/search/search`（`:516`），失败则回落 deprecated 的 `POST /api/v1/search/recall`（`:637`/`:656`），本地 digest 两条路径都在。也就是说 `/recall` 标了 `deprecated=True` 并不构成升级理由。升级的收益未被任何已识别的缺口要求，成本却是重跑 Phase 1 的全部服务端契约核对。

### 结论 2：per-user 凭据有**两个**注入点，不是一个

原方案只在 manifest header 上盖章。实际上插件有两条独立通道：

| 通道 | 承载能力 | 凭据来源 |
| --- | --- | --- |
| MCP（`config.mcp.openviking.headers`） | 16 个工具，模型显式调用 | backend 在 `build_container_config` 里盖章 `X-API-Key` |
| REST（插件进程内 `fetch`） | autoRecall / autoCapture / profile-inject | 容器 env `OPENVIKING_API_KEY` |

两处发的是**同一个**代理令牌（`ov_access.issue_proxy_token(user_id)`），真实 User Key 永不进容器。插件 HTTP 层刻意发 `Authorization: Bearer` 而非 `X-API-Key`（`lib/shared/ov-http.mjs` 的 `buildOvHeaders`），故 `ov_proxy` 必须**两个头都接受**——已实现（`_caller_user_id` 双凭据头）。

关键安全性质：`credentials.mjs` 的 `hasCredentialFields` 只检查**顶层** 8 个键，而 knob 文件顶层只有 `plugin`，所以它不参与凭据解析。已用负控制实测确认。

### 结论 3：hook-only 是**强制**的，且只能靠烘进镜像的 knob 文件实现

这是 Phase 0 最大的意外发现。`lib/mcp-config.mjs`：

```js
export function injectOpenVikingMcpConfig(config, pluginRoot, enabled = true) {
  if (!enabled) return false                    // ← hook-only 的唯一出口
  const current = config.mcp[OPENCODE_MCP_NAME]
  if (current?.enabled === false) return false
  config.mcp[OPENCODE_MCP_NAME] = createOpenVikingMcpConfig(pluginRoot)  // ← 覆写
  return true
}
```

`createOpenVikingMcpConfig` 返回 `type: "local"` + `command: ["node", ".../servers/mcp-proxy.mjs"]`。而镜像**故意**不把 node 放进 PATH（只有 `/opt/agent/skill-envs/pptx/bin/node`）。后果是：插件加载即覆写我们的 remote 条目 → opencode 起一个不存在的命令 → **agent 启动时既没有记忆工具也不报错**。这是静默失效，不是崩溃。

`mcpEnabled` 这个 knob **没有 env 变量**（`lib/shared/config-schema.mjs`），唯一配置途径是 `OPENVIKING_CLI_CONFIG_FILE` 指向的文件。故新增第三个 artefact `ovcli.conf`，三个 knob：

| knob | 值 | 解决的失效模式 |
| --- | --- | --- |
| `mcpEnabled` | `false` | 上述覆写。**load-bearing** |
| `dataDir` | `/data/state/openviking` | 插件默认写 `~/.config/opencode/openviking`，而 `/home/agent` 是 tmpfs → 重启即失。`/data` 是 named volume，且 `Dockerfile:202` 的 `install -d -o 1000 -g 1000 /data` 保证 uid 1000 可写 |
| `repoContext` | `false` | **判断决定，可翻转**。它既不是自动召回也不是自动写入，而是每次 `session.created` 触发一次仓库扫描，会污染 Phase 3 的中文召回评测语料 |
| `minQueryLength` | `2` | **P3-G 追加**（上游默认 3）。`lib/memory-recall.mjs:10` 在 `query.length < minQueryLength` 时直接 return ⇒ 中文高频的两字词查询（「故障」「部署」「网关」）被整段吞掉，而服务端两字查询完全正常。**非 load-bearing**，故未加进构建门禁断言（§11.12.10 A/F） |

配置解析路径已实测：`loadConfig` 在容器 env 下解析出 `mcp:{enabled:false}`、`runtime.dataDir:"/data/state/openviking"`、`credentialSource:"env"`、`apiKeySource:"env"`、`authMode:"api_key"`、`sendIdentityHeaders:false`、`endpoint:"http://backend:8000/ov"`、`autoRecall:true`、`autoCapture:true`，且**解析过程不创建任何目录**。两条负控制：去掉 knob 文件 → `mcpEnabled=true dataDir=`（证明门禁断言的是真实失效模式）；knob 文件带 `url`+`api_key` 且 env 也有凭据 → 仍解析为 `env/env`（证明文件不会 pin 凭据链）。

Dockerfile 构建门禁把上述 6 项变成构建失败条件，用 runtime 镜像自己的 node 跑（插件零依赖，故跨 stage 安全）。

### 结论 4：env 变量名必须用插件**已定义**的名字

原方案自拟了 `OPENVIKING_BASE_URL`。插件 `CREDENTIAL_ENV_VARS` 里确实有这个名字，但它与 `OPENVIKING_URL` 语义不同，且插件给 REST 调用拼 `/api/v1/...`、给 MCP 拼 `/mcp`——需要**两个**变量。最终 5 项（`container_manager._openviking_env`）：

```
OPENVIKING_URL               = settings.openviking_rest_url   # http://backend:8000/ov
OPENVIKING_MCP_URL           = settings.openviking_mcp_url    # http://backend:8000/ov/mcp
OPENVIKING_API_KEY           = ov_access.issue_proxy_token(user_id)
OPENVIKING_AUTH_MODE         = "api_key"
OPENVIKING_CLI_CONFIG_FILE   = /opt/agent/builtin-plugins/openviking/ovcli.conf
```

`OPENVIKING_AUTH_MODE` 显式 pin 是防御性的：`api_key` 模式下服务端会静默剥离 `X-OpenViking-Account` / `X-OpenViking-User`（`server/auth/plugins/api_key.py:97-103`），但不应依赖服务端兜底来阻止插件自报身份。`OPENVIKING_CREDENTIAL_SOURCE` **不设**——负控制 2 已证明 env 里有任一凭据变量就足以让 `pinned=false`，多一个变量是冗余真相源。

`_ov_env_stale()` 逐项比较这 5 个变量 + `verify_proxy_token`，任一漂移即重建容器（Docker 永不刷新已创建容器的 env）。

### 结论 5：kill switch 有**两个半边**，权限键必须对称

`AGENT_OPENVIKING_ENABLED=false` 原本只在 `builtin_mcp_servers()` 里 pop 掉 MCP 条目，插件那一半没有门禁 → 开关关掉后插件仍会被注入，而 `_openviking_env` 已不再下发凭据，于是**每次 agent 启动都在打注定失败的请求**。已在 `_discover_builtin_plugins()` 补 gate（manifest 是烘进镜像的只读文件，发现逻辑本身无法感知开关，只能在两个消费点各自施加）。

另一处对称性：`CONTAINER_DEFAULTS.permission` 的 allow 键必须写成 `openviking_*`，与用户隐藏 MCP 时 `build_container_config` 写入的 deny 键 `f"{_sanitize_mcp_permission_key(name)}_*"` **同形**，否则用户取消隐藏后工具仍被 deny。

### 结论 6：镜像 tag 不需要 bump；构建已实跑通过

`container_manager._image_stale()` 比较的是 `client.images.get(...).id`（**镜像 ID**）而非 tag，所以重建同名 `agent-demo:1.4.0` 就能让容器 recreate。bump 版本号反而要同步改 README / verify.sh / config.py / .env / docker-compose 共 8+ 处引用，无收益。

插件发布包不含 `tests/`，P5-12/P5-13 不可执行，已由 Dockerfile 构建门禁替代（§6 硬性项相应改为 6/6）。

**构建结果（已实跑）**：`bash scripts/build-agent.sh` → `EXIT=0`、`== done ==`、242 行日志。`--network=host` 的 WSL2 DNS 兜底**未触发**，plain build 一次通过。新镜像 `agent-demo:1.4.0` = `sha256:f7819d254443970969cca329a4c68ffa40e400c14f859c55ba98b191b8a5ac4f`，386253582 B。fetch 阶段 `#11 [fetch 5/8]` 的 present-file 契约门禁（`test -f index.mjs && test -f lib/config.mjs`）**DONE 2.0s 通过**，`@openviking/opencode-plugin@2026.9.25-2` 从 `registry.npmmirror.com` 装成。完整门禁与容器内端到端验证见 **§11.11**。

> **⚠ 上面第一段的推论被实测推翻了一半**：`_image_stale()` 只在**已停止**容器的路径上被调用。运行中的容器走 `ensure_container` 的 fast path，只检查 `_fastk_env_stale` / `_ov_env_stale`，**不检查镜像**，并且直接 `return container, password, True`（连配置都不重新注入）。所以"重建同名 tag 就会触发所有容器 recreate"只对停止的容器成立。详见 **§11.11 发现 A**——这是 Phase 0 的收尾必做项，不是可选项。

## 11. Phase 1/2 实测记录与测试工装约束（回写）

### 11.1 已通过项的真实响应体

`/health`（无认证，backend 容器内经 agent-net 直连）：

```json
{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key"}
```

带 root key 时额外返回身份：`"account_id":"default","user_id":"default","role":"root"`。

`/ready`：

```json
{"status":"ready","checks":{"agfs":{"status":"ok","checks":{"filesystem":"ok","multiwrite_sync":"not_supported"}},"vectordb":"ok","api_key_manager":"ok","embedding":"ok","ollama":"not_configured"}}
```

**安全注记（新发现）**：`/ready` 与 `/health` **均无需任何认证**即返回 200（实测在 `X-API-Key` 缺失时同样 200），且 `/ready` 泄露内部子系统拓扑（AGFS / vectordb / embedding / ollama 的配置状态）。这是服务端设计而非本方案缺陷，缓解措施是 §4.1 的「不发布 host 端口 + 仅挂 agent-net」，即由**网络边界而非应用层鉴权**兜底。

因此 **P2-06 的定性需要升级**：原表述是「若可达，记录为风险而非致命」。但用户容器与 openviking **同在 agent-net**，若 P2-06 实测可达，则任何拿到容器 shell 的用户都能读到内部拓扑并绕过代理直连——这不是风险而是致命，必须实测并据此决定是否需要为 openviking 单开一个只允许 backend 加入的网络。

`/api/v1/admin/accounts`（root key）：

```json
{"status":"ok","result":[{"account_id":"default","created_at":"2026-09-28T04:56:05.776044+00:00","user_count":0,"status":"active"}],"error":null,"telemetry":null,"profile":null}
```

`created_at` 早于本轮全部重启，**证明 named volume `openviking-data` 跨 daemon 重启持久**（P1-05 的持久化半边已顺带验证）。

### 11.2 「崩溃循环」是测试工装假象，不是产品缺陷

**现象**：`docker ps` 反复报 `Up 13 seconds`；`.State.StartedAt` 在几十分钟内前移 8 次（05:05:11 → 05:08:23 → 05:10:37 → 05:12:07 → 05:24:06 → 05:27:05 → 05:34:08 → 05:38:44）；`.State.Health.Log` 交替出现 `ExitCode 7`（`curl: (7) Failed to connect to 127.0.0.1 port 1933`）与 `ExitCode 0`；日志里出现 6+ 个完整启动块；曾观察到 `FinishedAt` 早于 `StartedAt`。

**排除项（均有实测证据）**：

- 非 OOM / 非资源限制：`OOMKilled=false`、`MemLimit=0`、`MemSwap=0`、`PidsLimit=<nil>`
- 非崩溃：`ExitCode=0`、`Error=[]`，全量日志 `grep -inE "killed|signal|exit|shutdown|fatal|panic|exception|failed|refused|timeout|oom"` **零命中**
- 非 entrypoint 提前退出：镜像 `/usr/local/bin/openviking-entrypoint` 末尾是 `wait "${SERVER_PID}" || SERVER_STATUS=$?; exit "${SERVER_STATUS:-0}"`，且 `trap 'forward_signal' INT TERM` 会把信号转给 server
- 非健康检查语义错误：脚本第 96-98 行确有 `--healthcheck` 分支 `exec curl -fsS "http://127.0.0.1:${SERVER_PORT}/health"`，与镜像 `HEALTHCHECK` 声明一致
- 非 daemon 重启计数丢失导致误读：`RestartCount` 恒为 0 恰是**新 daemon 首次拉起**的特征，而非「从未重启」

**真因**：本机 Docker 是**原生 dockerd 跑在 WSL2 `Ubuntu-24.04` distro 内**（`docker info` → `os=Ubuntu 24.04.4 LTS`、`root=/var/lib/docker`、`docker.service` 由该 distro 的 systemd 管理），而非 Docker Desktop。WSL 在 distro 内最后一个进程退出后约 8s **销毁整个 distro 实例** → systemd 停止 → dockerd 停止 → 容器收到 SIGTERM → entrypoint `forward_signal` → server 干净退出（`ExitCode=0`）。下一条 `wsl -d Ubuntu-24.04 -e ...` 命令重新引导 distro → `docker.service` 启动 → dockerd 按 `restart: unless-stopped` 恢复容器 → **`RestartCount` 归零**。

**决定性证据**：

1. 探测时刻 `pidof dockerd` = 288，`ps -o etimes=` = **9 秒**，同时 `systemctl` 显示 `docker.service loaded activating start`
2. 两次相邻命令之间 `/tmp/ovd9.out` **凭空消失**（distro 被销毁，`/tmp` 随之清空）
3. 单个长驻脚本内 **12 次采样 × 15s = 180s**：`StartedAt` 完全冻结在 `05:34:08.569035614Z`，`health` 从 `starting` 转 `healthy` 后连续 11 次采样全为 `healthy`，`docker events` 全程**零** `die`/`start`/`stop`，只有每 30s 一次的 healthcheck exec（首个落在 30s `StartPeriod` 内 `exit=7`，其后全部 `exit=0`）

**踩坑记录**：WSL2 的 `/proc/uptime` 报告的是**整个 Hyper-V 工具 VM** 的运行时长（实测 12400s ≈ 3.4h），**不是** distro 实例的运行时长。用 uptime 判断「distro 是否被回收」会得到完全错误的结论——这正是本次一开始误判的原因。要判断 daemon 是否重启，必须看 `ps -o etimes= -p $(pidof dockerd)`。

### 11.3 对 Phase 1–5 的硬性约束

1. **每轮测试必须在单个长驻会话内跑完**。跨命令的测试序列，两条命令之间服务都会被重启：内存态丢失、向量库写入可能未 flush、延迟基准不可信。
2. **执行前先起 keepalive**：后台常驻 `wsl -d Ubuntu-24.04 -e sleep <N>` 把 distro 钉住。
3. **脚本输出先重定向到 `/tmp` 再另起命令读**，不要 `docker events | head -N`：事件流默认携带全部 OCI/compose label，单条数百字符，`head` 触发 SIGPIPE 会提前杀死脚本后续逻辑。`docker events` 必须带 `--format`。
4. **WSL 与 dockerd 时钟相差 8 小时**（WSL 报 UTC，daemon 按本地 UTC+8 解析 `--since`/`--until` 裸时间戳）。`docker events --until <未来时刻>` 会退化成**阻塞实时流**导致脚本挂死。
5. **`docker exec <c> cmd <<'EOF'` 必须加 `-i`**，否则 heredoc 不进容器 stdin，被调程序读到 EOF 立即以 0 退出且**静默无输出**（曾导致一整轮误判）。backend 容器内解释器是 `python3`，没有 `python`。
6. **rich 控制台会折行日志**：`grep -c "session manager started"` 返回 0 是假阴性（该行实际被折成三段）。统计启动块要用单行文本（如 `Bot API proxy enabled`）。
7. **任何 CJK 正确性判定必须用码点，禁止肉眼比对渲染结果**。最小方法：`" ".join(f"U+{ord(c):04X}" for c in s[:n])`，并同时打印参考码点。违反此条已产生过一次完整的误报（见 §11.9）。
8. **不要用 `head -c N` 截断含 CJK 的裸字节流**（会切断多字节序列）；改用 Python 按字符切片，输出统一 `.decode("utf-8")` 后再打印，不要让裸字节直接落进 `/tmp/*.out`。
9. **读取 WSL 内 `/tmp/*.out` 要走「复制到 `/mnt/d/...` + 编辑器读取」**，不要经控制台管道（`cat` / `Get-Content`）—— 后者存在按本地代码页解码的风险。
10. **任何触碰 `backend/app/**` 的改动，验证前必须 `docker compose build backend && docker compose up -d backend`**（见 §11.5）。只 `--force-recreate` 会得到"代码看着对、行为完全没变"的静默假阴性。
11. **`wsl -e /bin/sh -c '…'` 里剥 CRLF 只能用 `tr -d '\r'`（单引号）**。写成 `tr -d "\r"` 时 sh 把 `\r` 当两个字面字符，tr 于是删掉文件里**所有的 `r`**，症状是 `docke: not found`；写成 `tr -d "\015"` 更隐蔽——它删掉所有 `0`/`1`/`5`，把 `agent-demo:1.4.0` 削成非法镜像引用（`invalid reference format`）。外层 PowerShell 用双引号时，`$?` 须转义为 `` `$? ``，否则被 PowerShell 先替换成 `True`。
12. **`docker exec` 里打印 env 值时禁止 `${v:+SET(len=…)}${v:-UNSET}` 这类"两个展开都留着"的写法**——变量有值时两个都会展开，等于把凭据明文打进日志。密钥类变量一律只打 `len=`。
13. **不要碰 docker-py 的 `container.image` 属性**。旧镜像被重建挤掉后它抛 `ImageNotFound`；平台代码自身已改用 `container.attrs["Image"]` 规避（`container_manager.py:758`、`:899` 均有注释说明），测试脚本照做。
14. **验证 Phase 0 接线前，探针容器必须 recreate 到新镜像**（R17 / §11.11 发现 A）。`ensure_container` 对运行中的容器不会换镜像、也不会重注入配置，却返回 `config_ok=True` —— 直接测会得到"配置看着对、插件根本不在容器里"的假阳性。
15. **判断"容器在旧镜像上"之前，必须先查 `.State.Status`**。`docker ps -a` 的 `IMAGE` 列不区分运行与退出，只看它会得出"一批活跃用户等着 recreate"的错误结论（R17 一度被高估）。停止的容器下次启动就会被 `_needs_recreate` 自愈，主动 start/stop 去"刷新"它们纯属无谓扰动。另：proxy token 长度随 `user_id` 长度变化（`p2probe` → 100，UUID → 140，`ovzh-eval` → 119，`studio-debug` → 123），别按固定长度断言。
16. **`.env` 绝对不能带 `\r`，且只能从 WSL 内部编辑**。混入 CR 后 compose 会把带 `\r` 的值原样传进容器（实测 `OV_ROOT_API_KEY len=65 last-byte-hex=0d`），`ov.conf` 经 `os.path.expandvars` 后裸 `\r` 落进 JSON 字符串 ⇒ `json.loads` 报 `Invalid control character at: line 6 column 86` ⇒ **crash-loop 到 `RestartCount=9`**。改完必须 `tr -d '\r' < .env > .env.lftmp && mv .env.lftmp .env`，并断言 CR 计数为 0。同理，**scratch 脚本自身在跑之前也要 `tr -d '\r'` 归一化**（Windows 侧写出来的文件默认 CRLF）。
17. **改 `.env` 后 `docker compose up -d` 可能是静默空操作**——compose 插值**优先读 OS 进程环境，其次才读 `.env` 文件**。若之前用 `set -a; . ./.env; set +a` 把旧值导出进了当前 shell，旧值就会遮蔽你对文件的修改，config 哈希不变，`up -d` 直接打印 `Running` 返回。标准范式：`env -u <VAR> docker compose up -d --force-recreate <svc>`，并**双断言**——比对 `docker inspect -f '{{.State.StartedAt}}'` 前后是否变化 + 查 `/metrics` 的运行时 gauge。
18. **`sed -i` 式的原地改写必须"源与目标是不同文件"**。`sed 's|…|…|' .env > .env` 会在打开输出时先截断输入 ⇒ 得到空文件。正确写法：`cp .env /tmp/env.bak && sed '…' /tmp/env.bak | tr -d '\r' > .env`。
19. **不要用 `2>/dev/null` 吞掉凭据派生类调用的 stderr，也不要用 `sed -n '/^XXX/,$p'` 过滤脚本输出**。两者叠加造成过一整轮静默失败：全栈被外部重启 ⇒ `ov_access.user_api_key()` 内的 `ensure_account()` 打 Admin API 抛 `OvUnavailable: cannot reach OpenViking at http://openviking:1933/...`，stderr 被吞；评测器随后打印 `FATAL: OV_API_KEY not set` 并 `sys.exit(2)`，而这一行恰好被 `sed` 的范围过滤掉 ⇒ 表现为"脚本跑完了、输出只有 1/4、没有任何错误"。正确做法：**派生 key 用重试循环**（10 次 × 3s，因为栈随时可能被外部重启）、**保留 stderr**、**取不到就 `exit 1`**、过滤锚点选一个失败时也会打印的标记。
20. **在 PowerShell 里不要内联复杂嵌套引号**（`python3 -c "…"` 或带 `'{"a":1}'` 的 grep 会产生十余条 ParserError / `The string is missing the terminator`）。可用的替代：①heredoc（`<<'PY'`）在 PowerShell 双引号串里**是**可靠的；②查文件内容改用 **Grep 工具**而不是 shell grep（顺带避免把密钥打进日志）；③复杂逻辑一律落成 scratch 脚本文件再 `sh /tmp/x.sh`。
21. **评测指标的口径缺陷要在设计阶段就堵住**：`precision@k` 的分母若钉死在 `limit`，而每条查询标注了多个 expected，则该指标会退化成 recall 的线性函数（`precision@5 = recall@5 × |expected| / 5`），失去独立信息量。必须同时输出 `precision@returned`（分母 = 实际返回条数）。同理，任何"滤除噪声后精度提升"的预估都要先做**离线重打分**（直接重算已存档 JSON 里的 `rows[].top`，零服务端负载）再决定是否实跑——本轮正是靠离线重打分推翻了"滤除脚手架能把 G4 提到 0.79"的错误预估（那个 0.79 其实是 `precision@returned`）。

### 11.4 backend env 漂移已修复

Docker **永不刷新已创建容器的 env**。原 backend 容器创建于 `backend/.env` 加入 `AGENT_OPENVIKING_*` 之前，实测容器内 `AGENT_OPENVIKING_ROOT_API_KEY` **长度为 0** → 所有 admin 操作与 per-user key 派生都会静默失败。已用 `docker compose up -d --force-recreate --no-deps backend` 修复，现 7 项齐全：

| 变量 | 值长度 |
| --- | --- |
| `AGENT_OPENVIKING_ENABLED` | 4 |
| `AGENT_OPENVIKING_URL` | 22 |
| `AGENT_OPENVIKING_REST_URL` | 22 |
| `AGENT_OPENVIKING_MCP_URL` | 26 |
| `AGENT_OPENVIKING_ROOT_API_KEY` | 64 |
| `AGENT_OPENVIKING_ACCOUNT_ID` | 14 |
| `AGENT_OPENVIKING_ADMIN_USER_ID` | 14 |

这与 `container_manager._ov_env_stale()` 要解决的是**同一类问题在 backend 自身上的体现**：改 `.env` 后必须 recreate，`docker restart` 无效。

### 11.5 backend 镜像陈旧：Phase 0 代码此前**根本没在运行**

§11.4 只证明了 env 到位，**没有**证明代码到位。`docker-compose.yml:39` 是 `build: ./backend`，且 `volumes:` 只挂了 `docker.sock` / `backend-data` / 配置目录 —— **`app/` 没有源码挂载，是烘进镜像的**。因此 Phase 0 新增与修改的 6 个文件在旧镜像里全都不存在，实测：

```
MISSING app/services/ov_access.py
MISSING app/routers/ov_proxy.py
/app/app/config.py:0     ← grep -c openviking
/app/app/main.py:0
backend 日志零 openviking 行
```

这也解释了 §11.1 里"backend 日志无 openviking 相关行"为什么是空的 —— 不是日志级别问题，是**路由压根没注册**。`docker compose up -d --force-recreate` 不会重建镜像，必须显式 `docker compose build backend`。重建后：

```
PRESENT app/services/ov_access.py
PRESENT app/routers/ov_proxy.py
/app/app/config.py:12
/ov routes: ['/ov/api/v1/{rest}', '/ov/health', '/ov/mcp']
```

**硬约束（补进 §11.3）**：任何触碰 `backend/app/**` 的改动，验证前必须 `docker compose build backend && docker compose up -d backend`。只 recreate 会得到"代码看着对、行为完全没变"的假阴性，而且**不报错**。

### 11.6 OpenViking Studio 与三级密钥的数据面边界

**服务端自带 Web UI，但不在 openapi 里。** `GET /` → `302 /studio/`，名为 **OpenViking Studio**（manifest：`"OpenViking Studio — agent-native context database UI"`），Vite SPA，454 files / 14475 KB，位于 `/app/.venv/lib/python3.13/site-packages/openviking/web_studio/dist`。因为是 StaticFiles catch-all 挂载，**枚举 `/openapi.json` 的 126 条 paths 永远找不到它**（我最初据此误判为"没有自带管理 UI"）。另有 `/docs`、`/redoc`（Swagger UI，CSS/JS 走 `cdn.jsdelivr.net`，需外网）。

**Studio 是纯客户端凭据模型**，无服务端 session。登录表单字段（从 bundle `index-DCY8seVS.js` 提取）：`baseUrl` / `accountId`(placeholder `default`) / `apiKey`("Enter X-API-Key or Bearer token") / `adminApiKey`("Root or account-admin key") / `identityHeaders` 开关；凭据存 localStorage。发头逻辑：

```js
R = o.identityHeaders ? (o.adminApiKey || o.apiKey) : (o.apiKey || o.adminApiKey)
headers["X-API-Key"] = R
if (o.identityHeaders) { headers["X-OpenViking-Account"] = o.accountId
                         headers["X-OpenViking-User"]    = o.userId }
```

**root key 进不了数据面，且这是白名单硬编码的**（`server/auth/plugins/api_key.py`）：

```python
_API_KEY_ROOT_ALLOWED_PATHS    = {"/api/v1/system/status", "/api/v1/system/wait", "/api/v1/debug/health"}
_API_KEY_ROOT_ALLOWED_PREFIXES = ("/api/v1/admin", "/api/v1/observer", "/api/v1/console",
                                  "/api/v1/tasks", "/api/v1/system/backend", "/api/v1/system/sync")
```

`fs/*` `search/*` `stats/*` `content/*` `sessions/*` `webdav/*` 对 root 一律 `403 PERMISSION_DENIED`。同一文件的 `resolve_identity()` 还会**静默剥离**租户断言头：

```python
# Silently ignore identity assertion headers in api_key mode.
if x_openviking_account: _remove_header(request, b"x-openviking-account")
if x_openviking_user:    _remove_header(request, b"x-openviking-user")
```

⇒ Studio 的 `identityHeaders` 开关在 `auth_mode: api_key` 下**完全无效**；只有 `server/auth/plugins/trusted.py` 的 trusted mode 才认这两个头。**这把 §3.2 的 backend 代理设计从"推荐"钉成了"必需"**：容器内绝不可能拿 root key 越权读别人的记忆，因为 root key 连自己的都读不到。

实测三级密钥的能力矩阵（account `agent-platform`）：

| 端点 | root | admin (`platform-admin`) | user (`studio-debug`) |
| --- | --- | --- | --- |
| `/api/v1/admin/accounts/{a}/users` | 200 | 200 | **403 `Requires role: root, admin`** |
| `/api/v1/stats/memories` | **403** | 200 | 200 |
| `/api/v1/fs/ls?uri=viking://` | **403** | 200 | 200 |
| `/api/v1/system/status` | 200 `user=default` | 200 `user=platform-admin` | 200 `user=studio-debug` |

**⚠ Phase 5 待查项（P5 隔离）—— 已解决，见 §11.8**：admin 与 user 两把不同的 key 对 `fs/ls?uri=viking://` 返回了**完全相同**的根目录（`viking://agent`、`viking://resources`）。命名空间看起来是 **account 级而非 user 级**，用户隔离可能只发生在更深的子树上。必须在 P5 里逐层 `fs/tree` 定位真实的 per-user 边界；若确实同账户内互相可见，则平台"每用户独立 User Key 即隔离"的前提不成立，需要改为**每用户一个 account**。

> **结论（Phase 2 重验实测）**：该待查项的怀疑方向对了一半。`viking://agent` 与 `viking://resources` 确实是 account 级共享，但根目录列表当时被 `level_limit` 截断了 —— 还有一个 `viking://user` 作用域没显示出来，而 per-user 隔离正是发生在这一层，且是**强隔离**。"每用户独立 User Key 即隔离"的前提**成立**，不需要改为每用户一个 account。但由此产生一条新的写入路径铁律，见 §11.8。

**host 端口**：`docker-compose.yml` openviking 服务已加 `ports: ["127.0.0.1:1933:1933"]`（仅 loopback）。这**放宽了 §4.1 "不发布 host 端口" 的立场**，理由写在 compose 注释里：root key 路径受限 ⇒ 发布端口不暴露任何租户数据；真正无认证的是 `/health` `/ready` `/docs` `/redoc` `/openapi.json` `/metrics` `/studio/*`。共享机器上应删掉该块改用一次性 socat 旁挂。~~这不削弱 P2-06 要测的边界 —— agent 容器仍只能经 backend `/ov` 代理到达 1933。~~ **这句已被 P2-06 实测证伪**：agent 容器与 openviking 同在 `agent-net`，可直连 1933，代理**不是**唯一网络路径（但容器只持有 proxy token，直连拿不到任何租户数据）。compose 注释里对应的措辞已一并修正，详见 §11.7 P2-06。

### 11.7 Phase 1/2 重验最终结果

§11.5 的镜像重建之前，所有"已通过"都是在**没有 `/ov` 路由**的 backend 上测得的，因此 Phase 1/2 全部重跑。结果：**Phase 1 八项全通过，Phase 2 八项通过、一项（P2-06）判为风险**。逐项实测证据：

**Phase 1**

| ID | 实测 |
| --- | --- |
| P1-01 | `/ready` 在 poll #0（1s 内）即 200 |
| P1-02 | `checks` = `agfs{filesystem:ok, multiwrite_sync:not_supported}` / `vectordb:ok` / `api_key_manager:ok` / `embedding:ok` / `ollama:not_configured`；**响应里没有 `rerank` 键**（不是 `not_configured`，是键不存在）⇒ §3.5 的 rerank 对照实验需先确认服务端是否支持该配置项 |
| P1-03 | `{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key"}`，且 `/health` **免认证** |
| P1-04 | 五点采样 × 8s，`.State.StartedAt` 冻结、`health=healthy`、`RestartCount=0` |
| P1-05 | `docker compose rm -sf openviking` + 重新 `up`：14s healthy，`StartedAt` `06:35:53`→`07:06:28`，`vector/count` 36→37→**39**，`fs/stat` 仍找到探针文件 |
| P1-06 | `docker restart`：`/ready` 在 poll #9（**11s**）恢复，`healthy`、`RestartCount=0`；日志扫描 `corrupt`/`traceback`/`critical`/`fatal`/`agfs error`/`panic` **全空**；重启后 `vector/count=39`、探针文件仍在 |
| P1-07 | 见 §5 表格 |
| P1-08 | 见 §5 表格 |

两个由 P1-05 暴露的**行为契约**，Phase 3/4 的测量方法必须据此调整：

1. **`vector/count` 是异步的**。写入返回后计数仍在涨（36→37→39）。任何"写完立刻数向量"的断言都会假阴性；Phase 4 的索引延迟基准必须轮询到稳定值。
2. **`stats/memories` 统计的不是文件数**。写普通文件时它恒为 0；只有 `remember()` 抽取出的**结构化记忆**才计入（实测 `total_memories:2, by_category.preferences:2, hotness_distribution:{cold:0,warm:2,hot:0}`）。categories 固定 8 类，与 §5 Phase 3 的分类设计一致。

**Phase 2**

- **P2-01/02**：openviking **仅** `agent-net`，`ip=172.19.0.4`，`aliases=[agent-docker-demo-openviking-1, openviking]`；backend 双网 `agent-net 172.19.0.5` + `platform-net 172.18.0.3`；backend 内 `getent hosts openviking` → `172.19.0.4 openviking`
- **P2-03**：backend→OV `/ready` 200、`/health` 200 `v0.4.21`；`/ov/health` **无凭据 → 401** `{"error":{"message":"无效的记忆服务访问凭据，请联系管理员重建容器。"}}`（代理自身也要求凭据，符合设计）
- **P2-04**：新建探针容器 `agent-p2probe`（`ensure_container("p2probe")` → image `agent-demo:1.4.0`、`config_ok=True`）内：
  - `/ov/health` → `{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key","account_id":"agent-platform","user_id":"p2probe","role":"user"}`
  - `/ov/api/v1/system/status` → `{"initialized":true,"user":"p2probe"}` ⇒ **证明代理注入的是派生 user key，不是容器手里的 proxy token**
  - MCP `initialize` → 200，`serverInfo:{name:"openviking",version:"1.27.0"}`，`protocolVersion:2025-03-26`
  - env 注入**恰好 5 项**（与 `_openviking_env()` 源码一致）：`OPENVIKING_API_KEY`(len=100) / `OPENVIKING_AUTH_MODE=api_key` / `OPENVIKING_CLI_CONFIG_FILE=/opt/agent/builtin-plugins/openviking/ovcli.conf` / `OPENVIKING_MCP_URL=http://backend:8000/ov/mcp` / `OPENVIKING_URL=http://backend:8000/ov`。`OPENVIKING_ENABLED`/`_REST_URL`/`_ACCOUNT_ID`/`_USER_ID` **按设计不注入**
  - 容器工具链：`curl` `wget` `getent` `python3` `sh` 有，**`node` MISSING** ⇒ §10 结论 3（hook-only 强制）实测成立
- **P2-05**：`tools/list` → **15** 个：`add_resource, cancel_watch, edit, find, forget, glob, grep, health, list, list_watches, read, remember, search, tree, write`。**两处契约偏差**：
  - `add_skill` **上游不存在**，但 `ov_proxy._ALLOWED_TOOLS` 里列着它 ⇒ 陈旧项，应删（留着无害，但会误导后续维护者以为可用）
  - `forget` / `cancel_watch` **被 `tools/list` 广告出去，却在 `tools/call` 时被代理拒**（`-32601 平台未开放记忆工具 X。`）⇒ agent 会"看得见调不动"。**建议**：在 `tools/list` 响应里也过滤掉非白名单工具，而不是只在 `tools/call` 拦。这是本轮唯一发现的**产品级 UX 瑕疵**
  - 白名单外的 REST 路径正常拦截：`/ov/api/v1/admin/accounts` → **404** `{"error":{"message":"记忆服务不提供该接口。"}}`
- **P2-06（风险项，量化）**：`agent-p2probe` 在 `agent-net`（`172.19.0.6`），`getent hosts openviking` → `172.19.0.4`，直连 `/ready` **200（0.54s）**、`/health` 200。**代理不是唯一网络路径**。但数据面是安全的：

  | 无 key 直连结果 | 端点 |
  | --- | --- |
  | **200** | `/health`(72B) `/ready`(197B) `/metrics`(**135125B**) `/openapi.json`(**318828B**) `/docs`(1013B) `/redoc`(895B) `/studio/`(2525B) |
  | **401 / 165B** | `/api/v1/fs/ls` `/api/v1/stats/memories` `/api/v1/console/dashboard/summary` `/api/v1/system/status`（后者报 `UNAUTHENTICATED "Missing API Key when resolving identity."`） |

  ⇒ **无租户数据泄漏**。真实损失 = API 全貌 + 运行指标披露 + DoS 面。`/metrics` 泄漏样例：`openviking_embedding_calls_total{account_id="__unknown__",error_code="OK",model_name="qwen3.7-text-embedding",provider="openai"} 36`。**缓解选项**（未实施，留给 Phase 5 决策）：把 openviking 挪到只与 backend 相连的独立网络，或用网络策略仅放行 backend IP。
- **P2-07**：frontend **仅** `platform-net`（镜像 `agent-docker-demo-frontend`，`nginx -g daemon off;`，有 `getent`/`nslookup`/`wget`/`nc`，无 `python3`/`node`）；`openviking -> NO_DNS`、`wget: bad address 'openviking:1933'`；backend/postgres 可见（预期）
- **P2-08**：伪造 admin key（len=126）经代理 → **401**（`X-API-Key` 与 `Authorization: Bearer` 两种拼法都拒）；**同一把 key 直连 OV → 200 `user=platform-admin`** ⇒ 401 确实来自 `_REQUEST_DROP_HEADERS` 丢弃 + `_upstream_headers` 覆写，不是 key 无效
- **P2-09**：51702 CJK 字符 / 145312 UTF-8 字节经 MCP `write` 通过代理写入 `viking://user/p2probe/resources/p2/big-chunk.md`；代理读回 **146586 bytes / 51702 chars / `identical=True`**；直连读回同为 146586 bytes（http=200，0.006s）；代理读 elapsed 0.02s ⇒ **无截断、无重编码，代理开销 ~14ms**
  - **踩坑记录**：首测用裸 `GET /ov/mcp` + `Accept: text/event-stream` → `curl: (28) timed out after 8002ms with 0 bytes`。OV 走 **streamable HTTP POST**，不支持独立 GET SSE 流；响应头为 `content-type: text/event-stream` + `transfer-encoding: chunked` + `x-accel-buffering: no`，且**无 `mcp-session-id`（无状态）**

**REST 面是只读的（对容器而言）**：`_ALLOWED_REST` 8 条里**不含 `/api/v1/content/write`** ⇒ 容器只能经 MCP `write`/`remember`/`edit`/`add_resource` 写入。**Phase 3 灌语料必须走 MCP，或用 host key 直连**。另：`/api/v1/search/grep` 的必填参数是 **`uri`**，不是 `target_uri`（用错会报 `body.uri: Field required`）。

### 11.8 OpenViking 作用域模型与 search 默认作用域（Phase 3 铁律）

§11.6 的待查项在此收口。实测三个作用域的语义（**引号内是服务端自己生成的 abstract 原文**）：

| 作用域 | 语义 | 隔离性 |
| --- | --- | --- |
| `viking://user/{uid}` | "User scope. Stores user's long-term memory, persisted across sessions." | **per-user 强隔离** |
| `viking://agent` | "Shared agent capabilities and configuration available across the account." | **account 级共享**，同账户任何 user key 都能 `ls` + `read` |
| `viking://resources` | "Resources scope. Independent knowledge and resource storage, not bound to specific account or Agent." | account 级/更宽共享 |

`viking://user/{uid}` 子树：`memories/` `peers/` `privacy/` `resources/`，外加脚手架 `.abstract.md`(L0) / `.overview.md`(L1)。首次访问会自动生成 `memories/identity.md`(439B) 与 `memories/soul.md`(1180B) 模板（内容为英文）。`remember()` 抽取后落 `viking://user/{uid}/memories/{category}/{peer}/{中文标题}.md`。

**跨用户隔离实测（用 `studio-debug` 与 `studio-other` 两把 key）**：

| 操作 | 结果 |
| --- | --- |
| K2 默认 `search` | 只见自己的记忆 |
| K2 显式 `target_uri: viking://user/studio-debug` | **403 PERMISSION_DENIED** |
| K2 `content/read` K1 的文件 | **403** |
| K2 `fs/ls viking://user/studio-debug` | **403** |
| K1/K2 `fs/ls` + `read` `viking://agent/memories/p1` | **两把 key 都能列出并读取** |

⇒ `viking://user/{uid}` 是真正的租户边界；`viking://agent` / `viking://resources` 是共享区。

**⚠ Phase 3 写入路径铁律 —— `search` 的默认作用域是调用者自己的 `viking://user/{uid}`**：

- 写到 `viking://agent/...` → `context_type: "resource"`，**默认 search 搜不到**；必须显式传 `target_uri: "viking://agent"` 才命中（实测 score 0.657）
- 写到 `viking://user/{uid}/memories/...` → `context_type: "memory"`，默认 search 命中 `memories[]`（实测 score 0.680）
- 响应结构：`{"status","result":{"memories":[...],"resources":[...]}}`，每条含 `context_type`/`uri`/`level`(0/1/2)/`score`/`abstract`/`tags`

**这就是上一轮"写入成功但搜不到"的根因**，不是索引故障。⇒ **平台侧任何自动写入（`autoCapture` / 语料灌入）必须落 `viking://user/{uid}/`，落到 `viking://agent` 会既搜不到、又跨租户可见。** Phase 5 须专门验一条："平台写入路径不落 `viking://agent` 与 `viking://resources`"。

**写入延迟量级（Phase 4 基线，均为实测单点）**：

| 路径 | 作用域 | `wait` | semantic | 延迟 |
| --- | --- | --- | --- | --- |
| `content/write` | `viking://agent/...` | true | `complete`（走 VLM 摘要） | **15324 ms** |
| `content/write` | `viking://user/{uid}/memories/...` | true | **`skipped`** | **269 ms** |
| MCP `write` 145KB | `viking://user/{uid}/resources/...` | 默认 false | `queued` | **< 0.1 s** |
| `remember` 2 messages | — | — | 异步抽取 | 提交即返回；**抽取完成时间未测准**（首次轮询 t=0s 已命中上一批产物） |

⇒ **57 倍的延迟差全部来自 semantic 处理是否触发**，不是体积。Phase 4 的延迟基准必须按 `processing_mode` 与 `wait` 分层统计，否则数字没有意义。

**中文语义召回的初步信号**（单点，**不是**基准，仅供 Phase 3 定标）：

| 查询 | 命中 | score |
| --- | --- | --- |
| 「网关重试间隔是多少」（原文高度重合） | `网关重试策略.md` | **0.89**（MCP 面呈现为 `[memory 89%]`） |
| 「数据库连接池上限是多少」（原句） | 对应记忆 | 0.680 |
| 「连接池最多能开多少个连接」（同义改写） | 同上 | 0.632 |
| **同用户作用域**的 `.overview.md` 脚手架 | **噪声，但越过阈值** | **0.61** |
| 无关脚手架 `.abstract.md`（agent 作用域探针） | 噪声 | 0.25 – 0.33 |

⇒ 同义改写相对原句仅衰减 0.048，中文语义召回本身可用。但**"0.35 能切开信号与噪声"这个判断已被 0.61 那个数据点推翻**：同一用户作用域下的 `.overview.md` 脚手架得分 0.61，落在真实命中区间（0.63–0.89）的下沿，`scoreThreshold` 无论怎么调都切不开它。0.25–0.33 只是**跨作用域无关**脚手架的底噪，不是本用户作用域的噪声上界。Phase 3 必须正式量化 Recall@K / MRR，并把"按 URI 过滤脚手架"作为与阈值并列的手段（R15 / P3-11）。

### 11.9 CJK 正确性：码点级双向验证（附一条被撤销的误报）

**上一轮曾记录"`remember()` 抽取产物 CJK 乱码（UTF-8 被按 GBK 解码）"，形如 `缃戝叧閲嶈瘯绛栫暐.md`（应为 `网关重试策略.md`）。该结论已被码点级证据推翻并撤销。**

验证方法：不看渲染出来的字符，直接打印 `U+XXXX` 码点。参考值 `网=U+7F51 关=U+5173 重=U+91CD 试=U+8BD5`；若真被 GBK 误解码则会是 `缃=U+7F03 戝=U+621D 叧=U+53E7`。四条独立路径全部返回**正确码点**：

| 路径 | 结果 |
| --- | --- |
| backend 容器 → **直连** OV `/api/v1/fs/ls` | `['日志保留策略.md', '网关重试策略.md']`，码点 `U+7F51 U+5173 U+91CD U+8BD5 U+7B56 U+7565` |
| backend 容器 → **直连** OV `/api/v1/search/search` + `content/read` | abstract 码点 `U+7F51 U+5173 U+91CD U+8BD5 U+91C7 U+7528 U+6307 U+6570 U+9000 U+907F U+FF1A` = 「网关重试采用指数退避：」 |
| 用户容器 → **经 `/ov` 代理** REST search | `abstract txt: - 网关重试采用指数退避：初始间隔 200ms，最多重试 6 次。` |
| 用户容器 → **经 `/ov` 代理** MCP `search` | `[memory 89%] viking://user/p2probe/memories/preferences/user/网关重试策略.md` |

⇒ **存储层、抽取层、代理层、传输层的中文全部干净**；`remember()` 由 LLM 生成的**中文文件名与中文 abstract 均正确**。P2-09 的 51702 CJK 字符往返 `identical=True` 是同一结论的独立佐证。

**误报根因**：那条乱码出自读取 `/tmp/p2e.out` 的环节 —— 该文件里对应片段是 `printf '%s' "$r" | head -c 900` 输出的**裸 JSON 字节流**，在读取时被按 CP936 解码。已排除的层面：容器 locale（openviking 容器 `LANG`/`LC_ALL`/`LC_CTYPE` **全空**、`locale` = POSIX，但 Python `stdout.enc=utf-8 fs.enc=utf-8 pref=utf-8` ⇒ **编码不依赖 locale**）；PowerShell 控制台（`chcp` = **65001**，且同一命令下中文显示正常）。

由这次误报提炼出的三条工装约束已写入 **§11.3 第 7–9 条**（码点判定 / 禁用 `head -c` 截 CJK / 读 `/tmp` 走文件复制）。

### 11.10 MCP 面契约（实测，供 Phase 3 直接使用）

`tools/list` 的 `inputSchema` 必填项与 REST 面**不一致**，照 REST 的字段名调 MCP 会 pydantic 报错：

| 工具 | required | 可选 |
| --- | --- | --- |
| `remember` | `["messages"]` — **唯一字段** | 无。传 `{content: ...}` → `messages Field required` |
| `write` | `["uri", "content"]` | `mode` / `wait` / `timeout` |
| `read` | `["uris"]`（**数组**） | `offset` / `limit` |
| `search` | `["query"]` | **21 个**可选，比 REST 面多出 `min_score`、`detail_by_category`、`other_peer_penalties` |

`remember` 正确调用形态（经代理）：

```sh
curl -sS -m 40 -X POST http://backend:8000/ov/mcp \
  -H "X-API-Key: $OPENVIKING_API_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"remember",
       "arguments":{"messages":[{"role":"user","content":"…"},{"role":"assistant","content":"…"}]}}}'
```

返回 `"Stored 2 message(s) and committed for memory extraction."`，**抽取是异步的** —— 紧接着的 `search` 仍会 `memories:[]`。Phase 3 若用 `remember` 灌语料，必须在提交与查询之间插入轮询等待，否则 Recall@K 会被系统性低估。

`tools/call health` → `"OpenViking is healthy (service initialized, storage: VikingFS)"`。MCP 文本结果里 score 以**整数百分比**呈现（`[memory 89%]`），REST 面是小数（`0.89`）；跨面对比分数时须先归一。

### 11.11 Phase 0 收尾：agent 镜像构建 + 容器内端到端门禁（实跑）

`bash scripts/build-agent.sh` → `EXIT=0` / `== done ==` / 242 行日志 / `--network=host` 兜底未触发。产物 `agent-demo:1.4.0` = `sha256:f7819d254443970969cca329a4c68ffa40e400c14f859c55ba98b191b8a5ac4f`（386253582 B，`created=2026-09-28T15:48:47+08:00`）。旧镜像 `sha256:6a886c03aca2…` 已被构建替换。

**镜像内 present-file 门禁（§10 结论 3 的 6 项断言）全部 PRESENT**：`index.mjs`、`lib/config.mjs`、`lib/mcp-config.mjs`、`package.json`（`"version": "2026.9.25-2"`）、`ovcli.conf`、`builtin-plugins/openviking/package.json`。`ovcli.conf` 内容与设计一致：`{"plugin": {"opencode": {"mcpEnabled": false, "repoContext": false, "dataDir": "/data/state/openviking"}}}`。（⚠️ P3-G 之后该文件多了第 4 个键 `"minQueryLength": 2`，本节记录的是当时的快照；镜像已重建为 `sha256:93d984254d3e`，构建门禁复跑通过，见 §11.12.10 G。）

**容器内端到端验证**（`agent-p2probe` 经 admin "update image" 路径 recreate 到新镜像后实测）：

| 检查点 | 实测 |
| --- | --- |
| 容器镜像 ID | `sha256:f7819d25…` = 新镜像；`Created` `07:20:02Z` → `08:07:46Z` |
| 插件包目录 | `index.mjs` `lib/`（12 文件）`lib/shared/`（22 文件）`servers/mcp-proxy.mjs` `scripts/` `INSTALL-ZH.md` |
| `node` 在 PATH | **否** ⇒ `mcpEnabled: false` 之外还有第二重保险，hook-only 被强制（§10 结论 3） |
| `opencode` 在 PATH | `/usr/local/bin/opencode`（内嵌 Bun 负责加载 ESM 插件） |
| 注入的 `opencode.json` | `/data/config/opencode/opencode.json`，**2216 B**，`-rw------- agent agent` |
| `plugin` 数组 | 3 项，含 `/opt/agent/builtin-plugins/openviking/node_modules/@openviking/opencode-plugin`（与 manifest `path` 逐字一致） |
| `mcp` servers | `['demo-mcp', 'openviking', 'web_search']` |
| `mcp.openviking` | `type=remote`，`url=http://backend:8000/ov/mcp`，`enabled=true`，`headers={'X-API-Key': <len 100>}`（Fernet 封装的 per-user proxy token，非 OpenViking Key） |
| `permission["openviking_*"]` | `"allow"`（与用户隐藏 MCP 时写入的 deny 键同形，§10 结论 5） |
| 容器 env | 恰好 5 个 `OPENVIKING_*`，与 `_openviking_env()` 源码一致；`OPENVIKING_ENABLED` / `_REST_URL` / `_ACCOUNT_ID` / `_USER_ID` 按设计**不下发** |

⇒ **Phase 0 接入改造至此全部落地并验证**。p0-16（agent 镜像构建）关闭。

#### 发现 A（R17）：**运行中**的容器不会因镜像重建而 recreate —— 但**已停止**的会自愈

`container_manager._ensure_container_sync()` 的两条路径不对称：

- **停止**的容器 → 走 `_needs_recreate()`（:753），其中第一项就是 `_image_stale()`（:648），比较镜像 **ID**，命中即 `remove(force=True)` 重建。
- **运行中**的容器 → 走 :715-740 的 fast path，**只**检查 `_fastk_env_stale` 和 `_ov_env_stale`；两者都为 False 时直接 `return container, password, True`，**既不检查镜像，也不重新注入 `opencode.json`**。

实测复现（running 路径的假阳性）：新镜像构建完成后对当时**正在运行**的 `agent-p2probe` 调 `ensure_container("p2probe", cfg)` → 返回 `config_ok=True`，但容器 `Image` 仍是旧的 `sha256:6a886c03aca2…`、`Created` 时间戳未变、容器内 `/opt/agent/builtin-plugins/openviking/` **不存在**、`opencode.json` 仍是旧的 1948 B 且 `mcp` 只有 `['demo-mcp','web_search']`。只有走 `recreate_container()` + `ensure_container()`（即 `agent_controller.recreate_for_user()` / `POST /admin/.../recreate`，注释自称 *"Admin 'update image' action after a rebuild"*）才真正换到新镜像。

这是**有意设计**（:716-723 注释：安全与正确性优先于会话连续性，但只针对"凭据不可用"这类必须立刻修的漂移；镜像升级会打断正在进行的会话，故留给管理员显式触发），不是 bug。运维手册推论：

> **`bash scripts/build-agent.sh` 之后，Phase 0 的插件接线对"重建当时正在运行"的 agent 容器不生效**，必须逐个走 admin "update image"。**已停止的容器无需处理** —— 下次启动时 `_needs_recreate` 会自动迁移。

**严重度下调依据（实测）**：本项曾被记为"4 个真实用户容器待 recreate"，那是**误判** —— 只看了 `docker ps -a` 的镜像列，没查 `.State.Status`。逐个 `docker inspect` 后发现 **6 个用户容器全部处于 `exited`**，只有测试探针 `agent-p2probe` 在跑。对每个容器直接求值 `_needs_recreate()`（只读）：

| 容器 | 状态 | 镜像 | `_needs_recreate` |
| --- | --- | --- | --- |
| `agent-p2probe` | running | `f7819d254443` | `False` |
| `agent-f4f5d8ce-…`（caesar, admin） | exited | `6a886c03aca2` | `True` — `stale image (want agent-demo:1.4.0)` |
| `agent-3c2e2f2a-…`（caesar789, user） | exited | `6a886c03aca2` | `True` — 同上（**已实跑自愈验证**） |
| `agent-f0b2f7e5-…`（caesar456, user） | exited | `6a886c03aca2` | `True` — 同上 |
| `agent-20873ad8-…`（123456, admin） | exited | `6a886c03aca2` | `True` — 同上 |
| `agent-92396af7-…`（admin） | exited | `6a886c03aca2` | `True` — 同上 |
| `agent-982aafb0-…`（delcheck…，测试号） | exited | `f1ebcc6a7803`（更旧） | `True` — 同上 |

⇒ **无待办项**。自愈路径已用真实用户 `caesar789`（`3c2e2f2a-…`）实跑验证：走**普通** `agent_controller.start_for_user()`（不是 admin 端点）→ 容器被重建到新镜像（`Created` `2026-09-20T03:35:50Z` → `2026-09-28T09:38:11Z`，`Image` = `sha256:f7819d25…`），启动期打出 3 次 `Cannot connect to container … /api/health` 后健康探针通过，返回 `{"running": true, "healthy": true}`。容器内实测：

| 检查点 | caesar789 自愈后实测 |
| --- | --- |
| 插件 | 目录存在；`index.mjs` / `lib/config.mjs` PRESENT；版本 `"2026.9.25-2"`；`ovcli.conf` 与仓库一致 |
| `node` 在 PATH | **否** |
| `opencode.json` | **2256 B**（比探针的 2216 B 大，因该用户 DB 里有额外配置），`-rw------- agent agent` |
| `mcp` / `plugin` | `['demo-mcp','openviking','web_search']` / 3 项且含 openviking |
| `mcp.openviking` | `remote` → `http://backend:8000/ov/mcp`，`enabled=true`，`headers={'X-API-Key': <len **140**>}` |
| `permission["openviking_*"]` | `allow` |
| env | 恰好 5 个 `OPENVIKING_*`；`OPENVIKING_URL=http://backend:8000/ov`、`_MCP_URL=…/ov/mcp`、`_AUTH_MODE=api_key`、`_CLI_CONFIG_FILE=/opt/agent/builtin-plugins/openviking/ovcli.conf`；`_ENABLED`/`_REST_URL`/`_ACCOUNT_ID`/`_USER_ID` 全部 UNSET |
| 数据面 | `GET /ov/health` → 200 `{"version":"v0.4.21","account_id":"agent-platform","user_id":"3c2e2f2a-…","role":"user"}`；`POST /ov/api/v1/search/search` → 200，`memories=0 resources=2` |

验证完 `stop_for_user()` 已把状态还原为 `exited`（镜像保留在新版）。

> **旁证**：proxy token 长度随 `user_id` 长度变化 —— 探针（`p2probe`，7 字符）是 100，UUID 用户（36 字符）是 140。因为令牌是 `encrypt_secret("ovproxy:<uid>")` 的 Fernet 密文，明文越长密文越长。任何按固定长度断言 token 的测试都会误判。

> **Phase 3 数据点**：一个从未写过记忆的真实用户，`search` 仍返回 `resources=2` —— 即同用户作用域下的脚手架噪声底确实存在，与 R15 一致。

#### 发现 B：此前 §11.7 的 P2-04 结论需限定适用范围

P2-04 是在 `agent-p2probe` **旧镜像**（无插件）上测的。因此：

- **仍然成立**：网络可达性、`/ov` 代理两个面的行为、5 个 env 注入、`/ov/health` 返回的 `account_id`/`user_id`/`role`、容器工具链清单（无 `node`）。这些都只依赖 backend 代理 + env，与镜像里有没有插件无关。
- **不成立 / 尚未测**：任何关于**插件本身运行时行为**的判断——`autoRecall` 是否真的在会话前置注入记忆、`autoCapture` 是否真的落盘、`profile-inject` 的 CJK 预算裁剪是否生效、`loadConfig()` 在真实容器 env 下解析出的 `credentialSource:"env"` 是否走通。§11.7 里引用的 `loadConfig()` 结果来自**静态读源码 + 模拟 env**，不是插件在容器里真跑过。⇒ 这些归入 **P5-10 / Phase 5**，且必须在 recreate 到新镜像的容器上测。

#### 工装教训

本轮踩到的五条（`tr -d "\r"` 吃掉所有 `r`、`${v:+…}${v:-…}` 泄漏凭据、`container.image` 抛 `ImageNotFound`、运行中容器不换镜像导致假阳性、只看 `docker ps -a` 的 IMAGE 列导致 R17 严重度高估）已并入 **§11.3 第 11–15 条**。

### 11.12 Phase 3 实测记录（中文记忆功能 / rerank A/B / R15 量化）

#### 11.12.1 评测工装

**评测器**：`benchmark/ov-zh/run_eval.py`，纯 HTTP，不依赖插件、不依赖 backend 代理（直连 `http://127.0.0.1:1933`）。

| 项 | 形态 |
| --- | --- |
| 数据文件 | `corpus.jsonl`（40 条，字段 `category`/`slug`/`id`/`content`）、`queries.jsonl`（30 条，字段 `id`/`group`/`query`/`expected`） |
| 运行模式 | `--mode {seed,eval,both}`；`seed` 灌库、`eval` 只检索 |
| 索引就绪判定 | `wait_index()` 轮询 `/api/v1/debug/vector/count`，**连续 3 轮不变**才算稳（embedding 异步，写返回后计数仍会涨；上限 180s） |
| 开关 | `--limit`（默认 5）、`--filter-scaffold`、`--overfetch N`、`--label`、`--out` |
| 凭据 | 从 `os.environ["OV_API_KEY"]` 读；缺失即 `FATAL` + `exit(2)`。**root key 不能写数据面（403），必须用派生 User Key** |
| 阈值判定 | `chk()` 7 项：G1 recall@5/mrr、G2 recall@5/mrr、G3 recall@5、G4 precision@5、G4 precision@returned |

**脚手架口径（重要，保证跨态可比）**：`scaffold_slots` 恒在**滤除前**的 `window[:limit]` 上计算。这样 rerank ON/OFF、`--filter-scaffold` 开/关、`--overfetch` 0/3/5 五种组合的噪声率都在同一把尺子上，可以直接横比。`is_scaffold()` 判 `.abstract.md` / `.overview.md`；`norm_uri()` 剥掉这两个后缀再 `rstrip("/")`，因此脚手架命中**能**被正确识别为"指向某个真实文档"，但**不计入** found（它不是 expected）。

**User Key 派生（踩坑点）**：`ov_access.derive_user_key()` 是纯本地函数，但**单独调用得到的 key 会被 OV 拒（`UNAUTHENTICATED / Invalid API Key`）**——必须先在 backend 容器里跑 `asyncio.run(ov_access.user_api_key(uid))`，它内部会走 `ensure_account()` + `_register_user()`（打 Admin API，用 root key）把账户和用户注册进去，之后本地重算值才生效。**因此该调用要求 OV 可达**；栈被重启时它会抛 `OvUnavailable`。脚本必须带重试（§11.3 第 19 条）。

**运行方式**（scratch 脚本 → WSL）：

```
wsl -e bash -lc "cd /mnt/d/Project/agent-docker-demo && python3 -m py_compile benchmark/ov-zh/run_eval.py && echo COMPILE_OK && tr -d '\r' < _scratch_pXX.sh > /tmp/pXX.sh && sh /tmp/pXX.sh > _scratch_pXX.out 2>&1; echo MARKER_DONE"
```

**存档输出**：`_scratch_p3i.out`(268) / `p3j`(95) / `p3k`(70) / `p3l`(14) / **`p3m`(481，A/B 权威)** / **`p3n`(113，离线重打分权威)** / **`p3o`(166，滤除矩阵权威)**；`benchmark/ov-zh/out-*.json`（`seed`、`eval-off`、`eval-on`、`baseline`、`filt-of0`、`nofilt-of3`、`filt-of3`、`filt-of5`）。

#### 11.12.2 P3-13 rerank A/B（权威数据，`_scratch_p3m.out`）

同一容器、同一镜像 digest、同一份干净 40 条语料，只切换 `OV_RERANK_API_KEY` 空/非空，各跑 30 查询：

| 指标 | rerank **OFF** | rerank **ON** | Δ |
| --- | --- | --- | --- |
| ALL hit@5 | **0.933** | 0.633 | −0.300 |
| ALL recall@5 | **0.892** | 0.517 | −0.375 |
| ALL mrr | **0.933** | 0.578 | −0.356 |
| ALL precision@5 | **0.273** | 0.153 | −0.120 |
| ALL scaffold_rate | **0.253** | 0.280 | +0.027 |
| ALL avg_returned | **5.00** | 4.40 | −0.60 |
| ALL p50 | **136 ms** | 3075 ms | **22.6×** |
| ALL p95 | **248 ms** | 3784 ms | 15.3× |
| 阈值判定 | **5 PASS / 2 FAIL** | **0 PASS / 7 FAIL** | |

分组 recall@5 / mrr（OFF → ON）：G1 `1.000/1.000 → 0.750/0.750`；G2 `1.000/1.000 → 0.625/0.562`；G3 `0.750/0.750 → 0.250/0.188`；G4 recall `0.792 → 0.417`、p@5 `0.633 → 0.333`、p@ret `0.633 → 0.522`。
分组 p50（OFF → ON，倍数）：G1 `177→3142`（17.8×）、G2 `138→3028`（21.9×）、G3 `169→3211`（19.0×）、G4 `129→2993`（23.2×）。p95：G1 `233→3534`、G2 `248→3334`、G3 `185→3575`、G4 `131→3075`。

**逐查询归因**（最能说明问题的三个数字）：

- **只有 OFF 命中：10 条** —— Q05 Q06 Q13 Q14 Q15 Q19 Q21 Q22 Q23 Q24，expected **全部落在 `tools` / `patterns` / `skills`**。
- **只有 ON 命中：1 条** —— Q18。
- **两态都 miss：1 条** —— Q20「上次挂的那回」exp=C16（OFF 的 #1 是 `memories/cases/.overview.md` 脚手架，ON 的 #1 是 C10）。这是**唯一真实的语义难点**（口语省略 + 指代），与 rerank 无关。

⇒ 净损失 9 条，且损失**高度集中在四个分类**，这不是"精排把分数排错了"的形态，是"整棵子树没被访问"的形态。追查下去就是 R18。

**rerank 成本**（`/metrics`）：`rerank_calls_total=297`、`rerank_tokens_total=99108`、`retrieval_rerank_used_total=30` ⇒ **每查询约 9.9 次调用 / 3304 tokens**。

**确定性验证**：本轮 `baseline` 复跑得到 ALL `recall@5=0.892 / mrr=0.933 / p@5=0.273 / p50=137ms`，与 p3m 存档的 OFF 运行（p50=136ms）**逐项一致（零方差）** ⇒ **OFF 态检索是确定性的**，上表数字可复现。

#### 11.12.3 rerank 配置模型与"永久关闭"的落地形态

**配置模型**（源码定位，v0.4.21）：

- `RerankConfig` 在 `openviking_cli/utils/config/rerank_config.py:8`，挂 `ov.conf` **顶层 `rerank` 键**（`open_viking_config.py:196`），`extra:"forbid"`。
- **旋钮陷阱**：`_effective_provider()` 第二分支 `if self.api_key: return "cohere"` ⇒ **唯一安全 off 态是 `api_key` 为空**，没有 `enabled: false`。
- rerank 的 4 个生效点全要求 `self._rerank_client and mode == RetrieverMode.THINKING`（`hierarchical_retriever.py:248 / :278 / :328 / :525`）。
- 相关常量：`MAX_CONVERGENCE_ROUNDS=3`、`DIRECTORY_DOMINANCE_RATIO=1.2`、`GLOBAL_SEARCH_TOPK=10`、**`MAX_PARALLEL_CHILD_SEARCHES=4`**、`LEVEL_URI_SUFFIX={0:".abstract.md", 1:".overview.md"}`。
- **`RetrievalConfig` 只有 5 个字段**：`hotness_alpha`、`score_propagation_alpha`、`recall_intent_timeout_s`、`recall_rewrite_timeout_s`、`enable_intent`。⚠️ 下面这些是 **`OpenVikingConfig` 的顶层键，不在 `retrieval` 段下**（§9 原表把它们写成 `retrieval.*` 是错的，已订正）：`enable_watch_scheduler=True`、`auto_generate_l0=True`、**`auto_generate_l1=True`**、`default_search_mode="thinking"`（**死配置**）、`default_search_limit=3`、`language_fallback="en"`（deprecated）、**`output_language_override=""`**。顶层共 77 个 public 属性。
  > `output_language_override` 的源码注释是 "bypasses content-based language detection … forces this language instead … (e.g., 'en','zh-CN','ja')"。**原以为它是解开 R18 盲区 / 中文化的正路，P3-C 已实测推翻**：它只作用于**由抽取 prompt 的 `{{ language }}` 变量驱动的生成物**（记忆正文、中文文件名、资源摘要），对 `.overview.md` 的**结构性英文标题完全无效**——因为 `generate_overview` 的渲染上下文是 `overview_context = {"memory_type", "directory_name", "items"}`，**根本没有 `language` 键**，且 `grep output_language memory_updater.py` → **零命中**。它是**全租户硬强制**（不是 account 级），副作用大于收益 ⇒ **决定不启用**。完整可改性矩阵与证据见 §11.12.9。
  > 同理 `auto_generate_l1=True` 的原判「只有 `remember` 抽取路径会触发生成，`content/write` 不会」**也已被推翻**：两条写入路径都会调 `generate_overview`（抽取路径 `memory_updater.py:1043`；写入路径 `:799` 的 `refresh_schema_overview` classmethod，由 `content_write.py:536`/`:1270`、`fs_service.py:425`/`:433` 调用）。**真正决定生成与否的是 registry 里该 memory type 有没有 `overview_template`**（§11.12.4 第 3 步）。

**可用端点**（DashScope）：`POST https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` → 200。MaaS 网关的 `/compatible-mode/v1/rerank[s]`、`/api/v1/services/rerank` 全 404/400。`OpenAIRerankClient` 用**路径标记**（`_DASHSCOPE_NATIVE_PATH_MARKERS = ("/api/v1/services/rerank",)`）决定是否套 nested envelope——**换网关地址就会静默换请求体形状**。

**观测面**：只有 `/metrics` 的 gauge 可靠。`/ready` 的 `checks` 两态**完全相同**（`['agfs','api_key_manager','embedding','ollama','vectordb']`，永不含 rerank）；`/api/v1/debug/health` 只回 `{"status":"ok","result":{"healthy":true}}`；`rerank_call_duration_seconds` 全落 `le="0.01"` bucket（`_sum=0.0`），分辨率不足。

**落地形态**（三重验证通过）：

- `.env` line 10–11：
  ```
  # disabled by P3-13 A/B (recall@5 0.892->0.517, p50 22.6x); re-enable by uncommenting
  #OV_RERANK_API_KEY=sk-****
  ```
  **注释掉而非清空**，key 值原地保留，复现实验只需去掉 `#`。CR 计数 = 0；`grep -c '^OV_RERANK_API_KEY=' .env` = 0。
- `docker-compose.yml` 的 rerank 注释块尾部写入 15 行实测依据（数字 + THINKING 盲区机制 + "不要在没重跑 A/B 的情况下打开"），紧邻 `- OV_RERANK_API_KEY=${OV_RERANK_API_KEY:-}`。**注释写在 compose 里而不是只写在文档里**，因为改 `.env` 的人不一定会翻 docs。
- 运行时断言：`env -u OV_RERANK_API_KEY docker compose config` → CONFIG_OK；`openviking_model_usage_available{model_type="rerank",valid="1"}` = **0.0**（两轮各验一次）；容器 `Status=running RestartCount=0`；`/health` → `{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key"}`。

#### 11.12.4 R18 类目盲区：四步机制链（已完全查清）

1. **配了 rerank ⇒ 自动切 THINKING，且无 per-request 退出口**。`hierarchical_retriever.py:127` 按"有没有 rerank client"决定模式。`SearchRequest.mode` 的 enum 是 `["list","context"]`——那是**响应形状**，不是检索模式；传 `thinking` / `quick` 一律 **HTTP 400**。
2. **THINKING 按 URI 目录层级下降，靠 `.overview.md` 给目录节点打分**决定是否进子树。实测 `tools` / `patterns` / `skills` / `profile` 四类**根本没有 overview**：
   - `content/read` → `NOT_FOUND: viking://user/ovzh-eval/memories/tools/.overview.md`
   - `content/overview` GET → `[Directory overview is not ready]`（**且这个端点不触发生成**）
   - 只有 `cases` / `events` / `entities` / `preferences` 有。
3. **overview 由 registry 的 `overview_template` 守卫，与写入路径无关**（原判「只由 `remember` 抽取路径生成、`content/write` 永不生成」**已被推翻**）。真正的守卫在 `memory_updater.py:1570-1572`：
   ```python
   memory_type = cls.memory_type_from_uri(directory_uri)   # :845-853 纯词法：parts.index("memories") → parts[idx+1]
   if not memory_type: return False
   schema = registry.get(memory_type)
   if not schema or not schema.overview_template:
       logger.debug(f"No overview_template for memory type: {memory_type}")
       return False
   ```
   live 默认 registry **只有 9 个 memory type**：`entities`（overview YES 249B）、`events`（YES 144B）、`soul`（NONE）、`cases`（YES 156B）、`trajectories`（NONE）、`identity`（NONE）、`experiences`（NONE）、`preferences`（YES 324B）、`profile`（NONE）⇒ **只有 4 个有模板**。`tools.yaml` / `skills.yaml` **磁盘上存在但未注册**；`patterns` **连 YAML 都没有**。
   **2×2 交叉实验**（同一 `write` 通道、只差类目）：MCP `write` 进 `cases/` → `overview=complete`；进 `tools/` → `overview=skipped`。⇒ 与 `semantic_status` 无关，**是 registry 缺口**。
   **附带发现（双 registry 不一致）**：写入路径的 `refresh_schema_overview` 是 classmethod，内部 `from … import get_default_registry` **硬编码 default registry**；抽取路径走 `compressor_v3.py` → `resolve_account_memory_registry(viking_fs, ctx.account_id, get_default_registry())` → `streaming_memory_updater.py:507`，**用 account registry**。⇒ **account 级模板覆盖只对 `remember` 抽取路径生效**，对 `content/write` 无效。
4. **无 overview 的目录无法打分 ⇒ 子树永不下降 ⇒ 这四类在 THINKING 下系统性不可见**。30 条查询里 10 条的唯一命中就在这四类（§11.12.2）。

**第二独立机制**：THINKING 下默认 `score_threshold` 会截断结果（rerank 把分数压到 0.08–0.18）。量化：默认 `total=1 returned=1`；`{"score_threshold":0}` → `total=5 returned=5`；`{"score_threshold":0.05}` → 同。⇒ 即使子树被访问，分数也会被阈值砍掉。

**兜底手段（THINKING 下有效，但 rerank OFF 下是负收益）**：定向 `target_uri` 可绕过剪枝。Q05 + `target_uri=memories/tools` → **0.8967 `tools/压测工具wrk.md` 排 #1**；而默认 scope 只有 `total=3`、best **0.1305**。⚠️ **但生产形态是 rerank OFF（永久关闭），此时 `target_uri` 定向反而有害**：`p3c-probe` 的 5 组对照里，定向到"预期类目"有 3 组返回 `hits=0`，2 组把**错误文档**顶到 #1，而无定向时全部命中正确文档（rerank OFF 下未注册类目照样以 `0.8567` 排 #1，根本不需要 `target_uri`）。完整对照表见 §11.12.8。⇒ **R18 与 R3 是同一根杠杆，rerank 一关，R18 的实际影响面收敛为零**，这也是"永久关闭 rerank"的第二个独立理由。

**已实测排除的对策**（别再试）：`content/reindex`（返回 `mode:"vectors_only" scanned 50 rebuilt 47 1941ms`，**不生成 overview**，跑完 Q05 仍 `total=3`）、`level=2`（Q22 → `total=0`，更糟）、`node_limit=40`（无变化）、`mode=thinking|quick` / `query_expansion=off` / `peer_scope=user` / `quotas={...}`（**全 400**）、`content/overview` GET（不生成）。

#### 11.12.5 R15 量化：脚手架滤除与超量拉取（`_scratch_p3n.out` + `_scratch_p3o.out`）

**方法**：先做**离线重打分**（内嵌 Python 直接重算 p3m 存档 JSON 的 `rows[].top`，里面已存 `scaffold`/`id` 标志，**零服务端负载**），再实跑 5 组矩阵交叉验证。

**离线重打分结果 —— 滤除脚手架对 `precision@5` 的 delta 在所有分组都是 +0.000**：

| grp | precision@5 raw → filtered | Δ | precision@returned raw → filtered | returned raw → filtered | scaffold_rate |
| --- | --- | --- | --- | --- | --- |
| G1 | 0.200 → 0.200 | **+0.000** | 0.200 → 0.260 | 5.000 → 3.875 | 0.225 |
| G2 | 0.200 → 0.200 | **+0.000** | 0.200 → 0.296 | 5.000 → 3.625 | 0.275 |
| G3 | 0.150 → 0.150 | **+0.000** | 0.150 → 0.175 | 5.000 → 3.500 | 0.300 |
| **G4** | **0.633 → 0.633** | **+0.000** | **0.633 → 0.792**（FAIL→**PASS**） | 5.000 → 4.000 | 0.200 |
| ALL | 0.273 → 0.273 | **+0.000** | 0.273 → 0.353 | 5.000 → 3.733 | 0.253 |

recall@5 / mrr / hit@5 滤除前后**完全不变**（G1 1.000/1.000、G2 1.000/1.000、G3 0.750/0.750、G4 0.792/1.000、ALL 0.892/0.933/0.933）。ON 态同样：p@5 delta 全 0，G4 p@ret `0.522→0.722`（仍 FAIL），ON 态 scaffold_rate G1 0.175 / G2 0.350 / G3 0.350 / G4 0.233 / ALL 0.280。

**实跑矩阵（全 rerank OFF，limit=5）**：

| run | ALL r@5 | ALL p@5 | ALL p@ret | returned | fetched | mrr | scaf | p50 | p95 | 阈值 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 0.892 | 0.273 | 0.273 | 5.00 | 5.00 | 0.933 | 0.253 | 137ms | 327ms | 5/7 |
| **filt-of0** | 0.892 | 0.273 | **0.353** | **3.73** | 5.00 | 0.933 | 0.253 | 147ms | 281ms | **6/7** |
| nofilt-of3 | 0.892 | 0.273 | 0.273 | 5.00 | 7.97 | 0.933 | 0.247 | 130ms | 240ms | 5/7 |
| filt-of3 | 0.892 | 0.273 | 0.278 | 4.73 | 7.97 | 0.933 | 0.247 | 133ms | 213ms | 5/7 |
| filt-of5 | 0.892 | 0.273 | 0.275 | 4.77 | 9.83 | 0.933 | 0.240 | 130ms | 217ms | 5/7 |

分组 p@5 在五个 run 里**完全不变**：G1 0.200 / G2 0.200 / G3 0.150 / **G4 0.633**。分组 mrr 五个 run 全同：G1 1.000 / G2 1.000 / G3 0.750 / G4 1.000 / ALL 0.933。

**三条硬结论**：

1. **超量拉取（overfetch）完全无效**。`nofilt-of3` 与 `baseline` 逐项相同（服务端排序稳定，取 8 条的前 5 条 = 取 5 条）；`filt-of3` / `filt-of5` 滤除后从深度 6–8 补位的文档**同样不是 expected**，故 G4 p@5 仍精确等于 0.633。"G4 rows where filtering changed anything (filt-of3 vs baseline)" 列表**为空**。
2. **R15 是 token 预算/噪声问题，不是 precision 问题**。脚手架从未是 expected 文档，滤掉它不会增加 found；而 `precision@5` 分母钉死在 `limit=5` ⇒ delta 恒为 +0.000。**真实代价是 `avg_returned` 5.00→3.73（−25% 文档进模型）**。
3. **G4 precision@5 = 0.633 是 recall 强加的算术天花板**。`precision@5 = found/5`，`found = recall@5 × |expected|`；G4 共 24 个 expected × recall 0.7917 = 19 found，`19/6/5 = 0.6333`，与实测完全吻合。阈值 0.75 要求 `recall@5 ≥ 0.9375` ⇒ **「G4 precision@5 ≥ 0.75」实为一个伪装成 precision 的 recall 阈值**。

超量拉取对延迟无可测影响（p50 130–147ms），所以"不生效"不是性能问题而是排序问题。

**G4 的 5 个 miss 已逐条定性**（`expected` / `found` / `missing`）：

```
Q25 数据库连接数的配置上限和出过的故障   exp=C12,C22,C25,C33  found=C12,C22,C33      missing=C25
Q26 各类超时与重试时间的设定             exp=C26,C21,C22,C29  found=全 4 条
Q27 幂等、去重与重复请求处理             exp=C24,C27,C21,C29  found=全 4 条
Q28 Kubernetes 上的部署、扩容与容器排障  exp=C36,C11,C19,C34  found=C36,C11,C19      missing=C34
Q29 SQL 执行计划、索引与查询性能         exp=C25,C37,C12,C38  found=C25,C37,C12      missing=C38
Q30 生产环境的发布、变更与安全管控       exp=C34,C17,C18,C33  found=C34,C33          missing=C17,C18
```

读语料正文后判定：

- **3 个属评测集过标注（应修 ground-truth，不是修系统）**：
  - `Q25→C25`：C25 是「订单列表查询耗时超过 3 秒，通过给 `created_at` 和 `user_id` 建复合索引，把耗时降到 120 毫秒以内」——讲**索引/耗时**，不讲**连接数配置**。
  - `Q29→C38`：C38 是「用 pprof 抓 CPU 和内存火焰图，定位热点函数」——**进程剖析** vs **SQL 执行计划**，语义无关。
  - `Q28→C34`：C34 是「部署走 Argo CD，镜像推到 harbor.internal，生产发布需要两个人工审批节点」——文本无 k8s/容器字样，**词汇零重叠**；且 C34 在 Q30 里排 #2，证明 embedding 认得它，只是 Q28 的语义重心在"扩容/排障"。
- **2 个是真实中文语义召回缺口**：`Q30→C17`（「2026 年 5 月 9 日发布了订单中心 v2.8.0，引入拆单逻辑，灰度比例从百分之五逐步放到百分之百」）与 `Q30→C18`（「6 月 20 日红蓝对抗演练，发现网关未限制内网横向访问，6 月 25 日修复」）。两者与查询**词面高度重叠**（发布/灰度、安全演练），都该进 top-5 却被挤出。
  **根因线索**：Q30 的 top-5 分数带**极窄（0.3989–0.4973）**，说明「发布 / 变更 / 安全管控」这类**多主题复合查询**在单向量检索下区分度塌缩。这是后续中文优化最值得攻的点（候选手段：查询分解、多向量；~~`output_language_override`~~ **已实测排除，它是伪杠杆**，见 §11.12.9）。

filt-of3 下 G4 完整 top-5（分数 / id / uri），可作为分数分布基线：

```
Q25  0.7092 C22 cases/连接池耗尽案例 · 0.6358 C12 entities/主数据库实例 · 0.5481 C16 events/2026年3月数据库故障 · 0.5222 C33 tools/数据库客户端 · 0.4612 C13 entities/缓存集群
Q26  0.7266 C21 cases/网关重试策略 · 0.6560 C26 patterns/超时设置规律 · 0.5415 C22 · 0.5172 C29 patterns/限流模式 · 0.5069 C27 patterns/幂等设计模式
Q27  0.7287 C27 幂等设计模式 · 0.6151 C24 cases/消息重复消费案例 · 0.4808 C21 · 0.4278 C29 · 0.4205 C01 profile/职业背景
Q28  0.5811 C11 entities/订单中心服务 · 0.5609 C36 skills/Kubernetes排障技能 · 0.5499 C05 profile/技术栈背景 · 0.4553 C19 events/2026年7月容量扩容 · 0.4287 C39 skills/故障复盘技能
Q29  0.6707 C37 skills/SQL调优技能 · 0.5793 C25 cases/慢查询案例 · 0.3599 C12 · 0.3562 C15 entities/监控看板 · 0.3314 C16
Q30  0.4973 C33 tools/数据库客户端 · 0.4660 C34 tools/部署工具链 · 0.4453 C10 preferences/依赖升级偏好 · 0.4132 C14 entities/消息队列主题 · 0.3989 C20 events/2026年8月依赖漏洞
```

**建议落地（插件侧）**：按 URI 过滤 `.abstract.md` / `.overview.md`，目的是**把 25% 的 token 预算还给真实记忆**；**不要**同时开 overfetch（无收益且无必要）；**不要**调 `scoreThreshold`（R15）。

> ⚠️ **这条建议的落点与对象都已被 P3-G 实测推翻，只保留结论不保留方案**（详见 §11.12.10）：
> - **落点不是插件侧**——`recallExcludeUris` 在 `2026.9.25-2` 是**死旋钮**，没有任何生产者。
> - **对象不是 `.abstract.md` / `.overview.md` 文件名**——context 模式下 overview 是 `tiers.py` **合成的 tier**，不是独立检索命中，按文件名过滤无从下手。真正的对象是 **7 个结构性目录节点 URI**。
> - **收益被低估了**：实测不是 25% 而是 **38.5%** 的 token 回收，且 **CJK 密度 +138%**、真实记忆零损失。
> - 「不开 overfetch」「不调 `scoreThreshold`」两条**仍然成立**。

#### 11.12.6 写入 / 删除通道与 Phase 3 剩余待办

**写入通道实测延迟**（决定平台该用哪条）：

| 通道 | 条件 | 延迟 |
| --- | --- | --- |
| `content/write` | `viking://agent` + `wait=true` + semantic `complete` | **15324 ms** |
| `content/write` | `viking://user/{uid}/memories/` + `wait=true` + semantic `skipped` | **269 ms**（40 条 p50 **332 ms** / max 789 ms） |
| MCP `write` | 145 KB + `wait=false` | **< 0.1 s** |

`content/write` 的响应形状：`{uri, root_uri, context_type:"memory", mode:"replace", written_bytes, content_updated:true, semantic_status:"skipped", vector_status:"complete", queue_status:{Semantic:{processed:0}, Embedding:{processed:2}}}`。⚠️ **`semantic_status:"skipped"` 不是 R18 的原因**（原判有误，已在 §7 R18 与 §11.12.4 订正）：2×2 实验证明走**同一条 `write` 通道**、`semantic_status` 同为 `skipped`，进 `cases/` 却 `overview=complete`、进 `tools/` 才 `overview=skipped`。**判断 overview 会不会生成，要看 `overview=complete|skipped` 这个字段，不要看 `semantic_status`**。

**`remember()` 异步抽取的完成判定（P3-01 实测，四步缺一不可）**：

1. **`POST /api/v1/system/wait`，body 必填**：传 `null` → **400**；传 `{}` 或 `{"timeout":N}` → 200。返回每个队列的 `{processed, requeue_count, error_count, errors[]}`，队列名有 `Embedding` / `Semantic` / `ExternalParse` / `AddResource` / `SessionCommit`。
2. **积压时会 504，必须重试**。P3-01 的 round0 就吃到 `wait=504`（前序语料还在排队），重试后正常。轮询间隔建议 ≥3 s。
3. **交叉验证**：`GET /api/v1/tasks?include_internal=true&limit=50` 与 `GET /api/v1/observer/queue`——`system/wait` 返回 200 只代表**它看到的那批**清空，不代表抽取产物已落盘。
4. **最终信号 = `GET /api/v1/stats/memories` 的 `total_memories` 发生变化**（P3-01：`total 43→45`、`preferences 8→10`，12 轮 / 35.8 s）。这是唯一不会骗人的判据。

**平台自动写入通道定为「混合」**（用户对"写入通道"未作选择，按推荐项执行）：即时可召回走 `content/write` 到 `viking://user/{uid}/`（269 ms，满足 P4-06），需要结构化抽取/分类/overview 的走 `remember`（异步，**35.8 s 量级**）。⚠️ 但 `remember` **不是"生成 L1 overview 的唯一路径"**（原判有误）——`content/write` 同样会调 `refresh_schema_overview`；两者的差别只在**用哪个 registry**（抽取路径用 account registry，写入路径硬编码 default registry，见 §11.12.4 第 3 步）。选 `remember` 的真实理由是**它才会做分类抽取**，而不是"只有它能生成 overview"。

**删除只有 MCP 一条路**（R20）：`POST http://127.0.0.1:1933/mcp`，**必须带 `Accept: application/json, text/event-stream`**，否则 **406** `Not Acceptable: Client must accept both application/json and text/event-stream`。不需要 session id；握手 `initialize{protocolVersion:"2025-03-26"}` → `notifications/initialized`（notify 无 id）→ `tools/call`。`tools/list` 回 **15 个工具**（`add_resource cancel_watch edit find forget glob grep health list list_watches read remember search tree write`）。schema：`forget required=['uri'] props=['recursive','uri']`；`write required=['uri','content'] props=['content','mode','timeout','uri','wait']`；`edit required=['uri','old_string','new_string']`；**`read` 的参数是 `uris`（数组）不是 `uri`**。实测删除 `_smoke-probe.md` 后 `total_memories 44→43`、`cases 6→5`、`vector/count 141→140`。

**Phase 3 剩余待办**：

- ~~**P3-C（阻塞 P3-01/P3-02）**：`remember()` 异步抽取的完成判定~~ —— **已完成**。完成判定四步见上；P3-01 **PASS**、P3-02 **FAIL（5/9 = 55.6%）**，归因见 §11.12.8。原判「验证 R18 解法 = `auto_generate_l1` + `output_language_override`」**前提错误**，真正的守卫是 registry 的 `overview_template`（§11.12.4 第 3 步），而 `output_language_override` 是伪杠杆（§11.12.9）。
- ~~**P3-G**：插件侧中文行为——P3-09 CJK token 预算、P3-10 `minQueryLength`、P3-12 CJK 密度启发式，并把 §11.12.5 的脚手架滤除建议落成具体改动。~~ —— **已完成**，四项结论与两处落地改动见 §11.12.10。原判「改动只能落在我们的 `ovcli.conf` / 环境变量 / 代理层」**成立且已被验证**：最终就是 `ovcli.conf` 一个键 + `ov_proxy.py` 一处请求改写。⚠️ 注意插件是 npm 上游包 `@openviking/opencode-plugin@2026.9.25-2`，不是我方代码，且其 `package.json` 的 `files` 白名单**不发货 `tests/*.test.mjs`** ⇒ 不能靠改包内代码，也不能把包内测试当 CI 门禁。插件侧可调项清单见 §11.12.9。
- **评测集扩充**：G4 至少扩到 30 条以消除小样本抖动；同时修掉 3 条过标注（Q25→C25、Q28→C34、Q29→C38）。**并且要按 §11.12.8 的实测结论重设计类目**：抽取器实际只路由进 4 个类目，自造 `tools`/`skills`/`patterns` 的语料在自然对话里永远产不出来，用它们测分类正确性是**评测集失真**而非系统缺陷。
- **可选正向杠杆（尚未实测）**：`EDITABLE_MEMORY_TEMPLATE_FIELDS` 里的字段 `description` **是抽取 prompt 的一部分且 account 级可改**（`PUT /api/v1/admin/accounts/{account_id}/memory-templates/{memory_type}`）⇒ 理论上可用来强化中文抽取指令。这是目前唯一**受支持的**中文优化通道，但**只对 `remember` 抽取路径生效**（双 registry 不一致，§11.12.4 第 3 步）。
- ~~**清理残留**：`agent-p2probe` 容器、探针用户（`p2probe` / `ovzh-eval` / `studio-debug` / `p3c-probe`）的记忆、`benchmark/ov-zh` 语料、`_scratch_*`、轮换 root key（R1）。注意 `docker ps -a` 里还有一个**独立的陈旧 `openviking` 容器（Exited (1)）**与 `agent-docker-demo-openviking-1` 并存，属 R5 残留，应删。~~ —— **已全部处置（§11.19，2026-09-29）**：本节当时点名的 4 个探针用户加上后续阶段新增的共 9 个全部先清内容后删号（vector 837→134、memories 651→2）、`agent-p2probe` 容器与双卷、陈旧 `openviking` 容器（R5 残留）均已删除、216 个 `_scratch_*` 已清空、root key 已轮换（R1）。`benchmark/ov-zh` 语料与证据文件按原样保留。

#### 11.12.7 REST / 契约速查（Phase 3 实测校正）

**正确路径**（打错会 404，已逐个验证）：

| 用途 | 路径 |
| --- | --- |
| 记忆统计 | **`/api/v1/stats/memories`**（`/api/v1/debug/stats/memories` **404**） |
| 会话统计 | `/api/v1/stats/sessions/{session_id}` |
| API 全貌 | `/openapi.json`（**126 paths / 318828 B / 无需认证**，R13） |
| debug 面 | **只有** `/api/v1/debug/health`、`/api/v1/debug/vector/count`、`/api/v1/debug/vector/scroll` |
| admin | `/api/v1/admin/accounts`、`/api/v1/admin/accounts/{account_id}/memory-templates[/{memory_type}]`（**这个有 delete**）、`/api/v1/admin/agent-evolution` |
| agent evolution | `/api/v1/agent-evolution/experiences/{outcomes,trajectories}` |
| grep | `/api/v1/search/grep`，**必填参数是 `uri` 不是 `target_uri`** |

**`/api/v1/content/read` 的 `result` 是纯字符串**（不是对象），top keys 恒为 `['error','profile','result','status','telemetry']`。

**`SearchRequest` 完整 29 字段**（默认值即实测可用值；调参前先对这张表，传错 enum 一律 400）：

```
context_type anyOf str/array/null=null   dedup_turns int=0
detail enum auto|abstract|overview|full 或 dict=null   exclude_uris array(maxItems 200)=null
filter object(additionalProperties:true)=null          image_url str/null=null
include_provenance bool=false            level int|str|array|null=null
limit int=10                             max_tokens int(64..32000)=1600
mode enum["list","context"]="list"       node_limit int/null=null
other_peer_penalty number|object|null=null   peer_scope enum["actor","all"]="all"
purpose enum["chat","coding"]|null=null  query str=""
query_expansion enum["off","auto"]="auto"    quotas object{str:int}|null=null
read_content bool=false                  rewrite bool|const"auto"=false
rewrite_max_bullets int=6                score_threshold number|null=null
session_id str/null=null                 since str/null=null
tags array/null=null                     target_uri str/null=""
telemetry bool|null=false                time_field str|null=null
until str/null=null
```

**MCP `search` 比 REST 多 3 个参数**：`min_score`、`detail_by_category`、`other_peer_penalties`（required 只有 `query`）。`remember` 的 required 是 `["messages"]`。**MCP 文本里的 score 呈整数百分比，REST 呈小数**——横比时须换算。

**MCP 枚举工具的 schema（R21 相关，实测）**：

```
find   required=['query']         props: query, target_uri="", limit=10, min_score=0.35, level(int[]), context_type(str[]), read_content=False
read   required=['uris']          props: uris(array), offset=0, limit=-1
list   required=['uri']           props: uri, recursive=False, offset=0, limit, sort_by∈[name,mtime], sort_order=asc
tree   required=None              props: uri="viking://", level_limit=3, node_limit=1000, include_abstract=False, offset=0, limit
grep   required=['uri','pattern'] props: uri, pattern(**array，不是字符串**), case_insensitive=False, node_limit=10
glob   required=['pattern']       props: pattern, uri="viking://", node_limit=100
```

**`fs/ls` 的正确契约（R21）**：默认（等价于 `output=agent`）→ **结构化 JSON 数组** `{uri, size, isDir, modTime, rel_path, abstract}`；`simple=true` → **URI 字符串数组**；⚠️ **`output=json` 与 `output=tree` 都会 400** `INVALID_ARGUMENT: Invalid output format: …`（**只有 `agent` 合法**）；**`show_all_hidden=true` 才会返回 `.overview.md` / `.abstract.md`**（默认隐藏，不加就会误判"overview 没生成"）。

**URI 形态不一致（R21，最容易踩的坑）**：

```
真实规范路径（fs/ls / tree / glob / REST content/read 认这个）： memories/events/2026/09/28/星槎计划启动会.md
search & grep 返回的折叠形式：                                    memories/events/2026-09-28/星槎计划启动会.md
REST content/read 拿折叠形式 → 404 NOT_FOUND "File not found"；拿嵌套形式 → 200 ✓
MCP read 两种都接受（容错）
```
⇒ **把 search 结果直接喂给 `content/read` 会 404**，必须先还原成嵌套形式（或改用 MCP `read`）。

**根级 recursive `fs/ls` 有深度截断（约 3 段），且所有 limit 旋钮都无效（R21）**：

```
fs/ls MEM?recursive=true&show_all_hidden=true              → 24 nodes（漏掉 events/2026/09/28 目录 + 3 个文件）
  同上 + node_limit=5000 / abs_limit=5000 / 两者同时 / limit=500 → 恒 24 nodes，**全部无效**
fs/ls MEM/events?recursive=…                               → 3 nodes（只有三个空 DIR）
fs/ls MEM/events/2026?recursive=…                          → 5 nodes ✓
MCP tree level_limit=6                                     → 22 entries ✓
MCP glob MEM/events **/*                                     → 5 files ✓
```
⇒ **枚举深层记忆树必须用 MCP `tree`（提 `level_limit`）或 `glob`，或者逐层 `fs/ls`；不要指望 REST 的 recursive**。

**Admin memory-templates 的 editable / locked 边界（R22）**：`_complete_template` 的 `editable = {"description"}`；`_apply_editable_values` 对非白名单字段 `raise ValueError(f"{location} is locked and must match the default template")`。⇒ **`overview_template` 是 locked 的，account 级覆盖改不了它**，只有字段 `description` 可改（而 `description` 会进抽取 prompt，所以是**唯一受支持的中文强化通道**，见 §11.12.9）。`EDITABLE_MEMORY_TEMPLATE_FIELDS` **不含 `cases`** ⇒ `cases.yaml` 的模板 bug（R22）在 account 级**无法绕过**。

**`system/wait` 的 body 必填**：`null` → 400，`{}` 或 `{"timeout":N}` → 200；积压时会 504 需重试（详见 §11.12.6 完成判定四步）。

**日期分桶的 UTC 缺陷（R23）**：`events` 按 `created_at` 的**日历日**分桶，服务端用 `datetime.now(timezone.utc)` 打戳且全树无本地化 ⇒ **北京时间 00:00–08:00 产生的记忆会落进前一天的桶**。唯一受支持的修法是 caller 传带 `+08:00` 偏移的 `created_at`，而上游插件从不传（§11.12.9）。**`TZ=Asia/Shanghai` 修不了它**。

**Phase 3 执行约束**：容器侧 REST 面**只读**（`ov_proxy._ALLOWED_REST` 8 项不含 `content/write`）⇒ 灌语料必须绕过代理，用派生 User Key 直连；写入一律落 `viking://user/{uid}/`（R16）；`vector/count` 异步；`stats/memories` 只数**抽取出的结构化记忆**（`content/write` 灌进去的不计）；CJK 判定一律用码点（§11.3 第 7 条）。

#### 11.12.8 P3-02 分类正确性：FAIL 归因与 `target_uri` 反证

**测试形态**：探针用户 `p3c-probe`，9 条自然中文对话（含 P3-01 那条）逐条走 MCP `remember`，每条埋一个唯一 marker 用于事后定位落点；等抽取完成后枚举整棵记忆树，比对"落点类目"与"预期类目"。

**结果**：`markers resolved = 9/9`（**没有丢记忆**）、`landed-in-expected = 5/9 = 55.6%`；只算 Part C 的 8 条 = **4/8 = 50%** ⇒ 阈值 ≥85% ⇒ **FAIL**。

**落点全景**（`p3c-probe` 记忆树，24 节点 = 15 文件 + 9 目录）：

```
memories/
├── .abstract.md 252B / .overview.md 307B   (generated_by: VikingFS.write_context / context_write)
├── cases/.overview.md 372B (MemoryUpdater) + probe-cases.md 215B
├── entities/内部服务/{.overview.md 327B, 青筠网关.md 340B}           ← probe 2 ✓
├── events/2026/09/28/{.overview.md 664B, 星槎计划启动会.md 807B,    ← probe 3 ✓
│                      连接池耗尽故障复盘.md 882B}                    ← probe 4（预期 cases）✗
├── identity.md 439B（根下单文件）                                    ← probe 8（预期 identity）✗
├── preferences/user/{.overview.md 523B (total_entries:4),
│   后端开发与代码风格.md 325B, 沟通回复风格.md 292B,
│   调试与排障工具.md 303B, 饮品偏好.md 313B}                          ← probe 0/1 ✓、probe 5（预期 tools）✗
├── profile.md 548B（根下单文件）                                     ← probe 7 ✓ + probe 6（预期 skills）✗
├── soul.md 1180B
└── tools/probe-tools.md 192B（**唯一没有 .overview.md 的叶子目录**，即 R18）
```

**逐条归因**：

| probe | 内容要点 | 预期类目 | 实际落点 | 判定 |
| --- | --- | --- | --- | --- |
| 0 / 1 | 代码风格 / 饮品偏好 | preferences | `preferences/user/` | ✓ |
| 2 | 内部网关服务 | entities | `entities/内部服务/` | ✓ |
| 3 | 项目启动会 | events | `events/2026/09/28/` | ✓ |
| 4 | 线上故障复盘 | **cases** | **events** | ✗ |
| 5 | 性能排查工具 | **tools** | **preferences** | ✗ |
| 6 | 会写 Rust 绑定 | **skills** | **profile** | ✗ |
| 7 | 带几个人 / 负责什么系统 | profile | `profile.md` | ✓ |
| 8 | 回答风格约定 | **identity** | **preferences** | ✗ |

**三条硬结论**：

1. **抽取器实际只会路由进 4 个类目**：`preferences` / `entities` / `events` / `profile`。`identity` **也从未被使用**（probe 8 被投进了 preferences，而 `identity.md` 是根下单文件，不是 `identity/` 目录）。⇒ **评测集自造的 `tools` / `skills` / `patterns` 三类，自然对话永远不会产出**。这不是系统 bug，是**评测集的 8 类设计本身失真**——它假设了一个抽取器并不具备的分类粒度。P3-02 的 FAIL 里**有 3 条属于判据设计错误**（probe 5 / 6 的预期类目不存在于抽取器的输出空间）。
2. **mis-filing 不影响召回**。rerank OFF 下，"错误类目"里的文档照样以高分排 #1（`tools/probe-tools.md` **0.8567**、`preferences/user/调试与排障工具.md` **0.7311**），**完全不需要 `target_uri`**。⇒ 分类正确性与检索可用性**解耦**：P3-02 FAIL 不构成生产阻塞。
3. **`target_uri` 定向是负收益**（推翻了 §11.12.4 里"兜底手段有效"的无条件表述）。5 组对照：

```
Q='线上故障连接池耗尽最后怎么解决的'
  target_uri=memories/cases → #1 0.5698 cases/probe-cases.md（**错误文档**）
  target_uri=(none)         → #1 0.8141 events/2026-09-28/连接池耗尽故障复盘.md ✓  #2 0.6434 …/.overview.md
Q='我抓火焰图用什么性能排查工具'
  target_uri=memories/tools → #1 0.3023 tools/probe-tools.md（**错误文档**，且低于 scoreThreshold=0.35）
  target_uri=(none)         → #1 0.7311 preferences/user/调试与排障工具.md ✓
Q='我会不会写 Rust 绑定给 Python 调'
  target_uri=memories/skills → hits=0        / (none) → #1 0.6232 profile.md ✓
Q='我带几个人、负责哪个系统'
  target_uri=memories/profile → hits=0       / (none) → #1 0.4968 青筠网关.md（误排）#2 0.4503 profile.md ✓
Q='回答风格上我跟你约定了什么'
  target_uri=memories/identity → hits=0      / (none) → #1 0.6249 preferences/user/沟通回复风格.md ✓
```
   ⇒ 5 组里 **3 组 `hits=0`、2 组把错误文档顶到 #1、0 组改善**。**平台侧一律不要传 `target_uri`**（该传的是 rerank ON + THINKING 场景，而那个场景已被永久关闭）。

**中文内容质量画像（P3-02 附带产出，CJK ratio = CJK 码点 / (CJK + ASCII 字母数字)）**：

| 文件 | bytes | cjk | alnum | ratio |
| --- | --- | --- | --- | --- |
| `entities/内部服务/青筠网关.md` | 149 | 40 | 0 | **1.000** |
| `preferences/user/饮品偏好.md` | 131 | 34 | 0 | **1.000** |
| `preferences/user/沟通回复风格.md` | 104 | 28 | 0 | **1.000** |
| `preferences/user/调试与排障工具.md` | 112 | 25 | 13 | 0.658 |
| `preferences/user/后端开发与代码风格.md` | 128 | 28 | 16 | 0.636 |
| `events/2026-09-28/连接池耗尽故障复盘.md` | 428 | 99 | 64 | 0.607 |
| `events/2026-09-28/星槎计划启动会.md` | 410 | 89 | 62 | 0.589 |
| `profile.md` | 363 | 63 | 97 | **0.394** |

ratio < 1 的部分**全是结构性英文**（`# Summary`、`# 2026-09-28 (Monday) ChatLog:`、`**user**:`、`(as of 2026-09-28)`，以及 `Python 3.13` / `FastAPI` / `py-spy` 这类专有名词），**不是 LLM 把内容英文化了**。⇒ **中文记忆正文与中文文件名都已正常工作**，P3-04/P3-05 的实质目标已达成。

4 个 `.overview.md` 的实测 ratio 就低得多：`memories/.overview.md` **0.000**、`cases/.overview.md` **0.000**、`entities/内部服务/.overview.md` **0.076**、`preferences/user/.overview.md` **0.220**——**残留英文全部集中在结构性脚手架**，而它无解（§11.12.9）。

**R22 的实证**：`cases/.overview.md` 里原样吐出了 `{{ no such element: dict object['case_name'] }}`。根因是上游模板不一致——`preferences.yaml:23` / `entities.yaml:24` / `events.yaml:112` 都写了 `|default(item.file_name, true)`，**唯独 `cases.yaml:51` 没有**：

```yaml
# preferences.yaml:23 / entities.yaml:24 / events.yaml:112
- [{{ item.file_content.topic|default(item.file_name, true) }}](./{{ item.file_name }})
# cases.yaml:51  ← 唯一缺 default
- [{{ item.file_content.case_name }}](./{{ item.file_name }}) — {{ item.file_content.task_signature }}
```
⇒ 任何缺 front-matter 的 `cases` 文件都会让 overview 带出 Jinja 垃圾，而这段垃圾**随后会成为 THINKING 模式的目录打分信号**（主动投毒）。`cases` 又不在 `EDITABLE_MEMORY_TEMPLATE_FIELDS` 里 ⇒ **account 级无法绕过**，只能等上游修。

#### 11.12.9 中文语言专项定案：可改性矩阵与两个伪杠杆

##### A. 生成物语言可改性矩阵（六路交叉验证：Z4 + E1 + E3 + F4 + F5 + G3）

| 生成物 | 语言来源 | `output_language_override` | `LANG`/`TZ` 等 env | Admin API 模板覆盖 |
| --- | --- | --- | --- | --- |
| 记忆正文（preferences/entities/events/cases 内容） | 抽取 prompt 里的 `{{ language }}`（由会话内容检测） | **能**（硬强制全租户） | 仅**空文本**的 fallback | `description` 字段可改 |
| 记忆文件名（中文 slug） | 同上 | **能** | 同上 | 同上 |
| 分类 `.overview.md` 的**标题**（`# Preferences Overview` 等） | **YAML 里的英文字面量** | **不能**（渲染上下文无 language 变量） | 不能 | **不能**（`overview_template` locked） |
| 分类 `.overview.md` 的**条目文本** | 数据值（`file_content.topic/category/name/summary`） | 间接（随正文语言） | 间接 | 间接 |
| 根 `memories/.overview.md` | **`core/directories.py:62` 的 Python 字符串字面量** | **不能** | 不能 | 不能 |
| 资源文件摘要 / VLM 通用目录 overview | `semantic_processor.resolve_output_language` | **能** | 仅空文本 | — |

**决定性证据**（三条，任一即可否定"用配置改 overview 语言"）：

```python
# ① generate_overview 的渲染上下文里没有 language 键
overview_context = {"memory_type": memory_type, "directory_name": directory_name, "items": items}
# ② grep output_language memory_updater.py  →  零命中
# ③ account 级覆盖被 locked 挡住
_apply_editable_values: raise ValueError(f"{location} is locked and must match the default template")
_complete_template:     editable = {"description"}
```

⇒ **结论**：中文内容层面**已经正常工作**（ratio 0.589–1.000、文件名是中文 slug），唯一残留英文是**结构性脚手架**，而它**不存在任何受支持的配置通道可改**。P3 中文专项到此收口，**不改 `ov.conf`**。

**`output_language_override` 为何仍决定不启用**：它能改的那几格（记忆正文、文件名）**本来就已经是中文**（内容检测正确），启用它 = **全租户硬强制**（不是 account 级，会影响同一实例上所有用户，包括未来可能的英文用户），**零收益 + 明确副作用**。

**语言检测源码要点**（`utils/language.py`，供后续复核）：`_SCRIPT_MIN_CHARS=2`、`_SCRIPT_MIN_RATIO=0.20`、`_JAPANESE_KANA_MIN_CHARS=3`、`_STRONG_DOMINANT_MIN_CHARS=10`、`_STRONG_DOMINANT_RATIO=0.95`、`_PRIMARY_LANGUAGES={"zh-CN","en"}`、`_LATIN_HINT_LANGUAGES={"it","fr","es","de","pt"}`；`_resolve_system_fallback_language(default="en")` **无 `lru_cache`**（每次调用都重读 env）。`resolve_with_override(config, detect)` = `override if override else detect()`。`_TIMEZONE_LANGUAGE_GROUPS['zh-CN']` 含 `asia/shanghai`、`prc`、`china standard time` 等 12 个值。系统回退顺序：`LC_ALL` → `LC_MESSAGES` → `LANGUAGE` → `LANG` → `locale.getlocale()` → `TZ` → `/etc/localtime` 的 zoneinfo 段 → `time.tzname`。`patch_merge_context_provider.py:80` **硬编码 `fallback_language="en"`**。blast radius（Z6）：`semantic_processor.py` 三处、`session.py:4463`、`session_extract_context_provider.py:162`、`patch_merge_context_provider.py:80`、`media/utils.py:438`。

##### B. R23 定案：`TZ=Asia/Shanghai` 是伪修复，**已决定不加**

原判（G1 实验）：容器加 `TZ=Asia/Shanghai` 后 `events` 桶从 `2026-09-28` 变成 `2026-09-29`、星期从 `Monday` 变成 `Tuesday` ⇒ 以为修好了日期分桶。**这个读法是错的**——G1 只翻转了 **naive `datetime.now()` 的 fallback 分支**，而真实路径根本不走 fallback。完整机制链（四路交叉验证）：

```python
# ① 服务端打戳：UTC，与 TZ 无关（session/session.py:1395）
created_at = spec.get("created_at") or datetime.now(timezone.utc).isoformat()

# ② 取首条消息时间：保留字符串自带偏移，不做任何本地化（memory_updater.py:682-689）
def _first_message_time(self) -> str | None:
    for msg_group in self.elements:
        for msg in msg_group:
            if hasattr(msg, "created_at") and msg.created_at:
                dt = parse_iso_datetime(msg.created_at)
                return dt.strftime("%Y-%m-%d")     # ← 返回的已是日期串，不是 raw ISO

# ③ 分桶：对日期串做**词法切分**；naive datetime.now() 只是 fallback（memory_updater.py:337-369）
def get_year(self, ranges_str):  ... first_time.split("-")[0] ... else str(datetime.now().year)
def get_month(self, ranges_str): ... first_time.split("-")[1] ... else f"{datetime.now().month:02d}"
def get_day(self, ranges_str):   ... first_time.split("-")[2] ... else f"{datetime.now().day:02d}"

# ④ parse_iso_datetime 不转时区（utils/time_utils.py:8-18）
normalized = _EXCESS_FRAC_RE.sub(r"\1", value)
if normalized.endswith("Z"): normalized = normalized[:-1] + "+00:00"
return datetime.fromisoformat(normalized)          # ← 保留偏移，不本地化
```

- **全树无本地化转换**（J4）：`grep astimezone|time.localtime|tzset|ZoneInfo|tzlocal|gettz` 遍历 `session/` `utils/` `storage/` → 仅 4 处命中，全部指向 UTC（`utils/time_utils.py:31`、`utils/search_filters.py:276`/`:288`）+ 1 处无关（`storage/vectordb/utils/data_processor.py:241`）。
- `get_current_timestamp()`（`utils/time_utils.py:36-43`）= `format_iso8601(datetime.now(timezone.utc))`，**永远 UTC "Z" 形式**；`format_iso8601`（`:21-33`）一律 `astimezone(timezone.utc)` 后 `isoformat(timespec="milliseconds").replace("+00:00","Z")`。
- **容器时钟实测**：OV 容器 `TZ=('UTC','UTC')`、naive now == utc now；agent 容器 `TZ=[]`（未设）、`utc == local == +0000`。

**自然实验（同一瞬间，三种 `created_at`）**——唯一能改桶的杠杆是 caller 传偏移串：

```
server default UTC   2026-09-28T19:14:45.193545+00:00 → 桶 events/2026/09/28  头 2026-09-28 19:14 Monday  iso 2026-09-28T19:14:45.193Z
caller Beijing +08   2026-09-29T03:12:44.725581+08:00 → 桶 events/2026/09/29  头 2026-09-29 03:12 Tuesday iso 2026-09-28T19:12:44.725Z
caller UTC 同瞬间     2026-09-28T19:12:44.725581+00:00 → 桶 events/2026/09/28  头 2026-09-28 19:12 Monday  iso 2026-09-28T19:12:44.725Z
```
⇒ **传 `+08:00` 串能改桶，且向量库仍拿到同一 UTC 瞬间，两者不矛盾**。

**但该杠杆我方不可达**：`AddMessageRequest.created_at: Optional[str] = None` 确实存在（`server/routers/sessions.py:142`；另见 `ingest/models.py:51`），可是官方插件 `@openviking/opencode-plugin@2026.9.25-2` **全包 `grep created_at` → 零命中**，且它的写入根本不走 `/messages`，而是 **`POST /api/v1/sessions/{id}/commit`**（`lib/memory-session.mjs:470`）。

⇒ **加 `TZ` 反而更糟**：主路径仍是 UTC，fallback 变成本地时间，**制造两套日期基准**。**决定：`docker-compose.yml` 不加 `TZ`，改代码不动，只在 compose 里留注释块记录"为何故意不设"**（已落地，注释引用 R23）。

**语言侧同理，收益≈0**（Z2/Z3 实测）：注入 `LANG` / `LC_ALL` / `LANGUAGE` / `TZ` 四类 env **只能翻转"空文本"的 fallback**；文本为 `'ok'` 或 `"print('x')"` 时，即使 fallback 已是 `zh-CN`，检测结果仍是 `en`（内容检测优先且合理）。

##### C. 插件侧真实形态（P3-G 的前置事实）

- 包身份：`@openviking/opencode-plugin@2026.9.25-2`，`type: module`，`main: index.mjs`，`oc-plugin: ["server","tui"]`，**`dependencies: {}`**（零依赖）。我方只在 `agent-image/builtin-plugins/openviking/` 放了 `manifest.json`（`path: /opt/agent/builtin-plugins/openviking/node_modules/@openviking/opencode-plugin`）与 `ovcli.conf`。
- **发货文件白名单不含测试**：`files` = `index.mjs`(3406B) / `lib/` / `servers/` / `scripts/` / `README.md` / `INSTALL.md` / `INSTALL-ZH.md` / `package.json`。⇒ **`tests/*.test.mjs` 不发货，不能当 CI 门禁**。
- 35 个 `.mjs`：`lib/{session-inject, runtime, memory-session, viking-uri-guard, config, v2-events, plugin-runtime, memory-recall, repo-context, mcp-config, v2-plugin, utils}.mjs` + `lib/shared/{mcp-proxy-config, mcp-proxy-core, input-filters, debug-log, workspace-peer, workspace-config, recall-compress-core, workspace-identity, plugin-config, retryable, pending-queue, setup-wizard, profile-inject, recall-core, uri-guard, batch-send, session-model, capture-utils, ov-http, config-schema, workspace-registry, credentials}.mjs` + `servers/mcp-proxy.mjs`。
- **插件打的 REST 端点全集**：`/api/v1/content/read`、`/api/v1/fs/ls`、`/api/v1/skills`、`/api/v1/system/status`、`/api/v1/search/find`、`/api/v1/search/search`、`/api/v1/search/recall`、`/api/v1/sessions/*`。**没有 `/messages`，也没有 `content/write`** ⇒ 印证 §B 的"created_at 杠杆不可达"。
- 关键调用点：`lib/session-inject.mjs:71` → `/api/v1/sessions/{id}/context?token_budget=${Math.max(1024, tokenBudget)}`；`lib/memory-session.mjs:454` → GET `/api/v1/sessions/{id}`（读 meta）；**`lib/memory-session.mjs:470` → POST `/api/v1/sessions/{id}/commit`（唯一写入触发）**。
- **`peer_scope` 降级逻辑**（`lib/shared/recall-core.mjs` 的 `postRecall`）：POST `/api/v1/search/recall` 时带 `peer_scope`，**只在收到 400/422 且是 unknown-field 拒绝时**去掉 `peer_scope` 重试，并写 memo（`peerScopeMemoPath()` → `stateFile("peer-scope.json")`）供 doctor 告警。⚠️ **降级的语义是把召回范围从 caller 自己的 peer 放宽到整个 user root** ⇒ **多租户隔离在旧服务端上会静默失效**，这对 P5 是硬约束（必须确认我们的服务端版本接受 `peer_scope`，否则要盯这个 memo 文件）。
- 因为 hook 是**一次性进程**，能力探测结果必须落盘：`isContextFaceLegacy` / `markContextFaceLegacy`（`stateFile("context-face.json")` + `LEGACY_CACHE_TTL_MS`）。
- `toISOString` 只出现在 `lib/utils.mjs:39` 与 `lib/shared/debug-log.mjs:30`（纯日志用途）。
- **`mcpEnabled` 的上游默认是 `true`**（`lib/shared/config-schema.mjs:63`：`{ name:"mcpEnabled", type:"bool", default: true, capability:"connection" }`；消费点 `lib/config.mjs:39` → `mcp: { enabled: config.mcpEnabled }`）。**我们的 `ovcli.conf` 显式设成 `false`**（同时 `repoContext: false`、`dataDir: /data/state/openviking`）⇒ 走纯 REST/hook 通道，不经 MCP。**这是有意的，改之前先确认 `ov_proxy._ALLOWED_REST` 白名单够用**。
- `remember` 在插件包里只有 3 处命中（`lib/shared/recall-core.mjs:435/:447/:631`），且**全是注释里的英文散文**（"remembered on disk"），**不是 MCP `remember` 工具调用** ⇒ **插件不主动写记忆，写入完全靠 session `commit`**。这对 P3-G 的"自动写入"假设是个重要修正。

#### 11.12.10 P3-G 定案与落地：插件侧中文行为 + 脚手架滤除

P3-G 的三项排查（P3-09 / P3-10 / P3-12）与 §11.12.5 建议的落地，最终收敛成**两处最小改动**：`ovcli.conf` 一个键 + `ov_proxy.py` 一处请求改写。原判「改动只能落在我们的 `ovcli.conf` / 环境变量 / 代理层」成立且已被验证。

##### A. 三项排查的定案

| 项 | 定案 | 依据 |
| --- | --- | --- |
| **P3-09** CJK token 预算 | **不改配置**（主路径已正确） | 服务端 `utils/token_estimation.py:11-44 estimate_text_tokens` 是 CJK-aware 的，**1.5 tokens/汉字**，与插件 `profile-inject.mjs:85` 同值且覆盖更宽 |
| **P3-10** `minQueryLength` | **改 `ovcli.conf` 为 `2`** ✅ 已落地 | 损失 100% 在插件门禁，服务端两字查询完全正常 |
| **P3-12** CJK 密度启发式 | **不改**（`capture-utils` 已 CJK-aware，且相关代码只在 fallback 路径生效） | `capture-utils.mjs:614-616` 判据已是 `cjk >= 4 \|\| alnum >= 6 \|\| text.length >= 12` |

**P3-09 细节**——预算链是 `render.py:49-51 fragment_tokens` → `budget.py:68` → `budget.py:44-46 per_entry_cap = max(1, max_tokens // max(1, n) * 2)`；`params.py:32 DEFAULT_MAX_TOKENS = 1600`，与插件默认 `recallMaxTokens` 一致。tiktoken **未安装** ⇒ 纯启发式，但启发式本身是 CJK-aware 的。活体验证：`max_tokens` 缺省 → `used_tokens 440` / `per_entry_cap 1066`；`=300` → `used_tokens 288` / `per_entry_cap 200`，tier 优雅降级（full → abstract → overview）。

插件另有一个 `recall-core.mjs:56 estimateTokens = chars/4` 的估算器，对中文**低估 6×**，但它**只作用于非 context-face 的 fallback 路径** ⇒ 主路径无需改配置。上游在 `profile-inject.mjs:63-65` 已点名 chars/4 "silently undercounts CJK content by 4-6×"，但只修了 profile 路径。

> ⚠️ **配置陷阱（记录以免后人踩）**：服务端 `SearchRequest.max_tokens: Field(default=1600, ge=64, le=32000)`，而插件 `recallMaxTokens` 的 clamp 上限是 **200000**。设成 >32000 会**每次 422**，且 `looksLikeUnknownField` 判为假 ⇒ 插件只记一条 `recall_context_face_error` 后**静默降级到 fallback**，质量悬崖无任何告警。**不要把 `recallMaxTokens` 调到 32000 以上。**

**P3-10 细节**——`lib/memory-recall.mjs:10 if (query.length < config.minQueryLength) return`，上游默认 3、范围 1–64、env `OPENVIKING_MIN_QUERY_LENGTH`、alias `recallMinQueryLength`。中文里两字查询（「故障」「部署」「网关」）是高频形态，被默认值整段吞掉。node 侧实测（容器内用 `/opt/agent/skill-envs/pptx/bin/node` 调 `loadConfig`）：

```
shipped minQueryLength = 3   candidate (ovcli.conf) = 2   alias recallMinQueryLength = 2   env OPENVIKING_MIN_QUERY_LENGTH=1 → 1（env 仍最高优先）
其余 recall 旋钮未受影响：{limit:10, thr:0.35, maxTok:1600, budget:2000, dedup:5, excl:[]}
Dockerfile 构建门禁 9 项断言值全部未变
```

**P3-12 细节**——`rankItem` / `dedupeItems` / `formatFallback` 的 `lexicalOverlapBoost` 对中文近乎失效（`QUERY_TOKEN_RE = /[a-z0-9一-龥]{2,}/gi` 把整串汉字切成**一个** token），但这些函数**只在非 context-face 的 fallback 路径执行**，context 模式下排序全在服务端 ⇒ **实质 moot**。（上游小瑕疵：`QUERY_TOKEN_RE` 用 `一-龥`(U+4E00–U+9FA5)，窄于 `capture-utils` 的 `[\u3400-\u9fff]`。）

**明确不改的四个键**：`recallExcludeUris`（死旋钮，见 B）、`recallMaxTokens`（服务端默认已 CJK-正确，且有上面的陷阱）、`recallTokenBudget`（只作用 fallback）、`scoreThreshold`（R15 已证对 `precision@5` delta 恒 +0.000）。

##### B. `recallExcludeUris` 是死旋钮——这是必须走代理层的根因

全插件树 `grep -rn "excludeUris\|exclude_uris\|recallExcludeUris\|recall_exclude"` **只有 3 处命中**：

```
lib/shared/recall-core.mjs:147   ← 消费者（读 options.excludeUris）
lib/shared/recall-core.mjs:148
lib/shared/config-schema.mjs:101 ← 声明（default: []）
```

而唯一的调用方 `lib/memory-recall.mjs:20-36` 传给 recall-core 的 options **只有** `{actorPeerId, legacyPeerId, sessionId, log}` ⇒ **`excludeUris` 永远拿不到值，在 `ovcli.conf` 里配它完全无效**。且插件是 npm 上游包，`package.json` 的 `files` 白名单不发货测试（§11.12.9 C），**不能靠改包内代码，也不能把包内测试当 CI 门禁** ⇒ 落点只能是代理层。

##### C. 两个决定落地形态的服务端事实

**① `viking://~` 是 home alias，在请求边界按 caller 身份展开**——这让「一份列表服务所有用户」成为可能，代理**永远不需要把 user_id 拼进 URI**，彻底规避转义/非法 URI 风险：

```python
# core/namespace.py:236-252
def resolve_request_uri(uri: str, ctx: RequestContext) -> str:
    """Supported aliases are the ``~`` home alias and the legacy ``session`` scope."""
    parts = uri_parts(uri)
    if parts and parts[0] == "~":
        return resolve_current_user_uri(uri, ctx)
    if ctx.role in {Role.USER, Role.ADMIN}:
        return resolve_current_user_uri(uri, ctx)
    return resolve_uri(uri).uri

# core/namespace.py:255+  resolve_current_user_uri
    if parts[0] == "~":
        if len(parts) == 1: return canonical_user_root(ctx)
        return f"{canonical_user_root(ctx)}/{'/'.join(parts[1:])}"
```

链路：`search.py:_resolve_uri_list` = `[validate_request_viking_uri(resolve_path_variables(uri), ctx) for uri in uris]` → `uri_validation.py:67` → `resolve_request_uri`。（`resolve_path_variables`（`core/path_variables.py:206`）只处理日历变量，与身份无关。注意 `resolve_uri` 对 `~` 作为**内部 scope** 是 fail-closed 的——别名只在请求边界有效。）

活体对照（探针 space `p3c-probe`，40 语料）：

```
A1 字面 7 条 viking://user/p3c-probe/...  : returned 4  used_tokens 549  tiers {overview:2, full:1, abstract:1}
A2 别名 7 条 viking://~ ...               : returned 4  used_tokens 549  tiers {overview:2, full:1, abstract:1}   ← 逐字节相同
A6 伪 scope viking://nope/x               : HTTP 400 INVALID_URI（合法 scope: agent, queue, resources, session, temp, upload, user）
```

**② `exclude_uris` 是 CONTEXT_ONLY 字段**——list 模式携带它是 400，所以改写必须按 mode 门控：

```python
# server/routers/search.py:161-173
CONTEXT_ONLY_FIELDS = (
    "query_expansion", "max_tokens", "quotas", "purpose", "detail",
    "dedup_turns", "exclude_uris", "peer_scope", "other_peer_penalty",
    "rewrite", "rewrite_max_bullets",
)
# 报错文案：f"{', '.join(names)} require mode='context'; set mode='context' or drop these fields"
```

`SearchRequest:197` / `RecallRequest:285` / `FindRequest:120` 全部 `model_config = ConfigDict(extra="forbid")` ⇒ **注入不存在的字段必然 4xx**。三条路径的差异：

| 路径 | `exclude_uris` 字段 | 语义 | 处置 |
| --- | --- | --- | --- |
| `/api/v1/search/search` | 有（`:234`，handler `:424`） | 由 `mode` 决定，**list 模式带它是 400**（活体 A5 已证） | **仅在 `mode == "context"` 时注入** |
| `/api/v1/search/recall` | 有（`RecallRequest:311`，handler `:519`） | `@router.post(..., deprecated=True)` → `fold_recall_request` → `assemble_context`，**恒为 context 语义，无 `mode` 字段** | **无条件注入** |
| `/api/v1/search/find` | **没有** | — | **不动** |

附带发现（A7）：`/search/recall` 注入后 `excluded: 0`，因为它的 quotas 是 `{events:10, entities:10, preferences:3, experiences:0}`，**没有 resources/skills 桶**，脚手架根本不入候选池 ⇒ 在该路径注入**无害且具防御性**（防上游改 quotas）。

##### D. 7 条脚手架清单，以及「必须全排」的反例

```
viking://~              (cjk=0 len=92)     viking://agent/skills   (cjk=0 len=200)
viking://~/peers        (cjk=0 len=207)     viking://resources      (cjk=0 len=139)
viking://~/privacy      (cjk=0 len=186)
viking://~/resources    (cjk=0 len=133)
viking://~/skills       (cjk=0 len=237)
```

7 条的 CJK 字符数**全为 0**——在一个记忆全是中文的部署里，它们是纯噪声，且 `purpose="coding"` 下 `resources` 桶配额最重，它们会**成建制**挤进召回块。

**部分排除是反效果**（A3 vs A4，实测）：

```
A4 不排除           : returned 9  used_tokens 893
A3 只排根 viking://~ : returned 9  used_tokens 923   ← 比不排除还大
```

原因：`gather._overfetch(want) = want + min(len(excluded), want * 2)` 会为被丢弃的节点**补位**，`~/peers`(207 chars) 顶替了 `~`(92 chars) ⇒ 块反而更大。排除是 **per node 而非 per subtree**（`retrieve/context_assembler/gather.py`），所以丢掉目录自己的 overview **不影响其子节点被检索到**。

##### E. 端到端收益（B1 走代理 vs B2 直连，同一 query 同一 space）

| 指标 | B2 直连（未过滤） | B1 走代理（已过滤） | Δ |
| --- | --- | --- | --- |
| returned | 9 | **4** | −5 条脚手架 |
| `used_tokens` | 893 | **549** | **−38.5%** |
| tiers | `{overview:7, full:1, abstract:1}` | `{overview:2, full:1, abstract:1}` | overview 7→2 |
| rendered chars | 2343 | **988** | **−57.8%** |
| **cjk 字符数** | **221** | **221** | **±0 ⇒ 中文内容零损失** |
| **cjk_ratio** | 0.094 | **0.224** | **+138%** |

B1 剩下的 4 条**全是真实记忆**：`memories/preferences/user`、`memories/events/2026/09/28/星槎计划启动会.md`、`memories/entities/内部服务`、`memories/entities/内部服务/青筠网关.md`。（URI 是**折叠形式** `2026-09-28`，见 R21——这种形式走 REST `content/read` 会 404，读正文必须走 MCP `read`。）

**B6（P3-10 的端到端证明）**：两字中文查询「故障」经代理 → `http=200 returned=4 used_tokens=506 cjk_ratio=0.227`，命中 `连接池耗尽故障复盘.md` / `调试与排障工具.md` / `青筠网关.md` / `entities/内部服务`——**全部高相关**。改前这条查询被插件门禁整段吞掉。

**旁路未被误改的反证**：B3 list 模式 `http=200`（若被注入会 400）、B4 `/search/find` `http=200`、B5 `/search/recall` `http=200`。

##### F. 落地形态

**① `agent-image/builtin-plugins/openviking/ovcli.conf`**（加一个键）：

```json
{"plugin": {"opencode": {"mcpEnabled": false, "repoContext": false, "dataDir": "/data/state/openviking", "minQueryLength": 2}}}
```

**② `backend/app/routers/ov_proxy.py`**（新增 `_without_scaffold()`，在 `_dispatch` 里挂钩一行）：

```python
    body = await request.body()
    body = _without_scaffold(upstream_path, body)   # ← 唯一改动点
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
```

函数契约（**只能收窄召回块，永不拒绝调用**）：

- 只认 `_SCAFFOLD_PATHS = {"/api/v1/search/search", "/api/v1/search/recall"}`；`/search/search` 还要求 `payload["mode"] == "context"`。
- **任何未正面识别的输入原样返回**：别的路径、非 JSON body、JSON 数组、空 body、`null`、非 context 模式 ⇒ 逐字节透传。
- 合并策略：**调用方自己的条目排在前**（`requested` 先去重后 `+= _SCAFFOLD_URIS`），这样 `_MAX_EXCLUDE_URIS = 200`（服务端 `retrieve/context_assembler/params.py` 的 `MAX_EXCLUDE_URIS`）触顶时，被截掉的是**我们的优化**而不是别人有意指定的排除项。
- 非字符串条目被剔除；`json.dumps(..., ensure_ascii=False)` ⇒ 中文 query 往返无损。
- `content-length` 已在 `_REQUEST_DROP_HEADERS` 里，`_open` 用 `client.build_request(..., content=body)` 自行重算 ⇒ 无陈旧长度问题。**`_relay` 的流式响应透传完全未动。**

这是本模块**唯一**改写 body 的地方，与「白名单中继」的设计意图兼容（已在模块 docstring 里点明）。

**决定不给 Dockerfile 门禁加 `minQueryLength` 断言**：它不是 load-bearing（不像 `mcp.enabled===false` 那样错了会让插件去 exec 一个镜像里不存在的 node），且那行 `node -e` 已极长易错。

##### G. 验证矩阵（全绿）

| 层 | 手段 | 结果 |
| --- | --- | --- |
| 配置解析 | 容器内 `node` 调 `lib/config.mjs:loadConfig` | `minQueryLength` shipped 3 → candidate 2；alias 同效；env 仍最高优先；**门禁 9 项断言值不变**；其余 recall 旋钮不变 |
| 单元 | `_scratch_p3g17.py`，直接 import `_without_scaffold` | **14 项全绿**：context/recall 注入、list/find/mcp 不动、调用方条目保留在首位、重复折叠（1 独有 + 1 重复 → 8 条）、250 条洪泛截断到 200、非 JSON/数组/空/null 透传、非字符串条目剔除、中文 query 往返无损 |
| 端到端 | `_scratch_p3g18.py`，走真实代理 `http://127.0.0.1:8000/ov`（凭 `ov_access.issue_proxy_token`）对比直连 | B1–B6 见 E 段 |
| 镜像门禁 | `bash scripts/build-agent.sh` | `#24 COPY builtin-plugins/` **未命中缓存**（DONE 0.8s）⇒ 改动确实进镜像；`#38` 打印 `[build] openviking plugin OK: hook-only, env credentials, dataDir /data/state/openviking`；`== done ==`。新镜像 ID **`sha256:93d984254d3e`**（原 `f7819d254443`） |

##### H. 复现命令

```bash
# backend 代码是烘进镜像的（docker-compose.yml 无 ./backend/app bind mount）⇒ 改完必须重建
docker compose build backend && docker compose up -d backend
# ovcli.conf 对 backend 的 _discover_builtin_plugins() 即时生效，但对 agent 容器需重建镜像
bash scripts/build-agent.sh
# 全部探针必须在容器内跑（WSL2 打不通 127.0.0.1:1933），带 health 等待 + 重试的通用 runner
bash _scratch_p3grun.sh
```

##### I. 对 §11.12.5 旧建议的修正

旧建议的**两个前提都被实测推翻**（已在 §11.12.5 末尾加 ⚠️ 引用块）：落点不是插件侧（`recallExcludeUris` 是死旋钮）；对象不是 `.abstract.md` / `.overview.md` **文件名**（context 模式下 overview 是 `tiers.py` **合成的 tier**，不是独立检索命中，按文件名过滤无从下手），真正的对象是 **7 个结构性目录节点 URI**。收益也被低估了：不是 25% 而是 **38.5%** 的 token 回收，外加 CJK 密度 +138%。「不开 overfetch」「不调 `scoreThreshold`」两条仍然成立。

---

### 11.13 Phase 4 实测记录（性能基准 P4-01 … P4-11）

执行时刻 **2026-09-29 03:31:53 – 03:36:31 UTC**（P4-01…P4-09）与 **04:00 UTC 前后**（P4-11）。
条件：**rerank OFF**（`/metrics` rerank gauge `0.0`）、OV `v0.4.21`、镜像 digest `sha256:569193ef…`、`auth_mode=api_key`、`account_id=agent-platform`、容器 `RestartCount=0`。
中文语料为 Phase 3 的 `ovzh-eval`（40 文档 / 8 类），**保持 pristine**：所有写入类套件走抛弃型用户 `p4-perf-write` / `p4-commit` / `p4-scale`，`ovzh-eval` 全程只读。

#### A. 工装约束（复现前必读）

1. **`benchmark/` 没有挂进 backend 容器** ⇒ `run_perf.py` 与 `queries.jsonl` 必须 `docker cp` 到 `/tmp/p4/`，产物用 `--out` 落容器内再 `docker cp` 回来。
2. **stdout 混有 `PERF-DONE` 结束标记** ⇒ 不能把 stdout 直接重定向成宿主的 `.json`，否则 JSON 无效。一律走 `--out` + `docker cp`。
3. **P4-11 必须在 agent 镜像内跑**：插件的 `node_modules` 不在仓库里（`Glob agent-image/builtin-plugins/openviking/**/*.mjs` 无匹配），只有烘进 `agent-demo:1.4.0` 的那份存在。
4. **零挂载传文件的可靠手法**（本轮验证，优于 heredoc 三层转义或 `-v /mnt/d`）：

   ```bash
   tr -d '\r' < _scratch_p411.mjs | docker run -i --rm --entrypoint sh agent-demo:1.4.0 -c \
     'cat > /tmp/p411.mjs; cd $PLUGIN_DIR; node /tmp/p411.mjs'
   ```

5. **`getkey` 会瞬时返回空串** ⇒ 派生 User Key 的脚本必须重试并加长度门禁 `[ ${#k} -gt 50 ]`。
6. **结果 JSON 里的 `ts` 是套件「开始」时刻，不是结束时刻。** 本轮用算术交叉验证过：`commit` 起于 03:35:03、`recallable_after_s=20.8` ⇒ 终于 03:35:24；`scale` 的 `ts` 恰为 03:35:27。读错这一点会把 R24 的归因整体错位（见 E 段）。
7. Unix 工具链只能在 bash/WSL 侧用：**PowerShell 侧管道接 `head`/`grep` 会 `CommandNotFoundException`**，过滤必须写进 `.sh` 内部。

#### B. 判定总表

| ID | 指标 | 阈值 | 实测 | 判定 |
| --- | --- | --- | --- | --- |
| P4-01 | 检索 P50 | ≤ 300 ms | **139.8 ms**（list）/ 172.5 ms（context） | ✅ |
| P4-02 | 检索 P95 | ≤ 800 ms | **314.8 ms**（list）/ 399.7 ms（context） | ✅ |
| P4-03 | 检索 P99 | ≤ 1500 ms | **335.0 ms**（list）/ 556.2 ms（context） | ✅ |
| P4-04 | `find` P95 | ≤ 500 ms | **382.6 ms**（p50 145.0 / p99 514.0） | ⚠️ 数值达标，**判据前提失真**（C.1） |
| P4-05 | 写入吞吐 | ≥ 20 条/s | `wait=false` **20.7/s** ✅ / `wait=true` **1.76/s** ❌ | ⚠️ **判据需拆成两条**（C.2） |
| P4-06 | 端到端提交 | ≤ 30 s | **20.8 s**（可召回口径，polls=9 × 2 s） | ✅ |
| P4-07 | 代理附加开销 P50 | ≤ 50 ms | **+12.0 ms**（mean +17.3 ms，n=100 交错） | ✅ |
| P4-08 | 10 并发 P95 退化 | ≤ 2× | **1.38×**（435.5 / 314.8），45.8 rps | ✅ |
| P4-09 | 规模不劣化 | 无显著劣化 | p95 268.4→362.5→352.6→274.0→387.7 | ❌ **测量无效**：外部配额耗尽（E 段，R24） |
| P4-10 | 稳态 RSS + 告警线 | 记录并设线 | 负载后 **757.9 MiB**（cgroup 峰值 **964.5 MiB**）；**容器无内存上限** | ⚠️ 见 F 段 |
| P4-11 | 超时降级不炸会话 | `timeoutMs=30000` 生效且降级 | **34/34 PASS**，最坏实测 **15.0 s**、理论上界 **20 s** | ✅ 但**判据表述需改写**（C.3 + D 段） |

八项达标、一项数值达标但判据失真、一项判据需拆分、一项因外部配额失效、一项待设告警线。**没有任何一项指向 OpenViking 的检索或中文能力缺陷。**

#### C. 三条判据失真（阈值表须随之修订）

##### C.1 P4-04 —— `find` 不是「廉价的 L0 端点」

阈值表的方法列写着「L0 ~100 tokens，应显著快于 L1/L2」。实测 `find` 返回的是**三级混合**：

```
levels_returned = {"0": 42, "1": 215, "2": 743}   # 共 1000 hits，avg_hits 5.0
```

L2 占 **74.3%**。而且它并不快：`find` p50 **145.0 ms** ≈ `search`(list) 的 **139.8 ms**，p95 **382.6 ms** 反而**慢于** `search`(list) 的 **314.8 ms**。

⇒「应显著快于」的前提不成立，`382.6 < 500` 的 PASS 是**巧合而非判据成立**。另需记录：`/search/find` 与 `/search/search` 的**响应形状完全相同**（`result` = `{memories, resources, skills, total}`，item keys = `abstract/context_type/level/score/tags/uri`），前序仅看 `item[0]` 的形状探针会误判为「只返回 level=2」。
**建议**：把 P4-04 的方法列改为「`find` 是分组检索端点，返回 L0/L1/L2 混合」，阈值保留 ≤ 500 ms 但删掉「显著快于」的因果假设。

##### C.2 P4-05 —— 一条阈值描述了两个语义不同的通道

`content/write` 的 `wait` 参数决定测的是哪件事，40 篇中文文档实测：

| 通道 | 语义 | per-call p50 | per-call p95 | wall | 吞吐 |
| --- | --- | --- | --- | --- | --- |
| `wait=false` | **API 接受**（异步索引，立即返回） | 39.4 ms | 81.4 ms | 1.93 s | **20.7 条/s** ✅ |
| `wait=true` | **含 embedding 落盘**（阻塞到向量可查） | 489.0 ms | 778.9 ms | 22.7 s | **1.76 条/s** ❌ |

`wait=true` 的 567.5 ms 均值基本就是**一次到阿里云 Model Studio 的 embedding 往返**，与 OV 自身无关。旁证：同一轮 `wait=true` 写 40 篇使 `vector_count` 229→310（**+81 ≈ 2 向量/篇**），说明 embedding 确实同步完成。

⇒ 按 P3-06 / P3-02 的先例处理：**如实报 + 归因 + 建议判据拆分**。
**建议**：P4-05 拆为「**API 接受吞吐 ≥ 20 条/s**」（实测 20.7 ✅）与「**含 embedding 落盘吞吐 ≥ 1 条/s**」（实测 1.76 ✅）两条；后者本质是上游 embedding 服务的 SLA，不应记在 OV 账上。

##### C.3 P4-11 —— `timeoutMs=30000` 不是召回路径的预算

阈值表写「确认 `timeoutMs=30000`（opencode harness 默认）生效」。读插件源码后，这只是**一半**事实：

- `lib/shared/config-schema.mjs`：`timeoutMs` **default 15000**，`harness: {opencode: 30000, dsh: 10000}`，min 1000 / max 300000，env `OPENVIKING_TIMEOUT_MS`。注释解释了差异：「opencode drives an editor session that tolerates a slower call; dsh runs inside a chat host that does not.」
- `lib/memory-recall.mjs:17,25`：**自动召回 hook 主动把它压到 5000 ms**（`fetchJSON(config,"/health",{},{timeoutMs:5000})`，以及 `options.timeoutMs ?? 5000`）。注释：「5s is this hook's own budget for a bare retrieval; when the request also spends a server fuse the helper hands down a longer deadline, and overriding it here would abort a request the server was still inside.」

⇒ 30000 是**连接预算的 harness override**，召回路径的真实预算是 **5 s/腿**。实测两个数都成立（H4 验证默认预算取自 `config.timeoutMs`，T1a 验证 harness override），但**判据表述必须改写**，否则「30000 生效」会被误读成「一次卡死的召回会占用会话 30 秒」。

#### D. P4-11 —— 召回阶梯的完整归因（34/34 PASS，RC=0）

测试不打桩、不用 mock，而是起**两个真实 HTTP 服务**分别复现两种故障模式，因为它们在 `lib/shared/ov-http.mjs` 里走**不同分支**：

| 组 | 故障模式 | `ov-http.mjs` 分支 | 结果 |
| --- | --- | --- | --- |
| **D\*** | 端口无人监听（ECONNREFUSED） | `timedOut === false` | **9/9 PASS**，`error.aborted === undefined`（**未被误标为超时**），message `"fetch failed"`，**53 ms 快速失败** |
| **H\*** | 已监听但永不应答 | `timedOut === true` → `{name:"AbortError", aborted:true}` | **18/18 PASS**，**3005 ms 精确命中 3000 deadline** |
| **T1\*** | 纯配置解析 | — | **7/7 PASS** |

关键单点证据：

- **H3 `timeoutMs:1` 被钳到 1002 ms** ⇒ `MIN_TIMEOUT_MS = 1000` 生效。源码注释解释了动机：「A request that aborts the instant it starts reads as a dead server.」
- **H7c = 5001 ms**：`/health` 门禁命中，**只发出 1 个请求**就返回 `undefined`。
- **H8c = 15029 ms**：半死服务（`/health` 正常应答、`/search/*` 挂起）⇒ 门禁**放行**，hook 走完整阶梯。
- **H9 / D9** 的错误文案可直接运维：`"Request timeout after 2000ms"` 与 `"OpenViking service unavailable at http://127.0.0.1:18099. Start it with: openviking-server --config ~/.openviking/ov.conf"`。
- **`P411-DONE` 正常打印、`timeout 240` 未触发** ⇒ `ov-http.mjs` 的 `finally { clearTimeout(timer) }` 生效，**无定时器泄漏**，事件循环自行排空。

**阶梯形状（本轮最重要的新增结论）** —— 通过 `options.log(stage, data)` 插桩 `recall-core` + 在假服务器侧记录每个请求的 `at_ms` 得到：

| # | stage（`recall-core` 上报） | 端点 | 累计 |
| --- | --- | --- | --- |
| 1 | `recall_context_face_error` | `POST /api/v1/search/search` | 5003 ms |
| 2 | `recall_endpoint_fallback` | `POST /api/v1/search/recall` | 10004 ms |
| 3 | `recall_search_summary` | `POST /api/v1/search/find` **× 2（并发）** | 15005 ms |

第 3 腿发出**两个** `find` 请求，但 `at_ms` **完全相同（18114 / 18114）⇒ 并发而非串行**，所以该 stage 只花 5 s 而非 10 s。整条阶梯 = **3 个串行 stage × 5 s = 15 s，共 4 个 HTTP 请求**。

⇒ **每腿各自持有 5 s deadline，腿之间没有共享 deadline。** 这既是设计（每一腿都可能触发服务端保险丝，过早放弃会打断服务端仍在处理的请求），也是风险（腿数增加会线性放大最坏耗时）。

**最坏情形上界 = 5 s（`/health` 门禁）+ 15 s（阶梯）= 20 s < 30 s 天花板** ⇒ P4-11 判 PASS。实测 15.0 s（H8，`/health` 立即应答）。

⚠️ **必须记录的局限**：`/health` 门禁**只能挡住「全死」**。一个「`/health` 正常但 `/search/*` 卡住」的 OV（embedding 模型 hang、后端队列饱和、上游限流重试）仍会让**单次会话轮付出 15 s**。门禁因此是个**弱预测器**，不能当作「有门禁就不会慢」的依据。

#### E. R24 —— embedding 配额耗尽使 P4-09 失效（新风险，高优先级）

P4-09 的规模曲线**不是一次有效测量**。真因不在 `run_perf.py`，而在**外部配额**：

```
2026-09-29 03:35:41,212  WARNING  Failed to generate embedding: OpenAI API error:
  Error code: 429 - {'error': {'message': 'Allocated quota exceeded, please increase
  your quota limit.', 'type': 'insufficient_quota', 'code': 'insufficient_quota'}}
  (uri=viking://user/p4-scale/memories/cases)
2026-09-29 03:35:41,256  WARNING  Embedding circuit breaker is open; re-enqueueing messages
```

时间线对齐（用 A.6 确认的「`ts` = 开始时刻」）：

| 套件 | 开始（UTC） | 相对首个 429 |
| --- | --- | --- |
| latency / latency-ctx / find / proxy / concurrency | 03:31:53 – 03:34:34 | **早于**，干净 |
| write（40 docs） | 03:34:39（wall 24.6 s ⇒ 终于 03:35:04） | **早于**，干净 |
| commit | 03:35:03（+20.8 s ⇒ 终于 03:35:24） | **早于**，干净 |
| **scale（500 docs）** | **03:35:27** | **首个 429 落在 +14 s** ❌ |

全日志 **99 次 429，零个其他错误码**（`grep -oE "Error code: [0-9]+" | sort | uniq -c` 只有 `99 Error code: 429`），且 URI 全部指向 `viking://user/p4-scale/…`。

⇒ **P4-01…P4-08 与 P4-11 不受影响；只有 P4-09 被污染。** 这解释了此前无法归因的两个异常：

- `vector_count` 500 docs 只涨 **+151**（312→463），而 `wait=true` 的 write 套件是 **2 向量/篇**（40 docs → +81）⇒ 500 篇若全部索引应 ≈ +1000。
- **cp200 的 `vector_count` 与 cp100 完全相同（都是 390）** ⇒ 索引在断路器打开期间**冻结**。

原判「`run_perf.py` 的判稳循环（连续 3 轮 × 3 s = 9 s）不足」是**次因**：即使判稳无限延长也等不到索引，因为 embedding 根本做不出来。**规模曲线的 x 轴（docs_written）与真实向量规模脱钩**，p95 序列 268.4→362.5→352.6→274.0→387.7 因此不构成「延迟不随规模劣化」的证据。

**后续状态**（04:10 – 04:16 UTC 复查）：

- 积压**落在命名卷里并跨容器重启存活**：`find /app/.openviking/data -path "*p4-scale*" | wc -l` = **523**；`_system/queue/{queue.db,queue.db-shm,queue.db-wal}` = **6.9 MiB**；数据目录共 63 MiB。
- 04:03 的日志仍在 `re-enqueueing messages`（此时已历经两次重启），OV CPU **99.5 %–105.6 %**。
- 04:03 – 04:13 期间**整个 compose 栈反复重启**（`docker ps -a` 显示 backend / frontend / postgres / searxng / demo-mcp / openviking / agent-p2probe **同时** "Up 22 seconds"），OV 的 `health` 探针连续 `exit=7`（curl 连不上）。自 **04:13:14** 起 `StartedAt` 冻结、`health=200` 稳定 ≥ 100 s，churn 停止。这是**栈级**现象，不是 OV 崩溃循环（`OOMKilled=false`、`ExitCode=0`）。
  > ⚠️ **归因更正（R25）**：这段 churn **不是** R24 的重试风暴造成的，而是宿主 WSL2 发行版用户态被反复拆除重建 ⇒ dockerd 被杀 ⇒ restart-manager 把所有 `unless-stopped` 容器一并拉起。证据是 `agent-p2probe` **没有任何 compose 标签**却与 6 个 compose 容器同秒（±30 ms）重启，而它的 `HostConfig.RestartPolicy.Name` 同样是 `unless-stopped`。R24 只负责 CPU 打满与索引冻结，**不负责重启**。完整取证见 §11.14。
- 重启后 3 个观察窗（各 90 s）**429 = 0、breaker = 0**，但**不能据此判定配额已恢复**——断路器可能仍在长冷却期，OV 未必在重试积压。**未主动打探针写以免再消耗配额。**

**R24 运行态已结案（自愈，门禁复绿）**：`vector_count` 由 **463 → 813**（+350，说明积压最终被消化完毕），最后一次 429 停在 **04:03:04.098**，此后所有观察窗 429/breaker/embed_fail/re-enqueue **全为 0**；`_system/queue` 的 `queue.db`/`queue.db-wal` mtime **冻在 04:06:13/14 不再增长**（`queue.db-shm` 仍随每次连接被触碰，属正常）；OV CPU 由 99.5 %–105.6 % 回落到 **2.07 %**、mem **638.4 MiB**；`health=200 ready=200`、`embedding`/`vlm` 的 `model_usage_available{valid="1"}` 均为 **1.0**。**⇒ Phase 5 的写入类用例可以开跑。** 但代码层缺陷（无重试上限、无死信队列、无写入背压）**一个都没修**，配额再被撞穿会原样复现。

这也**反向坐实了 R24 的归因**：P4-09 的 cp200 `vector_count` 与 cp100 同为 **390** 确实是"索引冻结"而非测量噪声——`p4-scale` 的 515 个 `.md` 在 04:00 前就已全部落盘（`find -newermt "2026-09-29 04:00"` = **0**），而向量数直到配额恢复后才涨到 813。

**风险登记 R24（建议加入 §7）**：

| 项 | 内容 |
| --- | --- |
| 风险 | 共享 OV 实例 + 计量制 embedding 上游 + **写入侧无背压** ⇒ 任一用户的批量写入可耗尽全局配额，断路器打开后消息被重新入队到**持久化队列**，跨重启无限重试 |
| 影响 | ① 索引静默滞后（写入返回 200，但记忆检索不到）；② 重试风暴占满 CPU，`/health` 探针 `exit=7`，进而波及**同栈所有租户**；③ 故障与「隔离失效」表象相似，易误诊 |
| 缓解 | 写入侧限速/配额分摊；对 embedding 429 设**重试上限 + 死信队列**而非无限 re-enqueue；把 `insufficient_quota` 计入告警；批量导入走独立 account 或独立 key |
| 状态 | **运行态已自愈**（配额恢复、积压消化、`vector_count 463→813`、CPU 回落 2.07 %、门禁四项全 0）；**代码缺陷未修复**，可复现 |

#### F. P4-10 —— 内存：测到了数，但「告警线」无从设起

| 时刻 | openviking | backend |
| --- | --- | --- |
| 负载中（mid） | **678.5 MiB** / 15.51 GiB（4.27 %），CPU 3.69 % | 124.9 MiB（0.79 %），CPU 0.15 % |
| 负载后（after） | **757.9 MiB**（4.77 %），CPU 2.70 % | 124.9 MiB，CPU 0.25 % |
| cgroup 峰值 | `memory.current` = **1011306496 B = 964.5 MiB** | — |
| 冷启基线（栈重启后） | **395.3 MiB**（`health=starting`，模型仍在加载，非稳态） | 84.2 MiB |

**关键事实：容器没有任何内存上限。**

```
HostConfig.Memory     = 0        # 0 = unlimited
HostConfig.MemorySwap = 0
/sys/fs/cgroup/memory.max = max
docker-compose.yml 的 openviking 服务块内无 mem_limit / deploy.resources
```

⇒ 阈值表要求的「设告警线」目前**无配置载体**：不存在会被触发的 OOM 边界，一次泄漏会吃掉整个 Docker Desktop VM（15.51 GiB）而不是只杀掉 OV。

另需 caveat：**964.5 MiB 的峰值是在 R24 的队列积压期间测得的**，含未消化 backlog 的压力，**不是纯净的稳态负载值**。

**建议**：① 在 compose 里为 `openviking` 显式设 `mem_limit`（以 964.5 MiB 峰值 × 1.5 ≈ **1.5 GiB** 起步）与 `cpus`，让越界表现为**单容器 OOM 重启**而非全栈拖垮；② 告警线设在 **1.0 GiB**（= 本轮实测峰值），触发即查队列积压与 embedding 失败率；③ 待 R24 修复、配额恢复后**重测一次纯净稳态**再收紧这两条线。

#### G. R19 的又一次实证

四个用户（`ovzh-eval` / `p4-perf-write` / `p4-commit` / `p4-scale`）在收尾时查询 `debug/vector/count`，**返回值全部是 463**（= 全局值）。再次确认 **`debug/vector/count` 是 account 级聚合，不可用作 per-user 计量或隔离证据**（详见 R19 / P5-19）。

#### H. 产物与复现

```
benchmark/ov-zh/run_perf.py           # Phase 4 harness（--suite latency|find|proxy|concurrency|write|commit|scale）
benchmark/ov-zh/out-p4-latency.json       # P4-01/02/03，mode=list，n=200，warmup=10，target=direct
benchmark/ov-zh/out-p4-latency-ctx.json   # 同上，mode=context（含 used_tokens 分布）
benchmark/ov-zh/out-p4-find.json          # P4-04
benchmark/ov-zh/out-p4-proxy.json         # P4-07，n_each=100，interleaved，shuffle seed=4
benchmark/ov-zh/out-p4-concurrency.json   # P4-08，n=200，workers=10
benchmark/ov-zh/out-p4-write.json         # P4-05，docs=40，user=p4-perf-write
benchmark/ov-zh/out-p4-commit.json        # P4-06，user=p4-commit，poll_interval=2.0
benchmark/ov-zh/out-p4-scale.json         # P4-09（❌ 无效，见 E 段）
benchmark/ov-zh/out-p4-11.txt             # P4-11 全量输出（34 项断言 + counts + seen 请求时间线）
```

`out-p4-latency-ctx.json` 的 `used_tokens`：**min 672 / max 875 / mean 763.1 / p50 761 / p95 861 / p99 875** ⇒ context 模式下每次召回注入约 **760 tokens**，恒在服务端 `max_tokens` 之内，与 §11.12.10 的「注入超预算 0 次」一致。

`out-p4-proxy.json` 的 `sample_body`：`{"mode":"context","purpose":"coding","query":"上次挂的那回"}`。

P4-07 附带的混淆因素须记录：**代理不是纯透传**——`_without_scaffold` 在 context 模式会注入 `exclude_uris`（§11.12.10 F）。因此 +12.0 ms 是「代理中继 + 脚手架滤除」的合计开销，不是纯转发开销。前序冒烟测得的 **−139.1 ms 已排除**：n=5 小样本噪声，n=100 全量为正值；且与 P2-09 单发 145 KB 往返的 ~14 ms 吻合。

```bash
# P4-01..P4-09（在 backend 容器内跑，产物 docker cp 回来）
bash _scratch_p4run.sh
# P4-11（在 agent 镜像内跑；timeout 240 兼作泄漏定时器探测器）
bash _scratch_p411.sh
```

> **重跑 P4-09 的前置条件**：embedding 配额恢复 **且** `p4-scale` 的 523 条积压清空（否则断路器仍会打开）。重跑前应换用新的抛弃型用户，并把 `run_perf.py` 的判稳循环从「连续 3 轮 × 3 s」放宽到「`vector_count` 达到 `docs × 2` 或超时」。
> **✅ 两个前置条件均已满足**（配额恢复、`vector_count 463→813`、`queue.db`/`-wal` mtime 冻在 04:06）。**新增第三个前置条件（R25）**：跑之前先确认 `dockerd` 的 `etimes` 大于预计套件时长，否则整轮数据会被一次宿主回收作废。

---

### 11.14 宿主环境取证：栈级反复重启的真因是 WSL2 用户态回收（R25）

Phase 4 期间反复观测到"整个 compose 栈同时重启"，最初被归因给 R24 的 embedding 重试风暴。本节记录**推翻该归因**的完整取证链，以及一个**被证伪的错误假设**。

#### A. 决定性矛盾：`who -b` 与 `/proc/uptime` 不可能同时为真

```
uptime_sec=97633.19          → up 1 day, 2:57   （内核 / 轻量 VM 未重启）
who -b  →  system boot  2026-09-29 13:12        （探针运行时 now=13:12:31，即 19 秒前才"开机"）
journalctl --list-boots:
  IDX 0  e834e50f91024ad0b924e4d0f55291ee  Tue 2026-09-29 10:27:10 CST → 13:12:31 CST
```

`boot_id` 来自 `/proc/sys/kernel/random/boot_id`，是**内核属性**。它在整轮观测中**从未变化**，而 `who -b`（读 `/run/utmp`，属用户态）却一路前移 10:26 → 13:02 → 13:12。

⇒ **WSL2 的内核/VM 一直在跑，被反复拆除重建的只是 Ubuntu-24.04 发行版的 systemd 用户态。**

#### B. 守护进程寿命与 PID 循环

```
containerd  pid=192→210   etimes=175→(新)   lstart=Tue Sep 29 12:57:21 / 13:12:20 2026
dockerd     pid=305→287→304→303   etimes=174→9   lstart=Tue Sep 29 12:57:22 / 13:12:21 2026
containerd-shim-runc-v2 ×7   pid=651..680   etimes=172   lstart=12:57:25

systemctl show docker.service:
  NRestarts=0        ExecMainPID=287(→303)
  ExecMainStartTimestamp=Tue 2026-09-29 12:57:26 CST (→ 13:12:22)
  ActiveEnterTimestamp=Tue 2026-09-29 12:57:44 CST (→ 空 = 仍在 activating)
systemctl show containerd.service:  NRestarts=0   ActiveEnterTimestamp=12:57:26
```

**`NRestarts=0` 是关键否证**：如果是 systemd 的 `Restart=` 策略在救活崩溃的 dockerd，该计数器必然递增。它恒为 0 ⇒ **是整个 unit 被重新拉起，而不是进程崩溃后被重启**。20 分钟内 `dockerd` PID 走了 `305 → 287 → 304 → 303` 四轮。

#### C. 上一次关机留下的原因（当前 journal 的**头部**）

```
10:26:44 systemd-resolved[165]: Clock change detected. Flushing caches.
10:26:44 systemd-journald[47]: Time jumped backwards, rotating.
10:26:44 systemd[1]: dmesg.service: Deactivated successfully.
10:26:44 systemd[1]: dmesg.service: Consumed 20.427s CPU time.
10:26:46 Exception:
10:26:46 unknown: Operation canceled @p9io.cpp:258 (AcceptAsync)
10:26:46 systemd-logind[182]: The system will power off now!
10:26:46 systemd-logind[182]: System is powering down.
10:26:46 systemd[1]: Stopping session-279.scope - Session 279 of User zhangzhixiao...
```

- **`systemd-logind` 主动 power off**，不是崩溃、不是 OOM。
- **`p9io.cpp`** 是 WSL 的 **9P/drvfs 文件共享 I/O 层**，承载 `/mnt/d`。它的 `AcceptAsync` 被取消紧跟在 poweroff 之前 ⇒ 宿主回收时 `/mnt/d` 上的在飞 I/O 被直接掐断。**这是本仓库所在路径**，属实质性数据风险。
- `Time jumped backwards, rotating` ⇒ journal 被轮转，**历史证据丢失**。

#### D. 一轮完整的拆除 → 重建（实测时间线）

```
# 拆除
13:01:08 systemd[2277]: Reached target shutdown.target - Shutdown.
13:01:08 systemd[2277]: Reached target exit.target - Exit the Session.
13:01:08 systemd[1]: Stopped user@1000.service - User Manager for UID 1000.
13:01:08 systemd[1]: Stopping systemd-user-sessions.service - Permit User Sessions...
13:01:08 systemd[1]: Stopping user-runtime-dir@1000.service
13:01:08 systemd[1]: Stopped target remote-fs.target - Remote File Systems.

# 重建（间隔 57 s）
13:02:05 systemd[1]: Reached target time-set / nss-lookup / sysinit / paths / timers / sockets / basic / getty-pre / getty
13:02:10 dockerd[304]: "restarting container" container=04aba8ab… exitCode=1
                       exitedAt="2026-09-29 05:02:10.505974606 +0000 UTC"
                       manualRestart=false restartCount=1 restartPolicy="{unless-stopped 0}"
13:02:15 systemd[2419]: Reached target paths/timers/sockets/basic/default （用户会话）
13:02:24 systemd[1]: Reached target multi-user.target / graphical.target

systemd-analyze @13:02:16 → Bootup is not yet finished (FinishTimestampMonotonic=0)
```

**单次 boot > 20 s**。dockerd 自身的一段：

```
12:55:30 dockerd[305] received task-delete event from containerd  container=f8dafcf9…
12:55:39 dockerd[305] Container failed to exit within 10s of signal 15 - using the force  container=9e59586c…
12:55:39 dockerd[305] stopping restart-manager                                          container=9e59586c…
12:55:39 dockerd[305] Container failed to exit within 10s of signal 15 - using the force  container=cfe833e0…
12:57:26 systemd[1]: Starting docker.service - Docker Application Container Engine...
12:57:26 dockerd[287] Starting up
12:57:27 dockerd[287] Loading containers: start. / containerd snapshotter integration enabled
12:57:27 dockerd[287] error unmounting container cfe833e0… error="layer not mounted"
12:57:27 dockerd[287] error unmounting container 9e59586c… error="layer not mounted"
12:57:27 dockerd[287] Deleting nftables IPv4/IPv6 rules error="Could not process rule: No such file or directory … delete table ip docker-bridges"
12:57:28 dockerd[287] Removing stale sandbox cid=9e59586ce4f6 isRestore=false
12:57:28 dockerd[287] Removing stale sandbox cid=cfe833e00e4c isRestore=false
12:57:29 dockerd[287] sbJoin ×7 （searxng→agent-net / postgres→platform-net / frontend→platform-net /
                                 agent-p2probe→agent-net / backend→platform-net / openviking→agent-net / demo-mcp→agent-net）
12:57:29 dockerd[287] Loading containers: done. / version=29.6.1 containerd-snapshotter=true
12:57:44 dockerd[287] Completed buildkit initialization / Daemon has completed initialization / API listen on /run/docker.sock
```

两条推论：
1. **`10 s SIGTERM 宽限 → 强杀`** 解释了先前观测到的"两个 `finished` 时间簇"。
2. **18 s 的 dockerd boot（12:57:26→12:57:44）** 解释了 dmesg 里的 `WSL (2 - init-systemd(Ubuntu-24.04)) ERROR: WaitForBootProcess:3488: /sbin/init failed to start within 10000ms` —— boot 超出了 WSL 的 10 s 预算，这是**症状而非病因**。

#### E. 被证伪的假设：「双 compose config 导致 config-hash 抖动」

`docker compose ls --all` 确实显示同一 project 挂两个 config：

```
agent-docker-demo   running(6)   /mnt/d/Project/agent-docker-demo/docker-compose.yml,/home/zhangzhixiao/agent-docker-demo/docker-compose.yml
openviking-repo     exited(1)    /home/zhangzhixiao/.openviking/openviking-repo/docker-compose.yml
```

容器标签也确实**分裂**：

| 容器 | `working_dir` / `config_files` | compose 标签 |
| --- | --- | --- |
| `openviking`、`backend` | `/mnt/d/Project/agent-docker-demo` | 完整 |
| `postgres`、`frontend`、`searxng`、`demo-mcp` | `/home/zhangzhixiao/agent-docker-demo` | 完整 |
| **`agent-p2probe`** | **（全部为空）** | **无** |

两份 compose 是**真实存在**的卫生问题（陈旧副本 mtime `2026-09-10 20:40`、7067 B、md5 `3b138050610850a8346b2ca95352b474`、`grep -c -i openviking` = **0**；活动副本 mtime `2026-09-29 09:12`、16120 B、md5 `8876af692d51adf59de41c9a68a5891c`、含 `openviking:156` 与 `openviking-data:298`；两边都无 `container_name` / 顶层 `name:` / `COMPOSE_PROJECT_NAME` 覆盖 ⇒ project 名都由目录名推导为 `agent-docker-demo`，故冲突），**但它不是重启的原因**：

> **`agent-p2probe` 没有任何 compose 标签，却与 6 个 compose 容器同秒（±30 ms）启动** —— `docker compose` 永远不会碰它。

真因在 **restart policy**（`_scratch_p417.sh` [B]，全 14 个容器）：

```
agent-docker-demo-backend-1     policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.139Z
agent-docker-demo-openviking-1  policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.134Z
agent-p2probe                   policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.093Z
agent-docker-demo-frontend-1    policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.124Z
agent-docker-demo-searxng-1     policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.115Z
agent-docker-demo-demo-mcp-1    policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.089Z
agent-docker-demo-postgres-1    policy=unless-stopped  rc=0  running=true   started=2026-09-29T05:12:24.131Z
openviking（已退役 WSL dev 实例） policy=unless-stopped  rc=0  running=false  started=2026-09-28T04:55:40Z
agent-3c2e2f2a… / f4f5d8ce… / f0b2f7e5… / 20873ad8… / 92396af7… / 982aafb0…
                                policy=unless-stopped  rc=0  running=false  （09-01…09-28）
```

⇒ **dockerd 重启后，restart-manager 把所有"当时在跑"的 `unless-stopped` 容器一并拉起，无论它们是否由 compose 管理**。7 个容器的 `started` 全落在 **50 ms 窗口内**，`rc=0` 是因为 `RestartCount` 只统计**容器自身**的重启，不统计 daemon 重生。6 个 exited `agent-*` 与已退役的 `openviking` 是**手动停的**，`unless-stopped` 正确地把它们留在停止态 ⇒ 与观测一致。

#### F. 其余假设的逐一排除

| 假设 | 反证 |
| --- | --- |
| OV 崩溃循环 | `RestartCount=0`；OV 日志 04:20–04:33:30 grep `shutdown\|sigterm\|sigkill\|stopping\|stopped\|traceback\|critical\|fatal` **零命中**；`exit=0` |
| healthcheck 触发重启 | `Health={"Test":["CMD","openviking-entrypoint","--healthcheck"],"Interval":30s,"Timeout":5s,"StartPeriod":30s,"Retries":3}`；探针 `exit=0` 返回 `{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key"}`；`exit=7 curl: (7) Failed to connect to 127.0.0.1 port 1933` 落在 `start_period` 内且 `RestartCount` 未增 |
| OOM | `OOMKilled=false`；mem 638.4 MiB / 15.51 GiB |
| WSL VM / 内核重启 | `/proc/uptime` = 1 day 2:57；`boot_id` 未变 |
| systemd 自动重启 docker | `NRestarts=0`（docker + containerd 都是） |
| `docker compose` 编排 | `agent-p2probe` 无 compose 标签却同步重启；3 min `ps` 高频监视 **`raw watcher hits: 0 lines`**；流式 `docker events` 未捕获任何 compose/docker-CLI 进程 |
| 我自己的脚本 | `_scratch_*` 里所有 compose 命令**只针对 `openviking` 单服务**（`_scratch_p1.sh:59/61/84`、`p3a:24`、`p3b:15`、`p3e:60,69`、`p3g:44`、`p3h:39,93`、`p3i:57`、`p3m:31`、`p3n:93`）⇒ 不可能停 postgres/searxng/frontend/demo-mcp/backend |
| cron / systemd timer | `no crontab for zhangzhixiao`；`/etc/cron.d` 只有 `.placeholder`/`e2scrub_all`/`sysstat`；12 个 timer（man-db、systemd-tmpfiles-clean、apt-daily、motd-news、dpkg-db-backup、logrotate、apt-daily-upgrade、e2scrub_all + 4 disabled）无一与 compose 相关 |
| 陈旧副本里的 `probe_alive.sh` | 全文 = "Probe 4: MCP hiding via preset mcps exclusion + global top-level deny"，是对 `agent-f4f5d8ce-…` 容器 4096 端口打 opencode 权限 PATCH 的探针，**与 compose 生命周期无关** |
| 配置文件在事发时被改 | `.env` 09-29 01:22:46 / `backend/.env` 09-28 12:13:23 / `docker-compose.yml` 09-29 09:12:47 / `openviking/ov.conf` 09-28 20:16:13（全 +0800）⇒ 都早于事发；`find -mmin -45` 只列出 `docs/openviking-integration-test-plan.md` 与 `_scratch_p410d/e/f.sh, p412..p415` |
| Windows 侧 Docker Desktop | `tasklist.exe \| grep -iE "docker\|com\.docker"` **零匹配**；`wsl.exe -l -v` 只有 `Ubuntu-24.04 Running 2` ⇒ dockerd 是 WSL 内原生 |

#### G. 「稳定窗」只在会话连续时存在

两次活体捕获的对照说明这一点：

| 探针 | 窗口 | 结果 |
| --- | --- | --- |
| `_scratch_p414.sh` | 04:40:52 – 04:45:19（4.5 min，10 采样） | `ov_started=04:40:31.736` **恒定**、`ov_health=200`、`running_containers=7`；事件直方图 `30 exec_start / 30 exec_die / 30 exec_create / 1 health_status` ⇒ **零个 die/start/stop/kill**；事后 `health=healthy restarts=0`、最近 6 min embedding 错误 = **0** |
| `_scratch_p416.sh` | 04:57 – 05:00（3 min） | 容器级事件捕获**为空**、`compose/docker-CLI processes seen` **为空**、`raw watcher hits: 0 lines`；但收尾时 `started=04:57:28.800/.792/.775`，`now=05:00:53` ⇒ **重启发生在探针启动之前的一瞬间** |

**重启时刻表（UTC）**：`04:13:14 → 04:32:49 → 04:35:29/30 → 04:37:25 → 04:37:27 → 04:38:05/06 → 04:40:30/33 → 04:40:31-34 → 04:54:23 → 04:55:39（dockerd[305] 收 SIGTERM）→ 04:57:21-28（dockerd[287]）→ 05:01:08（拆除）→ 05:02:05-24（dockerd[304]）→ 05:12:20-24（dockerd[303]）`。间隔从 2–3 min 到 14 min 不等，**与命令调用间隙高度相关**。

**⇒ 工装结论：任何"长采样"探针都必须假定自己会被宿主回收打断。** 采样脚本要能自证窗口完整性（把 `dockerd` 的 `etimes` 一并打进每个采样点），否则产出的"稳定"结论不可信。

#### H. 宿主配置现状

```
/etc/wsl.conf:
  [boot]
  systemd=true
  [user]
  default=zhangzhixiao
  （无 [boot] command=）

%USERPROFILE%\.wslconfig:  不存在  ⇒ 全部走 WSL 默认值（含 vmIdleTimeout）
```

#### I. 产物

```
benchmark/ov-zh/out-p4-14.txt   # 4.5 min 稳定窗（10 采样 + 事件直方图）
benchmark/ov-zh/out-p4-16.txt   # 3 min 高频 ps 监视 + compose 标签分裂取证
benchmark/ov-zh/out-p4-17.txt   # R24 门禁复绿 + 全 14 容器 restart policy + 宿主寿命
```

**处置结论：不改 `docker-compose.yml`、不改 `ov.conf`、不改任何应用代码。** 这是宿主环境问题，与 OpenViking 的容器化正确性无关；Phase 1/2 的结论不受影响（它们验证的是配置与网络契约，不依赖长时稳定性）。唯一需要带走的是 §G 的工装约束与 §7 R25 的取证纪律。

---

### 11.15 Phase 5 Group A 实测记录（兼容与隔离 P5-01 … P5-06）

**执行日期**：2026-09-29（UTC 05:40 – 06:20，跨越 3 次栈级重启，见 §G）。
**范围**：P5-01 跨用户越权 / P5-02 令牌伪造 / P5-03 头部提权 / P5-04 管理面不可达 / P5-05 真 Key 不下发 / P5-06 角色边界矩阵。**结论：6 项全 PASS，两个硬性项（越权 0、真 Key 0）零例外。**

#### 0. 工装与被测对象

| 角色 | 实体 | 关键事实 |
| --- | --- | --- |
| A（攻击者） | 容器 `agent-p2probe`，`user_id=p2probe`，`HOSTNAME=cfe833e00e4c` | 新镜像 `sha256:93d984254d3e`（P3-G 后重建，含插件 + `ovcli.conf`）；env 里有 `OPENVIKING_API_KEY`（Fernet 代理令牌，`len=100`，head=`gAAAAA`） |
| B（受害者） | 用户 `ovzh-eval` | Phase 3 评测集语料持有者，其 OV user key `len=119` |
| 代理 | `agent-docker-demo-backend-1`，`http://backend:8000/ov` | `docker exec -i -w /app … python3 -` 可直接 `from app.config import settings` / `from app.services import ov_access, opencode_config` |
| 服务端 | `agent-docker-demo-openviking-1`，`127.0.0.1:1933` | v0.4.21，`auth_mode=api_key`，`account_id=agent-platform` |
| 三把真凭据 | ROOT key `len=64`（head `b31fd5`）／`AGENT_SECRET_KEY` `len=64`／admin(`platform-admin`) key `len=126` | 全部**只在 backend 进程内**取出，经 **stdin** 落到 A 的 `/tmp`，用后 `os.remove`（见 §D 的假阳性教训） |

复现命令模式（所有 Phase 5 脚本同此形）：

```
bash -lc "cd /mnt/d/Project/agent-docker-demo && tr -d '\r' < _scratch_p5X.sh > /tmp/p5X.sh \
          && bash /tmp/p5X.sh 2>&1 | tee benchmark/ov-zh/out-p5-X.txt"
```

#### A. P5-01 跨用户越权 = 0 次泄露（12 条路径全 403）

**REST 面**（经 `http://backend:8000/ov`，带 A 自己的代理令牌）：

| # | 探针 | 结果 |
| --- | --- | --- |
| 1a | `POST /api/v1/content/read` `uri=viking://user/ovzh-eval/memories/identity.md` | **403** `PERMISSION_DENIED` |
| 1b | `POST /api/v1/content/read` `uri=viking://user/ovzh-eval/memories/preferences/.overview.md` | **403** |
| 1c | `GET /api/v1/fs/ls?uri=viking://user/ovzh-eval/memories` | **403** |
| 1d | `GET /api/v1/fs/ls?uri=viking://user/ovzh-eval` | **403** |
| 1e | `GET /api/v1/fs/ls?uri=viking%3A%2F%2Fuser%2Fovzh-eval%2Fmemories`（percent-encoded） | **403** |
| 1f | `POST /api/v1/search/search` `mode=list` + `target_uri=viking://user/ovzh-eval` | **403** |
| 1g | `POST /api/v1/search/find` + `target_uri=viking://user/ovzh-eval` | **403** |

标准拒绝报文（所有 REST 越权一致）：

```json
{"status":"error","result":null,"error":{"code":"PERMISSION_DENIED",
 "message":"Access denied for viking://user/ovzh-eval/memories/identity.md",
 "details":{"resource":"viking://user/ovzh-eval/memories/identity.md"}},
 "telemetry":null,"profile":null}
```

**MCP 面**（经 `http://backend:8000/ov/mcp`，streamable HTTP POST + dual `Accept`）：

| # | 工具 | 结果 | `isError` |
| --- | --- | --- | --- |
| 2a | `list` `uri=viking://user/ovzh-eval` | `"Error executing tool list: Access denied for viking://user/ovzh-eval"` | **true** |
| 2b | `tree` `uri=viking://user/ovzh-eval` | 同上（`tool tree`） | **true** |
| 2c | `grep` `uri=viking://user/ovzh-eval` `pattern=.` | `"grep failed for every pattern:\n  .: PermissionDeniedError: Access denied for viking://user/ovzh-eval"` | **true** |
| 2d | `glob` `pattern=**` `uri=viking://user/ovzh-eval` | `"Error: Access denied for viking://user/ovzh-eval"` | **false** ⚠️ |
| 2e | `read` **`uris=[viking://user/ovzh-eval/memories/identity.md]`** | `"Access denied for viking://user/ovzh-eval/memories/identity.md"` | **false** ⚠️ |
| 2f | `read` `uris=[viking://user/ovzh-eval/memories/preferences/.overview.md]` | `"Access denied for …"` | **false** ⚠️ |
| 2g | `search` `query=…` + `target_uri=viking://user/ovzh-eval` | `"Error executing tool search: Access denied for viking://user/ovzh-eval"` | **true** |

⇒ `isError` 三真两假的不一致即 **R26**。

**对照组（A → A，全 200，证明探针本身有效）**：

- `fs/ls viking://user/p2probe/memories` → `preferences`（isDir，modTime `2026-09-28T07:24:55.000Z`，abstract `[Directory abstract is not ready]`）+ `identity.md`（`size=439`）
- MCP `list uri=viking://user/p2probe` → `[dir] memories / peers / privacy / resources / sessions / skills`（6 个一级目录）
- MCP `read uris=[viking://user/p2probe/memories/identity.md]` → 200，正文 `# identity.md - Who Am I?` … `- **Creature:** AI assistant` …（439 B 全文）
- `~` 家目录别名 → **200**，且只解析到 `viking://user/p2probe/...`（与 `core/namespace.resolve_current_user_uri` 的说法一致）

**两条判据须修正**：

1. **`search` 的 `mode=context` 不接受 `target_uri`** → **400 `INVALID_ARGUMENT`「target_uri is not supported in mode='context'」**。原设计想用 context 面做越权探针，**该组合根本发不出去** ⇒ 越权探针一律用 `mode=list`。
2. **"B 独有 marker" 语义搜索命中 0.8368 不是泄露**。命中的 URI 是 `viking://user/p2probe/memories/preferences/user/日志保留策略.md` ⇒ **是 A 自己的记忆**（Phase 2 给 `p2probe` 灌过同一句），marker 并非 B 独有。这条曾一度被读成"语义搜索可跨用户命中"，**属判据设计缺陷而非服务缺陷**。

#### B. P5-02 令牌伪造 = 全 401

| # | 伪造方式 | 结果 |
| --- | --- | --- |
| 1 | 空令牌（`Authorization: Bearer `） | **401** |
| 2 | 末位字符翻转 | **401** |
| 3 | 首位字符翻转 | **401** |
| 4 | 截断到 32 字符 | **401** |
| 5 | 字面量 `ovproxy:p2probe`（未加密的明文形态） | **401** |
| 6 | 字面量 `ovproxy:platform-admin` | **401** |
| 7 | 纯垃圾串 | **401** |
| 8 | **令牌放 query string**（`?…&api_key=<valid>`） | **401**（不认 query） |
| 9 | `X-API-Key: bogus` + `Authorization: Bearer <valid>` 双头冲突 | **401** |

统一报文：`{"error":{"message":"无效的记忆服务访问凭据，请联系管理员重建容器。"}}`。

第 9 条的机制在 `_caller_user_id`（`ov_proxy.py:266-281`）：**`x-api-key` 优先，一旦存在就不再回落 `Authorization`** ⇒ 攻击者无法用"坏头遮蔽好头"或"好头兜底坏头"做任何组合。第 5/6 条证明 `PROXY_TOKEN_PREFIX="ovproxy:"` 的明文形态不是可用凭据（`verify_proxy_token` 走 Fernet 解密，失败一律返回 `None`，`ov_access.py:82-90`）。

#### C. P5-03 头部提权 = 全 403，并用直连对照坐实凭据本身有效

**H1–H6 伪造 identity 头**（都带 A 自己的合法代理令牌）：

| # | 注入头 | 结果 |
| --- | --- | --- |
| H1 | `X-OpenViking-Account: ovzh-eval` | **403** |
| H2 | `X-OpenViking-User: ovzh-eval` | **403** |
| H3 | `X-OpenViking-Actor-Peer: ovzh-eval` | **403** |
| H4 | 上述三者齐发 | **403** |
| H5 | `X-OpenViking-User: platform-admin` | **403** |
| H6 | `X-OpenViking-Account: agent-platform` | **403** |

⇒ 这些头**不在 `_REQUEST_DROP_HEADERS`（12 项）里、确实被原样转发**，但上游在 `api_key` 模式下**完全无视**它们。与 `container_manager.py:519-522` 的设计意图双向印证：「`OPENVIKING_AUTH_MODE` pins `api_key` so the plugin never sends `X-OpenViking-Account` / `-User` identity headers: the real identity is inside the key the proxy injects, and a container-supplied one would be a lie」。

**H7–H11 把真实外域 key 塞进第二个凭据头**：`X-API-Key: <B key>` / `Authorization: Bearer <B key>` / `Bearer <ROOT>` / `X-API-Key: <ROOT>` / `X-API-Key: <admin>` → **全 403** ⇒ 代理丢弃调用方凭据、只注入 A 自己的派生 key。

**直连对照（在 A 容器内绕过 `ov_proxy` 打 `openviking:1933`）—— 坐实这些 key 本身有效，403 是代理的功劳而非 key 是废的**：

| 直连探针 | 结果 |
| --- | --- |
| B 的 user key → `fs/ls viking://user/ovzh-eval/memories` | **200**（`viking://user/ovzh-eval/memories/cases` isDir，modTime `2026-09-28T13:04:36.000Z`） |
| admin key → `fs/ls` 同上 | **403** |
| ROOT key → `fs/ls` 同上 | **403**「ROOT API keys cannot access tenant-scoped data APIs in api_key mode…」 |
| B 的 key → `/api/v1/admin/accounts` | **403**「Requires role: root」 |
| ROOT key → `/api/v1/admin/accounts` | **200**（`[{"account_id":"default",…,"user_count":0},{"account_id":"agent-platform",…}]`） |
| **A 的代理令牌 → 任意上游端点** | **401 `UNAUTHENTICATED`「Invalid API Key」** |

最后一行是关键：**代理令牌不是 OV key**，它对上游毫无价值 ⇒ 即使容器被完全攻陷，攻击者拿到的 `OPENVIKING_API_KEY` 也只能经 `backend:8000/ov` 使用，而那条路径受白名单 + 派生 key 注入双重约束。

**⚠️ 但网络可达性本身没有被堵住** —— A 容器**不带任何 key** 直连：`/health` → **200**、`/api/v1/system/status` → **401**、`/api/v1/admin/accounts` → **401**。这就是 **R13**，见 §E。

#### D. P5-05 真 Key 不下发 = PASS（净测，含一次自伤假阳性的定性）

**容器 A 的 env 全量 25 项里，凭据形状只有 3 个**：

```
FASTK_API_KEY              len=100  head=gAAAAA
OPENVIKING_API_KEY         len=100  head=gAAAAA
OPENCODE_SERVER_PASSWORD   len=43   head=VjJUHw
identical(FASTK_API_KEY, OPENVIKING_API_KEY) = False   ← 两个不同的 Fernet 代理令牌
```

**9 个目录的文件系统扫描**（`/opt /data /root /etc /home /usr/local /workspace /app /var`），三把真凭据经 stdin 落 `/tmp` 后比对：

```
staged lens: root=64  B=119  secret_key=64
ROOT_OV_KEY        len=64   env_hits=0  fs_files=0  []
AGENT_SECRET_KEY   len=64   env_hits=0  fs_files=0  []
USER_B_OV_KEY      len=119  env_hits=0  fs_files=0  []
```

**首轮 `env_hits=1` 已定性为探针自伤假阳性**：脚本用 `docker exec -e KROOT=<key> …` 把待检 key 注进了**同一个 exec 的 env**，于是"扫描 env 找 key"必然命中自己。净测改为 **keys 经 stdin 落到 A 的 `/tmp`**（`/tmp` 不在扫描目录内、永不进 env/argv、用后 `os.remove`），并在脚本里加长度断言 `if [ ${#KROOT} -lt 20 ]; then echo INVALID; exit 1; fi` 防 R25 造成的空 key 被误读成"0 命中"。

> **工装教训**：任何"扫描 X 里有没有秘密"的探针，都必须先确认**注入秘密的通道本身不在 X 里**。`docker exec -e` 是最容易踩的坑（它同时污染 env 和 `/proc/<pid>/environ`）。

**两个附带辨明**：

1. `opencode.json`（容器内 `/data/config/opencode/opencode.json`）里的 `apiKey`（`len=36`，head=`21a57a`）是**宿主 LLM 供应商 `volces-gateway` 的 key**，与 OpenViking 无关：

```
host config path = /host-opencode/opencode.json
  provider volces-gateway  apiKey_len=36  head=21a57a
  baseURL=https://st8tp3ajl0df3n8b8l8qu.apigateway-cn-beijing.volceapi.com/v1
```

   依据 `opencode_config.build_container_config`（`:639`）的 `:771-788` —— **只改写 `baseURL`**（用户 provider → `{llm_proxy_base}/_user/{user_id}/{provider_id}`，宿主 → `{llm_proxy_base}/{provider_id}`），**`apiKey` 原样透传**；`text_tools.py:9-11` 亦明写「`apiKey` 原样来自宿主 `config/opencode.json`」。读宿主配置的正确入口是 `load_source_config()`（`:187`），不是 `host_config.py`。
   ⇒ **这是既有设计（LLM 凭据由平台注入 + baseURL 指向 llm-proxy），不属 Phase 5 隔离范围**，不计入"真 Key 进容器"。

2. `ovcli.conf`（`/opt/agent/builtin-plugins/openviking/ovcli.conf`）全文 **106 B**，**不含任何凭据**：

```json
{"plugin": {"opencode": {"mcpEnabled": false, "repoContext": false, "dataDir": "/data/state/openviking"}}}
```

   **`mcpEnabled:false` 的真正含义（源码澄清，此前被误读为"关闭 MCP"）**：`container_manager.py:56-61` + `:510-517` 的注释说明，它是为了**阻止插件用 `node servers/mcp-proxy.mjs` 的本地代理替换我们的远程 MCP 入口**（镜像 PATH 上没有 node，缺这个文件则 `mcpEnabled` 默认真会 spawn 一个跑不起来的本地代理）。⇒ **远程 MCP 入口 `http://backend:8000/ov/mcp` 是活跃生产通道**，本组所有 MCP 探测都打在真实通道上，不是打在一条被关掉的死路上。

#### E. P5-04 管理面经代理不可达 = PASS（但网络层未隔离 ⇒ R13）

**8 条 REST 路径全 404**（报文 `{"error":{"message":"记忆服务不提供该接口。"}}`，来自 `_rest_allowed` 的 False 分支，同时打 `logger.warning("Blocked OpenViking REST path %s; _ALLOWED_REST may be stale")`）：

```
/ov/api/v1/admin/accounts
/ov/api/v1/admin/accounts/agent-platform
/ov/api/v1/admin/../admin/accounts          ← 穿越写法 1
/ov/api/v1/debug/vector/count
/ov/api/v1/stats/memories
/ov/api/v1/sessions/x/../admin/accounts     ← 穿越写法 2（借 _SESSION_PATH 正则）
/ov/api/v1/forget
/ov/api/v1/content/write
```

**4 条非 API 路径全 404**（报文 `{"detail":"Not Found"}`，即 FastAPI 层连路由都没有）：`/ov/console`、`/ov/metrics`、`/ov/openapi.json`、`/ov/docs`。

**白名单内对照（证明 404 不是"代理整体挂了"）**：

| 探针 | 结果 |
| --- | --- |
| `/ov/api/v1/system/status` | **200** `{"status":"ok","result":{"initialized":true,"user":"p2probe"}}` |
| `/ov/health` | **200** `{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key","account_id":"agent-platform","user_id":"p2probe","role":"user"}` |
| `/ov/api/v1/sessions/p5-probe-sid` | 上游 **404** `{"status":"error","error":{"code":"NOT_FOUND","message":"Session p5-probe-sid not found"}}` ⇒ `_SESSION_PATH` 正则放行、由上游判存在性（与代理自己的 404 报文形状**不同**，可据此区分） |

**MCP 工具级拒绝**（`forget` / `cancel_watch` / `add_account` / `delete_resource`）→ **HTTP 200 + JSON-RPC `-32601`**：

```json
{"jsonrpc":"2.0","id":9,"error":{"code":-32601,"message":"平台未开放记忆工具 forget。"}}
```

即 agent 看见的是**工具失败**而非传输错误，符合 `ov_proxy.py:82-83` / `_denied_tool`(`:188-197`，只拦 `tools/call`) 的设计。

**`tools/list` 全量 dump（15 个工具的 `inputSchema`）—— R14 陈旧项坐实**：

```
add_resource   required=[]                              props=[path,temp_file_id,add_type,description,watch_interval,processing_mode,to,parent,tags,tag_mode,args]
cancel_watch   required=[to_uri]                        props=[to_uri]
edit           required=[uri,old_string,new_string]      props=[uri,old_string,new_string,replace_all,wait,timeout]
find           required=[query]                         props=[query,target_uri,limit,min_score,level,context_type,read_content]
forget         required=[uri]                           props=[uri,recursive]
glob           required=[pattern]                       props=[pattern,uri,node_limit]
grep           required=[uri,pattern]                   props=[uri,pattern,case_insensitive,node_limit]
health         required=[]                              props=[]
list           required=[uri]                           props=[uri,recursive,offset,limit,sort_by,sort_order]
list_watches   required=[]                              props=[]
read           required=[uris]                          props=[uris,offset,limit]
remember       required=[messages]                      props=[messages]
search         required=[query]                         props=[query,target_uri,session_id,limit,min_score,level,context_type,mode,
                                                               query_expansion,max_tokens,quotas,purpose,detail,detail_by_category,
                                                               dedup_turns,exclude_uris,peer_scope,other_peer_penalty,
                                                               other_peer_penalties,rewrite,rewrite_max_bullets,read_content]
tree           required=[]                              props=[uri,level_limit,node_limit,include_abstract,offset,limit]
write          required=[uri,content]                   props=[uri,content,mode,wait,timeout]
```

三个可直接落地的结论：① **没有 `add_skill`** ⇒ `_ALLOWED_TOOLS`(`ov_proxy.py:84-90`) 里的 `add_skill` 是陈旧配置，应删；② **`forget` / `cancel_watch` 在列表里却在 `tools/call` 被拒** ⇒ R14 主项复现；③ **`read` 的必填参数是 `uris`（复数，数组）** —— 首跑用 `uri` 会拿到 schema 校验错误而不是越权结果，本项差点因此漏测。另：`search` 确有 `exclude_uris` prop ⇒ 脚手架滤除（§11.12.10 E）用的是**真实上游参数**，不是我们自造的字段。

**R13 复核（网络层）**：见 §C 末尾。A 容器**无 key** 即可直连 `openviking:1933` 的 `/health`（200）。⇒ `ov_proxy.py:27-29` 宣称的 admin/console/debug「unreachable from a container **by construction**, not by a deny-list」**只在"经代理"这一条路径上成立**；`_ALLOWED_REST` 白名单**不是网络边界**。唯一真实防线是"容器内不存在有效 OV key"（§D 已净测成立），且 Group A 的 12 条越权 + 11 组伪造凭据**没有一条**能借直连绕过。**决策建议见 §7 R13（推荐①：把 openviking 移到只与 backend 相连的独立网络）。**

#### F. P5-06 角色边界矩阵（直连 `openviking:1933`，11 探针 × 3 角色）

```
PROBE                                                              ROOT  ADMIN  USER(p2probe)
GET  /api/v1/admin/accounts                                         200   403    403
GET  /api/v1/admin/accounts/agent-platform                          405   405    405
GET  /api/v1/admin/accounts/agent-platform/users                    200   200    403
GET  /api/v1/stats/memories                                         403   200    200
GET  /api/v1/debug/vector/count                                     403   200    200
GET  /api/v1/system/status                                          200   200    200
GET  /api/v1/fs/ls?uri=viking://user/ovzh-eval/memories             403   403    403
GET  /api/v1/fs/ls?uri=viking://user/p2probe/memories               403   403    200
POST /api/v1/search/search                                          403   200    200
GET  /api/v1/content/read?uri=…/p2probe/…/identity.md               403   403    200
GET  /health                                                        200   200    200
```

四条结论：

1. **ROOT 是纯管理面**：`admin/accounts` 与 `admin/accounts/{id}/users` 200，但**一切租户数据面一律 403**（`stats` / `debug` / `search` / `content` / `fs/ls`），报文「ROOT API keys cannot access tenant-scoped data APIs in api_key mode」。⇒ ROOT key 泄漏**不等于**用户记忆泄漏（但仍等于全租户管理权）。
2. **ADMIN 读不了任何用户空间**：可列账号内用户、可读 account 级计数，但连**自己 account 下**的 `p2probe` / `ovzh-eval` 都 403。可读共享作用域 `viking://agent`（200，含 `viking://agent/endpoints` isDir，modTime `2026-09-28T06:40:0…`）与 `viking://resources`（200，`[]` 空）⇒ **R16 的"account 级共享"第 2 次实证**。
3. `GET /api/v1/admin/accounts/{id}` 对**所有角色都 405** —— 该路径只接受 POST/PUT，不是权限问题。写探针时不要把它当"admin 可读单账号"的证据。
4. **R19 / P5-19 第 4 次实证**：ADMIN 与 USER(`p2probe`) 的 `stats/memories` 返回**逐字符相同**（`total_memories 651`、`by_category {profile:5, preferences:15, entities:7, events:10, cases:598, patterns:5, tools:6, …}`）⇒ 计数端点是 **account 级聚合**，**绝不能当 per-user 计量或隔离证据**。

#### G. R25 门禁与本组数据的作废重跑

本组数据**被 R25 作废 3 次**，最终有效轮次带存活门禁：

| 时刻(UTC) | 事件 | 证据 |
| --- | --- | --- |
| `05:41:06` / `05:44:11` | dockerd 重生 | 首轮 P5-01/02/03 数据作废 |
| `05:53:09` | 又一次栈级重启 | P5-04/06 首轮作废 |
| ~`06:00` | OV 不可达（**脚本执行中途恢复**） | `OvUnavailable: cannot reach OpenViking at http://openviking:1933/api/v1/admin/accounts: All connection attempts failed` ⇒ `/tmp/p5k.json` 未生成 ⇒ 三个 key 全空 ⇒ `grep -F ""` 匹配一切 ⇒ `env_hits=27 fs_files=34774`，**整段作废** |
| `06:08:55` | 栈级重启 | `ov_started=2026-09-29T06:08:55.817269253Z`；门禁 `t=0s ov_health=000 → t=5s 200`，`GATE_READY=2026-09-29T06:12:24Z` |

**故障表象是 503「记忆服务当前不可用」而非 403** ⇒ 极易误读成权限缺陷。三条已固化的工装纪律：

1. 所有跨分钟脚本**必须带存活门禁**，且每段输出打 `VALID=` 标记；
2. **key 长度合理性断言**（`if [ ${#KROOT} -lt 20 ]; then … exit 1; fi`）—— 空 key 会让 `grep -F ""` 命中一切，产出 `env_hits=27` 这种"看起来像泄露"的假数据；
3. **门禁判据只能用 OV 的 `/health`（无鉴权 200）**。踩过的坑：backend 容器**没有 curl** ⇒ `docker exec $BE curl http://127.0.0.1:8000/health` 恒空，导致门禁空转 **175 s**（35 轮 × 5 s）才因 `ov_health=200` 放行。若要探 backend，用 backend 内的 python `httpx`。

#### H. 中文乱码归因（结案：非服务端缺陷）

`benchmark/ov-zh/out-p5-b.txt`（12147 B）里的 `鏃ュ織淇濈暀绛栫暐` 定性过程：

```
iconv -f UTF-8 -t UTF-8            → FAILED
首个坏字节                          → offset 6853 (0xe5)，reason=invalid continuation byte，line 36
坏字节上下文                        → b'bstract":"- \xe6\x97\xa5\xe5\xbf\x97\xe4\xbf\x9d\xe7\x95\x99…'   ← 全是合法 UTF-8
同一段按 UTF-8 解码                 → viking://user/p2probe/memories/preferences/user/日志保留策略.md
                                       - 日志保留采用热/温/冷三级策略：热数据保留 7 天，温数据保留 90 天，冷数据归档…
同一段按 GBK 解码                   → 鏃ュ織淇濈暀绛栫暐.md
                                       - 鏃ュ織淇濈暀閲囩敤鐑/娓/鍐蜂笁绾х瓥鐣ワ細…
```

两条独立结论：

1. **乱码纯属 PowerShell 管道以 codepage 936 解码的显示层产物** —— 落盘字节是正确 UTF-8，按 GBK 解码才得到那串字。服务端存取 UTF-8 无误（与 §6「中文回读逐字符一致率 100%（40/40）」一致）。
2. `iconv` 报的 offset 6853 无效字节是**探针自身 `head -c 400` 把一个多字节字符切成两半**，同样非服务端问题。⇒ 截断中文输出要按**字符**而非**字节**（`cut -c` / python 切片），不要用 `head -c`。

#### I. 产物与复现

```
benchmark/ov-zh/out-p5-a.txt    # P5-01 越权 12 路径 + 对照组（首轮，部分被 R25 作废）
benchmark/ov-zh/out-p5-b.txt    # P5-02/03 令牌伪造 + 头部提权 + 直连对照（含 §H 的乱码样本）
benchmark/ov-zh/out-p5-c.txt    # MCP read(uris[]) 补测 + env 指纹 + opencode.json/ovcli.conf 取证
benchmark/ov-zh/out-p5-d.txt    # 净测轮（带门禁）：§D 三把真凭据 0 命中 + §E tools/list 全量 schema + §F 角色矩阵
benchmark/ov-zh/out-p5-enc.txt  # §H 编码定性（iconv / 首坏字节 / UTF-8 vs GBK 双解码）

_scratch_p5a.sh  _scratch_p5b.sh  _scratch_p5c.sh  _scratch_p5d.sh  _scratch_p5enc.sh  _scratch_p5chk.sh
```

---

## 11.16 Phase 5 Group B 完整数据（P5-09/10/11/12/13/14/16/17，2026-09-29）

**结论：8 项全部 PASS，暴露 2 个平台侧真实缺陷（R27 中危 / R28 低危）。** 产物：`benchmark/ov-zh/out-p5-hi.txt`（P5-10，74 行）、`out-p5-hi4.txt`（P5-11，73 行）、`out-p5-restore.txt`（残留还原，13 行）。目标容器 `agent-p2probe`（新镜像 `agent-demo:1.4.0`，含插件）。

### A. P5-09 版本三方握手

| 面 | 实测值 | 取证方式 |
| --- | --- | --- |
| opencode | **1.18.25** | 容器内 `opencode --version` |
| 插件 | **`2026.9.25-2`**（`openvikingSourceCommit=3e02c6ac29842be214fa1560dfaf6ea39a69322c`，`dependencies={}`） | `package.json` |
| 服务端镜像 | `sha256:569193efd49ad15a818c98ca66bfb566d1726713f1f3ec9c488b97fa66757d05` | `docker inspect` |
| 服务端 **REST** 面 | **`v0.4.21`** | `GET /ov/health` → `{"status":"ok","healthy":true,"version":"v0.4.21","auth_mode":"api_key","account_id":"agent-platform","user_id":"p2probe","role":"user"}` |
| 服务端 **MCP** 面 | **`1.27.0`** | `initialize` → `serverInfo={"name":"openviking","version":"1.27.0"}` |

⇒ **判据必须指明"哪一面"**。REST 与 MCP 的 version 字段**不同源**（`v0.4.21` vs `1.27.0`），任何"三方版本一致"的断言写法都会假失败。

### B. P5-10 既有能力共存（9 段证据，`PROBE_DONE` / rc=0）

**S1 容器内实渲染的 `/data/config/opencode/opencode.json`**（不是宿主文件，是 backend 渲染后写进容器的那份）：

- `mcp_keys = ["demo-mcp","openviking","web_search"]`，三者 **`enabled=true`**
  - `demo-mcp`：remote，`http://demo-mcp:8080/mcp`，无 headers
  - `openviking`：remote，`http://backend:8000/ov/mcp`，`headers={"X-API-Key":"<len=100 head=gAAAAA>"}`（**per-user 令牌已盖章**）
  - `web_search`：local，`command=["python3","/opt/agent/builtin-mcp/web_search/web_search_mcp.py"]`，`environment={"SEARXNG_URL":"http://searxng:8080"}`
- `plugin` **3 条**（oh-my-opencode-slim / openviking / present-file）
- `permission` **14 键**，全 `allow`：`bash, edit, external_directory, glob, grep, list, openviking_*, present_file, read, skill, task, todowrite, web_search*, webfetch`
  - ⚠️ 平台生成的 MCP 规则**只有 `openviking_*`**（`mcp_perm_rules={"openviking_*":"allow"}`）；`web_search*`（**无下划线**）是宿主 `opencode.json` **原样透传**来的 ⇒ R28 的键名分裂源头
- `top_keys = ["$schema","autoupdate","enabled_providers","mcp","model","permission","plugin","provider","share","small_model"]`；`enabled_providers=["volces-gateway"]`；`model=small_model="volces-gateway/deepseek-v4-pro"`；`agents_keys=[]`、`agent_keys=[]`

**S2 文件系统**：

| 插件 | version | main | node_modules | 备注 |
| --- | --- | --- | --- | --- |
| `oh-my-opencode-slim` | **2.2.15** | `dist/index.js` | ✅ | 5 份 README（含 `README.zh-CN.md`）+ `oh-my-opencode-slim.schema.json` |
| `@openviking/opencode-plugin` | **2026.9.25-2** | `index.mjs` | ❌（零依赖） | `INSTALL-ZH.md, INSTALL.md, README.md, index.mjs, lib, package.json, scripts, servers` —— **无 `tests/`**（印证 P5-12） |
| `opencode-present-file` | 1.0.0 | `index.js` | ✅ | |

`web_search`：`interpreter_resolved=/usr/bin/python3`、`script_isfile=true`、`script_size=8808`。
`/opt/agent/builtin-mcp` 列举 = `["fastk","openviking","web_search"]` ⇒ **`fastk` 无 `manifest.json`，`_discover_builtin_mcp()` 发现不了它**，宿主 `builtin_mcp.fastk.enabled=true` 是**死控制**（不影响本项判定，但记录在案）。

**S3 插件配置 `oh-my-opencode-slim.json`**：keys=`["agents","autoUpdate","preset","presets"]`、`preset="container"`、`autoUpdate=false`、`disabled_skills=null`。`presets.container`：

| preset | mcps | skills |
| --- | --- | --- |
| designer / explorer / fixer / oracle | `[]` | `[]` |
| **librarian** | **`["web_search"]`** ← 白名单形（R27 受害者） | `[]` |
| **orchestrator** | **`["*"]`** ← allow-all 形（可逆） | `["*"]` |

`agents` **9 个**（council, councillor, designer, explorer, fixer, librarian, observer, oracle, orchestrator），全 pin `volces-gateway/deepseek-v4-pro`。

**S4 web_search MCP stdio 真握手**：`serverInfo={"name":"web-search-mcp","version":"1.0.0"}`、`protocolVersion="2024-11-05"`、`tools/list=["search"]`、`stderr=""`。

**S5 openviking MCP 真握手**：`status=200`、`session_id_present=false`、`serverInfo={"name":"openviking","version":"1.27.0"}`、**15 工具**（`find, search, read, list, tree, remember, write, edit, add_resource, list_watches, cancel_watch, grep, glob, forget, health`）。

**S6 冲突判定（本项核心）**：

```
raw_name_overlap      = ["search"]      ← 两个 MCP 都注册裸名 search
prefixed_overlap      = []              ← 加前缀后零冲突 ✅
prefixed_ws           = ["web_search_search"]
prefixed_ov_count     = 15
permission_rule_covers_ws = false       ← 平台生成的规则不覆盖 ws（ws 靠宿主 web_search* 键）
permission_rule_covers_ov = true
```

⇒ **原设计的"无工具名冲突"判据须按前缀口径重写**：裸名确实重叠，但 opencode 以 server name 为前缀注册（`web_search_mcp.py` 的 docstring 自己也这么写），agent 实际看到的是 `web_search_search` / `openviking_search`。

**S7 node 动态 import 形状**（rc=0，stderr=""）：

| 插件 | exports | functions | arity |
| --- | --- | --- | --- |
| present-file | `["default"]` | `[]` | `[]` |
| oh-my-opencode-slim | `["OhMyOpenCodeLite","default"]` | `["OhMyOpenCodeLite"]` | `[1]` |
| openviking | `["OpenVikingPlugin","default"]` | `["OpenVikingPlugin"]` | `[0]` |

**S8 opencode server API（取证陷阱）**：`/api/health 200 len=16`、`/api/agent 200 len=91`、`/api/provider 200 len=91`、`/api/session 200 len=50` 是真 API；而 `/api/config`、`/api/app`、`/api/mcp`、`/api/tool`、`/api/permission` **全部 `200 len=2884` = SPA HTML 兜底**（正文以 `<!doctype html>` 开头）⇒ **不能用它查 MCP/permission 运行态**，只能读容器内文件。

**S9**：`searxng_url="http://searxng:8080"`、`searxng_healthz=200`。

### C. P5-11 用户开关

#### C.1 Layer 1 —— 渲染层纯函数矩阵（`L1_DONE`）

`L0` 基线：`source_origin="/host-opencode/opencode.json"`、`source_top_keys=["$schema","autoupdate","builtin_mcp","mcp","permission","provider","share"]`、**`source_builtin_mcp={"fastk":{"enabled":true},"web_search":{"enabled":true}}`**（宿主**没有** `openviking` 键 ⇒ OV 的可见性完全由 `settings.openviking_enabled` + 用户 hidden 集决定）、`source_builtin_skills=null`、`settings.openviking_enabled=true`、`hidden_mcp_servers_baseline=[]`、`hidden_builtin_skills_baseline=[]`。

| 用例 | hidden_mcps | mcp_perm_rules | orchestrator_mcps | librarian_mcps | roundtrip |
| --- | --- | --- | --- | --- | --- |
| C0_baseline | `[]` | `{openviking_*:allow}` | `["*"]` | `["web_search"]` | `[]` |
| C1_host_ov_disabled | `["openviking"]` | `{openviking_*:deny}` | `["*","!openviking"]` | `["web_search"]` | `["openviking"]` |
| C2_host_ov_enabled | `[]` | `{openviking_*:allow}` | `["*"]` | `["web_search"]` | `[]` |
| **C3_host_websearch_disabled** | `["web_search"]` | `{openviking_*:allow, web_search_*:deny}` | `["*","!web_search"]` | **`[]`** | `["web_search"]` |
| C4_user_hides_ov | `["openviking"]` | `{openviking_*:deny}` | `["*","!openviking"]` | `["web_search"]` | `["openviking"]` |
| **C5_platform_visible_user_hides** | `["openviking"]` | `{openviking_*:deny}` | `["*","!openviking"]` | `["web_search"]` | `["openviking"]` |
| **C6_platform_hidden_user_empty** | `["openviking"]` | `{openviking_*:deny}` | `["*","!openviking"]` | `["web_search"]` | `["openviking"]` |
| C7_both_hidden | 两个 | 两个都 deny | `["*","!openviking","!web_search"]` | `[]` | 两个 |

**所有用例的不变量**：`mcp_enabled={demo-mcp:true,openviking:true,web_search:true}`、`mcp_keys` 三项、`mcp_types={demo-mcp:remote,openviking:remote,web_search:local}`、`ov_entry_present=true`、**`ov_header_stamped=true`（即使被隐藏，per-user 令牌仍照常盖章）**、`plugin_list_len=3`、`skill_perm="allow"`、`disabled_skills=null`、`orchestrator_skills=["*"]`。

- **C3 = 「其余不受影响」的直接证据**：关掉 `web_search` 时 `openviking_*` 仍是 `allow`、`ov_entry_present` 仍 true。
- **C5 vs C6 = 「用户只能收窄」的直接证据**：平台可见 + 用户隐藏 与 平台隐藏 + 用户空集，产出**完全相同**（并集语义，用户无法反向放开）。
- **C8/C9 契约补充**：host **remote** MCP `enabled:false` → `hidden=["p5-remote-probe"]`；改回 true → `[]`。host **local** MCP `enabled:false` → **`hidden=[]`**（local MCP 根本不被注入容器配置，无可隐藏）。
- **`_apply_list_visibility` 幂等性全验**：`(["*","!openviking"], ∅) → ["*"]`、`(["*"], {"openviking"}) → ["*","!openviking"]`、重复隐藏同结果、`(["web_search"], {"web_search"}) → []`、`(["web_search"], {"openviking"}) → ["web_search"]`。
- **`_sanitize_mcp_permission_key` 实测**：`{"a b"→"a_b", "a-b"→"a-b", "a.b"→"a_b", "openviking"→"openviking", "web_search"→"web_search", "中文"→"__"}`（末项是 R28 的碰撞隐患）。

#### C.2 Layer 2 —— 活体推送到运行中容器（`L2_DONE` / `PROBE_DONE` / rc=0）

**门禁 stub 的边界（净测纪律）**：`p2probe` 在 DB 里**没有容器记录** ⇒ 真实 `agent_controller.get_agent_gate("p2probe")`（`:496-520`，`if not record: return False, None`）返回：

```
L2.gate_unpatched = {"password_len": 0, "running": false}     ← 未打补丁的真实门禁
L2.gate_patched   = {"stdin_password_len": 43, "target": "p2probe"}
```

**只 stub 了"查库拿 running/password"这一步**；`_sync_plugin_config`（真 `read_config_file` / `write_config_file`，走 Docker `get_archive` / `put_archive`）与 `PATCH /global/config`（真 `tunnel_relay`）**全走真实路径**。口令 `OPENCODE_SERVER_PASSWORD`（len=43）经 **stdin** 注入（`printf '%s' "$PW" | docker exec -i …`），**永不进 argv / env**。

`patch_shape`（`_visibility_patch` 的实际产出）：

```
hide       = {"permission": {"openviking_*": "deny"}}
show       = {"permission": {"openviking_*": "allow"}}
skill_hide = {"permission": {"skill": {"*": "allow", "pptx-generator": "deny"}}}
```

**基线（双视角）**：

```
L2.before                = {librarian_mcps:["web_search"], orchestrator_mcps:["*"],
                            mcp_perm_rules:{openviking_*:allow}, roundtrip_hidden:[],
                            ov_mcp_enabled:true, ws_mcp_enabled:true, disabled_skills:null}
L2.before_container_side = {perm_key_count:14, perm_mcp_rules:{openviking_*:allow},
                            librarian_mcps:["web_search"], orchestrator_mcps:["*"],
                            mcp_enabled:{demo-mcp:true,openviking:true,web_search:true},
                            plugin_list_len:3, disabled_skills:null}
```

**六步全部 `push_ok=true`，容器内外双视角每一步逐字段一致**：

| 步 | target | effective_hidden | perm_mcp_rules | orchestrator_mcps | **librarian_mcps** | **perm_key_count** | roundtrip_hidden |
| --- | --- | --- | --- | --- | --- | --- | --- |
| hide_ov | openviking | `["openviking"]` | `{openviking_*:deny}` | `["*","!openviking"]` | `["web_search"]` | 14 | `["openviking"]` |
| unhide_ov | openviking | `[]` | `{openviking_*:allow}` | `["*"]` | `["web_search"]` | 14 | `[]` |
| hide_ov_again | openviking | `["openviking"]` | `{openviking_*:deny}` | `["*","!openviking"]` | `["web_search"]` | 14 | `["openviking"]` |
| **hide_ws** | web_search | `["web_search"]` | `{openviking_*:deny, web_search_*:deny}` | `["*","!web_search"]` | **`[]`** | **15** | `["openviking","web_search"]` |
| **restore_ws** | web_search | `[]` | `{openviking_*:deny, web_search_*:allow}` | `["*"]` | **`[]` ← 未复原** | 15 | `["openviking"]` |
| **final_restore** | openviking | `[]` | `{openviking_*:allow, web_search_*:allow}` | `["*"]` | **`[]` ← 未复原** | 15 | `[]` |

```
L2.restored_to_baseline = false                       ← R27 就是这么抓到的
L2.residue_perm_keys    = 15 键（比基线多一个 web_search_*）
```

**六条硬结论**：

1. **OV 的 hide/unhide 完全可逆**：`openviking_*` allow→deny→allow；`orchestrator.mcps` `["*"]`→`["*","!openviking"]`→`["*"]`；**`librarian.mcps` 全程 `["web_search"]` 不受 OV 开关影响** ⇒ 用户开关 openviking **不干扰其他能力**（P5-11 主判据 PASS）。
2. **`mcp_enabled` 六步全程不变**（三项全 true）⇒ 运行态开关**只作用在 permission 层**，MCP 条目始终保留、始终连接（与 Layer 1 不变量、与 P2「stays connected but denied」互证）。
3. **`plugin_list_len=3` 全程不变** ⇒ 开关从不摘插件。
4. **`roundtrip_hidden` 每一步都精确等于 effective hidden 集** ⇒ `hidden_mcps_from_config` 是**忠实逆函数**，opencode 重启后状态可正确复原（不会"重启就丢失隐藏"）。
5. **容器内 `cat` 读回 ≡ backend 的 Docker-archive 读回（逐字段）** ⇒ 写入真的落在 `/data/config/opencode/`（volume），**独立再证 P5-14 的 `/data` WRITE_OK**，且 `put_archive` 能穿透只读 rootfs（`container_manager.write_config_file` docstring 的说法实测成立）。
6. `disabled_skills=null` 全程不变（本次未测 skill 开关路径）。

### D. R28 —— permission 键残留 + 键名分裂（低危）

```
基线 perm_keys（14）= [bash, edit, external_directory, glob, grep, list, openviking_*,
                       present_file, read, skill, task, todowrite, web_search*, webfetch]
残留 perm_keys（15）= 上述 + web_search_*        ← 多出的键，取消隐藏后不删
```

- **`web_search*`（无下划线）** 来自宿主 `opencode.json` 原样透传；**`web_search_*`（有下划线）** 由 `_sanitize_mcp_permission_key` 生成 ⇒ **两条键指向同一个 MCP 却不同名并存**。
- `web_search*: allow` 对 `web_search_*: deny` **无覆盖作用**（不同键）。当前二者值恰好都允许（残留态是 `allow`），所以**没有可见故障**；但一旦将来出现一 allow 一 deny，opencode 的优先级**未定义且未测**。
- 根因与 R27 同源：`_sync_plugin_config` 是 **read-modify-write**（因为 `PATCH /global/config` 走 mergeDeep，删不掉键）。

### E. R27 根因（源码级）与残留还原

**根因** —— `backend/app/services/opencode_config.py:541-569` `_apply_list_visibility`：

```python
:563  elif not item.startswith("!") and item not in hidden:
          result.append(item)          # ← 白名单项被隐藏 = 从文件里删除（不留痕迹）
:565  if "*" in result:
:566      for name in sorted(hidden):
:568          result.append(f"!{name}")  # ← 只有 allow-all 形才用 !name 标记 ⇒ 可逆
```

⇒ **allow-all 形（`["*"]`）可逆，白名单形（`["web_search"]`）不可逆**。`orchestrator.mcps=["*"]` 走前者 ⇒ OV 开关完全对称；`librarian.mcps=["web_search"]` 走后者 ⇒ 隐藏即删除，取消隐藏时无从恢复。

**自愈路径已证** —— `render_plugin_config`(:605-636) 的 `:628` `entry["mcps"] = ["web_search"]` 是**从零重建**，不读旧值 ⇒ **容器 start / recreate 会重渲染并完全自愈**，只有"容器运行中热开关"这一条路径受损。

**残留还原实测**（`out-p5-restore.txt`，`RESTORE_OK=true` / rc=0）：

```
R.before                 = {perm_count:15, librarian_mcps:[], perm_mcp:{openviking_*:allow, web_search_*:allow}, …}
R.perm_keys_removed      = ["web_search_*"]        ← 整份 permission 段替换（绕开 mergeDeep 删不掉键）
R.opencode_written       = true
R.librarian_before       = {"mcps": [], "model": "volces-gateway/deepseek-v4-pro", "skills": []}
R.plugin_written         = true
R.rendered_keys          = ["agents","autoUpdate","preset","presets"]
R.rendered_librarian     = {"mcps": ["web_search"], …}
R.rendered_agents_count  = 9
R.rendered_equals_restored = true                 ← 平台自己的 renderer 产出 ≡ 还原后的文件
R.after_container_side   = {perm_count:14, perm_keys:[…基线 14 键…], perm_mcp:{openviking_*:allow},
                            librarian_mcps:["web_search"], orchestrator_mcps:["*"], agents_count:9,
                            ov_header_len:100, plugin_list_len:3, roundtrip_hidden:[],
                            mcp_enabled:{demo-mcp:true,openviking:true,web_search:true},
                            disabled_skills:null}
```

⇒ 容器已回到**精确基线**（14 键、键集逐字符相等）。`R.rendered_equals_restored=true` 同时**反向验证了自愈路径**：还原后的文件与 `render_plugin_config` 的产出结构完全相等 ⇒ recreate 后就是这个状态。

### F. P5-14 只读 rootfs —— 写入路径归属（1/5 落 volume）

**容器 HostConfig**：`ReadonlyRootfs=true`，`Tmpfs={"/home/agent":"size=512m,uid=1000,gid=1000","/tmp":"size=256m"}`。

```
WRITE_DENY = /opt/agent, /opt/agent/builtin-plugins/openviking, /usr/local/bin, /library/pptx   ✅ 主判据 PASS
WRITE_OK   = /data, /data/state, /data/state/openviking, /workspace,
             /home/agent, /home/agent/.openviking, /tmp
```

**插件的 5 条写入路径**（源码定位 + 实测）：

| # | 路径 | 落点 | 性质 | 重启后 |
| --- | --- | --- | --- | --- |
| 1 | `initLogger(resolveDataDir())` | `/data/state/openviking/openviking-memory.log` | **volume** | **保留** ✅（实测写入 115 B） |
| 2 | `workspace-identity.mjs:33-39` `stateDir()` | `/home/agent/.openviking/state` | **tmpfs** | **丢失** ❌ |
| 3 | `pending-queue.mjs:57-59` `getPendingDir()`（未导出） | `/home/agent/.openviking/pending` | **tmpfs** | **丢失** ❌ —— 这是**离线补投队列**，OV 不可达期间攒的写入随重启蒸发 |
| 4 | `recall-core.mjs:415-418` `stateFile()` | `OPENVIKING_STATE_DIR` 否则 `~/.openviking/state` | **tmpfs**（env 未设） | **丢失** ❌ |
| 5 | `plugin-config.mjs:265-266` `debugLogPath` | tmpfs | 默认**惰性**（`debug-log.mjs:40 if (!c.debug) return noop`） | 无影响（默认不写） |

**容器 env 全量 27 项**，上述 4 个重定位 env **一个都没设**；`XDG_STATE_HOME=/data/state` 虽已设但**插件不读 XDG**（只读自己的 `OPENVIKING_*`）⇒ **`Dockerfile:310-313` 关于 dataDir 的注释有误**（它只对第 1 条成立）。

**重定位端到端验证有效**：注入 `OPENVIKING_STATE_DIR=/data/state/openviking/state` 后，`/data/state/openviking/state/ws-identity-c52ddf65534b.json` **真实落盘**。完整搬迁需 **3 个 env**：`OPENVIKING_STATE_DIR` + `OPENVIKING_PENDING_DIR` + `OPENVIKING_DEBUG_LOG`。

**判据改写**：原写法「`writePathAsync=true` 的落盘路径落在 `/data` 或 `/workspace`」**不可用** —— `writePathAsync` 只在 `config-schema.mjs:155` 被**声明**，全包**无任何读取点**（死旋钮，与 R15 的 `recallExcludeUris` 同类）。

### G. P5-16 配置越界钳制（33 用例全钳制、零抛错）

baseline（`ovcli.conf` + 平台默认解析结果）：`{"recallLimit":10,"scoreThreshold":0.35,"minQueryLength":3,"recallTokenBudget":2000,"recallMaxContentChars":500,"timeoutMs":30000,"captureMaxLength":24000,"authMode":"api_key","captureMode":"semantic","logLevel":"error","recallPeerScope":"all","enabled":true,"recallQueryFilters":[],"recallMaxTokens":1600,"recallCompressMaxBullets":6}`

| knob | 输入 → 输出 |
| --- | --- |
| `recallLimit` | `999→50`、`-5→1`、`0→1`、`"abc"→10`、`7.6→8`、`1e9→50` |
| `scoreThreshold` | `7→1`、`-1→0`、`nan→0.35` |
| `minQueryLength` | `0→1`、`9999→64` |
| `recallTokenBudget` | `1→200`、`99999999→50000` |
| `recallMaxContentChars` | `10→100`、`999999→5000` |
| `timeoutMs` | `10→1000`、`999999999→300000` |
| `captureMaxLength` | `1→200`、`99999999→100000` |
| `recallMaxTokens` | `1→64` |
| `recallCompressMaxBullets` | `0→1` |
| `enabled` | `"off"→false`、`"maybe"→true` |
| `recallQueryFilters` | `"a, b ,c"→["a","b","c"]`（split + trim） |
| `captureMode` | `bogus→semantic`（回落默认） |
| `logLevel` | `verbose→error`（回落默认） |
| `recallPeerScope` | `root→all`（回落默认） |
| **`authMode`** | **`bogus→""`、`API_KEY→""`** ⚠️ **唯一例外** |

- **层序正确**：env > workspace-file。
- **端到端 `loadConfig` 一致**：`TIMEOUT_MS=1` → `{"timeoutMs":1000,"captureTimeoutMs":30000,"recallCompressTimeoutMs":110000}`（派生 timeout 按 `plugin-config.mjs:262-266` 各自独立钳制）。
- ⚠️ **`authMode` 是 P5-16 里唯一的真实隐患**：它不是钳制到 enum 边界，而是**静默降级为空串**。平台在 `container_manager.py:519-522` 把 `OPENVIKING_AUTH_MODE` 钉死为 `api_key`；若该 env 被写成 `API_KEY`（大小写不符）或任何非法值，插件会得到 `authMode:""` ⇒ 退回 `trusted` 语义路径，而 Group A 已证服务端在 `api_key` 模式下**完全无视** identity 头 ⇒ 表现为"记忆功能静默失效"。且 enum **大小写敏感**。

### H. P5-17 多模态范围（写侧 out-of-scope 已定案）

| 面 | 取证 | 结果 |
| --- | --- | --- |
| REST | 10 条候选端点 `/ov/api/v1/{resources, resources/upload, upload, files, images, audio, embed, embeddings, models, multimodal}` | **全 404** |
| MCP schema | 15 个工具的 `inputSchema` 扫多模态关键字（image/audio/file/binary/base64/blob/mime/…） | **仅 1 处命中** —— `add_resource.temp_file_id` |
| MCP 读侧 | `read` 工具描述原文 | 含「**Raster images and supported audio return native MCP content blocks.**」⇒ **服务端读侧有多模态能力** |
| MCP 写侧 | `write` / `edit` / `remember` 的载荷类型 | 全是 `string` / `array<object>` ⇒ **平台自动写入面纯文本** |
| 资源树 | `list viking://user/p2probe/resources` | `[dir] p2` |

⇒ **定案**：平台经 `ov_proxy` 的自动记忆通道**不涉及任何多模态写入**，记为 **out-of-scope**；读侧若将来要支持图片记忆，入口是 `add_resource` + `read`，而 `add_resource` **已在 `_ALLOWED_TOOLS` 白名单内**（`forget` / `cancel_watch` 才是不放开的两个）。

### I. 工装纪律、R25 复现与产物

**本轮新增/复用的工装纪律**：

1. **backend 容器跑脚本文件必须 `-w /app -e PYTHONPATH=/app`** —— `python3 /tmp/x.py` 的 `sys.path[0]` 是 `/tmp` 而非 cwd，否则 `ModuleNotFoundError: No module named 'app'`（本轮真实踩到，`out-p5-hi.txt` 尾部就是它）。backend 的 `python3` = `/usr/local/bin/python3`（3.12.14）。
2. **凭据只走 stdin**：`printf '%s' "$PW" | docker exec -i …`，永不进 argv / env（延续 §11.15 D 的净测纪律）。
3. **可见性用例必须跑完整 hide→unhide→与基线逐字段 diff**。只测 hide 一侧会**完全漏掉 R27**；本轮是靠 `restored_to_baseline=false` + `perm_key_count 14→15` 两个 diff 抓到的。
4. **不能只信 backend 的读回**：`read_config_file` 走 Docker archive，与"容器进程实际看到的文件"理论上是同一份，但**必须从容器内 `cat` 独立复核**（本轮双视角逐字段一致，才算坐实落盘位置）。
5. **A 容器内 `node` 不在 PATH**：`/opt/agent/skill-envs/pptx/bin/node`；`docker cp` 到 A 容器不可用（rootfs read-only + `/tmp` 是 tmpfs）⇒ 一律 `tr -d '\r' < f | docker exec -i … sh -c "cat > /tmp/f"`。

**R25 第 15 次复现**（本组两次跑各撞一次）：

| 时刻 | 事件 | 证据 |
| --- | --- | --- |
| `07:26:38` | 栈级重启（`out-p5-hi.txt`） | A/OV/BE 同时 `started`；门禁第 1 轮 `local_ov=000 via_proxy=503`，第 2 轮 `200/200`；`kernel_uptime_sec=105712.28` |
| `07:42:07` | 又一次（`out-p5-hi3.txt`，因解包 bug 作废） | 同上，`kernel_uptime_sec=106643.45` |
| `07:45:01` | 栈级重启（`out-p5-hi4.txt`） | 门禁 2 轮放行；`kernel_uptime_sec=106817.77`；收尾 `DONE_P5HI at=2026-09-29T07:45:33Z` |

四个容器的 `created` **全部未变**（`agent-p2probe=2026-09-28T08:07:46Z`、`openviking=2026-09-28T17:22:47Z`、`backend=2026-09-29T02:35:13Z`、`frontend=2026-09-20T09:24:47Z`），A/OV/BE 的 `RestartCount` 全 **0**（frontend 为 1，历史遗留），`kernel_uptime_sec≈106000`（VM 未重启）⇒ **再次坐实"整栈被停止后重新启动，容器对象未重建、WSL VM 未重启"**。

**产物与复现**：

```
benchmark/ov-zh/out-p5-hi.txt      # P5-10 共存 9 段证据（S1–S9）；尾部 P5-11 因缺 PYTHONPATH 失败（已修）
benchmark/ov-zh/out-p5-hi3.txt     # P5-11 首跑（SKIP10=1）：L1 全绿，L2 因 dict 解包 bug 失败（已修）
benchmark/ov-zh/out-p5-hi4.txt     # P5-11 二跑：L1_DONE + L2_DONE + PROBE_DONE，rc=0（本文 §C 的数据源）
benchmark/ov-zh/out-p5-restore.txt # 残留还原：RESTORE_OK=true，rc=0（本文 §E 的数据源）

_scratch_p5hi.sh   # 驱动：门禁 + R25 栈状态 + push 探针 + 容器侧后置快照（SKIP10 开关）
_scratch_p5i.py    # 探针本体（在 backend 容器内跑，读 stdin 拿口令，monkeypatch get_agent_gate）
_scratch_p5r.sh    # 还原脚本（删 web_search_* 键 + 补回 librarian.mcps，并与 render_plugin_config 对比）
```

复现命令模式（唯一可靠）：

```bash
bash -lc "cd /mnt/d/Project/agent-docker-demo && tr -d '\r' < _scratch_p5hi.sh > /tmp/p5hi.sh \
          && SKIP10=1 bash /tmp/p5hi.sh > benchmark/ov-zh/out-p5-hi4.txt 2>&1"
```

---

## 11.17 Phase 5 Group C 完整数据（P5-18/19/20/21，2026-09-29）

**结论**：**3 PASS + 1 FAIL**——P5-18（派生 key 一致性）/ P5-19（计数端点非隔离证据）/ P5-20（删除通道）PASS，**P5-21（写入路径作用域）FAIL ⇒ 立 R29（高危：跨用户注入通道）**。产物：`benchmark/ov-zh/out-p5gc.txt`（241 行，8 段）+ `benchmark/ov-zh/out-p5gd.txt`（249 行，8 段全 VALID，`DONE_P5GD at=2026-09-29T08:43:53Z`）。两轮跑各撞上一次 R25 栈级重启（第 16/17 次复现，`08:30` / `08:42`），门禁设计生效，无一组数据被污染。

### A. P5-18 派生 Key 一致性（R11）= PASS

注册/回读矩阵（直连 `openviking:1933`，admin key 注册 + user key 回读）：

| 探针 | 结果 |
| --- | --- |
| `p5gc-a` / `p5gc-b` / `p5gd-a` / `p5gd-b` 注册 | 200×4，`returned_equals_derived=true`（len 115） |
| 重复注册（409） | 幂等拒绝，**不回读 key**（`_register_user:237` 注释：409 不走回读路径） |
| realign（key_override 已存在） | 200，回读全等 |
| health 身份核对 | user key → `/api/v1/auth/health` 返回自身 uid |

**R11 失败回落机制直测**（monkeypatch `_admin_post` 强制抛错）：`fallback_error_logged=true`、`fallback_override_set=true`（回落到"存服务端返回值"路径），真实用户零污染（`overrides_for_real_users=[]`）⇒ §4.3.2 的"不硬编码信任"设计按预期工作。

**Proxy Token 架构确认**（首轮 stage 1b "MISMATCH" 的最终解释）：

| 项 | 值 |
| --- | --- |
| 容器 env `OPENVIKING_API_KEY` | len 100，head `gAAAAA`（Fernet 密文） |
| 解码结果 | `ovproxy:p2probe`（`issue_proxy_token = encrypt_secret("ovproxy:" + user_id)`，`ov_access.py:76-79`） |
| 本地重算派生 key | len 117（真实 OV user key，只在 backend 内存/DB） |
| 正向断言 | `container_token_is_ovproxy_p2probe=true`；`P5_18_LIVE_KEY=MISMATCH live_len=100 derived_len=117` **是设计使然** |

⇒ 这是 P5-05「真 Key 0」的机制解释：容器那把 key 直连 OV 一切数据面 401。派生 key 由 `config.py:107` 注释明示"never at openviking_url: agent containers must not be able"。

### B. P5-19 计数端点非隔离证据（R19）= PASS（判据坐实）

| 探针 | 结果 |
| --- | --- |
| 三 user key（`p5gc-a` 新用户 0 记忆 / `p5gc-b` / `p2probe`）`stats/memories` | **逐字符相同**：`total_memories 651, cases 598, profile 5, preferences 15, entities 7, events 10, patterns 5, tools 6, skills 5, warm 651` |
| `debug/vector/count`（user key） | 200，`count=837` |
| `debug/vector/count`（ROOT key） | **403**（见下） |
| b 跨读 `viking://user/ovzh-eval/memories/cases/慢查询案例.md` | **403 PERMISSION_DENIED** |

⇒ 三用户 stats 逐字符相同 = **account 级聚合实锤**（R19 第 5 次实证）；隔离证据必须用内容面（跨读 403）。第一轮 `ovzh-eval_sample_uri=null` 是探针缺陷（根级 recursive `fs/ls` 深度截断，R21），跟进轮用 `fs/tree level_limit=3` 定位真实文件补测成功。

**ROOT key 契约发现**：ROOT key 对租户数据面 API 全部 403（报文「ROOT API keys cannot access tenant-scoped data APIs in api_key mode」）⇒ **跨用户取证必须用 user/admin key**（与 P5-06 角色矩阵一致：ROOT 是纯管理面）。

### C. P5-20 删除通道（R20）= PASS（判据修正 + 产品决策定案）

**判据修正**：原判「REST 面没有任何删除端点」**被实测推翻**——`openapi.json`（ROOT key 可读，126 paths）实有 **16 条 DELETE 路径**，含 `/api/v1/fs`（summary "Rm"，query：`uri` 必填 / `recursive` / `wait` / `timeout`）、`/api/v1/sessions/{session_id}`（path 参数）、`/api/v1/skills/{skill_name}`、admin 面 `DELETE …/users/{uid}`、`/webdav/resources`、`/api/v1/watches` 等；webdav 面 `[DELETE,GET,HEAD,MKCOL,MOVE,OPTIONS,PATCH,POST,PROPFIND,PUT]` 全方法集。

**但 agent 可达面无删除仍成立（三重防线）**：
1. `_ALLOWED_REST` 8 条无 DELETE：`content/write,read`、`fs/ls`、`skills` → **405**；`fs/rm`、`content/delete`、`memories`、`sessions/{x}` → **404**。
2. `_ALLOWED_TOOLS` 无 `forget`/`cancel_watch`：proxy `tools/call forget` → `-32601`「平台未开放记忆工具 forget。」
3. 容器内无真实 OV key（P5-05 净测；env 持有 proxy token，直连 401）。

**服务端隔离实测全绿**（直连，user key）：

| 探针 | 结果 |
| --- | --- |
| `forget` 跨用户 / 跨用户+`recursive:true` | `isError:true` "Access denied"；越权拒绝后目标文件仍在 |
| `forget` own | `"Deleted: …"`，随后 read 404 |
| `DELETE /api/v1/fs` own（query `uri`） | 200 `{"estimated_deleted_count":1}`，read 404 复核 |
| `DELETE /api/v1/fs` 跨用户 | **403 PERMISSION_DENIED** |

**产品决策（定案）：不放开 `forget`**。理由：① proxy `_dispatch` 对 MCP 工具参数**无 URI 前缀校验**（源码确认），放开即无防线；② 即便服务端隔离已实测，放开前仍须加 `viking://user/{caller_uid}/` 前缀守卫 + 修 R26 的 isError 归一化；③ 当前删除走管理员通道已够用（admin 面 `DELETE …/users/{uid}` → 202）。**最小放开路径** = `_ALLOWED_TOOLS` 加 `forget` + URI 守卫 + 拒绝形状归一化（与 R14 的 tools/list 过滤可合并同一补丁）。注：`forget` 的拒绝形状是 `isError:true`（不在 R26 影响面内）。

### D. P5-21 写入路径作用域（R16）= FAIL ⇒ R29（高危）

**通过的一半**：平台自动写入全落 user scope——p2probe 全量 `fs/tree` 复核，无一落 `viking://agent` / `viking://resources`。

**失败的一半（七步证据链，p5gd stage B/C/D/D2）**：

| 步 | 探针 | 结果 |
| --- | --- | --- |
| 1 | user key a 写 `viking://resources/p5gd-res-marker.md`（102 B「杭州湾跨海大桥全长三十六公里…」） | **200**，`mode:create, context_type:resource, semantic_status:complete`（进语义索引并生成中文摘要） |
| 2 | user key a 写 `viking://agent/p5gd-agent-marker.md`（86 B「舟山群岛拥有大小岛屿一千三百九十个…」） | **200** |
| 3 | 对照：a 写 `viking://user/p5gd-b/memories/p5gd-inject.md` | **403**（user scope 写隔离正常） |
| 4 | **agent 真实通道**：A 容器（p2probe）经 proxy `/ov/mcp` `tools/call write` 写 `viking://resources/p5gd-proxy-marker.md` | **成功**（"Wrote 116 bytes… mode:create"） |
| 5 | 另一用户 b 搜「杭州湾跨海大桥」 | **命中注入文档 score 0.7778**（与写入者 a 自搜**同分** = account 级共享索引） |
| 6 | b 搜「港珠澳大桥」 / b `content/read` 两个精确 URI | 命中 **0.7356**；read → **200 全文**（两作用域各一例） |
| 7 | b `fs/ls` 两作用域 / 第三方（studio-debug，role=user）ls | 200 / 200 —— 泄漏四面全通 |

⇒ **跨用户注入通道完整坐实**：score 0.78 ≫ 插件 `scoreThreshold` 0.35，召回链路必命中。search 命中证明用 resources 标记两例（0.7778/0.7356），agent 作用域用 read 全文 + ls 证明（其一轮搜索响应截断，判读从严）。

**守住的边界**：user scope 跨读/跨写/跨删全 403 ⇒ 硬性项「越权 0」仍成立——R29 属"合法通道被滥用"而非"越权"。

**根因两层**：① OV 服务端共享作用域无写 ACL（任何 user key 可写；反而 ROOT key 403 不能写）；② `ov_proxy._ALLOWED_TOOLS` 放行 `write` 且 `_dispatch` 对参数无 URI 校验。另：proxy 脚手架排除是 **per-node** 的（`_SCAFFOLD_URIS` 只排除 7 个节点本身，不含子节点）。R16 原判只覆盖"平台自动写入"，未覆盖"agent 主动写共享作用域" ⇒ 判据缺口由本项补上。修复见 R29。

### E. 清理、R25 与产物

| 清理项 | 结果 |
| --- | --- |
| p5gc 两处共享作用域孤儿标记（写入者用户已删） | admin key 补删，200×2 |
| p5gd a 的两处标记 / p2probe 的 proxy 标记 | 200×2 / 200（自清理） |
| 终态 `viking://resources` | `[]`（归零） |
| 终态 `viking://agent` | 只剩平台目录（endpoints/memories） |
| 四个探针用户（p5gc-a/b、p5gd-a/b） | `admin DELETE …/users/{uid}` → 202×4 |

**R25 第 16/17 次复现**：p5gc 轮 `08:30` / p5gd 轮 `08:42` 各一次，均为门禁第 1 轮 `local_ov=000 via_proxy=503`、第 2 轮 `200/200`——两段存活门禁 + 分段 `VALID` 标记持续生效。

**工装纪律（本组新增）**：① ROOT key 不能用于租户数据面取证（全 403），跨用户可见性探针必须用 user/admin key；② 泄漏探针须四面齐全（写→搜→读→ls），只测一环会低估严重度；③ 写入者被删后其共享作用域文件成孤儿，须 admin key 补删。

**产物与复现**：`benchmark/ov-zh/out-p5gc.txt`（P5-18/19/20 首轮 + P5-21 直写证据，241 行）、`benchmark/ov-zh/out-p5gd.txt`（跟进轮：泄漏四面 + agent proxy 通道 + DELETE fs 语义，249 行）；驱动 `_scratch_p5gc.sh` / `_scratch_p5gd.sh`。复现命令模式（唯一可靠）：

```bash
bash -lc "cd /mnt/d/Project/agent-docker-demo && tr -d '\r' < _scratch_p5gd.sh > /tmp/p5gd.sh \
          && bash /tmp/p5gd.sh > benchmark/ov-zh/out-p5gd.txt 2>&1"
```

## 11.18 Phase 5 Group D 完整数据（P5-07/08/15，2026-09-29）

**结论**：**3 项全部 PASS**。产物：`benchmark/ov-zh/out-p5ge.txt`（P5-07，`DONE_P5GE at 2026-09-29T09:58:26Z`）、`benchmark/ov-zh/out-p5gf.txt`（P5-15，`DONE_P5GF at 10:08:13Z`）、`benchmark/ov-zh/out-p5gg.txt`（P5-08，`DONE_P5GG at 10:09:31Z`）。本组为破坏性操作（容器删除重建、`.env` 轮换 + 字节级还原、OV stop/start），全部在 `p2probe` 单用户上完成，终态自洽（`.env` 与原始逐字节一致、tree 与基线全等、清理 200/404 复核通过）。

### A. P5-07 容器重建后记忆存续 = PASS

| 探针步 | 结果 |
| --- | --- |
| 旧容器 | `7940a10b…`（running） |
| `docker rm -f` + 第三次 `ensure_container` | 新容器 `3779a848…`（created 09:58:22），`identity_changed=yes` |
| 命名卷 | `agent-data-p2probe` / `agent-ws-p2probe` **原样保留**（数据在服务端 + 卷，不在容器 rootfs） |
| 口令 / 身份 | ret2=密码正确、ret3=True；token 解码 `ovproxy:p2probe: True` |
| 直读（backend 派生 key） | 200，`identical=true`（rstrip 判定，重建前后两轮均真） |
| **agent 通道**（容器内经 proxy MCP read） | 200，**完整中文内容**，`isError:false` |
| tree 全等 | 重建前后 `fs/tree` **12 叶逐项全等**（`tree_identical=true`） |
| 删除复核 | delete 200 → read 404 |

### B. P5-08 Key 轮换触发重建 = PASS（R12 runbook 实测 + 轮回自愈）

九个 stage 的关键证据：

| Stage | 操作 | 结果 |
| --- | --- | --- |
| 1 基线 | marker 写读 + tree + 旧容器/token/seed 快照 | marker 200、tree 17 叶、旧容器 `3779a848`、旧 token len=100、旧 seed len=64（⚠️ `identical=false` 为探针未修的尾随换行比较，cosmetic，见 D.2） |
| 2 轮换 | 改 `.env` 的 `AGENT_SECRET_KEY` | 新 seed len=64 ≠ 旧 |
| 3 backend 重建 | recreate backend 容器 | 旧 token `verifies_now=null`（Fernet 解不开） |
| 4 **stale 证据** | 查旧容器 env vs 新派生 | **四证据全中**：`old_token_verifies_now=null`、`ov_env_stale=true`、`seed_rotated_in_backend=true`、`new_key_direct_read_expect_401=401`（STAGE4_EXPECTATIONS MET） |
| 5 R12 重铸 | `reauthorize`（`POST {account}/users/{uid}/key` 携新 seed） | `reauthorize_key_matches_derivation=true`、read 200 `identical=true` |
| 6 容器自动重建 | 走正常 `ensure_container` | 新容器 `c45a24e4`、`identity_changed=yes`、5 env 键、token 解码 `ovproxy:p2probe`、agent MCP read 中文全文 |
| 7 还原 | `.env` 字节级还原 + backend 重建 | 为 stage 8 制造"轮回"（旧 K1 重新成为 backend 的派生结果） |
| 8 **轮回自愈** | 旧 K1 token 再度 stale | K2 token null、stale=true、K1 直测 401 → **第三次自动重建** → agent MCP read **200 time=0.094s** 中文全文 → K1 直测 200 `identical=true` |
| 9 终态 | 全面复核 | `/ov/health` 200、tree 17 叶与基线全等、容器 running、`.env` 与原始一致、delete 200 / read 404 |

**机制链（源码级）**：`user_seed(user_id) = HMAC(AGENT_SECRET_KEY, seed_context\0user_id)`（seed 从 `.env` 派生）⇒ 轮换即全体 seed 变；`ensure_container` 对 running 容器查 `_ov_env_stale`，stale 自动删除重建（数据在命名卷存活）。懒注册 409 → K1 key → OV 侧 K2 → 401 → `_forward` 自愈 realign → 200 的轮回链由 stage 8 端到端证实（`_register_user` 409 CONFLICT 不重铸，自愈由 realign 完成）。

### C. P5-15 服务宕机降级 = PASS（R9 判据满足）

| 阶段 | 证据 |
| --- | --- |
| 基线 | 会话 OK（回复「基线正常」8.36s）+ marker 写入 |
| `docker stop` OV | exit 143 |
| 降级期代理面 | `/ov/health` → **502 time=1.094s** + `{"error":{"message":"记忆服务不可达，请稍后重试。"}}`；MCP read → **502 time=0.083s**（快速失败，不挂起） |
| **宕机期会话** | **2/2 轮完整会话全部 OK**（「降级正常」5.70s、「降级仍正常」3.88s，VERDICT = `info.role=="assistant"` 且 parts 有非空 text） |
| 静默性 | 容器日志 15 分钟窗口 **0 条 openviking 行**（不阻塞、不报错；grep 管道尾接 sed 使 `|| echo` 兜底不触发，输出为空行属正常取证形态） |
| 恢复 | `docker start` + 3 轮回绿门禁 → MCP read **200 time=0.029s**、marker 中文命中「都江堰水利工程由李冰父子主持修建」、恢复会话 OK（「恢复正常」2.31s） |
| 清理 | delete 200 |

### D. 两个取证契约（本组新发现）

1. **opencode 1.18.25 会话驱动契约**：`POST /session/{id}/message` **只挂在根路径** —— `/api` 前缀下该 POST 落到 SPA catch-all（200 `text/html` len 2884，与 P5-10 已记录的 `/api/config` 等兜底同源），而 `/api/health`、`/api/session`、`GET /api/session/{id}/message` 等 GET 路由正常。响应是**同步 JSON `{info, parts}`**（非 SSE）。请求体 `{"parts":[{"type":"text","text":"…"}]}`；默认模型 `deepseek-v4-pro`，单轮 input tokens ~20534（含 system prompt + 工具定义 + openviking 插件）；首调 30.56s（冷），热后 2–8s。二进制取证：`grep -ao '"/session[^"]*"' /usr/local/bin/opencode` 命中 `/session/:id/message`、`/session/{id}/prompt_async`、`/session/{id}/command` 等。**首跑 BASELINE_SESSION_FAILED 即此因**（SSE 驱动打到 `/api` 前缀收到 SPA HTML），修法 = health/建会话走 `/api` GET、发消息走根路径 POST + 同步 JSON 解析。
2. **OV `content/read` 剥离尾随换行**：write 207B（含 `\n`）→ read 结果 206B（不含）⇒ 全等判定必须 `CONTENT.rstrip("\n")`（p5gg stage 1 的 `identical=false` 即此 cosmetic）。同因注意：MCP `read` 工具参数是 **`uris` 数组**（`{"uri":…}` 报 pydantic「uris Field required」）。

**R25 复发（第 18–20 次）**：p5ge/p5gf/p5gg 三段探针的门禁各撞上第 1 轮 `local_ov=000 via_proxy=503`、第 2 轮 `200/200`（前序 09:42–09:47 每 ~2 分钟整栈重启 5 次、OV 日志伴 `LockAcquisitionError`，09:47:22 后 12/12 轮稳定自愈）。存活门禁 + 分段 `VALID` + `DONE_*` 终标记持续生效，无一组数据被污染。

**工装纪律（本组新增）**：① 破坏性探针必须按「基线 → 破坏 → 取证 → 恢复 → 终态复核」五段走，终态须证明 `.env`/tree/容器状态与起点逐项一致；② 会话驱动用根路径 POST + 同步 JSON（D.1），health/建会话用 `/api` GET；③ MCP read 一律 `uris[]` 数组；④ 内容全等一律 rstrip 后比较；⑤ 破坏性操作前的基线会话必须先验证 OK 才继续（p5gf 首跑按设计在 baseline_rc=3 中止，未污染降级数据）。

**产物与复现**：`benchmark/ov-zh/out-p5ge.txt` / `out-p5gf.txt` / `out-p5gg.txt`；驱动 `_scratch_p5ge.sh` / `_scratch_p5gf.sh` / `_scratch_p5gg.sh`（+ 调试脚本 `_scratch_p5gl/m/n.sh`，用于定位 D.1 契约；全部 scratch 脚本已在收尾清理中删除，见 §11.19 C）。复现命令模式同 §11.17。

## 11.19 测试收尾清理与 root key 轮换（R1 结案，2026-09-29）

**结论**：测试残留全部清理完毕，R1 的 root key 轮换已执行并验证。产物：`benchmark/ov-zh/out-a6a.txt`（探针用户删除 + 残留计数）、`out-a6b.txt`（docker/WSL 清理）、`out-a6i.txt`（轮换验证）。

### A. OV 探针用户删除（9 个全清，终态 2 用户）

删除通道：**先用用户自己的 key 逐文件清 user scope，再 admin `DELETE …/users/{uid}`（202 异步）**。顺序不能反——admin key 对 user scope 是 403（`PERMISSION_DENIED: Access denied for viking://user/{uid}`），用户删掉后其文件即成**永久孤儿**（P5-21 清理教训的实证：本轮 8+1 个探针全部先清后删，无一孤儿）。

| 用户 | user scope 清理 | admin DELETE |
| --- | --- | --- |
| p2probe / p3c-probe / ovzh-eval / studio-debug / p4-smoke / p4-perf-write / p4-commit | `fs/ls` 递归枚举 + 逐具体文件 DELETE（深路径先删） | 202×7 |
| p4-scale（R24 积压） | **523 个积压文件 → 0** | 202 |
| studio-other（users 列表复核时发现的漏网） | 同上 | 202 |

终态 `GET /api/v1/admin/accounts/agent-platform/users`：`platform-admin` + `3c2e2f2a-…`（历史真实用户，对应 Exited 容器，R17 原则保留）。

计数复核（account 级，platform-admin key，即 A6.1 撞 R25 窗口的欠账补验）：`stats/memories` 从 P5-19 时的 651 条聚合 → **2 条**（platform-admin 自身 preferences:1 + events:1）；`debug/vector/count` **837 → 134**（探针语料向量随删除清理，残留对应保留用户的向量与语义脚手架）。

### B. 脚手架残留定性（保留 + 记录，不做文件系统级删除）

删除用户后文件系统的残留 = **服务端保护脚手架**，非用户内容：
- `sessions/oc-ses_*`、`sessions/mcp-store-*/history/archive_001` 嵌套空目录 + `.abstract.md` / `.overview.md` 元数据（内容仅 frontmatter：`directory: viking://…` + `generated_by: {component: SemanticProcessor, trigger: content_delete|parent_refresh}`，**无任何用户内容**，A5 抽样取证）
- `_system/tasks/{uid}` 空目录

这些路径的 API 删除被服务端拒绝（目录级 `DELETE /api/v1/fs` → 403「use a concrete content path instead」；逐文件删除时 fail 的全是这批脚手架）。**决策：保留 + 记录**——文件系统级 rm 会让向量索引悬空，风险大于收益；而**内容文件（探针语料）已全部删净**（p4-scale 523→0），无泄漏。残留计数（A3 侦察）：p2probe 46 / p3c-probe 72 / p4-smoke 1 / p4-commit 1，数据目录约 90 MiB。

### C. 容器 / 卷 / WSL / 工作区清理

| 项 | 处置 |
| --- | --- |
| `agent-p2probe` 容器 + `agent-data-p2probe` / `agent-ws-p2probe` 卷 | 删除 |
| 独立陈旧 `openviking` 容器（R5 残留，§11.7 已记） | 删除 |
| `agent-test-data` / `agent-test-ws` 孤儿卷（`labels=map[]`、无容器使用） | 删除 |
| WSL `~/.openviking/` 用户属主部分（openviking-repo 128M 等 6 项） | 删除 |
| WSL `~/.openviking/data/`、`ovcli.settings.conf`（**root 属主**，无免密 sudo） | **用户手动**：`sudo rm -rf ~/.openviking/data ~/.openviking/ovcli.settings.conf` |
| `/home/zhangzhixiao/agent-docker-demo/`（完整旧工作副本，9 月 21 日仍在用） | **保留**（用户资产；其 compose 标签分裂已记 §11.14E） |
| 仓库 `_scratch_*`（130 个 .sh + 86 个 .py/.mjs/.out/.log） | 全部删除（证据已固化在 benchmark/ov-zh/out-*.txt 与本文档） |

### D. R1 root key 轮换（结案）

流程：`openssl rand -hex 32` 生成新 key → 根 `.env` 的 `OV_ROOT_API_KEY` 替换（compose 对 openviking 与 backend 双注入 `${OV_ROOT_API_KEY:-}`，**一处改动两处生效**；`ov.conf` 的 `${OV_ROOT_API_KEY}` 只读挂载不动）→ `docker compose up -d openviking backend`（env 变化触发双容器 recreate）。

验证（out-a6i.txt，带 R25 存活门禁，第 3 轮 200）：

| 探针 | 结果 |
| --- | --- |
| 新 key `GET …/admin/accounts/agent-platform/users`（X-API-Key） | **200**，恰 2 用户 |
| 旧 key 同请求（X-API-Key / Bearer 双形式） | **401 / 401** |
| `/ready` | 200 |
| backend 进程内 `settings.openviking_root_api_key` | `is_new=True` / `is_old=False` |
| backend 用进程内 key（Bearer，即 `ov_access.py:163` 的发送形式）调 admin users | **200** |
| `stats/memories` / `debug/vector/count`（platform-admin key） | 200（2 条）/ 200（134） |

**R1 剩余项（用户手动，平台无法代办）**：`OV_EMBEDDING_API_KEY` / `OV_VLM_API_KEY` 是外部 DashScope 凭据，若认为已泄漏须在阿里云控制台自行轮换（新值替换进根 `.env` 后 `docker compose up -d openviking` 即生效）。

### E. R25 复发（第 21–23 次，收尾清理期）

`10:24:11Z`、`10:25:47Z`（间隔 96s）、~`10:37Z` 三次全栈拉起（WSL 用户态高频拆除重建期），撞掉 A0 首跑、A2 首跑（当时未带门禁）与 A6.1 首跑。**教训重申**：一切 OV API 操作前必须带双段存活门禁——A2 补门禁后重跑成功；A6.1 的 vector_count 复核并入本节 A/D 的轮换验证完成。R25 累计 23 次复现，处置不变（宿主侧问题，不改 compose / ov.conf）。

### F. 收尾后的环境终态

- compose 栈：frontend / backend / postgres / openviking 四容器 running，`/health` `/ready` 200，backend 以新 root key 正常工作
- OV 账号 `agent-platform`：2 用户（platform-admin + 历史用户），探针语料归零，仅存 §11.19 B 定性的脚手架与 platform-admin 自身 2 条结构化记忆
- 仓库根目录无 `_scratch_*`；`benchmark/ov-zh/out-*.txt` 证据保留



