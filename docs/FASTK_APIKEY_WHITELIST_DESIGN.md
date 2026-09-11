# fastk 知识库 API Key 白名单管控方案

> 目标：平台在**不修改 fastk 服务端**的前提下，按白名单管控每个用户可访问的知识库；Key 全程不暴露给用户；未授权访问时给用户明确引导。
>
> v4 修订：根据用户决策定稿——① **fastk-mcp 整体下线**（能力与 CLI 冗余，且是无用户上下文的共享直连绕过缺口）；② **不部署防火墙**（威胁模型为内部平台权限管理，非对抗隔离；改为"已知限制 + 配套缓解"，防火墙方案降级为附录可选项）；③ 凭据由 admin 手工同步；④ 403 文案带管理员信息（默认张智骁 / 00899219，**联系人名单为配置项，可修改**）；⑤ **知识库发现与选库链路**（`/databases/` 改过滤式保留服务端元数据 + SKILL.md 动态化 + 403 建设性引导），解决"agent 怎么知道要搜哪个库"；⑥ **补第三条通路**——前端引用徽章 `/api/fastk/chunk`（原缺口 C，现状只校验登录不查白名单）纳入同一份 kb_grants 判定与 Key 注入；第 4 节改为覆盖阶段 0~4 的端到端工作流；⑦ **Key 粒度校正**——服务端 Key 是逐库凭据、顶层目录端点 `GET /databases/` 免 Key，故代理拉目录不注入 Key、`/databases/` 过滤定位为发现层体验而非保密边界，并确立"token 绝不透传上游"实现铁律（现状 17 / 5.4 第 0 步 / 安全考量 7-8）。

---

## 1. 背景与约束

- fastk 服务（宿主机 `fastdb serve fastapi`，`/fastk/api` 前缀）已有知识库数据管理，**包含 key 与知识库的对应关系**（服务端配置层面）。
- **服务端不能随意修改**：平台不能依赖服务端新增接口或行为变更。实测服务端当前对请求**不做任何 Key 校验**（无 Key 与伪造 Key 均返回 200）。
- 推论：**白名单的强制点必须落在平台侧（backend 代理层）**；服务端将来若自行启用 Key 校验，平台注入 Key 的机制已就绪、无缝衔接，但方案不以它为前提。

## 2. 现状梳理（关键事实）

| # | 事实 | 对方案的影响 |
|---|------|------|
| 1 | CLI 读 `FASTK_API_KEY` env 发 `X-API-Key` 头；服务地址由 `FASTDB_BASE_URL` 决定（无 `--remote`/`--api-key` 参数） | 路线 2 可将两者分别替换为"代理地址"与"代理 token"，**CLI 零改动** |
| 2 | CLI 的 `resolve_db()` 先把逻辑名经 `FASTK_DB_MAP` 解析为物理名，再拼进请求路径 | 代理按**物理库名**查白名单即可，平台数据模型无需逻辑名字段 |
| 3 | CLI 错误处理已解析响应体 `error.message` 并打印（`fastk: HTTP 403 ...: <message>`） | 代理返回友好 403 文案，CLI **零改动**即可呈现给 agent → 前端对话 |
| 4 | `AgentController.start_for_user(user_id)` 由 JWT 获得 user_id；`_build_run_kwargs` 的 env 注入点现成 | token 注入链路零新增机制 |
| 5 | backend 已挂 agent-net，agent 容器经 `backend` DNS 名访问 backend（llm-proxy 即此模式：`http://backend:8000/llm-proxy/...`） | 代理通路的网络前提已具备 |
| 6 | backend 对宿主机无端口映射，frontend nginx 只转发 `/api/` 前缀 | 代理挂在 `/fastk/api/` 下**不暴露到平台外部** |
| 7 | backend → fastk 服务走 `host.docker.internal:8000`（`extra_hosts: host-gateway` 已配置） | 代理转发 upstream 通路已具备 |
| 8 | 密钥加密有现成模式：`UserLLMProvider.api_key_enc` 用 Fernet（`app/crypto.py`，`AGENT_SECRET_KEY` 派生） | Key 与 token 的加密直接复用 |
| 9 | 项目无 Alembic：`create_all` 自动建新表，给已有表加列需手写 ALTER | 极简模型的两张新表零迁移成本 |
| 10 | **直连路径（原缺口 A）**：agent 容器配了 `extra_hosts: host-gateway`（container_manager L389）。该配置**不可移除**——opencode_config 的回环地址重写（L624 `_walk`）让 agent 容器直连宿主机上的本地 LLM 服务（平台"本地回环 Provider"特性），删除会破坏该特性 | 直连路径无法根除 → 定位为**已知限制**（应用层白名单，非网络隔离），不部署防火墙（见 5.6） |
| 11 | **fastk-mcp 与 CLI 能力冗余（原缺口 B）**：7 个工具中 6 个与 CLI 调同一批 REST 端点（search/query/grep/list_files/get_stats/toc），CLI 反而多 instructions/count/alias 三个命令；MCP 独有能力仅 search 的 funnel+diversity 增强（多远程融合在当前 TOML 中 4 库全指向同一地址，未实际使用）；无用户上下文、无 Key 校验（TOML 可配 per-db key 但未配，且配了也是平台共享 Key） | **整体下线**（见 5.6），比"摘除受控库"更彻底：消除绕过缺口、消除 TOML 清单与服务端真实库清单的漂移、少一个常驻服务 |
| 12 | **技能文档泄露直连地址**：`fastk-search/SKILL.md` L9-10 明文写着 fastk 服务"经 `host.docker.internal:8000` 可达"——这是 agent 学习绕过路径的**内生来源**（提示注入可诱发直连） | 不部署防火墙的配套缓解：P0 **必须**清理该文案（agent 容器内可见的文档不得出现直连地址） |
| 13 | fastk-search / fastk-analyze 技能一律用 `--db <逻辑名>` 调 CLI | 技能层零改动，自动走代理 |
| 14 | **SKILL.md 静态库名表会漂移**：fastk-search SKILL.md L15-22 写死 global/vl_test 静态表——白名单下表里的库可能未授权、表外的库可能已授权；但工作流第 1 步"探库"（`fastk databases`）已是现成的动态发现入口 | SKILL.md 动态化改造：移除静态表，"可用库以 `fastk databases` 实时结果为准"（P0 ⑤） |
| 15 | CLI 的 `databases` 命令直接透传 `GET /databases/` 的 JSON；`instructions` 命令可逐库读服务端配置的使用说明 | 服务端本就是"库发现 + 库说明"的管理面；代理对探库做**过滤**而非重构即可（见 5.4） |
| 16 | **前端引用徽章是第三条通路（原缺口 C）**：`backend/app/routers/fastk.py` 的 `/api/fastk/chunk`、`/api/fastk/chunk-image` 供前端点击 `[[chunk:db/id]]` 徽章取回 chunk 全文与图片，直连 `settings.fastk_server_url`；仅 `Depends(get_current_user)` 校验登录，**不校验该用户是否有权访问该 db**，也不注入任何 Key | 必须补白名单校验（见 5.6 缺口 C）；同时补 Key 注入以保持"所有平台→服务端流量都带 Key"的一致性（服务端将来启用校验时无需再改） |
| 17 | **Key 是逐库凭据，顶层目录端点免 Key**：MCP client 的 `RemoteFastDB` 按库构造（`db_name` + 该库 `api_key`，client.py L26-43），所有方法都打 `/databases/{name}/...`，无顶层列表调用（`list_databases` 读本地 TOML）；CLI 的 `request()`（L85-92）对所有请求都挂 `X-API-Key`，包括 `cmd_databases` 打的顶层 `GET /databases/`——CLI 会发 key，但服务端目录端点不要求 key | 印证"授权单位是库"与服务端语义一致（5.1）；代理拉取上游目录**无需 Key**（5.4）；同时确立实现铁律：**代理必须清洗掉请求中的 token 头，绝不把 token 透传给上游**（服务端将来启用校验时 Fernet blob 会被判非法） |

