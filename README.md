# Agent Docker Platform

四层架构的 AI Agent 平台——**浏览器层 → 平台控制层 → 容器执行层 → 共享服务层**，全链路真实运行，无 mock。

每个用户拥有独立的 Docker 容器，容器内唯一的进程是 [opencode](https://opencode.ai) `serve`（headless agent runtime）。平台不实现任何 agent 逻辑，只负责容器生命周期管理、配置注入和透明反向代理。

## 核心原则：平台不实现任何 Agent 能力

容器里唯一的进程是 **`opencode serve`**（opencode 1.18.16 官方 headless 服务）。Agent loop、LLM 调用、工具执行、会话与消息存储，全部由它负责。

平台只做三件事：

1. **生命周期管理** — 用 Docker SDK 为每个用户创建 / 启动 / 停止加固容器
2. **配置注入** — 把开发者本机的 `opencode.json` 消毒后写进容器的配置卷
3. **透明反向代理** — 把浏览器的请求按 opencode 的原生路由原样转发进容器

> 因此当 opencode 新增能力时，平台**不需要改代码**：前端直接调用 opencode 的新路由即可。

## 架构图

```
浏览器层 (React SPA, nginx)
    │ JWT / fetch / EventSource
    ▼
平台控制层 (FastAPI)
    ├── 控制平面  Agent Controller + Container Manager (Docker SDK)
    ├── 配置平面  Config API (Provider / MCP / Skill CRUD)
    └── 数据平面  Tunnel（透明反向代理）+ SSE Pump（事件扇出）
    │ agent-net 上的 HTTP（容器不映射宿主端口）
    ▼
容器执行层 · per-user 加固容器
    └── opencode serve --hostname 0.0.0.0 --port 4096
    │
    ▼
共享服务层  SQLite/Postgres（容器台账）+ 宿主 LLM 代理（host.docker.internal）
```

## 请求流转

| 浏览器发起 | 平台做什么 | 容器里执行 |
|---|---|---|
| `POST /api/agent/start` | Docker SDK 创建容器、注入配置、健康探测、拉起 SSE 泵 | `opencode serve` 启动 |
| `POST /api/tunnel/oc/api/session` | 去掉 `/api/tunnel/oc` 前缀，raw bytes 透传 | `POST /api/session` |
| `POST /api/tunnel/oc/api/session/{id}/prompt` | 同上（超时放宽到 300s） | opencode 调用真实 LLM |
| `GET /api/tunnel/events` | 扇出容器的 `GET /api/event` | opencode 推 `session.next.*` |
| `GET /api/tunnel/providers` | 读容器的 `GET /config` 并展平 | opencode 返回生效配置 |
| `GET /api/config` | 读取宿主 opencode.json 总览 | — |
| `POST /api/config/providers/{id}` | 写入宿主 opencode.json | — |
| `POST /api/config/mcp/{name}` | 写入宿主 opencode.json | — |
| `POST /api/config/skills/{name}` | 写入 `~/.config/opencode/skills/` | — |
| `POST /api/config/reload` | 重新注入配置并重启容器 | opencode 重启加载新配置 |

平台**没有**任何 `llm.py`、`server.py`、prompt 模板或模型适配代码。

## 功能特性

### 容器生命周期
- 每用户独立 Docker 容器，非 root + cap-drop ALL + 只读根文件系统
- 双层健康检查（Dockerfile HEALTHCHECK + 平台探测）
- 崩溃自愈 + restart policy + `/workspace` 与 `/data` 卷持久化
- 空闲回收（默认 30 分钟无活动自动停止）

### 配置管理（CRUD）
通过 Web UI 或 REST API 直接管理宿主 `opencode.json` 和 Skills 目录：

- **LLM Provider** — 增删改查，支持 OpenAI-compatible / 自定义 baseURL
- **MCP 服务** — 增删改查，支持 Remote (URL) 和 Local (Command) 两种类型，含启用/禁用开关
- **Skills** — 增删改查，直接编辑 `SKILL.md` 文件（YAML frontmatter + Markdown）
- **一键重载** — 将宿主配置重新注入运行中的容器

### AI 对话
- 流式渲染（SSE）opencode 的实时输出
- 工具调用展示（可折叠）
- 多会话管理
- 运行时模型切换

## 快速部署

### 前置条件

- [Docker Engine](https://docs.docker.com/engine/install/) 24.0+
- [Docker Compose](https://docs.docker.com/compose/install/) v2.20+
- [opencode](https://opencode.ai) 兼容的 LLM Provider 配置（如 OpenAI、阿里百炼、火山引擎等）

### 步骤 1：克隆仓库

```bash
git clone https://github.com/Caeser-Zhang/agent-docker-platform.git
cd agent-docker-platform
```

### 步骤 2：配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，设置你的 opencode 配置路径：

```bash
# Linux / WSL（配置在 WSL 文件系统中）
OPENCODE_CONFIG_DIR=/home/<you>/.config/opencode

# WSL2（配置在 Windows 文件系统中）
OPENCODE_CONFIG_DIR=/mnt/c/Users/<you>/.config/opencode

# macOS
OPENCODE_CONFIG_DIR=/Users/<you>/.config/opencode
```

如果你的 `opencode.json` 中使用了 `127.0.0.1` 或 `localhost` 的 LLM 代理地址，平台会自动将其重写为 `host.docker.internal`，容器通过 `--add-host=host.docker.internal:host-gateway` 回源到宿主机。

> 没有现成的 opencode.json？可以使用 UI 中的"配置管理"功能从零创建 Provider 和 MCP 配置。

### 步骤 3：构建 Agent 镜像

```bash
docker build -t agent-demo:1.0.0 ./agent-image
```

**网络受限环境**（如 WSL2 IPv6 DNS 问题）请加 `--network=host`：

```bash
docker build --network=host -t agent-demo:1.0.0 ./agent-image
```

可指定已缓存的基础镜像避免外网拉取：

```bash
docker build --network=host -t agent-demo:1.0.0 \
    --build-arg NODE_IMAGE=node:20-slim \
    --build-arg RUNTIME_IMAGE=debian:bookworm-slim \
    ./agent-image
```

### 步骤 4：启动全栈

```bash
docker compose up -d
```

### 步骤 5：验证

```bash
# 前端
curl http://localhost:3000

# 后端健康检查
curl http://localhost:9123/api/health
```

浏览器访问 **http://localhost:3000**，注册账号 → 登录 → 点击"启动 Agent" → 创建会话 → 开始对话。

## 配置说明

### opencode.json 示例

如果你没有现成的 opencode 配置，可以在启动后通过 UI 的"配置管理"按钮创建，或手动创建：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
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
    "skill": "allow"
  },
  "provider": {
    "bailian": {
      "name": "阿里百炼",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "apiKey": "sk-your-api-key"
      },
      "models": {
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" }
      }
    }
  },
  "agents": {
    "coder": {
      "model": "bailian/deepseek-v4-flash"
    }
  },
  "enabled_providers": ["bailian"]
}
```

### 配置消毒流程

平台在将宿主 `opencode.json` 注入容器前，会执行以下消毒步骤：

1. **MCP 过滤** — 保留 `remote` 类型（URL 可达），丢弃 `local` 类型（命令依赖宿主机可执行文件，容器内不可用）
2. **回环地址重写** — `http://127.0.0.1:8787/v1` → `http://host.docker.internal:8787/v1`
3. **模型覆盖** — 强制将 `agents.*.model` 设为配置的默认 model，防止 opencode 回退到内置 provider
4. **Provider 白名单** — 添加 `enabled_providers`，限制 opencode 只使用用户配置的 provider
5. **叠加默认值** — `autoupdate:false`、`share:disabled`、所有 `permission` 设为 `allow`

配置通过 `container.put_archive()` 在容器创建后、启动前写入 `/data/config/opencode/opencode.json`。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_SECRET_KEY` | `change-this-in-production` | JWT 签名密钥，**生产环境必须修改** |
| `AGENT_DATABASE_URL` | `sqlite+aiosqlite:///./agent_demo.db` | 数据库连接字符串 |
| `AGENT_AGENT_IMAGE` | `agent-demo:1.0.0` | Agent 容器镜像名 |
| `AGENT_AGENT_NETWORK` | `agent-net` | Agent 容器网络名 |
| `AGENT_AGENT_PORT` | `4096` | opencode serve 端口 |
| `AGENT_AGENT_WORKDIR` | `/workspace` | 容器内工作目录 |
| `AGENT_OPENCODE_CONFIG_SOURCE` | `/host-opencode/opencode.json` | 宿主配置路径（在容器内） |
| `AGENT_CONTAINER_HOST_ALIAS` | `host.docker.internal` | 宿主机别名 |
| `AGENT_CORS_ORIGINS` | `["*"]` | CORS 允许来源 |
| `OPENCODE_CONFIG_DIR` | `~/.config/opencode` | 宿主 opencode 配置目录（docker-compose 用） |

## API 端点

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（用户名 + 密码） |
| POST | `/api/auth/login` | 登录，返回 JWT |

### 容器生命周期

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/status` | 容器状态 + 健康 |
| GET | `/api/agent/runtime` | 运行时自省（镜像、端口、配置来源、被剥离的字段） |
| POST | `/api/agent/start` | 启动容器（幂等） |
| POST | `/api/agent/stop` | 停止容器 |
| GET | `/api/agent/logs` | 容器日志 |
| GET | `/api/agent/containers` | 诊断信息 |

### 透明代理

| 方法 | 路径 | 说明 |
|---|---|---|
| ANY | `/api/tunnel/oc/{path}` | **透明代理到 opencode 的任意路由** |
| GET | `/api/tunnel/providers` | 由容器 `/config` 展平的 provider/model 列表 |
| POST | `/api/tunnel/config/reload` | 重新注入宿主配置并重启容器 |
| GET | `/api/tunnel/events` | SSE：opencode 事件扇出（支持 `lastEventId` 重放） |

### 配置管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 总览（providers + mcp + skills，API key 脱敏） |
| GET | `/api/config/providers/{id}` | 获取单个 Provider |
| POST | `/api/config/providers/{id}` | 新增/更新 Provider |
| DELETE | `/api/config/providers/{id}` | 删除 Provider |
| GET | `/api/config/mcp/{name}` | 获取单个 MCP Server |
| POST | `/api/config/mcp/{name}` | 新增/更新 MCP Server |
| PATCH | `/api/config/mcp/{name}` | 启用/禁用 MCP Server |
| DELETE | `/api/config/mcp/{name}` | 删除 MCP Server |
| GET | `/api/config/skills/{name}` | 获取单个 Skill 内容 |
| POST | `/api/config/skills/{name}` | 新增/更新 Skill（SKILL.md） |
| DELETE | `/api/config/skills/{name}` | 删除 Skill |
| POST | `/api/config/reload` | 将宿主配置重新注入运行中的容器 |

代理黑名单：`global/dispose`、`instance/dispose`、`global/upgrade`、`global/config`、`auth/` — 防止沙箱内的用户改写注入的凭据或关掉服务。

## 项目结构

```
agent-docker-demo/
├── docker-compose.yml                 # 全栈编排
├── .env.example                       # 环境变量模板
├── agent-image/                       # 容器执行层
│   ├── Dockerfile                     # 两阶段：取 opencode 二进制 → 加固运行时
│   ├── entrypoint.sh                  # 准备 XDG 目录 → 清除模型缓存 → exec opencode serve
│   └── opencode.default.json          # 无凭据回落配置
├── backend/                           # 平台控制层 (FastAPI)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                    # FastAPI 入口
│       ├── config.py                  # 环境变量配置
│       ├── auth.py                    # JWT 认证
│       ├── database.py               # 数据库连接
│       ├── models.py                  # SQLAlchemy 模型
│       ├── schemas.py                 # Pydantic 模型
│       ├── routers/
│       │   ├── auth.py               # 注册 / 登录
│       │   ├── agent.py              # 容器生命周期 + /runtime 自省
│       │   ├── tunnel.py             # 透明反向代理 + SSE + /providers
│       │   └── config.py             # Provider/MCP/Skill CRUD
│       └── services/
│           ├── container_manager.py  # Docker SDK、加固参数、配置注入
│           ├── agent_controller.py   # 状态机、健康探测、崩溃恢复
│           ├── opencode_config.py    # 宿主配置消毒 / 回环重写 / 默认值
│           ├── host_config.py         # Provider/MCP/Skill CRUD 操作
│           ├── tunnel_relay.py       # 到容器的 HTTP 调用（raw bytes 透传）
│           └── sse_pump.py           # 单上游连接 + 环形缓冲 + 扇出
├── frontend/                          # 浏览器层 (React SPA)
│   ├── Dockerfile                    # Vite 构建 → nginx 部署
│   ├── nginx.conf                    # SPA 路由 + API 代理 + SSE 支持
│   ├── package.json
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api.ts                    # 平台调用 + /tunnel/oc 直通 opencode
│       ├── oc/messages.ts            # SessionMessage 归一化 + SSE 归约器
│       └── components/
│           ├── Chat.tsx              # 会话、流式渲染、工具调用、切换 LLM
│           ├── ConfigPanel.tsx       # Provider/MCP/Skill 配置管理 UI
│           ├── Login.tsx             # 登录/注册
│           └── chatStyles.ts         # 内联样式
└── scripts/
    ├── wsl-sync.sh                   # Windows 工作副本 → WSL ext4
    ├── wsl-up.sh                     # 等 dockerd → compose up → 打印健康
    ├── wsl-keepalive.vbs             # WSL 常驻会话防关机
    ├── e2e.py                        # 四层端到端校验
    └── probe-*.py|sh                 # 从 OpenAPI 文档提取 opencode 契约
```

## 容器加固

每个用户容器均启用以下安全措施：

```python
# container_manager.py _build_run_kwargs()
--user 1000:1000                        # 非 root
--read-only                             # 只读根文件系统
--cap-drop ALL                          # 丢弃全部 capabilities
--security-opt no-new-privileges:true   # 禁止提权
--security-opt apparmor=docker-default
--tmpfs /tmp:size=256m                  # bun 运行时与工具输出的临时空间
--tmpfs /home/agent:size=64m,uid=1000
--cpus 2.0 --memory 2g --pids-limit 200
--network agent-net                     # 不映射宿主端口
--add-host host.docker.internal:host-gateway
--restart unless-stopped
HEALTHCHECK curl -fsS /api/health
```

写入面只有 `/workspace`（工作区卷）、`/data`（opencode 状态卷）、两个 tmpfs。

## opencode 契约要点

版本 1.18.16，路径参数为 `{sessionID}`：

```
POST /api/session                       { agent?, model?, location?: { directory } }
                                        -> { data: { id: "ses_..." } }
POST /api/session/{sessionID}/prompt    { "prompt": { "text": "..." } }        ← PromptInput 对象，不是字符串
POST /api/session/{sessionID}/model     { "model": { "providerID": "x", "id": "y" } }  ← ModelRef 对象
GET  /api/session/{sessionID}/message   -> { data: SessionMessage[], cursor }
GET  /api/event                         全局 SSE
GET  /api/health                        健康检查
GET  /config                            生效的合并配置
GET  /config/providers                  已配置的 provider
```

容易踩的坑：

- `POST .../model` 传 `"provider/model"` 字符串会 400 `Expected Model.Ref, got string`；传 `{providerID, modelID}` 会 400 `Missing key at model`。正确是 `{model:{providerID, id}}`。
- `GET /api/provider` 返回 `{"data":[]}`（空），真正能拿到用户 provider 的是 `GET /config` 与 `GET /config/providers`。
- `GET /config` 的输出会**剥掉** `mcp` / `plugin` 字段，但服务端启动时**确实加载**了它们——不能靠 `/config` 判断插件是否生效。
- 创建 session 时必须传 `agent: "coder"`，否则 opencode 会用内置的全局模型目录（models.json）做 title generation，导致 403 区域限制错误。

`SessionMessage` 是按 `type` 区分的联合类型：`user` / `assistant` / `system` / `synthetic` / `shell` / `compaction` / `agent-switched` / `model-switched`。`assistant.content[]` 内再分 `text` / `reasoning` / `tool`。

SSE 事件（`data` 内均带 `sessionID`）：

```
session.next.prompt.admitted / prompted
session.next.step.started / ended / failed
session.next.text.started / delta / ended         delta 用 textID 聚合
session.next.reasoning.started / delta / ended
session.next.tool.called / input.delta / progress / success / failed
session.next.model.switched · session.next.agent.switched
session.idle · session.created · session.updated · session.error
```

## 技术栈

| 层 | 技术 |
|---|---|
| 浏览器层 | React 18 + TypeScript + Vite 5 + nginx |
| 平台控制层 | Python 3.12 + FastAPI + uvicorn + SQLAlchemy + httpx + Docker SDK |
| 容器执行层 | Debian + opencode 1.18.16 (Node.js runtime) |
| 共享服务层 | SQLite (aiosqlite) / 可扩展 PostgreSQL |

## 生产部署建议

1. **修改密钥** — `AGENT_SECRET_KEY` 必须改为强随机值
2. **使用 PostgreSQL** — 将 `AGENT_DATABASE_URL` 改为 PostgreSQL 连接字符串
3. **Docker API 代理** — 后端挂载了 Docker socket，生产环境应替换为 Docker API 代理或远程 Docker daemon
4. **HTTPS** — 在 nginx 前加 TLS 终端（如 Caddy / Traefik）
5. **资源限制** — 根据实际负载调整 `container_cpu_limit`、`container_memory_limit`、`container_pids_limit`
6. **定期备份** — 备份 `~/.config/opencode/` 目录和数据库

## License

MIT