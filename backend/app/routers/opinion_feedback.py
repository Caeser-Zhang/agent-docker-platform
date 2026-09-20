"""意见反馈路由 —— 用户提交（含截图）+ 管理员看板。

两个 router 同处一个模块（沿用 library.py 范式），**权限边界即 router 边界**：

    router        /api/opinions/*        任意登录用户   仅「提交」一个写入口
    admin_router  /api/admin/opinions/*  role="admin"   看板/改状态/看截图/转心愿

普通用户没有任何读取他人反馈的路径，截图字节端点也只对管理员开放（D35）。

关键约定（见 docs/feedback-and-wishwall-design.md）：
  * ``name``/``uid`` 是提交时刻的快照（审计记录须保留身份原貌，D1）。
  * 截图五道防线：5MB 原始上限 → header 尺寸闸门（防解码炸弹）→ Pillow 重编码
    为 PNG（丢弃原始字节）→ 服务端随机命名（防路径穿越）→ 仅管理员可读。
  * 转心愿走 ``UPDATE ... WHERE linked_wish_id IS NULL`` + rowcount 判定，与用户
    自助通道（wishes.py）构成幂等竞争，先到先得（§6.8）。
"""
from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy import func, or_, select, update

from ..auth import get_current_user, require_admin
from ..config import settings
from ..database import async_session
from ..models import (
    FeedbackCategory,
    OpinionAttachment,
    OpinionFeedback,
    OpinionStatus,
    User,
    Wish,
    WishStatus,
    WishType,
)
from ..schemas import (
    OpinionAttachmentItem,
    OpinionCategoryStats,
    OpinionItem,
    OpinionListResp,
    OpinionPatch,
    OpinionPatchResp,
    OpinionStatsResp,
    OpinionSubmitResp,
    ToWishReq,
    ToWishResp,
)
from ..services.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/opinions", tags=["opinions"], dependencies=[Depends(get_current_user)]
)
admin_router = APIRouter(
    prefix="/api/admin/opinions", tags=["opinions"], dependencies=[Depends(require_admin)]
)

# --- 截图与文本上限（§6.7）---------------------------------------------------
MAX_OPINION_IMAGE_BYTES = 5 * 1024 * 1024   # 单张原始上传上限
MAX_OPINION_IMAGES = 3                      # 每条反馈最多张数
ATTACHMENT_MAX_EDGE = 1600                  # 重编码后最长边（截图需保留可读性）
DECODE_BOMB_EDGE = 20000                    # load() 前的尺寸闸门，防解码炸弹
_READ_CHUNK = 1024 * 1024

MIN_CONTENT_CHARS = 5
MAX_CONTENT_CHARS = 2000
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 5000

# 提交限流：每用户 60s 最多 5 次（人工填写反馈远低于此，纯防刷）。
_opinion_limiter = SlidingWindowLimiter()
_RATE_LIMIT = 5
_RATE_WINDOW = 60.0

_CATEGORIES = frozenset(c.value for c in FeedbackCategory)
_STATUSES = frozenset(s.value for s in OpinionStatus)
_WISH_TYPES = frozenset(t.value for t in WishType)


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _one_of(raw: str, allowed: frozenset[str], detail: str) -> str:
    """枚举白名单校验——非法值一律 422，绝不静默忽略。"""
    if raw not in allowed:
        raise HTTPException(status_code=422, detail=detail)
    return raw


def _escape_like(q: str) -> str:
    """转义 ``% _ \\``，否则用户搜「100%」会命中全表（§6.3）。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _attachment_dir() -> Path:
    return Path(settings.opinion_attachment_dir)


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """分块读，超限即在缓冲整个 body 之前拒绝（F17）。"""
    buf = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(
                status_code=413, detail=f"图片过大（超过 {cap // 1024 // 1024}MB 上限）"
            )
    return bytes(buf)


def _reencode_image(data: bytes) -> tuple[bytes, int, int]:
    """解码 → 限尺寸 → 统一重编码为 PNG（阻塞调用，须在 to_thread 里跑）。

    重编码本身就是一道安全闸：伪造扩展名的非图片字节会在 Image.open 处失败，
    SVG/HTML 之类可被浏览器执行的格式也会被栅格化为无害位图，GIF 只取第 0 帧。
    """
    try:
        img = Image.open(io.BytesIO(data))
        # 先读 header 里的尺寸再决定是否真正解码：一张几 KB 的 PNG 可以声明
        # 100000×100000，load() 时会吃掉数十 GB 内存。
        if max(img.size) > DECODE_BOMB_EDGE:
            raise HTTPException(status_code=422, detail="图片尺寸异常，请重新截图")
        img.load()  # 强制真正解码，截断文件在此暴露
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="图片无法解析，请重新截图") from exc

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    img.thumbnail((ATTACHMENT_MAX_EDGE, ATTACHMENT_MAX_EDGE), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), img.width, img.height


def _write_attachment(filename: str, data: bytes) -> None:
    """落盘（阻塞调用）。目录惰性创建，避免只读卷导致启动失败（§4.5）。"""
    d = _attachment_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(data)


def _status_filter(raw: str | None) -> list[str] | None:
    """逗号串 → 状态列表；含非法值直接 422（不静默忽略，测试 #18b）。"""
    if raw is None:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        return None
    for v in values:
        _one_of(v, _STATUSES, "反馈状态不合法")
    return values