## 3. 核心结论

**选定路线 2：后端代理（Key 不出平台）**，强制力由三部分组成——**所有通往 fastk 服务的通路都必须收敛到平台侧鉴权**：

1. **代理层白名单**（backend，核心）：CLI 请求全部经 backend 代理，按 token 定位用户、按物理库名查授权——未授权直接 403，根本到不了 fastk 服务；
2. **fastk-mcp 下线**（部署项）：消除无用户上下文的共享直连通道，agent 访问知识库的唯一通路收敛为 CLI → 代理；
3. **引用徽章通路补校验**（P0 ⑥）：前端点击 `[[chunk:db/id]]` 徽章取回 chunk 全文/图片走 `routers/fastk.py`，现状只校验登录、不查白名单——必须补 `kb_grants` 校验与 Key 注入，否则白名单在"结果溯源"环节失效（见 5.6 缺口 C）。

通路 1 与 3 共用同一套判定与解密逻辑（`kb_access.resolve_access(db, kb_name, user_id)`，落地签名见 §10.4），权限判定不分叉。

**定位声明**：本方案是**应用层访问控制**（所有正常路径收敛到代理），不是网络隔离。已知限制见 5.6——agent 理论上可经 `host.docker.internal` 直连宿主机 8000 绕过代理；接受该残留风险的依据是威胁模型（内部平台、员工用户、权限管理而非对抗隔离），且配套缓解已清除 agent 可见文档中的直连地址。若将来需要强隔离（如引入外部租户、知识库含高敏数据），按附录 A 启用网络封堵。

在此结构下：Key 只存在于平台 DB（密文）与 backend 内存中；容器内只有一个"代理 token"（加密编码的 user_id，泄露不构成权限升级）；服务端将来启用 Key 校验时平台无需再改。

## 4. 端到端工作流（从用户提问到结果展示）

### 阶段 0：权限管理（前置，管理员操作，与用户请求解耦）

```
管理员 → Admin API（require_admin）
   ① PUT  /api/admin/kb-keys/aicode  {"api_key":"sk-..."}   ← 手工同步服务端已配置的 key
   │        存 kb_keys(kb_name, api_key_enc=Fernet 密文)
   ② POST /api/admin/kb-grants       {"uid":"U","kb_name":"aicode"}
   │        存 kb_grants(user_id, kb_name)
   ③ log_audit("kbgrant.grant")
   ▼
平台 DB：kb_keys（库凭据，代理注入用）+ kb_grants（白名单，判定用）
```

授权/凭据变更**即时生效**（backend 每请求实时查 DB），用户容器无需 recreate。

### 阶段 1：用户提问 → agent 探库选库

```
用户（浏览器）："帮我查一下认证网关的超时重试策略"
   │  POST /api/agent/...（JWT 标识用户 U）
   ▼
backend → agent-{U} 容器（opencode serve，agent-net）
   │  SSE 事件经 sse_pump 回传前端
   ▼
agent（LLM）读 fastk-search 技能 → 工作流第 1 步【探库，必做】
   │  $ fastk databases
   │     CLI: GET http://backend:8000/fastk/api/databases/
   │          X-API-Key: <token = Fernet("kbproxy:U")>
   │          （CLI 对所有请求都挂该头；服务端目录端点本身免 Key，
   │            token 在这里只用于让代理知道"该为谁过滤"）
   ▼
backend 代理：token → user U → 拉上游全量目录（免 Key）→ 按 kb_grants 过滤
   │  返回：[{name:"fastdb",desc:...},{name:"aicode",desc:...}]   ← U 的授权范围
   ▼
agent 对照元数据按主题选库（必要时 `fastk instructions --db aicode` 读库说明）
   → 判定该查 aicode
```

**Key 的粒度（现状 17）**：服务端的 Key 是**逐库凭据**——顶层 `GET /databases/` 目录端点免 Key，只有 `/databases/{name}/...` 的读库操作（search/query/grep/files/stats/toc/instructions）才涉及 Key。因此：

- 代理拉上游目录**不注入任何 Key**（伪代码第 2 步）；
- 目录过滤是**发现层体验措施，不是保密边界**：库名与描述在服务端本就是免 Key 可枚举的，"agent 只见授权库"的价值在于避免选错库与 403 往返，而非隐藏库名；真正的边界是阶段 2 的逐库授权判定；
- 平台数据模型"一库一 Key"（`kb_keys` 以 kb_name 为主键）与服务端逐库 Key 语义天然一致。

### 阶段 2：检索执行（Key 权限管理与注入在此发生）

