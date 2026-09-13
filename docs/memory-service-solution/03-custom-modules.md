# 03 · 定制化模块详细设计

> 开源基座无法覆盖、需要自研的 6 个模块：memory-core 编排层、profile-engine、recommender、analytics、Web 控制台、Memory MCP Server。
> 设计总原则：**每个算法可解释、每个推荐有证据、每张图表能下钻到原始记忆**。

---

## 1. memory-core 编排层（Mem0/Graphiti 服务化封装）

**为什么需要定制**：Mem0 与 Graphiti 都是 Python 库（非服务），缺统一 REST API、批量提取管线、流式订阅与租户治理；两引擎写入语义不同（Mem0 事实记忆 vs Graphiti 实体图谱），需要编排一致性。

**模块结构**：

```
memory-core/
├── api/            # FastAPI 路由：memories, search, timeline, batch, events(SSE)
├── engines/
│   ├── mem0_engine.py      # MemoryEngine 适配器：命名空间映射(user_id/agent_id/run_id)
│   ├── graphiti_engine.py  # GraphEngine 适配器：group_id 映射、episode 摄入
│   └── base.py             # 引擎接口（可替换 provider）
├── pipeline/
│   ├── extractor.py        # 批量提取 worker（02 §3.1 管线）
│   ├── importance.py       # 重要度初评（写入时）
│   └── dedup.py            # 跨引擎一致性： Mem0 事实 ↔ Graphiti 实体 关联表
└── acl/                    # scope/ACL 谓词生成（唯一可信点）
```

**关键设计——双引擎一致性**：同一次提取产生的事实同时写入 Mem0（向量+SQL 历史）与 Graphiti（实体+事实边），以 `extraction_id` 关联；删除/失效操作双写（Mem0 软删 + Graphiti invalid_at），由 pipeline 幂等保障。图谱不可用时按 02 §6 降级。

## 2. profile-engine 用户画像引擎

### 2.1 画像数据模型（三个子模型）

| 子模型 | 数据结构 | 更新方式 |
|--------|----------|----------|
| **兴趣模型** | ① 主题标签分布 `{tag: weight}`（LLM 打标，指数衰减 `w *= exp(-Δt/τ)`，τ=30d）② 兴趣向量 = 近 90d 记忆 embedding 的衰减加权均值（半衰期 21d） | 事件驱动增量 + 每日重组 |
| **能力评估** | 从 Graphiti 实体中筛 `SKILL/TOOL/TECH` 类节点：`ability_score = f(证据边数, 时效, 项目级别)`；输出 top-N 技能 + 置信度 + 证据记忆链接 | 每日批（GDS 查询） |
| **行为特征** | 活跃时段直方图、检索模式（查询长度/失败率）、Agent 使用偏好（工具调用分布）、会话统计 | Redis 计数器实时 + 每日落表 |

画像快照（PG `profiles` 表）保留版本历史，供"画像演化回放"可视化。

### 2.2 睡眠计算批任务（借鉴 Letta arXiv:2504.13171）

触发条件（满足其一）：每日 03:00 低峰；用户连续 2h 无活动；Core 块容量告警。

| 任务 | 动作 | 产出 |
|------|------|------|
| 记忆重组 | 语义聚类近重复记忆合并（向量相似度 > 0.95 且同实体）→ 保留代表条 + 溯源链 | 存储瘦身、检索噪声下降 |
| Core 重写 | LLM 将 Core 块压缩为结构化摘要（人设/偏好/进行中任务），旧版本入历史 | 提示词 token 节省 |
| 兴趣重算 | 衰减加权向量 + 标签分布重归一化 | 画像向量 v(n) |
| 能力重评 | 图谱技能节点度中心性 + 时效衰减 | 能力 top-N |
| 一致性校验 | Core 块 ↔ 归档记忆 ↔ 图谱三方抽查冲突 → 冲突队列（控制台人工/LLM 仲裁） | 数据可信度 |

### 2.3 画像服务接口

