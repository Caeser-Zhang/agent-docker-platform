# 04 · 数据模型设计与 API 接口文档

---

## 1. 数据模型

### 1.1 PostgreSQL（元数据主库，db: `agent_memory`）

```sql
-- 组织与团队（与平台用户体系对接，此处为记忆域侧的影子表）
CREATE TABLE orgs    (id TEXT PRIMARY KEY, name TEXT NOT NULL, quota_json JSONB, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE teams   (id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs, name TEXT NOT NULL);
CREATE TABLE team_members (team_id TEXT, user_id TEXT, role TEXT CHECK (role IN ('owner','admin','editor','viewer')),
                           PRIMARY KEY (team_id, user_id));

-- 记忆主表（结构化元数据；内容与向量在 Qdrant，事实边在 Neo4j）
CREATE TABLE memories (
  id            TEXT PRIMARY KEY,            -- mem_<ulid>
  org_id        TEXT NOT NULL,
  scope         TEXT NOT NULL CHECK (scope IN ('personal','team','org')),
  owner_id      TEXT NOT NULL,               -- user_id；team scope 时= team_id
  team_id       TEXT,                        -- scope=team 时非空
  agent_id      TEXT, run_id TEXT,           -- 产生记忆的容器/会话（Mem0 命名空间对齐）
  content       TEXT NOT NULL,               -- 记忆正文（Mem0 提取后的事实句）
  content_type  TEXT NOT NULL DEFAULT 'text',-- text|json|markdown
  tags          TEXT[] DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|extracted|failed|deleted
  importance    REAL DEFAULT 0,              -- 03 §4.3 多因子分
  source        TEXT NOT NULL DEFAULT 'agent',    -- agent|user_explicit|system|import
  extraction_id TEXT,                        -- 双引擎关联键
  version       INTEGER NOT NULL DEFAULT 1,  -- 乐观锁
  valid_from    TIMESTAMPTZ DEFAULT now(),   -- 借鉴 Graphiti 双时间轴
  valid_to      TIMESTAMPTZ,                 -- 失效时间（NULL=生效）
  deleted_at    TIMESTAMPTZ,                 -- 软删除（30d 后物理清除）
  metadata      JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_mem_tenant ON memories (org_id, scope, owner_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_mem_valid  ON memories (org_id, valid_to);
CREATE INDEX idx_mem_tags   ON memories USING gin (tags);
-- 可选 RLS 兜底：ALTER TABLE memories ENABLE ROW LEVEL SECURITY; 策略按 current_setting('app.org_id')

-- 原始数据层（Graphiti Episode 对应，可溯源）
CREATE TABLE episodes (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, source_type TEXT,  -- message|file|api
  raw_content TEXT, memory_ids TEXT[] DEFAULT '{}', created_at TIMESTAMPTZ DEFAULT now());

-- 用户画像快照（版本化）
CREATE TABLE profiles (
  id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL, user_id TEXT NOT NULL,
  ver INTEGER NOT NULL,
  interest_tags JSONB,        -- {tag: weight}
  interest_vec JSONB,         -- 画像向量（同步至 Qdrant profiles collection）
  abilities JSONB,            -- [{name, score, confidence, evidence: [memory_id]}]
  behavior JSONB,             -- 时段直方图/检索模式/Agent 偏好
  computed_at TIMESTAMPTZ DEFAULT now(), trigger TEXT);  -- realtime|nightly|idle
CREATE UNIQUE INDEX uq_profiles ON profiles (org_id, user_id, ver);

-- Core 热记忆块（Letta 式 Core Memory）
CREATE TABLE core_blocks (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, user_id TEXT NOT NULL,
  label TEXT NOT NULL,        -- persona|preferences|tasks|custom
  content TEXT NOT NULL, byte_len INTEGER, version INTEGER DEFAULT 1,
  updated_by TEXT, updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, label));

-- 统计与审计
CREATE TABLE usage_daily (org_id TEXT, user_id TEXT, day DATE, writes INT, searches INT,
                          hit_rate REAL, llm_tokens INT, PRIMARY KEY (org_id, user_id, day));
CREATE TABLE audit_log (
  id BIGSERIAL PRIMARY KEY, org_id TEXT, actor TEXT, action TEXT, resource TEXT,
  before JSONB, after JSONB, ip INET, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE dlq (id BIGSERIAL PRIMARY KEY, event JSONB, error TEXT, created_at TIMESTAMPTZ);
```

