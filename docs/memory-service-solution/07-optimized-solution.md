# 07 · 优化方案 v2 · 模块化单体架构（当前生效版本）

> 本文是对 01–06 号 v1 方案的全面评估与重构结论。v1 文档保留作为深度设计参考（算法细节、API 契约仍然有效），
> 但**架构形态、组件清单、实施计划以本文为准**。

---

## 1. v1 方案复杂度评估（问题清单）

| # | v1 设计 | 问题 | 代价 |
|---|---------|------|------|
| P1 | 4 个微服务 + 独立 MCP 服务 | 单领域、单团队、无独立扩缩需求，属"过早分布式" | 5 套 CI/镜像/部署；服务间 REST 调用链；事件 schema 版本治理负担 |
| P2 | 5 套存储引擎（PG/Qdrant/Neo4j/Redis/MinIO） | 无规模依据的多语言持久化 | 4 套备份体系、4 套监控告警、约 19GB 常驻内存；Neo4j 社区版无热备 |
| P3 | Mem0 + Graphiti 双引擎 | 自造跨存储一致性问题（双写、extraction_id 对账、冲突仲裁） | 最复杂的核心链路，故障面最大 |
| P4 | Redis Streams 事件总线 + Consumer Group + DLQ 流 | 为日均数百事件引入 Kafka 级机制 | 运维与调试复杂度远超收益 |
| P5 | Working（会话记忆）层 | 与 opencode 自身的会话管理重复 | 冗余逻辑 |
| P6 | 网关谓词 + 服务 ACL + PG RLS 三重租户校验 | 三处强制点，三处都要测试维护 | 防御过度；两重即可 |
| P7 | MinIO 备份仓 + Grafana/Prometheus + mTLS + Superset | 单机部署形态下均为超前建设 | 组件数膨胀的主要来源 |
| P8 | 22 周三阶段路线图 | 被上述复杂度拖长 | 交付延迟放大所有风险 |

**根因**：v1 按"千级用户 SaaS"假设设计，而平台实际是**单机 docker-compose 部署、小团队维护**的形态。本次优化按实际规模重估一切容量与架构决策。

## 2. 规模重估（一切裁剪的依据）

| 维度 | 当前实际（依据：平台单机 compose、agent_demo 库、demo 默认配置） | 设计余量（10×） |
|------|------|------|
| 并发用户 | ≤ 50 | 500 |
| 记忆条数 | ≤ 10 万 | 100 万 |
| 向量数 | ≤ 10 万 | 100 万（pgvector HNSW 舒适区） |
| 事件量 | 日均数百 | 数万/日 |
| 维护团队 | 1–2 后端 + 1 前端 | — |

> 若实际规模显著超出上表，按 §9 触发条件表逐步恢复组件——**升级有明确路径，但只在被证明需要时支付成本**。

## 3. 优化决策总表

| 决策 | v1 | v2 | 理由 |
|------|----|----|------|
| 服务形态 | 4 微服务 + MCP 服务 | **1 个 FastAPI 模块化单体**（MCP 端点内建） | 同域同团队无独立扩缩需求；进程内调用替代 REST+事件总线 |
| 向量存储 | Qdrant | **pgvector**（PG 内） | ≤100 万向量 pgvector+HNSW 完全胜任；省一个引擎一套备份；经 Mem0 provider 抽象，超限可平滑换回 |
| 图谱/时序 | Neo4j + Graphiti | **PG 双时间戳事实表**（entity_facts）+ networkx 离线批算 | 时点查询=两条 WHERE 谓词；1–2 跳关联=SQL JOIN；PageRank/社区检测在 10 万边量级用 networkx 内存计算绰绰有余。消除 P2/P3 全部代价 |
| 缓存 | Redis | **进程内 LRU**（TTL 60s/300s） | 单副本部署，分布式缓存无意义 |
| 事件/队列 | Redis Streams | **PG 任务表**（`FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`） | 持久、可观测（就是一张表）、零新组件；DLQ=任务表的 failed 状态列 |
| 备份仓 | MinIO | **pg_dump/WAL → 本地卷/云存储直写** | 单库后一套备份覆盖全部数据 |
| 前端 | 独立控制台应用 | **并入平台既有 React SPA**（新路由 `/mem`） | 复用登录、nginx 反代、构建管线 |
| 记忆分层 | Core/Working/Archive 三层 | **Core + Archive 两层** | Working 层职责归还 opencode 会话管理（消除 P5） |
| 租户校验 | 三重 | **两重**：JWT 中间件注入谓词 + 仓储层强制过滤（RLS 降级为可选加固项） | 消除 P6 |
| 可观测 | Prometheus+Grafana 必选 | 服务暴露 `/metrics`；Grafana 为**可选运维件** | 消除 P7；平台需要时再加 |
| 审批流/Superset/mTLS | 默认建设 | **砍掉/延后**（审批模式保留代码路径，默认关） | 消除 P7 |
| 路线图 | 22 周 | **11 周** | 复杂度下降的自然结果 |