def _attachment_item(a: OpinionAttachment, *, with_url: bool = False) -> OpinionAttachmentItem:
    return OpinionAttachmentItem(
        id=a.id,
        width=a.width,
        height=a.height,
        size_bytes=a.size_bytes,
        content_type=a.content_type or "image/png",
        # 用户侧提交响应不带 url：附件字节仅管理员可读（D35）。
        url=f"/api/admin/opinions/attachments/{a.id}" if with_url else None,
        created_at=_iso(a.created_at) if with_url else None,
    )


# ------------------------------------------------------------------
#  用户侧：提交反馈
# ------------------------------------------------------------------
@router.post("", status_code=201, response_model=OpinionSubmitResp)
async def submit_opinion(
    category: str = Form(...),
    content: str = Form(...),
    images: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
):
    """提交一条意见反馈（multipart，无论是否带图都走同一分支）。

    校验顺序即代码顺序：轻量校验(1-4) → 限流(5) → 图片读取/解码(6-7)，
    避免非法请求白耗配额、未过配额就烧 CPU 做 Pillow 解码。
    """
    _one_of(category, _CATEGORIES, "反馈分类不合法")

    text = (content or "").strip()
    if len(text) < MIN_CONTENT_CHARS:
        raise HTTPException(status_code=422, detail=f"反馈内容至少 {MIN_CONTENT_CHARS} 个字")
    if len(text) > MAX_CONTENT_CHARS:
        raise HTTPException(status_code=422, detail=f"反馈内容不能超过 {MAX_CONTENT_CHARS} 字")
    if len(images) > MAX_OPINION_IMAGES:
        raise HTTPException(status_code=422, detail=f"最多上传 {MAX_OPINION_IMAGES} 张截图")

    wait = _opinion_limiter.hit(f"opinion:{user.id}", _RATE_LIMIT, _RATE_WINDOW)
    if wait > 0:
        raise HTTPException(
            status_code=429, detail=f"提交过于频繁，请 {int(wait) + 1} 秒后再试"
        )

    # 阶段一：全部图片解码完成后再开事务——任一张非法时一张都不落盘（测试 #11/#13）。
    encoded: list[tuple[bytes, int, int]] = []
    for raw in images:
        data = await _read_capped(raw, MAX_OPINION_IMAGE_BYTES)
        encoded.append(await asyncio.to_thread(_reencode_image, data))

    # 阶段二：反馈与附件在同一事务内落库（原子）。
    async with async_session() as db:
        async with db.begin():
            fb = OpinionFeedback(
                user_id=user.id,
                category=category,
                name=user.username or "",  # 快照（D1）
                uid=user.uid,              # 快照
                content=text,
            )
            db.add(fb)
            await db.flush()  # 取得 fb.id 供附件外键使用

            attachments: list[OpinionAttachment] = []
            for png, w, h in encoded:
                # 完全不用客户端文件名 → 路径穿越不成立（测试 #15）。
                filename = f"{uuid.uuid4().hex}.png"
                await asyncio.to_thread(_write_attachment, filename, png)
                att = OpinionAttachment(
                    feedback_id=fb.id,
                    filename=filename,
                    content_type="image/png",
                    width=w,
                    height=h,
                    size_bytes=len(png),
                )
                db.add(att)
                attachments.append(att)

        return OpinionSubmitResp(
            id=fb.id,
            category=fb.category,
            status=fb.status,
            created_at=_iso(fb.created_at) or "",
            attachments=[_attachment_item(a) for a in attachments],
        )


