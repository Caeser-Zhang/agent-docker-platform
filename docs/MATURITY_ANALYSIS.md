# 项目成熟度分析：六维评估与优化路线

> 基准：当前代码库（2026-09）。评估方法：全量代码走查 + 需求文档（REQUIREMENTS.md R1–R16）逐条核对。
> 结论先行：**工程底座扎实（容器生命周期、安全加固、配置消毒、防御性设计），但六个维度中「可观测」「可评测」「可训练」基本空白，「可运行」「可调试」有局部短板，「可规模化迭代」存在结构性债务。**

## 总览评分

| 维度 | 现状评分 | 一句话结论 |
|---|---|---|
| 可运行 | ★★★☆☆ | 分层脚本体系完整、自愈能力强，但缺一键启动、预检不全、体检脚本永远返回 0 |
| 可调试 | ★★★★☆ | 注释与诊断脚本质量高、启动相位可视，但无 trace_id、日志不可配级、无 ErrorBoundary |
| 可观测 | ★★☆☆☆ | 有审计表和 admin 面板，但零 metrics、零 tracing、健康检查是静态的、token 成本不可见 |
| 可训练 | ★☆☆☆☆ | 数据原料优质（卷内结构化轨迹），但零落库、零反馈、零导出，销毁即永久丢失 |
| 可评测 | ★☆☆☆☆ | 后端 15% 模块有高质量测试，但无 CI、前端零测试、核心模块零测试、无 Agent evals |
| 可规模化迭代 | ★★★☆☆ | 镜像版本管理优秀、backend 分层好，但 Chat.tsx 3080 行单体、无 Alembic、单副本假设写死 |

---

## 一、可运行（Runnable）

### 现状（做得好的）

- **分层脚本体系**：`build-*`（构建）→ `start.sh`（启动+健康等待）→ `verify.sh`（体检）→ `stop.sh`，职责清晰、幂等
- **启动自愈链完整**：Docker daemon 等待 60s → compose 全服务 `restart: unless-stopped`（对抗 WSL VM 重启）→ 平台 `recover()` 重启后恢复容器台账与 SSE Pump
- **agent 容器健壮性**：entrypoint.sh 多层回退（无注入配置回落内置默认、预烘焙 SDK 免运行时安装）、双层健康检查（Dockerfile HEALTHCHECK + 平台探测，均带 BasicAuth）、PID 1 信号正确转发
- **防御性设计沉淀**：build-agent.sh 失败自动 `--network=host` 重试（WSL DNS）、wsl-sync.sh 的 CRLF/可执行位修复、弱密钥 fail-fast、warmup session 吸收 provider 初始化竞态

### 缺口

| # | 缺口 | 位置 | 影响 |
|---|---|---|---|
| R-1 | `verify.sh` 所有失败只打 WARN/MISS，**永远退出 0** | scripts/verify.sh | 体检结果无法被 CI/自动化判定 |
| R-2 | `start.sh` 健康等待超时后**静默继续**（与 Docker 等待逻辑不对称） | scripts/start.sh L43-50 | 后端起不来时脚本"正常"结束 |
| R-3 | `/api/health` 只返回静态 `{"status":"ok"}`，**不检查 DB/Docker 连通** | backend/app/main.py L140-143 | postgres 挂掉后健康检查仍 200，探针失真 |
| R-4 | DB 连不上 → uvicorn 崩溃 → 容器**无限重启循环**，无友好提示 | main.py L45 + compose restart 策略 | 部署失败只有 docker logs 可查 |
| R-5 | 无一键启动（build+start 串联）、无 Docker 版本/磁盘/端口/镜像/配置五项预检 | scripts/ 整体 | 新环境部署门槛高 |
| R-6 | 容器创建失败**无回滚**（半建容器/卷残留）；`max_restart_per_hour` 是死配置 | container_manager.py L449-468；config.py L87 | 失败后状态残留 |
| R-7 | compose `depends_on` 无 `condition: service_healthy`，backend 不等 postgres | docker-compose.yml L42-43 | 首次启动可能撞 DB 未就绪 |
| R-8 | 硬编码残留：smoke_test 容器名、verify_user_mcp.py 的 9123 端口、README 引用不存在的 e2e.py | scripts/ | 文档/脚本与实际漂移 |

