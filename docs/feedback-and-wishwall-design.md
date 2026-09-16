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
| A3 | 反馈落库，字段含：**分类（Bug / 功能特性）**、用户姓名、工号、反馈内容、反馈时间、解决状态（默认"未解决"） |
| A4 | 用户体验管理后台新增"用户反馈"栏目，**按分类分 Tab 呈现**，各自带统计卡片，表格展示完整字段 |
| A5 | 管理员可在 已解决 / 评估中 / 已规划 / 开发中 之间切换状态并实时持久化 |
| A6 | **功能特性类反馈引导创建心愿单**：提交成功后立即引导用户自助发布（预填内容），管理员后台亦可一键转化，双通道均回写 `linked_wish_id` |
| A7 | **Bug 类反馈支持粘贴截图**：弹窗内 Ctrl+V 直接粘贴剪贴板图片，最多 3 张，随反馈一并落库并在后台可查看 |

**功能 B：心愿墙**

| # | 目标 |
|---|------|
| B1 | 独立页面呈现心愿卡片：加粗名称、截断描述、类型标签、状态标识、助力数、收藏数、助力/收藏按钮 |
| B2 | 关键词搜索（命中名称或描述） |
| B3 | 多条件过滤：状态（评估中/已规划/开发中/已实现，多选）+ 操作（全部/我创建的/我收藏的/我助力的，单选），两组之间 AND |
| B4 | 排序：助力数、收藏数、创建时间 各支持升/降序 |
| B5 | 助力与收藏实时更新数量并持久化，toggle 语义（再点取消），计数不漂移 |

### 1.2 非目标

- ❌ **反馈同步到代码仓 Issue**（GitHub / GitLab / Jira）：本期**不实现，也不预留**抽象层、CLI 或表字段（决策 D37）。理由见 §1.3
- ❌ 类型（模型/上下文记忆/用户体验/其他）维度的过滤器（仅展示标签，不参与筛选）
- ❌ 心愿墙种子/演示数据
- ❌ 反馈或心愿的富文本、图片附件（**心愿**不做附件；反馈的截图附件见 A7）、@提及
- ❌ 站内消息/邮件通知（状态变更后不主动推送）
- ❌ 心愿评论区
- ❌ Alembic 迁移体系（沿用现有 `create_all` + `_ensure_column` 惯例）
- ❌ 前端路由库引入（继续沿用 `App.tsx` 的 `page` 联合类型）

### 1.3 关于 Issue 出口的说明（后续迭代）

需求方提出"后续需要支持通过 CLI 或 API 将用户反馈直接提交到代码仓 Issue"。经确认，**本期不预留任何接口、字段或抽象层**（D37），遵循"不为假想需求设计"的原则：目标 Issue 平台（GitHub / GitLab / Gitee / 内部系统）尚未确定，过早抽象出的 `IssueTrackerBackend` Protocol 极可能与真实平台的字段模型不匹配，届时仍需推翻重写。

本期设计**已天然为该能力留出了低成本的接入面**，未来接入时的改动范围可预估如下（仅供后续排期参考，非本期工作量）：

| 已有基础 | 未来接入时如何用 |
|----------|------------------|
| `opinion_feedback.category` 明确区分 Bug / 功能特性 | Bug 直接映射为 Issue，功能特性映射为心愿（不进 Issue） |
| `opinion_feedback.name` / `uid` 快照 | 可作为 Issue 正文的"报告人"段落，无需回查用户表 |
| `opinion_attachment` 表 + 图片重编码为定尺寸 PNG | Issue 附件上传的现成字节源，无需二次处理 |
| `status` 五态字典 | 可映射为 Issue label（如 `status:evaluating`） |
| `linked_wish_id` 幂等模式（`WHERE ... IS NULL` + `rowcount`） | 未来 `issue_key` 回填可完全复用同一套幂等写法 |

未来接入时预计仅需：① `opinion_feedback` 用 `_ensure_column` 加 `issue_key` / `issue_url` 两列；② 新增一个 adapter 模块；③ 新增 `POST /api/admin/opinions/{id}/to-issue` 端点与 CLI 入口。**不需要改动本期任何已交付的表结构语义或端点契约。**

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
| F16 | **全站未挂载 `StaticFiles`**，无 `/static` 目录；二进制文件一律经鉴权端点以 `FileResponse` / `Response` 返回 | `backend/app/main.py`（无 `mount`） |
| F17 | 已有分块读取上限的上传助手 `_read_capped(file, cap) -> bytes`：按 1MB 分块累积，超限抛 **413**，避免超大 body 被完整缓冲 | `backend/app/routers/library.py:105-114` |
| F18 | 已有图片上传端点范式：`UploadFile = File(...)` + `_read_capped` + `asyncio.to_thread(...)` 落盘 + 返回 URL 字符串 | `routers/library.py:354-373` |
| F19 | **Pillow 11.0.0 已在依赖中**；既有 `set_thumb()` 用 `Image.open` → `convert("RGB")` → `thumbnail((640, 360), LANCZOS)` → `save(format="PNG", optimize=True)` 重编码，"so size/format stay bounded" | `backend/requirements.txt:18`、`services/pptx_library.py:1118-1130` |
| F20 | **`<img>` 无法携带 Authorization 头**（token 存 localStorage）；既有解法是 `fetch` 取 `Blob` → `URL.createObjectURL`，404 吞成 `null` 退化为占位，组件卸载时 `revokeObjectURL` | `frontend/src/api.ts:1879-1892`、`components/PptxLibrary.tsx:128-148` |
| F21 | 存储目录配置惯例为 `settings.xxx_dir`（如 `pptx_library_dir = "/library/pptx"`、`backup_dir = "/app/data/backups"`），均为容器内绝对路径、以卷挂载持久化 | `backend/app/config.py:94-143` |
| F22 | Form 字段无法承载数组，既有惯例是接受逗号/换行分隔文本后 `re.split` | `routers/library.py:117-121` |
| F23 | `apiCall<T>` 是**模块私有**函数（未 `export`）；`body instanceof FormData` 时自动**不设** `Content-Type`（交给浏览器带 boundary）；非 2xx 一律 `throw new Error(detail \|\| message \|\| HTTP xxx)` —— **状态码在抛错时丢失**，调用方只能靠 message 文案猜；`204` 返回 `undefined` | `frontend/src/api.ts:634-666` |

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

### 3.1 第二轮增补决策（分类与截图）

| ID | 议题 | 裁定 |
|----|------|------|
| D28 | 反馈是否分类 | **必填二选一**：`bug` / `feature`。无"其他"档——强制用户表态，避免分类退化为垃圾桶；分类错了管理员可在后台改（见 D34） |
| D29 | 分类何时确定 | **提交前由用户选**（弹窗顶部 antd `Segmented`，默认选中 `bug`）。不做服务端自动分类（关键词/LLM 判别），误判成本高于收益 |
| D30 | 功能特性如何"引导创建心愿单" | **双通道**：① 用户提交 `feature` 成功后，**弹窗不立即关闭**，改为渲染"转心愿"引导态（预填标题/描述/类型），用户可「去发布」或「稍后再说」；② 管理员后台保留 D23 的「一键转心愿」。两条路径都写 `linked_wish_id`，先到先得，后到者 409 |
| D31 | Bug 类如何呈现 | **仅落库 + 后台看板呈现**，不做用户侧后续引导（不提示"我们会尽快修复"之类承诺性文案），toast 统一为「已记录，感谢反馈」 |
| D32 | Bug 是否需要结构化字段 | **不拆字段**，仍存单一 `content`。选中 `bug` 时输入框 placeholder 切换为结构化模板提示（复现步骤 / 期望结果 / 实际结果 / 发生时间），仅引导不强制 |
| D33 | 截图附件 | ✅ **支持粘贴截图**。上限 **3 张 / 条反馈**，单张原始 ≤ **5MB**，仅 png/jpeg/webp/gif；服务端用 **Pillow 重编码为 PNG 并限制最长边 1600px**（F19 范式），顺带完成 magic-byte 校验 |
| D34 | 后台看板形态 | **「用户反馈」子标签内顶部分类 Tab**（Bug / 功能特性），各自带统计卡片行；表格含分类列但**默认按当前 Tab 过滤**；管理员可改任意反馈的 `category`（下拉），改后该行从当前 Tab 消失 |
| D35 | 附件可见性 | **仅管理员**可读取附件字节（`require_admin`）。提交者本人**不可回看**——弹窗提交即关闭，无"我的反馈"列表，避免额外端点与隐私面 |
| D36 | 附件存储位置 | **文件系统**（`settings.opinion_attachment_dir`，默认 `/app/data/opinion-attachments`，F21 惯例），DB 只存元数据与相对文件名。**不存 BLOB 进库**：PostgreSQL 大对象会拖慢 `create_all` 环境的备份与主从同步 |
| D37 | Issue 出口预留深度 | **先不预留**——不加 `issue_key`/`issue_url` 列、不写 `IssueTrackerBackend` Protocol、不做 CLI、不加 `to-issue` 端点。仅在 §1.3 记录未来接入面。理由：目标平台未定，过早抽象大概率返工 |

---

## 4. 领域模型与数据库设计

### 4.1 枚举字典

