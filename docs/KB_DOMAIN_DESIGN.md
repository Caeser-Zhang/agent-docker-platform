# 知识领域（KB Domain）设计 — Key 管理升级与领域聚合展示

版本 v1 · 2026-09-16 · 取代 `FASTK_APIKEY_WHITELIST_DESIGN.md` 中"一库一 Key、授权单位是库"的核心假设。

## 1. 背景与上游硬事实

原模型：`kb_keys.kb_name` 单值主键（一库一 Key，Fernet 密文），`kb_grants(user_id, kb_name)` 复合主键 + `revoked_at` 软删除；强制点收敛于 `kb_access.resolve_access`（kb_proxy 代理、fastk 徽章、my-catalog 三处共用）。

对 fastk 上游的三轮只读探测确立了三条硬事实，直接决定了本设计的边界：

1. **服务端不校验 key** — 无 key / 伪 key / 任意逐库 key 均 200 返回全量目录；
2. **不做 key↔库作用域限制** — A 库的 key 可读 B 库；
3. **OpenAPI 无任何 key 管理端点、`securitySchemes: None`** — 平台**无法从上游查询某个 key 覆盖哪些库**。

结论：**领域↔库的映射必须由平台自存**，不能寄望上游提供。授权判定、聚合展示全部基于平台侧数据；上游目录仅用于补充库的 description（发现层，非保密边界）。

## 2. 核心概念转变

| 维度 | 旧模型 | 新模型 |
|---|---|---|
| 授权单位 | 物理库（kb_name） | **知识领域**（domain，一个 Key 一个领域） |
| Key↔库 | 1:1 | **1:N**（一个领域 Key 覆盖多个库，平台维护映射） |
| 库↔领域 | — | **N:1 硬约束**（一库只属一个领域，UNIQUE(kb_name)） |
| 公共库 | 无此概念 | `key_type='public'` → **隐式全员放行**，不写 grant 行 |
| 私有库 | 全部靠 grant | `key_type='private'` → grant 名册判定 |
| 用户侧展示 | 平铺库名列表 | **领域卡片分组**（领域名+描述为卡头，其下平铺库） |
| agent 侧目录 | 过滤后平铺库名 | **不变**（领域仅是浏览器展示层概念） |

## 3. 数据模型（三张新表）

```
kb_domains
  id           String(36) PK (uuid4)
  name         String(50) UNIQUE NOT NULL      -- 展示名，1-50 字，全局唯一
  description  String(500) NOT NULL DEFAULT '' -- 可选领域描述，用户侧卡片展示
  key_type     String(10) NOT NULL DEFAULT 'private'  -- 'public' | 'private'
  api_key_enc  Text NOT NULL DEFAULT ''        -- Fernet 密文，任何 API 不回显
  created_at / updated_at

kb_domain_dbs
  domain_id    String(36) FK kb_domains.id ON DELETE CASCADE, PK
  kb_name      String(100) PK, UNIQUE          -- 一库只属一个领域（硬约束）
  created_at

kb_domain_grants
  user_id      String(36) FK users.id ON DELETE CASCADE, PK
  domain_id    String(36) FK kb_domains.id ON DELETE CASCADE, PK
  created_at
  revoked_at   可空                            -- 软删除，语义同旧 kb_grants
```

要点：
- `kb_domain_dbs.kb_name` 的 UNIQUE 是"一库一领域"的数据库级保证；把库加入第二个领域直接 409。
- grant 沿用复合主键 + `revoked_at` 软删除：撤销是盖戳，重新授权是清戳 + 重置 created_at（PK 禁止二次 INSERT）。
- SQLite 不设 `foreign_keys=ON`，CASCADE 只在 PostgreSQL 生效 → 删领域时在路由里**显式删除** dbs 与 grants 行（同旧 `delete_kb_key` 先例）。
- 领域 `name` 是展示名，允许中文；物理库名 `kb_name` 仍限 `^[A-Za-z0-9_-]{1,64}$`。

## 4. 一次性迁移（database.py，无 Alembic）

启动时 `init_db()` 中，`create_all` 之后执行 `_migrate_kb_domains`（幂等）：

