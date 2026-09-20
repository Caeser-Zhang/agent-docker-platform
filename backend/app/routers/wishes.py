"""心愿墙路由 —— 列表 / 发布 / 编辑 / 软删恢复 / 助力收藏 / 统计。

单一 router（同前缀下混用两种鉴权），**权限在函数内收敛**：

    普通用户：读列表、发心愿（含自助转心愿）、编辑自己的心愿、助力/收藏、看统计
    管理员：以上全部 + 编辑任意心愿 + 改 status + 软删除 / 恢复 + include_hidden

对应设计文档 §5.2 的权限矩阵；``include_hidden`` 与 ``author_name`` 也按角色
收敛——普通用户即使显式传 ``include_hidden=true`` 也拿不到软删记录。

计数一致性三重防线（§6.1）：wish_actions 唯一约束 + ``with_for_update()`` 行锁
+ SQL 级自增（``col + 1``）；递减用 ``case((col > 0, col - 1), else_=0)`` 兜底，
保证计数永不为负，且 SQLite / PostgreSQL 双方言安全。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..auth import get_current_user, require_admin
from ..database import async_session
from ..models import (
    FeedbackCategory,
    OpinionFeedback,
    OpinionStatus,
    User,
    Wish,
    WishAction,
    WishActionType,
    WishStatus,
    WishType,
)
from ..schemas import (
    WishActionReq,
    WishActionResp,
    WishCreate,
    WishItem,
    WishListResp,
    WishStats,
    WishUpdate,
)
from ..services.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/wishes", tags=["wishes"], dependencies=[Depends(get_current_user)]
)

MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 5000

# 发布限流 3 次/60s（转心愿本质就是发心愿，同样计入配额，§5.1）；
# 助力/收藏是高频交互，放宽到 60 次/60s，仅防脚本刷量。
_wish_create_limiter = SlidingWindowLimiter()
_wish_action_limiter = SlidingWindowLimiter()
_CREATE_LIMIT, _CREATE_WINDOW = 3, 60.0
_ACTION_LIMIT, _ACTION_WINDOW = 60, 60.0

_STATUSES = frozenset(s.value for s in WishStatus)
_TYPES = frozenset(t.value for t in WishType)
_ACTIONS = frozenset(a.value for a in WishActionType)
_SCOPES = frozenset({"all", "mine", "favorited", "boosted"})

# 6 种排序；同值一律以 id DESC 稳定收尾（翻页不抖动）。
_SORTS: dict[str, tuple] = {
    "boost_desc": (Wish.boost_count.desc(), Wish.id.desc()),
    "boost_asc": (Wish.boost_count.asc(), Wish.id.desc()),
    "favorite_desc": (Wish.favorite_count.desc(), Wish.id.desc()),
    "favorite_asc": (Wish.favorite_count.asc(), Wish.id.desc()),
    "created_desc": (Wish.created_at.desc(), Wish.id.desc()),
    "created_asc": (Wish.created_at.asc(), Wish.id.desc()),
}


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _one_of(raw: str, allowed: frozenset[str], detail: str) -> str:
    if raw not in allowed:
        raise HTTPException(status_code=422, detail=detail)
    return raw


def _escape_like(q: str) -> str:
    """转义 ``% _ \\``（§6.3），否则搜「100%」会命中全表。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _alive(include_hidden: bool = False):
    """软删除的唯一收敛入口（§6.2）——禁止在本模块裸写 ``select(Wish)``。

    ``include_hidden=True`` 仅在调用方已通过 ``require_admin`` 校验后才允许传入。
    """
    stmt = select(Wish)
    return stmt if include_hidden else stmt.where(Wish.deleted_at.is_(None))