```python
# backend/app/models.py 追加
class FeedbackCategory(str, Enum):
    bug = "bug"          # Bug（默认）
    feature = "feature"  # 功能特性 → 引导转心愿

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
| `category` | String(16) | NOT NULL, default `"bug"` | **新增（D28）**：`bug` / `feature` |
| `name` | String(64) | NOT NULL | 提交时刻的 `user.username` **快照** |
| `uid` | String(64) | NULL | 提交时刻的 `user.uid` **快照**，可为空 |
| `content` | Text | NOT NULL | 5 ~ 2000 字 |
| `status` | String(16) | NOT NULL, default `"open"` | 见 `OpinionStatus` |
| `linked_wish_id` | Integer | NULL | 转化后指向 `wishes.id`，**无外键**（心愿软删后仍可查） |
| `created_at` | DateTime | NOT NULL, default `utcnow` | 反馈时间 |

索引：
- `Index("ix_opinion_feedback_cat_status_created", "category", "status", "created_at")` —— 服务后台"分类 Tab + 状态筛选 + 时间倒序"主查询（D34）
- `Index("ix_opinion_feedback_created", "created_at")` —— 服务不带分类筛选的全量列表

#### `opinion_attachment`（新增，D33 / D36）

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `id` | Integer | PK | |
| `feedback_id` | Integer | FK → `opinion_feedback.id` **ON DELETE CASCADE**, NOT NULL, index | 归属反馈 |
| `filename` | String(64) | NOT NULL | **服务端生成的随机名**（`{uuid4().hex}.png`），非用户原始文件名 |
| `content_type` | String(32) | NOT NULL | 重编码后固定为 `image/png` |
| `width` | Integer | NOT NULL | 重编码后实际宽度，供前端预留占位尺寸防抖动 |
| `height` | Integer | NOT NULL | 同上 |
| `size_bytes` | Integer | NOT NULL | 落盘后字节数 |
| `created_at` | DateTime | NOT NULL | |

约束：`UniqueConstraint("feedback_id", "filename", name="uq_opinion_attachment_file")`

> **为何用独立表而非 `opinion_feedback.images` JSON 列**：需要按 `feedback_id` 级联删除、需要存宽高元数据供前端占位、且 SQLite 上 JSON 列的查询与索引能力有限。
>
> **列名注意**：属性名不可用 `bytes`（遮蔽 Python 内置类型），统一命名为 `size_bytes`。

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
        Index("ix_opinion_feedback_cat_status_created", "category", "status", "created_at"),
        Index("ix_opinion_feedback_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    category = Column(String(16), nullable=False, default=FeedbackCategory.bug.value)
    name = Column(String(64), nullable=False)
    uid = Column(String(64), nullable=True)
    content = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default=OpinionStatus.open.value)
    linked_wish_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OpinionAttachment(Base):
    __tablename__ = "opinion_attachment"
    __table_args__ = (
        UniqueConstraint("feedback_id", "filename", name="uq_opinion_attachment_file"),
    )

    id = Column(Integer, primary_key=True, index=True)
    feedback_id = Column(
        Integer, ForeignKey("opinion_feedback.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    filename = Column(String(64), nullable=False)      # 服务端随机名，非用户原名
    content_type = Column(String(32), nullable=False)  # 恒为 image/png
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    size_bytes = Column(Integer, nullable=False)
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

四张表均为**全新表**，无既有列变更 → **不需要 `_ensure_column`**。

`backend/app/database.py` 的 `init_db()` 中，在模型 import 列表追加：

```python
from .models import (  # noqa: F401  (ensure tables are registered)
    ...,
    MessageFeedback,
    OpinionFeedback,    # 新增
    OpinionAttachment,  # 新增
    Wish,               # 新增
    WishAction,         # 新增
)
```

`Base.metadata.create_all()` 会在 PostgreSQL 与 SQLite 上分别建表；索引与唯一约束随 DDL 一并创建。

### 4.5 附件存储目录配置

`backend/app/config.py` 追加（沿用 F21 惯例）：

```python
# 意见反馈截图附件落盘目录（容器内绝对路径，需随 data 卷持久化）
opinion_attachment_dir: str = "/app/data/opinion-attachments"
```

- 目录**不在启动时强制创建**，而是首次写入前 `Path(...).mkdir(parents=True, exist_ok=True)`（`asyncio.to_thread` 包裹），避免只读卷导致启动失败
- 该路径必须落在**已挂载的持久化卷**内（与 `backup_dir = "/app/data/backups"` 同卷），否则容器重建后 DB 有元数据但文件丢失
- `docker-compose.yml` **无需改动**：已核实第 54 行存在 `backend-data:/app/data` 卷挂载，新目录天然持久化

---

## 5. API 契约

### 5.1 意见反馈

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| `POST` | `/api/opinions` | `get_current_user` | 提交反馈（multipart，含分类与截图） |
| `GET` | `/api/admin/opinions` | `require_admin` | 分页 + 分类 Tab + 状态筛选 + 关键词搜索 |
| `GET` | `/api/admin/opinions/stats` | `require_admin` | 看板统计卡片数据（按分类 × 状态） |
| `PATCH` | `/api/admin/opinions/{id}` | `require_admin` | 修改状态 / 分类 |
| `GET` | `/api/admin/opinions/{id}/attachments` | `require_admin` | 某条反馈的截图元数据列表 |
| `GET` | `/api/admin/opinions/attachments/{aid}` | `require_admin` | 截图字节（`FileResponse`，D35） |
| `POST` | `/api/admin/opinions/{id}/to-wish` | `require_admin` | 管理员通道：一键转为心愿 |

> **路由顺序无坑，但有一个前提**：`/attachments/{aid}` 与 `/{id}/attachments` 前缀相似。因 `{id}` / `{aid}` 声明为 `int`，Starlette 会编译成 `[0-9]+` 正则，字面量 `attachments` 不匹配 → 正常落到下一条路由。**若把它们写成 `str`，先注册者会吞掉后者并返回 422**。`stats` 同理（路径深度与 `{id}` 不同，本就不冲突）。

#### `POST /api/opinions`

**Content-Type 统一为 `multipart/form-data`**（无论是否带图，避免 JSON / multipart 双分支）。理由：与既有上传端点范式一致（F18），且截图与反馈在**同一事务**内落库，不产生需要 GC 的临时孤儿文件。

Form 字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `category` | str | ✅ | `bug` \| `feature` |
| `content` | str | ✅ | 5 ~ 2000 字 |
| `images` | File[] | ❌ | 0 ~ 3 张，单张 ≤ 5MB，png/jpeg/webp/gif |

> Form 无法承载数组，但 `images` 是文件字段——FastAPI 以 `list[UploadFile] = File(default=[])` 接收**同名重复 part**，前端 `FormData.append("images", f)` 多次调用即可，不受 F22 的标量限制。

```python
@router.post("", status_code=201)
async def submit_opinion(
    category: str = Form(...),
    content: str = Form(...),
    images: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
):
    ...
```

响应 `201`：
```json
{
  "id": 42,
  "category": "bug",
  "status": "open",
  "created_at": "2026-09-15T10:12:33Z",
  "attachments": [
    { "id": 7, "width": 1600, "height": 900, "size_bytes": 184320, "content_type": "image/png" }
  ]
}
```

校验与错误（**顺序即代码顺序**）：

| # | 场景 | 状态码 | detail |
|---|------|--------|--------|
| 1 | `category` 不在枚举内 | 422 | `反馈分类不合法` |
| 2 | `content` strip 后长度 < 5 | 422 | `反馈内容至少 5 个字` |
| 3 | 长度 > 2000 | 422 | `反馈内容不能超过 2000 字` |
| 4 | `len(images) > 3` | 422 | `最多上传 3 张截图` |
| 5 | 60 秒内已提交 ≥ 5 次 | 429 | `提交过于频繁，请 {n} 秒后再试` |
| 6 | 单张原始字节 > 5MB | 413 | `图片过大（超过 5MB 上限）` |
| 7 | Pillow 无法解码（伪造扩展名 / 损坏） | 422 | `图片无法解析，请重新截图` |

限流键：`f"opinion:{user.id}"`，`wait = _opinion_limiter.hit(key, 5, 60)`；`wait > 0` → 429（F15：返回 `0.0` 表示放行）。

> **顺序要点**：1-4 的轻量校验**先于**限流（5），避免非法请求白耗配额；限流**先于**图片解码（6-7），避免未过配额就烧 CPU 做 Pillow 解码。

落库事务（单次 commit，保证反馈与附件原子性）：

```python
async with async_session() as db:
    async with db.begin():
        fb = OpinionFeedback(
            user_id=user.id,
            category=category,
            name=user.username or "",   # 快照，D1
            uid=user.uid,               # 快照
            content=content.strip(),
        )
        db.add(fb)
        await db.flush()                # 取得 fb.id 供附件外键使用

        for raw in images:
            data = await _read_capped(raw, MAX_OPINION_IMAGE_BYTES)   # F17，超限 413
            png, w, h = await asyncio.to_thread(_reencode_image, data)  # F19 范式
            filename = f"{uuid.uuid4().hex}.png"                      # 服务端随机名
            await asyncio.to_thread(_write_attachment, filename, png)
            db.add(OpinionAttachment(
                feedback_id=fb.id, filename=filename,
                content_type="image/png", width=w, height=h, size_bytes=len(png),
            ))