### 1.2 Qdrant collections

| collection | 向量 | payload 关键字段 | 索引 |
|------------|------|------------------|------|
| `memories` | embedding (模型维度, cosine) | `org_id, scope, owner_id, team_ids[], tags[], importance, valid_to, deleted_at` | HNSW(m=16, ef=128) + payload index(org_id, scope) |
| `profiles` | 画像向量 | `org_id, user_id, ver` | HNSW |

租户谓词示例（memory-core 强制拼接）：

```json
{"must": [{"key":"org_id","match":{"value":"o_1"}},
          {"key":"scope","match":{"any":["personal","team","org"]}},
          {"key":"deleted_at","match":{"value":null}}]}
```

### 1.3 Neo4j / Graphiti 图模型（引擎管理 + 本体定制）

- **Episode 节点**：原始数据溯源层；**Entity 节点**：`Person/Project/Tech/Tool/Team/Concept`（Pydantic 本体，含 `group_id`）；**RELATES_TO 事实边**：`fact, valid_at, invalid_at, episodes[]`；**Community 节点**：GDS Louvain 产出 + LLM 摘要。
- `group_id` 约定：`{org_id}:{u|t}{id}`，个人图谱与团队图谱分域，团队图谱由成员个人图谱白名单事实聚合生成（治理审批）。

### 1.4 Redis 键规划

| 键 | 用途 |
|----|------|
| `o:{org}:cache:search:{hash}` | 检索结果缓存 TTL 60s |
| `o:{org}:profile:{user}` | 画像热缓存 TTL 300s |
| `o:{org}:core:{user}` | Core 块缓存 |
| `stream:memory.events` | 全局事件流（payload 含 org_id） |
| `stream:dlq` | 死信 |

**事件 schema**（v1）：`{schema_ver:1, event_id, type: created|extracted|updated|invalidated|deleted|profile_rebuilt|rec_feedback, org_id, payload{...}, ts}`

## 2. REST API 规范（OpenAPI 3.1）

**全局约定**：Base `https://<host>/mem/v1`；认证 `Authorization: Bearer <JWT>`（容器 MCP 走内部令牌）；错误格式 RFC 9457 `application/problem+json`；写接口支持 `Idempotency-Key` 头；分页 `cursor` 式（`?limit=50&cursor=...`）；全响应含 `X-Request-Id`。

### 2.1 记忆 CRUD

```
POST   /memories                     写入（202；body: content, scope, team_id?, tags?, important?, metadata?）
POST   /memories/batch               批量写入（≤500 条/批，返回逐条结果）
GET    /memories/{id}                详情（含溯源 episodes、图谱边引用）
PATCH  /memories/{id}                更新（If-Match: version 乐观锁）
DELETE /memories/{id}                软删除（query: ?reason=）
GET    /memories                     游标列表（filter: scope/team/tags/时间/importance_min）
```

**写入响应示例**：

```json
HTTP/1.1 202 Accepted
{
  "id": "mem_01J9ZK...", "status": "pending",
  "extraction": {"mode": "batch", "eta_seconds": 25},
  "links": {"self": "/mem/v1/memories/mem_01J9ZK...", "events": "/mem/v1/events?memory_id=mem_01J9ZK..."}
}
```

### 2.2 检索与上下文

```
POST   /search        {query, scope?, team_ids?, tags?, k=10, rerank="rrf", with_graph=true}
POST   /context       高阶：为任务组装上下文包 {task, token_budget=1500, include_profile=true}
GET    /timeline      ?from=&to=&scope=&tags=    时间线（含事件类型聚合）
```

`/search` 响应（融合三路，可解释）：

```json
{
  "results": [{
    "memory_id": "mem_01J9...", "content": "用户偏好 TypeScript 而非 Python",
    "score": 0.87, "score_breakdown": {"semantic": 0.81, "bm25": 0.79, "graph": 0.92},
    "graph_edges": [{"fact": "prefers(TS, over Python)", "valid_at": "2026-08-01", "invalid_at": null}],
    "provenance": {"episode_id": "ep_...", "source": "agent"}
  }],
  "took_ms": 142, "cache": "miss"
}
```

