"""管理员用户体验指标看板路由（任务二）—— 仅 admin 可见。

四层指标（产品经理视角）：
  L1 结果层：任务成功率、回合成功率、错误率
  L2 效率与性能：回合耗时（均值 + p50/p90/p99）、成本
  L3 过程与轨迹：工具调用准确率、Token 消耗效率
  L4 主观满意度：点赞/点踩（整合任务一）

分位数在应用层排序取值（SQLite 无 percentile_cont），GROUP BY 聚合走 SQL。
所有查询按 created_at 时间窗过滤；明细行数受 metrics_collector 的保留上限约束。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

from ..auth import require_admin
from ..crypto import decrypt_password_compat
from ..database import async_session
from ..models import AgentContainer, AgentRoundMetrics, MessageFeedback, ToolCallMetrics
from ..services.container_manager import container_manager
from ..services.metrics_collector import backfill_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/ux", tags=["admin-ux"], dependencies=[Depends(require_admin)])


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _pct(sorted_vals: list[float], p: float) -> float | None:
    """最近秩法分位数（输入须已升序）。空列表返回 None。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _round_filters(days: int, user_id: str | None, model_provider: str | None) -> list:
    conds = [AgentRoundMetrics.created_at >= _cutoff(days)]
    if user_id:
        conds.append(AgentRoundMetrics.user_id == user_id)
    if model_provider:
        conds.append(AgentRoundMetrics.model_provider == model_provider)
    return conds


@router.get("/overview")
async def ux_overview(
    days: int = Query(30, ge=1, le=365),
    user_id: str | None = Query(None),
    model_provider: str | None = Query(None),
):
    """四层核心指标汇总（时间窗内）。"""
    conds = _round_filters(days, user_id, model_provider)
    cols = [
        AgentRoundMetrics.succeeded,
        AgentRoundMetrics.is_task,
        AgentRoundMetrics.task_success,
        AgentRoundMetrics.errored,
        AgentRoundMetrics.duration_ms,
        AgentRoundMetrics.total_tokens,
        AgentRoundMetrics.cost,
        AgentRoundMetrics.tool_calls,
        AgentRoundMetrics.tool_errors,
    ]
    async with async_session() as db:
        rows = (await db.execute(select(*cols).where(*conds))).all()

        # L4 主观满意度（反馈表按 created_at 过滤，独立于 round 维度过滤）
        fb_conds = [MessageFeedback.created_at >= _cutoff(days)]
        if user_id:
            fb_conds.append(MessageFeedback.user_id == user_id)
        up = (await db.execute(
            select(func.count(MessageFeedback.id)).where(*fb_conds, MessageFeedback.verdict == "up")
        )).scalar_one()
        down = (await db.execute(
            select(func.count(MessageFeedback.id)).where(*fb_conds, MessageFeedback.verdict == "down")
        )).scalar_one()
        # 点踩原因分布
        down_rows = (await db.execute(
            select(MessageFeedback.reason_codes).where(*fb_conds, MessageFeedback.verdict == "down")
        )).scalars().all()

    total = len(rows)
    succeeded = sum(1 for r in rows if r.succeeded)
    errored = sum(1 for r in rows if r.errored)
    task_rows = [r for r in rows if r.is_task]
    task_success = sum(1 for r in task_rows if r.task_success is True)

    durations = sorted(float(r.duration_ms) for r in rows if r.duration_ms is not None)
    total_cost = sum(float(r.cost) for r in rows if r.cost is not None)
    token_vals = [r.total_tokens for r in rows if r.total_tokens is not None]
    total_tokens = sum(token_vals)
    tool_calls = sum(r.tool_calls or 0 for r in rows)
    tool_errors = sum(r.tool_errors or 0 for r in rows)

    # 点踩原因码分布
    reason_dist: dict[str, int] = {}
    for raw in down_rows:
        try:
            codes = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            codes = []
        if isinstance(codes, list):
            for c in codes:
                if isinstance(c, str):
                    reason_dist[c] = reason_dist.get(c, 0) + 1

    fb_total = up + down
    return {
        "window_days": days,
        "filters": {"user_id": user_id, "model_provider": model_provider},
        "l1_outcome": {
            "rounds_total": total,
            "round_success_rate": (succeeded / total) if total else None,
            "error_rate": (errored / total) if total else None,
            "task_rounds": len(task_rows),
            "task_success_rate": (task_success / len(task_rows)) if task_rows else None,
        },
        "l2_efficiency": {
            "duration_avg_ms": (sum(durations) / len(durations)) if durations else None,
            "duration_p50_ms": _pct(durations, 50),
            "duration_p90_ms": _pct(durations, 90),
            "duration_p99_ms": _pct(durations, 99),
            "total_cost": total_cost,
            "avg_cost": (total_cost / total) if total else None,
        },
        "l3_process": {
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "tool_accuracy": (1 - tool_errors / tool_calls) if tool_calls else None,
            "total_tokens": total_tokens,
            "avg_tokens_per_round": (total_tokens / total) if total else None,
            # Token 效率：每个成功回合消耗的 token（越低越好）。
            "tokens_per_success": (total_tokens / succeeded) if succeeded else None,
        },
        "l4_satisfaction": {
            "thumbs_up": up,
            "thumbs_down": down,
            "total": fb_total,
            "satisfaction_rate": (up / fb_total) if fb_total else None,
            "down_reasons": reason_dist,
        },
    }