```

> **磁盘写入在事务内**：若 commit 失败会留下孤儿文件。可接受——附件目录由一个幂等的对账脚本清理（`SELECT` 全部 `filename` 与目录做差集删除），不引入两阶段提交。反之若先写文件后开事务，DB 失败时孤儿文件概率相同，且顺序更绕。

#### `GET /api/admin/opinions`

Query：`page`(默认 1)、`page_size`(默认 20，上限 100)、`category`(单值，`bug`/`feature`，缺省为全部)、`status`(逗号分隔串，如 `open,planned`)、`q`(关键词)

```python
async def list_opinions(
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,   # Tab 单选，缺省 = 全部
    status: str | None = None,     # "open,planned" → split(",")
    q: str | None = None,
) -> ...
```

> 多值参数采用逗号串而非 FastAPI 的 `List[str]` 重复键，理由见 §7.5（与既有 `uxQuery` 的序列化行为一致）。

响应：
```json
{
  "items": [
    {
      "id": 42,
      "category": "bug",
      "name": "张三",
      "uid": "10086",
      "content": "导出按钮点击后无响应",
      "status": "open",
      "linked_wish_id": null,
      "attachment_count": 2,
      "created_at": "2026-09-15T10:12:33Z"
    }
  ],
  "total": 137,
  "page": 1,
  "page_size": 20
}
```

- 排序固定 `created_at DESC, id DESC`
- `q` 命中 `content` **或** `name` **或** `uid`（`or_` 组合，均 `.ilike()` + 转义，见 §6.3）
- `attachment_count` 由一次 `GROUP BY feedback_id` 子查询批量取回后在 Python 侧回填，**禁止**逐行 COUNT（N+1）
- 统计数字**不在本端点返回**，独立走 `/stats`：列表每次翻页都重算聚合是浪费

#### `GET /api/admin/opinions/stats`

响应（供 D34 的分类 Tab 徽标与统计卡片）：
```json
{
  "bug":     { "total": 92, "by_status": { "open": 60, "resolved": 12, "evaluating": 10, "planned": 6, "developing": 4 }, "with_attachment": 41, "unresolved_7d": 18 },
  "feature": { "total": 45, "by_status": { "open": 20, "resolved": 5,  "evaluating": 12, "planned": 5, "developing": 3 }, "linked_to_wish": 25, "conversion_rate": 0.556 }
}
```

单条 `GROUP BY category, status` 聚合查询即可产出全部数字，`with_attachment` / `linked_to_wish` 各一条 `COUNT`。

- `unresolved_7d`：`category='bug' AND status='open' AND created_at < now()-7d` 的条数——看板上最需要被看见的"积压告警"
- `conversion_rate`：`linked_to_wish / total`，保留 3 位小数，`total == 0` 时返回 `0.0`

#### `PATCH /api/admin/opinions/{id}`

请求（字段均可选，至少一项）：
```json
{ "status": "planned", "category": "feature" }
```

响应 `200`：`{ "id": 42, "status": "planned", "category": "feature" }`

| 场景 | 状态码 |
|------|--------|
| 非法 `status` / `category` 枚举 | 422 |
| 反馈不存在 | 404 |
| body 为空对象 | 422 |

> 允许管理员改 `category`（D34）：用户误选分类是常态。改分类**不**自动改 `status`，也**不**触发转心愿引导——保持操作正交，避免管理员一次点击产生两个副作用。

#### `GET /api/admin/opinions/{id}/attachments`

响应：
```json
{ "feedback_id": 42, "items": [
  { "id": 7, "width": 1600, "height": 900, "size_bytes": 184320,
    "content_type": "image/png", "url": "/api/admin/opinions/attachments/7",
    "created_at": "2026-09-15T10:12:33Z" }
] }
```

反馈不存在 → 404；无附件 → `items: []`（非 404）。

#### `GET /api/admin/opinions/attachments/{aid}`

- 成功：`FileResponse(path, media_type="image/png")`（F16：全站无 StaticFiles，必须走端点）
- 元数据存在但文件丢失（卷未挂载 / 被误删）→ **404** `{"detail": "附件文件不存在"}`，并 `logger.warning` 记录 filename，便于运维发现卷配置问题
- 鉴权：`require_admin`（D35）。**前端不能用 `<img src>` 直连**（F20：无法带 Authorization），须 fetch 成 Blob 再 `createObjectURL`

#### `POST /api/admin/opinions/{id}/to-wish`（管理员通道）

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
2. `UPDATE opinion_feedback SET linked_wish_id = :wid, status = 'evaluating' WHERE id = :fid AND linked_wish_id IS NULL`
3. `rowcount == 0` → rollback + **409**（并发下另一通道已抢先转化）

错误：反馈不存在 → 404；`linked_wish_id` 已非空 → **409** `{"detail": "该反馈已转化为心愿"}`。

> **不限制 `category`**：管理员可以把误标为 `bug` 的反馈转成心愿（改分类与转化是两个独立操作，见上文 PATCH 说明）。

#### 用户自助通道（D30 ①）

用户侧**不新增端点**，复用 `POST /api/wishes`，仅增加一个可选字段：

```json
{ "title": "…", "description": "…", "type": "ux", "source_feedback_id": 42 }
```

服务端在同一事务内：
1. 校验 `source_feedback_id` 对应的反馈**存在**、**属于当前用户**、`category == "feature"`、`linked_wish_id IS NULL`
2. `INSERT wishes`（`author_id` = 当前用户）
3. `UPDATE opinion_feedback SET linked_wish_id = :wid, status = 'evaluating' WHERE id = :fid AND linked_wish_id IS NULL`
4. `rowcount == 0` → rollback + **409** `{"detail": "该反馈已转化为心愿"}`

| 校验失败场景 | 状态码 | detail |
|--------------|--------|--------|
| 反馈不存在 | 404 | `关联的反馈不存在` |
| 反馈不属于当前用户 | 403 | `无权关联该反馈` |
| `category != "feature"` | 422 | `仅功能特性类反馈可转为心愿` |
| 已被转化（含管理员抢先） | 409 | `该反馈已转化为心愿` |

> `source_feedback_id` 缺省时行为与普通发布心愿完全一致，不影响既有调用方。两条通道靠 `WHERE linked_wish_id IS NULL` + `rowcount` 收敛为**幂等竞争**，先到先得，无需分布式锁。

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
{
  "title": "支持导出对话为 Markdown",
  "description": "……",
  "type": "ux",
  "source_feedback_id": 42
}
```

- `title`：1 ~ 120 字（strip 后非空）
- `description`：0 ~ 5000 字
- `type`：枚举校验，缺省 `other`
- `source_feedback_id`：**可选**（D30 用户自助通道）。传入时在同一事务内回写该反馈的 `linked_wish_id` 并置 `status = "evaluating"`；校验规则与错误码见 §5.1「用户自助通道」。缺省时行为与普通发布完全一致

响应 `201`：返回完整心愿对象（`boost_count = favorite_count = 0`），并附 `"linked_feedback_id": 42 | null`
限流：`f"wish:create:{user.id}"`，**3 次 / 60 秒** → 429

> 从反馈引导态过来的发布**同样计入限流**。这是有意的：转心愿本质就是发心愿，不该因为多带一个关联字段就绕过配额。

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

### 6.7 截图附件的安全与体积控制

沿用 F19 的 `set_thumb()` 范式，但参数针对截图场景放宽（缩略图是 640×360，截图需保留可读性）：

```python
# backend/app/routers/opinion_feedback.py
MAX_OPINION_IMAGE_BYTES = 5 * 1024 * 1024   # 原始上传上限 5MB
MAX_OPINION_IMAGES = 3                      # 每条反馈上限
ATTACHMENT_MAX_EDGE = 1600                  # 重编码后最长边
DECODE_BOMB_EDGE = 20000                    # 解码前的尺寸闸门，防解码炸弹

def _reencode_image(data: bytes) -> tuple[bytes, int, int]:
    """解码 → 限尺寸 → 统一重编码为 PNG。

    重编码本身就是一道安全闸：伪造扩展名的非图片字节会在 Image.open 处失败，
    SVG/HTML 之类可被浏览器执行的格式也会被栅格化为无害位图。
    """
    try:
        img = Image.open(io.BytesIO(data))
        # 先读 header 里的尺寸再决定是否真正解码：一张几 KB 的 PNG 可以声明
        # 100000×100000，load() 时会吃掉数十 GB 内存。
        if max(img.size) > DECODE_BOMB_EDGE:
            raise HTTPException(422, "图片尺寸异常，请重新截图")
        img.load()                          # 强制真正解码，截断文件在此暴露
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, "图片无法解析，请重新截图") from exc

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    img.thumbnail((ATTACHMENT_MAX_EDGE, ATTACHMENT_MAX_EDGE), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), img.width, img.height
```

> `except HTTPException: raise` 必须在宽泛的 `except Exception` **之前**，否则尺寸闸门抛出的 422 会被吞掉并重新包装成"图片无法解析"，误导排查。

**安全要点（逐条对应威胁）：**

