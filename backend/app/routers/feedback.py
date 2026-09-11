"""用户体验反馈路由（任务一）—— assistant 回复的点赞/点踩采集。

用户端接口，全部要求登录（``get_current_user``）。契约（见最终设计）：
  * verdict ∈ {"up","down"}；点击后不可取消，重提同一 (user_id, message_id)
    幂等返回 already=true（不改写既有 verdict/原因）。
  * 点踩时携带 reason_codes（白名单）+ 可选 reason_text（「其他」自由文本）。
  * context 是前端提交的**本轮完整上下文快照**（上一 user 提问 → 本 assistant
    全部输出）；后端二次截断，超限则置 context_truncated=true。
  * 轻限流：SlidingWindowLimiter，防刷。
  * 提供 GET 让前端在加载会话时回填已锁定状态。

反馈行 user_id 无 FK（证据留存约定），写入永不因 FK 缺失而失败。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..auth import get_current_user
from ..database import async_session
from ..models import MessageFeedback, User
from ..services.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])

# 提交限流：每用户每 60s 最多 30 次（远高于正常人工点击频率，仅防刷）。
_feedback_limiter = SlidingWindowLimiter()
_RATE_LIMIT = 30
_RATE_WINDOW = 60.0

# 点踩原因码白名单（与前端 FeedbackModal 保持一致）。
REASON_CODES = frozenset({
    "misunderstood",     # 没理解我的意图
    "wrong_answer",      # 答案错误
    "tool_failure",      # 工具调用失败
    "too_verbose",       # 太啰嗦
    "ignored_constraints",  # 忽略了约束/要求
    "interrupted",       # 中途被打断
    "other",             # 其他（配合 reason_text）
})

# context 快照与 reason_text 的落库上限（防超大 payload）。
_MAX_CONTEXT_CHARS = 60_000
_MAX_REASON_TEXT = 500


class FeedbackIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=255)
    message_id: str = Field(..., min_length=1, max_length=255)
    user_message_id: str | None = Field(None, max_length=255)
    verdict: str = Field(..., pattern="^(up|down)$")
    reason_codes: list[str] = Field(default_factory=list)
    reason_text: str | None = None
    turn_errored: bool = False
    model_provider: str | None = Field(None, max_length=128)
    model_id: str | None = Field(None, max_length=128)
    agent: str | None = Field(None, max_length=128)
    # 本轮完整上下文快照（前端组装）；后端整体 JSON 化后二次截断。
    context: dict = Field(default_factory=dict)


def _serialize_context(context: dict) -> tuple[str, bool]:
    """把 context dict 序列化为 JSON 字符串，超限截断。返回 (text, truncated)。"""
    try:
        text = json.dumps(context, ensure_ascii=False)
    except (TypeError, ValueError):
        text = "{}"
    if len(text) > _MAX_CONTEXT_CHARS:
        return text[:_MAX_CONTEXT_CHARS], True
    return text, False


@router.post("")
async def submit_feedback(body: FeedbackIn, user: User = Depends(get_current_user)):
    """提交一条点赞/点踩反馈。幂等：重复提交返回 already=true。"""
    # 轻限流
    wait = _feedback_limiter.hit(f"fb:{user.id}", _RATE_LIMIT, _RATE_WINDOW)
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"Too many feedback submissions; retry in {wait:.0f}s")

    # 原因码白名单校验（仅 down 有意义；up 忽略原因）
    reason_codes: list[str] = []
    if body.verdict == "down":
        for code in body.reason_codes:
            if code in REASON_CODES and code not in reason_codes:
                reason_codes.append(code)

    reason_text = None
    if body.verdict == "down" and body.reason_text:
        reason_text = body.reason_text.strip()[:_MAX_REASON_TEXT] or None

    context_text, truncated = _serialize_context(body.context)

    async with async_session() as db:
        existing = (await db.execute(
            select(MessageFeedback).where(
                MessageFeedback.user_id == user.id,
                MessageFeedback.message_id == body.message_id[:255],
            ).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            return {
                "ok": True,
                "already": True,
                "verdict": existing.verdict,
                "message_id": existing.message_id,
            }

        row = MessageFeedback(
            user_id=user.id,
            session_id=body.session_id[:255],
            message_id=body.message_id[:255],
            user_message_id=body.user_message_id[:255] if body.user_message_id else None,
            verdict=body.verdict,
            reason_codes=json.dumps(reason_codes, ensure_ascii=False),
            reason_text=reason_text,
            turn_errored=bool(body.turn_errored),
            model_provider=body.model_provider,
            model_id=body.model_id,
            agent=body.agent,
            context=context_text,
            context_truncated=truncated,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # 并发重复提交（自然键撞车）—— 视为已存在。
            # 注意：rollback 会 expire 掉所有实例，此后不能再读 row 的属性
            # （async 下会触发懒加载 → MissingGreenlet），故直接用入参回显。
            await db.rollback()
            return {
                "ok": True,
                "already": True,
                "verdict": body.verdict,
                "message_id": body.message_id[:255],
            }

    return {
        "ok": True,
        "already": False,
        "verdict": body.verdict,
        "message_id": row.message_id,
    }


@router.get("/session/{session_id}")
async def list_session_feedback(
    session_id: str,
    user: User = Depends(get_current_user),
    limit: int = Query(500, ge=1, le=2000),
):
    """某会话下当前用户已提交的反馈，供前端加载时回填锁定态。

    仅返回渲染所需的最小字段（message_id → verdict），不含 context 快照。
    """
    async with async_session() as db:
        rows = (await db.execute(
            select(MessageFeedback)
            .where(
                MessageFeedback.user_id == user.id,
                MessageFeedback.session_id == session_id[:255],
            )
            .order_by(MessageFeedback.id.desc())
            .limit(limit)
        )).scalars().all()

    return {
        "session_id": session_id,
        "feedback": [
            {
                "message_id": r.message_id,
                "verdict": r.verdict,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