**技术选型不变的项**：Mem0 作为嵌入式库保留（提取/更新决策引擎，是省下数月自研 LLM 管线的关键）；调研报告的可信度结论与"自建召回评测为准入闸"原则继续有效；REST API 与 MCP 工具面契约（04 文档）不变，只是由单体统一提供。

## 4. v2 系统架构

```mermaid
flowchart TB
    subgraph AN["agent-net（既有）"]
        C1["Agent 容器 opencode × N"]
        SH["stdio shim（随镜像分发，非部署单元）"]
    end
    subgraph PN["platform-net（既有）"]
        FE["平台前端 SPA（既有容器）<br/>+ 记忆控制台模块 /mem"]
        BE["平台 backend（既有容器）<br/>JWT 签发 · LLM Proxy"]
        MS["memory-service（唯一新增容器）<br/>FastAPI 模块化单体"]
        PG[("PostgreSQL（既有实例）<br/>schema: memory + pgvector")]
    end

    C1 -->|"MCP over HTTP (agent-net)"| MS
    C1 -.->|"可选 stdio"| SH
    SH -->|"HTTP + 容器令牌"| MS
    FE -->|"/api/mem/* 反代"| MS
    MS -->|"全部业务数据（事务）"| PG
    MS -->|"抽取/Embedding"| BE
```

### 4.1 memory-service 内部模块（替代 v1 的 4+1 服务）

```
services/memory-service/          # 唯一新增代码库
├── api/            # REST(OpenAPI 3.1) + MCP HTTP 端点 + SSE —— 对外契约唯一出口
├── auth/           # JWT/MCP 令牌校验 · 租户谓词注入（第一强制点）
├── store/          # 仓储层：memories/entities/facts/profiles/jobs（谓词二次强制点）
├── engines/
│   ├── mem0_engine.py     # Mem0(pgvector provider) 提取+更新决策
│   └── fact_extractor.py  # 实体+时序事实抽取（同一次 LLM 调用产出，替代 Graphiti）
├── pipeline/       # 写入管线 worker（--worker 进程，消费 PG 任务表）
├── profile/        # 画像模块：事件钩子增量 + 每夜批（合并/重写/重算）
├── recommend/      # 两阶段推荐（进程内查询，无独立服务）
├── analytics/      # 用法统计 + 重要度评分 + networkx 图算批任务
└── console_api/    # 控制台专用聚合接口
```

**模块间通信 = 进程内函数调用**；`api`/`store` 是仅有的两个跨模块契约面。部署上支持两种进程角色：`web`（API 服务）与 `--worker`（提取管线+定时批），同镜像不同启动参数，可各自横向扩副本。

### 4.2 保留组件必要性矩阵（每个组件不可替代的业务价值）

| 组件 | 功能 | 不可替代性论证 | 移除后果 |
|------|------|----------------|----------|
| **memory-service** | 记忆 CRUD、检索、提取管线、MCP/REST 接入、画像、推荐、分析的业务逻辑载体 | 核心需求的全部实现体；拆掉即无产品 | 系统不存在 |
| **PostgreSQL + pgvector**（复用既有实例，新增 schema） | 元数据/向量/时序事实/画像/任务队列/审计的**唯一持久层**，事务保证一致 | 4 个 v1 存储的能力在本规模下的交集承载者；平台已在运行，零新增运维 | 无持久化；它是"一个数据库替代四个引擎"的复杂度消解点 |
| **Mem0 库**（嵌入依赖，非部署单元） | LLM 记忆提取与 ADD/UPDATE/DELETE/NOOP 更新决策的成熟引擎 | 自研等价物 = 数月提示词工程+评测调优+持续跟进模型演进；Apache-2.0 无成本 | 提取质量与迭代速度显著劣化 |
| **平台前端 + 记忆控制台模块** | 可视化观测（时间线/画像/统计/实体关系），需求 2 的直接交付 | 用户与管理员唯一交互面；并入既有 SPA 后复用认证/构建/部署 | 无可视化，"可视化云端记忆服务"定位不成立 |
| **平台 JWT + LLM Proxy**（既有组件复用） | 多租户身份与 LLM 统一出口（计费/限流/密钥） | 平台已解决，重复建设即浪费 | 凭据管理失控 |
| **MCP 端点**（内建于 memory-service）+ 镜像内 stdio shim | Agent 容器原生工具面（search/add/context 等 10 工具） | Agent 接入的平台既定机制（web_search 先例）；REST 直连会让 Agent 提示词承担 API 语义 | Agent 无法零改造接入 |
| **std shim 随 agent 镜像分发** | 兼容仅支持 stdio 的运行时 | ~100 行脚本，打进既有镜像构建，不是部署单元 | 部分旧容器无法接入（可后补） |