| 威胁 | 防线 |
|------|------|
| 上传伪装成 `.png` 的可执行/脚本文件 | Pillow `Image.open` + `load()` 解码失败即 422；成功解码后**重新编码为 PNG**，原始字节被完全丢弃，落盘的一定是 Pillow 自己生成的位图 |
| 文件名路径穿越（`../../etc/passwd`） | **完全不使用用户提供的文件名**，落盘名一律 `f"{uuid.uuid4().hex}.png"`（服务端生成） |
| 解码炸弹（超小文件解压出巨型位图） | `_read_capped` 先限原始字节 5MB（F17）；`Image.open` 后**先检查 `img.size`**，超过 `20000 × 20000` 直接 422，再 `load()` 解码 |
| 磁盘写满 | 5MB × 3 张 × 限流 5 次/60s ⇒ 单用户每分钟最多 75MB；重编码后实际远小于此。生产环境需监控 `opinion_attachment_dir` 所在卷使用率 |
| 越权读取他人截图 | 附件读取端点仅 `require_admin`（D35），无用户侧读取路径 |
| GIF 动图多帧 | `Image.open` 只取第 0 帧，重编码为静态 PNG。符合"截图"语义，无需支持动画 |

**为何统一转 PNG 而非保留 JPEG/WebP**：截图多为大面积纯色 + 文字，PNG 无损压缩比 JPEG 更小且无 ringing 伪影；统一单一格式也免去 `content_type` 嗅探与前端 `<img>` 兼容分支。代价是照片类截图体积偏大——已被 `ATTACHMENT_MAX_EDGE = 1600` 与 `optimize=True` 约束在可接受范围。

**文件读写必须走 `asyncio.to_thread`**（F18 惯例）：Pillow 解码与磁盘 IO 都是阻塞调用，直接在事件循环里执行会卡住整个 worker。

### 6.8 双通道转心愿的幂等竞争

D30 的用户自助通道与 D23 的管理员通道可能对同一条反馈并发触发。两者共用同一把"锁"——**`linked_wish_id IS NULL` 的条件更新**：

```sql
UPDATE opinion_feedback
   SET linked_wish_id = :wid, status = 'evaluating'
 WHERE id = :fid AND linked_wish_id IS NULL
```

- `rowcount == 1` → 本通道胜出，事务提交，心愿生效
- `rowcount == 0` → 已被对方抢先，**rollback**（连带撤销刚 INSERT 的心愿，不留孤儿）+ 返回 409

这个写法同时覆盖三种情形：① 用户双开标签页重复提交；② 用户与管理员同时操作；③ 网络重放。无需 `SELECT ... FOR UPDATE`，也无需应用层分布式锁——单条条件 UPDATE 的原子性由数据库行级写锁保证，PostgreSQL 与 SQLite 行为一致。

> **前端配套**：收到 409 时应刷新该行数据（管理员看板）或提示"该反馈已转化为心愿"并关闭引导态（用户弹窗），而不是当作普通失败重试。

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
├── OpinionFeedbackModal.tsx（新组件）                                ← A1/A3/A6/A7
│   ├── 态 1「填写」：分类 Segmented(Bug/功能特性) + 内容 + 粘贴截图
│   └── 态 2「引导」（仅 feature）：提交成功后不关闭，改为转心愿引导     ← D30①
│           ├── [去发布心愿]（预填标题/描述/类型）→ 调 POST /api/wishes
│           └── [稍后再说] → 关闭
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
        └── 用户反馈栏目                                              ← D34
            ├── 分类 Tab：[🐞 Bug (92)] [✨ 功能特性 (45)]
            ├── 统计卡片行（随 Tab 切换内容）
            ├── 搜索 + 状态筛选
            └── 表格：时间/姓名/工号/内容/截图/分类/状态/操作
                       └─ 操作：[转为心愿] | [查看心愿] | [查看截图]
```

### 7.2 `OpinionFeedbackModal.tsx`（新组件）

组件内部是一个**两态状态机**（`phase: "form" | "guide"`），不是两个弹窗——避免关闭再打开造成的焦点丢失与动画割裂。

#### 态 1：填写

| 元素 | 规格 |
|------|------|
| 标题 | `意见反馈`；副标题一行灰字：`我们会认真阅读每一条反馈` |
| 分类选择 | antd `Segmented`，两项 `🐞 遇到问题(Bug)` / `✨ 功能建议`，**默认选中 Bug**（D29）。必填但恒有值，故无校验态 |
| 内容输入 | antd `Input.TextArea`，`rows={6}`、`showCount`、`maxLength={2000}` |
| placeholder | **随分类切换**（D32）：<br>Bug → `请尽量写清：\n1. 复现步骤\n2. 期望结果\n3. 实际结果\n4. 发生时间`<br>功能特性 → `希望增加什么能力？在什么场景下会用到？` |
| 输入框下方提示 | 仅 Bug 分类显示一行灰字：`可 Ctrl+V 直接粘贴截图（最多 3 张）`；功能特性分类**不显示**（见下方说明） |
| 截图区 | 缩略图行 + `[+ 添加]` 按钮，每张带 ✕ 删除角标 |
| 底部按钮 | `取消`（关闭并清空）/ `提交`（`loading` 态，禁用防重复提交） |

> **截图能力对两种分类都开放**，但引导文案只在 Bug 下显示。理由：功能建议附竞品截图/手绘草图同样有价值，能力上不做限制；而"粘贴截图"的提示语在 Bug 场景才最贴合用户心智，功能建议下多一行提示反而增加认知负担。

**粘贴截图实现：**

```tsx
const handlePaste = (e: React.ClipboardEvent) => {
  const items = e.clipboardData?.items;
  if (!items) return;
  const files: File[] = [];
  for (const it of items) {
    if (it.kind === "file" && it.type.startsWith("image/")) {
      const f = it.getAsFile();
      if (f) files.push(f);
    }
  }
  if (!files.length) return;   // 纯文本粘贴不拦截，走默认行为插入文字
  e.preventDefault();          // 阻止图片被当作富文本插入 textarea
  addImages(files);
};
// <Input.TextArea onPaste={handlePaste} />
```

`addImages` 的客户端前置校验（服务端仍会复校，见 §5.1）：

| 检查 | 处理 |
|------|------|
| 已有 + 新增 > 3 张 | 截断到 3 张，`message.warning("最多 3 张截图，已忽略多余的")` |
| 单张 > 5MB | 拒绝该张，`message.error("图片超过 5MB，请压缩后重试")` |
| 类型不在 png/jpeg/webp/gif | 拒绝该张，`message.error("仅支持 png / jpg / webp / gif")` |

粘贴成功的图片先用 `URL.createObjectURL` 本地预览（**立即显示，不等上传**），组件卸载或图片被移除时 `revokeObjectURL`（F20 惯例）。真正的字节上传发生在点「提交」时，与文本一并 multipart 送出——不做"先传图拿 token"的两段式，避免产生需要 GC 的临时文件。

#### 提交结果分流（A2 / A6 / D30 / D31）

```
提交成功
  ├─ category === "bug"
  │    → message.success("已记录，感谢反馈")        ← D31，不做承诺性文案
  │    → phase 保持，onClose() 立即关闭              ← D13
  │
  └─ category === "feature"
       → message.success("感谢你的建议！")
       → phase = "guide"，弹窗**不关闭**，内容替换为引导态