def _alive_with_author(include_hidden: bool = False):
    """列表用：在 _alive() 之上 LEFT JOIN users 取**当前**用户名（§6.4）。

    LEFT JOIN 而非 INNER：作者被删后心愿仍应展示（author_name 为 null →
    前端显示「已注销用户」）。用 EXISTS 子查询而非 JOIN 处理 scope 过滤，
    避免一个心愿多条 action 行放大结果集导致 total / 分页错乱。
    """
    return _alive(include_hidden).add_columns(User.username).outerjoin(
        User, User.id == Wish.author_id
    )


def _wish_item(
    w: Wish,
    *,
    is_mine: bool,
    my_boosted: bool = False,
    my_favorited: bool = False,
    author_name: str | None = None,
    admin_view: bool = False,
    linked_feedback_id: int | None = None,
) -> WishItem:
    return WishItem(
        id=w.id,
        title=w.title,
        description=w.description,
        type=w.type,
        status=w.status,
        boost_count=w.boost_count,
        favorite_count=w.favorite_count,
        created_at=_iso(w.created_at) or "",
        updated_at=_iso(w.updated_at),
        my_boosted=my_boosted,
        my_favorited=my_favorited,
        is_mine=is_mine,
        # 管理员专属字段：普通用户一律置空（服务端是最终防线，前端只是不渲染）。
        author_name=author_name if admin_view else None,
        deleted_at=_iso(w.deleted_at) if admin_view else None,
        linked_feedback_id=linked_feedback_id,
    )


async def _my_actions(db, wish_id: int, user_id: str) -> tuple[bool, bool]:
    """当前用户对某心愿的 (已助力, 已收藏)。"""
    rows = (
        await db.execute(
            select(WishAction.action).where(
                WishAction.wish_id == wish_id, WishAction.user_id == user_id
            )
        )
    ).scalars().all()
    return WishActionType.boost.value in rows, WishActionType.favorite.value in rows


def _validate_payload(title: str, description: str, type_: str) -> tuple[str, str, str]:
    t = (title or "").strip()
    if not t:
        raise HTTPException(status_code=422, detail="心愿标题不能为空")
    if len(t) > MAX_TITLE_CHARS:
        raise HTTPException(status_code=422, detail=f"心愿标题不能超过 {MAX_TITLE_CHARS} 字")
    d = (description or "").strip()
    if len(d) > MAX_DESCRIPTION_CHARS:
        raise HTTPException(
            status_code=422, detail=f"心愿描述不能超过 {MAX_DESCRIPTION_CHARS} 字"
        )
    return t, d, _one_of(type_ or WishType.other.value, _TYPES, "心愿类型不合法")


def _status_list(raw: str | None) -> list[str] | None:
    """逗号串 → 状态列表；含非法值直接 422（不静默忽略）。"""
    if raw is None:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        return None
    for v in values:
        _one_of(v, _STATUSES, "心愿状态不合法")
    return values


