# Agent Docker Platform — API 文档

平台控制层（FastAPI）的全部 HTTP 接口契约。opencode 原生接口经透明代理透传，见 [§7 隧道 API](#7-透明代理隧道) 与 [§8 opencode 常用透传端点](#8-opencode-常用透传端点)。

## 目录

- [通用约定](#通用约定)
- [1. 健康检查](#1-健康检查)
- [2. 认证 API](#2-认证-api)
- [3. Agent 生命周期 API](#3-agent-生命周期-api)
- [4. 全局配置 API](#4-全局配置-api)
- [5. 工作区 API](#5-工作区-api)
- [6. SSE 事件流](#6-sse-事件流)
- [7. 透明代理（隧道）](#7-透明代理隧道)
- [8. opencode 常用透传端点](#8-opencode-常用透传端点)
- [9. 错误码汇总](#9-错误码汇总)

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
  "username": "alice"
}
```

**错误**

| 状态码 | 说明 |
|---|---|
| 400 | `Username already taken` |

### `POST /api/auth/login` — 登录

**请求体** 同注册。

**成功响应** `200` 同注册。

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

### `GET /api/agent/containers` — 全部容器诊断

> 注意：当前实现未做 admin 角色校验，任何登录用户均可调用。

**响应**

```json
{ "containers": [ { "...": "Docker 容器诊断信息" } ] }
```

---

## 4. 全局配置 API

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

## 5. 工作区 API

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

#### `POST /api/workspace/skills/import` — zip 批量导入项目级 Skills

> 路由定义在 `/skills/{name}` 之前，`import` 不会被当作 skill 名捕获。

**请求** `multipart/form-data`，字段 `file`（`.zip`）

**支持的 zip 布局**（自适应识别）：

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

**错误**：非 zip / 空文件 / 各类校验失败 → 400（中文 detail）

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
  ]
}
```

`path` 为工作区相对路径，前端自行组装树。读取失败 → 500。

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

## 6. SSE 事件流

### `GET /api/tunnel/events` — opencode 事件扇出（SSE）

把容器内 opencode 的 `GET /api/event` 事件流（经 SSE Pump：单上游连接 + 200 条环形缓冲 + 多订阅者扇出）中继给浏览器。

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
data: {"type":"session.next.text.delta","data":{...,"sessionID":"ses_..."}}

```

- 每个事件两行：`id:`（用于断线重连游标）+ `data:`（完整事件对象 JSON）
- 空闲 20 秒发送注释保活行：`: keep-alive`
- Agent 未运行时发送单条事件后结束流：`data: {"type":"agent.disconnected","data":{"message":"Agent not running"}}`

### 前端关心的 SSE 事件类型

`data` 内均带 `sessionID`：

```
session.next.prompt.admitted / prompted
session.next.step.started / ended / failed
session.next.text.started / delta / ended           # delta 用 textID 聚合
session.next.reasoning.started / delta / ended
session.next.tool.called / input.delta / progress / success / failed
session.next.model.switched · session.next.agent.switched
session.idle · session.created · session.updated · session.error
permission.v2.asked / replied                       # 触发工具审批卡片
question.v2.asked / replied / rejected              # 触发提问应答卡片
```

---

## 7. 透明代理（隧道）

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

## 8. opencode 常用透传端点

以下 opencode 1.18.16 原生端点均可经 `/api/tunnel/oc/...` 访问（均经实测）。完整契约以 opencode 官方文档为准。

### 会话

```http
# 创建会话（必须传 agent，否则 title generation 会 403）
POST /api/tunnel/oc/api/session
{ "agent": "coder", "model": { "providerID": "bailian", "id": "deepseek-v4-flash" } }
→ { "data": { "id": "ses_..." } }

# 发送 prompt（注意嵌套结构）
POST /api/tunnel/oc/api/session/{sessionID}/prompt
{ "prompt": { "text": "总结一下", "parts": [ { "type": "file", "mime": "application/pdf", "filename": "a.pdf", "url": "/workspace/tmp/a.pdf" } ] } }
→ { "data": { ... } }

# 切换模型（ModelRef 对象，不是字符串）
POST /api/tunnel/oc/api/session/{sessionID}/model
{ "model": { "providerID": "bailian", "id": "deepseek-v4-flash" } }

# 拉取消息
GET /api/tunnel/oc/api/session/{sessionID}/message
→ { "data": SessionMessage[], "cursor": ... }

# 重命名 / 删除会话（legacy 面）
PATCH  /api/tunnel/oc/session/{id}    { "title": "新标题" }
DELETE /api/tunnel/oc/session/{id}
```

**prompt body 的坑**：`prompt.text` 必填，多模态 `parts` 嵌套在 `prompt` 内。顶层传 `parts` → 400 `Missing key at ["prompt"]`。

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

## 9. 错误码汇总

| 状态码 | 场景 |
|---|---|
| 400 | 请求体校验失败、JSON 无效、frontmatter 缺字段、名称非法、skill zip 布局/大小不符、上传文件名为空 |
| 401 | JWT 缺失/无效/过期、登录凭证错误 |
| 403 | 命中代理黑名单（`global/dispose`、`auth/` 等） |
| 404 | 资源不存在（provider/skill/文件） |
| 405 | 代理端点收到不在白名单的 HTTP 方法 |
| 409 | 工作区端点在容器未创建时调用 |
| 413 | 上传超 10MB、预览超 2MB、skill zip 超限 |
| 422 | Pydantic 请求体校验失败（`detail` 为错误数组） |
| 500 | 容器/文件系统操作失败 |
| 503 | Agent 容器未运行（tunnel 端点）、无容器可重载 |