@router.get("/trends")
async def ux_trends(
    days: int = Query(30, ge=1, le=365),
    user_id: str | None = Query(None),
    model_provider: str | None = Query(None),
):
    """按天分桶的时间序列（成功率 / 平均耗时 / 工具准确率 / 满意度）。"""
    conds = _round_filters(days, user_id, model_provider)
    cols = [
        AgentRoundMetrics.created_at,
        AgentRoundMetrics.succeeded,
        AgentRoundMetrics.duration_ms,
        AgentRoundMetrics.tool_calls,
        AgentRoundMetrics.tool_errors,
        AgentRoundMetrics.total_tokens,
        AgentRoundMetrics.cost,
    ]
    fb_conds = [MessageFeedback.created_at >= _cutoff(days)]
    if user_id:
        fb_conds.append(MessageFeedback.user_id == user_id)

    async with async_session() as db:
        rows = (await db.execute(
            select(*cols).where(*conds).order_by(AgentRoundMetrics.created_at)
        )).all()
        fb_rows = (await db.execute(
            select(MessageFeedback.created_at, MessageFeedback.verdict).where(*fb_conds)
        )).all()

    # Python 分桶（跨方言安全；不依赖 date()/date_trunc 差异）。
    buckets: dict[str, dict] = {}

    def _bucket(day: str) -> dict:
        b = buckets.get(day)
        if b is None:
            b = {
                "date": day, "rounds": 0, "succeeded": 0, "durations": [],
                "tool_calls": 0, "tool_errors": 0, "total_tokens": 0, "cost": 0.0,
                "up": 0, "down": 0,
            }
            buckets[day] = b
        return b

    for r in rows:
        if r.created_at is None:
            continue
        day = r.created_at.date().isoformat()
        b = _bucket(day)
        b["rounds"] += 1
        if r.succeeded:
            b["succeeded"] += 1
        if r.duration_ms is not None:
            b["durations"].append(float(r.duration_ms))
        b["tool_calls"] += r.tool_calls or 0
        b["tool_errors"] += r.tool_errors or 0
        b["total_tokens"] += r.total_tokens or 0
        b["cost"] += float(r.cost) if r.cost is not None else 0.0

    for fr in fb_rows:
        if fr.created_at is None:
            continue
        b = _bucket(fr.created_at.date().isoformat())
        if fr.verdict == "up":
            b["up"] += 1
        elif fr.verdict == "down":
            b["down"] += 1

    series = []
    for day in sorted(buckets.keys()):
        b = buckets[day]
        durs = sorted(b["durations"])
        fb_total = b["up"] + b["down"]
        series.append({
            "date": day,
            "rounds": b["rounds"],
            "success_rate": (b["succeeded"] / b["rounds"]) if b["rounds"] else None,
            "duration_avg_ms": (sum(durs) / len(durs)) if durs else None,
            "duration_p90_ms": _pct(durs, 90),
            "tool_accuracy": (1 - b["tool_errors"] / b["tool_calls"]) if b["tool_calls"] else None,
            "total_tokens": b["total_tokens"],
            "cost": b["cost"],
            "satisfaction_rate": (b["up"] / fb_total) if fb_total else None,
            "thumbs_up": b["up"],
            "thumbs_down": b["down"],
        })
    return {"window_days": days, "series": series}


