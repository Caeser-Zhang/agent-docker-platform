# 意见反馈 & 心愿墙 设计文档

> 状态：已评审（27 项决策达成共识），待实施
> 涉及范围：`backend/app/{models,database,schemas}.py`、`backend/app/routers/`、`frontend/src/`
> 关联文档：`docs/present_file-tool-design.md`、`docs/API.md`

---

## 1. 目标与非目标

### 1.1 目标

**功能 A：用户意见反馈**

| # | 目标 |
|---|------|
| A1 | 主界面提供视觉明显的"意见反馈"入口，弹窗内多行输入 → 提交 / 取消 |
| A2 | 提交成功后 toast 致谢并立即关闭弹窗 |
| A3 | 反馈落库，字段含：用户姓名、工号、反馈内容、反馈时间、解决状态（默认"未解决"） |
| A4 | 用户体验管理后台新增"用户反馈"栏目，表格展示完整字段 |
| A5 | 管理员可在 已解决 / 评估中 / 已规划 / 开发中 之间切换状态并实时持久化 |

**功能 B：心愿墙**

| # | 目标 |
|---|------|
| B1 | 独立页面呈现心愿卡片：加粗名称、截断描述、类型标签、状态标识、助力数、收藏数、助力/收藏按钮 |
| B2 | 关键词搜索（命中名称或描述） |
| B3 | 多条件过滤：状态（评估中/已规划/开发中/已实现，多选）+ 操作（全部/我创建的/我收藏的/我助力的，单选），两组之间 AND |
| B4 | 排序：助力数、收藏数、创建时间 各支持升/降序 |
| B5 | 助力与收藏实时更新数量并持久化，toggle 语义（再点取消），计数不漂移 |

### 1.2 非目标

- ❌ 类型（模型/上下文记忆/用户体验/其他）维度的过滤器（仅展示标签，不参与筛选）
- ❌ 心愿墙种子/演示数据
- ❌ 反馈或心愿的富文本、图片附件、@提及
- ❌ 站内消息/邮件通知（状态变更后不主动推送）
- ❌ 心愿评论区
- ❌ Alembic 迁移体系（沿用现有 `create_all` + `_ensure_column` 惯例）
- ❌ 前端路由库引入（继续沿用 `App.tsx` 的 `page` 联合类型）

---

## 2. 现状勘察（代码事实）

| # | 事实 | 位置 |
|---|------|------|
| F1 | `/api/feedback` 前缀已被"消息点赞/点踩"占用 | `backend/app/routers/feedback.py:30` |
| F2 | 路由模块直接使用模块级 `async_session`，测试通过 `monkeypatch.setattr(mod, "async_session", db_factory)` 注入 | `routers/feedback.py`、`tests/test_feedback_api.py` |
| F3 | 限流器 `SlidingWindowLimiter.hit(key, limit, window)` 为模块级单例，测试需 `_hits.clear()` | `backend/app/routers/feedback.py` |
| F4 | `User` 模型含 `uid`（工号）、`username`（姓名）、`role` | `backend/app/models.py` |
| F5 | `MessageFeedback` 模型定义于 models.py 第 327 行附近，新模型追加其后 | `backend/app/models.py:327` |
| F6 | `TokenResponse` **不含** `uid`，且无 `/api/auth/me` 端点 | `backend/app/schemas.py` |
| F7 | 鉴权依赖：`get_current_user`、`require_admin` | `backend/app/auth.py:38` / `:60` |
| F8 | 双 router 导出惯例：`library.router` + `library.admin_router`，在 `main.py` 分别 `include_router` | `routers/library.py:36-41`、`main.py:149-150` |
| F9 | `init_db()` 中显式 import 模型列表后 `create_all`；加列走 `_ensure_column` | `backend/app/database.py` |
| F10 | 前端无路由库，`page: "chat" \| "admin" \| "library" \| "kbaccess"` | `frontend/src/App.tsx` |
| F11 | 管理后台 `UxDashboard` 已有 `detailTab: "rounds" \| "feedback"`，`PAGE = 20`，样式为手写 `adminStyles` | `frontend/src/components/UxDashboard.tsx` |
| F12 | Chat 侧边栏结构：`sidebarHeader → statusPanel → sessionsSection`；`userInfo` 行已挤了 4 个按钮（代码注释已抱怨拥挤）；存在 `quickActions` 区域 | `frontend/src/Chat.tsx:2378-2414` |
| F13 | 生产库 PostgreSQL 16，测试库 SQLite → 搜索必须用 `.ilike()`、计数自增必须用 SQL 表达式而非 Python 读改写 | 全局约束 |
| F14 | `api.ts` 提供 `apiCall` 助手；`uxQuery`（第 955 行）为**模块私有**函数，用 `usp.set(k, String(v))` 序列化 → 数组 `["a","b"]` 变为 `status=a%2Cb`（**逗号串，非重复键**） | `frontend/src/api.ts:955-963` |
| F15 | `SlidingWindowLimiter.hit(key, limit, window) -> float`：返回 `0.0` 表示放行，非零表示需等待的秒数 | `backend/app/services/rate_limit.py:18` |

---

## 3. 决策记录

