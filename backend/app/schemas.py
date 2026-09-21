"""Pydantic request/response schemas."""
from pydantic import BaseModel


class RegisterRequest(BaseModel):
    username: str
    password: str
    # Optional employee number (工号); auto-assigned when omitted.
    uid: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    role: str = "user"


class StartAgentRequest(BaseModel):
    workspace: str | None = None
    # True = block until the startup flow finishes (scripts / e2e tests).
    # False (default) = return immediately; the caller polls GET /agent/status
    # for the live phase (creating → starting → warming → running).
    wait: bool = False


class AgentStatusResponse(BaseModel):
    running: bool
    healthy: bool = False
    status: str = "absent"
    # Epoch seconds when the current background start flow began (only set
    # while a start is in flight) — lets the UI show an accurate total wait
    # even for a browser that attached mid-start (P1-4).
    phase_since: float | None = None
    container_name: str | None = None
    workspace: str | None = None
    message: str = ""
    error: str | None = None


class CreateSessionRequest(BaseModel):
    title: str = "New Session"


class PromptRequest(BaseModel):
    parts: list[dict]


class SessionInfo(BaseModel):
    id: str
    title: str = ""


# --- 意见反馈 & 心愿墙 -------------------------------------------------------
# 业务校验（枚举合法性、长度、归属权限）一律在 router 内做，以便返回中文
# detail；这里的模型只描述形状。时间字段统一为 isoformat() 字符串（全站惯例）。
# 注意：提交反馈是 multipart，字段用 Form(...) 直接声明，**不**建 OpinionCreate。


class OpinionAttachmentItem(BaseModel):
    id: int
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    content_type: str = "image/png"
    # 仅管理侧元数据列表返回（附件字节端点仅管理员可读，D35）。
    url: str | None = None
    created_at: str | None = None


class OpinionSubmitResp(BaseModel):
    """POST /api/opinions 的 201 响应。"""

    id: int
    category: str
    status: str
    created_at: str
    attachments: list[OpinionAttachmentItem] = []


class OpinionItem(BaseModel):
    """后台看板表格的一行（8 列，D34）。"""

    id: int
    category: str
    name: str
    uid: str | None = None
    content: str
    status: str
    linked_wish_id: int | None = None
    attachment_count: int = 0
    created_at: str


class OpinionListResp(BaseModel):
    items: list[OpinionItem]
    total: int
    page: int
    page_size: int


class OpinionCategoryStats(BaseModel):
    """单个分类的统计卡片数据。

    ``with_attachment`` / ``unresolved_7d`` 只对 bug 有意义，``linked_to_wish`` /
    ``conversion_rate`` 只对 feature 有意义；统一放在一个模型里，各 Tab 取所需。
    """

    total: int = 0
    by_status: dict[str, int] = {}
    with_attachment: int = 0
    unresolved_7d: int = 0
    linked_to_wish: int = 0
    conversion_rate: float = 0.0


class OpinionStatsResp(BaseModel):
    bug: OpinionCategoryStats
    feature: OpinionCategoryStats


class OpinionPatch(BaseModel):
    """管理员改状态 / 改分类（二者正交，改分类不联动状态）。"""

    status: str | None = None
    category: str | None = None


class OpinionPatchResp(BaseModel):
    id: int
    status: str
    category: str


class ToWishReq(BaseModel):
    """管理员通道：一键把反馈转为心愿（弹窗里字段可被改过）。"""

    title: str
    description: str = ""
    type: str = "other"


class ToWishResp(BaseModel):
    feedback_id: int
    wish_id: int
    status: str
    linked_wish_id: int


class WishCreate(BaseModel):
    """发布心愿。``source_feedback_id`` 非空即用户自助转心愿通道（D30 ①）。"""

    title: str
    description: str = ""
    type: str = "other"
    source_feedback_id: int | None = None


class WishUpdate(BaseModel):
    """编辑心愿。``status`` 仅管理员可改（权限矩阵见设计文档 §5.2）。"""

    title: str | None = None
    description: str | None = None
    type: str | None = None
    status: str | None = None


class WishItem(BaseModel):
    id: int
    title: str
    description: str
    type: str
    status: str
    boost_count: int = 0
    favorite_count: int = 0
    created_at: str
    updated_at: str | None = None
    # 以下四项由服务端按当前登录用户计算。
    my_boosted: bool = False
    my_favorited: bool = False
    is_mine: bool = False
    # 仅管理员请求时返回非空（普通用户前端也不渲染）。
    author_name: str | None = None
    # 仅管理员 include_hidden=true 时非空。
    deleted_at: str | None = None
    # 仅 POST 响应回填（来源反馈 id），列表不返回。
    linked_feedback_id: int | None = None


class WishListResp(BaseModel):
    items: list[WishItem]
    total: int
    page: int
    page_size: int


class WishActionReq(BaseModel):
    action: str  # boost | favorite


class WishActionResp(BaseModel):
    wish_id: int
    action: str
    active: bool
    boost_count: int
    favorite_count: int


class WishStats(BaseModel):
    total: int = 0
    by_status: dict[str, int] = {}
    mine: int = 0
    my_boosted: int = 0
    my_favorited: int = 0


# --- AI 文本润色 -------------------------------------------------------------
class PolishModelRef(BaseModel):
    """opencode ModelRef 的最小形状 —— 前端把当前会话选中的模型原样透传。

    字段名沿用 opencode 的 ``providerID`` / ``id``，避免前后端再做一次改名。
    """
    providerID: str
    id: str


class PolishReq(BaseModel):
    text: str
    # 缺省时后端回退到宿主配置的默认模型。
    model: PolishModelRef | None = None


class PolishResp(BaseModel):
    text: str
    model: str          # "providerID/id"，前端提示条展示用
    elapsed_ms: int