```

#### 态 2：转心愿引导（仅 feature）

| 元素 | 规格 |
|------|------|
| 标题 | `要不要把它发布到心愿墙？` |
| 说明文案 | 两行灰字：`发布后其他用户可以看到并助力，我们会按助力数评估优先级。` |
| 预填标题 | 取反馈内容**首行**或前 30 字（`content.split("\n")[0].slice(0, 30)`），可编辑 |
| 预填描述 | 反馈内容全文，可编辑 |
| 预填类型 | 默认 `other`，下拉可改（4 项） |
| 底部按钮 | `[稍后再说]`（默认按钮，关闭）/ `[去发布]`（primary，`loading`） |

`[去发布]` → `POST /api/wishes` 带 `source_feedback_id`：

- `201` → `message.success("已发布到心愿墙")` + 关闭弹窗 + 询问是否跳转（`Modal.confirm` → `onNavigate("wishes")`）
- `409` → `message.warning("该反馈已转化为心愿")` + 关闭引导态（§6.8：可能是管理员抢先转化）
- `429` → `message.warning(detail)`，引导态**保持**，用户可稍后再点
- 其他失败 → `message.error(detail)`，引导态保持，预填内容不丢

> 409 / 429 / 422 的区分依赖 `apiCall` 抛出的 Error 上附带的 `status`（§7.5 适配 1）。既有 `apiCall` 只 `throw new Error(detail)`，状态码丢失，靠文案匹配无法可靠分流——该适配是本功能的**前置依赖**，须在 F1 步完成。

`[稍后再说]` → 直接关闭，反馈已落库不受影响；管理员后续仍可从后台转化（D30②）。

#### 通用行为

- **不展示姓名/工号输入框**（D1 + D25：服务端从登录态取）
- 客户端预校验：内容 strip 后 < 5 字 → 提交按钮 `disabled`，下方红字提示"请至少输入 5 个字"
- 提交失败（429 / 422 / 网络错误）→ 弹窗**保持在态 1**，内容与截图均不清空
- 态 1 关闭时若已有内容或截图 → `Modal.confirm` 二次确认，防误关丢失；态 2 关闭无需确认（反馈已落库）
- 重新打开弹窗 → 全部字段重置为初始态（分类回到 Bug，截图清空）

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

该弹窗需支持**外部预填**并透传 `source_feedback_id`，以便 §7.2 的引导态（用户自助通道）复用同一组件，而不是写第二份表单。

**从后台「查看心愿」跳入：** `App.tsx` 透传目标心愿 id，`WishWall` 首次加载后清空过滤条件、按 `created_desc` 定位到该心愿所在页并短暂高亮（`scrollIntoView` + 2 秒描边动画）。找不到（已被隐藏/删除）→ `message.warning("该心愿已不可见")`。

### 7.4 `UxDashboard.tsx` 的 `opinions` 子标签

沿用手写 `adminStyles`（D11），**不引入 antd**——后台既有子标签全部是原生 `<table>` / `<select>` / `<input>`，混用会造成两套视觉语言。

#### 布局（自上而下）

```
[分类 Tab 行]        Bug(92) | 功能特性(45) | 全部(137)
[统计卡片行]         按当前 Tab 渲染 4 张卡片
[筛选行]             搜索框 + 状态多选 + 刷新按钮
[表格]               8 列
[分页]               PAGE = 20
```

#### ① 分类 Tab（D34）

三个按钮式 Tab：`Bug` / `功能特性` / `全部`，默认 **Bug**（积压最需要被先看到）。徽标数字取自 `/stats` 的 `bug.total` / `feature.total`（两者相加即"全部"，**不再单独请求**）。

切换 Tab → `category` 参数变化 + `page` 重置为 1 + 重新拉列表；**统计卡片同步切换**（见下）。Tab 状态存在组件 `useState`，不做 URL 持久化（与既有子标签一致）。

#### ② 统计卡片行（D34）

卡片随 Tab 切换而切换内容，避免"一张卡塞两个分类的数字"造成误读：

| 当前 Tab | 4 张卡片 |
|----------|----------|
| Bug | `总数` / `未解决` (`by_status.open`) / **`超 7 天未解决`**（`unresolved_7d`，> 0 时卡片描红）/ `带截图` (`with_attachment`) |
| 功能特性 | `总数` / `待评估` (`by_status.open`) / `已转心愿` (`linked_to_wish`) / `转化率` (`conversion_rate` × 100，保留 1 位 + `%`) |
| 全部 | `总数` / `未解决`（两分类 `open` 相加）/ `超 7 天未解决` / `已转心愿` |

卡片下方一行小字铺开状态明细徽标（`未解决 / 已解决 / 评估中 / 已规划 / 开发中` 五态计数，取自 `by_status`），点击某态即等价于把该态加入状态筛选。

统计与列表**分离请求**（§5.1）：翻页只重拉列表；状态/分类变更或转心愿成功后，两者一起重拉。

#### ③ 表格列（8 列）

| 列 | 内容 |
|----|------|
| `反馈时间` | `created_at` 本地化，`yyyy-MM-dd HH:mm` |
| `分类` | `<select>` 2 项（Bug / 功能特性），`onChange` 立即 PATCH（D34：管理员可纠正误选）。当前 Tab 为单一分类时该列仍可见但下拉可改——改完该行从当前视图消失，属预期 |
| `姓名` | 提交时刻快照 |
| `工号` | 快照 |
| `反馈内容` | clamp 2 行 + 点击就地展开/收起（`opinionContentCell`） |
| `截图` | `attachment_count === 0` → 灰字 `—`；否则 `查看截图(2)` 链接，点击走附件预览（见下） |
| `解决状态` | `<select>` 5 项，`onChange` 立即 PATCH |
| `操作` | 按分类与 `linked_wish_id` 分流（见下） |

`操作` 列分流规则：

| 条件 | 按钮 |
|------|------|
| `category === "feature"` 且 `linked_wish_id == null` | `转为心愿` |
| `linked_wish_id != null` | `查看心愿` |
| `category === "bug"` 且未转化 | 灰字 `—`（D31：bug 只记录与跟进，不引导转心愿；管理员如确需转化，先把分类改为"功能特性"） |

两个 `<select>` 变更失败 → 回滚为原值 + 提示（乐观更新回滚惯例，同 §7.3）。

#### ④ 截图预览

`<img src>` 无法带 `Authorization`（F20），因此：

```ts
// 点「查看截图」→ 先取元数据，再逐张取字节
const meta = await listOpinionAttachments(feedbackId);
const urls: string[] = [];
for (const it of meta.items) {
  const blob = await fetchOpinionAttachmentBlob(it.id);   // apiCall → res.blob()
  urls.push(URL.createObjectURL(blob));
}
setPreview({ urls, meta });   // 交给预览浮层
```

- 预览浮层：`position: fixed` 全屏遮罩 + 图片纵向排列（最大宽 90vw），点击遮罩或 `Esc` 关闭
- 关闭时对全部 `urls` 执行 `URL.revokeObjectURL`（F20 生命周期惯例），组件卸载时同样清理
- 单张取字节失败（404 = 文件丢失）→ 该位置显示占位灰块 + `附件文件不存在`，**不阻断**其余图片
- 元数据里的 `width/height/size_bytes` 渲染为图注（`1600×900 · 180 KB`），便于管理员判断是否为用户截的完整界面
- 不做缩略图：附件已在服务端重编码到长边 ≤1600（D33），单张 ≤ 数百 KB，直出即可

#### ⑤ 转为心愿（管理员通道）

弹出预填弹窗（标题取 `content` 首行前 30 字，描述取全文，类型默认 `other`）→ 提交 `to-wish`：

- `201` → 该行 `操作` 变 `查看心愿`、`解决状态` 变 `评估中`，统计卡片重拉
- `409` → 提示「该反馈已转化为心愿」（可能是用户自助抢先，§6.8）+ 刷新该行，**不重复建心愿**
- `查看心愿` → `onNavigate("wishes")` 并携带目标 id 供高亮定位

### 7.5 `api.ts` 增量

```ts
export type OpinionStatus = "open" | "resolved" | "evaluating" | "planned" | "developing";
export type FeedbackCategory = "bug" | "feature";
export type WishStatus = "evaluating" | "planned" | "developing" | "done";
export type WishType = "model" | "memory" | "ux" | "other";
export type WishScope = "all" | "mine" | "favorited" | "boosted";
export type WishSort = "boost_desc" | "boost_asc" | "favorite_desc"
                     | "favorite_asc" | "created_desc" | "created_asc";

export interface OpinionItem { id: number; category: FeedbackCategory; name: string;
  uid: string | null; content: string; status: OpinionStatus;
  linked_wish_id: number | null; attachment_count: number; created_at: string }
export interface OpinionListResp { items: OpinionItem[]; total: number;
  page: number; page_size: number }

/** GET /api/admin/opinions/stats（D34 看板卡片） */
export interface OpinionCategoryStats {
  total: number; by_status: Record<OpinionStatus, number>;
  with_attachment?: number; unresolved_7d?: number;      // bug
  linked_to_wish?: number; conversion_rate?: number;     // feature
}
export type OpinionStatsResp = Record<FeedbackCategory, OpinionCategoryStats>;

export interface OpinionAttachment { id: number; width: number; height: number;
  size_bytes: number; content_type: string; url: string; created_at: string }
export interface OpinionAttachmentListResp { feedback_id: number; items: OpinionAttachment[] }

export interface WishItem { id: number; title: string; description: string;
  type: WishType; status: WishStatus; boost_count: number; favorite_count: number;
  created_at: string; my_boosted: boolean; my_favorited: boolean; is_mine: boolean;
  author_name?: string | null; deleted_at?: string | null }
export interface WishListResp { items: WishItem[]; total: number; page: number; page_size: number }
export interface WishStats { total: number; by_status: Record<WishStatus, number>;
  mine: number; my_boosted: number; my_favorited: number }

/** 意见反馈（multipart，D33）：images 传 File[]，为空则不 append 该 part。
 *  返回体里的 attachments 只是回执（D35：提交者本人不可回看，前端仅用于
 *  显示「已上传 N 张」），故其形状比管理员端点的 OpinionAttachment 窄。 */
export interface OpinionCreateResp { id: number; category: FeedbackCategory;
  status: OpinionStatus; created_at: string;
  attachments: { id: number; width: number; height: number;
                 size_bytes: number; content_type: string }[] }

export async function submitOpinion(body: {
  category: FeedbackCategory; content: string; images?: File[];
}): Promise<OpinionCreateResp> {
  const fd = new FormData();
  fd.append("category", body.category);
  fd.append("content", body.content);
  for (const f of body.images ?? []) fd.append("images", f);   // 同名重复 part（F22）
  return apiCall<OpinionCreateResp>("/api/opinions", { method: "POST", body: fd });
}

export async function listOpinions(params: { page?: number; page_size?: number;
  category?: FeedbackCategory; status?: OpinionStatus[]; q?: string }) { /* GET /api/admin/opinions */ }
export async function getOpinionStats() { /* GET /api/admin/opinions/stats */ }
export async function patchOpinion(id: number,
  body: Partial<{ status: OpinionStatus; category: FeedbackCategory }>) { /* PATCH */ }
export async function listOpinionAttachments(id: number) { /* GET /{id}/attachments */ }

/** F20：<img> 无法带 Authorization → 取 Blob 再 createObjectURL，实现见下方「三处适配」 */
export async function fetchOpinionAttachmentBlob(aid: number): Promise<Blob>;

