# Agent Docker Platform

分层的 AI Agent 平台——主链路 **Web 层 → 控制层（内含代理层）→ 容器层 → 共享服务层 / 知识库层**，横切 **数据层**，全链路真实运行，无 mock。

每个用户拥有独立的 Docker 容器，容器内唯一的进程是 [opencode](https://opencode.ai) `serve`（headless agent runtime）。平台不实现任何 agent 逻辑，只负责容器生命周期管理、配置注入和透明反向代理。

> 用户侧功能清单（普通用户 / 管理员）见 **[docs/USER_FEATURES.md](docs/USER_FEATURES.md)**，完整的接口契约见 **[docs/API.md](docs/API.md)**。

## 核心原则：平台不实现任何 Agent 能力

容器里唯一的进程是 **`opencode serve`**（opencode 1.18.16 官方 headless 服务）。Agent loop、LLM 调用、工具执行、会话与消息存储，全部由它负责。

平台只做四件事：

1. **生命周期管理** — 用 Docker SDK 为每个用户创建 / 启动 / 停止加固容器
2. **配置注入** — 把开发者本机的 `opencode.json` 消毒后写进容器的配置卷，支持全局级与项目级两级配置
3. **透明反向代理** — 把浏览器的请求按 opencode 的原生路由原样转发进容器
4. **工作区文件服务** — 附件上传（`tmp/` 隔离目录）、文件树浏览、文件预览与 `@` 引用

> 因此当 opencode 新增能力时，平台**不需要改代码**：前端直接调用 opencode 的新路由即可。

## 架构图

全局七层视图（① Web → ② 控制 → ③ 代理 → ④ 容器 → ⑤ 共享服务 / ⑥ 知识库，横切 ⑦ 数据层）：

```mermaid
flowchart TB
    subgraph WEB["① Web 层 · platform-net"]
        FE["React SPA + nginx :3000<br/>会话 UI · 三级配置面板 · 管理台"]
    end

    subgraph CTRL["② 控制层 · FastAPI backend（跨两张网络的唯一通道）"]
        CORE["认证授权 · 容器生命周期 · 三级配置消毒注入<br/>业务 / 审计 / 观测"]
        subgraph PROXY["③ 代理层 · 数据平面（同进程，不含 agent 逻辑）"]
            PX["Tunnel 透传 · SSE 扇出 · LLM 回源<br/>知识库白名单代理 · 模板库"]
        end
    end

    subgraph RT["④ 容器层 · agent-net（每用户一容器：非 root · 只读 rootfs · cap-drop ALL · 不映射宿主端口）"]
        OC["opencode serve :4096（容器内唯一进程）<br/>Agent loop · LLM 调用 · 工具执行 · 会话存储"]
        EXT["内置插件 / Skills / 工具 / MCP<br/>（只读镜像层，用户不可卸载）"]
    end

    subgraph SHARED["⑤ 共享服务层 · agent-net（无宿主端口）"]
        SS["SearXNG 元搜索 · demo-mcp 远程 MCP 样例"]
    end

    subgraph KB["⑥ 知识库层 · 宿主或外部服务"]
        FK["fastk / FastDB REST<br/>databases · search · chunk · image"]
    end

    UP["上游 LLM Provider · OpenAI-compatible"]

    subgraph DATA["⑦ 数据层"]
        PG[("PostgreSQL 16<br/>用户 / 容器台账 / 领域授权 / 审计 / UX 指标")]
        VOL["Docker Volumes<br/>工作区 · 容器数据 · PPTX 模板库"]
        DS["docker.sock · 宿主挂载配置目录"]
    end

    FE -->|"HTTP /api/* · JWT · SSE"| CORE
    FE -->|"直连代理面"| PX
    CORE -->|"Docker SDK：创建 / 启停 / 销毁 / recreate"| DS
    DS -->|"容器运行时"| RT
    CORE -->|"put_archive 注入消毒后的 opencode.json"| OC
    OC --- EXT
    PX -->|"透传 + SSE 单上游（Basic Auth）"| OC
    OC -->|"LLM 请求 · 知识库检索"| PX
    PX -->|"HTTPS 回源 + delta 归一化"| UP
    PX -->|"注入领域真实 Key · 仅只读"| FK
    EXT -->|"web_search / 远程 MCP"| SS
    CORE -->|"读写"| PG
    CORE -->|"模板库唯一写入方"| VOL
    OC -.->|"rw /workspace · /data；ro /library/pptx"| VOL
    CORE -.->|"宿主 opencode.json / skills"| DS
    PX -.->|"事件与指标旁路落库"| PG

    style WEB fill:#eef5ff,stroke:#4a7fd4
    style CTRL fill:#fff8ee,stroke:#d98c2b
    style PROXY fill:#ffe8d4,stroke:#d9752b
    style RT fill:#eefbf1,stroke:#3d9a5b
    style SHARED fill:#f4f0ff,stroke:#7a5cd0
    style KB fill:#fdeef4,stroke:#c94f86
    style DATA fill:#f2f4f7,stroke:#6b7a90
```

### 分层职责

| 层 | 部署形态 | 职责 | 明确不做 |
|---|---|---|---|
| ① Web 层 | `frontend` 容器（nginx，宿主 `:3000`） | SPA 路由、`/api` 反代、SSE 长连接、流式渲染与审批交互 | 不直连容器，不持有任何凭据 |
| ② 控制层 | `backend` 容器（FastAPI `:8000`，无宿主端口） | 认证授权、容器生命周期、三级配置消毒注入、业务与观测 | 不实现 agent loop、不解析模型输出语义 |
| ③ 代理层 | 与②同进程（数据平面） | Tunnel 透传、SSE 扇出、LLM 回源、知识库白名单代理、模板库 | 不改写业务语义（仅归一化 SSE delta、注入/剥离凭据） |
| ④ 容器层 | 每用户一个 `agent-{uid}` 容器 | `opencode serve` 全权负责 agent loop、工具执行、会话存储 | 不映射宿主端口、不持有真实上游 Key、不可写镜像层 |
| ⑤ 共享服务层 | `searxng` / `demo-mcp`（仅 agent-net） | 元搜索、远程 MCP 样例 | 不落用户数据 |
| ⑥ 知识库层 | 宿主 / 外部 fastk（FastDB REST） | 知识库检索、片段正文与附图 | 授权判定不在上游，由平台领域模型裁决 |
| ⑦ 数据层 | `postgres` 容器 + Docker 卷 + 宿主挂载目录 | 用户与台账、领域授权、审计与 UX 指标、工作区与模板资产 | 不存明文密钥（Fernet 加密） |

要点：

- 前端只与 backend 通信：nginx 把 `/api/*` 反代到 `backend:8000`；用户容器**不映射宿主端口**，只能经 `agent-net` 由 backend 访问
- backend 同时挂在 `platform-net`（接前端与 PostgreSQL）和 `agent-net`（接用户容器与共享服务）两张网络上，是两层之间唯一的通道
- 每用户两块独立卷：`agent-workspace-*`（工作区，含上传附件与项目级配置）与 `agent-data-*`（opencode XDG 状态）；模板库 `agent-pptx-lib` 全平台单副本，backend 唯一写入方、容器只读挂载
- 容器内的 LLM 请求统一经 backend 的 **LLM Proxy**（`/llm-proxy/{provider}`）回源真实上游——平台顺带归一化 SSE tool-call delta（部分网关的续传块携带空 `id`/`name`，会打断 `@ai-sdk/openai-compatible` 的流式解析）；上游地址实时读宿主配置，改配置无需重启后端
- 容器内的知识库检索统一经 backend 的 **KB Proxy**（`/fastk/api/*`）：容器只拿到不透明代理 token，真实领域 Key 仅在 backend 内存解密后注入，且只放行只读接口
- 观测为旁路：SSE Pump 与 LLM Proxy 把事件/指标喂给 MetricsCollector 落库，驱动管理员 UX 看板，不参与主链路决策

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
    B->>P: POST /api/tunnel/oc/session/{id}/prompt_async<br/>{parts:[text, file, agent…]}
    P->>C: raw bytes 透传 → 204 No Content
    C->>P: Agent loop → POST /llm-proxy/{provider}（API key 透传）
    P->>L: 回源真实上游（SSE tool-call delta 归一化）
    C-->>P: GET /event → message.part.delta / message.part.updated
    P-->>B: SSE 扇出 → 逐段流式渲染 / 工具卡片 / 审批卡片
```

## 功能特性

> 按角色展开的完整清单见 **[docs/USER_FEATURES.md](docs/USER_FEATURES.md)**。

### 容器生命周期
- 每用户独立 Docker 容器，非 root + cap-drop ALL + 只读根文件系统
- 双层健康检查（Dockerfile HEALTHCHECK + 平台探测）
- 崩溃自愈 + restart policy + `/workspace` 与 `/data` 卷持久化
- 空闲回收（默认 30 分钟无活动自动停止）；阶段化启动进度 `creating → starting → warming → running`

### 三级配置管理（CRUD + Web UI）

| | 全局级 `/api/config/*` | 项目级 `/api/workspace/*` | 用户级 `/api/user-config/*` |
|---|---|---|---|
| 存储位置 | 宿主机 `opencode.json` + `~/.config/opencode/skills/` | 容器卷 `/workspace/opencode.json` + `/workspace/.opencode/skills/` | 平台数据库（密钥加密存储） |
| 作用范围 | 所有用户容器共享 | 仅该用户本工作区 | 仅该用户容器 |
| 可编辑者 | **管理员** | 用户本人 | 用户本人 |
| 生效方式 | 注入时消毒（MCP 过滤 / 内置 MCP·插件注入 / LLM Proxy 改写 / 模型覆盖） | opencode 启动时原生合并 | 注入时与全局配置合并 |
| Skill 导入 | 单个 SKILL.md 编辑 | **zip 批量导入**（三种布局自适应，500 文件/单文件 5MB/总量 20MB 上限） | — |

- **LLM Provider** — 平台级（管理员）与个人级（用户）双层增删改查，支持 OpenAI-compatible / 自定义 baseURL，用户可选「激活 LLM」
- **MCP 服务** — Remote (URL) 与 Local (Command) 两种类型，含启用/禁用开关；平台内置 MCP（`web_search` 等）由**管理员总闸 + 用户个人开关**双级控制（只能收窄不能放开），运行时推送约 2 秒生效
- **Skills** — 全局 / 项目级增删改查 + zip 导入，直接编辑 `SKILL.md`（YAML frontmatter + Markdown）；管理员可控制内置 Skill 可见性
- **一键重载** — 将合并后的配置重新注入运行中的容器

### AI 对话（普通用户）
- 流式渲染（SSE）opencode 的实时输出，工具调用可折叠展开；**4 秒静默自动轮询兜底**，支持 `lastEventId` 断线重放
- 多会话管理：创建 / 重命名 / 删除 / 总结，切换会话自动恢复消息；消息级重新生成 / 分叉 / 回退（含文件改动恢复）
- **项目空间** — 新建 `projects/{名称}` 目录或绑定已有目录，会话按项目分组；删除项目只删会话、保留目录
- 运行时模型切换 + Agent 模式切换（build / plan 等 primary agents，自动过滤 subagent），支持**单条消息级覆盖**并设为会话默认
- 工具权限审批卡片（允许一次 / 总是允许 / 拒绝）与 Agent 提问应答卡片（单选 / 多选 / 自定义输入，一次多问），均由 SSE 事件驱动实时出现
- **`/` 斜杠命令**与 **`@` 引用**（工作区文件模糊搜索 + subagent），选中转为 FilePart 随 prompt 发送
- **快捷技能** — PPT 生成（模板 / 配色 / 版式配方三组单选，启用 `pptx-generator`）、流程图生成（plantuml，只交付 `.puml` 源码）
- **Skill 显式指定** — 输入框下拉多选（全局 + 项目级合并展示）
- **文件上传** — 图片 base64 内联，其它附件进入容器工作区 `tmp/` 隔离目录（≤10MB），上传后自动追加 `@tmp/…` 引用
- **工作区文件面板** — 树状浏览（剪除 `.git` / `node_modules` 等）、多选、zip 打包下载、批量删除；预览覆盖 HTML（iframe 沙箱）/ Markdown / 图片 / 文本代码 / **PPTX（`pptx-wasm` 浏览器端高保真渲染，缩略图导航 + 缩放）**；一键 `@ 引用`（PPTX 可引用当前页）
- **交付预览** — Agent 通过 `present_file` 主动交付产物时自动弹出标签页（按路径去重、关闭记忆、可全局开关）
- **知识库（fastk）** — 侧栏只读目录展示已授权库；容器内检索工具经平台白名单代理（凭据由平台注入，仅只读接口放行）；回复中的 `[[chunk:db/id]]` 渲染为引用徽章，弹窗展示片段正文与鉴权附图，撤销授权后同步 403
- **体验反馈** — 每条回复点赞 / 点踩（提交后锁定、幂等），点踩含 7 类原因码 + 补充文本（≤500 字）+ 上下文快照，数据汇入管理员 UX 看板

### 管理员能力（Admin）
- **仅 admin 角色可进入**（Chat 顶部「模板库」/「权限」/「管理」按钮）；后端每个端点都实时回读数据库角色，降权即时生效
- 管理员来源：**首个注册用户自动成为 admin**；或 `AGENT_ADMIN_USERNAMES` 环境变量（逗号分隔，登录时自动提升）

**容器管理**
- **平台总览** — 用户数 / 容器记录数 / Docker 运行中容器数 / 单容器资源限额
- **全用户容器列表** — 用户名 + 工号 / Docker 状态 / 台账状态 / 健康探测 / 重启次数 / 启动时间 / 最近活动；台账有记录但容器不存在时显式标注；支持搜索筛选与 5 秒自动刷新
- **实时资源采样**（可开关）— 各容器 CPU 占用率 + 内存用量/限额（并行 `docker stats` 采样）
- **容器操作** — 重启（保留数据卷，自动重建 SSE Pump）、**更新镜像**（recreate，保留卷）、停止、销毁（删容器 + 卷，需输入容器名二次确认）；支持**批量重启 / 停止 / 销毁**
- **日志查看** — 双 Tab 模态窗口：容器日志（tail 100–2000 行，mono 渲染自动滚底）+ **请求日志**（该用户经平台的 API 调用记录）
- **生命周期审计** — 容器创建/启动/停止/销毁操作轨迹
- 操作串行锁定 + toast 反馈

**用户体验看板**
- 粒度日/周/月/年（驱动默认窗口），按工号 / 模型厂商过滤
- **L0 用户 · L1 结果 · L2 效率 · L3 过程 · L4 满意度** 分层指标 + 趋势图
- 工具排行与调用明细下钻、LLM 上游 TTFT 与状态码分布、回合明细、反馈明细（原因码 / 补充文本 / 上下文快照）、历史回补

**PPTX 共享模板库**
- 单物理副本存于 `agent-pptx-lib` 卷，用户容器**只读挂载**，作为用户侧模板选择器数据源
- 导入入库（≤25MB，走规范化流水线并返回规范化报告）、种子重跑（按内容去重）、缩略图手动上传或浏览器端自动补齐
- 元数据编辑、**上架 / 下架**、删除、详情弹窗与统计卡

**知识库权限**
- 凭据录入 / 轮换 / 删除（连带撤销授权），接口永不返回密钥明文
- 用户授权双列表 + **用户 × 知识库点击矩阵**，授权即时生效于容器侧白名单代理

## 快速启动

### 前置条件

- [Docker Engine](https://docs.docker.com/engine/install/) 24.0+
- [Docker Compose](https://docs.docker.com/compose/install/) v2.20+
- 一个可从部署服务器访问的 [opencode](https://opencode.ai) 兼容 LLM Provider（如 OpenAI、阿里百炼、火山引擎等）；**没有有效 Provider 和模型时只能启动平台，发送对话不会产生回复**

### 方式 A：Linux / WSL 内首次部署

项目应运行在 Linux 或 WSL 的 ext4 文件系统。准备好 Docker Engine 后，在仓库根目录执行：

```bash
# 1. 必须先配置 Provider 与模型；示例中的占位值不能用于真实请求
cp config/opencode.json.example config/opencode.json
# 编辑 config/opencode.json，填入部署服务器可访问的 baseURL、有效 apiKey 与 model ID
# 若 Provider 部署在本机回环地址，容器会自动通过 host.docker.internal 回源

# 2. 构建 Agent 运行时镜像（仅首次，或 agent-image/ 改动后）
bash scripts/build-agent.sh

# 3. 构建平台前端与后端镜像（仅首次，或相应目录改动后）
bash scripts/build-backend.sh
bash scripts/build-frontend.sh

# 4. 启动 PostgreSQL、SearXNG、后端与前端；脚本会等待后端健康并运行检查
bash scripts/start.sh
```

浏览器打开 `http://<服务器IP或域名>:3000` → 注册 → 登录 → 启动 Agent → 确认「配置管理」中能看到 Provider 和模型 → 创建会话。**首个注册用户自动成为管理员**。若通过反向代理或其他域名访问，必须把该完整 Origin 加入 `AGENT_CORS_ORIGINS`，然后执行 `docker compose up -d`。

> `start.sh` 不会构建镜像。构建和启动已拆分，确保日常部署只重建变更的层。

### 代码更新后的部署

在仓库根目录，按改动范围执行对应构建，再启动服务：

```bash
# 仅 frontend/ 改动
bash scripts/build-frontend.sh
bash scripts/start.sh

# 仅 backend/ 改动
bash scripts/build-backend.sh
bash scripts/start.sh

# agent-image/ 改动
bash scripts/build-agent.sh
bash scripts/start.sh
# 已存在的用户 Agent 容器仍使用旧镜像；在管理面板销毁该用户容器后，
# 下一次启动会创建使用新镜像的容器。

# Compose、多个层或不确定影响范围
bash scripts/build-agent.sh
bash scripts/build-backend.sh
bash scripts/build-frontend.sh
bash scripts/start.sh

# 栈体检，可独立执行
bash scripts/verify.sh
```

配置改动的生效方式：修改 `backend/.env` 后运行 `bash scripts/start.sh`；修改 `config/opencode.json` 或在 UI 保存 Provider/MCP/Skill 后，使用 UI 的「重载配置」或调用 `POST /api/config/reload` 重启用户 Agent 并重新注入配置。

停止平台服务：`bash scripts/stop.sh`。该命令停止前端、后端、PostgreSQL 与 SearXNG，不会删除卷，也不会停止用户 Agent 容器。

### 方式 B：Windows 编辑 + WSL2 部署

> 不要在 `/mnt/d/...` 直接构建：跨文件系统构建较慢，且 NTFS 不保留 shell entrypoint 所需的可执行位。Windows 工作副本应同步到 WSL ext4 后运行。

在 Windows PowerShell 中执行（发行版、路径按实际环境调整）：

```powershell
# 1. 同步 Windows 工作副本到 WSL ~/agent-docker-demo，自动清理 CRLF 并修复可执行位
wsl -d Ubuntu-24.04 -- bash /mnt/d/Project/agent-docker-demo/scripts/wsl-sync.sh

# 2. 可选：保持 WSL VM 常驻，避免空闲关机带走 Docker 容器
wscript.exe D:\Project\agent-docker-demo\scripts\wsl-keepalive.vbs

# 3. 在 WSL 中首次构建并启动
wsl -d Ubuntu-24.04 -- bash -lc 'cd ~/agent-docker-demo && bash scripts/build-agent.sh && bash scripts/build-backend.sh && bash scripts/build-frontend.sh && bash scripts/start.sh'
```

日常迭代：先重复同步命令，再按改动范围调用上节的 `build-*.sh` 与 `start.sh`。Windows 浏览器访问 **http://localhost:3000**。

### 部署后无响应排查

按顺序执行以下命令；每一步都必须成功，不能只看前端端口是否返回 200：

```bash
# 1. 查看服务状态与后端日志
docker compose ps
docker compose logs --tail=100 backend frontend

# 2. 检查平台健康与前端代理（在部署服务器执行）
curl -i http://127.0.0.1:9123/api/health
curl -i http://127.0.0.1:3000/api/health

# 3. 确认 Agent 镜像存在、网络存在、用户 Agent 已运行
docker image inspect "$(grep '^AGENT_AGENT_IMAGE=' backend/.env | cut -d= -f2)"
docker network inspect agent-net
docker ps --filter 'name=agent-'

# 4. 检查 Agent 日志；重点看 provider/model、401/403、DNS、timeout、Model unavailable
docker logs --tail=200 <agent-container-name>

# 5. 配置 UI 中必须能看到至少一个 Provider 和模型；修改配置后重载容器
#    或执行 UI 的「重载配置」，再重新发送一条短消息
```

常见原因：Provider 的 `baseURL` 或 API key 无效、部署服务器无法访问上游、模型 ID 不存在、`AGENT_CORS_ORIGINS` 未包含浏览器 Origin、Agent 镜像未构建或 backend 无法连接 Docker socket。**请求返回 401/403/404/502 时不要当作“无响应”，先查看浏览器 Network 和上述日志中的实际状态码。**

### 常见问题（WSL）

| 症状 | 原因 | 解决 |
|---|---|---|
| WSL 内 curl 正常，Windows 浏览器打不开 | WSL VM 空闲自动关机（默认 ~60s），容器全部停止 | 运行 `wsl-keepalive.vbs`；或在 `%USERPROFILE%\.wslconfig` 设 `vmIdleTimeout=-1` |
| 首次构建 agent 镜像 DNS 解析/超时失败 | WSL2 IPv6 DNS 问题 | `docker build --network=host -t agent-demo:1.4.0 ./agent-image`（`build-agent.sh` 已内置自动重试） |
| `bash: \r: command not found` | 脚本带 Windows CRLF 行尾 | 走 `wsl-sync.sh` 同步（自动转换）；仓库已加 `.gitattributes` 强制 LF |
| 改了前端代码但页面没变化 | 前端镜像未重建 | `bash scripts/build-frontend.sh && bash scripts/start.sh` |
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

1. **剥离宿主 plugin** — 用户自己声明的 `plugin` 条目被丢弃（引用宿主路径 / npm 包，在只读容器内安装会失败）
2. **MCP 过滤** — 保留 `remote` 类型（URL 可达），丢弃 `local` 类型（命令依赖宿主机可执行文件，容器内不可用）
3. **内置 MCP 注入** — 自动发现 `agent-image/builtin-mcp/` 下的 manifest 声明，注入内置 MCP server（如 `web_search`），通过 SearXNG 元搜索引擎提供 web 搜索能力；SearXNG 地址由 `AGENT_SEARXNG_URL` 环境变量统一指定
4. **内置插件注入** — 自动发现 `agent-image/builtin-plugins/` 下的 manifest 声明，把预烘焙进镜像的插件（`oh-my-opencode-slim`，构建时已装好完整 node_modules 依赖树与 ast-grep 原生二进制）以路径形式注入 `plugin` 数组；插件树位于只读镜像内，用户无法卸载或篡改
5. **回环地址重写** — `http://127.0.0.1:8787/v1` → `http://host.docker.internal:8787/v1`
6. **LLM Proxy 改写** — 所有 provider 的 `options.baseURL` 统一改写为 `{AGENT_LLM_PROXY_BASE}/{provider_id}`（默认 `http://backend:8000/llm-proxy`）：平台转发到真实上游的同时归一化 SSE tool-call delta——部分网关在续传块里发空 `id`/`name`，会让 `@ai-sdk/openai-compatible` 报 `tool call delta is missing id or name` 中断整个流。上游地址在转发时实时读宿主配置，改配置无需重启后端
7. **模型覆盖与清理** — 强制 `agents.*.model` 指向默认 model、显式设置 `small_model`（规避 opencode 廉价模型回退解析 bug）、剥离 `@ai-sdk/openai-compatible` 不接受的模型级 `options.thinking`，防止 opencode 回退到内置 provider 或硬 400
8. **Provider 白名单** — 添加 `enabled_providers`，限制 opencode 只使用用户配置的 provider
9. **叠加默认值** — `autoupdate:false`、`share:disabled`、所有 `permission` 设为 `allow`（含 `web_search*`）

配置通过 `container.put_archive()` 在容器创建后、启动前写入 `/data/config/opencode/opencode.json`。

> 项目级配置（`/workspace/opencode.json`）**不做消毒**——它位于容器卷内，由 opencode 启动时按原生规则与全局配置合并。

### 环境变量

所有 `AGENT_*` 变量集中在 [backend/.env](backend/.env)（**随仓库提交，含演示默认值**，经 docker-compose `env_file` 注入后端容器）。克隆后无需任何配置即可启动；本地修改（连接外部数据库、换密钥等）后执行 `docker compose up -d` 生效，无需重建镜像。注意：**不要把真实凭据提交到仓库**。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_SECRET_KEY` | `change-this-in-production` | JWT 签名密钥，**生产环境必须修改** |
| `AGENT_DATABASE_URL` | `postgresql+asyncpg://agent:agentpass@postgres:5432/agent_demo` | 数据库连接串（栈内 postgres 服务；改回 `sqlite+aiosqlite:////app/data/agent_demo.db` 可切回 SQLite，历史数据保留在 `backend-data` 卷；指向外部 PG 时主机名用 `host.docker.internal` 或 IP） |
| `AGENT_AGENT_IMAGE` | `agent-demo:1.4.0` | Agent 容器镜像名 |
| `AGENT_AGENT_NETWORK` | `agent-net` | Agent 容器网络名 |
| `AGENT_AGENT_PORT` | `4096` | opencode serve 端口 |
| `AGENT_AGENT_WORKDIR` | `/workspace` | 容器内工作目录 |
| `AGENT_OPENCODE_CONFIG_SOURCE` | `/host-opencode/opencode.json` | 项目内置 opencode 配置路径（`config/` 目录挂载进 backend） |
| `AGENT_CONTAINER_HOST_ALIAS` | `host.docker.internal` | 宿主机别名 |
| `AGENT_LLM_PROXY_BASE` | `http://backend:8000/llm-proxy` | 容器内 provider 的统一回源地址：消毒时所有 provider 的 baseURL 被改写为 `{base}/{provider_id}`，经平台 LLM 代理转发并归一化 SSE tool-call delta（.env 未覆盖，用代码默认值） |
| `AGENT_SEARXNG_URL` | `http://searxng:8080` | 内置 web_search MCP 使用的 SearXNG 实例地址 |
| `AGENT_CONTAINER_CPU_LIMIT` | `2.0` | 容器 CPU 限额 |
| `AGENT_CONTAINER_MEMORY_LIMIT` | `2g` | 容器内存限额 |
| `AGENT_CONTAINER_PIDS_LIMIT` | `200` | 容器进程数限额 |
| `AGENT_IDLE_THRESHOLD` | `1800` | 空闲回收阈值（秒，默认 30 分钟） |
| `AGENT_ADMIN_USERNAMES` | `admin` | 管理员用户名列表（逗号分隔，登录时自动提升）；首个注册用户也自动成为 admin |
| `AGENT_CORS_ORIGINS` | `["http://localhost:3000","http://localhost:5173"]` | CORS 允许来源 |

## API 端点概览

> 完整的请求/响应结构、错误码与示例见 **[docs/API.md](docs/API.md)**。

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（用户名 + 密码，可选工号 `uid`，缺省自动分配 10001+），注册即返回 JWT；首个用户自动成为 admin |
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
| GET | `/api/admin/request-logs` | 平台请求日志（按用户/时间过滤） |
| POST | `/api/admin/containers/{user_id}/restart` | 重启容器（等待健康探测通过后重建 SSE Pump） |
| POST | `/api/admin/containers/{user_id}/recreate` | 用最新镜像重建容器（保留数据卷） |
| POST | `/api/admin/containers/{user_id}/stop` | 停止指定用户的容器 |
| POST | `/api/admin/containers/{user_id}/destroy` | 销毁容器 + 数据卷（台账记录保留，状态置 destroyed） |
| GET | `/api/admin/audit` | 容器生命周期审计轨迹 |
| GET | `/api/admin/ux/*` | 用户体验看板：`overview` / `trends` / `user-activity` / `tools` / `tool-calls` / `llm` / `rounds` / `feedback`（+ `backfill` 历史回补） |
| GET/POST | `/api/admin/kb-keys`、`/api/admin/kb-grants` | 知识库凭据录入/轮换/删除、用户授权/撤销 |
| GET | `/api/admin/kb-users`、`/api/admin/kb-user-access` | 授权用户列表、用户 × 知识库矩阵 |
| GET/POST | `/api/admin/library/*` | PPTX 模板库：统计 / 列表 / 导入入库 / 元数据与上下架 / 缩略图 / 删除 / 种子重跑 |

### 透明代理

| 方法 | 路径 | 说明 |
|---|---|---|
| ANY | `/api/tunnel/oc/{path}` | **透明代理到 opencode 的任意路由**（raw bytes 透传） |
| GET | `/api/tunnel/providers` | 由容器 `/config` 展平的 provider/model 列表 |
| POST | `/api/tunnel/config/reload` | 重新注入宿主配置并重启容器 |
| GET | `/api/tunnel/events` | SSE：opencode 事件扇出（支持 `lastEventId` 重放） |

> 容器内的 LLM 请求**不走隧道**：provider 的 baseURL 已被消毒改写为 `http://backend:8000/llm-proxy/{provider}`，由 backend 在 `agent-net` 上直接反向代理到真实上游（API key 原样透传，SSE tool-call delta 归一化）。

### 全局配置管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 总览（providers + mcp + skills，API key 脱敏） |
| GET/POST/DELETE | `/api/config/providers/{id}` | Provider 增删改查 |
| GET/POST/PATCH/DELETE | `/api/config/mcp/{name}` | MCP Server 增删改查 + 启停 |
| GET/POST/DELETE | `/api/config/skills/{name}` | 全局 Skill 增删改查（SKILL.md） |
| POST | `/api/config/reload` | 将宿主配置重新注入运行中的容器 |

> 读接口对所有登录用户开放，但**全局 MCP 列表仅 admin 可见**；MCP 写入/启停/删除与内置 Skill 可见性为 `require_admin`。前端只给管理员渲染全局编辑入口。

### 工作区（项目级配置 + 文件服务）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/api/workspace/config` | 项目级 opencode.json 读取（缺省自动创建骨架）/ 保存 |
| GET | `/api/workspace/skills/all` | 全局 + 项目级 skills 合并列表（供输入框下拉） |
| GET/POST/DELETE | `/api/workspace/skills/{name}` | 项目级 Skill 增删改查 |
| POST | `/api/workspace/skills/import` | **压缩包批量导入**项目级 skills（zip/rar/7z/tar.gz 等） |
| POST | `/api/workspace/files/upload` | 聊天附件上传（`tmp/` 目录，≤10MB） |
| GET | `/api/workspace/files` | 工作区文件树（扁平列表，剪除 .git/node_modules 等） |
| GET | `/api/workspace/file-content?path=` | 单文件预览读取（text/image/pptx 大纲，≤2MB） |
| GET | `/api/workspace/file-raw?path=` | 原始字节读取（PPTX 高保真渲染等） |
| POST | `/api/workspace/files/download` | 多文件 zip 打包下载 |
| POST | `/api/workspace/files/delete` | 批量删除 |

代理黑名单：`global/dispose`、`instance/dispose`、`global/upgrade`、`global/config`、`auth/` — 防止沙箱内的用户改写注入的凭据或关掉服务。

### 用户级配置（仅本人，密钥加密存储）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/DELETE | `/api/user-config/llm/{id}` | 个人 LLM Provider / 模型 CRUD（响应脱敏） |
| PUT | `/api/user-config/active-llm` | 选择激活的 LLM |
| GET/POST/DELETE | `/api/user-config/mcp/{name}` | 个人 MCP Server CRUD + 启停 |
| GET/PUT | `/api/user-config/builtin-mcp` | 平台内置 MCP 的个人开关（只能收窄，管理员全局停用时不可开启） |

### 项目空间

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/projects` | 项目列表（会话按项目目录分组） |
| POST | `/api/projects` | 新建（`mode=create`，创建 `projects/{名称}`）或绑定已有目录（`mode=bind`） |
| DELETE | `/api/projects/{id}` | 删除项目（删会话记录，保留目录文件） |

### 体验反馈

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/feedback` | 提交点赞/点踩（`verdict` ∈ up/down，按 message_id 幂等锁定；点踩含 7 类原因码 + 补充文本 ≤500 字 + 上下文快照 ≤60000 字符；限流 30 次/60s） |
| GET | `/api/feedback/session/{session_id}` | 某会话下当前用户已提交的反馈（回填锁定态） |

### 知识库（fastk）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kb/my-databases` | 当前用户被授权的知识库列表 |
| GET | `/api/kb/my-catalog` | 只读目录 + 描述（后端不可达时软降级） |
| GET | `/api/fastk/chunk` | 引用徽章 `[[chunk:db/id]]` 的片段正文（撤销授权后 403） |
| GET | `/api/fastk/chunk-image` | 片段附图的鉴权中继 |
| ANY | `/fastk/api/{path}` | **容器侧白名单代理**：剥离调用方凭据、按 `kb_grants` 过滤目标库并注入真实密钥，仅放行只读接口 |

### PPTX 模板库（用户侧只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/library/templates`、`/api/library/styles` | 已上架模板与样式列表（样例库受 `pptx_library_allow_samples` 门控） |
| GET | `/api/library/{id}`、`/file`、`/thumb` | 模板详情、文件与缩略图（容器只读挂载同一物理副本） |

## 项目结构

核心目录与职责如下：

```
agent-docker-demo/
├── docker-compose.yml                 # 全栈编排（frontend + backend + 双网络）
├── .env.example                       # 环境变量模板
├── agent-image/                       # 容器执行层
│   ├── Dockerfile                     # 两阶段：取 opencode 二进制 + 预烘焙插件 → 加固运行时
│   ├── entrypoint.sh                  # 准备 XDG 目录 → 链接预烘焙 SDK → 种子插件配置 → exec opencode serve
│   ├── opencode.default.json          # 无凭据回落配置
│   ├── builtin-mcp/                   # 内置 MCP server（web_search，SearXNG 后端）
│   ├── builtin-plugins/               # 内置插件 manifest（oh-my-opencode-slim，依赖树预烘焙于镜像）
│   └── builtin-tools/packages.list    # 额外 CLI 工具清单（构建时 apt 安装）
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
│       │   ├── auth.py                # 注册 / 登录（限流 + 管理员自动提升）
│       │   ├── agent.py               # 容器生命周期 + /runtime 自省
│       │   ├── admin.py               # 管理员 API：总览 / 容器列表 / 日志 / 请求日志 / 重启 / 更新镜像 / 停止 / 销毁 / 审计
│       │   ├── admin_ux.py            # 用户体验看板：L0–L4 指标 / 趋势 / 工具 / LLM / 回合 / 反馈 / 回补
│       │   ├── projects.py            # 项目空间：新建 / 绑定已有目录 / 删除
│       │   ├── feedback.py            # 点赞点踩采集（原因码 + 上下文快照）
│       │   ├── user_config.py         # 用户级配置：个人 LLM / MCP / 内置 MCP 开关
│       │   ├── config.py              # 全局配置：Provider/MCP/Skill CRUD（写入受角色门控）
│       │   ├── workspace.py           # 项目级配置 + Skill zip 导入 + 文件上传/树/预览/打包下载/删除
│       │   ├── library.py             # PPTX 模板库：用户只读 + 管理员入库/上下架/缩略图
│       │   ├── kb_keys.py             # 知识库凭据与授权（管理员）+ 用户可见目录
│       │   ├── kb_proxy.py            # /fastk/api 容器侧白名单代理（只读 + 凭据注入）
│       │   ├── fastk.py               # 引用徽章内容查询与附图鉴权中继
│       │   ├── tunnel.py              # 透明反向代理 + SSE + /providers
│       │   └── llm_proxy.py           # OpenAI 兼容 LLM 反向代理（SSE tool-call delta 归一化）
│       └── services/
│           ├── container_manager.py   # Docker SDK、加固参数、配置注入、卷读写
│           ├── agent_controller.py    # 状态机、健康探测、崩溃恢复、空闲回收
│           ├── opencode_config.py     # 宿主配置消毒 / 内置注入 / LLM Proxy 改写 / 默认值
│           ├── host_config.py         # 宿主 Provider/MCP/Skill CRUD 操作
│           ├── tunnel_relay.py        # 到容器的 HTTP 调用（raw bytes 透传）
│           └── sse_pump.py            # 单上游连接 + 环形缓冲 + 扇出
├── frontend/                          # 浏览器层 (React SPA)
│   ├── Dockerfile                     # Vite 构建 → nginx 部署
│   ├── nginx.conf                     # SPA 路由 + API 代理 + SSE 支持
│   └── src/
│       ├── App.tsx                    # 页面路由与角色门控（chat / admin / library / kbaccess）
│       ├── api.ts                     # 平台调用（含 admin API）+ /tunnel/oc 直通 opencode
│       ├── oc/messages.ts             # SessionMessage 归一化 + SSE 归约器
│       ├── oc/feedback.ts             # 反馈上下文快照采集
│       └── components/
│           ├── Chat.tsx               # 会话、流式渲染、@引用、项目空间、文件面板/预览、上传、快捷技能
│           ├── ChunkRef.tsx           # 知识库引用徽章 [[chunk:db/id]] 弹窗
│           ├── FeedbackBar.tsx        # 点赞/点踩条（锁定态回填）
│           ├── FeedbackModal.tsx      # 点踩原因码 + 补充说明
│           ├── AdminPanel.tsx         # 管理员面板：容器管理 + 用户体验看板
│           ├── UxDashboard.tsx        # L0–L4 指标、趋势图与明细下钻
│           ├── PptxLibrary.tsx        # PPTX 模板库管理（入库/上下架/缩略图）
│           ├── KbAccessAdmin.tsx      # 知识库凭据与用户授权矩阵
│           ├── ConfigPanel.tsx        # 全局/项目级/用户级 Provider/MCP/Skill 配置 UI
│           ├── Login.tsx              # 登录/注册
│           ├── chatStyles.ts          # 内联样式
│           └── adminStyles.ts         # 管理面板内联样式
├── docs/
│   ├── USER_FEATURES.md               # 用户侧功能特性（普通用户 / 管理员）
│   ├── REQUIREMENTS.md                # 需求分解（R1-R16）
│   └── API.md                         # API 完整文档
└── scripts/
    ├── build-agent.sh                 # 构建 Agent 运行时镜像
    ├── build-backend.sh               # 构建后端镜像
    ├── build-frontend.sh              # 构建前端镜像
    ├── start.sh                       # 启动平台服务并执行健康检查
    ├── stop.sh                        # 停止平台服务（保留卷与用户 Agent）
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

版本 1.18.16，路径参数为 `{sessionID}`。前端通过隧道调用以下核心接口：

```
POST /api/session                       { agent?, model?, location?: { directory } }
                                        -> { data: { id: "ses_..." } }
POST /session/{sessionID}/prompt_async  { parts: [{ type: "text", text: "..." }, ...] }
                                        -> 204；回复通过 /event SSE 增量返回
POST /api/session/{sessionID}/model     { "model": { "providerID": "x", "id": "y" } }  ← ModelRef 对象
GET  /session/{sessionID}/message       -> [{ info: Message, parts: Part[] }]（历史消息）
GET  /event                             全局 SSE；assistant 增量为 message.part.delta
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

历史消息采用 V1 结构：`[{ info: Message, parts: Part[] }]`。`info.role` 标识 `user` / `assistant`，正文和 reasoning 在 `parts[]` 中，tool part 的状态在 `part.state` 中。

SSE 事件由平台 `/api/tunnel/events` 转发，`/event` 的业务 payload 位于 `properties`：

```
message.updated          # 全量替换 info
message.part.delta       # 增量：按 partID 追加 delta
message.part.updated     # 全量替换 part
message.part.removed / message.removed
session.created / session.updated / session.status / session.idle
permission.* / question.*
```

前端收到 `prompt_async` 的 204 后，依赖 `message.part.delta` 即时更新 assistant 气泡；消息完成或断线时再通过 legacy message 接口拉取完整历史进行校准。

## 技术栈

| 层 | 技术 |
|---|---|
| ① Web 层 | React 18 + TypeScript + Vite 5 + nginx |
| ② 控制层 / ③ 代理层 | Python 3.12 + FastAPI + uvicorn + SQLAlchemy + httpx + Docker SDK |
| ④ 容器层 | Debian + opencode 1.18.16 (Node.js runtime) |
| ⑤ 共享服务层 | SearXNG 元搜索（web_search MCP 后端）· demo-mcp（远程 MCP 样例） |
| ⑥ 知识库层 | fastk / FastDB REST（宿主或外部部署，经平台白名单代理访问） |
| ⑦ 数据层 | PostgreSQL 16（默认，可切回 SQLite）· Docker Volumes · 宿主挂载目录 |

## 生产部署建议

1. **修改密钥** — `AGENT_SECRET_KEY` 与 postgres 的 `POSTGRES_PASSWORD`/连接串必须改为强随机值
2. **数据库已使用 PostgreSQL** — 栈内自带 postgres:16-alpine 服务；如需外部数据库，修改 `backend/.env` 的 `AGENT_DATABASE_URL` 后重新执行 `bash scripts/start.sh`
3. **Docker API 代理** — 后端挂载了 Docker socket，生产环境应替换为 Docker API 代理或远程 Docker daemon
4. **HTTPS** — 在 nginx 前加 TLS 终端（如 Caddy / Traefik）
5. **资源限制** — 根据实际负载调整 `AGENT_CONTAINER_CPU_LIMIT`、`AGENT_CONTAINER_MEMORY_LIMIT`、`AGENT_CONTAINER_PIDS_LIMIT`
6. **定期备份** — 备份 `~/.config/opencode/` 目录、数据库与各用户 workspace 卷

## License

MIT
