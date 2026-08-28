# 内部知识库云端 Agent 作业平台 · 用户需求分析报告

| | |
|---|---|
| **文档目的** | 作为下一版本开发的用户需求分析报告，为产品迭代提供决策依据 |
| **分析对象** | Agent Docker Platform（四层架构：浏览器层 → 平台控制层 → 容器执行层 → 共享服务层） |
| **分析方法** | 全量源码走查（后端 7 个核心模块、前端 7 个核心文件、镜像与编排配置）+ opencode 1.18.16 官方 OpenAPI 契约对照（`opencode-api.json`，约 95 个路径）+ `docs/REQUIREMENTS.md`（R1–R16）需求基线比对 |
| **报告日期** | 2026-08-27 |

---

## 0. 执行摘要

平台当前的本质是 **"opencode headless serve 的多租户 Web 化宿主"**：每个用户一个加固 Docker 容器，容器内唯一的进程是 opencode 1.18.16，平台只负责容器生命周期管理、配置消毒注入、透明反向代理与工作区文件服务。这一架构决策使平台以极低的代码量获得了完整的 agent 能力面（凡是官方 Web 路径覆盖的核心循环均可用），是本工程最大的亮点。

但围绕"**内部知识库云端 agent 作业平台**"这一工程主题，当前实现存在三个层面的核心结论：

1. **主题缺口（最严重）**：代码库中不存在任何知识库能力——无文档接入、无 RAG/向量检索、无知识沉淀与共享机制（全库检索 `知识库/knowledge/向量/embedding/RAG` 零命中）。"知识库"目前仅以三种原始形态存在：SKILL.md 技能文档、工作区文件、SearXNG 联网搜索。主题五大核心词中，"知识库"基本缺位。
2. **功能覆盖断层（最可惜）**：与本地 opencode 相比，约 10 项服务端协议已支持的能力前端未接入（**diff 审查、revert 撤销、fork 会话、手动 compact、LSP 符号引用、todo 面板、上下文用量**等），其中 diff 审查还被前端 `SKIP_PART_TYPES` 刻意跳过 patch part——这是与本地 TUI 体验最大的单项差距，且补齐成本最低。
3. **工程债集中（最紧迫）**：同步 Docker SDK 调用阻塞事件循环（系统性，全部用户互相拖累）、backend 单实例承载全部内存态（SSE/LLM 流量单点）、安全基线薄弱（弱 JWT 密钥随仓库提交、LLM Proxy 无认证、代理黑名单可绕过）、数据零备份零落库（销毁容器即永久丢失）。

**最优先建议**（详见第 8 章）：P0 修复事件循环阻塞与两个正确性 bug；P1 补齐 diff 审查/撤销等协议级能力 + 建立知识库 MVP；P2 架构演进（LLM Proxy 可选化、消息落库、备份机制）。

---

## 1. 平台现状概览

```text
浏览器层    React SPA (Chat / ConfigPanel / AdminPanel / Login) + nginx :3000
平台控制层  FastAPI :9123 —— 容器生命周期 · 配置消毒 · 透明代理 · SSE 扇出 · LLM Proxy
容器执行层  每用户一个加固容器，唯一进程 opencode serve :4096（agent-net 隔离网络）
共享服务层  PostgreSQL 16 · SearXNG 元搜索（web_search MCP 后端）
```

核心设计原则（README L9-20）：**平台不实现任何 Agent 能力**。Agent loop、LLM 调用、工具执行、会话存储全部由 opencode 负责；平台只做生命周期管理、配置注入、透明反向代理、文件服务四件事。当 opencode 升级新增能力时，平台原则上不需要改代码。

当前已交付的能力面（摘要）：

| 域 | 已交付能力 |
|---|---|
| 多租户 | JWT 认证、注册/登录、admin 角色体系、每用户独立容器 + 双卷（workspace/data） |
| 生命周期 | 幂等创建、健康探测、崩溃自愈（Docker restart policy）、30 分钟空闲回收、镜像陈旧检测重建 |
| 对话 | 流式 SSE 渲染、多会话管理、模型/Agent 运行时切换、工具审批卡片、提问应答卡片、中断 |
| 知识相关（原始形态） | Skill 管理（全局/项目级 + zip 批量导入）、@文件引用、文件树浏览与在线预览、附件上传、内置 web_search MCP |
| 配置 | 两级配置 CRUD（Provider/MCP/Skill）+ Web UI、一键重载、LLM Proxy 统一回源 |
| 运维 | 管理面板（总览/容器列表/资源采样/日志/重启/停止/销毁） |

---

## 2. 消毒过程分析

### 2.1 消毒机制定位

"消毒"指平台在把宿主机 `opencode.json` 注入用户容器之前执行的安全化/适配化改写流水线。核心实现在 [opencode_config.py](../backend/app/services/opencode_config.py) 的纯函数 `build_container_config()`（L287-401），由 [container_manager.py](../backend/app/services/container_manager.py) 的 `inject_opencode_config()`（L160-202）调用，经 Docker `put_archive` API 以 tar 归档写入容器 `/data/config/opencode/opencode.json`（mode 0600，uid 1000）。容器侧由 [entrypoint.sh](../agent-image/entrypoint.sh) L26-29 兜底：检测配置缺失/为空时回落镜像内 `opencode.default.json`。

设计意图（模块 docstring L1-23）：平台自身不碰任何 LLM，唯一职责是把"消毒后的配置"正确送进每个容器。

### 2.2 十二步消毒流水线（按实际执行顺序）