# ------------------------------------------------------------------
#  管理侧：看板（以下全部 require_admin）
# ------------------------------------------------------------------
@admin_router.get("", response_model=OpinionListResp)
async def list_opinions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    """分类 Tab + 状态多选 + 关键词搜索 + 分页，固定 created_at DESC, id DESC。"""
    if category is not None:
        _one_of(category, _CATEGORIES, "反馈分类不合法")
    statuses = _status_filter(status)

    stmt = select(OpinionFeedback)
    if category is not None:
        stmt = stmt.where(OpinionFeedback.category == category)
    if statuses:
        stmt = stmt.where(OpinionFeedback.status.in_(statuses))
    if q and q.strip():
        like = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(
                OpinionFeedback.content.ilike(like, escape="\\"),
                OpinionFeedback.name.ilike(like, escape="\\"),
                OpinionFeedback.uid.ilike(like, escape="\\"),
            )
        )

    async with async_session() as db:
        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    stmt.order_by(
                        OpinionFeedback.created_at.desc(), OpinionFeedback.id.desc()
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )

        # 附件数一次 GROUP BY 批量取回，禁止逐行 COUNT（N+1）。
        ids = [r.id for r in rows]
        counts: dict[int, int] = {}
        if ids:
            res = await db.execute(
                select(OpinionAttachment.feedback_id, func.count(OpinionAttachment.id))
                .where(OpinionAttachment.feedback_id.in_(ids))
                .group_by(OpinionAttachment.feedback_id)
            )
            counts = {fid: n for fid, n in res.all()}

    return OpinionListResp(
        items=[
            OpinionItem(
                id=r.id,
                category=r.category,
                name=r.name,
                uid=r.uid,
                content=r.content,
                status=r.status,
                linked_wish_id=r.linked_wish_id,
                attachment_count=counts.get(r.id, 0),
                created_at=_iso(r.created_at) or "",
            )
            for r in rows
        ],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@admin_router.get("/stats", response_model=OpinionStatsResp)
async def opinion_stats():
    """分类 × 状态聚合，供 Tab 徽标与统计卡片（D34）。"""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    OpinionFeedback.category,
                    OpinionFeedback.status,
                    func.count(OpinionFeedback.id),
                ).group_by(OpinionFeedback.category, OpinionFeedback.status)
            )
        ).all()

        att_rows = (
            await db.execute(
                select(OpinionFeedback.category, func.count(func.distinct(OpinionFeedback.id)))
                .join(
                    OpinionAttachment,
                    OpinionAttachment.feedback_id == OpinionFeedback.id,
                )
                .group_by(OpinionFeedback.category)
            )
        ).all()

        linked = (
            await db.execute(
                select(func.count())
                .select_from(OpinionFeedback)
                .where(
                    OpinionFeedback.category == FeedbackCategory.feature.value,
                    OpinionFeedback.linked_wish_id.is_not(None),
                )
            )
        ).scalar_one()

        # 看板上最需要被看见的「积压告警」：7 天前提交且仍未解决的 bug。
        stale = (
            await db.execute(
                select(func.count())
                .select_from(OpinionFeedback)
                .where(
                    OpinionFeedback.category == FeedbackCategory.bug.value,
                    OpinionFeedback.status == OpinionStatus.open.value,
                    OpinionFeedback.created_at < _cutoff(7),
                )
            )
        ).scalar_one()

    def _blank() -> dict:
        return {"total": 0, "by_status": {s.value: 0 for s in OpinionStatus}}

    acc = {FeedbackCategory.bug.value: _blank(), FeedbackCategory.feature.value: _blank()}
    for cat, st, n in rows:
        bucket = acc.get(cat)
        if bucket is None:
            continue
        bucket["total"] += int(n)
        bucket["by_status"][st] = bucket["by_status"].get(st, 0) + int(n)
    with_attachment = {cat: int(n) for cat, n in att_rows}
    linked = int(linked or 0)

    bug_total = acc[FeedbackCategory.bug.value]["total"]
    feature_total = acc[FeedbackCategory.feature.value]["total"]

    return OpinionStatsResp(
        bug=OpinionCategoryStats(
            total=bug_total,
            by_status=acc[FeedbackCategory.bug.value]["by_status"],
            with_attachment=with_attachment.get(FeedbackCategory.bug.value, 0),
            unresolved_7d=int(stale or 0),
        ),
        feature=OpinionCategoryStats(
            total=feature_total,
            by_status=acc[FeedbackCategory.feature.value]["by_status"],
            with_attachment=with_attachment.get(FeedbackCategory.feature.value, 0),
            linked_to_wish=linked,
            # total == 0 时不除零。
            conversion_rate=round(linked / feature_total, 3) if feature_total else 0.0,
        ),
    )


