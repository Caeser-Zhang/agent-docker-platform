# fastk 知识库 API Key 白名单管控方案

> 目标：平台在**不修改 fastk 服务端**的前提下，按白名单管控每个用户可访问的知识库；Key 全程不暴露给用户；未授权访问时给用户明确引导。
>
> v4 修订：根据用户决策定稿——① **fastk-mcp 整体下线**（能力与 CLI 冗余，且是无用户上下文的共享直连绕过缺口）；② **不部署防火墙**（威胁模型为内部平台权限管理，非对抗隔离；改为"已知限制 + 配套缓解"，防火墙方案降级为附录可选项）；③ 凭据由 admin 手工同步；④ 403 文案带管理员信息（默认张智骁 / 00899219，**联系人名单为配置项，可修改**）；⑤ **知识库发现与选库链路**（`/databases/` 改过滤式保留服务端元数据 + SKILL.md 动态化 + 403 建设性引导），解决"agent 怎么知道要搜哪个库"。

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

## 3. 核心结论

**选定路线 2：后端代理（Key 不出平台）**，强制力由两部分组成：

1. **代理层白名单**（backend，核心）：CLI 请求全部经 backend 代理，按 token 定位用户、按物理库名查授权——未授权直接 403，根本到不了 fastk 服务；
2. **fastk-mcp 下线**（部署项）：消除无用户上下文的共享直连通道，agent 访问知识库的唯一通路收敛为 CLI → 代理。

**定位声明**：本方案是**应用层访问控制**（所有正常路径收敛到代理），不是网络隔离。已知限制见 5.6——agent 理论上可经 `host.docker.internal` 直连宿主机 8000 绕过代理；接受该残留风险的依据是威胁模型（内部平台、员工用户、权限管理而非对抗隔离），且配套缓解已清除 agent 可见文档中的直连地址。若将来需要强隔离（如引入外部租户、知识库含高敏数据），按附录 A 启用网络封堵。

在此结构下：Key 只存在于平台 DB（密文）与 backend 内存中；容器内只有一个"代理 token"（加密编码的 user_id，泄露不构成权限升级）；服务端将来启用 Key 校验时平台无需再改。

## 4. 总体数据流

```
管理员（Admin API）
   │  ① 录入库凭据 kb_keys(kb_name, api_key_enc)   ← 与服务端已配置的 key 手工保持同步
   │  ② 维护授权 kb_grants(user_id, kb_name)
   ▼
平台 DB（PostgreSQL）
   │
   ▼ 用户 start（JWT → user_id）
AgentController.start_for_user
   │  注入（创建/重建容器时）：
   │    FASTDB_BASE_URL = http://backend:8000      ← 改指向 backend 代理
   │    FASTK_API_KEY   = <token>                   ← Fernet(user_id)，非真实 Key
   ▼
容器内 CLI（零改动）：--db 逻辑名 → FASTK_DB_MAP → 物理名
   │  GET http://backend:8000/fastk/api/databases/{物理名}/...
   │  X-API-Key: <token>
   ▼
backend 代理（kb_proxy 路由）
   ├─ token 无效/缺失 → 401
   ├─ 顶层 "databases/"（探库）→ 拉上游全量列表，按 kb_grants 过滤后返回
   │     → agent 列库只见授权库，且保留服务端元数据（发现入口）
   ├─ 查 kb_grants：用户未授权该库
   │     → 403 {"error":{"message":"无权限访问知识库 'xxx'。可运行 fastk databases
   │            查看你当前可访问的知识库；如需开通 'xxx'，请联系管理员{kb_admin_contact}。"}}
   │     → CLI 打印该消息 → agent 转述用户 → 前端对话可见
   └─ 已授权 → kb_keys 解密真实 Key → 注入 X-API-Key → 转发 fastk 服务 → 透传响应
```

（fastk-mcp 通道已下线，不在图中。）

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

- 授权"库 aicode" = 授权使用 aicode 的 key，语义等价于"授权 key 实体"，但少了中间实体；
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
    # 1. token = request.headers["X-API-Key"]
    #    Fernet 解密 → "kbproxy:{user_id}"（无效/缺失 → 401）
    # 2. 若 path 是顶层 "databases/"（探库请求）：
    #    拉取上游 GET /databases/ 全量列表 → 按该用户 kb_grants 过滤后返回。
    #    【过滤式而非构造式】：保留服务端列表项自带的元数据（描述等），
    #    agent 探库既见"我有权限的全部库"，又拿到选库所需的语义信息
    # 3. 否则从 path 解析物理库名（databases/{name}/...）：
    #    kb_grants 无该 (user, kb) → 403 {"error": {"message": 引导文案（见下）}}
    #    kb_keys 无该库凭据 → 500 提示管理员凭据缺失
    # 4. 解密真实 Key → headers["X-API-Key"] = key → 转发
    #    settings.fastk_server_url/fastk/api/{path}，透传响应