| # | 步骤 | 实现位置 | 具体操作 |
|---|---|---|---|
| 0 | 加载宿主源配置 | opencode_config.py L128-143 | 读 `/host-opencode/opencode.json`，`utf-8-sig` 剥 BOM；文件缺失/解析失败均降级为空配置 + 日志，绝不让配置问题炸掉 API |
| 1 | 剥离宿主专属键 | L291-294 | `HOST_ONLY_KEYS = ("plugin", "builtin_mcp")` 被丢弃：用户 plugin 引用宿主路径/npm 包，只读容器内安装必失败；`builtin_mcp` 是平台内部开关表，不得泄入容器 |
| 2 | MCP 逐项过滤 | L299-306 | 仅保留 `type == "remote"`（URL 型）；`local`（command 型）引用宿主可执行文件，容器内不可用，全部剥离；过滤后为空则删除 `mcp` 键 |
| 3 | 内置 MCP 注入 | L313-315 + L171-245 | 扫描 `agent-image/builtin-mcp/*/manifest.json`（当前唯一实例 web_search，SearXNG 后端），`${SEARXNG_URL}` 环境占位符解析；与用户 remote MCP 同名时**内置版静默覆盖** |
| 4 | 内置插件注入 | L317-327 + L248-284 | 扫描 `builtin-plugins/` manifest，注入预烘焙进镜像的 `oh-my-opencode-slim`（完整 node_modules + ast-grep 二进制在构建期烘焙，运行时零安装零网络），路径指向只读镜像内，用户无法卸载或篡改 |
| 5 | 回环地址全树重写 | L329 + L79-100 | 递归遍历 dict/list/str，凡"长得像 URL"且 host 精确命中 `127.0.0.1/localhost/0.0.0.0/[::1]` 的字符串，重写为 `host.docker.internal`；API key 等非 URL 字符串不受影响 |
| 6 | 容器默认值叠加 | L331 + L58-76 | 深合并 `CONTAINER_DEFAULTS` 作为底层 base：`autoupdate:false`、`share:"disabled"`、全部 `permission` 设为 `allow`（含 web_search*）——理由：容器即沙箱，headless 服务无人应答 ask 提示 |
| 7 | 模型回退与 small_model | L333-346 | 无 `model` 时按 `sorted(provider) → sorted(models)` 确定性取第一个；`small_model` 缺失时强制等于主模型，绕过 opencode 廉价模型回退解析 bug（"Model unavailable" 会毒化 session runner） |
| 8 | Agent 模型强制改写 | L348-360 | 遍历所有 agent，**无条件** `agent_cfg["model"] = default_model`，防止宿主配置引用容器内不可用模型时 opencode 静默回落 OpenCode Zen（受限地区 403） |
| 9 | 剥离不兼容模型选项 | L365 + L146-168 | 删除每个 model 的 `options.thinking`——opencode 1.18.16 的 `@ai-sdk/openai-compatible` 会以 `thinking` 字段硬 400；宿主桌面配置普遍设置了它，需透明剥离 |
| 10 | LLM Proxy 改写 | L367-382 | 所有配置了 baseURL 的 provider，统一改写为 `http://backend:8000/llm-proxy/{provider_id}`：平台转发真实上游 + 归一化 SSE tool-call delta（部分网关续传块携带空 `id`/`name`，会让 ai-sdk 报流错误中断整个回复） |
| 11 | Provider 白名单 | L384-391 | 注入 `enabled_providers` = 用户配置的 provider 列表，限制 opencode 只用这些，防止静默路由到内置 provider 目录（wandb/nvidia/OpenCode Zen 等）吃 403；配套 entrypoint.sh L110 每次启动清理陈旧 models.json 缓存 |
| 12 | 结果日志 | L393-400 | 记录来源、provider 列表、被剥离键、最终 model、插件数；`describe_source()` 诊断面只回传名单不泄密钥 |

### 2.3 触发时机与执行频率

消毒的唯一出口是 `inject_opencode_config()`，每次调用从零重建（读盘一次、无缓存、无 mtime 增量判断），天然幂等。全部触发点：

| 触发点 | 代码位置 | 频率 |
|---|---|---|
| 新建容器后、首次 start 前 | container_manager.py L421-428 | 每用户首次使用 1 次 |
| 已停止容器重新 start 前 | L410-413 | 每次冷启动（含 30 分钟空闲回收后再启动） |
| 显式重载 `reload_config()` | L434-452，由 `POST /api/config/reload` 与 `POST /api/tunnel/config/reload` 触发 | 用户/管理员手动，随后容器重启 + SSE Pump 重建 |
| 管理面板容器重启 | L476-483（restart 前重注入，因为纯 `docker restart` 会永远保留创建时的旧配置） | 管理员手动 |

**不会触发的情形**：容器 running 时 `ensure_container` 直接短路返回（L394-396）——运行中容器不感知宿主配置变更，必须显式 reload/restart；镜像 digest 过期走删旧建新路径，照常注入。注入顺带执行 `_inject_global_skills()`（L201），且"skills 失败绝不阻塞关键配置注入"。

### 2.4 技术方法