| ID | 议题 | 裁定 |
|----|------|------|
| D1 | 反馈的姓名/工号从哪来 | **服务端从登录态快照写入**，前端不传、不展示可编辑字段 |
| D2 | 管理端"用户反馈"栏目放哪 | **UxDashboard 的第三个子标签**（与 轮次分析 / 点赞点踩 并列） |
| D3 | 反馈状态取值 | **英文 code 存储 + 中文展示**，共 5 项：`open`(未解决,默认) / `resolved`(已解决) / `evaluating`(评估中) / `planned`(已规划) / `developing`(开发中)；下拉可回退到 `open` |
| D4 | 谁可以发布心愿 | **所有登录用户** |
| D5 | 心愿墙形态 | **独立页面**（`page = "wishes"`），非弹窗 |
| D6 | 助力/收藏语义 | **均为 toggle**：未操作→点击即操作；已操作→点击即取消 |
| D7 | 计数实现 | **冗余计数列 `boost_count` / `favorite_count`**，与 action 行在**同一事务**内用 SQL 表达式自增/自减 |
| D8 | 过滤/排序/分页位置 | **全部服务端**完成 |
| D9 | 管理员如何改心愿状态 | **心愿墙卡片内联编辑**（管理员可见状态下拉），不跳转到后台 |
| D10 | 编辑/删除权限 | **作者可编辑自己的心愿**（标题/描述/类型），**管理员可删除任意心愿**；管理员亦可编辑任意心愿 |
| D11 | UI 组件库 | **用户端用 antd 5.29**（已有 `ConfigProvider` 主题），**管理后台沿用手写 `adminStyles`** |
| D12 | 反馈入口位置 | **Chat 侧边栏 `quickActions` 独立一行**，不挤进 `userInfo` 按钮行 |
| D13 | 提交成功反馈 | **toast 致谢 + 立即关闭弹窗**（不停留"感谢页"） |
| D14 | 反馈内容约束 | **5 ~ 2000 字**；限流 **5 次 / 60 秒 / 用户** |
| D15 | 心愿描述截断 | **CSS clamp 3 行** + "展开/收起"就地切换，不弹层 |
| D16 | 心愿删除方式 | **软删除**（`deleted_at`），所有查询收敛到 `_alive()` |
| D17 | 管理员隐藏/恢复 | **管理员可隐藏（软删）+ 可恢复**；后台提供"已隐藏"视图 |
| D18 | 管理后台心愿列表 | **分页 + 状态筛选 + 关键词搜索**（沿用 `PAGE = 20`） |
| D19 | 测试范围 | **只写核心后端 API 测试**，不引入前端测试框架 |
| D20 | 过滤器组合语义 | **状态多选（组内 OR）**、**操作单选（antd Segmented）**、**两组之间 AND** |
| D21 | 默认排序 | **`boost_count DESC, id DESC`**；排序下拉 6 项；所有排序追加 `, id DESC` 稳定次级键 |
| D22 | 文件与表命名 | 路由 `opinion_feedback.py`（导出 `router` + `admin_router`）、`wishes.py`；表 `opinion_feedback` / `wishes` / `wish_actions` |
| D23 | 反馈 → 心愿 | **管理员一键"转为心愿"**，预填反馈内容为心愿描述 |
| D24 | 转化后的联动 | 弹窗**预填** → 管理员确认 → 回写 `linked_wish_id` → 反馈状态置 `evaluating`；已转化的反馈按钮变为**"查看心愿"**（幂等，重复调用返回 409） |
| D25 | 弹窗是否展示工号 | **不展示**（`TokenResponse` 无 `uid`，不为此新增 `/api/auth/me`） |
| D26 | 卡片字段 | **用户视角严格按需求 7 项**；**管理员视角额外显示作者 + 创建日期** |
| D27 | 增补项 | ✅ 管理员可编辑任意心愿；✅ 心愿墙顶部统计条（总数/各状态数/我的贡献）。❌ 类型过滤。❌ 种子数据 |

---

## 4. 领域模型与数据库设计

### 4.1 枚举字典

```python
# backend/app/models.py 追加
class OpinionStatus(str, Enum):
    open = "open"              # 未解决（默认）
    resolved = "resolved"      # 已解决
    evaluating = "evaluating"  # 评估中
    planned = "planned"        # 已规划
    developing = "developing"  # 开发中

class WishStatus(str, Enum):
    evaluating = "evaluating"  # 评估中（默认）
    planned = "planned"        # 已规划
    developing = "developing"  # 开发中
    done = "done"              # 已实现

class WishType(str, Enum):
    model = "model"            # 模型
    memory = "memory"          # 上下文记忆
    ux = "ux"                  # 用户体验
    other = "other"            # 其他（默认）

class WishActionType(str, Enum):
    boost = "boost"            # 助力
    favorite = "favorite"      # 收藏
```

中文展示映射放在**前端**（`OPINION_STATUS_LABEL` / `WISH_STATUS_LABEL` / `WISH_TYPE_LABEL`），后端只存 code，避免双份字典。

### 4.2 表结构

#### `opinion_feedback`

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `id` | Integer | PK, autoincrement | |
| `user_id` | Integer | NOT NULL, index | **无外键**（与 `MessageFeedback` 一致，避免用户删除时级联阻塞） |
| `name` | String(64) | NOT NULL | 提交时刻的 `user.username` **快照** |
| `uid` | String(64) | NULL | 提交时刻的 `user.uid` **快照**，可为空 |
| `content` | Text | NOT NULL | 5 ~ 2000 字 |
| `status` | String(16) | NOT NULL, default `"open"` | 见 `OpinionStatus` |
| `linked_wish_id` | Integer | NULL | 转化后指向 `wishes.id`，**无外键**（心愿软删后仍可查） |
| `created_at` | DateTime | NOT NULL, default `utcnow` | 反馈时间 |

索引：`Index("ix_opinion_feedback_status_created", "status", "created_at")` —— 服务后台"按状态筛选 + 时间倒序"主查询。

#### `wishes`

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `id` | Integer | PK | |
| `author_id` | Integer | NOT NULL, index | 无外键 |
| `title` | String(120) | NOT NULL | 心愿名称 |
| `description` | Text | NOT NULL, default `""` | 心愿描述，前端 clamp 3 行 |
| `type` | String(16) | NOT NULL, default `"other"` | 见 `WishType` |
| `status` | String(16) | NOT NULL, default `"evaluating"` | 见 `WishStatus` |
| `boost_count` | Integer | NOT NULL, default `0` | 冗余计数 |
| `favorite_count` | Integer | NOT NULL, default `0` | 冗余计数 |
| `deleted_at` | DateTime | NULL | 软删除标记，NULL 表示存活 |
| `created_at` | DateTime | NOT NULL | |
| `updated_at` | DateTime | NOT NULL, onupdate | |

索引：
- `Index("ix_wishes_alive_boost", "deleted_at", "boost_count")`
- `Index("ix_wishes_alive_created", "deleted_at", "created_at")`
- `Index("ix_wishes_status", "status")`

#### `wish_actions`

| 列 | 类型 | 约束 |
|----|------|------|
| `id` | Integer | PK |
| `wish_id` | Integer | FK → `wishes.id` **ON DELETE CASCADE**, NOT NULL |
| `user_id` | Integer | NOT NULL |
| `action` | String(16) | NOT NULL（`boost` / `favorite`） |
| `created_at` | DateTime | NOT NULL |

约束：`UniqueConstraint("wish_id", "user_id", "action", name="uq_wish_action_user")`

> `wish_id` 用真外键 + CASCADE：心愿若被物理清理，action 行自动跟随；`user_id` 不用外键，与全站惯例一致。

### 4.3 模型代码骨架