```

**知识库发现与选库链路**（回答"agent 怎么知道要搜哪个库"）：

agent 的选库决策分三层，全部动态、白名单感知：

| 层 | 机制 | 实现 |
|----|------|------|
| **发现**（有哪些库） | `fastk databases` = 权威发现入口 | 代理过滤式拦截（上伪代码第 2 步）：返回的即"你有权限的全部库"，实时反映授权增删（无需 recreate），且带服务端元数据 |
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

### 5.6 直连路径与 fastk-mcp 处置

**直连路径（原缺口 A）：已知限制，不部署防火墙（已决策）**

agent 容器配有 `extra_hosts: host-gateway`，理论上可 `curl http://host.docker.internal:8000/...` 绕过代理。该配置**不可移除**——平台的"本地回环 Provider"特性依赖它（opencode_config 把回环 provider baseURL 重写为 `host.docker.internal`，agent 容器直连宿主机上的本地 LLM 服务）。

- **接受理由**：威胁模型是内部平台的权限管理（员工用户、编程助手），不是对抗性隔离；绕过需要 agent 主动构造直连请求，正常工作流（CLI/技能/提示词）100% 走代理；WSL2 iptables 封堵实施脆弱（重启丢规则、Docker 子网动态、backend 双网卡出口不确定），运维成本高于残余风险；
- **配套缓解（P0 必做）**：清理 agent 可见文档中的直连地址——[fastk-search/SKILL.md](../config/skills/fastk-search/SKILL.md) L9-10 改为"fastk 服务经平台代理访问；`FASTDB_BASE_URL`/`FASTK_API_KEY` 已由平台注入，不要自行覆盖或尝试直连其他地址"，删除"运行在宿主机上，经 `host.docker.internal:8000` 可达"的表述（现状 12：这是 agent 学习绕过路径的内生来源）；
- **验证**：agent 容器内可见的技能文档不含 `host.docker.internal`；正常路径（CLI/技能）全量经代理；
- 防火墙封堵方案保留于**附录 A**，将来需要强隔离（外部租户 / 高敏知识库）时启用。

**fastk-mcp 整体下线（原缺口 B，已决策）**

能力分析（现状 11）：7 个工具中 6 个与 CLI 调同一批 REST 端点，CLI 还多 instructions/count/alias 三个命令；MCP 独有能力仅 search 的 funnel+diversity 质量增强（多远程融合未实际使用）。它是无用户上下文的共享直连服务——白名单语境下不只是冗余，更是最容易走的绕过缺口（MCP 是 agent 原生工具，无需 bash）。

- **下线实施点**：
  1. [agent-image/builtin-mcp/fastk/manifest.json](../agent-image/builtin-mcp/fastk/manifest.json)：`enabled: false`（保留 manifest，将来可逆）；
  2. [docker-compose.yml](../docker-compose.yml)：移除 fastk-mcp 服务定义（不再部署常驻容器）；
  3. `backend/.env` 的 `AGENT_FASTK_MCP_URL` 与 `config.py` 的 `fastk_mcp_url`：不再被引用，可留作将来重启的基础（最小改动：只动 manifest + compose）；
  4. `mcp-fastk/` 源码目录保留（P2 演进基础，见下）；
  5. 已运行容器需 recreate 刷新 opencode 配置（builtin MCP 注入发生在配置生成时）；
- **验证**：recreate 后 agent 的工具列表中无 fastk MCP 工具；技能（fastk-search/fastk-analyze）经 CLI 正常工作；
- **后续演进（P2）**：若将来重启 MCP 通路，必须以**带用户上下文**的形态回归（backend 按用户注入带 token 的 headers，fastk-mcp 校验后经 backend 代理转发），不得回到共享直连形态；search 的 funnel+diversity 增强若需要，移植到 CLI（`--diversify` 参数）。

### 5.7 前端（P1）

AdminPanel 增加凭据录入与授权矩阵管理；用户侧 ConfigPanel 增加"我可访问的知识库"（`GET /api/kb/my-databases`）。

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
2. **管控定位**：应用层白名单（所有正常路径收敛到代理），非网络隔离；直连路径为已知限制（5.6），配套缓解已清除 agent 可见文档中的直连地址；强隔离需求出现时按附录 A 启用防火墙。
3. **代理日志**：记录 user_id + 库名 + 端点 + 状态码（计量与审计），**不记录 token 与 Key**；异常高频 403 可作为探测行为告警依据。
4. **管理面**：admin API 永不回显明文 Key；凭据录入/轮换、授权变更全量审计；`my-databases` 仅返回库名。
5. **命名统一**：口径统一用 `FASTK_API_KEY`（携带 token 时语义为"代理凭据"，文档注明），避免 `FASTDB_API_KEY` 变体混淆。
6. **服务端将来启用校验**：平台只需 admin 同步录入新 Key，无需代码变更；在此之前不向用户宣传"服务端级管控"，口径为"平台访问控制"。