- **纯函数流水线**：单入口 + 10 个私有帮助函数，无全局可变状态；`copy.deepcopy` 隔离宿主配置内存副本。
- **"发现"与"注入"分离**：`builtin_mcp_servers()` 同时服务配置构建与 `/api/config` 路由，两端一致。
- **传输方式**：单文件 tar 归档 `put_archive`，可穿透只读 rootfs（写入的是 /data 卷），对 created-not-started 容器也可用——保证 opencode 一开机配置就在位。
- **层层降级的错误处理**：源配置坏 → 空配置；manifest 坏 → 跳过；构建异常 → 返回 False；put_archive 遇 NotFound（首建卷空）→ 一次性容器 mkdir 后重试；全部失败 → 容器仍启动，entrypoint 回落默认配置。
- **配套容器加固**（container_manager.py L262-340）：`user 1000:1000`、`read_only` rootfs、`cap_drop ALL`、`no-new-privileges`、apparmor、tmpfs `/tmp`(256m) 与 `/home/agent`(512m)、`cpus 2.0 / memory 2g（禁 swap）/ pids 200`、仅 `agent-net` 网络不映射宿主端口、`restart unless-stopped`、带 BasicAuth 的 HEALTHCHECK（15s/5s/4 次/45s start-period）、BasicAuth 密码 `secrets.token_urlsafe(32)` 每容器随机。

### 2.5 消毒的边界与盲区

| # | 盲区 | 影响 |
|---|---|---|
| a | **消毒失败静默降级**：注入失败仅记日志返回 False，容器照常启动；回落配置 `"provider": {}` 且无 model → 得到 healthcheck 通过（健康检查与 provider 无关）但**无法对话的"僵尸健康"容器**，前端与启动流程完全不感知 | 高 |
| b | **项目级配置完全绕过消毒**：`/workspace/opencode.json` 只做 JSON 合法性 + 1MB 校验、零消毒（workspace.py L246-265），而 opencode 原生优先级中项目级叠加在全局之上 → 可重新声明 plugin/local MCP、覆盖 permission、为 provider 重设 baseURL **绕过 LLM Proxy**；配合 bash/edit 全放行与 /workspace 可写，理论上存在 agent 自我持久化攻击面（爆炸半径受强沙箱限制，主要限容器内与出站网络） | 高 |
| c | 运行中容器不自动刷新配置，依赖手动 reload | 中 |
| d | 宿主零 provider 时不设白名单，可能回落内置 provider 吃 403；`_first_model` 返回 None 时首条 prompt 无模型可跑 | 中 |
| e | `agents.*.model` 无条件覆盖，宿主为不同 agent 配置差异化模型（如 reviewer 用便宜模型）的意图被抹平 | 低 |
| f | 内置 MCP 与用户 remote MCP 同名时静默覆盖，无日志 | 低 |
| g | manifest `${VAR}` 解析经 `getattr(settings, ...)` 无白名单，理论上构成"manifest → 平台 Settings（含 secret_key）"读取通道（目录只读受控，风险低） | 低 |

---

## 3. 消毒原因说明

### 3.1 数据安全维度

- **凭据治理**：宿主 `opencode.json` 携带 LLM apiKey。消毒通过 LLM Proxy 改写把所有 LLM 流量收口到平台，API key 虽然仍注入容器（固有信任模型），但流量路径统一、可观测；同时剥离 `builtin_mcp` 等平台内部键，防止平台侧配置语义泄入租户容器。
- **防篡改**：内置 MCP/插件路径指向只读镜像内，用户无法卸载或篡改（opencode_config.py L308-312 注释明示这是设计目标）；代理黑名单（tunnel.py L43-49）拦截 `global/config`、`auth/`、`instance/dispose` 等路由，防止沙箱内用户改写注入的凭据或远程关闭服务。
- **需正视的缺口**：apiKey 本身未脱敏注入每个容器（全体用户共享同一组 key），且项目级配置可重设 baseURL 绕过统一出口——当前消毒保障的是"路径受控"，不是"凭据隔离"。

### 3.2 合规与供应链维度

- **供应链收窄**：剥离用户 plugin（避免只读容器内运行时 npm 安装引入任意代码）、剥离 local MCP（避免引用宿主可执行文件）、`autoupdate:false` 锁死 opencode 版本、构建期校验预烘焙插件产物完整性（Dockerfile L67-75 检查 dist/index.js 与 ast-grep 平台包）。所有进入容器的代码要么来自受控镜像，要么是 URL 型 remote MCP。
- **审计与可观测**：LLM 流量、容器事件（SSE Pump）全部经平台中转，为日志留存与合规审计提供了物理通道（当前尚未落库，见 6.2）。

### 3.3 系统性能与兼容性维度

消毒的相当一部分步骤是在**修复 opencode 1.18.16 与 OpenAI 兼容网关之间的兼容性缺陷**，不做消毒平台基本不可用：

| 消毒步骤 | 规避的故障 |
|---|---|
| LLM Proxy 改写 | 部分网关 tool-call 续传块携带空 `id`/`name` → ai-sdk 流解析硬错误，**整条回复中断** |
| 剥离 `options.thinking` | `@ai-sdk/openai-compatible` 对该字段硬 400 |
| `small_model` 显式设置 | opencode 廉价模型回退解析 bug → "Model unavailable" 毒化 session runner |
| 回环地址重写 | 宿主回环部署的 LLM 服务在容器网络内不可达 |
| models.json 缓存清理 | 陈旧模型目录缓存导致 403 区域限制错误 |
| Provider 白名单 | 静默路由到 OpenCode Zen 等内置 provider 吃 403 |

### 3.4 用户隐私与多租户隔离维度

- **租户间隔离**：`share:"disabled"` 关闭会话公开分享（防止租户数据外泄渠道）；permission 全放行仅因为容器本身即沙箱；每容器独立卷与独立随机 BasicAuth 密码。
- **隐私权衡**：统一 LLM 出口意味着**所有用户的代码与对话对平台运营方可见**——这是与"本地执行"的根本隐私差异，应在产品文档中向用户显式披露（当前 README 未作说明）。

### 3.5 必要性总结