```python
class OpinionFeedback(Base):
    __tablename__ = "opinion_feedback"
    __table_args__ = (
        Index("ix_opinion_feedback_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(64), nullable=False)
    uid = Column(String(64), nullable=True)
    content = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default=OpinionStatus.open.value)
    linked_wish_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Wish(Base):
    __tablename__ = "wishes"
    __table_args__ = (
        Index("ix_wishes_alive_boost", "deleted_at", "boost_count"),
        Index("ix_wishes_alive_created", "deleted_at", "created_at"),
        Index("ix_wishes_status", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    author_id = Column(Integer, nullable=False, index=True)
    title = Column(String(120), nullable=False)
    description = Column(Text, nullable=False, default="")
    type = Column(String(16), nullable=False, default=WishType.other.value)
    status = Column(String(16), nullable=False, default=WishStatus.evaluating.value)
    boost_count = Column(Integer, nullable=False, default=0)
    favorite_count = Column(Integer, nullable=False, default=0)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class WishAction(Base):
    __tablename__ = "wish_actions"
    __table_args__ = (
        UniqueConstraint("wish_id", "user_id", "action", name="uq_wish_action_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    wish_id = Column(Integer, ForeignKey("wishes.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, nullable=False)
    action = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
```

### 4.4 迁移策略

三张表均为**全新表**，无既有列变更 → **不需要 `_ensure_column`**。

`backend/app/database.py` 的 `init_db()` 中，在模型 import 列表追加：

```python
from .models import (  # noqa: F401  (ensure tables are registered)
    ...,
    MessageFeedback,
    OpinionFeedback,   # 新增
    Wish,              # 新增
    WishAction,        # 新增
)
```

`Base.metadata.create_all()` 会在 PostgreSQL 与 SQLite 上分别建表；索引与唯一约束随 DDL 一并创建。

---

## 5. API 契约

### 5.1 意见反馈

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| `POST` | `/api/opinions` | `get_current_user` | 提交反馈 |
| `GET` | `/api/admin/opinions` | `require_admin` | 分页 + 状态筛选 + 关键词搜索 |
| `PATCH` | `/api/admin/opinions/{id}` | `require_admin` | 修改状态 |
| `POST` | `/api/admin/opinions/{id}/to-wish` | `require_admin` | 一键转为心愿 |

#### `POST /api/opinions`

请求：
```json
{ "content": "希望支持导出对话为 Markdown" }
```

响应 `201`：
```json
{ "id": 42, "status": "open", "created_at": "2026-09-15T10:12:33Z" }
```

校验与错误：

| 场景 | 状态码 | body |
|------|--------|------|
| `content` strip 后长度 < 5 | 422 | `{"detail": "反馈内容至少 5 个字"}` |
| 长度 > 2000 | 422 | `{"detail": "反馈内容不能超过 2000 字"}` |
| 60 秒内已提交 ≥ 5 次 | 429 | `{"detail": "提交过于频繁，请稍后再试"}` |

限流键：`f"opinion:{user.id}"`，`wait = _opinion_limiter.hit(key, 5, 60)`；`wait > 0` → 429（F15：返回值为 0.0 表示放行）。

> 注意：长度校验**先于**限流，避免无效请求消耗配额。

#### `GET /api/admin/opinions`

Query：`page`(默认 1)、`page_size`(默认 20，上限 100)、`status`(逗号分隔串，如 `open,planned`)、`q`(关键词)

```python
async def list_opinions(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,   # "open,planned" → split(",")
    q: str | None = None,
) -> ...
```

> 采用逗号串而非 FastAPI 的 `List[str]` 重复键，理由见 §7.5（与既有 `uxQuery` 的序列化行为一致）。

响应：
```json
{
  "items": [
    {
      "id": 42,
      "name": "张三",
      "uid": "10086",
      "content": "希望支持导出对话为 Markdown",
      "status": "open",
      "linked_wish_id": null,
      "created_at": "2026-09-15T10:12:33Z"
    }
  ],
  "total": 137,
  "page": 1,
  "page_size": 20,
  "status_counts": { "open": 90, "resolved": 20, "evaluating": 15, "planned": 8, "developing": 4 }
}
```

- 排序固定 `created_at DESC, id DESC`
- `q` 命中 `content` **或** `name` **或** `uid`（`or_` 组合，均 `.ilike()`）
- `status_counts` 一次聚合查询返回，供后台顶部徽标

#### `PATCH /api/admin/opinions/{id}`

请求：`{ "status": "planned" }`
响应 `200`：`{ "id": 42, "status": "planned" }`
错误：非法枚举 → 422；不存在 → 404。

#### `POST /api/admin/opinions/{id}/to-wish`

请求（弹窗确认后提交，字段均可被管理员改过）：
```json
{
  "title": "支持导出对话为 Markdown",
  "description": "希望支持导出对话为 Markdown",
  "type": "ux"
}
```

响应 `201`：
```json
{ "feedback_id": 42, "wish_id": 88, "status": "evaluating", "linked_wish_id": 88 }
```

事务内三步（原子）：
1. `INSERT wishes`（`author_id` = 当前管理员 id，`status = "evaluating"`）
2. `UPDATE opinion_feedback SET linked_wish_id = :wid, status = 'evaluating' WHERE id = :fid`
3. commit

错误：`linked_wish_id` 已非空 → **409** `{"detail": "该反馈已转化为心愿"}`；反馈不存在 → 404。

### 5.2 心愿墙

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| `GET` | `/api/wishes` | `get_current_user` | 列表（搜索/过滤/排序/分页） |
| `POST` | `/api/wishes` | `get_current_user` | 发布心愿 |
| `PATCH` | `/api/wishes/{id}` | 作者 or 管理员 | 编辑标题/描述/类型；管理员可改状态 |
| `DELETE` | `/api/wishes/{id}` | 管理员 | 软删除 |
| `POST` | `/api/wishes/{id}/restore` | 管理员 | 恢复 |
| `POST` | `/api/wishes/{id}/actions` | `get_current_user` | 助力/收藏 toggle |
| `GET` | `/api/wishes/stats` | `get_current_user` | 顶部统计条 |

#### `GET /api/wishes`

Query 参数：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `q` | str | — | 关键词，命中 `title` **或** `description` |
| `status` | str | — | 逗号分隔串（`evaluating,planned`），后端 split 后组内 OR |
| `scope` | str | `all` | `all` / `mine` / `favorited` / `boosted`，单选 |
| `sort` | str | `boost_desc` | 见下表 |
| `page` | int | 1 | |
| `page_size` | int | 20 | 上限 100 |
| `include_hidden` | bool | false | 仅管理员有效，为真时含软删记录 |