**被移除组件的去向**：Qdrant/Neo4j/Redis/MinIO → 触发条件见 §9；profile-engine/recommender/analytics 独立服务 → 降级为进程内模块（功能未删，形态收敛）；独立 MCP Server → 端点内建；Superset/mTLS/审批流默认值 → 移除或延后。

## 5. 精简后的核心业务流程

### 5.1 写入管线（对比 v1：去掉 Streams/Consumer Group/DLQ 流，任务表一站到底）

```
Agent/控制台 → POST /v1/memories
  → 校验 JWT/scope → INSERT memories(pending) + INSERT jobs  [单事务]
  → 202 返回 memory_id
worker（--worker 进程）：
  → SELECT ... FROM jobs FOR UPDATE SKIP LOCKED LIMIT 50      [批量领取]
  → LISTEN/NOTIFY 唤醒（重要级 high 的任务即时通知）
  → 一次 LLM 调用同时产出：Mem0 事实决策 + 实体/时序事实（替代 v1 双引擎两次抽取）
  → 单事务写回：memories(extracted) + embedding + entity_facts + importance 初评
  → 进程内钩子：profile 增量更新（计数器/标签）
失败：jobs.retries+1，3 次后 status=failed（即 DLQ，控制台可见可重放）
```

批量策略保留 v1 的成本控制设计：普通写入缓冲 `max(50 条, 30s)` 合并 LLM 调用，`important=true` 走即时快路径。

### 5.2 检索管线（对比 v1：三路减两路 + SQL 内融合）

```
query → 向量召回(pgvector, 租户谓词, top-20) ∥ BM25(tsvector, top-20)
      → RRF 融合 → 命中记忆的实体做 1 跳事实关联（entity_facts JOIN，含 valid_at 过滤）
      → 进程内 LRU 缓存(60s) → 返回 {memories[], facts[], score_breakdown}
```

图遍历信号从"并行第三路"降为"结果增强"，因为 10 万记忆量级下实体命中率与 1 跳 JOIN 成本远低于 v1 假设。

### 5.3 画像 / 推荐 / 分析（形态收敛，算法从 03 文档）

- **画像**：进程内钩子做增量（写入/检索事件直接更新内存计数→批量落库）；每夜 03:00 单个批任务完成"去重合并 + Core 重写 + 兴趣重算 + 能力重评"（能力评估从 GDS 中心性简化为**实体证据计数+时效衰减**，等价信息量、零图库依赖）。
- **推荐**：R1（记忆回顾）+ R2（知识关联）检索式推荐进程内实现；新增 **S1/S2 问题推荐**（会话开场/下一轮建议提问，接口设计见 §11.1）；R3/R4 移入 backlog（低频）。反馈写表，每夜微调场景权重。
- **分析**：用法统计=日聚合表；重要度=多因子模型（03 §4.3 不变）；关联挖掘=每夜 networkx（从 entity_facts 构图算 PageRank/社区，10 万边内存秒级）。

### 5.4 团队记忆与安全（对比 v1：流程与强制点各减一）

- scope 三级（personal/team/org）+ RBAC 保留；**审批制默认关闭**（代码路径保留）。
- 租户强制点收敛为两处：JWT 中间件注入谓词 → 仓储层强制拼接（CI 跨租户攻击用例不变，RLS 变为可选加固）。
- MCP 容器令牌（短 TTL、可吊销）不变。

## 6. 数据模型变更（相对 04 文档的 delta）