消毒不是可选的安全装饰，而是平台的**可用性前提**（3.3 的兼容性修复占 6/12 步）+ **多租户安全底线**（3.1/3.4）+ **供应链合规**（3.2）三重必需。当前实现方向正确、工程质量高（幂等、降级、可诊断），主要短板在 2.5(b) 项目级配置旁路与失败可观测性。

---

## 4. 用户视角使用体验评估

### 4.1 核心操作流程与便捷性

从注册到完成一次对话的完整链路（最少 3 次主动操作）：

```text
注册/登录(1) → [容器自动预热，无需手动点启动] → 等待启动收敛(被动) →
点"+"创建会话(1) → 输入消息+Enter(1) → 流式接收 → (可选)审批/提问卡片交互
```

**便捷性亮点**：
- 容器自动预热（Chat.tsx L359-392）：挂载即自动 `startAgent`，用户永远不需要点"启动并等待"；
- 附着初始化 `Promise.allSettled` 并行拉取 5 类数据（runtime/providers/sessions/agents/skills）；
- 乐观 UI：发送即渲染用户气泡，204 后靠 SSE 流式追加，首字延迟接近 LLM 本身的首 token 延迟；
- 附件上传后自动追加 `@tmp/...` 引用，文件即达 agent 上下文。

**便捷性短板**：
- 冷启动黑盒：容器启动只有四段循环文案（创建中/启动中/预热中），无进度百分比、无预计耗时；镜像 HEALTHCHECK 45s start-period + 启动预算 120s，首次等待可能超过一分钟，且期间输入区完全不可用；
- 刷新页面后当前会话不恢复（`currentSession` 初始 null），必须手动重新点选；切换会话先清空再拉取，有短暂空白闪烁；
- 停止 Agent 会清空全部 UI 状态（sessions/turns/providers 全清零），重启后需完整重走等待与加载。

### 4.2 响应速度

| 环节 | 实现 | 评价 |
|---|---|---|
| 消息流式 | SSE 主通道（`/api/tunnel/events?lastEventId=`），nginx `proxy_buffering off` 正确配置 | 好 |
| SSE 丢事件兜底 | 生成期 2.5s 慢轮询 + "4s 内有 SSE 事件则轮询让位"的自适应 + 完成后 400ms 去抖重拉 | 设计用心，但意味着**最坏情况下消息出现延迟 2.5s** |
| 容器启动轮询 | 1s 间隔 | 合理 |
| @ 文件搜索 | 200ms 去抖 + 复用 opencode `/find/file` 索引 | 好 |
| 服务端代理开销 | 每个隧道请求 = 2 次同步 Docker API 调用 + 2 次 DB 查询（密码/状态无缓存）；Docker 调用阻塞事件循环 | **系统性瓶颈**，用户多时全体变慢 |
| LLM 链路 | 浏览器 → nginx → backend → 容器 → LLM Proxy → 上游，五跳；LLM Proxy 每请求新建 httpx client + 每请求读盘解析配置 | 每次调用多付一次 TLS 握手与磁盘 IO |

### 4.3 界面友好度

- **视觉一致性尚可**：chatStyles.ts 定义了 opencode light 风格调色板 token，工具调用块有状态色点（pending 灰/running 橙/completed 绿/error 红）+ 可折叠头部 + turn 底部 tokens/成本显示，信息密度合理。
- **实现方式落后**：纯内联样式（无 CSS 方案），导致无法写 `:hover/:focus` 伪类（TreeRow 需手工 onMouseEnter 模拟悬停）、无响应式布局（全库仅 1 条媒体查询）、移动端基本不可用（侧栏固定 300px、`100vh` 地址栏遮挡、Enter 发送在虚拟键盘上易误触）。
- **错误提示体系薄弱**：后端英文技术错误原文直出（如 "does not support media type"）；全局单一 error 字符串多错误互相覆盖、无级别分级；工具状态文案直接显示英文枚举（pending/running/streaming…）——整体处于**中英混杂**状态。
- **部分操作静默失败**：ConfigPanel 的 Provider/MCP/Skill 保存与删除多为裸 await 无 catch，失败时用户点保存毫无反馈（对比 ProjectConfigTab 有完整 try/catch）。

### 4.4 学习曲线与适应成本

首屏同时暴露 **7 个概念域**：容器生命周期（running/healthy/stopped…）、会话、Agent（primary/subagent 双层 + 条件性 @ 可见）、模型与 Provider、MCP、Skill、全局/项目 scope。侧栏还常驻"四层架构"面板与 runtime 自省信息（镜像名/被剥离的配置键）——这是开发者视角的架构说明，对普通用户是纯噪音。

对熟悉 opencode/编码助手的目标用户尚可接受；对普通内部用户（知识库场景的典型用户）学习曲线陡峭。降低适应成本的三个杠杆：概念收敛（首屏只留会话+输入框）、默认值优化（自动创建首个会话）、按角色分层暴露（普通用户隐藏运维细节）。

### 4.5 现有用户反馈主要痛点（按严重度排序）