```
agent 执行：$ fastk search --db aicode "认证网关 超时重试"
   │  CLI resolve_db(): 逻辑名 aicode →（FASTK_DB_MAP）→ 物理名 aicode
   │  POST http://backend:8000/fastk/api/databases/aicode/search
   │       X-API-Key: <token>        ← 容器内只有 token，没有真实 Key
   ▼
backend 代理（kb_proxy）
   ├─ ① 解 token → user U（无效 → 401）
   ├─ ② 路径解析物理库名 = aicode
   ├─ ③ 查 kb_grants(U, aicode)
   │      ✗ 无授权 → 403 {"error":{"message":
   │            "无权限访问知识库 'aicode'。可运行 fastk databases 查看你当前可访问的
   │             知识库；如需开通 'aicode'，请联系管理员张智骁（工号 00899219）。"}}
   │      ✓ 有授权 → 继续
   ├─ ④ 查 kb_keys(aicode) → Fernet 解密得真实 Key（仅在 backend 内存中）
   ├─ ⑤ 注入 X-API-Key: <真实 Key> → 转发
   │      http://host.docker.internal:8000/fastk/api/databases/aicode/search
   ▼
fastk 服务 → 返回检索结果 JSON（chunks：chunk_id/text/path/section/score/…）
   ▼
backend 透传响应 → CLI 打印 JSON → agent 获得工具输出
```

### 阶段 3：结果使用与汇总（agent → 前端）

```
agent 阅读检索结果 → 组织回答，对每个引用的 chunk 加规范化标记：
   "混合检索默认采用 DBSF 融合，RRF 为可选方案。[[chunk:aicode/fb62184133c0c818]]"
   │  · 库名用物理名，chunk_id 原样复制（前端正则 [A-Za-z0-9_-]{1,64}）
   │  · 分数只用于排序判断，不向用户展示
   ▼
SSE 流式回传 backend → 前端 Chat.tsx
   ▼
TextWithChunkRefs（ChunkRef.tsx）解析标记 → 渲染为行内可点击徽章
用户看到：带引用徽章的结论 + 原文可溯源
```

### 阶段 4：引用溯源（点击徽章取 chunk 全文）——需补白名单

```
用户点击徽章 → GET /api/fastk/chunk?db=aicode&chunk_id=fb62...（JWT）
   ▼
backend routers/fastk.py
   ├─ 现状：仅校验登录，db 是自由参数 → 【缺口 C：可读任意库的 chunk】
   └─ 方案：查 kb_grants(U, aicode) → 无授权 403；有授权则注入真实 Key 后转发
   ▼
返回 chunk 全文 + images[] → ChunkViewer 模态展示（图片经 /api/fastk/chunk-image 中转）
```

### 三条通路的收敛结果

| 通路 | 入口 | 白名单强制点 | 状态 |
|------|------|------------|------|
| A. agent 检索 | 容器内 CLI → `/fastk/api/{path}` | 代理路由（token→用户→查授权→注入 Key） | P0 ③ 新建 |
| B. fastk-mcp | agent 原生 MCP 工具 | — | P0 ④ 整体下线（通路消失） |
| C. 引用溯源 | 前端徽章 → `/api/fastk/chunk` | 同一份 kb_grants + kb_keys | P0 ⑥ 补校验（见 5.6） |

A 与 C 共用同一套数据模型与解密注入逻辑（已抽 `kb_access.py`：`resolve_access(db, kb_name, user_id) -> KbAccess(granted, api_key)` + `granted_kbs(...)`，两处调用，落地签名见 §10.4），避免权限判定逻辑分叉。

## 5. 详细设计

### 5.1 数据模型（极简两张表）

服务端已有"key ↔ 知识库"的管理数据，平台侧**不重复建模这个关系的管理面**，只存代理转发所需的**最小必要副本**（库名 + 密文凭据）——代理注入 Key 时必须在平台侧解出"该库用什么 Key"，这份数据无法省略；逻辑名、描述、指纹、uuid 主键、双时间戳等字段全部砍掉（逻辑名职责已由 CLI 的 `FASTK_DB_MAP` 承担，见现状 2）：

```python
class KbKey(Base):
    """库凭据：服务端已配置的 key 的最小副本，仅供代理注入。"""
    __tablename__ = "kb_keys"
    kb_name: Mapped[str] = mapped_column(String(100), primary_key=True)  # 物理库名
    api_key_enc: Mapped[str] = mapped_column(Text)                        # Fernet 密文
    created_at: Mapped[datetime]                                          # 单时间戳，审计用

class KbGrant(Base):
    """白名单：用户 ↔ 知识库（授权单位是库而非 key，贴近服务端模型与管理员心智）。"""
    __tablename__ = "kb_grants"
    user_id: Mapped[str]   # FK users.id, CASCADE
    kb_name: Mapped[str]   # FK kb_keys.kb_name, CASCADE
    created_at: Mapped[datetime]
    __table_args__ = (UniqueConstraint("user_id", "kb_name", name="uq_kb_grant"),)
```

说明：

- 授权"库 aicode" = 授权使用 aicode 的 key，语义等价于"授权 key 实体"，但少了中间实体；**这一粒度与服务端一致**——服务端的 Key 本就是逐库凭据（现状 17：目录端点免 Key，只有 `/databases/{name}/...` 读库操作涉及 Key），MCP client 也按库持有各自 `api_key`；
- Key 轮换 = `UPDATE kb_keys WHERE kb_name=?` 单点更新，授权关系不动；
- 删除库凭据级联清理该库全部授权（FK CASCADE）；
- Key 值本身当前不参与服务端鉴权（服务端不校验），是"服务端将来启用校验时"的预置数据；**同步责任在 admin（已确认：服务端改 key 后手工同步平台，将来服务端若暴露管理 API 再自动同步，P2）**。

### 5.2 管理端 API（backend/app/routers/kb_keys.py，main.py 注册）

挂 `require_admin`，两个平铺资源（比嵌套子资源更简单）：

| 端点 | 说明 |
|------|------|
| `GET /api/admin/kb-keys` | 库凭据清单（仅库名 + 是否已录入，**不回显明文**） |
| `PUT /api/admin/kb-keys/{kb_name}` | 录入/轮换该库凭据 `{api_key}` |
| `DELETE /api/admin/kb-keys/{kb_name}` | 删除（级联删该库授权） |
| `GET /api/admin/kb-grants` | 授权矩阵（按库或按用户过滤） |
| `POST /api/admin/kb-grants` | `{username 或 uid, kb_name}` 授权（凭据未录入时报错提示先录入） |
| `DELETE /api/admin/kb-grants/{user_id}/{kb_name}` | 取消授权 |