### 优化建议

1. **P0**：`/api/health` 升级为真实就绪检查（DB ping + Docker ping + 可选 pump 存活）；`verify.sh` 失败项累计返回非零退出码
2. **P0**：`start.sh` 健康等待加 ready 标志位检查，超时即报错退出
3. **P1**：新增 `scripts/up.sh` 一键脚本：预检（Docker 版本 ≥24、磁盘 ≥5G、端口 3000/15432 空闲、`config/opencode.json` 存在、agent 镜像存在）→ 按需构建缺失镜像 → start → verify，任一步失败即停
4. **P1**：compose 加 postgres `healthcheck` + backend `depends_on: condition: service_healthy`；容器创建失败补回滚（删半建容器与卷）
5. **P2**：清理硬编码与文档漂移（端口、容器名、不存在的脚本引用）

---

## 二、可调试（Debuggable）

### 现状（做得好的）

- **注释质量极高**：大量"为什么"级设计注释记录真实故障史（401 误杀、ENOSPC、Bun GC 竞态、models.json 缓存 403）
- **启动相位实时可视**：`_start_phases`（creating→starting→warming）+ 启动错误双通道（内存 + DB `last_error`）→ 前端 1s 轮询展示进度
- **事后取证工具齐备**：diag-agent-crash.sh（退出码分流判定 OOM/段错误/SIGTERM + docker events 回溯）、diag-net.sh（WSL2 网络疑难）
- **错误语义映射**：tunnel 三类错误→503/504/500；LLM Proxy 未知 provider→404、上游失败→502（OpenAI 风格错误体）
- **前端错误处理**：统一 `apiCall` 拦截（401 清 session、双格式解析）、SSE 断线黄条 + 2s 自动重连、容器重启时工具调用标记中断

### 缺口

| # | 缺口 | 位置 | 影响 |
|---|---|---|---|
| D-1 | **无 trace_id / correlation_id / X-Request-ID**，无请求追踪中间件 | backend 全局 | 用户报错无法关联后端日志，排障靠时间戳猜 |
| D-2 | 日志纯文本、**LOG_LEVEL 不可配置**（Settings 无字段） | main.py L31-34 | 生产无法临时开 DEBUG |
| D-3 | **无通用 Exception handler**：未捕获异常走默认 500，无 logger.exception | main.py | 三种错误格式并存，前端各处兼容 |
| D-4 | **前端无 ErrorBoundary**：任何组件渲染抛错 = 整页白屏 | frontend/src/main.tsx | 前端崩溃对开发者不可见 |
| D-5 | 无日志轮转（compose 无 `logging:` 段） | docker-compose.yml | 长期运行日志无限增长 |
| D-6 | 诊断脚本全手动触发，无"一键收集全部日志打包"工具 | scripts/ | 现场排障组装信息慢 |

### 优化建议

1. **P0**：加 X-Request-ID 中间件（生成 → 注入 contextvars → 回传响应头），前端报错带 ID 即可检索后端日志。改动小、收益大
2. **P0**：加通用 Exception handler（`logger.exception` + 统一错误体 + request_id）；前端 App 包一层 ErrorBoundary
3. **P1**：Settings 加 `AGENT_LOG_LEVEL`；日志切 JSON 结构化格式（为后续接入日志平台留口）
4. **P1**：compose 各服务加 `logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}`
5. **P2**：新增 `scripts/collect-diag.sh`：打包 backend/frontend/agent 容器日志 + diag-agent-crash 输出 + 配置脱敏快照

---

## 三、可观测（Observable）

### 现状（做得好的）

- **审计事件**：`AuditEvent` 表覆盖容器生命周期五类动作（start/stop/destroy/restart/recreate），destroy 审计行内嵌卷备份元数据；fire-and-forget 不阻塞业务
- **Admin 面板**：平台总览、双源容器列表（DB+Docker、absent 显式标注）、`?stats=1` 实时 CPU/内存采样、日志 tail 100–2000、镜像陈旧检测（按 digest 比对）
- **SSE Pump**：200 条环形缓冲 + 单调 seq 支持断线补发（`lastEventId` 重放）