# ------------------------------------------------------------------
#  列表
# ------------------------------------------------------------------
@router.get("", response_model=WishListResp)
async def list_wishes(
    q: str | None = None,
    status: str | None = None,
    scope: str = "all",
    sort: str = "boost_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_hidden: bool = False,
    user: User = Depends(get_current_user),
):
    is_admin = (user.role or "") == "admin"
    # include_hidden 仅管理员有效：普通用户传 true 也看不到软删记录。
    hidden = bool(include_hidden) and is_admin

    _one_of(scope, _SCOPES, "过滤范围不合法")
    if sort not in _SORTS:
        raise HTTPException(status_code=422, detail="排序方式不合法")
    statuses = _status_list(status)

    stmt = _alive_with_author(hidden)
    if q and q.strip():
        like = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(
                Wish.title.ilike(like, escape="\\"),
                Wish.description.ilike(like, escape="\\"),
            )
        )
    if statuses:
        stmt = stmt.where(Wish.status.in_(statuses))

    if scope == "mine":
        stmt = stmt.where(Wish.author_id == user.id)
    elif scope in ("favorited", "boosted"):
        action = (
            WishActionType.favorite.value
            if scope == "favorited"
            else WishActionType.boost.value
        )
        stmt = stmt.where(
            select(WishAction.id)
            .where(
                WishAction.wish_id == Wish.id,
                WishAction.user_id == user.id,
                WishAction.action == action,
            )
            .exists()
        )

    async with async_session() as db:
        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    stmt.order_by(*_SORTS[sort])
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .all()
        )

        # 本页的 my_boosted / my_favorited 一次取回后在 Python 侧回填（避免 N+1，
        # 也避免每行两个 EXISTS 子查询）。
        ids = [w.id for w, _ in rows]
        boosted: set[int] = set()
        favorited: set[int] = set()
        if ids:
            res = await db.execute(
                select(WishAction.wish_id, WishAction.action).where(
                    WishAction.user_id == user.id, WishAction.wish_id.in_(ids)
                )
            )
            for wid, act in res.all():
                if act == WishActionType.boost.value:
                    boosted.add(wid)
                elif act == WishActionType.favorite.value:
                    favorited.add(wid)

    return WishListResp(
        items=[
            _wish_item(
                w,
                is_mine=(w.author_id == user.id),
                my_boosted=w.id in boosted,
                my_favorited=w.id in favorited,
                author_name=author_name,
                admin_view=is_admin,
            )
            for w, author_name in rows
        ],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


# ------------------------------------------------------------------
#  发布（含用户自助转心愿通道，D30 ①）
# ------------------------------------------------------------------
@router.post("", status_code=201, response_model=WishItem)
async def create_wish(body: WishCreate, user: User = Depends(get_current_user)):
    title, description, type_ = _validate_payload(body.title, body.description, body.type)

    wait = _wish_create_limiter.hit(f"wish:create:{user.id}", _CREATE_LIMIT, _CREATE_WINDOW)
    if wait > 0:
        raise HTTPException(
            status_code=429, detail=f"发布过于频繁，请 {int(wait) + 1} 秒后再试"
        )

    source_id = body.source_feedback_id
    async with async_session() as db:
        async with db.begin():
            if source_id is not None:
                fb = (
                    await db.execute(
                        select(OpinionFeedback).where(OpinionFeedback.id == source_id)
                    )
                ).scalar_one_or_none()
                if fb is None:
                    raise HTTPException(status_code=404, detail="关联的反馈不存在")
                # 归属校验：自助通道只能关联自己的反馈（管理员通道另走 to-wish）。
                if fb.user_id != user.id:
                    raise HTTPException(status_code=403, detail="无权关联该反馈")
                if fb.category != FeedbackCategory.feature.value:
                    raise HTTPException(status_code=422, detail="仅功能特性类反馈可转为心愿")
                if fb.linked_wish_id is not None:
                    raise HTTPException(status_code=409, detail="该反馈已转化为心愿")

            wish = Wish(
                author_id=user.id,
                title=title,
                description=description,
                type=type_,
                status=WishStatus.evaluating.value,
            )
            db.add(wish)
            await db.flush()

            if source_id is not None:
                # 条件更新 + rowcount：与管理员通道构成幂等竞争，先到先得（§6.8）。
                res = await db.execute(
                    update(OpinionFeedback)
                    .where(
                        OpinionFeedback.id == source_id,
                        OpinionFeedback.linked_wish_id.is_(None),
                    )
                    .values(
                        linked_wish_id=wish.id,
                        status=OpinionStatus.evaluating.value,
                    )
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount == 0:
                    # 抛错让 db.begin() 回滚，连带撤销刚插入的心愿，不留孤儿。
                    raise HTTPException(status_code=409, detail="该反馈已转化为心愿")

        return _wish_item(
            wish,
            is_mine=True,
            linked_feedback_id=source_id,
            admin_view=(user.role or "") == "admin",
            author_name=user.username,
        )


# ------------------------------------------------------------------
#  编辑：作者可改 title/description/type；status 仅管理员
# ------------------------------------------------------------------
@router.patch("/{wish_id:int}", response_model=WishItem)
async def update_wish(
    wish_id: int, body: WishUpdate, user: User = Depends(get_current_user)
):
    is_admin = (user.role or "") == "admin"

    if (
        body.title is None
        and body.description is None
        and body.type is None
        and body.status is None
    ):
        raise HTTPException(status_code=422, detail="至少提供一个要修改的字段")
    # 权限矩阵：作者改 status → 403（管理员才可推进心愿生命周期）。
    if body.status is not None and not is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可修改心愿状态")
    if body.status is not None:
        _one_of(body.status, _STATUSES, "心愿状态不合法")

    async with async_session() as db:
        wish = (
            await db.execute(_alive().where(Wish.id == wish_id))
        ).scalar_one_or_none()
        if wish is None:
            raise HTTPException(status_code=404, detail="心愿不存在")
        is_mine = wish.author_id == user.id
        if not is_mine and not is_admin:
            raise HTTPException(status_code=403, detail="无权编辑该心愿")

        # 字段级校验放在权限校验之后：无权限者不该探测出校验规则差异。
        if body.title is not None or body.description is not None or body.type is not None:
            t, d, ty = _validate_payload(
                body.title if body.title is not None else wish.title,
                body.description if body.description is not None else wish.description,
                body.type if body.type is not None else wish.type,
            )
            if body.title is not None:
                wish.title = t
            if body.description is not None:
                wish.description = d
            if body.type is not None:
                wish.type = ty
        if body.status is not None:
            wish.status = body.status

        await db.commit()
        await db.refresh(wish)
        boosted, favorited = await _my_actions(db, wish_id, user.id)
        author_name = (
            (await db.execute(select(User.username).where(User.id == wish.author_id))).scalar_one_or_none()
            if is_admin
            else None
        )
        return _wish_item(
            wish,
            is_mine=is_mine,
            my_boosted=boosted,
            my_favorited=favorited,
            author_name=author_name,
            admin_view=is_admin,
        )


# ------------------------------------------------------------------
#  软删除 / 恢复：管理员专属
# ------------------------------------------------------------------
@router.delete("/{wish_id:int}")
async def delete_wish(wish_id: int, admin: User = Depends(require_admin)):
    """软删除（D24）。rowcount == 0 → 404，重复删除不静默成功。

    不删 wish_actions 行：恢复后计数与用户操作状态需保持一致。
    """
    async with async_session() as db:
        res = await db.execute(
            update(Wish)
            .where(Wish.id == wish_id, Wish.deleted_at.is_(None))
            .values(deleted_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="心愿不存在")
    logger.info("wish soft-deleted by admin %s: id=%s", admin.id, wish_id)
    return {"id": wish_id, "deleted": True}


@router.post("/{wish_id:int}/restore")
async def restore_wish(wish_id: int, admin: User = Depends(require_admin)):
    """恢复软删除的心愿（管理员专属）。"""
    async with async_session() as db:
        res = await db.execute(
            update(Wish)
            .where(Wish.id == wish_id, Wish.deleted_at.is_not(None))
            .values(deleted_at=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="心愿不存在或未被删除")
    logger.info("wish restored by admin %s: id=%s", admin.id, wish_id)
    return {"id": wish_id, "deleted": False}


# ------------------------------------------------------------------
#  助力 / 收藏 toggle
# ------------------------------------------------------------------
@router.post("/{wish_id:int}/actions", response_model=WishActionResp)
async def toggle_action(
    wish_id: int, body: WishActionReq, user: User = Depends(get_current_user)
):
    action = _one_of(body.action, _ACTIONS, "操作类型不合法")

    wait = _wish_action_limiter.hit(
        f"wish:action:{user.id}", _ACTION_LIMIT, _ACTION_WINDOW
    )
    if wait > 0:
        raise HTTPException(
            status_code=429, detail=f"操作过于频繁，请 {int(wait) + 1} 秒后再试"
        )

    col = Wish.boost_count if action == WishActionType.boost.value else Wish.favorite_count
    active = True
    try:
        async with async_session() as db:
            async with db.begin():
                # 行锁：挡住两个请求同时读到 existing is None 后双双 INSERT。
                # SQLite 上 with_for_update() 退化为 no-op，此时唯一约束兜底。
                wish = (
                    await db.execute(
                        _alive().where(Wish.id == wish_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if wish is None:
                    raise HTTPException(status_code=404, detail="心愿不存在")

                existing = (
                    await db.execute(
                        select(WishAction).where(
                            WishAction.wish_id == wish_id,
                            WishAction.user_id == user.id,
                            WishAction.action == action,
                        )
                    )
                ).scalar_one_or_none()

                if existing is None:
                    db.add(WishAction(wish_id=wish_id, user_id=user.id, action=action))
                    # SQL 级自增（非 Python += 1），消除读-改-写窗口的丢失更新。
                    await db.execute(
                        update(Wish)
                        .where(Wish.id == wish_id)
                        .values({col.key: col + 1})
                        .execution_options(synchronize_session=False)
                    )
                    active = True
                else:
                    await db.delete(existing)
                    # 双方言安全的「减到 0 为止」（func.greatest 在旧版 SQLite 上不可用）。
                    await db.execute(
                        update(Wish)
                        .where(Wish.id == wish_id)
                        .values({col.key: case((col > 0, col - 1), else_=0)})
                        .execution_options(synchronize_session=False)
                    )
                    active = False
    except IntegrityError:
        # 并发双击：唯一约束已挡住重复行 → 视为「已生效」，回显权威计数即可。
        active = True
        logger.info(
            "wish action race resolved by unique constraint: wish=%s user=%s action=%s",
            wish_id, user.id, action,
        )

    # 事务已提交，重新读取权威计数：锁定过的 ORM 对象属性此时已过期，直接读会
    # 在 async 上下文触发惰性加载 → MissingGreenlet。
    async with async_session() as db:
        fresh = (
            await db.execute(
                select(Wish.boost_count, Wish.favorite_count).where(Wish.id == wish_id)
            )
        ).one_or_none()
    if fresh is None:
        raise HTTPException(status_code=404, detail="心愿不存在")

    return WishActionResp(
        wish_id=wish_id,
        action=action,
        active=active,
        boost_count=int(fresh.boost_count or 0),
        favorite_count=int(fresh.favorite_count or 0),
    )


# ------------------------------------------------------------------
#  统计条
# ------------------------------------------------------------------
@router.get("/stats", response_model=WishStats)
async def wish_stats(user: User = Depends(get_current_user)):
    async with async_session() as db:
        rows = (
            await db.execute(
                _alive()
                .with_only_columns(Wish.status, func.count(Wish.id))
                .group_by(Wish.status)
            )
        ).all()

        mine = (
            await db.execute(
                select(func.count())
                .select_from(Wish)
                .where(Wish.deleted_at.is_(None), Wish.author_id == user.id)
            )
        ).scalar_one()

        act_rows = (
            await db.execute(
                select(WishAction.action, func.count(WishAction.id))
                .join(Wish, Wish.id == WishAction.wish_id)
                .where(Wish.deleted_at.is_(None), WishAction.user_id == user.id)
                .group_by(WishAction.action)
            )
        ).all()

    by_status = {s.value: 0 for s in WishStatus}
    total = 0
    for st, n in rows:
        by_status[st] = by_status.get(st, 0) + int(n)
        total += int(n)
    mine_count = int(mine or 0)
    acted = {a: int(n) for a, n in act_rows}

    return WishStats(
        total=total,
        by_status=by_status,
        mine=mine_count,
        my_boosted=acted.get(WishActionType.boost.value, 0),
        my_favorited=acted.get(WishActionType.favorite.value, 0),
    )
