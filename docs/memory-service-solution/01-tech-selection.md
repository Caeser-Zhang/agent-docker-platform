# 01 · 技术选型报告与开源项目评估

> 选型原则：**优先成熟开源，最小定制**。所有基座均为 Apache-2.0/MIT 级宽松许可证；
> Benchmark 数据采信遵循调研报告的可信度分级（同行评审论文 > 统一 harness 独立复现 > 厂商自报），
> **不采用任何厂商自报分数作为选型依据**（见报告第 06 节）。

---

## 1. 记忆引擎基座：Mem0 vs Zep/Graphiti vs Letta

| 维度 | Mem0 | Zep / Graphiti | Letta |
|------|------|----------------|-------|
| GitHub ★（2026-09 实测） | **65,203** | 30,837（graphiti） | 24,716 |
| 许可证 | Apache-2.0 | Apache-2.0（引擎） | Apache-2.0 |
| 架构范式 | 向量+图+KV 混合，LLM 自动提取 | 时序知识图谱，双时间轴事实失效 | OS 式三层记忆，Agent 自编辑 |
| 多租户 | user_id / agent_id / run_id 三级命名空间，**原生** | group_id 图谱隔离 | 单 Agent 域内 |
| 时序推理 | 弱（2026-05 起补强） | **最强**：valid_at/invalid_at 一等公民 | 无专用时序索引 |
| Agent 接入 | drop-in SDK / REST / MCP（OpenMemory） | 引擎 + 官方 MCP Server | 需接受其 Agent 循环 |
| 自托管成本 | 低（向量库起步） | 中（图库 + 每次摄入多次 LLM 调用） | 中 |
| 关键短板 | 记忆结构扁平；写入含 LLM 成本；团队级治理需自建 | 图库运维门槛；实体消解风险；完整服务（冲突消解）闭源 | 运行时锁定；每轮 token 开销高；公开基准少 |

**决策：双引擎组合 —— Mem0 承载"个人偏好/事实记忆"，Graphiti 承载"时序事实图谱（团队记忆与关联挖掘）"。**

理由：
1. 调研报告第 07 节对"容器化 Agent Workspace 场景"的组合建议即为此路线：*Mem0 做用户偏好层 + Graphiti 做业务事实层，通过 MCP 端点统一暴露*。
2. 本服务核心需求"个人记忆 + 团队记忆 + 关联关系挖掘 + 重要度评估"恰好落在两家强项的并集：Mem0 的三级命名空间覆盖个人/Agent/会话隔离；Graphiti 的实体-事实边-社区三层子图天然支撑关联挖掘（Neo4j GDS 可直接在图上算 PageRank/社区检测）与时间点查询。
3. Letta 的"记忆即运行时"与平台铁律冲突（*平台不实现任何 Agent 能力，容器内唯一进程是 opencode serve*），迁入即运行时锁定。**采取"借鉴不引入"策略**：其 Core/Recall/Archival 分层映射为本服务的热记忆/会话记忆/归档记忆逻辑视图（见 03 文档），睡眠计算思想落地为画像引擎的空闲期批任务。
4. 风险对冲：Mem0 写入含 LLM 调用的成本问题，在架构层用**事件缓冲 + 批量提取**化解（02 文档 §3.1）；两引擎均经适配器封装，供应商风险可替换。

**加速器参考**：Mem0 官方开源的 **OpenMemory**（MIT）提供了"本地记忆 + MCP Server + React UI"的参考实现，其 MCP 工具面定义与 UI 骨架可作为本方案 MCP Server 与控制台的起步代码参考，但其**单用户本地定位**与多租户/团队/分析需求差距，正是本方案定制开发的空间。

---

## 2. 向量数据库：Qdrant vs Milvus vs Chroma vs pgvector

| 维度 | **Qdrant（选型）** | Milvus | Chroma | pgvector |
|------|------|--------|--------|----------|
| 语言/形态 | Rust，单二进制 | Go，分布式组件多（etcd/Pulsar/MinIO） | Python 嵌入式 | PG 扩展 |
| 多租户支持 | payload 过滤 + 多 collection，**成熟** | partition key / 多 collection | 弱（无原生租户概念） | 行级过滤（性能受限） |
| 过滤式召回性能 | HNSW + payload index，**强** | 强 | 一般 | 中（高维+过滤退化明显） |
| 运维成本 | **低** | 高（4+ 组件） | 最低 | 低（复用 PG） |
| 规模上限 | 亿级/集群 | 百亿级 | 千万级（单机） | 千万级 |
| Mem0 官方支持 | **一等 provider** | 支持 | 支持 | 支持 |
| 备份 | snapshot API → S3 | 组件级备份 | 文件复制 | pg_dump/WAL |