### 2.3 画像

```
GET  /profiles/{user_id}                 快照（本人或 viewer+ 权限）
GET  /profiles/{user_id}/history         ?metric=interests|abilities（演化回放）
POST /profiles/{user_id}/feedback        画像纠错（用户显式修正，高优先级事件）
```

### 2.4 推荐

```
POST /recommendations      {scene: "recall|relate|growth|decision", k=5, context?}
POST /recommendations/{id}/feedback    {action: accepted|dismissed|adopted}
```

推荐响应每项含 `evidence[]`（来源记忆/图谱路径）与 `score_breakdown`（03 §3.2 因子分解）。

### 2.5 分析与管理

```
GET  /analytics/usage        ?dim=org|user|agent&metric=writes|searches|tokens（日粒度）
GET  /analytics/graph/insights    {top_entities, communities, similar_users}
GET  /analytics/importance        ?user_id=   重要度分布与 Top-N
GET  /admin/audit                 审计日志（admin）
GET/PUT /admin/teams/{id}/members 配额与角色（admin）
```

### 2.6 实时流（SSE）

```
GET /events?topics=memory.*,profile.updated,rec.feedback&memory_id=...
→ data: {"event_id":"...","type":"memory.extracted","payload":{"id":"mem_...","importance":0.72}}
心跳 15s；断线 `Last-Event-ID` 续传（Redis Streams offset 映射）。
```

## 3. MCP 工具接口（Agent 容器接入）

stdio/HTTP MCP Server 暴露（完整 schema 见 03 §6）：`memory_search` / `memory_add` / `memory_get` / `memory_update` / `memory_delete` / `memory_timeline` / `memory_context` / `profile_get` / `recommendations_get` / `team_search`。

`memory_context` 响应（token 预算受控的上下文包）：

```json
{
  "core_blocks": {"preferences": "TS > Python; 邮件联系"},
  "memories": [{"id": "mem_...", "content": "...", "importance": 0.8}],
  "graph_facts": [{"fact": "uses(React 19)", "valid_at": "2026-07-01"}],
  "profile_hints": {"top_interests": ["agent-memory", "k8s"]},
  "token_estimate": 980
}
```

## 4. 接入示例

### 4.1 Agent 容器（MCP manifest，仿 web_search 先例）

```json
{
  "name": "memory",
  "transport": "stdio",
  "command": ["python", "/opt/mcp/memory_mcp.py"],
  "env": {
    "MEMORY_API_URL": "http://memory-mcp.agent-net:8140",
    "MCP_TOKEN": "${AGENT_MEMORY_TOKEN}"
  }
}
```

Agent 内自然语言即可触发：*"记住这个项目用 pnpm 而不是 npm"* → `memory_add(important=true)`。

### 4.2 服务端接入（Python）

```python
import httpx

API = "https://mem.example.com/mem/v1"
H = {"Authorization": f"Bearer {jwt}", "Idempotency-Key": "ord-20260913-001"}

r = httpx.post(f"{API}/memories", headers=H, json={
    "content": "生产环境Postgres迁移定于9月20日窗口", "scope": "team",
    "team_id": "t_a", "tags": ["ops", "deadline"], "important": True})
mem_id = r.json()["id"]

hits = httpx.post(f"{API}/search", headers=H, json={
    "query": "下次数据库维护是什么时候？", "k": 5, "with_graph": True}).json()
```

### 4.3 批量导入（历史对话回填）

```bash
curl -X POST $API/memories/batch -H "Authorization: Bearer $JWT" \
  -d '{"items":[{"content":"...","scope":"personal","source":"import","created_at":"2026-01-01T00:00:00Z"}, ...]}'
# ≤500条/批；created_at 保留原始双时间轴逻辑时间（T），系统时间 T' 自动记录
```

### 4.4 错误码

| 码 | 场景 |
|----|------|
| 400 | schema 校验失败 / scope 非法 |
| 401/403 | 令牌无效 / 越权（租户谓词不匹配，审计记录） |
| 404 / 410 | 记忆不存在 / 已软删 |
| 409 | version 乐观锁冲突 |
| 429 | 限流或提取队列背压（含 Retry-After） |
| 503 | 存储降级（如 Neo4j 不可用，响应头 `X-Degraded: graph`） |