`sort` 取值（6 项）：

| code | ORDER BY |
|------|----------|
| `boost_desc` | `boost_count DESC, id DESC` |
| `boost_asc` | `boost_count ASC, id DESC` |
| `favorite_desc` | `favorite_count DESC, id DESC` |
| `favorite_asc` | `favorite_count ASC, id DESC` |
| `created_desc` | `created_at DESC, id DESC` |
| `created_asc` | `created_at ASC, id DESC` |

响应：
```json
{
  "items": [
    {
      "id": 88,
      "title": "支持导出对话为 Markdown",
      "description": "……",
      "type": "ux",
      "status": "evaluating",
      "boost_count": 12,
      "favorite_count": 5,
      "created_at": "2026-09-15T10:20:00Z",
      "my_boosted": true,
      "my_favorited": false,
      "is_mine": false,
      "author_name": "张三",
      "deleted_at": null
    }
  ],
  "total": 63,
  "page": 1,
  "page_size": 20
}
```

- `author_name`：**LEFT JOIN `users`** 取**当前** `username`（与反馈的快照策略刻意不同——心愿是长期展示的社区内容，作者改名后应跟随；反馈是审计记录，须保留提交时刻原貌）
- `author_name` 字段仅当请求者为管理员时返回非空（普通用户前端也不渲染）
- `my_boosted` / `my_favorited` / `is_mine` 由服务端按当前登录用户计算

**`scope` 实现要点：**

```python
if scope == "mine":
    stmt = stmt.where(Wish.author_id == user.id)
elif scope == "favorited":
    stmt = stmt.where(
        select(WishAction.id)
        .where(WishAction.wish_id == Wish.id,
               WishAction.user_id == user.id,
               WishAction.action == "favorite")
        .exists()
    )
elif scope == "boosted":
    # 同上，action == "boost"
```

用 **EXISTS 相关子查询**而非 JOIN：JOIN 会因一个心愿多条 action 行而放大结果集，导致 `total` 与分页错乱。

**`my_boosted` / `my_favorited` 计算：** 列表查完后，一次 `SELECT wish_id, action FROM wish_actions WHERE user_id = :me AND wish_id IN (:ids)` 取回本页相关行，在 Python 侧组装成两个 set 再回填。避免 N+1，也避免每行两个 EXISTS 子查询。

#### `POST /api/wishes`

请求：
```json
{ "title": "支持导出对话为 Markdown", "description": "……", "type": "ux" }
```

- `title`：1 ~ 120 字（strip 后非空）
- `description`：0 ~ 5000 字
- `type`：枚举校验，缺省 `other`

响应 `201`：返回完整心愿对象（`boost_count = favorite_count = 0`）
限流：`f"wish:create:{user.id}"`，**3 次 / 60 秒** → 429

#### `PATCH /api/wishes/{id}`

请求（字段全部可选）：
```json
{ "title": "…", "description": "…", "type": "model", "status": "planned" }
```

权限矩阵：

| 字段 | 作者 | 管理员 |
|------|------|--------|
| `title` / `description` / `type` | ✅ | ✅ |
| `status` | ❌ (403) | ✅ |

错误：非作者非管理员 → 403；不存在或已软删 → 404；无任何有效字段 → 422。
成功后刷新 `updated_at`（由 `onupdate` 自动完成）。

#### `DELETE /api/wishes/{id}`

软删除：`UPDATE wishes SET deleted_at = utcnow() WHERE id = :id AND deleted_at IS NULL`。
`rowcount == 0` → 404（幂等保护：重复删除返回 404 而非静默 200）。
响应 `200`：`{ "id": 88, "deleted": true }`。**不删除 action 行**（恢复后计数与用户操作状态需保持一致）。

#### `POST /api/wishes/{id}/restore`

`UPDATE wishes SET deleted_at = NULL WHERE id = :id AND deleted_at IS NOT NULL`；`rowcount == 0` → 404。
响应 `200`：`{ "id": 88, "deleted": false }`。

#### `POST /api/wishes/{id}/actions`（toggle 核心）

请求：`{ "action": "boost" }`（`boost` | `favorite`）
响应 `200`：
```json
{ "wish_id": 88, "action": "boost", "active": true, "boost_count": 13, "favorite_count": 5 }
```
限流：`f"wish:action:{user.id}"`，**60 次 / 60 秒** → 429

实现（三重防线，见 §6.1）：

```python
async with async_session() as db:
    async with db.begin():
        wish = (await db.execute(
            select(Wish).where(Wish.id == wish_id, Wish.deleted_at.is_(None))
                          .with_for_update()
        )).scalar_one_or_none()
        if wish is None:
            raise HTTPException(404, "心愿不存在")

        existing = (await db.execute(
            select(WishAction).where(
                WishAction.wish_id == wish_id,
                WishAction.user_id == user.id,
                WishAction.action == action,
            )
        )).scalar_one_or_none()

        col = getattr(Wish, "boost_count" if action == "boost" else "favorite_count")

        if existing is None:
            db.add(WishAction(wish_id=wish_id, user_id=user.id, action=action))
            await db.execute(
                update(Wish).where(Wish.id == wish_id).values({col.key: col + 1})
            )
            active = True
        else:
            await db.delete(existing)
            # 双方言安全的"减到 0 为止"写法（优先于 func.greatest，后者在旧版 SQLite 上不可用）
            await db.execute(
                update(Wish).where(Wish.id == wish_id)
                            .values({col.key: case((col > 0, col - 1), else_=0)})
            )
            active = False

    # 事务已提交，重新读取权威计数（避免使用 ORM 对象上过期的 Python 值）
    fresh = (await db.execute(
        select(Wish.boost_count, Wish.favorite_count).where(Wish.id == wish_id)
    )).one()
    return {..., "boost_count": fresh.boost_count, "favorite_count": fresh.favorite_count}
```