| # | 痛点 | 依据 |
|---|---|---|
| 1 | Token 过期无 401 统一处理：`apiCall` 不识别 401，过期后用户面对一连串 HTTP 401 横幅，不被引导重新登录 | api.ts L260-276 |
| 2 | 冷启动黑盒等待（见 4.1） | Chat.tsx L165-172 |
| 3 | 登录空字段静默失败：空用户名/密码点登录无任何反应 | Login.tsx L13 |
| 4 | "重载配置"会重启容器且无确认、无后果提示，可能中断进行中的对话 | Chat.tsx L477-491 |
| 5 | 刷新后会话不恢复；停止 Agent 后状态全清 | Chat.tsx L87, L464-468 |
| 6 | SSE 断线不可见：onerror 仅 console.warn，网络抖动时用户只觉得"卡住" | Chat.tsx L349-352 |
| 7 | 配置面板部分保存静默失败（见 4.3） | ConfigPanel.tsx L270-479 |
| 8 | 权限审批卡片只有 action+resources，不展示工具入参，用户"盲批" | Chat.tsx L1445-1469 |
| 9 | 上传无进度无取消；大图片全量 base64 内联撑大请求体 | Chat.tsx L726-744 |
| 10 | 发送失败时乐观气泡残留（pending-user 不回收） | Chat.tsx L719-722 |
| 11 | 删除/重命名用原生 confirm/prompt，与整体 UI 割裂 | Chat.tsx L539/L554 |
| 12 | 会话列表无搜索/分页，一次拉全量，历史多后侧栏无限增长 | api.ts L351-354 |
| 13 | 非图片上传后输入框"莫名多出 `@tmp/xxx`" | Chat.tsx L757-761 |
| 14 | 无障碍几乎缺失：会话条目/菜单项是 div onClick，屏幕阅读器不可感知；模态无 Esc 关闭、无焦点陷阱 | Chat.tsx 多处 |
| 15 | 移动端不可用（无响应式） | 全局 |

### 4.6 满意度定性评价

工程正确性投入显著（SSE 双通道兜底、事件对账、异步启动状态机、注释详尽记录历史 bug），**对开发者用户的"能用"体验是成立的**；但产品化层面存在系统性短板：面向普通内部用户的"好用"尚未达成。核心矛盾：界面按"开发者 opencode Web 封装"设计，而工程主题要求的用户群是"内部知识库的使用者"。

---

## 5. 功能覆盖度分析

### 5.1 对比基准与方法

以仓库随附的 `opencode-api.json`（opencode 1.18.16 官方 OpenAPI 契约，约 95 个路径）作为"本地全能力面"的地面真相，与前端 `api.ts` 实际封装的端点集合做差集，并用前端全目录检索验证缺失端点确实无人调用；后端消毒代码、tunnel 黑名单、镜像定义提供"云端主动砍掉/新增了什么"的证据。

### 5.2 已覆盖能力（云端 = 本地核心循环）

会话完整 CRUD、流式消息（等价官方 Web UI 路径 `prompt_async`）、运行时模型切换、Agent 切换与多 agent 协作（预烘焙 oh-my-opencode-slim 提供 orchestrator 等 6 个 subagent）、Permission 审批（once/always/reject）、Question 结构化应答（单选/多选/自定义）、Skill 指定与 zip 批量导入、@文件引用、/ 斜杠命令（含插件注册的命令）、附件上传、文件树与在线预览、内置 web_search、interrupt 中断、容器内 git/rg/fd/python3 工具链。

### 5.3 本地具备、云端缺失或弱化的能力

**A 类：服务端协议已支持、前端未接入（补齐成本最低、价值最高）**

| 能力 | opencode 端点 | 云端现状 |
|---|---|---|
| **交互式 diff 审查** | `GET /session/{id}/diff` | 双重缺失：端点未调用 + messages.ts L211 `SKIP_PART_TYPES` 刻意不渲染 patch part。用户只能事后在文件树里看被改后的文件——**与本地 TUI 体验最大的单项差距** |
| **revert / undo 工作流** | `/session/{id}/revert`、`/unrevert`、`/revert/stage\|clear\|commit` | 未调用。改坏了只能靠 agent 写反向补丁或手动改 |
| fork 会话（分叉实验） | `/session/{id}/fork` | 未调用 |
| 手动 compact 上下文 | `POST /api/session/{id}/compact` | 未调用；仅自动压缩被被动显示 |
| LSP 符号引用 | `GET /find/symbol` | 未调用；@ 补全只用文件级，**符号级引用不可用** |
| todo 任务面板 | `/session/{id}/todo` | 未调用（TUI 任务可视化缺失） |
| 上下文/token 用量面板 | `/api/session/{id}/context` | 未调用 |
| 会话标题自动摘要 | `/session/{id}/summarize` | 未调用，改名靠手输 |
| shell 快捷执行 | `/session/{id}/shell` | 未调用 |

**B 类：被消毒管线/平台策略主动剥离（架构决定）**

| 被剥离能力 | 机制 | 影响 |
|---|---|---|
| 用户自定义 plugin | 消毒步骤 1 剥离 | 只能用平台预烘焙插件，无法扩展 |
| 本地（stdio/command）MCP | 消毒步骤 2 剥离 | 本地常用 MCP（文件系统、数据库等）云端全部失效，仅 URL 型可用 |
| BYOK（自带 API key 直连厂商） | `auth/` 路由被黑名单 + baseURL 全部改写 | 用户不能用自己的 key，全部走平台统一出口 |
| `options.thinking` 推理深度 | 消毒步骤 9 剥离 | 推理控制弱化（兼容性所迫） |
| 按 agent 差异化模型 | 消毒步骤 8 强制覆盖 | "title 生成用便宜模型"等优化被接管 |
| 会话分享 | `share:"disabled"` | 云端无任何分享渠道（本地可生成公开链接） |
| 自动更新 | `autoupdate:false` | 锁死 1.18.16（可理解为稳定性考量） |

**C 类：环境本质差异**

TUI 全部交互范式（13 个 `/tui/*` 端点、键盘快捷键效率流、终端原生渲染）在 headless serve 下不存在；宿主文件系统直访（云端仅 /workspace 卷）；本地 IDE/编辑器联动；隐私本地执行与离线使用（云端全部流量经平台，无法离线）。

### 5.4 云端新增、本地没有的能力