- `GET /v1/profiles/{user_id}`（全量快照）、`GET /v1/profiles/{user_id}/interests|abilities|behavior`
- `GET /v1/profiles/{user_id}/history?metric=interests`（演化回放）
- 画像向量化：兴趣标签 + embedding 拼接为检索/推荐用的 `profile_vec`（Qdrant 独立 collection `profiles`）。

## 3. recommender 推荐引擎（自研两阶段）

### 3.1 推荐场景（v2 调整：以"提问推荐"为核心场景）

> 2026-09 需求澄清：推荐的核心产品形态是**两种提问推荐**，均由**前端调用 REST**（推荐结果展示给用户，非 Agent 侧）；
> 原"记忆回顾/知识关联"降级为 `memory_context` 的 side-channel；R3（技能成长）/R4（决策辅助）移入 backlog。

| 场景 | 时机 | 上下文来源 | 输出 |
|------|------|------------|------|
| R1 开场提问 | 新建对话时（session-start） | 画像（兴趣/能力）+ 高重要度近期记忆 + Core blocks 中的进行中任务 + 团队热点（可选） | 2 个可能提问 |
| R2 轮末下一问 | 一轮对话完成时（next-turn） | 本轮对话内容（user msg + agent 回复）+ 向量检索的相关记忆 + 实体事实（entity_facts 1 跳）+ 画像 | k 个下一轮提问（默认 2） |

### 3.2 管线与打分

```
召回（各路 top-50，合并）:
  A. Qdrant KNN：profile_vec / 最近任务向量 → memories
  B. Neo4j：当前任务实体 → 二跳邻域事实边（valid_at 生效）
  C. 规则：importance top + staleness 高（R1 专用）
排序:
  score = α·sem_sim + β·graph_proximity + γ·importance + δ·recency
          − η·seen_penalty − θ·entropy_penalty
  权重按场景配置（R1 偏重 γ+staleness，R2 偏重 β+α）
重排:
  MMR(λ=0.7) 保证多样性；同 source 去重
输出:
  item + score 分解（每因子贡献值）+ 证据链(来源记忆/图谱路径)  ← 可解释性
```

- **冷启动**：无画像时用 onboarding 显式选择 + org 级热门（popularity prior），首次会话后即切换画像驱动。
- **反馈闭环**：推荐项的 `accepted/dismissed/adopted` 事件写回 Streams → 画像行为特征 + 权重在线微调（bandit 思想：场景级权重 ε-greedy 探索，Phase 3 可选 LightFM 替换排序层）。
- **效果评估**：离线 NDCG@10 / HitRate@5（以采纳行为为正样本）；在线 CTR/采纳率看板（控制台 + Grafana）。

## 4. analytics 分析服务

### 4.1 使用模式分析

- 指标族：写入/检索 QPS（按 org/user/agent/scope 维度）、记忆留存曲线（cohort）、检索命中率（top-K 被采纳比例）、提取管线健康（积压/延迟/LLM 成本/千次写入 token 花费）。
- 实现：事件流物化到 PG 统计表（日聚合）+ Grafana 看板；明细查询走 API。

### 4.2 关联关系挖掘（Neo4j GDS）

| 挖掘任务 | GDS 算法 | 业务输出 |
|----------|----------|----------|
| 核心实体识别 | PageRank / 度中心性 | 团队知识枢纽（人/项目/技术） |
| 主题社区 | Louvain 社区检测 + 社区摘要（LLM） | 自动知识域聚类，图谱可视化着色 |
| 相似用户/项目 | 节点相似度（Jaccard） | 协作推荐、团队记忆发现 |
| 影响传播 | BFS 路径 | "这条事实从哪次会话传播到团队"溯源 |

### 4.3 重要度评估（多因子模型，写入时初评 + 每日重算）

```
importance = w1·access_score(访问频次×时间衰减)
           + w2·graph_score(实体 PageRank 归一化)
           + w3·recency_score(新鲜度)
           + w4·explicit_score(用户显式 pin/important 标记)
           + w5·source_score(显式指令 > 对话隐含 > 系统生成)
           + w6·reference_score(被其他记忆/会话引用次数)
权重默认 (0.25,0.2,0.1,0.2,0.15,0.1)，org 可调；重要性驱动：检索加权、推荐 γ 项、TTL 淘汰豁免
```