### 缺口（REQUIREMENTS.md 模块三"监控与告警"基本未动工）

| # | 缺口 | 说明 |
|---|---|---|
| O-1 | **零 metrics**：无 prometheus-client 依赖、无 `/metrics` 端点、无 Grafana | QPS/错误率/延迟/队列深度全部不可见 |
| O-2 | **零 tracing**：无 OpenTelemetry；一次对话横跨 前端→backend→容器→LLM Proxy→上游，无链路视图 | 跨层排障只能逐层看日志 |
| O-3 | **token 用量/成本完全不可观测**：LLM Proxy 转发所有请求但只记错误；`info.cost/tokens` 字段存在于消息中但平台不聚合 | 无法回答"这个月花了多少/哪个用户花最多" |
| O-4 | 审计覆盖不全：**认证事件零审计**（登录成功/失败）、**配置变更零审计**（provider/MCP/skill 增删改）、无 IP 字段、无保留期清理、查询无过滤 | R11.2 要求的 action/user_id 过滤未实现 |
| O-5 | 资源指标无时序：`container_metrics` 表未建，docker stats 只有按需单次采样，无历史曲线（R9 规划的 15s 采集 + range 查询未实现） | 无法回答"容器昨晚为什么 OOM" |
| O-6 | 关键数据面行为不可见：SSE Pump **QueueFull 静默丢事件无计数**、LLM Proxy 的 SSE tool-call 重写无"重写了多少 chunk"计数、tunnel 无耗时/错误率 | 最关键的链路行为是黑盒 |
| O-7 | 容器日志无流式推送（只有 tail 拉取）；**R12 告警完全未实现**（无规则引擎、webhook、通知渠道） | — |
| O-8 | 前端零监控：无 Sentry、无 web-vitals、无全局错误钩子 | 用户侧体验劣化无感知 |

### 优化建议

1. **P0**：后端接入 prometheus-client 暴露 `/metrics`（先做四类基础指标：HTTP 请求计数/耗时直方图、LLM Proxy per-provider 请求/错误/token 计数、SSE Pump 连接数/丢弃数、容器状态 gauge）——`token_usage` 从 LLM Proxy 响应体即可提取，这是成本可观测的第一步
2. **P0**：审计补齐认证与配置变更事件（`log_audit` 已就绪，只是没接）+ 请求 IP 字段 + 保留期清理任务
3. **P1**：建 `container_metrics` 表 + 15s 定时采样（挂到现有 idle_reclaim 后台任务框架）；Admin 面板加资源历史曲线（可直接用轻量方案：数据存 PG，前端画 sparkline，不必上 Grafana）
4. **P1**：OpenTelemetry 按 span 粒度接入（前端 → api/tunnel → llm_proxy → 上游），trace_id 与 D-1 的 request_id 打通
5. **P2**：告警规则最小集（容器 unhealthy > 5min、LLM Proxy 错误率 > 10%、磁盘 > 85%）+ webhook 通知；前端接 Sentry（或自托管 GlitchTip）
6. **P2**：容器日志 SSE 流式推送（复用现有 pump 模式）

---

## 四、可训练（Trainable）

> 定义：**数据沉淀 → 反馈信号 → 模型/系统改进**的闭环能力。当前该维度几乎空白，但数据原料质量出乎意料地好。

### 现状（数据原料优质）

- opencode 在每用户 `agent-data-{uid}` 卷 `/data/share` 中**以结构化 JSON 持久化完整轨迹**：对话文本、tool call 输入/输出、reasoning 推理链、patch/workspace diff、subtask 委派、**cost/tokens 计量**（形状见 frontend/src/oc/messages.ts L1-41，按官方 OpenAPI 逆向）
- 会话可通过 HTTP API（`GET /session/{id}/message`，经 tunnel 代理）程序化拉取——做 SFT/轨迹数据提取的优质原料

