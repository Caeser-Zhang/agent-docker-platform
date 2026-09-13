# 05 · 部署方案、性能与安全、备份容灾与运维建议

---

## 1. 部署拓扑

### 1.1 两档部署形态

| 档位 | 形态 | 适用 | 说明 |
|------|------|------|------|
| A | **docker-compose 融合部署** | 开发/POC/≤200 用户 | 新增 `docker-compose.memory.yml`，与平台 compose 共网（platform-net/agent-net），一键起停 |
| B | **Kubernetes**（Phase 3） | 生产/多组织 SaaS | 四微服务 Deployment + HPA；Qdrant Operator / Neo4j StatefulSet；PG 云托管；事件总线按规模评估 Kafka |

### 1.2 服务与资源规格（档位 A 基线，1000 活跃用户量级）

| 服务 | 副本 | CPU/Mem | 存储 |
|------|------|---------|------|
| memory-core | 2 | 1C/1G | — |
| Memory MCP Server | 2 | 0.5C/512M | — |
| profile-engine | 1（worker 可扩） | 1C/1G | — |
| recommender | 1 | 1C/1G | — |
| analytics | 1 | 1C/1G | — |
| Qdrant | 1（集群化备用） | 2C/4G | volume 50G（1M 向量 ≈ 2-4G，含余量） |
| Neo4j | 1 | 4C/8G（JVM heap 4G） | volume 100G |
| PostgreSQL（复用实例，独立 db） | 1 | — | volume 50G |
| Redis | 1 | 1C/2G（maxmemory 1.5G） | AOF volume 10G |
| MinIO（备份仓） | 1 | 0.5C/1G | volume 200G |

compose 片段（骨架，网段与平台既有约定一致）：

```yaml
services:
  memory-core:
    build: ./services/memory-core
    environment: [AGENT_DATABASE_URL, QDRANT_URL, NEO4J_URI, REDIS_URL, LLM_PROXY_URL]
    networks: [platform-net]
  memory-mcp:
    build: ./services/memory-mcp
    networks: [agent-net, platform-net]   # agent 容器可达 + 调用 core
  qdrant:
    image: qdrant/qdrant:v1.12
    volumes: [qdrant-data:/qdrant/storage]
    networks: [platform-net]
  neo4j:
    image: neo4j:5.26-community
    environment: [NEO4J_AUTH, NEO4J_PLUGINS='["apoc","graph-data-science"]']
    volumes: [neo4j-data:/data]
    networks: [platform-net]
```

## 2. 高并发与性能设计

### 2.1 容量目标（SLO）

| 指标 | 目标 |
|------|------|
| 检索 API p95（含三路召回+重排） | ≤ 300ms（缓存命中 ≤ 50ms） |
| 写入 ACK p99 | ≤ 100ms（异步提取不阻塞） |
| 提取管线端到端延迟（普通写入） | ≤ 90s；important 快路径 ≤ 10s |
| 单核吞吐 | 检索 ≥ 150 QPS/副本（1M 向量，k=10） |
| MCP `memory_context` p95 | ≤ 500ms / ≤ 1500 tokens |

### 2.2 多级缓存策略

| 层 | 内容 | TTL | 失效 |
|----|------|-----|------|
| L1 控制台/SDK | SWR 本地缓存 | 30s | — |
| L2 Redis | 检索结果（key=hash(query+租户谓词)）、画像、Core 块 | 60s / 300s / 随写失效 | 写事件主动失效（`memory.updated/deleted`） |
| L3 引擎层 | Qdrant 页缓存、Neo4j 页缓存 | — | 内存 |
| 穿透防护 | 空结果短 TTL(5s)、单飞(singleflight)合并并发同查询 | | |

### 2.3 其他性能要点

- **Embedding/提取批量**：批量接口一次 embedding 多条；提取批合并 LLM 调用（02 §3.1，预计降 LLM 成本 60%+）。
- **HNSW 调优**：m=16、ef_construct=128 起步；召回率压测 <95% 再升 ef；>10M 向量启用 scalar quantization（int8，内存 -75%）。
- **连接池**：PG pgbouncer（transaction 模式 + `SET LOCAL` 租户上下文）；Qdrant/Neo4j 各自客户端池。
- **Neo4j 写入**：episode 摄入串行 per group_id（Graphiti 语义要求），跨 group 并行；GDS 查询全部走只读副本路由。
- **SSE 扇出**：Redis Pub/Sub → 进程内扇出，单连接 10K 事件/用户上限。

## 3. 安全实现清单（与 02 §5 对应的落地项）