export async function opinionToWish(id: number,
  body: { title: string; description: string; type: WishType }) { /* POST to-wish */ }

export async function listWishes(params: { q?: string; status?: WishStatus[]; scope?: WishScope;
  sort?: WishSort; page?: number; page_size?: number; include_hidden?: boolean }) { /* GET */ }
/** source_feedback_id：用户自助转心愿通道（D30①），缺省时等同普通发布 */
export async function createWish(body: { title: string; description: string;
  type: WishType; source_feedback_id?: number }) { /* POST /api/wishes */ }
export async function patchWish(id: number, body: Partial<{ title: string; description: string;
  type: WishType; status: WishStatus }>) { /* PATCH */ }
export async function deleteWish(id: number) { /* DELETE */ }
export async function restoreWish(id: number) { /* POST restore */ }
export async function toggleWishAction(id: number, action: "boost" | "favorite") { /* POST actions */ }
export async function getWishStats() { /* GET stats */ }
```

#### 与既有 `apiCall` 的三处适配（F23）

`apiCall` 是模块私有函数，因此**所有新请求函数必须写在 `api.ts` 内部**（与既有 `api` 对象风格一致），不外泄该助手。它需要三处调整：

| # | 问题 | 处理 |
|---|------|------|
| 1 | **抛错丢状态码**：`throw new Error(detail)` 后调用方无法区分 `409`（已转化）/ `429`（限流）/ `422`（校验），而 §7.2 与 §7.4 的分流恰好依赖这个区分。靠 message 文案匹配是脆的（改一个字就静默失效） | 抛错前给 Error 附状态码：`const e = new Error(msg) as Error & { status?: number }; e.status = resp.status; throw e;`。**向后兼容**——既有 `catch (e) { message.error(e.message) }` 全部不受影响。前端判定统一写 `(e as {status?: number}).status === 409` |
| 2 | FormData 提交 | **已支持**，无需改动：`body instanceof FormData` 时不设 `Content-Type`，浏览器自动带 boundary |
| 3 | 取 Blob 而非 JSON（附件字节） | 加可选参数 `raw?: boolean`：为真时跳过 JSON 解析直接返回 `Response`，仍复用统一的 401 会话过期拦截。既有调用点不传该参数 → 行为不变 |

对应到附件读取：

```ts
/** F20 惯例：<img> 无法带 Authorization，须取 Blob 再 createObjectURL */
export async function fetchOpinionAttachmentBlob(aid: number): Promise<Blob> {
  const res = await apiCall<Response>(`/api/admin/opinions/attachments/${aid}`, { raw: true });
  return res.blob();
}
```

404（文件丢失）会经适配 1 抛出带 `status: 404` 的 Error，调用方 catch 后渲染占位灰块（§7.4④）。

> 若不接受改 `apiCall`，退路是照抄 `fetchLibraryThumb` 再写一份"手写 fetch + 拼 Authorization"的逻辑。**不推荐**：那会把鉴权与 401 拦截复制成第二份，且仍然解决不了适配 1 的状态码丢失问题。

**数组参数序列化（已核实 F14）**：`uxQuery` 用 `usp.set(k, String(v))`，数组 `["a","b"]` 会被序列化为 `status=a%2Cb`，即**逗号串**而非重复键。因此：

- 前端：数组参数直接交给 `uxQuery`，无需改造；但 `uxQuery` 当前是 `api.ts` 的**模块私有函数**，需 `export` 出来（或在 `api.ts` 内新增请求函数时直接调用，不导出——**推荐后者**，所有新请求函数都写在 `api.ts` 里，与既有 `api` 对象风格一致）
- 后端：多值参数一律声明为 `status: str | None`，内部 `[s for s in status.split(",") if s]`，并逐项校验枚举合法性（非法值 → 422，不静默忽略）
- `category` 是**单值**（Tab 单选），`uxQuery` 直接 `String(v)` 即可，无歧义

这样避免了引入 FastAPI `List[str]` 重复键与前端 `URLSearchParams.append` 的双向改造，改动面最小。

### 7.6 样式

- 心愿墙：新增 `frontend/src/wishStyles.ts`（或复用 antd token），卡片网格 `repeat(auto-fill, minmax(320px, 1fr))`，移动端单列
- Chat 侧边栏：`chatStyles` 追加 `quickActionsRow` / `quickActionButton` 两个键
- 后台：`adminStyles` 追加 6 个键
  | 键 | 用途 |
  |----|------|
  | `opinionContentCell` | 内容列 clamp 2 行 + 展开态切换 |
  | `statusSelect` | 状态/分类两个内联 `<select>` 共用 |
  | `opinionTabRow` / `opinionTabButton` | 分类 Tab 行与按钮（含 `data-active` 态） |
  | `statCardRow` / `statCard` | 统计卡片行（`repeat(auto-fit, minmax(160px, 1fr))`）；`statCard` 带 `data-alert="true"` 描红变体（`unresolved_7d > 0`） |
  | `attachmentOverlay` / `attachmentFigure` | 截图预览浮层（fixed 全屏遮罩）与单图容器 + 图注 |

---

## 8. 测试计划（D19：只写核心后端测试）

新增 `backend/tests/test_opinion_feedback_api.py` 与 `backend/tests/test_wishes_api.py`，复用 `conftest.py` 的 `app_client_factory(routers, user_id, username, role, uid)`。

### 8.1 `test_opinion_feedback_api.py`（27 例）

multipart 提交沿用既有惯例（`tests/test_skills_import.py` 的 `files=` 写法）：

```python
resp = await client.post("/api/opinions",
    data={"category": "bug", "content": "导出按钮点击后无响应"},
    files=[("images", ("a.png", png_bytes, "image/png"))])
```

**分类与基础校验（A1 / D28 / D32）**

| # | 用例 | 断言 |
|---|------|------|
| 1 | 提交 `category="bug"` | 201；库中 `category == "bug"`；`name`/`uid` 为登录态快照；`status == "open"`；`linked_wish_id IS NULL` |
| 2 | 提交 `category="feature"` | 201；`category == "feature"`；**不自动**建心愿（转化是显式动作，D30） |
| 3 | 缺 `category` / 值为 `"other"` | 422，且不落库 |
| 4 | 内容 4 字 | 422，且不落库 |
| 5 | 内容 2001 字 | 422 |
| 6 | 60 秒内第 6 次提交 | 429；第 5 次仍 201 |
| 7 | 未登录提交 | 401 |

**截图附件（D33 / §6.7）**

| # | 用例 | 断言 |
|---|------|------|
| 8 | 带 1 张合法 PNG | 201；`attachment_count == 1`；`opinion_attachment` 落 1 行；`filename` 为服务端 `uuid4().hex.png`，**不含**客户端原始名 |
| 9 | 3200×1800 大图 | 201；落盘图长边 == 1600；`content_type == "image/png"`；`width/height` 与落盘一致 |
| 10 | GIF/WebP 输入 | 201；落盘一律转 PNG（统一格式，§6.7） |
| 11 | 4 张图 | 422（超 `MAX_OPINION_IMAGES`），且**一张都不落盘**（校验先于写文件） |
| 12 | 单张 6MB | 413（`_read_capped` 超限，F17），不落库不落盘 |
| 13 | 伪造扩展名的非图片字节（`evil.png` = `b"MZ..."`） | 422「图片无法解析」；**目录中无残留文件** |
| 14 | 尺寸超 `DECODE_BOMB_EDGE` 的图（构造 30000×1 像素） | 422「图片尺寸异常」，且在 `load()` 之前返回（不解码全图像素） |
| 15 | 客户端文件名 `../../etc/passwd` | 201；落盘路径仍在附件目录内（服务端随机命名，路径穿越不成立） |
| 16 | 事务回滚时（如构造 commit 失败）附件已写盘 | 断言对账脚本可清除孤儿文件；**或**在测试中直接验证 `opinion_attachment` 无行 → 前端不可见，孤儿文件无害 |

**后台看板（D34 / D35）**

| # | 用例 | 断言 |
|---|------|------|
| 17 | 普通用户访问 `GET /api/admin/opinions` / `/stats` / `/{id}/attachments` / `/attachments/{aid}` | 四个端点均 403（D35：附件仅管理员可读） |
| 18 | 管理员列表：`category=bug` 过滤 + 分页 | 只返回 bug；`total` 正确；第 2 页与第 1 页无交集；固定 `created_at DESC, id DESC`；`attachment_count` 与库中实际行数一致（无 N+1 也不误计） |
| 18b | `status=open,planned`（逗号串）+ `q` 组合 | 状态组内 OR、与 `category`/`q` 之间 AND；`q` 命中 `content` / `name` / `uid` 三者任一均生效；搜 `"100%"` 不返回全表（通配符转义，§6.3）；`status` 含非法值 → 422 而非静默忽略 |
| 19 | `GET /stats` | `bug.by_status` 各态之和 == `bug.total`；`feature.conversion_rate == round(linked/total, 3)`；`total == 0` 时 `conversion_rate == 0.0`（不除零）；`unresolved_7d` 只计 7 天前且 `status='open'` 的 bug（用 `created_at` 回填构造） |
| 20 | `PATCH` 改 `status` 与 `category` | 200；重查生效；改 `category` **不**改 `status`、**不**建心愿（正交，§5.1）；空 body → 422；非法枚举 → 422；不存在 id → 404 |

**转心愿双通道（D30 / §6.8）**

| # | 用例 | 断言 |
|---|------|------|
| 21 | 管理员 `to-wish` | 201；心愿 `author_id` = 管理员；反馈 `linked_wish_id` 回填、`status == "evaluating"` |
| 22 | 管理员 `to-wish` 重复调用 | 409；**心愿表只多 1 行**（条件 UPDATE + `rowcount` 判定，未重复插入） |
| 23 | 用户自助 `POST /api/wishes` 带 `source_feedback_id` | 201；心愿 `author_id` = **提交者本人**；反馈回填 |
| 24 | 自助通道四类校验 | 反馈不存在 → 404；反馈不属于当前用户（非管理员）→ 403；`category == "bug"` → 422；`linked_wish_id` 已非空 → 409 |
| 25 | 双通道竞争：管理员 `to-wish` 与用户自助**并发** | 恰好一方 201、另一方 409；库中该反馈只关联 1 个心愿 |
| 26 | 附件端点：元数据存在但文件被删 | `GET /attachments/{aid}` → 404「附件文件不存在」，且有 `logger.warning`（运维可发现卷未挂载） |

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

附件相关用例（8.1 的 8~16、26）额外需要把落盘目录指到 pytest 临时目录，否则会往 `/app/data` 写：

```python
@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    d = tmp_path / "opinion-attachments"
    monkeypatch.setattr(opinion_feedback.settings, "opinion_attachment_dir", str(d))
    yield d
    # 用例结束自动随 tmp_path 回收，无需手工清理