04 文档的 memories/core_blocks/profiles/episodes/audit_log/usage_daily DDL **继续有效**，全部落在 PG 单 schema `memory`。变化项：

```sql
-- ① memories 增加 PG 内向量与全文（原 Qdrant/独立 BM25 职责）
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE memories ADD COLUMN embedding vector(1024);   -- 维度随 Embedding 模型
ALTER TABLE memories ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;
CREATE INDEX idx_mem_hnsw ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_mem_tsv  ON memories USING gin (tsv);

-- ② 新增时序事实表（原 Neo4j/Graphiti 职责；双时间轴语义完整保留）
CREATE TABLE entities (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, scope TEXT NOT NULL, owner_id TEXT NOT NULL,
  name TEXT NOT NULL, type TEXT,                -- Person/Project/Tech/Concept/Team
  aliases TEXT[] DEFAULT '{}',
  UNIQUE (org_id, owner_id, name));
CREATE TABLE entity_facts (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
  subject_id TEXT REFERENCES entities, object_id TEXT REFERENCES entities,
  predicate TEXT NOT NULL,                       -- uses/prefers/owns/works_on...
  fact_text TEXT NOT NULL,
  valid_at TIMESTAMPTZ, invalid_at TIMESTAMPTZ,  -- 双时间轴：失效不删除
  memory_id TEXT REFERENCES memories,            -- 溯源到产生它的事实记忆
  created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX idx_facts_temporal ON entity_facts (org_id, subject_id, valid_at, invalid_at);
-- 时点查询：WHERE valid_at <= :T AND (invalid_at IS NULL OR invalid_at > :T)

-- ③ 新增任务表（原 Redis Streams 职责）
CREATE TABLE jobs (
  id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL, memory_id TEXT,
  type TEXT NOT NULL, priority INT DEFAULT 5,      -- 1=important 快路径
  status TEXT DEFAULT 'pending',                   -- pending|running|done|failed
  retries INT DEFAULT 0, last_error TEXT, run_after TIMESTAMPTZ DEFAULT now(),
  created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX idx_jobs_pick ON jobs (status, priority, run_after);
-- 删除项：Qdrant collections、Neo4j 图模型、Redis 键规划章节整体作废
```

**一致性从"跨存储对账"变为"单库事务"**：记忆正文、向量、事实边在同一事务内写回，v1 的 extraction_id 对账与冲突仲裁任务不再存在。

## 7. 部署与备份（v2）

```yaml
# docker-compose.memory.overlay.yml —— 叠加在平台 compose 之上，仅 1 个新服务
services:
  memory-service:
    build: ./services/memory-service
    command: web            # API 进程；扩容时另起 command: web --worker 或独立 worker 服务
    environment:
      - AGENT_DATABASE_URL=postgresql://agent:***@postgres:5432/agent_memory
      - MEMORY_MCP_PUBLIC_URL=http://memory-service:8100   # agent-net 可达别名
      - LLM_PROXY_URL=http://backend:8000/llm-proxy
    networks: [platform-net, agent-net]   # 双网：前端经反代、Agent 容器直连 MCP
    deploy: { resources: { limits: { cpus: '2', memory: 1536M } } }
```

- **资源**：新增常驻内存 ≈ 1.5GB（v1 约 19GB）；pgvector 100 万向量约占 PG 4–6GB 磁盘。
- **备份**：单套 `pg_dump 每日全量 + WAL 归档`（RPO ≤ 5min / RTO 30min）；卷级加密随宿主。**恢复 = 恢复一个数据库**，v1 的 4 套备份矩阵作废。
- **监控**：`/metrics`（RED + 管线深度 + 业务指标）+ 结构化日志；Grafana 接入为可选步骤（平台需要时 30 分钟接好）。

## 8. 实施计划（11 周，v1 为 22 周）