### 缺口

| # | 缺口 | 说明 |
|---|---|---|
| T-1 | **平台侧零落库**：models.py 全部 5 张表无 sessions/messages 表；后端从不读取 `/data/share` | 会话数据是"数据孤岛"，分散在 N 个卷 |
| T-2 | **零反馈机制**：无点赞/点踩/纠错/标注 UI、API、表；连 roadmap（R1-R16）都未规划 | 无法区分好/坏样本，DPO/偏好对无从谈起 |
| T-3 | **destroy 即永久丢失**：`_backup_workspace_sync` 只备份 workspace 卷，**不备份 data 卷**（R8.1 规划两卷都备份，实现只做了一半） | 全部会话历史在销毁时无备份直接消失 |
| T-4 | **零导出管道**：无定时采集/ETL、无 OpenAI messages/ShareGPT/DPO 格式转换器、无跨用户聚合 | 数据在卷里但取不出来、聚不拢 |
| T-5 | 无 A/B 实验、无模型对比、无 prompt 版本管理 | 系统改进无法归因 |

### 优化建议

1. **P0**：destroy 前备份扩展到 data 卷（`_backup_workspace_sync` 模式直接复制，改动极小）——先止血数据丢失
2. **P1**：建 `feedback` 表（message_id/session_id/user_id/rating/comment）+ 消息级反馈 API + Chat 界面点赞点踩按钮——反馈信号是训练闭环的起点，也是产品体验常规项
3. **P1**：会话镜像落库或定时采集：二选一——(a) 后端消费 SSE 事件流旁路写入 `session_messages` 表；(b) 定时任务经 tunnel API 拉取增量。落库后统一导出
4. **P2**：训练数据导出器：V1 `{info,parts}` → ShareGPT/DPO 格式（消息模型已在 messages.ts 完整逆向，可平移后端实现）；带反馈标注过滤
5. **P2**：引入 Alembic（见 S-2），为高频写入的会话/反馈表提供演进能力

---

## 五、可评测（Evaluatable）

### 现状（有的部分质量高）

- **后端 5 个测试文件、约 57 用例**：crypto 全覆盖、schema 迁移守卫、压缩包导入（含路径穿越/解压炸弹防护，~31 用例）、用户配置 CRUD + 多租户隔离——conftest 基建好（独立 SQLite + 依赖覆盖 + ASGI 内存客户端，不碰生产库）
- **手工 E2E 脚本**：verify_user_mcp.py 最完整（注册→加 MCP→启动容器→注入断言→tools/call→清理，带 PASS/FAIL 与退出码）；verify.sh 的前端 bundle 特征串抽查（检测旧镜像）
- docs/USER_NEEDS_ANALYSIS.md 8.2 已提出关键指标建议（冷启动 P95、并发容量、备份率）；5.1 有用 opencode-api.json 做功能覆盖度差集的方法论（人工做过一次）

### 缺口

| # | 缺口 | 说明 |
|---|---|---|
| E-1 | **CI/CD 完全缺失**：无 .github/workflows、无 pre-commit/husky、构建不跑测试 | 回归全靠手动，测试是"本地按需执行" |
| E-2 | **核心模块零测试**：container_manager(1094 行)、agent_controller(916 行)、**opencode_config 消毒流水线(521 行——文档定位为"可用性前提+安全底线")**、tunnel/SSE/LLM Proxy 数据面——已知 P0/P1 缺陷恰好都在无测试模块里 | 改核心逻辑等于裸奔 |
| E-3 | 无覆盖率统计（无 pytest-cov），前端零测试（package.json 无 test script、无 vitest/eslint/tsc 独立检查） | 覆盖情况不可见不可追踪 |
| E-4 | **无 Agent/平台质量评测**：无 evals 目录、无黄金集、无回归基准；消毒管线与 SSE 流式链路无质量度量；8.2 指标无落地 | "平台升级后 Agent 是否还能正常工作"无法自动回答 |
| E-5 | 需求无验收标准：REQUIREMENTS R1-R16 无 DoD/可测条件 | 需求完成度判定靠感觉 |
| E-6 | 测试依赖混入生产 requirements.txt | 镜像臃肿 |