```

> 图片字节用 Pillow 在测试内现场生成（`Image.new("RGB", (3200, 1800)).save(buf, "PNG")`），不入库二进制 fixture 文件——仓库里不该躺着测试图片。解码炸弹用例（#14）用 `Image.new` 造 30000×1 的极简 PNG，压缩后仅数百字节，不会拖慢测试。

并发用例（8.1 #25、8.2 #8/#9）在 SQLite 上会因 `with_for_update()` 退化为 no-op 而假绿，处理策略见 §10。

---

## 9. 实施拆分

### 9.1 后端（7 步，可独立提交）

| 步 | 内容 | 产出 |
|----|------|------|
| B1 | `config.py` 追加 `opinion_attachment_dir: str = "/app/data/opinion-attachments"`（F21 惯例；卷已由 `docker-compose.yml` 的 `backend-data:/app/data` 覆盖，**无需改 compose**） | 配置项 |
| B2 | `models.py` 追加 4 个枚举（含 `FeedbackCategory`）+ 4 个模型（含 `opinion_attachment`）；`database.py` 的 `init_db()` import 列表追加；既有库升级走 `_ensure_column` 补 `category` / `linked_wish_id`（F9） | 建表 + 平滑升级 |
| B3 | `schemas.py` 追加请求/响应模型（`OpinionItem`、`OpinionListResp`、`OpinionStatsResp`、`OpinionPatch`、`OpinionAttachmentItem`、`WishCreate`（含可选 `source_feedback_id`）、`WishUpdate`、`WishItem`、`WishListResp`、`WishActionReq`、`WishStats`、`ToWishReq`）。**注意**：提交反馈是 multipart，**不建** `OpinionCreate` Pydantic 模型，字段用 `Form(...)` 直接声明（F22） | 校验层 |
| B4 | `routers/opinion_feedback.py` 之**用户侧**：`POST /api/opinions`（multipart + 图片重编码，§6.7）；`_reencode_image` 与常量同文件私有 | 1 个端点 |
| B5 | `routers/opinion_feedback.py` 之**管理侧**：`GET /api/admin/opinions`、`/stats`、`/{id}/attachments`、`/attachments/{aid}`、`PATCH /{id}`、`POST /{id}/to-wish`（条件 UPDATE 幂等，§6.8） | 6 个端点 |
| B6 | `routers/wishes.py`（列表/发布含 `source_feedback_id` 自助通道/编辑/软删/恢复/toggle/stats） | 7 个端点 |
| B7 | `main.py` 注册 3 个 router（`opinion_feedback.router` + `.admin_router` + `wishes.router`，F8 惯例）；跑 `pytest backend/tests/test_opinion_feedback_api.py backend/tests/test_wishes_api.py` | 44 个测试用例通过 |

> B4 与 B5 拆开提交的理由：图片处理是本期唯一有安全面的代码，独立成一个 commit 便于日后 review / revert，不与 CRUD 混在一起。

### 9.2 前端（8 步）

| 步 | 内容 |
|----|------|
| F1 | `api.ts`：先做 `apiCall` 三处适配（§7.5），再追加类型与 13 个请求函数（多值参数交给既有 `uxQuery`，天然生成逗号串） |
| F2 | `OpinionFeedbackModal.tsx` 态 1（分类 Segmented + placeholder 切换 + `onPaste` 截图 + 本地 objectURL 预览） |
| F3 | `OpinionFeedbackModal.tsx` 态 2（转心愿引导 + 409/429 分流） |
| F4 | `Chat.tsx` 侧边栏 `quickActions` 行接入两个入口 + `chatStyles` 新样式键 |
| F5 | `App.tsx` `page` 联合类型追加 `"wishes"` + 渲染分支 + 导航透传（含"查看心愿"定位参数） |
| F6 | `WishWall.tsx`（统计条 / 工具栏 / 卡片网格 / 分页 / 乐观更新） |
| F7 | 心愿发布 & 编辑弹窗（发布弹窗需接受外部预填 + `source_feedback_id`，供态 2 复用） |
| F8 | `UxDashboard.tsx` 追加 `opinions` 子标签（分类 Tab / 统计卡片 / 8 列表格 / 截图预览浮层 / 转心愿弹窗），原"反馈"文案改"点赞点踩" |

### 9.3 手工验证清单

**意见反馈 · 填写与分类（A1 / D28 / D29 / D32）**

- [ ] 侧边栏 `quickActions` 行两个按钮视觉明显、不与既有 4 个按钮拥挤
- [ ] 弹窗打开时输入框自动 focus，分类默认停在 **Bug**
- [ ] 切到「功能特性」→ placeholder 文案随之切换为建议模板；切回 Bug → 恢复复现步骤模板（D32）
- [ ] 反馈内容 4 字时提交按钮 disabled，且下方有红字提示；恰好 5 字时可提交
- [ ] 输入超过 2000 字时 `showCount` 变红且无法继续输入
- [ ] 有内容时点关闭 → 弹出二次确认；确认后内容清空
- [ ] 弹窗内**不出现**工号/姓名输入框（D25）
- [ ] **提交 Bug** → toast「已记录，感谢反馈」+ 弹窗**立即关闭**，不出现引导态（D31）
- [ ] **提交功能特性** → toast「感谢你的建议！」+ 弹窗**不关闭**，切换到引导态（D30①）
- [ ] 提交后重新打开弹窗 → 分类回到 Bug、输入框为空、截图清空
- [ ] 60 秒内连续提交 6 次 → 第 6 次 toast 警告，弹窗保持打开且内容与截图均不丢
- [ ] 断网提交 → 错误提示，弹窗保持打开且内容与截图均不丢
- [ ] 提交失败（422/429/网络）后**不**进入引导态

**意见反馈 · 粘贴截图（D33）**

- [ ] 截图工具截屏后在输入框 `Ctrl+V` → 立即出现本地预览缩略图（不等上传）
- [ ] 连续粘贴 3 张 → 均可预览；第 4 张 → 提示「最多 3 张截图」且不加入
- [ ] 粘贴**纯文本**（如从网页复制一段字）→ 正常插入文本，**不**被截图逻辑拦截
- [ ] 预览图可单张移除；移除后计数释放，可再粘贴
- [ ] 提交成功后重新打开 → 预览区为空（objectURL 已 revoke，无内存泄漏）
- [ ] 粘贴 >5MB 的高清截图 → 提交时提示单张超限，弹窗保持、其余图不丢
- [ ] 粘贴一个改了扩展名的非图片文件 → 服务端 422「图片无法解析」，弹窗保持
- [ ] 上传后服务端落盘图长边 ≤1600、格式为 PNG（可在容器内 `ls` + 看图属性验证）

**后台看板（D34 / D35）**

- [ ] 「用户反馈」子标签顶部三个 Tab：Bug / 功能特性 / 全部，各自带正确徽标数（两分类之和 == 全部）
- [ ] 默认停在 **Bug** Tab
- [ ] 切 Tab → 表格与统计卡片**同时**切换，分页回到第 1 页
- [ ] Bug Tab 四张卡片：总数 / 未解决 / 超 7 天未解决 / 带截图，数字与库中一致
- [ ] 构造一条 7 天前且未解决的 bug → `超 7 天未解决` 卡片 +1 且描红
- [ ] 功能特性 Tab 四张卡片：总数 / 待评估 / 已转心愿 / 转化率（百分比 1 位小数）
- [ ] 无任何功能特性反馈时 → 转化率显示 `0.0%`，**不出现** NaN / Infinity
- [ ] 卡片下方状态明细徽标五态齐全，点击某态 → 等价于加入状态筛选
- [ ] 表格 8 列齐全；姓名为提交时刻快照（改用户名后旧反馈仍显示旧名）
- [ ] 后台原「反馈」标签文案已改为「点赞点踩」，数据未受影响
- [ ] 状态下拉切换 → 立即持久化 → 刷新页面后仍为新状态；可选回「未解决」
- [ ] **分类下拉**切换（bug → feature）→ 立即持久化；该行从 Bug Tab 消失、出现在功能特性 Tab；`解决状态`**未被**连带修改
- [ ] 下拉请求失败 → 值回滚为原值 + 错误提示
- [ ] 关键词搜索命中内容 / 姓名 / 工号 三种情况
- [ ] 状态筛选 + 分类 Tab + 搜索 三者组合生效（AND）
- [ ] 分页翻页无重复、无遗漏，`total` 与 Tab 徽标数一致
- [ ] 「截图」列：无附件显示 `—`；有附件显示 `查看截图(N)`
- [ ] 点击查看截图 → 全屏浮层显示 N 张图 + 图注（尺寸 · 体积）；点遮罩或 Esc 关闭
- [ ] 手工删掉卷里某个附件文件后点查看 → 该位置显示「附件文件不存在」占位，其余图正常
- [ ] **普通用户**直接请求附件端点 → 403（用 curl 带普通用户 token 验证 D35）
- [ ] Bug 行「操作」列为 `—`，无转心愿入口（D31）

**反馈转心愿 · 双通道（D30 / D23 / D24）**

- [ ] 用户自助：引导态预填（标题=内容首行前 30 字，描述=全文，类型=其他），三项均可编辑
- [ ] 引导态点「去发布」→ 201 → toast + 弹窗关闭 + 询问是否跳转心愿墙
- [ ] 引导态点「稍后再说」→ 弹窗关闭，反馈已落库，后台可见且 `linked_wish_id` 为空
- [ ] 引导态关闭后，管理员后台该 feature 行「转为心愿」按钮**仍可点**
- [ ] 管理员通道：点「转为心愿」→ 预填弹窗 → 提交成功 → 该行按钮变「查看心愿」、状态变「评估中」
- [ ] 点「查看心愿」→ 跳转心愿墙并定位/高亮该心愿
- [ ] 管理员转化的心愿作者为**管理员**；用户自助转化的心愿作者为**提交者本人**
- [ ] **竞争验证**：双开标签页（用户端 + 后台）对同一条 feature 反馈同时转化 → 一方成功、另一方提示「该反馈已转化为心愿」，心愿墙**只多一条**
- [ ] 自助通道对 bug 类反馈调接口 → 422；对他人反馈调接口 → 403
- [ ] 引导态遇 429（发布限流）→ 提示后**保持引导态**，预填内容不丢，可稍后重试

**心愿墙**

- [ ] 独立页面进入/返回导航正常，浏览器刷新后停留在心愿墙（若已实现 page 持久化）
- [ ] 卡片严格 7 项字段；名称加粗；类型标签四色正确；状态标识四态正确
- [ ] 描述超 3 行被 clamp，出现「展开」，点击就地展开为「收起」，不弹层
- [ ] 描述恰好 3 行内不出现「展开」
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
- [ ] 发布成功后列表首位出现新心愿（默认排序下按助力数，需切「最新创建」验证）
- [ ] 发布限流：60 秒内第 4 次发布被拒并提示
- [ ] 作者可编辑自己的心愿（标题/描述/类型），**看不到**状态下拉
- [ ] 非作者非管理员**看不到**编辑按钮
- [ ] 管理员可编辑任意心愿，且可改状态（内联下拉）
- [ ] 管理员视角卡片额外显示作者姓名 + 创建日期；普通用户视角**不显示**
- [ ] 管理员隐藏心愿 → 普通用户列表立即消失
- [ ] 管理员「已隐藏」视图可见该心愿，点恢复 → 普通用户列表重现
- [ ] 恢复后原有助力数/收藏数不变，且此前助力过的用户仍显示为已助力
- [ ] 对已隐藏心愿调用 toggle → 404 提示，不产生计数变化
- [ ] 作者改名后，心愿卡片上的作者名跟随更新（JOIN 策略验证）
- [ ] 移动端窄屏：卡片单列，工具栏可换行不溢出

**回归**

- [ ] 既有「点赞/点踩」功能（`/api/feedback`）未受影响
- [ ] 既有 `api.ts` 调用点未因 `apiCall` 三处适配而行为变化（重点验：任一 4xx 的错误提示文案照旧、401 仍自动登出）
- [ ] `pytest backend/tests` 全绿
- [ ] 应用启动时 `init_db()` 在 PostgreSQL 上成功建 4 张新表，索引与唯一约束存在
- [ ] 既有库（已有数据）升级后启动无异常，旧表未被改动，`_ensure_column` 补列成功且历史行 `category` 有默认值
- [ ] 容器重启后附件仍可读（`backend-data` 卷持久化验证）
- [ ] **无** Issue 相关字段/端点/CLI 被引入（D37 明确出范围，避免"顺手加"）

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `func.greatest` 在 SQLite 上不可用 | 测试库递减用例失败 | **已规避**：统一改用 `case((col > 0, col - 1), else_=0)`，见 §5.2 |
| `with_for_update()` 在 SQLite 上是 no-op | 并发用例（8.1 #25、8.2 #8/#9）在本地假绿 | **转心愿竞争不依赖行锁**：靠单条 `UPDATE ... WHERE linked_wish_id IS NULL` + `rowcount` 判定（§6.8），SQLite 与 PostgreSQL 语义一致，可直接测。助力并发用例标记 `@pytest.mark.skipif(sqlite)`，在 PostgreSQL 上单独跑一次 |
| `uxQuery` 数组序列化格式与后端不符 | 过滤器全部失效 | **已核实**（F14）：生成逗号串。后端统一 `status: str \| None` + `split(",")`，见 §7.5 |
| `apiCall` 改造（附状态码 / `raw`）波及既有调用点 | 全站请求错误处理回归 | 两项均为**追加式**改动：`status` 是 Error 上的新属性，`raw` 是默认 falsy 的可选参数，既有调用点行为不变。回归清单已列入 §9.3 |
| **上传图片是唯一的新增攻击面** | 恶意图导致内存爆掉 / 存储被塞满 / XSS | 五道防线（§6.7）：`_read_capped` 限 5MB（413）→ header 尺寸闸门防解码炸弹 → Pillow 重编码丢弃原始字节（顺带栅格化 SVG/HTML）→ 服务端 `uuid4` 命名防路径穿越 → `require_admin` + Blob 读取，附件永不进 `<img src>` 直连 |
| 附件目录无限增长 | 磁盘占满 | 单张重编码后长边 ≤1600 的 PNG 通常 100~400KB，3 张/条 + 5 次/60 秒限流构成天然上界。孤儿文件（事务回滚遗留）由**幂等对账脚本**清理：`SELECT filename` 与目录做差集删除。脚本非本期交付，但目录结构已为其留好 |
| 事务内写文件 → commit 失败留孤儿 | 磁盘泄漏少量文件 | 可接受（§5.1）：孤儿文件无 DB 行指向 → 前端永不可见 → 无隐私风险，只占空间，由对账脚本兜底。不为此引入两阶段提交 |
| 用户误选分类（把功能建议选成 Bug） | Bug 看板被污染，建议石沉大海 | ① 弹窗内分类文案带一句解释；② 管理员可在后台改 `category`（D34）；③ Bug Tab 的表格仍显示完整内容，管理员扫读时能识别出误分类 |
| 引导态被用户直接关掉 → feature 反馈未转心愿 | 转化率偏低 | 后台「功能特性」Tab 的 `已转心愿` / `转化率` 卡片就是为此设计的观测口（D34）；管理员可手动补转（D30②） |
| 计数列与 action 行长期漂移 | 展示数字失真 | 提供一个管理员专用的对账脚本（`SELECT` 重算并 `UPDATE`），非本期范围但预留 |
| 心愿墙内容无人发布 → 空页面 | 上线即冷场 | 已明确**不做种子数据**（D27）；改由"反馈一键转心愿"双通道（D30）作为初始内容来源 |
| `/api/feedback` 与 `/api/opinions` 命名混淆 | 后续维护误改 | 文档与代码注释双向标注；`UxDashboard` 文案已改为"点赞点踩" vs "用户反馈" |
| 后续 Issue 需求到来时发现无处挂载 | 返工 | **已知并接受**（D37）：§1.3 列出 5 项天然接入面与 3 步接入路径，届时改动局限在"加两列 + 一个 adapter + 一个端点"，不触碰本期契约 |

---

## 11. 验收标准

1. §1.1 中 **A1~A7**、**B1~B5** 全部目标可演示通过（A6 双通道、A7 粘贴截图为第二轮新增，需单独演示）
2. §9.3 手工验证清单全部勾选（含"粘贴截图"与"后台看板"两组新增项）
3. §8 中 **44 个**后端测试用例全部通过（`test_opinion_feedback_api.py` 27 + `test_wishes_api.py` 17）
4. 无新增 lint / type 报错
5. 既有功能（对话、点赞点踩、后台轮次分析、模板库、知识库）无回归；`apiCall` 三处适配未改变任何既有调用点的可观测行为
6. §1.2 非目标**未被实现**——尤其确认代码中不存在 Issue 相关字段、端点、CLI 或抽象层（D37）