配套：

- 写操作全部 `log_audit`（`kbkey.rotate` / `kbgrant.grant` / `kbgrant.revoke`）；
- 用户侧只读 `GET /api/kb/my-databases`：返回自己被授权的库名列表（**事前展示**，供前端 ConfigPanel 与技能提示使用，不返回 Key）。

### 5.3 注入侧（container_manager / agent_controller）

`_build_run_kwargs` 的 environment 修改两行：

```python
"FASTDB_BASE_URL": "http://backend:8000",       # 原 host.docker.internal:8000，改指向代理
"FASTK_API_KEY":  issue_proxy_token(user_id),   # Fernet 加密 "kbproxy:{user_id}"，非真实 Key
```

- `issue_proxy_token` = `crypto.encrypt_secret(f"kbproxy:{user_id}")`——无状态、不可伪造、无需持久化；吊销 = 轮换 `AGENT_SECRET_KEY`（全量失效）或 P1 引入 per-user 版本号；
- 注意：已存在容器的 env 不变，token 注入随 create/recreate 生效；`AGENT_SECRET_KEY` 轮换后旧 token 全失效，需 recreate 全部容器（平台 secret 正常不轮换，可接受）；
- `extra_env` 机制本身不需要——这两项都是平台级注入（所有容器一致地指向代理），直接改 `_build_run_kwargs` 即可，无需按用户透传（token 从 `user_id` 参数生成，`_build_run_kwargs` 已持有）。

### 5.4 代理侧（backend/app/routers/kb_proxy.py）

```python
@router.api_route("/fastk/api/{path:path}", methods=["GET", "POST"])
async def kb_proxy(path: str, request: Request, db=Depends(get_db)):
    # 0. 【铁律】无论走哪个分支，转发上游前必须丢弃请求带来的 X-API-Key
    #    （CLI 对所有请求都挂 token 头）——token 是平台内部凭据，
    #    绝不能透传给 fastk 服务，否则服务端将来启用校验时会被判非法
    # 1. token = request.headers["X-API-Key"]
    #    Fernet 解密 → "kbproxy:{user_id}"（无效/缺失 → 401）
    # 2. 若 path 是顶层 "databases/"（探库/目录请求）：
    #    拉取上游 GET /databases/ 全量目录 → 按该用户 kb_grants 过滤后返回。
    #    · 目录端点服务端免 Key，此处【不注入 Key】（现状 17）
    #    · 过滤式而非构造式：保留服务端列表项自带的元数据（描述等），
    #      agent 探库既见授权范围内的库，又拿到选库所需的语义信息
    #    · 定位：发现层体验措施（减少选错库与 403 往返），非保密边界
    # 3. 否则从 path 解析物理库名（databases/{name}/...）：
    #    kb_grants 无该 (user, kb) → 403 {"error": {"message": 引导文案（见下）}}
    #    kb_keys 无该库凭据 → 500 提示管理员凭据缺失
    # 4. 解密真实 Key → headers["X-API-Key"] = key（覆盖，非追加）→ 转发
    #    settings.fastk_server_url/fastk/api/{path}，透传响应
```

**知识库发现与选库链路**（回答"agent 怎么知道要搜哪个库"）：

agent 的选库决策分三层，全部动态、白名单感知：

| 层 | 机制 | 实现 |
|----|------|------|
| **发现**（有哪些库） | `fastk databases` = 权威发现入口 | 代理过滤式拦截（上伪代码第 2 步）：返回授权范围内的库，实时反映授权增删（无需 recreate），且带服务端元数据。目录端点服务端免 Key，代理拉取时不注入 Key；过滤的价值是减少选错库与 403 往返（库名本身在服务端免 Key 可枚举，非保密对象） |
| **选择**（该搜哪个） | SKILL.md 工作流指引 | 第 1 步"探库"升为**必做**（"不要凭记忆假设库存在"）；按用户问题主题对照列表元数据选库，必要时 `fastk instructions --db X` 读服务端配置的库说明再选 |
| **兜底**（猜错库） | 403 建设性引导 | 文案含"可运行 fastk databases 查看可访问清单"——agent 收到 403 后能立即列出可用库 + 给出联系管理员的引导，而不是干巴巴转述报错 |

配套决策：**不注入 `FASTK_AVAILABLE_DBS` 之类的 env 快照**——授权是即时生效的，env 快照会过期漂移，等于引入第二份会打架的"真相"；agent 多跑一次探库（输出小、成本低）远优于依赖过期快照误导选库。权威来源唯一：`fastk databases`。

**403 用户引导链路**（实测链路零改动）：

- 代理返回 `{"error": {"message": f"无权限访问知识库 '{kb_name}'。可运行 fastk databases 查看你当前可访问的知识库；如需开通 '{kb_name}'，请联系管理员{settings.kb_admin_contact}。"}}`——联系人名单**不是硬编码**，读配置项（见下），默认 `张智骁（工号 00899219）`；
- CLI 的 `request()` 已解析 `error.message`，打印：`fastk: HTTP 403 Forbidden: 无权限访问知识库 'vl_test'。可运行 fastk databases 查看你当前可访问的知识库；如需开通 'vl_test'，请联系管理员张智骁（工号 00899219）。`（默认配置下的实际效果）；
- agent（LLM）在工具输出中读到该消息 → **先跑 `fastk databases` 列出可用库** → 在对话中给出"你有这些库可用；如需 vl_test 请联系管理员张智骁"的建设性回应 → 前端展示。文案对 agent 友好（可执行的下一步 + 明确联系对象），无需 agent 自行发挥；
- 事前引导双保险：`fastk databases` 只返回授权库（代理拦截）+ `GET /api/kb/my-databases` 供前端展示，用户多数情况下不会走到 403。

**管理员名单配置**（403 文案联系人可配置）：

```python
# backend/app/config.py — Settings
kb_admin_contact: str = "张智骁（工号 00899219）"   # 多名管理员直接用顿号/逗号拼接
```

```env
# backend/.env
AGENT_KB_ADMIN_CONTACT=张智骁（工号 00899219）
```

- 名单变更 = 改 `.env` 后**重启 backend**（文案在 backend 侧渲染；agent 容器无感知、无需 recreate）；
- 用字符串拼接而非结构化列表：名单仅用于文案展示（无通知路由等结构化用途），最简形态即可；
- 401 文案（token 无效）不涉及联系人，保持固定。