## 5. Web 可视化控制台（自研 React）

### 5.1 信息架构（页面清单）

| 页面 | 核心视图 | 交互 |
|------|----------|------|
| **记忆总览** | 存储分布环形图（scope/类型）、写入/检索趋势、Top 记忆卡片 | 下钻到记忆详情 |
| **记忆时间线** | 纵向时间轴（写入/更新/失效事件流，valid_at/invalid_at 可视化） | 过滤（scope/标签/Agent）；点击查看原始 episode 溯源 |
| **图谱探索** | G6 力导图：实体-事实边，社区着色，节点大小=PageRank，失效边虚化 | 点选实体 → 侧栏事实列表 → 记忆详情；时点回放滑杆（"3 个月前的图"） |
| **画像中心** | 兴趣标签云 + 雷达图、能力卡片（证据链展开）、行为热力图（时段×动作） | 画像演化回放（版本对比滑杆） |
| **推荐中心** | 推荐流（每条附证据链与得分分解）、采纳/忽略操作 | 反馈闭环入口 |
| **团队治理**（admin） | 成员/角色管理、共享记忆审批、ACL 配置、配额与用量 | — |
| **系统运维**（admin） | 管线健康、提取队列、LLM 成本、审计日志检索 | — |

### 5.2 技术实现要点

- 复用平台 React 栈与 nginx `/api/mem/*` 反代；OpenAPI → TS client 自动生成。
- 图谱增量渲染：G6 数据以 Graphiti 查询结果增量 merge，万节点内前端渲染，更大规模服务端聚合社区后下发。
- 实时性：SSE 订阅 `/v1/events`（记忆写入/画像更新推送），控制台无刷新更新。
- 权限：与 JWT RBAC 一致，页面级 + 操作级双控。

## 6. Memory MCP Server（Agent 接入面）

**形态**：两模式——① stdio 模式：脚本随 agent 镜像分发，容器内本地进程，经 agent-net 调用服务端（与 web_search MCP 同构，manifest 注入）；② 远端 streamable-http 模式：外部 agent 直连（云端 SaaS 场景）。

**工具面（v1，10 个）**：

| 工具 | 说明 |
|------|------|
| `memory_search(query, scope?, k?)` | 三路融合检索（默认 personal+team 可见域） |
| `memory_add(content, scope, tags?, important?)` | 写入（同步 ACK 异步提取；important 走快路径） |
| `memory_get(id)` / `memory_update(id, content)` / `memory_delete(id)` | 单条操作（软删） |
| `memory_timeline(range, filter?)` | 时间线拉取 |
| `memory_context(task)` | **高阶工具**：为当前任务组装上下文包（检索+图谱+画像提示，控制 token 预算） |
| `profile_get()` | 本人画像摘要 |
| `recommendations_get(scene?, k?)` | R1/R2 推荐 |
| `team_search(query, team_id)` | 团队域检索 |

实现参考 OpenMemory（单机版）与 graphiti-mcp（多 group）的成熟模式，认证用 per-container MCP 令牌（02 §5.3）。stdio 模式下 Server 经 `MEMORY_API_URL + MCP_TOKEN` 环境变量（backend 注入）访问 memory-core，容器内不落长期凭据。

## 7. 团队记忆治理（Mem0 之上的定制层）

- **共享写入策略**：写入 team scope 需 `editor+`；可选"审批制"（贡献 → admin 审核入库），审批队列在控制台治理页。
- **冲突消解**：同实体矛盾事实（Graphiti 已自动 invalid_at 软失效）；同 ID 并发更新用 PG 乐观锁（version 字段）；语义冲突（新旧偏好矛盾）由睡眠计算任务的冲突队列仲裁，记录仲裁人与依据（审计）。
- **配额**：org/team/user 三级配额（记忆条数、存储字节、每日提取 LLM token），超限 429，控制台可视化用量。