多用户与认证体系、管理员面板（总览/容器列表/资源采样/日志/操作）、Web 文件树与在线预览（HTML iframe 沙箱/Markdown/图片）、附件上传、Web 化两级配置管理、Skill zip 包管理、LLM Proxy SSE 归一化容错、SearXNG 聚合搜索内置、SSE Pump 多订阅者扇出（多标签页同看）、空闲回收与容器自愈运维、全套容器安全加固。

### 5.5 任务执行效率差异

| 维度 | 本地 opencode | 云端平台 | 差距评价 |
|---|---|---|---|
| 冷启动 | 毫秒级进程启动 | 容器冷启动，健康期 45s、预算 120s；30 分钟空闲后回收重付 | **最大效率税** |
| 请求链路 | opencode → 厂商，两跳 | 五跳（浏览器→nginx→backend→容器→proxy→上游） | 固有架构成本 |
| 流式可靠性 | TUI 直连事件流 | SSE 泵可能丢事件，2.5s 兜底轮询 | 最坏 2.5s 延迟 |
| 资源上限 | 整机 | 2 CPU / 2G / 200 pids | 大仓库全量扫描、并行 agent 受限 |
| 代理超时 | 无 | prompt 类 300s、其余 60s 硬超时 | 超长任务被掐断 |
| 交互范式 | 键盘驱动 | 鼠标驱动 | 高频操作点击成本高 |
| 文件访问 | 本地任意路径无限制 | 上传 ≤10MB、预览 ≤2MB | 两跳中转 |
| 模型切换敏捷性 | `auth login` 随时接新 provider | 白名单 + Proxy 限制 | 需平台侧配置 |

### 5.6 覆盖度结论

官方 Web 路径覆盖的核心对话循环已**完整可用**（约 40% 端点被封装）；约 10 项协议级能力未接入 UI（A 类）；约 7 项被策略性阉割（B 类，其中 BYOK 与本地 MCP 剥离对内部用户感知最强）。差距的 80% 不在架构，而在**前端未跟进协议面**——这决定了下一版本的高性价比改进路径。

---

## 6. 用户体验与工程缺陷分析

### 6.1 缺陷分级总览

**P0（影响正确性/全体用户）**

1. **同步 Docker SDK 调用阻塞事件循环**（系统性）：tunnel 调用链（每请求 2 次 Docker API）、workspace 全部文件操作、agent 日志接口均同步执行；Docker daemon 慢时（WSL2 下常见 50-200ms）卡住整个事件循环，**所有用户**的 SSE 心跳与 LLM 流同时停摆。admin 路由已示范正确的 `asyncio.to_thread` 用法，其余未跟进。
2. `get_status` None 解引用：后台启动早期 DB 行尚未 upsert 时访问 `record.last_error`（agent_controller.py L415-416）→ 前端启动轮询收到 500。
3. 代理黑名单可绕过：`lstrip("/")` 后做字符串前缀匹配（tunnel.py L109-111），`./global/config`、`foo/../global/config` 可穿透，httpx 规范化后命中被禁路由。

**P1（架构性/安全性）**

4. backend 单实例承载全部内存态：SSE bus、启动任务、阶段缓存全在进程内；backend 重启 = 全平台所有流式会话同时中断，且**无法水平扩展**（多副本时事件不互通）。
5. LLM Proxy 强制全量收口（见 3.1）：为修一个上游 bug 把数据面全压到控制面单点；且**无认证**——agent-net 上任意容器可借 backend 探测宿主 loopback（内网 SSRF 面）；每请求新建 client + 读盘解析配置。
6. 安全基线：弱 JWT 密钥随仓库提交（`.env` + 代码默认值）；24h 长 token 无撤销无 refresh；SSE token 走 URL query（日志泄露面）；容器 BasicAuth 密码明文入库与容器 env；backend 9123 端口直暴宿主；PG 弱凭据 + 端口暴露（15432）；登录无速率限制（可暴力破解）；首个注册用户自动成为 admin（公开部署抢占风险）；注册无密码强度约束；bcrypt 同步调用阻塞事件循环；隧道请求体无大小上限（内存 DoS 面）；CORS 危险默认 `["*"]`+credentials。
7. 数据安全：**销毁容器直接删卷无备份**（代码注释自认 "should backup volumes before calling this"）；消息/事件完全不落平台库（跨用户分析、审计、检索、容器重建后的历史恢复全部不可能）；workspace/data 卷**无磁盘配额**（单用户可写满宿主盘拖垮全体）；无 Alembic，schema 演进靠手写 ALTER TABLE。

**P2（体验/健壮性）**

8. SSE Pump：慢消费者静默丢事件（QueueFull 直接 pass，客户端不知道丢了数据）；后端重启后 seq 归零、id 空间重叠撞号；多行 `data:` 解析为覆盖而非拼接（对 opencode 单行 JSON 的隐性耦合）。
9. 自愈只做一半：`health_check_interval`/`max_restart_per_hour` 两个配置是死代码——无持续健康监控、无 crash-loop 熔断；容器崩溃但 pump 任务仍在时永久退避重试泄漏；`restart_count` 只计手动重启，指标失真；`idle` 状态在状态机中从未被写入。
10. 空闲回收判定过窄：只有非 GET 隧道请求更新 `last_activity`，用户长时间只浏览/SSE 静默监听会被误回收。
11. 消毒失败静默降级（见 2.5a）；注入结果不并入启动 API 响应与 DB 状态。
12. 前端体验缺陷群（见 4.5 全部 15 项）。

### 6.2 缺陷对用户体验的影响程度评估