**决策：Qdrant。**
- 本服务目标规模（单组织 ≤ 10M 向量）远未达到 Milvus 的优势区间，其组件堆栈的运维成本不划算；Milvus 作为超大规模演进候补写入 roadmap。
- Chroma 定位嵌入式/原型，缺生产级租户与分布式能力，排除。
- pgvector 的诱惑是与平台 PG 共栈，但高维向量 + 复杂 payload 过滤场景性能弱于专用引擎，且 Qdrant 的 snapshot 备份更贴合"向量库独立容灾"需求。**pgvector 保留为 Phase 1 的最小验证选项**（若 MVP 数据量 < 50 万且不想新增组件），接口层经 Mem0 provider 抽象，切换成本受控。

---

## 3. 图数据库：Neo4j vs FalkorDB

| 维度 | **Neo4j（选型）** | FalkorDB |
|------|------|----------|
| Graphiti 支持 | **一等支持**（5.26+） | 支持（含嵌入式 lite） |
| 图算法生态 | **GDS 库**：PageRank、Louvain 社区检测、节点相似度——直接支撑关联挖掘与重要度评估 | 算法库弱 |
| 可视化生态 | Neo4j Browser/Bloom + 大量前端库适配 | 较少 |
| 运维 | 中（JVM）；社区版**无在线热备** | 低（Redis 协议） |
| 资源占用 | 高（建议 4C8G 起步） | 低 |

**决策：Neo4j Community + GDS。** GDS 的图算法是"分析功能（关联挖掘/重要度评估）"的关键生产力，FalkorDB 无对等能力。社区版无在线热备的短板用"低峰定时 `neo4j-admin dump` + 快照校验"补偿（05 文档 §5）。Graphiti 对 Kuzu 的支持已弃用，不予考虑。

---

## 4. 可视化方案：Grafana vs Superset vs 自研控制台

| 候选 | 擅长 | 在本场景的角色 | 结论 |
|------|------|----------------|------|
| Grafana + Prometheus | 时序指标、告警、运维大盘 | 服务 QPS/延迟/队列积压/存储水位等**运维可观测性** | ✅ 采纳（运维侧） |
| Superset | SQL 驱动的 BI 即席分析 | 管理员侧使用统计的即席查询（可选，Phase 2 评估） | ⭕ 可选 |
| **自研 React 控制台** | 记忆时间线、图谱交互探索、画像雷达、推荐管理 | **业务可视化主体** | ✅ 自研 |

**决策：业务控制台自研，Grafana 承担运维监控。**
理由：需求要求的"记忆数据流转、存储分布、使用频率、关联关系、画像"均为**业务语义视图**——需要与 memory/图谱/画像数据模型深度耦合的交互（点击图谱节点下钻记忆详情、画像维度雷达对比、记忆回放时间轴），Grafana/Superset 的面板模型无法承载；调研报告中 Letta ADE 被明确列为优势项（*"ADE 可视化调试：实时查看上下文窗口、记忆块、归档存储"*），证明记忆可视化是差异化能力而非附属品，值得自研投入。

自研技术栈：**React + TypeScript**（与平台 frontend 同栈，复用组件与团队技能）+ **AntV G6**（图谱布局/交互）+ **ECharts**（统计图表）+ **Ant Design**（基座组件）。

---

## 5. 推荐系统框架：TensorRec vs LightFM vs 自研两阶段

| 维度 | TensorRec | LightFM | **自研两阶段（选型）** |
|------|-----------|---------|------------------|
| 维护状态 | **已停滞**（TF1 时代，数年无实质更新） | 维护缓慢，Python 3.11+ 适配存疑 | 自主可控 |
| 冷启动 | 弱（需训练） | 支持 user/item features（混合模型） | 内容向量召回天然冷启动友好 |
| 可解释性 | 黑盒分解 | 部分可解释 | **每条推荐附证据链**（来源记忆/图谱路径/画像因子） |
| 数据要求 | 交互矩阵 | 交互矩阵 + 侧特征 | 记忆/画像即特征，无需独立交互收集 |
| 与记忆数据耦合 | 需另建特征管道 | 需另建特征管道 | 直接消费 Qdrant/Neo4j/画像 |