## 8. 实施计划

| 阶段 | 内容 | 涉及 |
|------|------|------|
| **P0** | ① 两张极简表 + admin API + 审计；② `issue_proxy_token` 注入（`FASTDB_BASE_URL` 改指向 backend）；③ `/fastk/api/{path}` 代理路由（`/databases/` 过滤式拦截、403 建设性引导文案——联系人读 `kb_admin_contact` 配置项）；④ fastk-mcp 下线（manifest `enabled: false` + compose 移除服务）；⑤ SKILL.md 动态化改造（清理直连地址 + **移除静态库名表**，改为"可用库以 `fastk databases` 实时结果为准、探库必做、不凭记忆假设库存在"） | backend（models/routers/container_manager/**config**）+ agent-image manifest + docker-compose + config/skills |
| P1 | `GET /api/kb/my-databases` + 前端（admin 授权矩阵 / 用户库列表）；token per-user 版本号（精细吊销）；代理访问日志告警 | backend + frontend |
| P2 | search 的 diversity 增强移植 CLI（`--diversify`）；fastk-mcp 多租户演进（带用户上下文回归）；服务端若提供管理 API 则对接自动同步凭据 | agent-image CLI + mcp-fastk + backend |

P0 验证清单：

1. admin 录入 aicode 凭据并授权用户 U → U start → 容器内 `env | grep FASTK` 仅见 token 与 `http://backend:8000`，无真实 Key；
2. 容器内 `fastk search --db aicode "..."` 正常返回（经代理）；
3. `fastk search --db vl_test "..."` → CLI 打印"无权限访问知识库 'vl_test'。可运行 fastk databases 查看你当前可访问的知识库；如需开通 'vl_test'，请联系管理员张智骁（工号 00899219）。"（默认配置），agent 对话中给出"可用库清单 + 联系管理员"的建设性回应；修改 `AGENT_KB_ADMIN_CONTACT` 并重启 backend 后，文案随新名单变化；
4. admin 移除授权（不 recreate）→ U 下一次 CLI 调用立即 403；
5. `fastk databases` 仅列出 U 被授权的库，且列表结构与服务端原始响应一致（过滤式，元数据保留）；admin 新增授权后（不 recreate）下一次探库即出现新库；
6. recreate 后 agent 工具列表无 fastk MCP 工具（fastk-mcp 已下线）；
7. agent 容器内可见的技能文档不含 `host.docker.internal` 字样、不含静态库名表（SKILL.md 已动态化）；
8. 伪造/篡改 token 的请求被 401；pytest 覆盖代理路由授权过滤、`/databases/` 按授权过滤（非构造）、token 校验、admin API 权限与不回显明文。

## 9. 决策记录

原 4 个待决策问题已全部关闭：

1. ~~受控库范围~~ → 随 fastk-mcp 整体下线而消失（所有库均经 CLI → 代理管控，MCP 不再挂任何库）；
2. ~~防火墙部署~~ → **不部署**；直连路径定位为已知限制（应用层白名单），配套清理 SKILL.md 直连地址；防火墙方案保留于附录 A 备用；
3. ~~凭据同步流程~~ → **admin 手工同步**（服务端 key 变更后经 admin API 录入/轮换；将来服务端暴露管理 API 再自动同步，P2）；
4. ~~403 文案~~ → 模板 **"无权限访问知识库 'X'。可运行 fastk databases 查看你当前可访问的知识库；如需开通 'X'，请联系管理员{kb_admin_contact}。"**（建设性引导：agent 收到 403 后先列可用库再引导联系管理员）；联系人名单经配置项 `AGENT_KB_ADMIN_CONTACT` 维护（默认：张智骁，工号 00899219；多名管理员用顿号/逗号拼接），改 `.env` 重启 backend 生效。

## 附录 A：直连封堵方案（备用，当前不启用）

若将来需要强隔离（引入外部租户、知识库含高敏数据），在宿主机/WSL 防火墙拒绝 agent-net 子网到 fastk 服务端口（8000）的入站流量，放行 backend（backend 的 host-gateway 访问不受影响，需按 backend 双网卡实际出口子网验证）：

```bash
# 示意（需持久化机制，WSL 重启后规则丢失）
iptables -I INPUT -s <agent-net-subnet> -p tcp --dport 8000 -j REJECT
```

验证：agent 容器内 `curl -m 3 http://host.docker.internal:8000/fastk/api/databases/` 应超时/拒绝，而 `fastk databases`（走代理）正常。注意 fastk-mcp 已下线，agent-net 上不再有其他依赖宿主机 8000 的服务，规则复杂度较 v3 降低。