> **递减写法选型**：采用 `case((col > 0, col - 1), else_=0)` 而非 `func.greatest(col - 1, 0)`。后者在 PostgreSQL 上没问题，但 SQLite 需 3.44+ 才有多参数 `max()` 标量函数，SQLAlchemy 的 `greatest` 在旧版 SQLite 方言下可能编译失败。`case` 写法编译为标准 `CASE WHEN`，两种方言通吃，无需条件分支。
>
> **提交后重查计数**：`with_for_update()` 锁定的 ORM 对象在 `db.begin()` 提交后属性已过期，直接读 `wish.boost_count` 会触发惰性加载——在 async 上下文中抛 `MissingGreenlet`。因此必须重新 `select` 一次权威值（`expire_on_commit=False` 虽已配置，但并发下该对象内存值仍可能不是最新，重查更可靠）。

#### `GET /api/wishes/stats`

响应：
```json
{
  "total": 63,
  "by_status": { "evaluating": 30, "planned": 18, "developing": 10, "done": 5 },
  "mine": 4,
  "my_boosted": 11,
  "my_favorited": 7
}
```

### 5.3 路由注册

`backend/app/main.py`：

```python
from .routers import (..., opinion_feedback, wishes)

app.include_router(opinion_feedback.router)
app.include_router(opinion_feedback.admin_router)
app.include_router(wishes.router)
```

`opinion_feedback.py`：
```python
router = APIRouter(prefix="/api/opinions", tags=["opinions"],
                   dependencies=[Depends(get_current_user)])
admin_router = APIRouter(prefix="/api/admin/opinions", tags=["opinions"],
                         dependencies=[Depends(require_admin)])
```

`wishes.py`：
```python
router = APIRouter(prefix="/api/wishes", tags=["wishes"],
                   dependencies=[Depends(get_current_user)])
```
（管理员专属操作 `DELETE` / `restore` / 改状态 在**函数内**调用 `require_admin` 依赖，因同前缀下混用两种鉴权）

---

## 6. 关键实现细节

### 6.1 计数一致性：三重防线

| 防线 | 机制 | 防住什么 |
|------|------|----------|
| 1 | `UniqueConstraint(wish_id, user_id, action)` | 并发双击 / 网络重放导致的重复 action 行 |
| 2 | `select(...).with_for_update()` 行锁 | 两个请求同时读到 `existing is None` 后双双 INSERT |
| 3 | `values({col: col + 1})` SQL 级自增（非 Python `+= 1`） | 读-改-写窗口内的丢失更新 |

递减用 `case((col > 0, col - 1), else_=0)` 兜底（选型理由见 §5.2），保证计数**永不出现负数**。

`IntegrityError` 捕获策略（沿用 `routers/feedback.py` 惯例）：捕获后 **rollback**，并只回显入参与一次重查的计数，**不要**在异常路径上访问已过期 ORM 对象的惰性属性（会触发 `MissingGreenlet`）。

### 6.2 软删除收敛

`wishes.py` 内定义唯一入口：

```python
def _alive(include_hidden: bool = False):
    return select(Wish) if include_hidden else select(Wish).where(Wish.deleted_at.is_(None))
```

所有列表、详情、toggle、编辑路径**必须**经过 `_alive()`，禁止裸写 `select(Wish)`。`include_hidden=True` 仅在 `require_admin` 校验通过后允许传入。

### 6.3 搜索通配符转义

用户输入 `%` `_` `\` 会被 `ilike` 当作通配符：

```python
def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

# 使用
Wish.title.ilike(f"%{_escape_like(q)}%", escape="\\")
```

SQLite 与 PostgreSQL 均支持 `ESCAPE` 子句，方言安全。

### 6.4 快照 vs JOIN

| 场景 | 策略 | 理由 |
|------|------|------|
| 意见反馈的 `name` / `uid` | **写入时快照** | 审计记录，须保留提交时刻的身份原貌；后台表格不依赖 `users` 表存活性 |
| 心愿的 `author_name` | **查询时 LEFT JOIN `users`** | 社区长期展示内容，作者改名后应跟随；LEFT JOIN 保证用户被删后心愿仍可展示（`author_name` 为 `null` → 前端显示"已注销用户"） |

### 6.5 `linked_wish_id` 幂等

`to-wish` 端点先检查 `fb.linked_wish_id is not None` → 409。检查与写入在同一事务内，配合 `UPDATE ... WHERE id = :fid AND linked_wish_id IS NULL` 的 `rowcount` 判断，双重保证不会生成两条心愿。

### 6.6 限流器实例

```python
# opinion_feedback.py
_opinion_limiter = SlidingWindowLimiter()

# wishes.py
_wish_create_limiter = SlidingWindowLimiter()
_wish_action_limiter = SlidingWindowLimiter()
```

统一调用形态（`hit()` 返回 float，`0.0` 为放行，见 F15）：

```python
wait = _opinion_limiter.hit(f"opinion:{user.id}", 5, 60)
if wait > 0:
    raise HTTPException(429, f"提交过于频繁，请 {int(wait) + 1} 秒后再试")
```

测试中必须 `monkeypatch` 或 `_limiter._hits.clear()`，否则用例间相互污染（见 `tests/test_feedback_api.py` 既有做法）。

---

## 7. 前端设计

### 7.1 信息架构

```
App.tsx  page: "chat" | "admin" | "library" | "kbaccess" | "wishes"   ← 新增 "wishes"
│
├── Chat.tsx
│   └── 侧边栏 quickActions 行                                        ← D12
│       ├── [💬 意见反馈]  → 打开 <OpinionFeedbackModal />
│       └── [🌟 心愿墙]    → onNavigate("wishes")
│
├── WishWall.tsx（新页面）                                            ← D5
│   ├── 顶部统计条（total / by_status / mine / my_boosted / my_favorited）← D27
│   ├── 工具栏：搜索框 | 状态多选 | scope Segmented | 排序下拉 | [发布心愿]
│   ├── 卡片网格
│   └── 分页器
│
└── UxDashboard.tsx
    └── detailTab: "rounds" | "feedback" | "opinions"                 ← D2
        （原 "反馈" 文案改为 "点赞点踩"，避免与"用户反馈"混淆）
        └── 用户反馈表格 + 状态下拉 + [转为心愿] 按钮