| 阶段 | 周期 | 范围 | 里程碑验收 |
|------|------|------|------------|
| **P1 记忆基座** | 第 1–4 周 | schema+pgvector、Mem0 接入、写入管线（任务表）、检索（向量+BM25+RRF）、MCP 端点+镜像注入、控制台总览/时间线页 | **M1**：容器内"记住 X"→新会话召回；自建召回评测 Top-5 ≥ 70%；检索 p95 < 300ms（10 万向量）；跨租户用例 100% 拦截 |
| **P2 事实·团队·画像** | 第 5–8 周 | entities/entity_facts + 时点查询、团队 scope/RBAC/配额、画像模块（钩子+夜批）、分析统计+重要度、控制台画像/实体页 | **M2**：时点查询 ≥ 90%；团队越权 0 逃逸；画像 Top5 一致率 ≥ 60%；软删→清除链路审计可查 |
| **P3 推荐·生产化** | 第 9–11 周 | 问题推荐 S1/S2（§11.1）+ 检索式推荐 R1/R2 + 反馈表、短期记忆摄取 plugin（§11.3）、安全加固（fuzz/加密选项）、备份恢复演练、压测 | **M3**：S1/S2 建议问题点击率 ≥ 10%、推荐采纳率 ≥ 15%（证据链完整）；轮次摄取成功率 ≥ 99%；恢复演练通过；50 并发压测达 SLO |

团队配置：1–2 后端 + 1 前端（v1 需 3+1）。风险登记沿用 06 文档，新增一条：**pgvector 单点性能若在 P3 压测不达标，按 §9 触发表换 Qdrant**（Mem0 provider 切换，API 不变）。

## 9. 组件恢复触发条件表（升级路径，非默认建设）

| 组件 | 触发条件 | 迁移成本 |
|------|----------|----------|
| Redis（缓存/队列分担） | web 副本 > 1 且缓存命中率下降明显；或任务表竞争成为瓶颈 | 低（LRU→Redis 客户端替换；任务表保留为持久层） |
| Qdrant | 向量 > 500 万 或 pgvector 调优后 p95 仍 > 300ms | 中（Mem0 provider 配置切换 + 数据迁移脚本） |
| Neo4j + Graphiti | 事实 > 100 万 或 >2 跳深查询占比 > 20% | 中（entity_facts 表 → Graphiti episode 导入，schema 同构） |
| Kafka | 事件消费方跨团队/跨服务 | 高（接口已按 publish/subscribe 抽象则中） |
| K8s | 用户 > 1000 或多机部署需求 | 高（单体无状态，迁移本身不难） |
| Grafana | 运维成熟度要求 | 低 |

## 10. 复杂度对比量化（v1 → v2）

| 维度 | v1 | v2 | 降幅 |
|------|----|----|------|
| 新增部署单元（容器） | 11（5 服务 + 4 存储 + 监控 + 控制台） | **1**（memory-service；控制台并入既有前端镜像） | -91% |
| 存储引擎数 | 5 | **1** | -80% |
| 备份体系数 | 4 | **1** | -75% |
| 服务间接口 | REST×多 + 事件 schema 版本治理 | 进程内调用 + 1 个 OpenAPI | 接口面消除 |
| 一致性问题域 | 跨 3 存储对账 | 单库事务 | 消除 |
| 新增常驻内存 | ~19 GB | ~1.5 GB | -92% |
| CI/镜像流水线 | 5 条 | 1 条 | -80% |
| 交付周期 | 22 周 | 11 周 | -50% |
| 核心功能覆盖 | 全部需求 | **全部需求**（仅 R3/R4 推荐场景与审批制延后为可选项） | 无核心缺失 |

## 11. 疑问澄清与方案增补（v2.1）

> 对应评审提出的 5 个问题：问题推荐接口、画像定时任务、短期记忆接入、可视化定位、与直接使用 Mem0 的差异。

### 11.1 问题推荐接口（新增场景 S1/S2，独立于 R1/R2）

R1/R2 是**检索式**推荐（推荐已有记忆）；S1/S2 是**生成式**推荐（基于记忆与画像生成建议提问），服务于对话引导。两个显式接口：

```
POST /v1/recommendations/questions/session-start     # S1：新建对话时
  body:  {agent_id?, first_message?}                  # 均可选；冷启动时纯画像驱动
  resp:  {questions: [{id, text, rationale,
           grounding: {profile_tags: ["k8s","agent-memory"], memory_ids: ["mem_..."]}}],
           generated_by: "cache|llm", ttl_seconds: 300}

POST /v1/recommendations/questions/next-turn         # S2：一轮对话完成时
  body:  {session_id, turn: 6, last_user_message?}    # last_user_message 可缺省，服务端可从 episodes 取
  resp:  同上（grounding 附加 used_turn_summary: true）
```

**生成管线**（单次 LLM 调用 + 进程内排序，p95 ≤ 2s，预取后命中 ≤ 50ms）：