实现参考：与 [llm_proxy.py](../backend/app/routers/llm_proxy.py) 的 `_forward` 同构（请求头清洗、超时、响应透传），方向相反——llm-proxy 是"透传用户 Key、解析 upstream"，本代理是"解析用户身份、注入平台 Key"。

### 5.5 变更生效机制

| 场景 | 生效方式 |
|------|----------|
| 新用户首次启动 | 自动（创建即注入 token） |
| 授权新增/移除 | **即时生效**（backend 每请求实时查 DB，无需 recreate） |
| Key 轮换 / 凭据补录 | **即时生效**（容器侧无 Key） |
| token 机制变更（如 secret 轮换）/ fastk-mcp 下线 | recreate 容器（刷新 env 与 opencode 配置） |
| **存量容器 env 漂移（旧直连配置）** | **自动 recreate**（`_fastk_env_stale` 检测，见 §10.6）——Docker `start`/`restart` 不刷新创建时 env，故必须重建 |

### 5.6 三条通路的收敛：直连路径、fastk-mcp、引用徽章

**直连路径（原缺口 A）：已知限制，不部署防火墙（已决策）**

agent 容器配有 `extra_hosts: host-gateway`，理论上可 `curl http://host.docker.internal:8000/...` 绕过代理。该配置**不可移除**——平台的"本地回环 Provider"特性依赖它（opencode_config 把回环 provider baseURL 重写为 `host.docker.internal`，agent 容器直连宿主机上的本地 LLM 服务）。

- **接受理由**：威胁模型是内部平台的权限管理（员工用户、编程助手），不是对抗性隔离；绕过需要 agent 主动构造直连请求，正常工作流（CLI/技能/提示词）100% 走代理；WSL2 iptables 封堵实施脆弱（重启丢规则、Docker 子网动态、backend 双网卡出口不确定），运维成本高于残余风险；
- **配套缓解（P0 必做）**：清理 agent 可见文档中的直连地址——[fastk-search/SKILL.md](../config/skills/fastk-search/SKILL.md) L9-10 改为"fastk 服务经平台代理访问；`FASTDB_BASE_URL`/`FASTK_API_KEY` 已由平台注入，不要自行覆盖或尝试直连其他地址"，删除"运行在宿主机上，经 `host.docker.internal:8000` 可达"的表述（现状 12：这是 agent 学习绕过路径的内生来源）；
- **验证**：agent 容器内可见的技能文档不含 `host.docker.internal`；正常路径（CLI/技能）全量经代理；
- 防火墙封堵方案保留于**附录 A**，将来需要强隔离（外部租户 / 高敏知识库）时启用。

**fastk-mcp 整体下线（原缺口 B，已决策）**

能力分析（现状 11）：7 个工具中 6 个与 CLI 调同一批 REST 端点，CLI 还多 instructions/count/alias 三个命令；MCP 独有能力仅 search 的 funnel+diversity 质量增强（多远程融合未实际使用）。它是无用户上下文的共享直连服务——白名单语境下不只是冗余，更是最容易走的绕过缺口（MCP 是 agent 原生工具，无需 bash）。

- **下线实施点**（已执行，方式较原计划修正，见 §10.3）：
  1. [agent-image/builtin-mcp/fastk/manifest.json](../agent-image/builtin-mcp/fastk/manifest.json)：**删除整个 manifest 文件**（原计划 `enabled: false` 不可行——`opencode_config.build_container_config()` 会强制所有 MCP 条目 `enabled=True`，manifest 里的 `enabled:false` 无法阻止注入；唯一干净的下线方式是删除 manifest，使 `_discover_builtin_mcp()` 不再发现该服务器）；
  2. [docker-compose.yml](../docker-compose.yml)：移除 fastk-mcp 服务定义（不再部署常驻容器）；
  3. `backend/.env` 的 `AGENT_FASTK_MCP_URL` 与 `config.py` 的 `fastk_mcp_url`：标注 DEPRECATED 并注释掉/保留备用，不再被任何代码路径引用；
  4. `mcp-fastk/` 源码目录保留（P2 演进基础，见下）；
  5. 已运行容器需 recreate 刷新 opencode 配置（builtin MCP 注入发生在配置生成时）；
- **验证**：recreate 后 agent 的工具列表中无 fastk MCP 工具（manifest 已删，结构上保证）；技能（fastk-search/fastk-analyze）经 CLI 正常工作；
- **后续演进（P2）**：若将来重启 MCP 通路，必须以**带用户上下文**的形态回归（backend 按用户注入带 token 的 headers，fastk-mcp 校验后经 backend 代理转发），不得回到共享直连形态；search 的 funnel+diversity 增强若需要，移植到 CLI（`--diversify` 参数）。

**引用徽章通路（缺口 C）：补白名单校验（P0 必做）**

[routers/fastk.py](../backend/app/routers/fastk.py) 的 `/api/fastk/chunk` 与 `/api/fastk/chunk-image` 是前端点击 `[[chunk:db/id]]` 徽章后取回 chunk 全文/图片的通路。现状：仅 `Depends(get_current_user)` 校验登录，`db` 是自由 query 参数，**不查白名单**、不注入 Key，直连 `settings.fastk_server_url`。

- **风险**：任意登录用户可手工构造 `GET /api/fastk/chunk?db=<任意库>&chunk_id=<id>` 读取未授权库的 chunk 全文与图片。chunk_id 虽是 sha/hex 不易枚举，但会从历史消息、日志、他人分享中泄露；且**授权被回收后**，旧消息里的徽章仍可点开——白名单在此通路完全失效。
- **修复**（已落地为 `_kb_headers()` 辅助函数，两处端点共用）：
  ```python
  # 复用 kb_access（与代理路由同一判定路径，避免分叉）
  access = await kb_access.resolve_access(session, db, user.id)
  if not access.granted:
      raise HTTPException(status_code=403, detail=kb_access.denial_message(db))
  if access.api_key is None:
      raise HTTPException(status_code=500, detail=f"知识库 '{db}' 缺少可用凭据……")
  headers = {"X-API-Key": access.api_key}  # 与代理路由一致：所有平台→服务端流量都带真实 Key
  ```
  `chunk` 与 `chunk-image` 两个端点均经 `_kb_headers()`。前端徽章遇 403 时 ChunkViewer 直接展示后端 `denial_message`（已含管理员联系人，前端不再硬编码第二份名单）。
