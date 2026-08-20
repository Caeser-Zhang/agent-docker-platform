# Agent Docker Platform

四层架构的 AI Agent 平台——**浏览器层 → 平台控制层 → 容器执行层 → 共享服务层**，全链路真实运行，无 mock。

每个用户拥有独立的 Docker 容器，容器内唯一的进程是 [opencode](https://opencode.ai) `serve`（headless agent runtime）。平台不实现任何 agent 逻辑，只负责容器生命周期管理、配置注入和透明反向代理。

> 完整的接口契约见 **[docs/API.md](docs/API.md)**。

## 核心原则：平台不实现任何 Agent 能力

容器里唯一的进程是 **`opencode serve`**（opencode 1.18.16 官方 headless 服务）。Agent loop、LLM 调用、工具执行、会话与消息存储，全部由它负责。

平台只做四件事：

1. **生命周期管理** — 用 Docker SDK 为每个用户创建 / 启动 / 停止加固容器
2. **配置注入** — 把开发者本机的 `opencode.json` 消毒后写进容器的配置卷，支持全局级与项目级两级配置
3. **透明反向代理** — 把浏览器的请求按 opencode 的原生路由原样转发进容器
4. **工作区文件服务** — 附件上传（`tmp/` 隔离目录）、文件树浏览、文件预览与 `@` 引用

> 因此当 opencode 新增能力时，平台**不需要改代码**：前端直接调用 opencode 的新路由即可。

## 架构图

```mermaid
flowchart TB
    subgraph Browser["浏览器层 · Browser Layer"]
        SPA["React SPA<br/>(Chat / ConfigPanel / AdminPanel / Login)"]
        NGINX["nginx<br/>:3000 · SPA 路由 + API 代理"]
        SPA --> NGINX
    end

    subgraph Platform["平台控制层 · Platform Control Layer (FastAPI :8000)"]
        direction TB
        subgraph Control["控制平面"]
            AC["Agent Controller<br/>状态机 · 健康探测 · 崩溃自愈"]
            CM["Container Manager<br/>Docker SDK · 加固参数 · 配置注入"]
        end
        subgraph ConfigPlane["配置平面"]
            CFG["Config API<br/>Provider / MCP / Skill CRUD<br/>(全局级: 宿主 opencode.json)"]
            WS["Workspace API<br/>项目级配置 · Skill 导入<br/>文件上传 / 文件树 / 预览"]
        end
        subgraph DataPlane["数据平面"]
            TUNNEL["Tunnel<br/>透明反向代理 (raw bytes)"]
            PUMP["SSE Pump<br/>单上游 + 环形缓冲 + 扇出"]
        end
        DB[("SQLite / Postgres<br/>用户/角色 + 容器台账")]
        AC --> CM
        AC --> PUMP
    end

    subgraph Runtime["容器执行层 · Container Runtime Layer (agent-net)"]
        C1["容器 agent-u1<br/>opencode serve :4096<br/>非root · 只读根fs · cap-drop ALL"]
        C2["容器 agent-u2<br/>opencode serve :4096<br/>…"]
        C1 -. "卷 workspace-u1<br/>(tmp/ 附件 · .opencode/)" .-> CN
        C2 -. "卷 workspace-u2" .-> CN
        CN["Docker Volumes<br/>per-user workspace + data"]
    end

    subgraph Shared["共享服务层 · Shared Services"]
        LLM["宿主 LLM 代理<br/>(host.docker.internal 回源)"]
    end

    NGINX -->|"JWT · /api/*"| Platform
    TUNNEL -->|HTTP · basic auth| C1
    TUNNEL -->|HTTP · basic auth| C2
    PUMP -->|GET /api/event · SSE| C1
    C1 -->|HTTPS| LLM
    C2 -->|HTTPS| LLM
```

要点：

- 前端只与 backend（9123）通信，nginx 把 `/api/*` 反代过去；用户容器**不映射宿主端口**，只能经 `agent-net` 由 backend 访问
- backend 同时挂在 `platform-net`（接前端）和 `agent-net`（接用户容器）两张网络上
- 每用户两块独立卷：`workspace-*`（工作区，含上传附件与项目级配置）与 `data-*`（opencode 状态）

### 一次对话的请求流转

```mermaid
sequenceDiagram
    autonumber
    participant B as 浏览器 (React SPA)
    participant P as 平台 (FastAPI)
    participant C as 容器 (opencode serve)
    participant L as LLM Provider

    B->>P: POST /api/auth/login → JWT
    B->>P: POST /api/agent/start
    P->>C: 创建加固容器 + 注入消毒后的 opencode.json + 拉起 SSE Pump
    C-->>P: /api/health 探测通过
    B->>P: GET /api/tunnel/events (SSE, token query)
    P-->>B: 订阅 PUMP 扇出流
    B->>P: POST /api/tunnel/oc/api/session/{id}/prompt<br/>{prompt:{text, parts:[file…]}}
    P->>C: raw bytes 透传 (300s 超时)
    C->>L: Agent loop 调用真实 LLM
    C-->>P: session.next.* 事件流
    P-->>B: SSE 扇出 → 流式渲染 / 工具卡片 / 审批卡片
```

## 功能特性

### 容器生命周期
- 每用户独立 Docker 容器，非 root + cap-drop ALL + 只读根文件系统
- 双层健康检查（Dockerfile HEALTHCHECK + 平台探测）
- 崩溃自愈 + restart policy + `/workspace` 与 `/data` 卷持久化
- 空闲回收（默认 30 分钟无活动自动停止）

### 两级配置管理（CRUD + Web UI）
| | 全局级 `/api/config/*` | 项目级 `/api/workspace/*` |
|---|---|---|
| 存储位置 | 宿主机 `opencode.json` + `~/.config/opencode/skills/` | 容器卷 `/workspace/opencode.json` + `/workspace/.opencode/skills/` |
| 作用范围 | 所有用户容器共享 | 仅该用户本工作区 |
| 生效方式 | 注入时消毒（MCP 过滤/回环重写/模型覆盖） | opencode 启动时原生合并 |
| Skill 导入 | 单个 SKILL.md 编辑 | **zip 批量导入**（三种布局自适应，500 文件/单文件 5MB/总量 20MB 上限） |

- **LLM Provider** — 增删改查，支持 OpenAI-compatible / 自定义 baseURL
- **MCP 服务** — 增删改查，Remote (URL) 与 Local (Command) 两种类型，含启用/禁用开关
- **Skills** — 增删改查 + zip 导入，直接编辑 `SKILL.md`（YAML frontmatter + Markdown）
- **一键重载** — 将宿主配置重新注入运行中的容器

### AI 对话
- 流式渲染（SSE）opencode 的实时输出，工具调用可折叠展开
- 多会话管理：创建 / 重命名 / 删除，切换会话自动恢复消息
- 运行时模型切换 + Agent 模式切换（build / plan 等 primary agents，自动过滤 subagent）
- 工具权限审批卡片：允许一次 / 总是允许 / 拒绝，SSE 事件驱动实时出现
- Agent 提问应答卡片：单选 / 多选 / 自定义输入，支持一次多问
- **Skill 显式指定** — 输入框下拉多选（全局 + 项目级合并展示），随 prompt 一并下发
- **文件上传** — 输入框上传按钮，附件进入容器工作区 `tmp/` 隔离目录（≤10MB），不污染工作区根目录
- **`@` 文件引用** — 输入 `@` 触发工作区文件模糊搜索自动补全（opencode `/find/file`），选中后转换为 FilePart 随 prompt 发送；上传成功后自动在输入框追加 `@tmp/…` 引用
- **工作区文件树** — 侧栏树状浏览目录与文件（自动剪除 `.git` / `node_modules` 等重目录），支持折叠 / 刷新
- **前端预览** — HTML（iframe 沙箱渲染）、Markdown（标题/列表/代码块/加粗）、图片（base64）、纯文本；一键 `@ 引用` 插入输入框

### 管理员 Docker 管理（Admin Panel）
- **仅 admin 角色可进入**（Chat 顶部「管理」按钮）；后端每个端点都实时回读数据库角色，降权即时生效
- 管理员来源：**首个注册用户自动成为 admin**；或 `AGENT_ADMIN_USERNAMES` 环境变量（逗号分隔，登录时自动提升）
- **平台总览** — 用户数 / 容器记录数 / Docker 运行中容器数 / 单容器资源限额
- **全用户容器列表** — Docker 状态 / 台账状态 / 健康探测 / 重启次数 / 启动时间 / 最近活动；台账有记录但容器不存在时显式标注
- **实时资源采样**（可开关）— 各容器 CPU 占用率 + 内存用量/限额（并行 `docker stats` 采样）
- **容器操作** — 重启（保留数据卷，自动重建 SSE Pump）、停止、销毁（删容器 + 卷，需输入容器名二次确认防误删）
- **日志查看** — 模态窗口 mono 渲染，tail 100–2000 行可选，自动滚底
- 5 秒自动刷新、操作串行锁定 + toast 反馈

## 快速启动

### 前置条件

- [Docker Engine](https://docs.docker.com/engine/install/) 24.0+
- [Docker Compose](https://docs.docker.com/compose/install/) v2.20+
- [opencode](https://opencode.ai) 兼容的 LLM Provider 配置（如 OpenAI、阿里百炼、火山引擎等；也可启动后在 UI「配置管理」中从零创建）

### 方式 A：Linux / WSL 内一键启动（3 条命令）

适用于仓库在 Linux/WSL 文件系统中的情况（WSL 内 `git clone`，或已通过方式 B 同步过去）：

```bash
# 1. 首次：准备环境变量（编辑 .env，把 OPENCODE_CONFIG_DIR 指向你的 opencode 配置目录）
cp .env.example .env

# 2. 一键启动
bash scripts/up.sh

# 3. 栈体检（可选，也可随时单独运行）
bash scripts/verify.sh
```

[up.sh](scripts/up.sh) 是幂等的，重复执行无副作用，它做了 4 件事：

1. 等待 Docker 守护进程就绪（最多 60s，兼容 WSL VM 冷启动）
2. `agent-demo:1.0.0` 镜像缺失时自动构建；构建失败自动用 `--network=host` 重试（WSL2 DNS 问题规避）
3. `docker compose up -d --build` —— 代码有变更时自动重建镜像并生效
4. 等待后端健康探测通过，随后调用 `verify.sh` 打印各层状态

浏览器打开 **http://localhost:3000** → 注册 → 登录 → 点「启动 Agent」→ 创建会话开始对话。**首个注册的用户自动成为管理员**，登录后可在聊天页顶部点「管理」进入 Docker 管理面板。

### 方式 B：Windows + WSL2（代码在 Windows 上编辑）

> 为什么不直接在 `/mnt/d/...` 上跑：跨文件系统构建慢一个数量级，且 NTFS 会丢失 entrypoint 的可执行位。项目必须运行在 WSL 的 ext4 文件系统中。

在 Windows PowerShell 中执行（路径按实际位置调整）：

```powershell
# 1. 同步 Windows 工作副本 → WSL ext4（~/agent-docker-demo，自动修复 CRLF / 可执行位）
wsl -d Ubuntu-24.04 -- bash /mnt/d/Project/agent-docker-demo/scripts/wsl-sync.sh

# 2. 钉住 WSL VM，防止空闲 ~60s 自动关机带走全部容器
wscript.exe D:\Project\agent-docker-demo\scripts\wsl-keepalive.vbs

# 3. 在 WSL 内一键启动（即方式 A 的 up.sh）
wsl -d Ubuntu-24.04 -- bash ~/agent-docker-demo/scripts/up.sh
```

之后 Windows 浏览器直接访问 **http://localhost:3000**（WSL2 mirrored 网络自动映射端口）。

- **日常迭代** = 改代码（Windows）→ 重复步骤 1、3
- **建议**：把 `wsl-keepalive.vbs` 的快捷方式放进 `shell:startup`（Win+R），开机自动拉起 VM 和容器

### 手动分步（等效于 up.sh，供理解与自定义）

```bash
cp .env.example .env                                 # 配置 OPENCODE_CONFIG_DIR，见下
docker build -t agent-demo:1.0.0 ./agent-image       # agent 镜像；网络受限环境加 --network=host
docker compose up -d --build                         # 平台栈（前端 + 后端）
curl http://localhost:9123/api/health                # 验证 → {"status":"ok"}
```

`OPENCODE_CONFIG_DIR` 按配置所在位置设置：

```bash
# Linux / WSL（配置在 WSL 文件系统中）
OPENCODE_CONFIG_DIR=/home/<you>/.config/opencode

# WSL2（配置在 Windows 文件系统中）
OPENCODE_CONFIG_DIR=/mnt/c/Users/<you>/.config/opencode

# macOS
OPENCODE_CONFIG_DIR=/Users/<you>/.config/opencode
```

如果你的 `opencode.json` 中使用了 `127.0.0.1` 或 `localhost` 的 LLM 代理地址，平台会自动将其重写为 `host.docker.internal`，容器通过 `--add-host=host.docker.internal:host-gateway` 回源到宿主机。

> 没有现成的 opencode.json？可以使用 UI 中的「配置管理」功能从零创建 Provider 和 MCP 配置。

### 常见问题（WSL）

| 症状 | 原因 | 解决 |
|---|---|---|
| WSL 内 curl 正常，Windows 浏览器打不开 | WSL VM 空闲自动关机（默认 ~60s），容器全部停止 | 运行 `wsl-keepalive.vbs`；或在 `%USERPROFILE%\.wslconfig` 设 `vmIdleTimeout=-1` |
| 首次构建 agent 镜像 DNS 解析/超时失败 | WSL2 IPv6 DNS 问题 | `docker build --network=host -t agent-demo:1.0.0 ./agent-image`（`up.sh` 已内置自动重试） |
| `bash: \r: command not found` | 脚本带 Windows CRLF 行尾 | 走 `wsl-sync.sh` 同步（自动转换）；仓库已加 `.gitattributes` 强制 LF |
| 改了前端代码但页面没变化 | 平台镜像未重建 | `docker compose up -d --build`（`up.sh` 默认带 `--build`） |
| 需要停止服务 | — | `docker compose down`（数据在卷中，不受影响；WSL 侧另需 `wsl --shutdown` 才会关 VM） |

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

> 项目级配置（`/workspace/opencode.json`）**不做消毒**——它位于容器卷内，由 opencode 启动时按原生规则与全局配置合并。

### 环境变量

所有 `AGENT_*` 变量集中在 [backend/.env](backend/.env)（**随仓库提交，含演示默认值**，经 docker-compose `env_file` 注入后端容器）。克隆后无需任何配置即可启动；本地修改（连接外部数据库、换密钥等）后执行 `docker compose up -d` 生效，无需重建镜像。注意：**不要把真实凭据提交到仓库**。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_SECRET_KEY` | `change-this-in-production` | JWT 签名密钥，**生产环境必须修改** |
| `AGENT_DATABASE_URL` | `postgresql+asyncpg://agent:agentpass@postgres:5432/agent_demo` | 数据库连接串（栈内 postgres 服务；改回 `sqlite+aiosqlite:////app/data/agent_demo.db` 可切回 SQLite，历史数据保留在 `backend-data` 卷；指向外部 PG 时主机名用 `host.docker.internal` 或 IP） |
| `AGENT_AGENT_IMAGE` | `agent-demo:1.0.0` | Agent 容器镜像名 |
| `AGENT_AGENT_NETWORK` | `agent-net` | Agent 容器网络名 |
| `AGENT_AGENT_PORT` | `4096` | opencode serve 端口 |
| `AGENT_AGENT_WORKDIR` | `/workspace` | 容器内工作目录 |
| `AGENT_OPENCODE_CONFIG_SOURCE` | `/host-opencode/opencode.json` | 宿主配置路径（在容器内） |
| `AGENT_CONTAINER_HOST_ALIAS` | `host.docker.internal` | 宿主机别名 |
| `AGENT_CONTAINER_CPU_LIMIT` | `2.0` | 容器 CPU 限额 |
| `AGENT_CONTAINER_MEMORY_LIMIT` | `2g` | 容器内存限额 |
| `AGENT_CONTAINER_PIDS_LIMIT` | `200` | 容器进程数限额 |
| `AGENT_IDLE_THRESHOLD` | `1800` | 空闲回收阈值（秒，默认 30 分钟） |
| `AGENT_ADMIN_USERNAMES` | `""` | 管理员用户名列表（逗号分隔，登录时自动提升）；首个注册用户也自动成为 admin |
| `AGENT_CORS_ORIGINS` | `["*"]` | CORS 允许来源 |
| `OPENCODE_CONFIG_DIR` | `~/.config/opencode` | 宿主 opencode 配置目录（docker-compose 用） |

## API 端点概览

> 完整的请求/响应结构、错误码与示例见 **[docs/API.md](docs/API.md)**。

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（用户名 + 密码），注册即返回 JWT；首个用户自动成为 admin |
| POST | `/api/auth/login` | 登录，返回 JWT（有效期 24h）；响应含 `role` 字段 |

### 容器生命周期

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/status` | 容器状态 + 健康 |
| GET | `/api/agent/runtime` | 运行时自省（镜像、端口、配置来源、被剥离的字段） |
| POST | `/api/agent/start` | 启动容器（幂等） |
| POST | `/api/agent/stop` | 停止容器 |
| GET | `/api/agent/logs` | 容器日志（最近 100 行） |

### 管理员（仅 role=admin，403 守卫）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/overview` | 平台总览：用户数 / 容器记录数 / 运行中容器数 / 资源限额 |
| GET | `/api/admin/containers` | 全部用户的容器状态（`?stats=1` 附带 CPU/内存实时采样） |
| GET | `/api/admin/containers/{user_id}/logs` | 指定用户容器日志（`?tail=` 可选 100–2000） |
| POST | `/api/admin/containers/{user_id}/restart` | 重启容器（等待健康探测通过后重建 SSE Pump） |
| POST | `/api/admin/containers/{user_id}/stop` | 停止指定用户的容器 |
| POST | `/api/admin/containers/{user_id}/destroy` | 销毁容器 + 数据卷（台账记录保留，状态置 destroyed） |

### 透明代理

| 方法 | 路径 | 说明 |
|---|---|---|
| ANY | `/api/tunnel/oc/{path}` | **透明代理到 opencode 的任意路由**（raw bytes 透传） |
| GET | `/api/tunnel/providers` | 由容器 `/config` 展平的 provider/model 列表 |
| POST | `/api/tunnel/config/reload` | 重新注入宿主配置并重启容器 |
| GET | `/api/tunnel/events` | SSE：opencode 事件扇出（支持 `lastEventId` 重放） |

### 全局配置管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 总览（providers + mcp + skills，API key 脱敏） |
| GET/POST/DELETE | `/api/config/providers/{id}` | Provider 增删改查 |
| GET/POST/PATCH/DELETE | `/api/config/mcp/{name}` | MCP Server 增删改查 + 启停 |
| GET/POST/DELETE | `/api/config/skills/{name}` | 全局 Skill 增删改查（SKILL.md） |
| POST | `/api/config/reload` | 将宿主配置重新注入运行中的容器 |

### 工作区（项目级配置 + 文件服务）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/api/workspace/config` | 项目级 opencode.json 读取（缺省自动创建骨架）/ 保存 |
| GET | `/api/workspace/skills/all` | 全局 + 项目级 skills 合并列表（供输入框下拉） |
| GET/POST/DELETE | `/api/workspace/skills/{name}` | 项目级 Skill 增删改查 |
| POST | `/api/workspace/skills/import` | **zip 批量导入**项目级 skills |
| POST | `/api/workspace/files/upload` | 聊天附件上传（`tmp/` 目录，≤10MB） |
| GET | `/api/workspace/files` | 工作区文件树（扁平列表，剪除 .git/node_modules 等） |
| GET | `/api/workspace/file-content?path=` | 单文件预览读取（text/image/binary，≤2MB） |

代理黑名单：`global/dispose`、`instance/dispose`、`global/upgrade`、`global/config`、`auth/` — 防止沙箱内的用户改写注入的凭据或关掉服务。

## 项目结构

```
agent-docker-demo/
├── docker-compose.yml                 # 全栈编排（frontend + backend + 双网络）
├── .env.example                       # 环境变量模板
├── agent-image/                       # 容器执行层
│   ├── Dockerfile                     # 两阶段：取 opencode 二进制 → 加固运行时
│   ├── entrypoint.sh                  # 准备 XDG 目录 → 清除模型缓存 → exec opencode serve
│   └── opencode.default.json          # 无凭据回落配置
├── backend/                           # 平台控制层 (FastAPI)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                    # FastAPI 入口 + CORS + 全局异常处理
│       ├── config.py                  # 环境变量配置（pydantic-settings）
│       ├── auth.py                    # JWT 签发与校验
│       ├── database.py                # 数据库连接（async SQLAlchemy）+ 角色列迁移 / 管理员自动提升
│       ├── models.py                  # 容器台账等 ORM 模型
│       ├── schemas.py                 # Pydantic 请求/响应模型
│       ├── routers/
│       │   ├── auth.py                # 注册 / 登录
│       │   ├── agent.py               # 容器生命周期 + /runtime 自省
│       │   ├── admin.py               # 管理员 API：总览 / 容器列表 / 日志 / 重启 / 停止 / 销毁
│       │   ├── tunnel.py              # 透明反向代理 + SSE + /providers
│       │   ├── config.py              # 全局配置：Provider/MCP/Skill CRUD
│       │   └── workspace.py           # 项目级配置 + Skill zip 导入 + 文件上传/树/预览
│       └── services/
│           ├── container_manager.py   # Docker SDK、加固参数、配置注入、卷读写
│           ├── agent_controller.py    # 状态机、健康探测、崩溃恢复、空闲回收
│           ├── opencode_config.py     # 宿主配置消毒 / 回环重写 / 默认值
│           ├── host_config.py         # 宿主 Provider/MCP/Skill CRUD 操作
│           ├── tunnel_relay.py        # 到容器的 HTTP 调用（raw bytes 透传）
│           └── sse_pump.py            # 单上游连接 + 环形缓冲 + 扇出
├── frontend/                          # 浏览器层 (React SPA)
│   ├── Dockerfile                     # Vite 构建 → nginx 部署
│   ├── nginx.conf                     # SPA 路由 + API 代理 + SSE 支持
│   └── src/
│       ├── api.ts                     # 平台调用（含 admin API）+ /tunnel/oc 直通 opencode
│       ├── oc/messages.ts             # SessionMessage 归一化 + SSE 归约器
│       └── components/
│           ├── Chat.tsx               # 会话、流式渲染、@引用、文件树/预览、上传
│           ├── AdminPanel.tsx         # 管理员 Docker 面板：总览 / 容器表格 / 资源采样 / 日志 / 操作
│           ├── ConfigPanel.tsx        # 全局/项目级 Provider/MCP/Skill 配置 UI
│           ├── Login.tsx              # 登录/注册
│           ├── chatStyles.ts          # 内联样式
│           └── adminStyles.ts         # 管理面板内联样式
├── docs/
│   ├── REQUIREMENTS.md                # 需求分解（R1-R16）
│   └── API.md                         # API 完整文档
└── scripts/
    ├── up.sh                          # 一键启动：等 dockerd → 补建 agent 镜像 → compose up --build → 健康检查
    ├── verify.sh                      # 栈体检：容器 / 镜像 / 端点 / 前端产物抽查
    ├── wsl-sync.sh                    # Windows 工作副本 → WSL ext4（含 CRLF 修复）
    ├── wsl-keepalive.vbs              # WSL 常驻会话，防止 VM 空闲自动关机
    ├── e2e.py                         # 四层端到端校验
    └── probe-*.py|sh                  # 开发期 opencode 契约探测工具
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
POST /api/session/{sessionID}/prompt    { "prompt": { "text": "...", "parts": [...] } }  ← PromptInput 对象
POST /api/session/{sessionID}/model     { "model": { "providerID": "x", "id": "y" } }  ← ModelRef 对象
GET  /api/session/{sessionID}/message   -> { data: SessionMessage[], cursor }
GET  /api/event                         全局 SSE
GET  /api/health                        健康检查
GET  /config                            生效的合并配置
GET  /config/providers                  已配置的 provider
GET  /find/file?query=&limit=&type=file 工作区文件模糊搜索（注意：不带 /api 前缀）
GET  /file/content?path=                读取文件内容（注意：不带 /api 前缀）
```

容易踩的坑：

- `POST .../prompt` 的 body 必须是 `{ "prompt": { "text": "...", "parts": [...] } }`——`text` 必填，多模态 `parts` 嵌套在 `prompt` 内；顶层传 `parts` 会 400 `Missing key at ["prompt"]`
- `POST .../model` 传 `"provider/model"` 字符串会 400 `Expected Model.Ref, got string`；传 `{providerID, modelID}` 会 400 `Missing key at model`。正确是 `{model:{providerID, id}}`
- `GET /api/provider` 返回 `{"data":[]}`（空），真正能拿到用户 provider 的是 `GET /config` 与 `GET /config/providers`
- `GET /config` 的输出会**剥掉** `mcp` / `plugin` 字段，但服务端启动时**确实加载**了它们——不能靠 `/config` 判断插件是否生效
- 创建 session 时必须传 `agent: "coder"`，否则 opencode 会用内置的全局模型目录（models.json）做 title generation，导致 403 区域限制错误
- 文件路由 `/find/file`、`/file/content` **不带 `/api` 前缀**（与 session 路由不同），经隧道访问即为 `/api/tunnel/oc/find/file`
- FilePart 格式：`{type:"file", mime, filename?, url}`，`url` 为容器内绝对路径（如 `/workspace/tmp/a.pdf`）

会话管理与审批端点（均经 1.18.16 实测）：

```
PATCH  /session/{id}          {title}     → 裸 legacy Session（改名只在 legacy 面，V2 无路由）
DELETE /session/{id}                      → 裸 true（删除同上）
GET    /api/permission/request            → {data: PermissionRequest[]}   注意 /request 后缀，/api/permission 是 404
POST   /api/session/{sid}/permission/{rid}/reply   {reply:"once"|"always"|"reject"} → 204
GET    /api/question/request              → {data: QuestionRequest[]}     同样带 /request 后缀
POST   /api/session/{sid}/question/{rid}/reply     {answers: string[][]}   → 204（每个 answer 为选中 label 数组）
POST   /api/session/{sid}/question/{rid}/reject                            → 204
```

审批 UI 靠 SSE 的 `permission.v2.asked/replied`、`question.v2.asked/replied/rejected` 事件触发列表刷新。

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

1. **修改密钥** — `AGENT_SECRET_KEY` 与 postgres 的 `POSTGRES_PASSWORD`/连接串必须改为强随机值
2. **数据库已使用 PostgreSQL** — 栈内自带 postgres:16-alpine 服务；如需外部数据库，改 `backend/.env` 的 `AGENT_DATABASE_URL` 后 `docker compose up -d` 即可（SQLite→PG 历史数据可用 `scripts/migrate-sqlite-to-pg.py` 迁移）
3. **Docker API 代理** — 后端挂载了 Docker socket，生产环境应替换为 Docker API 代理或远程 Docker daemon
4. **HTTPS** — 在 nginx 前加 TLS 终端（如 Caddy / Traefik）
5. **资源限制** — 根据实际负载调整 `AGENT_CONTAINER_CPU_LIMIT`、`AGENT_CONTAINER_MEMORY_LIMIT`、`AGENT_CONTAINER_PIDS_LIMIT`
6. **定期备份** — 备份 `~/.config/opencode/` 目录、数据库与各用户 workspace 卷

## License

MIT