@router.get("/tools")
async def ux_tools(
    days: int = Query(30, ge=1, le=365),
    user_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """工具调用准确率排行（GROUP BY tool_name）。"""
    conds = [ToolCallMetrics.created_at >= _cutoff(days)]
    if user_id:
        conds.append(ToolCallMetrics.user_id == user_id)
    err_expr = func.sum(case((ToolCallMetrics.is_error.is_(True), 1), else_=0))
    async with async_session() as db:
        rows = (await db.execute(
            select(
                ToolCallMetrics.tool_name,
                func.count(ToolCallMetrics.id).label("calls"),
                err_expr.label("errors"),
            )
            .where(*conds)
            .group_by(ToolCallMetrics.tool_name)
            .order_by(func.count(ToolCallMetrics.id).desc())
            .limit(limit)
        )).all()
    return {
        "window_days": days,
        "tools": [
            {
                "tool_name": r.tool_name,
                "calls": int(r.calls or 0),
                "errors": int(r.errors or 0),
                "accuracy": (1 - int(r.errors or 0) / int(r.calls)) if r.calls else None,
            }
            for r in rows
        ],
    }


@router.get("/rounds")
async def ux_rounds(
    days: int = Query(7, ge=1, le=365),
    user_id: str | None = Query(None),
    session_id: str | None = Query(None),
    only_failed: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """回合明细行（最新在前），供下钻排障。"""
    conds = _round_filters(days, user_id, None)
    if session_id:
        conds.append(AgentRoundMetrics.session_id == session_id)
    if only_failed:
        conds.append(AgentRoundMetrics.succeeded.is_(False))
    async with async_session() as db:
        total = (await db.execute(
            select(func.count(AgentRoundMetrics.id)).where(*conds)
        )).scalar_one()
        rows = (await db.execute(
            select(AgentRoundMetrics).where(*conds)
            .order_by(AgentRoundMetrics.id.desc())
            .limit(limit).offset(offset)
        )).scalars().all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "rounds": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "session_id": r.session_id,
                "round_seq": r.round_seq,
                "message_id": r.message_id,
                "is_task": r.is_task,
                "succeeded": r.succeeded,
                "task_success": r.task_success,
                "errored": r.errored,
                "error_text": r.error_text,
                "duration_ms": r.duration_ms,
                "total_tokens": r.total_tokens,
                "cost": r.cost,
                "tool_calls": r.tool_calls,
                "tool_errors": r.tool_errors,
                "model_provider": r.model_provider,
                "model_id": r.model_id,
                "agent": r.agent,
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/feedback")
async def ux_feedback(
    days: int = Query(30, ge=1, le=365),
    verdict: str | None = Query(None, pattern="^(up|down)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """反馈明细（只读，含 context 快照），最新在前。"""
    conds = [MessageFeedback.created_at >= _cutoff(days)]
    if verdict:
        conds.append(MessageFeedback.verdict == verdict)
    async with async_session() as db:
        total = (await db.execute(
            select(func.count(MessageFeedback.id)).where(*conds)
        )).scalar_one()
        rows = (await db.execute(
            select(MessageFeedback).where(*conds)
            .order_by(MessageFeedback.id.desc())
            .limit(limit).offset(offset)
        )).scalars().all()

    def _codes(raw: str) -> list[str]:
        try:
            v = json.loads(raw) if raw else []
            return v if isinstance(v, list) else []
        except (TypeError, ValueError):
            return []

    def _ctx(raw: str):
        try:
            return json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return {}

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "feedback": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "session_id": r.session_id,
                "message_id": r.message_id,
                "user_message_id": r.user_message_id,
                "verdict": r.verdict,
                "reason_codes": _codes(r.reason_codes),
                "reason_text": r.reason_text,
                "turn_errored": r.turn_errored,
                "model_provider": r.model_provider,
                "model_id": r.model_id,
                "agent": r.agent,
                "context": _ctx(r.context),
                "context_truncated": r.context_truncated,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


class BackfillIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1, max_length=255)


@router.post("/backfill")
async def ux_backfill(body: BackfillIn):
    """按需回补某用户某会话的历史指标（容器须运行中）。幂等写入。"""
    async with async_session() as db:
        record = (await db.execute(
            select(AgentContainer).where(AgentContainer.user_id == body.user_id)
        )).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="No container record for this user")
    if record.status != "running":
        raise HTTPException(status_code=409, detail=f"Container not running (status={record.status})")

    base_url = container_manager.get_container_url(body.user_id)
    auth = ("opencode", decrypt_password_compat(record.password_enc))
    try:
        result = await backfill_session(body.user_id, body.session_id, base_url, auth)
    except Exception as e:  # noqa: BLE001
        logger.warning("UX backfill failed for %s/%s: %s", body.user_id, body.session_id, e)
        raise HTTPException(status_code=502, detail=f"Backfill failed: {e}")
    return {"ok": True, **result}