- **抽公共模块**：`kb_access.py` 提供 `resolve_access(db, kb_name, user_id) -> KbAccess(granted, api_key)`（查 kb_grants 授权 + kb_keys 解密）供 kb_proxy 与 fastk.py 共用，避免权限判定逻辑分叉（实际签名见 §10.4）；
- **验证**：未授权库的 chunk 请求返回 403；授权回收后旧徽章点开为 403；授权库正常取回全文与图片。

### 5.7 前端

- **P0（缺口 C 配套）**：[ChunkRef.tsx](../frontend/src/components/ChunkRef.tsx) 的 ChunkViewer 处理 403——展示"无权限访问该知识库内容，请联系管理员{kb_admin_contact}"，与 CLI 侧文案口径一致（管理员名单由后端接口或错误详情下发，避免前端再硬编码一份）；
- **P1**：AdminPanel 增加凭据录入与授权矩阵管理；用户侧 ConfigPanel 增加"我可访问的知识库"（`GET /api/kb/my-databases`）。

## 6. 与路线 1（env 注入真实 Key）的对比（为何弃）

| | 路线 1 | **路线 2（选定）** |
|---|---|---|
| Key 进容器 | 进（agent 可 `env` 读出，提示注入可诱导泄露） | 不进（仅代理 token，泄露=本人既有权限） |
| 服务端不校验时 | 白名单完全无效（容器直连即可访问全部库，且 CLI 本身就带真实 Key） | **代理层白名单有效**（强制点在平台侧） |
| CLI 改动 | 需加 `FASTK_API_KEYS` 多 Key 支持 + 镜像 rebuild | **零改动** |
| 授权变更生效 | recreate 容器 | 即时生效 |
| 额外成本 | — | 代理路由 + fastk-mcp 下线（减法）+ SKILL.md 文案清理 |

路线 1 仅在"服务端强制校验 Key"的前提下才有约束力——该前提已因"服务端不能随意修改"不成立，故弃。

## 7. 安全考量

1. **Key 暴露面**：真实 Key 只存在于平台 DB（Fernet 密文）与 backend 转发时的内存；容器内仅有代理 token（加密 user_id）——泄露后只能经 backend 代理、仅代表该用户既有权限、平台单方可吊销。
2. **管控定位**：应用层白名单（三条通路——CLI 代理、引用徽章、已下线的 MCP——中前两条收敛到同一份 kb_grants 判定），非网络隔离；直连路径为已知限制（5.6），配套缓解已清除 agent 可见文档中的直连地址；强隔离需求出现时按附录 A 启用防火墙。
3. **权限回收的彻底性**：授权回收后，不仅新的 CLI 调用 403，**历史消息里的引用徽章也立即失效**（chunk 端点实时查 kb_grants）——避免"搜到时有权、回收后仍可从旧消息读全文"的残留泄露。
4. **代理日志**：记录 user_id + 库名 + 端点 + 状态码（计量与审计），**不记录 token 与 Key**；异常高频 403 可作为探测行为告警依据（含 chunk 端点的手工构造探测）。
5. **管理面**：admin API 永不回显明文 Key；凭据录入/轮换、授权变更全量审计；`my-databases` 仅返回库名。
6. **命名统一**：口径统一用 `FASTK_API_KEY`（携带 token 时语义为"代理凭据"，文档注明），避免 `FASTDB_API_KEY` 变体混淆。
7. **token 绝不透传上游**：CLI 对所有请求（含免 Key 的目录端点）都挂 `X-API-Key: <token>`，代理转发前必须丢弃该头——token 是平台内部凭据，泄给 fastk 服务既无意义，也会在服务端将来启用校验时被判非法（现状 17 铁律，实现见 5.4 第 0 步）。
8. **库名不是保密对象**：服务端目录端点免 Key，库名与描述对任何能触达服务端者可枚举；因此 `/databases/` 过滤定位为发现层体验（减少选错库与 403 往返），**真正的边界是逐库读操作的 kb_grants 判定**——不要对外宣称"隐藏了库清单"。
9. **服务端将来启用校验**：平台只需 admin 同步录入新 Key，无需代码变更（代理路由与 chunk 端点都已注入 Key，目录端点免 Key 无需处理）；在此之前不向用户宣传"服务端级管控"，口径为"平台访问控制"。

## 8. 实施计划