1. 内省：仅当旧表 `kb_keys` 存在时执行（新库直接跳过）；
2. 每条旧 `kb_keys` 行 → 一个领域：`id=uuid4`、`name=kb_name`、`description=''`、`key_type='private'`、`api_key_enc` 原样搬（密文兼容，密钥不变）；
3. `kb_domain_dbs(domain_id, kb_name)` 一一对应；
4. 旧 `kb_grants` 按 kb_name → domain_id 映射为 `kb_domain_grants`，`created_at`/`revoked_at` 原样保留；
5. `DROP TABLE kb_grants; DROP TABLE kb_keys;`（先删子表）。

名称冲突不可能发生：旧 kb_name 是主键天然唯一，name=kb_name 迁移后仍唯一。

同时从 `_add_missing_columns` 中移除 kb_grants.revoked_at 的补列逻辑（表已不存在）。

## 5. 授权判定（kb_access.py 重写）

`resolve_access(db, kb_name, user_id) -> KbAccess(granted, api_key)` 签名不变（三个消费方 kb_proxy / fastk 徽章 / 用户侧零改动），内部逻辑改为：

```
kb_name → kb_domain_dbs 反查唯一领域 → kb_domains
  ├─ 领域不存在            → granted=False（未纳管的库一律拒绝）
  ├─ key_type='public'     → granted=True（隐式放行，不查名册）
  └─ key_type='private'    → 查 kb_domain_grants(user_id, domain_id, revoked_at IS NULL)
api_key = decrypt(domain.api_key_enc) or None
```

`granted_kbs(db, user_id)`（目录过滤用）：

```
公共领域的全部库 ∪ 用户有 active grant 的私有领域的全部库
```

代理 token、`catalog_headers()`、`denial_message()` 机制不变。

## 6. Key 类型切换

- PUT `/api/admin/kb-domains/{id}` 携带 `key_type` + `confirm_name`（必须等于领域名，防误触）；
- **public→private 立即收紧**：前端弹窗告知"N 名用户将立即失去访问"（影响面 = 当前 active grant 数之外的全体用户；实际保留名册内用户仍有权）并要求输入领域名确认；
- **private→public 立即全员放行**；
- 两个方向的已有 grant 行**都保留**：公共时不参与判定，切回私有时名册仍在。

生效即时的原因：`resolve_access` 每请求重读 DB，无缓存、无需重建容器。

## 7. 私有领域成员管理（三种授权方式，一核心）

三入口共用"标识符列表 → 查 `users.uid`（工号）→ 解析为用户"核心：

| 入口 | 形式 |
|---|---|
| 手动单个授权 | POST members `{username 或 uid}`，即旧 grant 语义 |
| 批量工号导入 | 粘贴文本（换行/逗号/空格/Tab 分隔） |
| Excel 导入 | .csv / .xlsx（新增 openpyxl 依赖） |

**解析约定（csv/xlsx）**：取第一 sheet；表头关键词（工号/员工号/uid/emp/no，不区分大小写）自动定位列；无表头取第一列。上限 5000 行 / 5MB；去重；未匹配标识符**不自动建用户**。

**预览确认 + 增量追加**两步协议：

1. POST `/api/admin/kb-domains/{id}/import/preview`（multipart：`text` 字段或 `file` 字段）— 解析不落库，返回：
   - `matched`: 匹配到的用户（id/username/uid）
   - `already`: 已有 active grant 的用户（幂等跳过）
   - `unmatched`: 未匹配标识符原样列出
   - `source_meta`: 来源文件名/sheet/识别列（前端回显，可下拉改选列重解析）
   - `preview_token`: 服务端暂存解析结果（内存 TTL 10 分钟），返回不透明令牌
2. POST `/api/admin/kb-domains/{id}/import/commit` `{preview_token}` — 校验令牌归属后写入 grants（软删除行清戳复活），返回写入计数。

## 8. API 端点（领域资源式，旧端点直删）

管理端（require_admin，前缀 /api/admin）：