| 缺陷群 | 用户体验影响 | 系统性能影响 | 修复紧迫度 |
|---|---|---|---|
| 事件循环阻塞（Docker 同步调用） | 用户增多后全体操作变慢、流式卡顿 | 高（线性恶化） | **P0** |
| 单实例内存态 | 后端发版 = 所有用户对话中断 | 高（不可水平扩展） | P1 |
| 数据零备份零落库 | 误删容器 = 工作成果永久丢失（用户最不可接受） | 中 | **P1** |
| 安全基线薄弱 | 感知低但风险敞口大（凭据/SSRF/暴力破解） | 低 | P1 |
| 前端体验缺陷群 | 高频感知（登录/错误/断线/会话恢复） | 低 | P1（低成本高感知） |
| 协议能力未接入 | 与本地相比"功能少一截"的直接来源 | 低 | **P1**（高性价比） |

---

## 7. 工程主题符合度评估

### 7.1 评估框架

将"内部知识库云端 agent 作业平台"拆解为五个核心词，逐项评估当前实现：

| 主题词 | 评估 | 符合度 |
|---|---|---|
| **内部**（多租户/内网部署） | JWT 多用户、admin 体系、每用户隔离容器、网络隔离——实现完整 | ★★★★☆ |
| **知识库** | **基本缺位**（详见 7.2） | ★☆☆☆☆ |
| **云端**（云端化执行） | 容器化云端执行、Web 访问、运维面板——实现完整；但单宿主机、单实例 backend 制约云端弹性 | ★★★☆☆ |
| **agent 作业** | 依托 opencode 能力面完整（见第 5 章）；但"作业"所需的调度/异步/批量维度缺失（详见 7.3） | ★★★☆☆ |
| **平台**（产品化） | 有认证/配置/管理面板骨架；产品化成熟度不足（见第 4/6 章） | ★★★☆☆ |

### 7.2 知识管理维度——主题最大缺口

全代码库检索 `知识库 / knowledge / 向量 / embedding / RAG / 检索增强` **零命中**（`kb`/`rag` 的命中均为 storage、keyboard 等无关子串）。当前"知识"仅以三种**原始、未成体系**的形态存在：

1. **Skill 文档**（SKILL.md）：本质是"操作规程"而非"知识资产"，且靠文本前缀注入（`请使用 skill: xxx`），无检索、无版本、无使用统计；
2. **工作区文件**：每用户私有，无跨用户共享、无统一入库、无全文检索；
3. **SearXNG 联网搜索**：面向公网，与"内部知识库"目标相反。

**缺失的知识库核心能力**：内部文档接入（上传/同步/爬取）、切分与索引（向量/全文）、检索增强问答（把检索结果注入 agent 上下文）、知识沉淀闭环（对话产出反哺知识库）、跨用户知识共享与权限、知识库管理界面（分类/标签/生命周期）。平台的 `@文件引用` + `find/file` 模糊搜索是现成的接入点，但目前只搜单用户工作区。

### 7.3 Agent 作业调度维度

已实现：每用户容器生命周期（启动/健康/自愈/空闲回收/销毁）、会话级交互式作业。
缺失的"作业平台"属性：

- **异步作业**：无后台任务队列——关闭浏览器即丢失执行中的作业（SSE 断开不影响容器内执行，但用户无任何恢复入口）；
- **批量作业**：无法一次向多个会话/多个 agent 下发任务（REQUIREMENTS.md R14 批量操作仅规划了容器运维批量，无作业批量）；
- **定时/计划作业**：无任何调度器；
- **作业状态可观测**：无作业列表、进度、历史、结果导出；
- **并行度管理**：容器 2 CPU/200 pids 限额下无并行作业编排。

### 7.4 云端协作维度

- 已有：多用户隔离、管理员运维协作。
- 缺失：用户间**任何**协作通道——share 被消毒强制禁用、会话不可转让、无团队/工作区共享、无评论/@ 提及、无知识协同编辑。作为"平台"（多用户产品）仅有"并行独享"而无"协作"，与内部平台应有的组织形态不匹配。

### 7.5 工程主题符合度总评

**当前实现 ≈ "opencode 多租户容器托管平台"**，与主题的前半句（内部、云端、agent 运行时）高度吻合（架构选型正确、工程质量扎实），与后半句（**知识库**、**作业**平台）存在实质性偏航：知识能力缺位、作业只有"会话"一种形态、协作为零。综合符合度约 **55–60%**。

### 7.6 与主题更紧密结合的改进方向

1. **知识库 MVP（最高优先）**：平台级知识库服务（文档上传/接入 → 切分索引 → 检索 API），以 MCP 或检索工具形式注入每个 agent 容器（复用现有 builtin-mcp 注入机制，工程路径现成）；前端增加知识库管理页与对话内 `@知识库` 引用。
2. **知识沉淀闭环**：会话产出（文档/代码/结论）一键入库；Skill 与知识库统一为"知识资产"管理。
3. **作业化**：会话作业与后台作业解耦（作业队列 + 作业列表/进度/历史），支持批量与定时下发。
4. **协作化**：工作区/知识库的团队共享与只读分享链接（重新评估 share 禁用策略：从"一刀切禁用"改为"管理员可控"）。
5. **概念收敛**：面向普通用户的知识问答界面（隐藏容器/Agent/Provider 概念），与开发者高级界面分层。

---

## 8. 下一版本需求建议汇总

### 8.1 需求优先级矩阵

**P0 — 正确性与性能止血（先做）**