```
grounding 组装（无 LLM，进程内查询）:
  S1: 画像 top-5 兴趣标签 + 近 7d 高频记忆主题 + 未完结任务的高重要度记忆（≤3 条）
  S2: 当前会话轮次摘要（§11.3 ingest 提供）+ 对最后轮次向量检索相关记忆 top-3 + 画像提示
→ LLM 生成 4 个候选问题（prompt 强制多样性：不同意图类别、不得与已问问题重复）
→ 进程内排序：画像相关性 + 新颖度（与该用户历史提问的向量距离）→ 取 top-2
→ 预取机制：S2 由 §11.3 的轮次摄取钩子触发预生成（对话完成的瞬间后台跑），
  前端请求到达时直接命中缓存——用户零感知延迟
→ 反馈：问题被点击即 POST /v1/recommendations/{id}/feedback {action: adopted}，
  汇入画像行为特征（高频采纳的主题标签加权）
```

消费方：**平台前端**（对话框下方渲染建议 chips，与 Chat.tsx 同级组件）为主；MCP 工具 `suggest_questions` 为可选（供 agent 自主场景）。M3 验收：点击率 ≥ 10%。

### 11.2 用户画像异步刷新：提供，三触发调度

| 触发 | 机制 | 时机 | 作用 |
|------|------|------|------|
| ① 实时钩子 | 写入/检索事件的进程内回调 | 每次事件 | 轻量增量：计数器、标签命中累计（不重算向量） |
| ② **定时夜批** | worker 内 APScheduler → `jobs(type=profile_rebuild)` | 每日 03:00（org 时区可配） | 全量重组：去重合并、Core 块 LLM 重写、兴趣向量衰减重算、能力重评、冲突校验 |
| ③ 空闲插队 | worker 周期扫描（15min） | 距上次画像版本 > 6h 且期间新增事件 ≥ 20 的活跃用户 | 保证活跃用户画像白天不过期 |

**多副本防护**：`jobs` 表唯一约束（user_id + 日期批次）+ `pg_try_advisory_lock('profile:{user}')`，多 worker 不重复执行——复用既有任务表，不引入新调度组件。

**新鲜度 SLO**：活跃用户画像滞后 ≤ 6h；`GET /v1/profiles/{user_id}` 响应含 `computed_at` 与 `trigger`（realtime/nightly/idle/manual）；控制台画像页展示新鲜度与手动刷新按钮（`POST /v1/profiles/{user_id}/rebuild`，admin/本人）。

### 11.3 短期记忆接入机制：plugin 旁路摄取 + MCP 显式读写，不用 MCP 做自动摄取

**原则**：短期记忆本体（会话消息、滚动上下文）留在 opencode 容器内由其 session 机制管理；云端只做**摄取（写入口）与注入（读出口）两个旁路**，不复制会话状态。

```mermaid
sequenceDiagram
    participant OC as opencode（容器内）
    participant PL as memory plugin（镜像预装，确定性代码）
    participant MS as memory-service
    participant AG as Agent(LLM 决策)

    Note over OC,PL: ① 自动摄取（非 MCP，零 token、零 LLM 决策）
    OC->>PL: 事件: message.updated / session.idle(30s)
    PL->>MS: POST /v1/ingest/turn {session_id, turn, messages, agent_id}
    MS->>MS: 落 episodes + jobs(extract) → 批量管线（§5.1）
    MS--)PL: 202 + 顺带触发 S2 预生成（§11.1 预取）

    Note over AG,MS: ② 显式记忆（MCP，需要 agent 判断的操作）
    AG->>MS: MCP memory_add("记住我用 pnpm", important=true)
    Note over AG,MS: ③ 消费注入（MCP，新会话/每轮可选）
    AG->>MS: MCP memory_context(task) → Core块+相关记忆+画像提示

    Note over OC,PL: 会话结束升级
    OC->>PL: 事件: session.end
    PL->>MS: POST /v1/ingest/session-end {session_id}
    MS->>MS: jobs(summarize) → pipeline 判定 importance ≥ 阈值的轮次升级为长期记忆 + entity_facts
```

**为什么自动摄取不走 MCP**：MCP 工具调用需要 LLM 决策——每轮自动提交既不可靠（agent 可能不调）又消耗 token；摄取是**平台行为**而非 agent 能力，必须是确定性通道。plugin 是打进 agent 镜像的 TS 插件（与 builtin-mcp 注入同批、同发布节奏），只做"事件监听 → 截断打包 → HTTP POST"三件事，携带容器令牌。MCP 面保留给真正需要 agent 判断的操作（显式记忆、上下文拉取）。