```

### 7.2 `OpinionFeedbackModal.tsx`（新组件）

- antd `Modal` + `Input.TextArea`（`rows={6}`, `showCount`, `maxLength={2000}`）
- 标题：`意见反馈`；副标题一行灰字：`我们会认真阅读每一条反馈`
- **不展示姓名/工号输入框**（D1 + D25：服务端从登录态取）
- 底部：`取消`（关闭并清空）/ `提交`（`loading` 态，禁用防重复提交）
- 客户端预校验：strip 后 < 5 字 → 提交按钮 `disabled`，下方红字提示"请至少输入 5 个字"
- 成功：`message.success("感谢你的反馈！")` + **立即 `onClose()`**（D13），不渲染感谢页
- 失败：429 → `message.warning(detail)`；422 → `message.error(detail)`；弹窗**保持打开**且内容不清空
- 关闭时若有内容 → antd `Modal.confirm` 二次确认，防误关丢失

### 7.3 `WishWall.tsx`（新组件）

**卡片字段（用户视角，严格 7 项，D26）：**

1. 心愿名称（`font-weight: 600`，加粗）
2. 描述（CSS `-webkit-line-clamp: 3`，超长显示"展开"，就地切换，D15）
3. 类型标签（antd `Tag`，四色映射：模型/上下文记忆/用户体验/其他）
4. 状态标识（antd `Tag`：评估中=default / 已规划=processing / 开发中=warning / 已实现=success）
5. 助力数量 + 助力按钮（`my_boosted` 为真时高亮 + 实心图标）
6. 收藏数量 + 收藏按钮（`my_favorited` 为真时高亮）
7. （操作行）

**管理员视角额外：** 作者姓名、创建日期、状态下拉（内联改状态，D9）、编辑按钮、隐藏/恢复按钮。
**作者视角额外：** 编辑按钮。

**工具栏：**

| 控件 | 组件 | 行为 |
|------|------|------|
| 搜索 | antd `Input.Search` | 300ms 防抖，回车/点击立即触发，`page` 重置为 1 |
| 状态过滤 | antd `Select mode="multiple"` | 4 选项，组内 OR，变更即请求 |
| 操作过滤 | antd `Segmented` | 全部 / 我创建的 / 我收藏的 / 我助力的，单选（D20） |
| 排序 | antd `Select` | 6 项，默认"助力数从多到少"（D21） |
| 发布 | antd `Button type="primary"` | 打开发布弹窗 |

**乐观更新：** 点击助力/收藏立即在本地 ±1 并翻转按钮态，请求失败则回滚 + `message.error`。避免等待往返造成的"点了没反应"。

**发布/编辑弹窗：** `title`（`Input`, maxLength 120, showCount）、`description`（`TextArea`, rows 5, maxLength 5000）、`type`（`Select` 4 项）。编辑时管理员额外可见 `status` 下拉。

### 7.4 `UxDashboard.tsx` 的 `opinions` 子标签

沿用手写 `adminStyles`（D11）：

- 顶部：状态徽标行（来自 `status_counts`）+ 搜索框 + 状态筛选
- 表格列：`反馈时间` / `姓名` / `工号` / `反馈内容`（长文本 clamp 2 行 + 点击展开）/ `解决状态`（`<select>` 5 项，`onChange` 立即 PATCH）/ `操作`（`转为心愿` 或 `查看心愿`）
- 分页：沿用 `PAGE = 20`
- 状态变更失败 → 回滚 `<select>` 的 `value` 并提示
- `转为心愿` → 弹出预填弹窗（标题取内容前 30 字，描述取全文，类型默认 `other`）→ 提交 `to-wish` → 成功后按钮变 `查看心愿`（跳 `page="wishes"` 并高亮该心愿）；409 → 提示"该反馈已转化"并刷新该行

### 7.5 `api.ts` 增量

```ts
export type OpinionStatus = "open" | "resolved" | "evaluating" | "planned" | "developing";
export type WishStatus = "evaluating" | "planned" | "developing" | "done";
export type WishType = "model" | "memory" | "ux" | "other";
export type WishScope = "all" | "mine" | "favorited" | "boosted";
export type WishSort = "boost_desc" | "boost_asc" | "favorite_desc"
                     | "favorite_asc" | "created_desc" | "created_asc";

export interface OpinionItem { id: number; name: string; uid: string | null;
  content: string; status: OpinionStatus; linked_wish_id: number | null; created_at: string }
export interface OpinionListResp { items: OpinionItem[]; total: number; page: number;
  page_size: number; status_counts: Record<OpinionStatus, number> }

export interface WishItem { id: number; title: string; description: string;
  type: WishType; status: WishStatus; boost_count: number; favorite_count: number;
  created_at: string; my_boosted: boolean; my_favorited: boolean; is_mine: boolean;
  author_name?: string | null; deleted_at?: string | null }
export interface WishListResp { items: WishItem[]; total: number; page: number; page_size: number }
export interface WishStats { total: number; by_status: Record<WishStatus, number>;
  mine: number; my_boosted: number; my_favorited: number }

export async function submitOpinion(content: string) { /* POST /api/opinions */ }
export async function listOpinions(params: { page?: number; status?: OpinionStatus[]; q?: string }) { /* GET */ }
export async function patchOpinionStatus(id: number, status: OpinionStatus) { /* PATCH */ }
export async function opinionToWish(id: number, body: { title: string; description: string; type: WishType }) { /* POST */ }

export async function listWishes(params: { q?: string; status?: WishStatus[]; scope?: WishScope;
  sort?: WishSort; page?: number; page_size?: number; include_hidden?: boolean }) { /* GET */ }
export async function createWish(body: { title: string; description: string; type: WishType }) { /* POST */ }
export async function patchWish(id: number, body: Partial<{ title: string; description: string;
  type: WishType; status: WishStatus }>) { /* PATCH */ }