| # | 需求 | 来源章节 |
|---|---|---|
| P0-1 | 全部 Docker SDK 调用移入 `asyncio.to_thread`；容器密码/状态加 TTL 缓存，消除每请求 2×Docker+2×DB | 6.1-1 |
| P0-2 | 修复 `get_status` None 解引用；代理黑名单改路径段规范化匹配 | 6.1-2/3 |
| P0-3 | 消毒/注入失败并入启动响应与 DB 状态（failed + error），消灭"僵尸健康"容器 | 2.5-a |
| P0-4 | 前端 401 统一拦截跳登录；登录空字段提示；ConfigPanel 保存统一错误反馈 | 4.5-1/3/7 |

**P1 — 用户价值与主题对齐（核心迭代）**

| # | 需求 | 来源章节 |
|---|---|---|
| P1-1 | **diff 审查 + revert/undo 工作流**（渲染 patch part + 接入 revert 端点）——单项体验提升最大 | 5.3-A |
| P1-2 | 接入 fork / 手动 compact / context 用量 / todo / summarize / LSP 符号引用 | 5.3-A |
| P1-3 | **知识库 MVP**：文档接入 + 索引 + 检索 MCP 注入 + `@知识库` 引用 + 知识管理页 | 7.6-1 |
| P1-4 | 刷新后恢复当前会话；冷启动进度反馈（阶段+耗时）；SSE 断线可见提示 | 4.5-2/5/6 |
| P1-5 | 安全基线：强密钥出库、SSE 短期 ticket、容器密码加密、下线 9123 直暴、登录限流、审批卡片展示工具入参 | 6.1-6 |
| P1-6 | destroy 前卷备份（tar.gz 导出）；消息/审计事件落库（至少审计级） | 6.1-7 |
| P1-7 | remote MCP 增强：支持平台代管的 MCP 网关，缓解 local MCP 剥离的扩展性损失 | 5.3-B |

**P2 — 架构演进与产品化**

| # | 需求 | 来源章节 |
|---|---|---|
| P2-1 | LLM Proxy 可选化（按 provider 开关）或独立为无状态多副本服务；连接池 + 配置缓存 | 6.1-5 |
| P2-2 | 作业平台化：后台作业队列、作业列表/进度/历史、批量与定时下发 | 7.3 |
| P2-3 | 协作：工作区/知识库团队共享、管理员可控的分享链接 | 7.4 |
| P2-4 | 持续健康监控 + crash-loop 熔断（激活两个死配置）；SSE 慢消费者显式断开；空闲回收计入 GET 活动 | 6.1-9/10 |
| P2-5 | 产品化：响应式布局、无障碍基线、错误文案中文化与分级 toast、概念分层（普通/高级界面）、项目级配置消毒或告警 | 4.3/4.4/2.5-b |
| P2-6 | 卷磁盘配额；Alembic 迁移；会话列表分页搜索 | 6.1-7/4.5-12 |

### 8.2 建议跟踪的关键指标

| 指标 | 现状基线 | 目标方向 |
|---|---|---|
| 冷启动到可对话耗时 | 45s+（无进度反馈） | 有进度反馈，P95 < 60s |
| 单实例并发用户流式会话 | 受事件循环阻塞制约 | 阻塞消除后实测并给出容量基线 |
| 与本地 opencode 的功能面差距 | 约 10 项协议能力未接入 | A 类全部补齐 |
| 知识库能力 | 0 | MVP：接入/索引/检索/引用闭环 |
| 数据丢失风险 | destroy 即永久丢失 | 100% destroy 前有备份 |
| 消息/审计留存 | 0（仅容器内） | 审计级全量落库 |

---

## 附录 A：证据索引（关键代码位置）

| 主题 | 文件:位置 |
|---|---|
| 消毒流水线 | backend/app/services/opencode_config.py:287-401（12 步）；HOST_ONLY_KEYS L47；CONTAINER_DEFAULTS L58-76 |
| 消毒触发点 | backend/app/services/container_manager.py:410-452, 476-483 |
| 配置注入与兜底 | container_manager.py:160-202；agent-image/entrypoint.sh:26-29 |
| 容器加固 | container_manager.py:262-340（user/read_only/cap_drop/资源限额/healthcheck） |
| 项目级配置不消毒 | backend/app/routers/workspace.py:246-265 |
| 代理黑名单（可绕过） | backend/app/routers/tunnel.py:43-49, 109-111 |
| 事件循环阻塞点 | tunnel.py:113 → agent_controller.py:406-407；workspace.py 各文件原语；agent.py:81（对照正确示范 admin.py:45,85,136-139） |
| get_status None 解引用 | backend/app/services/agent_controller.py:415-416 |
| SSE Pump 丢事件/seq 重置 | backend/app/services/sse_pump.py:33-53, 84-93, 149-159 |
| LLM Proxy 单点/无认证 | backend/app/routers/llm_proxy.py:31-33, 76-89, 82, 210；opencode_config.py:374-382 |
| destroy 无备份 | agent_controller.py:288（注释自认）；container_manager.py:508-531 |
| 前端 401/错误处理 | frontend/src/api.ts:260-276；Login.tsx:13, 26 |
| patch part 被跳过 | frontend/src/oc/messages.ts:211 |
| SSE 兜底轮询 | frontend/src/components/Chat.tsx:511-524 |
| 空闲回收 | agent_controller.py:590-618；config.py:71 |
| 需求基线 | docs/REQUIREMENTS.md（R1-R16，其中 R8.1 备份、R9-R12 监控告警、R14 批量操作尚未实现） |
| opencode 能力面地面真相 | opencode-api.json（1.18.16 官方 OpenAPI，约 95 路径） |
| 知识库能力缺失 | 全库检索 `知识库/knowledge/向量/embedding/RAG` 零命中（2026-08-27） |