**决策：自研两阶段推荐（召回 → 排序 → 重排），LightFM 作为 Phase 3 可选增强。**
理由：① TensorRec 排除（停维护）；② 记忆推荐的核心是"相关 + 重要 + 及时 + 可解释"，检索式召回（画像向量 + 图谱近邻）+ 规则化加权排序（MMR 多样性重排）即可达到生产可用，且天然可解释——这与平台"无 mock、全链路真实"的工程文化一致；③ 待交互反馈数据积累后（点赞/采纳/忽略），再引入 LightFM 混合模型学习隐式偏好，作为排序层的旁路实验模型（shadow mode 灰度）。

---

## 6. 事件总线与缓存：Redis Streams vs Kafka

**决策：Redis 7 一石二鸟（缓存 + Streams 事件总线）。**
- 事件量级预估（10K 活跃用户）≈ 数百 events/s，远低于 Kafka 的经济门槛；Streams 的 Consumer Group 已满足"一事件多消费者（画像/推荐/分析）"语义。
- 接口层抽象 `EventBus`（publish/subscribe），Kafka 作为规模化演进候补（06 roadmap）。
- 缓存分层：客户端 SWR → Redis（检索结果 60s TTL、画像 5min TTL）→ Qdrant/Neo4j。详见 05 文档 §3。

---

## 7. 选型结论汇总表

| 层 | 组件 | 选型 | 版本基线 | 用法 | 定制程度 |
|----|------|------|----------|------|----------|
| 记忆引擎 | 个人/偏好记忆 | **Mem0 OSS** | ≥ 0.1.x（2026 主干） | Python 库嵌入 memory-core | 适配层（config/命名空间映射） |
| 记忆引擎 | 时序图谱 | **Graphiti** | ≥ 0.x 主干 | Python 库嵌入 memory-core | group_id 映射、本体定义 |
| 向量库 | 召回存储 | **Qdrant** | 1.12+ | Docker 部署 | 低 |
| 图数据库 | 图谱存储 | **Neo4j Community + GDS** | 5.26+ | Docker 部署 | 低 |
| 关系库 | 元数据/画像/审计/ACL | **PostgreSQL 16** | 平台已有 | 独立 database | 低（含 RLS 可选） |
| 缓存/总线 | 缓存 + 事件 | **Redis 7** | 7.4 | Docker 部署 | 低 |
| MCP | Agent 接入 | **自研 Memory MCP Server**（参考 OpenMemory/Graphiti-MCP） | — | 挂 agent-net | **全定制** |
| API 服务 | 记忆/画像/推荐/分析 | **自研 4 微服务**（FastAPI） | — | Docker 部署 | **全定制** |
| 前端 | 业务控制台 | **自研 React + G6 + ECharts** | — | 与平台 frontend 同栈 | **全定制** |
| 运维监控 | 指标/告警 | **Grafana + Prometheus** | — | 复用平台 | 低 |
| BI（可选） | 即席分析 | Superset | — | Phase 2 评估 | — |

**定制开发范围一览**（详细设计见 03 文档）：

| 定制模块 | 开源无法覆盖的原因 | 规模估计 |
|----------|--------------------|----------|
| memory-core 服务（Mem0/Graphiti 编排、批量提取管线、流式） | 两引擎是库不是服务，缺统一 API/批量/流式/租户治理 | 大 |
| 多租户网关与 ACL（个人/团队/组织三级 scope） | Mem0 命名空间无团队共享语义；Graphiti group_id 无 RBAC | 中 |
| profile-engine 画像引擎 | 无对等开源；借 Letta sleep-time compute 思想自研 | 大 |
| recommender 推荐服务 | TensorRec 停维护；LightFM 冷启动与可解释性不足 | 中 |
| analytics 分析服务（使用模式/重要度评估） | 重要度评估需融合检索频次+图谱中心性+时效的多因子模型，无现成实现 | 中 |
| Web 可视化控制台 | Grafana/Superset 不承载业务语义视图 | 大 |
| Memory MCP Server | OpenMemory 单用户定位，需多租户化 + 团队工具面 | 小-中 |