@admin_router.patch("/{opinion_id:int}", response_model=OpinionPatchResp)
async def patch_opinion(opinion_id: int, body: OpinionPatch):
    """管理员改状态 / 改分类。二者正交：改分类不联动状态、不触发转心愿。"""
    if body.status is None and body.category is None:
        raise HTTPException(status_code=422, detail="至少提供一个要修改的字段")
    if body.status is not None:
        _one_of(body.status, _STATUSES, "反馈状态不合法")
    if body.category is not None:
        _one_of(body.category, _CATEGORIES, "反馈分类不合法")

    async with async_session() as db:
        fb = (
            await db.execute(select(OpinionFeedback).where(OpinionFeedback.id == opinion_id))
        ).scalar_one_or_none()
        if fb is None:
            raise HTTPException(status_code=404, detail="反馈不存在")

        if body.status is not None:
            fb.status = body.status
        if body.category is not None:
            fb.category = body.category
        await db.commit()

        return OpinionPatchResp(id=fb.id, status=fb.status, category=fb.category)


@admin_router.get("/{opinion_id:int}/attachments")
async def list_attachments(opinion_id: int):
    """某条反馈的截图元数据列表（无附件返回空数组，非 404）。"""
    async with async_session() as db:
        exists = (
            await db.execute(
                select(OpinionFeedback.id).where(OpinionFeedback.id == opinion_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="反馈不存在")

        rows = (
            (
                await db.execute(
                    select(OpinionAttachment)
                    .where(OpinionAttachment.feedback_id == opinion_id)
                    .order_by(OpinionAttachment.id)
                )
            )
            .scalars()
            .all()
        )

    return {
        "feedback_id": opinion_id,
        "items": [_attachment_item(a, with_url=True).model_dump() for a in rows],
    }


@admin_router.get("/attachments/{attachment_id:int}")
async def get_attachment(attachment_id: int):
    """截图字节（D35：仅管理员可读，前端须 fetch 成 Blob，不能 <img src> 直连）。"""
    async with async_session() as db:
        att = (
            await db.execute(
                select(OpinionAttachment).where(OpinionAttachment.id == attachment_id)
            )
        ).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="附件不存在")

    base = _attachment_dir().resolve()
    # filename 是服务端生成的随机名；再校验一次落盘位置，纵深防御。
    path = (base / att.filename).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        # 元数据在但文件没了 → 多半是卷未挂载或被误删，留痕便于运维定位。
        logger.warning(
            "opinion attachment file missing: id=%s filename=%s dir=%s",
            att.id, att.filename, base,
        )
        raise HTTPException(status_code=404, detail="附件文件不存在")

    return FileResponse(path, media_type=att.content_type or "image/png")


@admin_router.post("/{opinion_id:int}/to-wish", status_code=201, response_model=ToWishResp)
async def to_wish(
    opinion_id: int,
    body: ToWishReq,
    admin: User = Depends(require_admin),
):
    """管理员通道：一键把反馈转为心愿（不限制 category，误标 bug 也能转）。"""
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="心愿标题不能为空")
    if len(title) > MAX_TITLE_CHARS:
        raise HTTPException(status_code=422, detail=f"心愿标题不能超过 {MAX_TITLE_CHARS} 字")
    description = (body.description or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise HTTPException(
            status_code=422, detail=f"心愿描述不能超过 {MAX_DESCRIPTION_CHARS} 字"
        )
    _one_of(body.type or WishType.other.value, _WISH_TYPES, "心愿类型不合法")

    async with async_session() as db:
        fb = (
            await db.execute(select(OpinionFeedback).where(OpinionFeedback.id == opinion_id))
        ).scalar_one_or_none()
        if fb is None:
            raise HTTPException(status_code=404, detail="反馈不存在")
        if fb.linked_wish_id is not None:
            raise HTTPException(status_code=409, detail="该反馈已转化为心愿")

        # 上面的 select 已触发 autobegin，不能再 db.begin()（会报「transaction
        # already begun」）；显式 commit 收尾，409 路径不 commit → close 时自动回滚。
        wish = Wish(
            author_id=admin.id,  # 管理员通道：作者记为操作者本人
            title=title,
            description=description,
            type=body.type or WishType.other.value,
            status=WishStatus.evaluating.value,
        )
        db.add(wish)
        await db.flush()  # 取得 wish.id

        # 条件更新 + rowcount：与用户自助通道共用同一把「锁」（§6.8）。
        res = await db.execute(
            update(OpinionFeedback)
            .where(
                OpinionFeedback.id == opinion_id,
                OpinionFeedback.linked_wish_id.is_(None),
            )
            .values(
                linked_wish_id=wish.id,
                status=OpinionStatus.evaluating.value,
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 0:
            # 已被另一通道抢先 → 不 commit，session 关闭时回滚，连带撤销刚插入的心愿。
            raise HTTPException(status_code=409, detail="该反馈已转化为心愿")
        await db.commit()
        wish_id = wish.id

    return ToWishResp(
        feedback_id=opinion_id,
        wish_id=wish_id,
        status=OpinionStatus.evaluating.value,
        linked_wish_id=wish_id,
    )
