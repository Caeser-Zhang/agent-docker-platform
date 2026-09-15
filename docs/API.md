# Agent Docker Platform — API 文档

平台控制层（FastAPI）的全部 HTTP 接口契约。opencode 原生接口经透明代理透传，见 [§8 隧道 API](#8-透明代理隧道) 与 [§9 opencode 常用透传端点](#9-opencode-常用透传端点)。

## 目录

- [通用约定](#通用约定)
- [1. 健康检查](#1-健康检查)
- [2. 认证 API](#2-认证-api)
- [3. Agent 生命周期 API](#3-agent-生命周期-api)
- [4. 管理员 API](#4-管理员-api)
- [5. 全局配置 API](#5-全局配置-api)
- [6. 工作区 API](#6-工作区-api)
- [7. SSE 事件流](#7-sse-事件流)
- [8. 透明代理（隧道）](#8-透明代理隧道)
- [9. opencode 常用透传端点](#9-opencode-常用透传端点)
- [10. 用户反馈 API](#10-用户反馈-api)
- [11. 管理员 UX 指标 API](#11-管理员-ux-指标-api)
- [12. 知识库（KB）API](#12-知识库kb-api)
- [13. 错误码汇总](#13-错误码汇总)

---

## 通用约定

| 项 | 值 |
|---|---|
| Base URL（浏览器侧） | `http://localhost:3000/api`（nginx 反代 → backend :8000） |
| Base URL（直连 backend） | `http://localhost:9123/api` |
| 请求格式 | `application/json`（文件上传除外，为 `multipart/form-data`） |
| 响应格式 | `application/json`（SSE 端点为 `text/event-stream`） |

### 鉴权

除 `POST /api/auth/register`、`POST /api/auth/login`、`GET /`、`GET /api/health` 外，**所有端点均要求 JWT**：

```
Authorization: Bearer <access_token>
```

- Token 为 JWT（HS256），有效期 **24 小时**（1440 分钟）
- 唯一例外：SSE 端点 `GET /api/tunnel/events` 因 `EventSource` 无法携带自定义请求头，改用 **query 参数 `token`**

### 容器状态前置条件

| 前置条件 | 相关路由 | 不满足时返回 |
|---|---|---|
| 容器**已创建**（存在即可，运行/停止均可） | `/api/workspace/*` 全部端点 | `409` Agent 容器尚未创建，请先启动 Agent 再管理项目级配置 |
| 容器**正在运行** | `/api/tunnel/oc/*`、`/api/tunnel/providers` | `503` Agent not running. Please start the agent first. |

### 错误响应格式

```json
{ "detail": "错误描述（字符串）" }
```

请求体校验失败（422）时 `detail` 为错误数组（已剔除 `ctx` 字段以兼容二进制上传场景）：

```json
{ "detail": [ { "type": "missing", "loc": ["body", "content"], "msg": "Field required" } ] }
```

### Skill 命名规则

全部 skill 名称（全局与项目级）须匹配：

```
^[a-z0-9]+(-[a-z0-9]+)*$    # 1-64 位小写字母数字，单词间单连字符
```

---

## 1. 健康检查

### `GET /` — 服务信息（无鉴权）

```json
{ "service": "Agent Docker Platform", "version": "1.0.0", "status": "running" }
```

### `GET /api/health` — 平台健康（无鉴权）

```json
{ "status": "ok" }
```

---

## 2. 认证 API

### `POST /api/auth/register` — 注册（注册即登录）

**请求体**

```json
{ "username": "alice", "password": "secret123" }
```

**成功响应** `200`

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "user_id": "usr_xxx",
  "username": "alice",
  "role": "user"                  // "user" | "admin"
}
```

`role` 决定能否进入前端 Docker 管理面板（见 [§4 管理员 API](#4-管理员-api)）。数据库为空时注册的首个用户自动获得 `admin`；用户名命中 `AGENT_ADMIN_USERNAMES` 环境变量也会获得 `admin`。

**错误**

| 状态码 | 说明 |
|---|---|
| 400 | `Username already taken` |

### `POST /api/auth/login` — 登录

**请求体** 同注册。

**成功响应** `200` 同注册（含 `role`）。登录时若用户名命中 `AGENT_ADMIN_USERNAMES` 且角色不是 admin，会自动提升。

**错误**

| 状态码 | 说明 |
|---|---|
| 401 | `Invalid credentials` |

---

## 3. Agent 生命周期 API

### `GET /api/agent/status` — 容器状态

**响应**

```json
{
  "running": true,
  "healthy": true,
  "status": "running",            // absent / created / running / stopped / exited ...
  "container_name": "agent-abc123",
  "workspace": "/workspace",
  "message": ""
}
```

### `GET /api/agent/runtime` — 运行时自省

描述容器内实际运行的程序与配置来源（证明"平台只启动 opencode serve"）。

**响应**

```json
{
  "runtime": "opencode serve",
  "image": "agent-demo:1.0.0",
  "port": 4096,
  "workdir": "/workspace",
  "network": "agent-net",
  "config": { "...": "配置来源与消毒描述" }
}
```

### `POST /api/agent/start` — 启动容器（幂等）

不存在则创建加固容器，已停止则重启。创建后自动：注入消毒配置 → 健康探测 → 拉起 SSE Pump。

**请求体**（可选）

```json
{ "workspace": null }
```

**响应** 同 `GET /api/agent/status`。

### `POST /api/agent/stop` — 停止容器

优雅停止，保留数据卷。

**响应** 同 `GET /api/agent/status`（`running=false, status="stopped"`）。

### `GET /api/agent/logs` — 容器日志

**响应**

```json
{ "logs": "...最近 100 行日志文本..." }
```

> 原先此处的 `GET /api/agent/containers`（全部容器诊断）已删除——任何登录用户都能看到全平台容器属于越权。等价能力由 [§4 管理员 API](#4-管理员-api) 的 `GET /api/admin/containers` 提供，且仅限 admin 角色。

---

## 4. 管理员 API

平台级 Docker 容器管理（前端"Docker 容器管理"面板的数据源）。**全部端点要求 `role=admin`**，角色从数据库实时读取（非 JWT 声明），降权立即生效；非管理员一律 `403 Admin privileges required`。同为 admin 专属的用户体验指标端点见 [§11](#11-管理员-ux-指标-api)。

### 管理员的三个来源

| 来源 | 说明 |
|---|---|
| 首个注册用户 | 数据库为空时注册的第一个用户自动成为 admin（bootstrap） |
| 环境变量 | `AGENT_ADMIN_USERNAMES=admin,ops`（逗号分隔），启动时自动提升、登录时兜底提升 |
| 手动改库 | `UPDATE users SET role='admin' WHERE username='...'`（SQLite 无后台时的途径） |

### `GET /api/admin/overview` — 平台总览

**响应**

```json
{
  "users": { "total": 6, "admins": 1 },
  "containers": {
    "records": 3,                    // agent_containers 表记录数
    "by_status": { "stopped": 3 },   // 记录状态分布
    "docker_total": 11,              // Docker 守护进程中的平台容器数
    "docker_running": 1
  },
  "platform": {
    "image": "agent-demo:1.0.0", "network": "agent-net", "port": 4096,
    "cpu_limit": 2.0, "memory_limit": "2g"
  }
}
```

### `GET /api/admin/containers` — 全部用户容器

**Query** `stats`（默认 `true`）：是否采集 CPU/内存。每个运行中容器的采样阻塞 1-2s（守护进程取两次读数），多容器并行采样。

**响应**：双源合并——Docker 守护进程（地面真相）∪ `agent_containers` 记录（补用户名/最近活动/错误）。仅库里有记录、Docker 里没有的容器显示 `docker_status="absent"`。

```json
{
  "containers": [
    {
      "user_id": "464d6e13-...",
      "username": "alice",
      "container_name": "agent-464d6e13-...",
      "db_status": "running",          // running/starting/stopped/failed/destroyed/unmanaged
      "docker_status": "running",      // running/exited/.../absent
      "health": "healthy",             // Docker healthcheck，可为 null
      "image": "agent-demo:1.0.0",
      "started_at": "2026-08-20T03:41:22.238Z",
      "last_activity": "2026-08-20T03:41:27.239Z",
      "restart_count": 2,
      "last_error": null,
      "stats": {                        // stats=false 或非运行中时缺省
        "cpu_percent": 8.5,
        "mem_usage_mb": 287.1, "mem_limit_mb": 2048.0,
        "mem_percent": 14.0, "pids": 24
      }
    }
  ]
}
```

### `GET /api/admin/containers/{user_id}/logs` — 容器日志

**Query** `tail`（默认 200，1-2000）。

**响应**

```json
{ "user_id": "...", "tail": 200, "logs": "...日志文本..." }
```

### `POST /api/admin/containers/{user_id}/restart` — 原地重启

`docker restart`（保留容器对象：env/挂载/标签，opencode 密码不变）。内部流程：停 SSE Pump → 重启容器 → 健康探测 → 重建 Pump → `restart_count+1`。通常耗时 15-20s（含 10s 优雅停止窗口）。

**响应** `{ "ok": true, "message": "Container restarted" }`；重启后健康探测失败返回 `409`。

### `POST /api/admin/containers/{user_id}/stop` — 优雅停止

停止容器、保留数据卷与容器对象，用户下次 `start` 直接复用。

**响应** `{ "ok": true, "message": "Agent stopped" }`

### `POST /api/admin/containers/{user_id}/destroy` — 销毁（不可逆）

删除容器**与全部数据卷**（工作区文件、opencode 会话历史永久丢失），DB 记录置 `destroyed`。用户下次进入聊天时容器会重新创建。前端要求输入容器名二次确认。

**响应** `{ "ok": true, "message": "Container and volumes destroyed" }`

**操作端点公共错误**

| 状态码 | 说明 |
|---|---|
| 404 | `No Docker container for this user`（Docker 中无此容器，含 absent 记录） |
| 409 | 重启后健康探测失败 |

---

## 5. 全局配置 API

管理**宿主机全局** `opencode.json` 与 `~/.config/opencode/skills/`。写入后需调用 `POST /api/config/reload` 注入容器生效。

### `GET /api/config` — 配置总览（秘密脱敏）

**响应**

```json
{
  "providers": {
    "bailian": {
      "name": "阿里百炼",
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://...", "hasApiKey": true },
      "models": { "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" } }
    }
  },
  "mcp": {
    "my-server": { "type": "remote", "enabled": true, "url": "https://...", "command": null }
  },
  "skills": [ { "name": "code-review", "description": "...", "dir": "code-review" } ]
}
```

API key / header / env 等秘密仅以布尔（`hasApiKey` / `hasHeaders` / `hasEnv`）暴露，永不回传。

### Provider CRUD

#### `GET /api/config/providers` — 列出全部 Provider

```json
{
  "providers": {
    "bailian": {
      "name": "阿里百炼",
      "npm": "@ai-sdk/openai-compatible",
      "baseURL": "https://...",
      "hasApiKey": true,
      "models": ["deepseek-v4-flash"]
    }
  }
}
```

#### `POST /api/config/providers/{provider_id}` — 创建/更新 Provider（合并式）

省略的字段保留旧值；`options` 浅合并（留空 `apiKey` 不会清空已保存的 key）；`models` 为空对象时不覆盖现有模型列表。

**请求体**

```json
{
  "name": "阿里百炼",
  "npm": "@ai-sdk/openai-compatible",
  "options": { "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1", "apiKey": "sk-..." },
  "models": { "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" } }
}
```

**响应** `{ "status": "ok", "provider_id": "bailian" }` · **错误**：底层异常 → 400

#### `DELETE /api/config/providers/{provider_id}` — 删除 Provider

**响应** `{ "status": "ok" }` · **错误**：不存在 → 404

### MCP CRUD

#### `GET /api/config/mcp` — 列出全部 MCP Server

```json
{
  "mcp": {
    "remote-one": { "type": "remote", "enabled": true, "url": "https://...", "hasHeaders": true },
    "local-one":  { "type": "local",  "enabled": false, "command": ["npx", "-y", "pkg"], "hasEnv": true }
  }
}
```

#### `POST /api/config/mcp/{name}` — 创建/更新 MCP Server（全量替换该条目）

**请求体**（按类型二选一）

```json
// Remote
{ "type": "remote", "url": "https://mcp.example.com/sse", "enabled": true,
  "headers": { "Authorization": "Bearer ..." }, "timeout": 30000 }

// Local
{ "type": "local", "command": ["npx", "-y", "@mcp/server"], "enabled": true,
  "environment": { "KEY": "value" }, "cwd": "/somewhere", "timeout": 30000 }
```

**响应** `{ "status": "ok", "name": "remote-one" }`
**错误**：`type` 非 local/remote、local 缺 `command`、remote 缺 `url`、name 非法 → 400

> local 类型在注入容器时会被消毒流程丢弃（命令依赖宿主机可执行文件）。

#### `PATCH /api/config/mcp/{name}` — 启用/禁用

**请求体** `{ "enabled": false }` → **响应** `{ "status": "ok", "name": "...", "enabled": false }` · 不存在 → 404

#### `DELETE /api/config/mcp/{name}` — 删除

**响应** `{ "status": "ok" }` · 不存在 → 404

### Skill CRUD（全局级）

#### `GET /api/config/skills` — 列出全局 Skills

```json
{ "skills": [ { "name": "code-review", "description": "...", "dir": "code-review" } ] }
```

#### `GET /api/config/skills/{name}` — 读取单个 Skill

```json
{ "name": "code-review", "description": "...", "content": "---\nname: code-review\n...\n---\n\n正文", "dir": "code-review" }
```

不存在 → 404。`content` 为 SKILL.md 全文（含 YAML frontmatter）。

#### `POST /api/config/skills/{name}` — 创建/更新 Skill

**请求体** `{ "content": "---\nname: code-review\ndescription: 代码审查\n---\n\n..." }`

frontmatter 必须含 `name` 与 `description` 字段。

**响应** `{ "status": "ok", "name": "...", "description": "...", "dir": "..." }`
**错误**：frontmatter 缺字段 / name 非法 → 400

#### `DELETE /api/config/skills/{name}` — 删除 Skill（整个目录）

**响应** `{ "status": "ok" }` · 不存在 → 404

### `POST /api/config/reload` — 重载配置到容器

把宿主配置重新消毒注入容器并重启（opencode 仅在启动时读配置），随后重建 SSE Pump。

**响应**

```json
{ "reloaded": true, "message": "Config reloaded into container" }
```

无容器时：`{ "reloaded": false, "message": "No container to reload — 请先启动 Agent" }`

---

## 6. 工作区 API

管理**项目级**配置（容器卷 `/workspace/opencode.json`）、项目级 Skills（`/workspace/.opencode/skills/`）与聊天附件 / 文件浏览。全部经 Docker archive API 操作，容器运行或停止均可，但**容器必须已创建**（否则 409）。

### `GET /api/workspace/config` — 读取项目级 opencode.json

文件不存在时自动创建骨架 `{"$schema": "https://opencode.ai/config.json"}`。

**响应**

```json
{
  "scope": "project",
  "exists": true,
  "created": false,           // 本次请求是否刚创建了骨架
  "valid": true,              // JSON 是否可解析
  "content": "{ ... }",       // 文件原文
  "config": { ... }           // 解析后的 dict；解析失败为 null
}
```

### `PUT /api/workspace/config` — 保存项目级 opencode.json（整文件替换）

**请求体**

```json
{ "content": "{ \"$schema\": \"https://opencode.ai/config.json\", ... }" }
```

**响应**

```json
{ "status": "ok", "message": "已保存，重启 Agent 后生效（点击「重载到容器」）" }
```

**错误**：JSON 非法 / 顶层非对象 / 超 1MB → 400

### `GET /api/workspace/skills/all` — 全局 + 项目级 Skills 合并列表

聊天输入框 skill 选择器的数据源。

**响应**

```json
{
  "skills": [
    { "name": "code-review", "description": "...", "dir": "code-review", "scope": "global"  },
    { "name": "my-skill",    "description": "...", "dir": "my-skill",    "scope": "project" }
  ]
}
```

全局列表读取失败时仅记 warning 并跳过，不影响 project 部分。

### 项目级 Skill CRUD

#### `GET /api/workspace/skills` — 仅列出项目级 Skills

结构与 `/skills/all` 相同（全部 `scope: "project"`）。

#### `GET /api/workspace/skills/{name}` — 读取单个项目级 Skill

```json
{ "scope": "project", "name": "my-skill", "description": "...", "dir": "my-skill", "content": "---\n...\n---\n\n正文" }
```

name 非法 → 400；不存在 → 404

#### `POST /api/workspace/skills/{name}` — 创建/更新项目级 Skill

**请求体** `{ "content": "---\nname: my-skill\ndescription: ...\n---\n\n..." }`（frontmatter 必须含 `name` 与 `description`，≤512KB）

**响应** `{ "status": "ok", "name": "...", "description": "...", "dir": "...", "scope": "project" }`

#### `DELETE /api/workspace/skills/{name}` — 删除项目级 Skill（整个目录）

**响应** `{ "status": "ok" }` · 不存在 → 404

#### `POST /api/workspace/skills/import` — 压缩包批量导入项目级 Skills

> 路由定义在 `/skills/{name}` 之前，`import` 不会被当作 skill 名捕获。

**请求** `multipart/form-data`，字段 `file`。支持格式：`.zip`、`.rar`、`.7z`、`.tar`、`.tar.gz`/`.tgz`、`.tar.bz2`/`.tbz2`、`.tar.xz`/`.txz`；扩展名不在支持列表时按文件头魔数嗅探（误命名文件仍可导入），无法识别 → 400。

**支持的布局**（自适应识别，各格式通用）：

| 布局 | 结构 |
|---|---|
| 裸结构 | 根目录直接是 `SKILL.md` + 资源文件 |
| 单包裹 | `<skill-name>/SKILL.md` + 资源 |
| 多 skill | `<a>/SKILL.md`、`<b>/SKILL.md` … |

**校验与限制**

- 每个条目须通过命名规则 + frontmatter 校验（`description` **必填**；`name` 取 frontmatter，缺省用目录名；zip 内不允许重名）
- 拒绝绝对路径 / `..` / 含 `:` 的成员名（防路径穿越）
- ≤500 个文件、单文件 ≤5MB、解压总量 ≤20MB
- **替换语义**：导入前先删除同名旧目录再写入

**响应**

```json
{
  "status": "ok",
  "imported": [
    { "name": "my-skill", "description": "...", "dir": "my-skill", "scope": "project", "fileCount": 3 }
  ],
  "message": "已导入 1 个 skill，重启 Agent 后生效（点击「重载到容器」）"
}
```

**错误**：不支持的压缩包格式 / 空文件 / 超限（413）/ 损坏压缩包 / 各类校验失败 → 400（中文 detail）

### 文件服务

#### `POST /api/workspace/files/upload` — 聊天附件上传

上传到容器工作区 `tmp/` 隔离目录（会话级临时附件，不污染工作区根目录）。

**请求** `multipart/form-data`，字段 `file`（filename 自动清洗为纯 basename，≤10MB）

**响应**

```json
{
  "status": "ok",
  "path": "tmp/report.pdf",              // 容器内相对路径
  "absPath": "/workspace/tmp/report.pdf",
  "filename": "report.pdf",
  "size": 102400,
  "mime": "application/pdf",
  "isImage": false
}
```

**错误**：文件名无效/空文件 → 400；超 10MB → 413；写失败 → 500

> 前端上传成功后自动在输入框追加 `@tmp/report.pdf`，发送时转换为 FilePart。

#### `GET /api/workspace/files` — 工作区文件树（扁平列表）

`find` 命令扫描工作区卷，已剪除 `.git`、`node_modules`、`.opencode/cache`、`.cache` 等重目录。容器运行时用 `exec_run`，停止时用临时容器只读挂载。

**响应**

```json
{
  "files": [
    { "path": "tmp",           "type": "dir",  "size": 4096 },
    { "path": "tmp/report.pdf","type": "file", "size": 102400 },
    { "path": "index.html",    "type": "file", "size": 2048 }
  ],
  "protected": ["opencode.json", ".opencode"]
}
```

`path` 为工作区相对路径，前端自行组装树。`protected` 是平台托管路径前缀清单，删除接口用它决定是否需要二次确认（见下）。读取失败 → 500。

#### `POST /api/workspace/files/delete` — 批量删除文件/目录

删除工作区内的文件或目录，目录连同整棵子树一起删（底层 `rm -rf`）。逐个路径独立处理，一个坏路径不会让整批失败。

**请求体**

```json
{ "paths": ["tmp/report.pdf", "draft"], "force": false }
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `paths` | `string[]` | 工作区相对路径，文件或目录；自动去重 |
| `force` | `bool` | 默认 `false`；仅在前端对平台托管路径完成二次确认后传 `true` |

**响应**

```json
{ "status": "ok", "deleted": ["tmp/report.pdf", "draft"], "failed": [] }
```

`status` 为 `ok`（全部成功）或 `partial`（部分失败）。

**错误**

| 状态码 | 场景 |
|---|---|
| 400 | 未选择路径 / 超过 200 个路径 / 试图删除工作区根目录 |
| 409 | `force=false` 且命中平台托管路径（`opencode.json`、`.opencode/`） |
| 409 | Agent 容器尚未创建 |
| 500 | 全部路径删除失败 |

路径安全由 `container_manager._workspace_path()` 把关（拒绝 `..`、绝对路径、盘符），越界路径归入 `failed`。

#### `GET /api/workspace/file-content?path=…` — 单文件预览读取

**查询参数** `path`：工作区相对路径（如 `tmp/report.md`）

**响应**（按文件类型三选一）

```json
// 图片（扩展名映射到 image/* mime）
{ "type": "image",  "mime": "image/png", "base64": "iVBORw..." }

// 文本（前 4KB 不含 NUL 字节）
{ "type": "text",   "mime": "text/markdown", "content": "# 标题..." }

// 二进制（其余）
{ "type": "binary", "mime": "application/pdf", "size": 102400 }
```

MIME 按扩展名推导（覆盖 html/md/txt/json/csv/js/ts/tsx/py/sh/css/png/jpg/jpeg/gif/svg/webp，其余 `application/octet-stream`）。

**错误**：文件不存在 → 404；超 2MB → 413

---

## 7. SSE 事件流

### `GET /api/tunnel/events` — opencode 事件扇出（SSE）

把容器内 opencode 的 `GET /event` 事件流（经 SSE Pump：单上游连接 + 200 条环形缓冲 + 多订阅者扇出）中继给浏览器。

`/event` 是 opencode 官方 Web UI 使用的事件面。与只覆盖部分生命周期事件的 `/api/event` 相比，它会发出 assistant 的 `message.part.delta` 增量，平台前端据此逐段渲染回复。

**鉴权**（例外方式）：query 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `token` | 是 | JWT（EventSource 无法发送 Authorization 头） |
| `lastEventId` | 否 | 断线重连回放起点（默认 0），连接后先回放错过的事件 |

```
GET /api/tunnel/events?token=<JWT>&lastEventId=42
```

**响应头**

```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

**事件格式**

```
id: 42
data: {"type":"message.part.delta","properties":{"sessionID":"ses_...","messageID":"msg_...","partID":"prt_...","field":"text","delta":"你好"}}

```

- 每个事件两行：`id:`（用于断线重连游标）+ `data:`（完整事件对象 JSON）
- `/event` 的业务 payload 位于 `properties`；前端会同时兼容旧事件面的 `data`
- 空闲 20 秒发送注释保活行：`: keep-alive`
- Agent 未运行时发送单条事件后结束流：`data: {"type":"agent.disconnected","data":{"message":"Agent not running"}}`

### 前端关心的 SSE 事件类型

V1 durable 事件是当前前端的主路径。`message.updated` 与 `message.part.updated` 都是**全量替换**；`message.part.delta` 是增量文本，必须按 `partID` 追加到既有内容。

```
message.updated          # properties: { sessionID, info: Message }
message.part.delta       # properties: { sessionID, messageID, partID, field: "text"|"reasoning", delta }
message.part.updated     # properties: { sessionID, part: Part, time }
message.part.removed     # properties: { messageID, partID }
message.removed          # properties: { sessionID, messageID }
session.created / session.updated / session.status / session.idle
permission.* / question.*
```

旧版 `session.next.*` / `permission.v2.*` / `question.v2.*` 事件仍被前端 reducer 兼容，但不应作为 v1.18.16 流式渲染的依赖。

---

## 8. 透明代理（隧道）

### `ANY /api/tunnel/oc/{path}` — 透传到 opencode 任意路由

**核心机制**：去掉 `/api/tunnel/oc` 前缀后，请求体以 **raw bytes 原样透传**（绝不 json.loads 再序列化——这是曾导致 "Model unavailable" 的根因），透传请求头仅 `content-type`。

| 项 | 值 |
|---|---|
| 允许的方法 | GET / POST / PUT / PATCH / DELETE（其余 → 405） |
| 超时 | `*/prompt` 结尾 → 300s（agent 运行可能数分钟）；其余 → 60s |
| 保活 | 非 GET 请求成功后刷新活跃时间戳（防空闲回收） |
| 响应 | 上游 body 为 JSON（dict/list）时按上游 status 返回 JSON；否则原始字节 + 上游 content-type |

**路径映射示例**

| 浏览器请求 | 容器内 opencode 收到 |
|---|---|
| `POST /api/tunnel/oc/api/session` | `POST /api/session` |
| `POST /api/tunnel/oc/api/session/ses_x/prompt` | `POST /api/session/ses_x/prompt` |
| `GET /api/tunnel/oc/find/file?query=a` | `GET /find/file?query=a` |
| `GET /api/tunnel/oc/config` | `GET /config` |

**代理黑名单**（归一化后命中前缀即 403 `Path <p> is not proxied`）：

```
global/dispose · instance/dispose · global/upgrade · global/config · auth/
```

防止沙箱内用户改写注入的凭据或远程关停服务。

**错误**：Agent 未运行 → 503

### `GET /api/tunnel/providers` — Provider/Model 目录

向容器内 opencode 发 `GET /config`（15s 超时），以**容器实际生效配置**为准（非平台侧副本）。

**响应**

```json
{
  "providers": [
    { "id": "bailian", "name": "阿里百炼", "baseURL": "https://...",
      "models": [ { "id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash" } ] }
  ],
  "default": "bailian/deepseek-v4-flash",
  "smallModel": "bailian/qwen-turbo",
  "source": { "...": "配置来源描述" }
}
```

上游异常时：`{ "providers": [], "default": null, "error": "opencode /config returned <status>" }`。apiKey 永不返回。

### `POST /api/tunnel/config/reload` — 重注入宿主配置并重启容器

等价于 `POST /api/config/reload` 的隧道入口：`container_manager.reload_config()` + `agent_controller.restart_pump()`（重启会断开上游 SSE，需重建 pump）。

**响应** `{ "reloaded": true, "source": { ... } }` · 无容器 → 503

---

## 9. opencode 常用透传端点

以下 opencode 1.18.16 原生端点均可经 `/api/tunnel/oc/...` 访问（均经实测）。完整契约以 opencode 官方文档为准。

### 会话

```http
# 创建会话
POST /api/tunnel/oc/api/session
{ "agent": "build", "model": { "providerID": "bailian", "id": "deepseek-v4-flash" }, "location": { "directory": "/workspace" } }
→ { "data": { "id": "ses_..." } }

# 异步发送 prompt（前端使用；立即返回 204，后续内容走 SSE）
POST /api/tunnel/oc/session/{sessionID}/prompt_async
{ "parts": [
  { "type": "text", "text": "总结一下" },
  { "type": "file", "mime": "application/pdf", "filename": "a.pdf", "url": "file:///workspace/tmp/a.pdf" },
  { "type": "agent", "name": "reviewer" }
] }
→ 204 No Content

# 同步 prompt（兼容接口；会等待整个 agent run，不适合浏览器流式 UI）
POST /api/tunnel/oc/api/session/{sessionID}/prompt
{ "prompt": { "text": "总结一下", "parts": [ ... ] } }

# 切换模型（ModelRef 对象，不是字符串）
POST /api/tunnel/oc/api/session/{sessionID}/model
{ "model": { "providerID": "bailian", "id": "deepseek-v4-flash" } }

# 拉取消息：v1.18.16 的真实历史在 legacy 路径，返回裸数组
GET /api/tunnel/oc/session/{sessionID}/message
→ [{ "info": { "id": "msg_...", "role": "assistant", "time": { ... } }, "parts": [ ... ] }]

# 注意：/api/session/{sessionID}/message 可返回 { "data": [], "cursor": {} }
# 对当前 V1 会话并非前端历史消息来源

# 重命名 / 删除会话（legacy 面）
PATCH  /api/tunnel/oc/session/{id}    { "title": "新标题" }
DELETE /api/tunnel/oc/session/{id}
```

**发送方式选择**：浏览器对话应使用 `prompt_async`，其请求体顶层是 `parts`，204 后以 `/api/tunnel/events` 的 `message.part.delta` 流式展示。同步 `prompt` 的 body 才是 `{ "prompt": { "text", "parts" } }`，会等待整个执行完成，不应用于交互式流式界面。

### 审批与提问

```http
# 拉取待审批的工具权限（注意 /request 后缀）
GET /api/tunnel/oc/api/permission/request
→ { "data": PermissionRequest[] }

# 回复审批
POST /api/tunnel/oc/api/session/{sid}/permission/{rid}/reply
{ "reply": "once" | "always" | "reject" }        → 204

# 拉取 Agent 提问
GET /api/tunnel/oc/api/question/request
→ { "data": QuestionRequest[] }

# 回复提问（每个 answer 为选中的 label 数组）
POST /api/tunnel/oc/api/session/{sid}/question/{rid}/reply
{ "answers": [["选项A"], ["自定义文本"]] }        → 204

# 拒绝回答提问
POST /api/tunnel/oc/api/session/{sid}/question/{rid}/reject   → 204
```

### 文件（注意：不带 `/api` 前缀）

```http
# 工作区文件模糊搜索（@ 引用自动补全的数据源）
GET /api/tunnel/oc/find/file?query=report&limit=15&type=file
→ ["tmp/report.pdf", "docs/report.md"]

# 读取文件内容（path 为容器内绝对路径）
GET /api/tunnel/oc/file/content?path=/workspace/tmp/report.pdf
→ { "type": "text", "content": "..." }
```

### 配置与健康

```http
GET /api/tunnel/oc/api/health        → opencode 健康检查
GET /api/tunnel/oc/config            → 生效的合并配置（会剥掉 mcp/plugin 字段）
GET /api/tunnel/oc/config/providers  → 已配置的 provider
```

### FilePart（多模态附件）

```json
{ "type": "file", "mime": "application/pdf", "filename": "report.pdf", "url": "/workspace/tmp/report.pdf" }
```

`url` 为**容器内绝对路径**。前端发送逻辑：上传的附件 + 文本中解析出的 `@path` 引用（去重、校验存在性）统一转换为 FilePart。

---

## 10. 用户反馈 API

assistant 回复的点赞/点踩采集。**登录即可用，不限 admin**。数据落 `message_feedback` 表，自然键 `(user_id, message_id)`，投票后**不可取消**（前端点击即锁定）。指标消费方见 [§11 管理员 UX 指标 API](#11-管理员-ux-指标-api)。

### `POST /api/feedback` — 提交点赞/点踩

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | ✓ | opencode 会话 id（≤255） |
| `message_id` | string | ✓ | 被评价的 **assistant** 消息 id（≤255）；无 assistant 消息时前端用 `user:{id}` 合成 |
| `user_message_id` | string \| null | | 触发本轮的 user 消息 id |
| `verdict` | `"up"` \| `"down"` | ✓ | 其余取值 → 422 |
| `reason_codes` | string[] | | 仅 `down` 生效；非白名单码静默丢弃，自动去重 |
| `reason_text` | string \| null | | 仅 `down` 生效（「其他」自由文本）；服务端 strip 后**截断至 500 字** |
| `turn_errored` | bool | | 本轮是否报错（前端快照，默认 false） |
| `model_provider` / `model_id` / `agent` | string \| null | | 各 ≤128 |
| `context` | object | | 本轮完整上下文快照（前端组装：上一 user 提问 → 本 assistant 全部输出 + 工具轨迹）；服务端 JSON 化后**截断至 60 000 字符**，超限置 `context_truncated=true` |

**原因码白名单**（与前端 `FeedbackModal` / `oc/feedback.ts` 的 `REASON_OPTIONS` 一致）

| 码 | 含义 |
|---|---|
| `misunderstood` | 没理解我的意图 |
| `wrong_answer` | 答案错误 |
| `tool_failure` | 工具调用失败 |
| `too_verbose` | 太啰嗦 |
| `ignored_constraints` | 忽略了约束/要求 |
| `interrupted` | 中途被打断 |
| `other` | 其他（配合 `reason_text`） |

**响应**

```json
{ "ok": true, "already": false, "verdict": "down", "message_id": "msg_..." }
```

- **幂等**：同一 `(user_id, message_id)` 重复提交返回 `already=true` + 既有 `verdict`，**不改写**已存原因与上下文；并发撞唯一索引同样降级为 `already=true`。
- `verdict="up"` 时忽略 `reason_codes` / `reason_text`（点赞只记态度）。
- 反馈行 `user_id` **无外键**（证据留存约定），写入不因用户记录缺失而失败。

**错误**

| 状态码 | 说明 |
|---|---|
| 422 | `verdict` 非 `up/down`、必填字段缺失或超长 |
| 429 | 轻限流：每用户 60s 内最多 30 次提交（滑动窗口，仅防刷） |

### `GET /api/feedback/session/{session_id}` — 回填已锁定状态

前端加载会话时调用，把已投过票的消息渲染为锁定态（「感谢反馈」）。

**Query** `limit`（默认 500，1-2000）

**响应**（不含 `context` 快照，仅渲染所需最小字段）

```json
{
  "session_id": "ses_...",
  "feedback": [
    { "message_id": "msg_...", "verdict": "up", "created_at": "2026-09-01T02:11:05.120000+00:00" }
  ]
}
```

---

## 11. 管理员 UX 指标 API

**仅 admin**（路由级 `require_admin`，前缀 `/api/admin/ux`）。

数据来源为**服务端 SSE tap**——每容器一个 Pump 在 `ContainerEventBus.push_event()` 处挂载 observer（前端不可篡改），辅以按需**历史回补**；落 `agent_round_metrics` / `tool_call_metrics` / `message_feedback` 三表，明细行受保留窗口自动清理。

**统计单位（双轨）**

| 单位 | 定义 | 对应字段 |
|---|---|---|
| 回合 round | user prompt → `session.idle`，每回合 1 行 | 自然键 `(user_id, session_id, message_id)` |
| 任务 task | 该回合出现过 `todo.updated` 且 todos 全部 `completed` | `is_task=true` / `task_success` |

**四层指标**：L1 结果（任务成功率、回合成功率、错误率）· L2 效率与性能（耗时均值 + p50/p90/p99、成本）· L3 过程与轨迹（工具调用准确率、Token 效率）· L4 主观满意度（点赞/点踩，整合 [§10](#10-用户反馈-api)）。

分位数在应用层用最近秩法计算（SQLite 无 `percentile_cont`）；**所有比率/均值字段在无样本时为 `null`**，前端显示「—」。

**公共 Query**

| 参数 | 适用端点 | 默认 | 说明 |
|---|---|---|---|
| `days` | 全部 | overview/trends/tools/feedback = 30；rounds = 7 | 时间窗 1-365，按 `created_at` 过滤 |
| `user_id` | overview/trends/tools/rounds | — | 按用户过滤 |
| `model_provider` | overview/trends | — | 按模型供应商过滤 |

### `GET /api/admin/ux/overview` — 四层汇总

```json
{
  "window_days": 30,
  "filters": { "user_id": null, "model_provider": null },
  "l1_outcome": {
    "rounds_total": 128, "round_success_rate": 0.92, "error_rate": 0.05,
    "task_rounds": 41, "task_success_rate": 0.78
  },
  "l2_efficiency": {
    "duration_avg_ms": 24510.5, "duration_p50_ms": 18200.0,
    "duration_p90_ms": 61000.0, "duration_p99_ms": 145000.0,
    "total_cost": 3.412, "avg_cost": 0.0266
  },
  "l3_process": {
    "tool_calls": 612, "tool_errors": 24, "tool_accuracy": 0.96,
    "total_tokens": 1840233, "avg_tokens_per_round": 14376.8, "tokens_per_success": 15594.5
  },
  "l4_satisfaction": {
    "thumbs_up": 17, "thumbs_down": 5, "total": 22, "satisfaction_rate": 0.77,
    "down_reasons": { "wrong_answer": 3, "too_verbose": 2, "other": 1 }
  }
}
```

> L4 只按 `days` / `user_id` 过滤反馈表，**不受 `model_provider` 影响**（反馈行不记录 provider 维度的回合关联）。`down_reasons` 一条反馈可含多个码，故各码计数之和可大于 `thumbs_down`。

### `GET /api/admin/ux/trends` — 按天时间序列

Python 侧分桶（跨方言安全，不依赖 `date()`/`date_trunc` 差异），只返回**有数据的日期**。

```json
{
  "window_days": 30,
  "series": [
    {
      "date": "2026-09-01", "rounds": 22, "success_rate": 0.95,
      "duration_avg_ms": 21033.4, "duration_p90_ms": 54000.0, "tool_accuracy": 0.97,
      "total_tokens": 310442, "cost": 0.61,
      "satisfaction_rate": 0.8, "thumbs_up": 4, "thumbs_down": 1
    }
  ]
}
```

### `GET /api/admin/ux/tools` — 工具调用准确率排行

**Query** 额外支持 `limit`（默认 50，1-200）；按调用次数降序，`GROUP BY tool_name`。

```json
{
  "window_days": 30,
  "tools": [
    { "tool_name": "bash", "calls": 210, "errors": 12, "accuracy": 0.943 },
    { "tool_name": "read", "calls": 180, "errors": 0, "accuracy": 1.0 }
  ]
}
```

### `GET /api/admin/ux/rounds` — 回合明细（下钻排障）

**Query** 额外支持 `session_id`、`only_failed`（默认 false）、`limit`（默认 100，1-500）、`offset`。最新在前。

```json
{
  "total": 128, "limit": 20, "offset": 0,
  "rounds": [
    {
      "id": 91, "user_id": "464d...", "session_id": "ses_...", "round_seq": 3,
      "message_id": "msg_...", "is_task": true, "succeeded": true,
      "task_success": true, "errored": false, "error_text": null,
      "duration_ms": 18420, "total_tokens": 24110, "cost": 0.031,
      "tool_calls": 7, "tool_errors": 0,
      "model_provider": "anthropic", "model_id": "claude-...", "agent": "build",
      "source": "tap",                          // tap=实时采集 / backfill=历史回补
      "created_at": "2026-09-01T02:11:05.120000+00:00"
    }
  ]
}
```

### `GET /api/admin/ux/feedback` — 反馈明细（只读，含上下文快照）

**Query** 额外支持 `verdict`（`up`/`down`，缺省为全部）、`limit`（默认 100，1-500）、`offset`。最新在前。

```json
{
  "total": 22, "limit": 20, "offset": 0,
  "feedback": [
    {
      "id": 7, "user_id": "464d...", "session_id": "ses_...",
      "message_id": "msg_...", "user_message_id": "msg_...",
      "verdict": "down", "reason_codes": ["wrong_answer", "other"],
      "reason_text": "忽略了我只改前端的约束", "turn_errored": false,
      "model_provider": "anthropic", "model_id": "claude-...", "agent": "build",
      "context": { "user_text": "...", "assistant_text": "...", "tools": [] },
      "context_truncated": false,
      "created_at": "2026-09-01T02:15:41.880000+00:00"
    }
  ]
}
```

> `context` 为落库 JSON 的反序列化结果。若写入时快照被截断（`context_truncated=true`），落库串不再是合法 JSON，本端点返回 `context: {}`——需要完整前缀时直接查库读原始列。

### `POST /api/admin/ux/backfill` — 历史指标回补

按需回补某用户某会话的历史回合（复用与实时采集同一套结算器，**幂等写入**：已有自然键的回合跳过）。**要求该用户容器正在运行**（需回连容器读取会话历史）。

**请求体**

```json
{ "user_id": "464d6e13-...", "session_id": "ses_..." }
```

**响应**

```json
{ "ok": true, "session_id": "ses_...", "records": 12, "inserted": 5 }
```

`records` = 从容器的会话历史读出的回合数，`inserted` = 本次新增行数（差值为已存在而被幂等跳过）。

**错误**

| 状态码 | 说明 |
|---|---|
| 404 | `No container record for this user` |
| 409 | `Container not running (status=...)` |
| 502 | `Backfill failed: ...`（容器不可达 / 历史读取或结算异常） |

---

## 12. 知识库（KB）API

fastk 知识库白名单体系（设计详见 [FASTK_APIKEY_WHITELIST_DESIGN.md](./FASTK_APIKEY_WHITELIST_DESIGN.md)）。核心模型：

- **凭据（`kb_keys` 表）**：每个物理库一条真实 API key，Fernet 加密存储，**只写不读**——任何端点都不回传 key 本体（仅 `has_api_key` 布尔与时间戳），管理员会话被盗也无法导出。
- **白名单（`kb_grants` 表）**：用户 ↔ 数据库授权矩阵，自然键 `(user_id, kb_name)`；回收为软删除（盖 `revoked_at`），重新授权时复活原行。
- **权限判定**统一走 `services/kb_access.py`，三个消费方共享同一决策路径：Agent 代理（§12.3）、引用角标（§12.4）、管理端点（§12.1），口径永不漂移。
- 授权/回收**立即生效**（每次请求实时重读白名单），无需重建容器。

相关配置（`backend/.env`）：`AGENT_FASTK_SERVER_URL`（真实 fastk 服务器，仅后端直连）、`AGENT_KB_PROXY_BASE`（注入容器的代理基址，默认 `http://backend:8000`）、`AGENT_KB_ADMIN_CONTACT`（渲染进 403 文案的管理员联系方式）、`AGENT_KB_CATALOG_KEY`（读取服务器**全局目录** `GET /fastk/api/databases/` 的凭据——该端点是服务器级的，`kb_keys` 的逐库 key 不适用；仅用于取描述，不改变授权边界，留空表示免 key；**绝不注入 agent 容器**，否则容器可绕过白名单直连宿主枚举并读取全部库）。

### 12.1 管理端点（前缀 `/api/admin`，全部要求 `role=admin`）

#### `GET /api/admin/kb-keys` — 已录入凭据列表

```json
{ "items": [ { "kb_name": "global", "has_api_key": true, "created_at": "...", "updated_at": "..." } ] }
```

#### `PUT /api/admin/kb-keys/{kb_name}` — 录入 / 轮换凭据

请求体 `{ "api_key": "..." }`。幂等 upsert：不存在时 `action=kbkey.create`，已存在时 `kbkey.rotate`。`kb_name` 须匹配 `^[A-Za-z0-9_-]{1,64}$`（否则 400 `无效的知识库名`）。写审计日志。

#### `DELETE /api/admin/kb-keys/{kb_name}` — 删除凭据（连带物理删除全部授权）

该库的所有 grant 行（活跃与已回收）一并物理删除——凭据已不存在，保留悬空授权无意义。响应含 `revoked_users`（删除瞬间仍活跃的用户 id 列表）。404 `该知识库凭据不存在`。写审计日志。

#### `GET /api/admin/kb-grants` — 白名单矩阵

可选 query `kb_name` / `user_id` 缩小范围；仅返回活跃行（`revoked_at IS NULL`），每条带 `username` / `uid` 冗余便于展示。

#### `POST /api/admin/kb-grants` — 授权

请求体 `{ "kb_name": "...", "username": "..." }` 或 `{ "kb_name": "...", "uid": "..." }`（二选一，均缺则 400）。前置校验：

| 条件 | 结果 |
|---|---|
| 该库尚未录入凭据 | 400，提示先 `PUT /api/admin/kb-keys/{kb_name}` |
| 用户不存在 | 404 `用户不存在` |
| 已有活跃授权 | 幂等，直接返回成功 |
| 授权曾被回收 | 复活软删除行：清 `revoked_at` 并重置 `created_at`（视为全新授权） |

写审计日志。

#### `DELETE /api/admin/kb-grants/{user_id}/{kb_name}` — 回收

软删除（盖 `revoked_at`）；旧会话中已渲染的引用角标随之失效（见 §12.4）。无活跃授权时 404 `该授权不存在`。写审计日志。

#### `GET /api/admin/kb-users` — 全部用户（矩阵行选择器数据源）

仅标识信息：`user_id` / `username` / `uid` / `role`，无秘密。

#### `GET /api/admin/kb-user-access?user_id=...` — 单用户授权视图

```json
{
  "user_id": "...",
  "username": "...",
  "granted":   [ { "kb_name": "global",  "created_at": "..." } ],
  "available": [ { "kb_name": "finance", "has_api_key": true } ]
}
```

`granted` = 活跃白名单行；`available` = 已录入凭据但尚未授权给该用户的库（即可直接授权的全集）。404 `用户不存在`。

### 12.2 用户端点

#### `GET /api/kb/my-databases` — 当前用户可读的库名

```json
{ "databases": ["global", "hr"] }
```

登录即可用。前端借此提前展示 Agent 可检索范围，而不是先撞 403。仅名称，无凭据。

#### `GET /api/kb/my-catalog` — 可读库名 + 描述（展示用）

```json
{ "databases": [ { "name": "global", "description": "全员通用知识库" } ] }
```

名称来自白名单（`kb_grants`），描述取自 fastk 服务器目录（与代理目录同一套过滤，剥离 `uri`；目录端点是服务器级的，用全局 `AGENT_KB_CATALOG_KEY` 读取）。**过滤由白名单驱动**：响应遍历 `granted` 构造，未授权的库即使出现在全局目录里也不会进入响应（连名字都没有），故全局 key 只影响能否拿到描述、不放宽可见范围。**软失败**：fastk 不可达、或目录返回非 200（如 key 失效的 401，会记一条 warning）时，返回已授权库名 + 空描述，面板仍能展示"能访问什么"。

### 12.3 Agent 容器代理（白名单强制点）

#### `GET|POST /fastk/api/{path}` — 转发到 fastk 服务器的只读代理

**不走 JWT**：调用方是 Agent 容器内置的 fastk CLI，鉴权凭请求头 `X-API-Key` = 容器创建时注入的 `FASTK_API_KEY`——一个不透明的 Fernet 代理令牌（编码 `kbproxy:<user_id>`），**不是真实 key**。容器同时被注入 `FASTDB_BASE_URL`（= `AGENT_KB_PROXY_BASE`），CLI 流量只能经过本代理。

决策流：令牌 → user_id（无效则 401）→ 每请求实时读 `kb_grants`：

| 路径形态 | 行为 |
|---|---|
| `databases`（目录） | 拉取服务器全量列表后**只保留已授权的库**，并剥离 `uri` 字段（宿主存储路径，容器内无意义）；不注入 key |
| `databases/{kb}/...` | 未授权 → 403（文案含 `fastk databases` 指引与管理员联系方式）；已授权但库无可用凭据 → 500；否则注入该库真实 key 后转发 |
| 其他 | 404 `未知的知识库端点` |

**只读约束**：GET 全放行（服务器所有 GET 均为读）；POST 仅允许 `/search`、`/query`、`/grep` 三个读端点（CLI 实际使用的全部），其余 POST 一律 405。调用方的 `X-API-Key` 转发前**必定剥除**，绝不上传。

错误响应形状与平台惯例不同（CLI 契约，原样打印 `error.message`）：

```json
{ "error": { "message": "无权限访问知识库 'finance'。……" } }
```

上游不可达 → 502 `fastk 服务不可达`。

> 旧环境容器（`FASTDB_BASE_URL` 直连 fastk 服务器、或无有效代理令牌）构成白名单绕过——`container_manager` 在启动时检测此类容器并**强制重建**（工作区/数据卷保留，会话不丢）。

### 12.4 引用角标端点（浏览器侧，JWT）

渲染聊天历史中的来源引用。**每次请求重查白名单**：授权被回收后，旧会话里已渲染的角标同样失效（403 文案与代理一致，前端原样展示）。

#### `GET /api/fastk/chunk?db=&chunk_id=` — 取一个 chunk 的完整内容与元数据

#### `GET /api/fastk/chunk-image?db=&chunk_id=&index=` — 中继 chunk 附带图片（`index` 默认 0）

两端点错误语义相同：

| 状态码 | 说明 |
|---|---|
| 400 | `db` / `chunk_id` 非法（同 `^[A-Za-z0-9_-]{1,64}$` 闭集） |
| 403 | 用户不在该库白名单（含已回收） |
| 500 | 该库缺少可用凭据（运维错误，文案含管理员联系方式） |
| 502 | fastk 服务不可达 |

---

## 13. 错误码汇总

| 状态码 | 场景 |
|---|---|
| 400 | 请求体校验失败、JSON 无效、frontmatter 缺字段、名称非法（skill/知识库）、skill zip 布局/大小不符、上传文件名为空、kb-grants 缺 username 与 uid、知识库尚未录入凭据 |
| 401 | JWT 缺失/无效/过期、登录凭证错误、KB 代理令牌无效 |
| 403 | 命中代理黑名单（`global/dispose`、`auth/` 等）、非 admin 访问 `/api/admin/*`、用户不在知识库白名单（代理与引用角标） |
| 404 | 资源不存在（provider/skill/文件/知识库凭据/授权/用户）、admin 操作的容器在 Docker 中不存在、回补目标用户无容器记录、未知的知识库代理端点 |
| 405 | 代理端点收到不在白名单的 HTTP 方法、KB 代理收到非读端点的 POST |
| 409 | 工作区端点在容器未创建时调用、admin 重启后健康探测失败、回补时容器未运行 |
| 413 | 上传超 10MB、预览超 2MB、skill zip 超限 |
| 422 | Pydantic 请求体校验失败（`detail` 为错误数组） |
| 429 | 反馈提交触发轻限流（每用户 60s / 30 次） |
| 500 | 容器/文件系统操作失败、知识库缺少可用凭据 |
| 502 | UX 历史回补失败（容器不可达或结算异常）、fastk 服务不可达 |
| 503 | Agent 容器未运行（tunnel 端点）、无容器可重载 |