| 阶段 | 内容 | 涉及 |
|------|------|------|
| **P0**（已完成，验证结论见 §10.5） | ① 两张极简表 + `kb_access` 公共模块（落地为 `resolve_access`/`granted_kbs`，见 §10.4）+ admin API + 审计；② `issue_proxy_token` 注入（`FASTDB_BASE_URL` 改指向 backend）；③ `/fastk/api/{path}` 代理路由（`/databases/` 过滤式拦截、403 建设性引导文案——联系人读 `kb_admin_contact` 配置项、只读守卫见 §10.2）；④ fastk-mcp 下线（**删除 manifest** + compose 移除服务，见 §10.3）；⑤ SKILL.md 动态化改造（清理直连地址 + **移除静态库名表**，改为"可用库以 `fastk databases` 实时结果为准、探库必做、不凭记忆假设库存在"）；⑥ **引用徽章通路补校验**（`routers/fastk.py` 两个端点查 kb_grants + 注入 Key，ChunkViewer 处理 403） | backend（models/routers/container_manager/**config**）+ agent-image manifest + docker-compose + config/skills + frontend/ChunkRef |
| P1 | `GET /api/kb/my-databases` + 前端（admin 授权矩阵 / 用户库列表）；token per-user 版本号（精细吊销）；代理访问日志告警 | backend + frontend |
| P2 | search 的 diversity 增强移植 CLI（`--diversify`）；fastk-mcp 多租户演进（带用户上下文回归）；服务端若提供管理 API 则对接自动同步凭据 | agent-image CLI + mcp-fastk + backend |

P0 验证清单（逐项实测结论见 §10.5）：

1. admin 录入 aicode 凭据并授权用户 U → U start → 容器内 `env | grep FASTK` 仅见 token 与 `http://backend:8000`，无真实 Key；
2. 容器内 `fastk search --db aicode "..."` 正常返回（经代理）；
3. `fastk search --db vl_test "..."` → CLI 打印"无权限访问知识库 'vl_test'。可运行 fastk databases 查看你当前可访问的知识库；如需开通 'vl_test'，请联系管理员张智骁（工号 00899219）。"（默认配置），agent 对话中给出"可用库清单 + 联系管理员"的建设性回应；修改 `AGENT_KB_ADMIN_CONTACT` 并重启 backend 后，文案随新名单变化；
4. admin 移除授权（不 recreate）→ U 下一次 CLI 调用立即 403；
5. `fastk databases` 仅列出 U 被授权的库，且列表结构与服务端原始响应一致（过滤式，元数据保留）；admin 新增授权后（不 recreate）下一次探库即出现新库；
6. recreate 后 agent 工具列表无 fastk MCP 工具（fastk-mcp 已下线）；
7. agent 容器内可见的技能文档不含 `host.docker.internal` 字样、不含静态库名表（SKILL.md 已动态化）；
8. **引用徽章通路**：agent 引用 aicode 的 chunk → 前端徽章可点开全文与图片；U 未授权的库 `GET /api/fastk/chunk?db=vl_test&chunk_id=...` → 403；admin 回收 aicode 授权后，旧消息里的 aicode 徽章点开也是 403；
9. 伪造/篡改 token 的请求被 401；pytest 覆盖代理路由授权过滤、`/databases/` 按授权过滤（非构造）、token 校验、`kb_access.resolve_access` 判定、chunk 端点未授权 403、admin API 权限与不回显明文；
10. **token 不外泄**：代理转发上游的请求头中不含平台 token（pytest 断言转发 headers 的 `X-API-Key` 等于解密后的真实 Key，或目录分支下不含该头）；`fastk databases` 走通且上游调用未携带任何 Key。

## 9. 决策记录

原 4 个待决策问题已全部关闭：

1. ~~受控库范围~~ → 随 fastk-mcp 整体下线而消失（所有库均经 CLI → 代理管控，MCP 不再挂任何库）；
2. ~~防火墙部署~~ → **不部署**；直连路径定位为已知限制（应用层白名单），配套清理 SKILL.md 直连地址；防火墙方案保留于附录 A 备用；
3. ~~凭据同步流程~~ → **admin 手工同步**（服务端 key 变更后经 admin API 录入/轮换；将来服务端暴露管理 API 再自动同步，P2）；
4. ~~403 文案~~ → 模板 **"无权限访问知识库 'X'。可运行 fastk databases 查看你当前可访问的知识库；如需开通 'X'，请联系管理员{kb_admin_contact}。"**（建设性引导：agent 收到 403 后先列可用库再引导联系管理员）；联系人名单经配置项 `AGENT_KB_ADMIN_CONTACT` 维护（默认：张智骁，工号 00899219；多名管理员用顿号/逗号拼接），改 `.env` 重启 backend 生效。

## 10. 实施修正与验证结论（P0 落地后回写）

本节记录实现阶段相对设计的修正点与端到端验证结论，作为后续维护的事实依据。

### 10.1 内置 fastk CLI 兼容性——误报修正

设计阶段曾怀疑内置只读 CLI（[agent-image/builtin-tools/fastk-cli/fastk](../agent-image/builtin-tools/fastk-cli/fastk)）与 fastdb 服务端契约不兼容。**核实结论：误报，CLI 无需修改。**

- CLI 走 `FASTDB_BASE_URL + /fastk/api` 前缀，与服务端 REST 契约一致；`base_url()` 已做尾斜杠与重复前缀归一化。
- CLI 把 `FASTK_API_KEY` 挂到 `X-API-Key` 头——这正是代理路由识别 token 的头（`kb_proxy.py` 读 `request.headers.get("x-api-key")`），契合无缝。
- CLI 的 `resolve_db()` 内置 `global→fastdb` 逻辑映射，其余库名透传为物理名；配合 §10.4 的 SKILL.md 动态化（探库必做），不依赖任何静态默认库。

### 10.2 只读守卫决策

代理路由对写操作的拦截采取**白名单式只读守卫**（`kb_proxy.py`）：

- **GET 全放行**：服务端所有 GET 均为读操作。
- **POST 仅限三个检索端点**：`_READ_POST_PATHS = ("/search", "/query", "/grep")`，即只读 CLI 实际会发的 POST 体；其余 POST（如 `/documents` 等可能变更索引的端点）一律 405「知识库为只读访问，不支持该操作。」
- 决策理由：把"容器只读"从约定升级为代理层强制，不依赖容器自觉；将来 CLI 若新增只读 POST 端点，需同步扩 `_READ_POST_PATHS`（集中一处，易审计）。

### 10.3 fastk-mcp 下线方式修正

原计划用 manifest `enabled: false` 下线（§5.6 已同步修正）。**实际必须删除 manifest 文件**：

- `opencode_config.build_container_config()` 在合并配置后**强制所有 MCP 条目 `enabled=True`**（确保 builtin MCP 一定连上），manifest 里的 `enabled:false` 会被覆盖，无法阻止注入。
- 唯一的可见性开关 `hidden_mcp_servers()` 走的是 permission deny 规则（读 host config `builtin_mcp` 段），属"隐藏"而非"不注入"，且增加配置复杂度。
- 删除 manifest 后 `_discover_builtin_mcp()` 不再发现 fastk 服务器，从源头杜绝注入——最干净、可逆（恢复文件即回归）。
- 配套：compose 移除 fastk-mcp 服务；`.env` 的 `AGENT_FASTK_MCP_URL` 与 `config.py` 的 `fastk_mcp_url` 标 DEPRECATED 保留备用。

### 10.4 公共模块实际签名

设计稿中的 `resolve_key(kb_name, user_id) -> str | None` 落地为语义更清晰的拆分（[kb_access.py](../backend/app/services/kb_access.py)），避免"授权"与"凭据"两个正交问题被一个返回值混淆：

- `resolve_access(db, kb_name, user_id) -> KbAccess(granted: bool, api_key: str | None)`：**granted** 是白名单判定（强制点， denial→403）；**api_key** 是转发时注入的真实凭据（缺失→500，属运维错误而非用户错误）。
- `granted_kbs(db, user_id) -> list[str]`：目录过滤用，返回该用户可读的物理库名。
- `issue_proxy_token(user_id)` / `verify_proxy_token(token)`：容器凭据为 `Fernet("kbproxy:<uid>")`，无状态、不可伪造、仅值同持有者自身的授权。
- `denial_message(kb_name)`：403 文案，管理员联系人读 `settings.kb_admin_contact`（不硬编码）。

### 10.5 端到端验证结论

在 WSL 部署 fastdb（dev mode，`database.token: ''` 不校验 Key）+ 重建 backend 镜像后，于 backend 容器内对生产 PostgreSQL 实测，验证清单（§8）逐项结论：

| # | 验证项 | 结论 |
|---|--------|------|
| 1 | 容器 env 仅见 token 与 `http://backend:8000`，无真实 Key | ✅ 代码核实：`container_manager` 注入 `FASTDB_BASE_URL=settings.kb_proxy_base`、`FASTK_API_KEY=issue_proxy_token(user_id)`，无真实 Key、无 `FASTK_DEFAULT_DB` |
| 2 | `fastk search --db fastdb` 经代理正常 | ✅ 代理 `POST /databases/fastdb/search` → 200（2094 bytes） |
| 3 | 未授权库 403 文案含管理员联系人 | ✅ `POST /databases/vl_test/search` → 403，文案含「可运行 fastk databases」与「请联系管理员张智骁（工号 00899219）」 |
| 4 | 移除授权后即时 403（不 recreate） | ✅ revoke vl_test 后下一次请求立即 403（代理每请求查 kb_grants） |
| 5 | 探库仅列授权库、新增授权即时可见 | ✅ 目录过滤为 `['fastdb']` 且剥离 `uri`、保留 description/model/dimension；grant vl_test 后目录立即变 `['fastdb','vl_test']` |
| 6 | recreate 后无 fastk MCP 工具 | ✅ 结构保证（manifest 已删，`_discover_builtin_mcp()` 不再发现） |
| 7 | 技能文档无 `host.docker.internal`/静态库表 | ✅ 源文件已改（SKILL.md 动态化，探库必做） |
| 8 | 徽章通路 403 | ✅ pytest 覆盖（`routers/fastk.py` 查 kb_grants + 注入 Key） |
| 9 | 伪造 token 401 | ✅ 无 token→401、伪造 token→401；pytest 亦覆盖 |
| 10 | token 不外泄 | ✅ pytest 断言转发 headers 的 `X-API-Key` 为解密后真实 Key（目录分支不含该头）；代理 `_DROP_HEADERS` 始终丢弃调用方 `x-api-key` |

补充实测：只读守卫 `POST /databases/fastdb/documents` → 405「知识库为只读访问」。

> 备注：实测中 vl_test 授权后 `search` 上游返回 409（非 200），系 fastdb 服务端对该库（视觉-语言多模态索引）纯文本检索的自身响应，经代理透传；**与白名单逻辑无关**——代理已正确放行（granted+注入 Key）并转发，403→409→403 的翻转恰好证明授权判定即时生效。

### 10.6 存量容器 env 漂移绕过白名单——根因与修复

**现象（用户报告）**：caesar 用户已授权 `fastdb`、**未**授权 `vl_test`，却仍能 `fastk grep --db vl_test` 命中 vl_test 内容。

**根因**：caesar 的运行容器创建于代理注入代码落地**之前**，其 env 为旧版直连配置——`FASTDB_BASE_URL=http://host.docker.internal:8000`（直指 fastk 服务）、**无 `FASTK_API_KEY`**、`FASTK_DEFAULT_DB=global`。容器内 CLI 因此完全绕过 backend 代理，直达不做 Key 校验的服务端，白名单形同虚设。

**为何长期未被纠正（系统性缺口）**：
- Docker 的 `start`/`restart` **复用创建时 env**，只有 recreate（删除重建）才会刷新；
- `container_manager._needs_recreate` 原本只检查**镜像过期**与**挂载指纹**，不检查 env 漂移；
- 且运行中的容器在 `_ensure_container_sync` 里**提前 return**，连镜像/挂载检查都跳过。
- 三者叠加 → 所有旧容器永久绕过白名单，非 caesar 个例。

**修复**（[container_manager.py](../backend/app/services/container_manager.py)）：
1. 新增 `_fastk_env_stale(container)`：读 `container.attrs["Config"]["Env"]`，命中任一即判为漂移——`FASTDB_BASE_URL != settings.kb_proxy_base`、存在 `FASTK_DEFAULT_DB`、或 `FASTK_API_KEY` 无法被 `verify_proxy_token` 解出 user_id。token 不做字面比对（Fernet 含时间戳，重签即变），改为解码校验，任意合法代理 token 均放行（代理按 token 内 user_id 实时查授权）；
2. 纳入 `_needs_recreate`（针对停止容器）；
3. **运行中容器分支也做该检查**：安全优先于会话连续性，命中即 `remove(force=True)` 后走重建路径（用户数据在命名 volume 中不丢）。

**验证**（重建 backend 镜像 + 重启后，对 caesar 真实容器实测）：
- 旧容器 `_fastk_env_stale=True`；触发 `ensure_container` 后日志「Running container ... bypasses the KB whitelist (stale fastk env) — recreating」，新容器 env 为 `FASTDB_BASE_URL=http://backend:8000`、`FASTK_API_KEY` 已注入、无 `FASTK_DEFAULT_DB`、`_fastk_env_stale=False`；
- 容器内 `fastk databases` 仅列 `fastdb`（剥离 uri）；`fastk grep --db vl_test the` → **HTTP 403** 含管理员联系人文案；`fastk grep --db fastdb the` → 正常返回（合法权限不受影响）；
- 单测 `tests/test_container_fastk_env.py`（6 例）覆盖：旧直连/带默认库/缺 token/伪 token/空 env → stale，当前供给配置 → 非 stale。全量后端测试 121 项通过。

## 附录 A：直连封堵方案（备用，当前不启用）

若将来需要强隔离（引入外部租户、知识库含高敏数据），在宿主机/WSL 防火墙拒绝 agent-net 子网到 fastk 服务端口（8000）的入站流量，放行 backend（backend 的 host-gateway 访问不受影响，需按 backend 双网卡实际出口子网验证）：

```bash
# 示意（需持久化机制，WSL 重启后规则丢失）
iptables -I INPUT -s <agent-net-subnet> -p tcp --dport 8000 -j REJECT
```

验证：agent 容器内 `curl -m 3 http://host.docker.internal:8000/fastk/api/databases/` 应超时/拒绝，而 `fastk databases`（走代理）正常。注意 fastk-mcp 已下线，agent-net 上不再有其他依赖宿主机 8000 的服务，规则复杂度较 v3 降低。