### 优化建议

1. **P0**：建最小 CI（GitHub Actions 或本地 Makefile：`make test`）：后端 pytest + 覆盖率、前端 `tsc --noEmit` + build。先让"回归可一键执行"
2. **P0**：补消毒流水线测试（opencode_config.py 是纯函数逻辑，最易测、价值最高）：12 步消毒每步一个用例 + 被剥离字段断言
3. **P1**：补 agent_controller 状态机与 container_manager 加固参数测试（Docker SDK 可 mock/fake，skills_import 测试已示范 fake 模式）
4. **P1**：把 verify_user_mcp.py 升级为可重复的 E2E 套件（参数化 BASE、清理可靠），纳入 CI 的可选 stage（需 Docker 的 job）
5. **P2**：平台级 evals 最小集：
   - 契约差集自动化：opencode-api.json 与前端 api.ts 调用面比对（5.1 方法论脚本化，opencode 升级时自动发现不兼容）
   - 冒烟黄金集：10-20 条典型 prompt（对话/工具调用/文件引用/审批），升级镜像后跑通即 PASS——回答"升级后还能用吗"
   - 指标基线：冷启动耗时、首 token 延迟 P95（USER_NEEDS_ANALYSIS 8.2 落地）
6. **P2**：REQUIREMENTS.md 为每条 R 补可测的 DoD；requirements 拆 dev/prod

---

## 六、可规模化迭代（Scalable Iteration）

### 现状（做得好的）

- **镜像版本管理优秀**：semver tag 单一来源（.env）、按 digest 的陈旧检测、start 自动重建保卷、admin 面板 recreate 升级路径、opencode/插件双 pin 保证构建可复现
- **backend 模块化良好**：26 文件 7.2k 行，routers/services 分层清晰，最大文件职责单一
- **为扩展保留的余地**：持久状态全部外置 DB（recover() 双源恢复）、per-user 天然隔离（独立卷/密钥/配额）

### 缺口

| # | 缺口 | 位置 | 说明 |
|---|---|---|---|
| S-1 | **Chat.tsx 3080 行单体**：会话管理+SSE+渲染+工具 UI+审批+文件树+日志弹窗全在一个组件；chatStyles.ts 1386 行、ConfigPanel 1131 行、api.ts 1046 行 | frontend/src | REQUIREMENTS L303-327 已规划组件拆分（10 个组件），未实施。前端是迭代最慢的层 |
| S-2 | **无 Alembic**：手写 additive-only 迁移（`_add_missing_columns` 双方言内省），改列/删列/加唯一约束无机制，无迁移历史、不可回滚；注释自认"列数增长后此模式会腐烂" | database.py L52-99 | 一旦开始沉淀会话/反馈/指标数据（本路线图 T-2/T-3/O-3 都要加表加列），此模式立即成为瓶颈 |
| S-3 | **单副本假设写死**：SSE Pump 内存总线、一次性 SSE 票据、内存限流器、启动阶段跟踪、`_gate_cache` 全在进程内存（tunnel.py 注释明说"single worker assumption"）；Docker sock 限单机 | sse_pump.py / tunnel.py L274 / rate_limit.py | 无法多副本部署；用户量增长只能垂直扩 |
| S-4 | **opencode 版本升级强耦合**：前端消息层按 1.18.16 OpenAPI 硬编码、tunnel 依赖 legacy/v2 路由差异的补丁知识；每次升 OPENCODE_VERSION 需人工回归，无版本探测/适配层 | messages.ts L3、api.ts L487-490 | 升级成本高（正是 E-4 契约差集自动化要解决的） |
| S-5 | 无镜像 registry/CI 流水线、无多版本共存与按用户迁移（R15.2 的 images API 未实现）、无 changelog | — | 升级回滚手段有限 |
| S-6 | REQUIREMENTS 规划未落地的：R12 告警、R14 批量操作、R15 配额管理（无 `user_quotas` 表，只有全局限额）、R10 分页过滤（admin 全量返回） | — | 用户规模上去前的功能债 |