```
GET    /kb-domains                       列表（id/name/description/key_type/has_api_key/db_count/member_count/时间戳）
POST   /kb-domains                       创建 {name, description?, key_type?, api_key?}
GET    /kb-domains/{id}                  详情（含 dbs[]、members[]）
PUT    /kb-domains/{id}                  改名/描述/key_type(带 confirm_name)/轮换 api_key
DELETE /kb-domains/{id}                  删领域（显式级联删 dbs+grants，返回受影响成员数）
GET    /kb-domains/{id}/dbs              关联库列表
POST   /kb-domains/{id}/dbs              {kb_name} 关联（已被他域占用 → 409）
DELETE /kb-domains/{id}/dbs/{kb_name}    解除关联
GET    /kb-domains/{id}/members          成员名单（active）
POST   /kb-domains/{id}/members          单个授权 {username|uid}
DELETE /kb-domains/{id}/members/{user_id} 撤销（软删除）
POST   /kb-domains/{id}/import/preview   导入预览（multipart）
POST   /kb-domains/{id}/import/commit    导入提交 {preview_token}
GET    /kb-users                         保留（授权选人搜索）
```

用户端：

```
GET    /api/kb/my-domains                [{id, name, description, key_type, databases:[{name, description}]}]
```

- 取代 my-catalog / my-databases；一次返回领域分组 + fastk 目录描述（fail-soft：目录不可达时 description 为空）；
- 只返回用户有权访问的领域（公共 ∪ 私有有名册）；**未关联任何库的领域隐藏**（管理端仍可见并标"未关联库"）。

删除：`/api/admin/kb-keys*`、`/api/admin/kb-grants*`、`/api/admin/kb-user-access`、`/api/kb/my-databases`、`/api/kb/my-catalog`。前后端同仓同镜像部署，无第三方调用方，不留兼容层。

## 9. 审计（kbdomain.* 命名空间）

| 动作 | detail |
|---|---|
| kbdomain.create / update / delete | domain_id, name（delete 附 affected_members） |
| kbdomain.type_switch | domain_id, name, from, to, affected_users |
| kbdomain.db_add / db_remove | domain_id, name, kb_name |
| kbdomain.member_grant / member_revoke | domain_id, name, user_id, username |
| kbdomain.member_import | domain_id, name, source(text/csv/xlsx), matched/created/skipped 计数, preview_token |

旧 kbkey.*/kbgrant.* 动作随端点删除；历史审计行不动（只读日志）。

## 10. 前端（原地重构两处）

**管理端 `KbAccessAdmin.tsx`** → 领域主视图：
- 领域表格：名称/描述/类型标签(public·private)/关联库数/成员数/操作；
- 抽屉：改名称描述、切换类型（影响面确认弹窗 + 输入领域名）、轮换 Key、增删关联库、成员名单 + 三入口导入（粘贴/csv/xlsx → 预览确认页 → 提交）。

**用户侧 `Chat.tsx` 知识库面板** → 领域卡片分组：
- 卡片头 = 领域名 + 描述；卡内平铺该领域的库名 + fastk 描述；
- 数据源换为 `GET /api/kb/my-domains`。

agent 面（kb_proxy 目录）保持平铺库名，不改。

## 11. 测试策略

重写（领域语义）：`test_kb_access.py`、`test_kb_admin_api.py`、`test_kb_proxy.py`（公共隐式放行、私有名册、反查唯一领域、目录过滤含公共库）。`test_container_fastk_env.py` 不受影响保留。

新增五组：
1. **迁移**：造旧表旧数据 → init_db → 验证领域/grants 搬迁正确、旧表已 DROP、幂等重跑；
2. **类型切换**：两方向立即生效 + grant 行保留 + confirm_name 校验；
3. **导入**：粘贴/csv/xlsx 解析、表头定位、未匹配、重复幂等、预览令牌过期/跨领域拒绝；
4. **my-domains**：聚合正确、空领域隐藏、公共领域全员可见、无权领域不可见；
5. **库归属**：一库一领域 UNIQUE 冲突 409、解除关联后访问即断。

验收：backend 全量 pytest 绿 + 前端 tsc 绿。

## 12. 不变量（继承自白名单设计，仍然成立）

- 真实 Key 只在 backend 内存解密，容器内只有代理 token；`_DROP_HEADERS` 铁律不透传；
- `AGENT_KB_CATALOG_KEY` 只买描述不放宽边界，绝不进容器；
- 目录过滤是发现层便利，不是保密边界；强制点唯一收敛于 `resolve_access`；
- Key 写-only：任何端点不回显密文/明文。