**消费注入两种模式**（可配）：a) agent 每轮自主调 `memory_context`（默认，opencode 提示词引导）；b) plugin 在 session 开始时把 `memory_context` 结果作为首条 system 消息注入（更确定，适合弱提示词场景）。

**接口**：`POST /v1/ingest/turn`、`POST /v1/ingest/session-end`（仅容器令牌可调，幂等键 session_id+turn）；摄取内容默认 30 天滚动窗口归档于 episodes，超期按 org 保留策略清除。

### 11.4 可视化：未缩减，形态为"并入既有前端"，Grafana 仅作运维补充

v2 砍掉的是**独立部署的控制台容器**（部署单元），不是可视化功能——页面清单（记忆时间线/画像雷达/实体关系图/推荐流/团队治理）全部保留，渲染载体收敛：

| 路径 | 说明 | 状态 |
|------|------|------|
| **A（默认）：并入平台 frontend** | `frontend/src/components/` 新增 `MemoryConsole.tsx` 模块（与 Chat.tsx/AdminPanel.tsx 同构），路由 `/mem`，经既有 nginx 反代 `/api/mem/*` → memory-service。复用登录态、主题、构建与部署 | **v2 既定** |
| B：挂载到 agent-docker-demo 前端 | 机制与 A 完全相同（反代 + 同源 JWT）；若 demo 为独立仓库，两种集成方式：子路径挂载（nginx location 反代）或发布为私有 npm 模块引入。workspace 内未发现该项目，按独立仓库对待 | 支持即可 |
| C：Grafana/Prometheus | **仅运维指标**（RED、管线深度、token 成本、告警），经 `/metrics` 接入，平台需要时 30 分钟配好 | 可选 |

边界重申：记忆时间线（双时间轴交互）、画像雷达（版本回放）、实体关系图（G6 力导 + 下钻记忆详情）、推荐证据链展示均为**业务语义视图**，Grafana 的时序面板模型与 Superset 的 SQL 面板模型均无法承载，必须由前端模块自研渲染——这是 v1 D5 决策在 v2 中不变的部分。

### 11.5 与直接使用 Mem0 的区别

| 维度 | 直接用 Mem0 OSS（SDK / OpenMemory） | 本方案（memory-service） |
|------|--------------------------------------|---------------------------|
| 形态 | Python 库 / 单用户本地 MCP + 简易 UI | 云端多租户服务（REST + MCP + SSE） |
| 多租户 | user_id 命名空间，**无鉴权语义**，无团队概念 | org/team/user 三级 scope + JWT + RBAC + 审计 + 配额 |
| 团队记忆 | 无 | team 域共享、角色控制、软删保留期 |
| 时序事实 | 弱（事实记忆为主） | entity_facts 双时间轴 + 时点查询（Graphiti 核心能力以 SQL 承载） |
| 写入管线 | 同步阻塞，每次 add 含 LLM 调用 | 同步 ACK + 批量异步提取（LLM 成本 -60%）+ 快路径 |
| 重要度评估 | 无 | 多因子模型（访问/图谱/时效/显式标记/引用） |
| 用户画像 | 无 | 兴趣/能力/行为三模型 + 三触发异步刷新（§11.2） |
| 推荐 | 无 | S1/S2 问题推荐 + R1/R2 检索式推荐，全带证据链 |
| 可视化 | OpenMemory 单用户列表页 | 多租户控制台（时间线/画像/实体图/治理） |
| 分析 | 无 | 用法统计、关联挖掘（networkx）、重要度分布 |
| Agent 接入 | 需改 agent 代码（SDK）或单机 OpenMemory | 平台 MCP 注入零改造 + 摄取 plugin 旁路 |
| 运维 | 使用方自理 | 单库备份、/metrics、审计、恢复演练 |

**一句话**：Mem0 是本方案内嵌的"记忆提取与更新决策引擎"（省去数月自研的核心部件），本方案提供它没有的**产品化服务层**——多租户、团队、时序事实、画像、推荐、可视化、治理。
**边界诚实声明**：如果需求仅是"单用户本地记忆增强"，直接用 OpenMemory 即可，无需建设本服务；本方案的价值在多用户、团队协作、可视化观测与个性化推荐这些 Mem0 明确不提供的维度。