### 优化建议

1. **P1**：Chat.tsx 按已规划拆分：FilesPanel（L2124+ 已是现成边界）、SessionSidebar、MessageList、ToolCard、ApprovalCard、Composer——不必一次到位，先拆文件树与消息渲染两块（改动风险最低、收益最大）；顺手为拆分引入 vitest 组件测试（配合 E-3）
2. **P1**：引入 Alembic：现有 `create_all + _add_missing_columns` 保留为 bootstrap，新增表一律走 migration；`user_quotas`、`feedback`、`container_metrics` 作为第一批 migration 实践
3. **P2**：水平扩展预备清单（按需实施，不必现在做）：SSE 层换 Redis pubsub、票据/限流换共享存储、Docker 操作换 API 代理（同时缓解生产安全）、reclaim 加分布式锁
4. **P2**：opencode 版本适配层：启动时探测版本 → 前端能力开关；契约差集脚本（E-4）作为升级门禁
5. **P2**：配额管理最小集（R3.2/R15）：`user_quotas` 表 + start 时校验（每用户容器数/卷大小上限）——多租户平台规模化的前置条件

---

## 优先级路线图（汇总）

### P0 — 立即（止血 + 高杠杆小改动）

| 事项 | 维度 | 预估改动量 |
|---|---|---|
| `/api/health` 真实就绪检查（DB+Docker ping） | 可运行 | 小 |
| verify.sh 非零退出码；start.sh 健康等待超时报错 | 可运行 | 小 |
| X-Request-ID 中间件 + 通用 Exception handler | 可调试 | 小 |
| 前端 ErrorBoundary | 可调试 | 小 |
| destroy 备份扩展到 data 卷（止住会话数据丢失） | 可训练 | 小 |
| `/metrics` 端点四类基础指标（含 token 用量计数） | 可观测 | 中 |
| 审计补齐认证/配置变更事件 + IP 字段 | 可观测 | 小 |
| 最小 CI（pytest + 覆盖率 + tsc + build） | 可评测 | 中 |
| 消毒流水线（opencode_config.py）单元测试 | 可评测 | 中 |

### P1 — 近期（补体系）

- 一键 up.sh + 五项预检；compose healthcheck 依赖；容器创建回滚（可运行）
- LOG_LEVEL 可配 + JSON 日志 + 日志轮转；collect-diag.sh（可调试）
- container_metrics 时序表 + 15s 采样 + admin 历史曲线；审计过滤查询 + 保留期清理（可观测）
- feedback 表 + API + Chat 点赞点踩；会话数据落库或定时采集（可训练）
- agent_controller/container_manager 测试；verify_user_mcp 参数化纳入 CI（可评测）
- Chat.tsx 拆分第一批；Alembic 引入 + 第一批 migration（可规模化）

### P2 — 中期（面向规模与质量）

- 告警规则 + webhook；OTel 链路追踪；前端 Sentry；容器日志流式推送（可观测）
- 训练数据导出器（ShareGPT/DPO 格式 + 反馈过滤）（可训练）
- 平台 evals：契约差集自动化 + 冒烟黄金集 + 冷启动/首 token 基线；REQUIREMENTS 补 DoD（可评测）
- 配额管理（user_quotas）；水平扩展预备（Redis pubsub / Docker API 代理）；opencode 版本适配层；镜像 registry + changelog（可规模化）

---

## 附：维度间的依赖关系

```
可评测(CI/测试) ──► 可规模化迭代(重构 Chat.tsx 才敢动手)
       │                    │
       ▼                    ▼
可观测(metrics) ──► 可训练(数据采集) ──► 可评测(evals 黄金集)
       │                                      │
       ▼                                      ▼
  可调试(trace_id 贯穿)              可运行(升级门禁回归)
```

关键洞察：**「可评测」是其余维度的杠杆支点**——没有 CI 和测试，前端重构（S-1）与 opencode 升级（S-4）都不敢动；没有指标与数据采集，训练闭环与 evals 都无米下锅。建议以 P0 清单为切入，先让"改了敢验、验了可信"成立。