export async function deleteWish(id: number) { /* DELETE */ }
export async function restoreWish(id: number) { /* POST restore */ }
export async function toggleWishAction(id: number, action: "boost" | "favorite") { /* POST actions */ }
export async function getWishStats() { /* GET stats */ }
```

**数组参数序列化（已核实 F14）**：`uxQuery` 用 `usp.set(k, String(v))`，数组 `["a","b"]` 会被序列化为 `status=a%2Cb`，即**逗号串**而非重复键。因此：

- 前端：数组参数直接交给 `uxQuery`，无需改造；但 `uxQuery` 当前是 `api.ts` 的**模块私有函数**，需 `export` 出来（或在 `api.ts` 内新增请求函数时直接调用，不导出——**推荐后者**，所有新请求函数都写在 `api.ts` 里，与既有 `api` 对象风格一致）
- 后端：多值参数一律声明为 `status: str | None`，内部 `[s for s in status.split(",") if s]`，并逐项校验枚举合法性（非法值 → 422，不静默忽略）

这样避免了引入 FastAPI `List[str]` 重复键与前端 `URLSearchParams.append` 的双向改造，改动面最小。

### 7.6 样式

- 心愿墙：新增 `frontend/src/wishStyles.ts`（或复用 antd token），卡片网格 `repeat(auto-fill, minmax(320px, 1fr))`，移动端单列
- Chat 侧边栏：`chatStyles` 追加 `quickActionsRow` / `quickActionButton` 两个键
- 后台：`adminStyles` 追加 `opinionContentCell` / `statusSelect` 键

---

## 8. 测试计划（D19：只写核心后端测试）

新增 `backend/tests/test_opinion_feedback_api.py` 与 `backend/tests/test_wishes_api.py`，复用 `conftest.py` 的 `app_client_factory(routers, user_id, username, role, uid)`。

### 8.1 `test_opinion_feedback_api.py`（10 例）

| # | 用例 | 断言 |
|---|------|------|
| 1 | 提交合法反馈 | 201；库中 `name`/`uid` 为登录态快照；`status == "open"`；`created_at` 非空 |
| 2 | 内容 4 字 | 422，且不落库 |
| 3 | 内容 2001 字 | 422 |
| 4 | 60 秒内第 6 次提交 | 429 |
| 5 | 未登录提交 | 401 |
| 6 | 普通用户访问 `GET /api/admin/opinions` | 403 |
| 7 | 管理员列表分页 | `total` 正确；第 2 页与第 1 页无交集；按 `created_at DESC` |
| 8 | 状态筛选 + 关键词搜索组合 | 结果集符合 AND 语义；`q` 命中 `name` 也生效 |
| 9 | `PATCH` 状态为 `planned` | 200；重查为 `planned`；非法值 → 422 |
| 10 | `to-wish` | 201；生成心愿且 `author_id` = 管理员；反馈 `linked_wish_id` 回填、`status == "evaluating"`；**重复调用 → 409** |

### 8.2 `test_wishes_api.py`（17 例）

| # | 用例 | 断言 |
|---|------|------|
| 1 | 发布心愿 | 201；计数为 0；`status == "evaluating"` |
| 2 | 标题为空 / 超 120 字 | 422 |
| 3 | 发布限流（第 4 次） | 429 |
| 4 | 助力 toggle：首次 | 200 `active=true`，`boost_count == 1` |
| 5 | 助力 toggle：再次 | 200 `active=false`，`boost_count == 0`，**不为负** |
| 6 | 收藏与助力互不干扰 | 各自计数独立 |
| 7 | 唯一约束生效 | 绕过 API 直接插入重复 action 行 → `IntegrityError` |
| 8 | 并发助力（同用户 2 线程） | 最终 `boost_count == 1`，action 行仅 1 条 |
| 9 | 并发助力（不同用户 N=10） | `boost_count == 10` |
| 10 | 关键词搜索 | 命中标题 or 描述；`%` / `_` 被正确转义（搜 `"100%"` 不返回全表） |
| 11 | 状态多选过滤 | 组内 OR |
| 12 | `scope=mine` / `favorited` / `boosted` | 各自结果正确，且 `total` 未被 JOIN 放大 |
| 13 | scope + status + q 组合 | AND 语义 |
| 14 | 6 种排序 | 顺序正确；同值时以 `id DESC` 稳定 |
| 15 | 作者 PATCH 标题/描述/类型 | 200；作者 PATCH `status` → 403 |
| 16 | 管理员 PATCH `status` / 编辑任意心愿 | 200 |
| 17 | 软删除 + 恢复 | 删除后普通列表不可见、`include_hidden=true`（管理员）可见；toggle 已删心愿 → 404；恢复后计数与 `my_boosted` 状态保持 |

### 8.3 测试隔离清单

每个测试文件顶部 fixture 必须：

```python
monkeypatch.setattr(opinion_feedback, "async_session", db_factory)
monkeypatch.setattr(wishes, "async_session", db_factory)
opinion_feedback._opinion_limiter._hits.clear()
wishes._wish_create_limiter._hits.clear()
wishes._wish_action_limiter._hits.clear()
```

---

## 9. 实施拆分

### 9.1 后端（5 步，可独立提交）

| 步 | 内容 | 产出 |
|----|------|------|
| B1 | `models.py` 追加 3 个枚举 + 3 个模型；`database.py` 的 `init_db()` import 列表追加 | 建表生效 |
| B2 | `schemas.py` 追加请求/响应模型（`OpinionCreate`、`OpinionItem`、`OpinionListResp`、`OpinionStatusUpdate`、`WishCreate`、`WishUpdate`、`WishItem`、`WishListResp`、`WishActionReq`、`WishStats`、`ToWishReq`） | Pydantic 校验层 |
| B3 | `routers/opinion_feedback.py`（`router` + `admin_router`，含 `to-wish` 事务） | 4 个端点 |
| B4 | `routers/wishes.py`（列表/发布/编辑/软删/恢复/toggle/stats） | 7 个端点 |
| B5 | `main.py` 注册 3 个 router；跑 `pytest backend/tests/test_opinion_feedback_api.py backend/tests/test_wishes_api.py` | 27 个测试用例通过 |

### 9.2 前端（7 步）

| 步 | 内容 |
|----|------|
| F1 | `api.ts` 追加类型与 11 个请求函数（多值参数交给既有 `uxQuery`，天然生成逗号串，见 §7.5） |
| F2 | `OpinionFeedbackModal.tsx` |
| F3 | `Chat.tsx` 侧边栏 `quickActions` 行接入两个入口 + `chatStyles` 新样式键 |
| F4 | `App.tsx` `page` 联合类型追加 `"wishes"` + 渲染分支 + 导航透传 |
| F5 | `WishWall.tsx`（统计条 / 工具栏 / 卡片网格 / 分页 / 乐观更新） |
| F6 | 心愿发布 & 编辑弹窗 |
| F7 | `UxDashboard.tsx` 追加 `opinions` 子标签（表格 + 状态下拉 + 转心愿弹窗），原"反馈"文案改"点赞点踩" |

### 9.3 手工验证清单

**意见反馈**

- [ ] 侧边栏 `quickActions` 行两个按钮视觉明显、不与既有 4 个按钮拥挤
- [ ] 弹窗打开时输入框自动 focus
- [ ] 反馈内容 4 字时提交按钮 disabled，且下方有红字提示
- [ ] 反馈内容恰好 5 字时可提交
- [ ] 输入超过 2000 字时 `showCount` 变红且无法继续输入
- [ ] 有内容时点关闭 → 弹出二次确认；确认后内容清空
- [ ] 提交成功 → toast「感谢你的反馈！」+ 弹窗立即关闭，无停留
- [ ] 提交后重新打开弹窗 → 输入框为空
- [ ] 60 秒内连续提交 6 次 → 第 6 次 toast 警告，弹窗保持打开且内容不丢
- [ ] 断网提交 → 错误提示，弹窗保持打开且内容不丢
- [ ] 弹窗内**不出现**工号/姓名输入框
- [ ] 后台"用户反馈"子标签：表格 6 列齐全，姓名为提交时刻快照（改用户名后旧反馈仍显示旧名）
- [ ] 后台原"反馈"标签文案已改为"点赞点踩"，数据未受影响
- [ ] 状态下拉切换 → 立即持久化 → 刷新页面后仍为新状态
- [ ] 状态下拉可选回"未解决"
- [ ] 关键词搜索命中内容 / 姓名 / 工号 三种情况
- [ ] 状态筛选 + 搜索 组合生效（AND）
- [ ] 分页翻页无重复、无遗漏，`total` 与徽标数一致
- [ ] 顶部状态徽标数字随状态变更实时刷新

**反馈转心愿**

- [ ] 点"转为心愿" → 弹窗预填（标题=内容前 30 字，描述=全文，类型=其他）
- [ ] 预填内容可编辑后提交
- [ ] 转化成功 → 该行按钮变"查看心愿"，反馈状态变"评估中"
- [ ] 点"查看心愿" → 跳转心愿墙并定位/高亮该心愿
- [ ] 已转化的反馈再次触发接口（如双开标签页）→ 409 提示"该反馈已转化"，前端刷新该行
- [ ] 新生成的心愿作者为操作管理员

**心愿墙**

- [ ] 独立页面进入/返回导航正常，浏览器刷新后停留在心愿墙（若已实现 page 持久化）
- [ ] 卡片严格 7 项字段；名称加粗；类型标签四色正确；状态标识四态正确
- [ ] 描述超 3 行被 clamp，出现"展开"，点击就地展开为"收起"，不弹层
- [ ] 描述恰好 3 行内不出现"展开"
- [ ] 顶部统计条：总数 / 各状态数 / 我创建的 / 我助力的 / 我收藏的 数字与实际一致
- [ ] 助力按钮点击 → 数字立即 +1、图标高亮（乐观更新，无等待感）
- [ ] 再点助力 → 数字 -1、图标恢复
- [ ] 收藏与助力互不影响
- [ ] 快速连点助力 5 次 → 最终状态与计数一致，无漂移、无负数
- [ ] 断网点击助力 → 数字回滚 + 错误 toast
- [ ] 搜索命中标题、搜索命中描述，两种情况均生效
- [ ] 搜索 `100%` 不返回全表（通配符已转义）
- [ ] 状态多选：选 2 项 = 两者并集；清空 = 全部
- [ ] 操作 Segmented 四态切换正确，且为单选
- [ ] 「我收藏的」+ 状态「已实现」组合 = 交集
- [ ] 6 种排序逐一验证方向正确；助力数相同的心愿顺序稳定（不随刷新跳动）
- [ ] 切换任一过滤条件后 page 重置为 1
- [ ] 过滤后无结果 → 显示空态插画/文案，而非空白
- [ ] 分页总数与过滤条件联动
- [ ] 发布心愿：标题必填、超 120 字受限；描述可为空
- [ ] 发布成功后列表首位出现新心愿（默认排序下按助力数，需切"最新创建"验证）
- [ ] 发布限流：60 秒内第 4 次发布被拒并提示
- [ ] 作者可编辑自己的心愿（标题/描述/类型），**看不到**状态下拉
- [ ] 非作者非管理员**看不到**编辑按钮
- [ ] 管理员可编辑任意心愿，且可改状态（内联下拉）
- [ ] 管理员视角卡片额外显示作者姓名 + 创建日期；普通用户视角**不显示**
- [ ] 管理员隐藏心愿 → 普通用户列表立即消失
- [ ] 管理员"已隐藏"视图可见该心愿，点恢复 → 普通用户列表重现
- [ ] 恢复后原有助力数/收藏数不变，且此前助力过的用户仍显示为已助力
- [ ] 对已隐藏心愿调用 toggle → 404 提示，不产生计数变化
- [ ] 作者改名后，心愿卡片上的作者名跟随更新（JOIN 策略验证）
- [ ] 移动端窄屏：卡片单列，工具栏可换行不溢出

**回归**

- [ ] 既有"点赞/点踩"功能（`/api/feedback`）未受影响
- [ ] `pytest backend/tests` 全绿
- [ ] 应用启动时 `init_db()` 在 PostgreSQL 上成功建 3 张新表，索引与唯一约束存在
- [ ] 既有库（已有数据）升级后启动无异常，旧表未被改动

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `func.greatest` 在 SQLite 上不可用 | 测试库递减用例失败 | **已规避**：统一改用 `case((col > 0, col - 1), else_=0)`，见 §5.2 |
| `with_for_update()` 在 SQLite 上是 no-op | 并发用例（8/9）在本地假绿 | 并发测试标记 `@pytest.mark.skipif(sqlite)`，在 PostgreSQL 上单独跑一次；或改用 `INSERT ... ON CONFLICT DO NOTHING` + `rowcount` 判定，不依赖行锁 |
| `uxQuery` 数组序列化格式与后端不符 | 过滤器全部失效 | **已核实**（F14）：生成逗号串。后端统一 `status: str \| None` + `split(",")`，见 §7.5 |
| 计数列与 action 行长期漂移 | 展示数字失真 | 提供一个管理员专用的对账脚本（`SELECT` 重算并 `UPDATE`），非本期范围但预留 |
| 心愿墙内容无人发布 → 空页面 | 上线即冷场 | 已明确**不做种子数据**（D27）；改由"反馈一键转心愿"（D23）作为初始内容来源 |
| `/api/feedback` 与 `/api/opinions` 命名混淆 | 后续维护误改 | 文档与代码注释双向标注；`UxDashboard` 文案已改为"点赞点踩" vs "用户反馈" |

---

## 11. 验收标准

1. §1.1 中 A1~A5、B1~B5 全部目标可演示通过
2. §9.3 手工验证清单全部勾选
3. §8 中 27 个后端测试用例全部通过
4. 无新增 lint / type 报错
5. 既有功能（对话、点赞点踩、后台轮次分析、模板库、知识库）无回归