| 项 | 实现 |
|----|------|
| 传输加密 | nginx 终结 TLS1.3，HSTS；内网明文仅限 compose 桥（K8s 档启用 mesh mTLS） |
| 存储加密 | 宿主卷 LUKS/云盘 KMS；可选内容字段 AES-256-GCM（密钥经 env/KMS，`crypto.py` 模式复用平台实现） |
| 密钥管理 | 全部经 `.env`/secret 注入，禁止入库；MCP per-container 令牌短 TTL + 可吊销 |
| 权限 | JWT claims → 网关谓词 → 服务内 ACL 二次校验；RLS 兜底；团队 RBAC 矩阵（04 §2） |
| 越权测试 | CI 集成跨租户攻击用例集（伪造 org_id、IDOR、MCP 令牌跨容器重放） |
| 审计 | audit_log 全写操作 + 越权尝试，WORM 导出（月度打包至 MinIO，保留 1 年） |
| 数据合规 | 导出/删除 API（GDPR 式）；软删 30d 物理清除；备份同步覆盖 |
| Prompt 注入 | 提取管线输入与系统指令隔离（消息角色分离）；记忆内容注入检索结果时标记不可信来源 |

## 4. 备份与容灾

### 4.1 备份矩阵

| 存储 | 方式 | 频率 | 保留 | RPO/RTO |
|------|------|------|------|---------|
| PostgreSQL | WAL 归档（wal-g→MinIO）+ 每日全量 | 连续 WAL | 7 天 PITR + 4 周月档 | RPO ≤ 5min / RTO 30min |
| Qdrant | snapshot API 全 collection → MinIO；写入后异步触发增量快照（按事件窗口） | 每日 + 每 6h 增量 | 14 天 | RPO ≤ 6h / RTO 30min |
| Neo4j | `neo4j-admin database dump`（社区版，03:00 低峰停写窗口 <5min，经维护窗口切换） | 每日 | 14 天 | RPO ≤ 24h / RTO 1h |
| Redis | RDB 每小时 + AOF everysec | — | 3 天 | 缓存可重建；Streams 以消费位移 checkpoint 入 PG |
| 配置/镜像 | Git + registry | 每次变更 | 永久 | — |

> 说明：Neo4j Community 无在线热备是已知约束；若合规要求图谱 RPO < 24h，两个选项——升级单机付费版在线备份，或将团队图谱关键事实（事实边表）同步落 PG 作为恢复源（推荐，成本最低）。

### 4.2 容灾策略

- **单机故障**：compose `restart: unless-stopped` + 数据卷独立；K8s 档自动重调度。
- **降级运行**（02 §6）：Neo4j 宕机 → 纯向量检索降级；Redis 宕机 → PG 队列兜底提取，绕过缓存。
- **演练**：季度恢复演练（从 MinIO 快照重建全栈至隔离环境，校验记忆条数抽样一致 + 检索冒烟），演练报告归档审计。

## 5. 可观测性与运维建议

### 5.1 监控（Prometheus + Grafana，复用平台）

- **RED 指标**：各服务 QPS/错误率/延迟直方图；MCP 调用按工具维度。
- **管线指标**：`extraction_queue_lag`（Streams 积压）、`llm_tokens_total`（按 org，配额告警）、`dlq_size`、提取成功率。
- **业务指标**：日活记忆写入/检索用户数、检索命中率、推荐采纳率、画像新鲜度（snapshot 距今时长）。
- **告警（建议基线）**：提取积压 >5K 持续 10min；dlq 增长；检索 p95 >500ms；Neo4j heap >85%；任一备份任务失败。

### 5.2 运维手册要点

1. **升级策略**：微服务滚动重启（无状态）；Qdrant snapshot 先行；Neo4j 升级走 dump/restore；Mem0/Graphiti 依赖升级先在影子环境跑回归评测集。
2. **成本监控**：LLM token 按租户日账单（usage_daily），org 配额自动限流；存储水位月报。
3. **数据生命周期**：importance < 0.15 且 180d 未访问 → 进入低频层（可转 pgvector 冷表），控制台可见。
4. **记忆评测例程**：每月用真实对话抽样跑自建召回评测集（报告第 06 节结论：*run your own recall eval*），分数进 Grafana 趋势，防引擎升级回归。
5. **Runbook 目录**：Qdrant 快照恢复、Neo4j dump 恢复、Streams 重放（按 offset 回放至幂等消费者）、租户迁移（导出某 org 全量 → 导入新环境）。

### 5.3 成本效益摘要（1000 活跃用户/月量级估算）

| 项 | 量级 | 备注 |
|----|------|------|
| 基础设施 | 自托管 4C8G×2 节点起步 | 全开源零 License 费用 |
| LLM 提取 | 主要可变成本 | 批量管线 + 小模型（4o-mini 级）；千次写入 ≈ 数十万 token |
| 对比 SaaS | Mem0 Pro $249/月起（图功能锁档）、Zep Cloud $25/月起/用户 | 自托管在 >50 用户后显著占优，且数据自主可控 |
