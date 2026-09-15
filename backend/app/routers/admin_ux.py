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
from ..models import AgentContainer, AgentRoundMetrics, LLMProxyMetrics, MessageFeedback, RequestLog, ToolCallMetrics, User
from ..services.container_manager import container_manager
from ..services.metrics_collector import backfill_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/ux", tags=["admin-ux"], dependencies=[Depends(require_admin)])


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# 时间维度粒度 → 默认统计窗口（天）。粒度自带默认范围，切换粒度即切换窗口。
_GRANULARITY_WINDOW = {"day": 30, "week": 84, "month": 365, "year": 1825}


def _resolve_granularity(granularity: str) -> str:
    return granularity if granularity in _GRANULARITY_WINDOW else "day"


def _resolve_window(granularity: str, days: int | None) -> int:
    """显式 days 优先（向后兼容）；否则用粒度自带的默认范围。"""
    if days is not None:
        return max(1, min(days, 3650))
    return _GRANULARITY_WINDOW.get(granularity, 30)


def _bucket_key(dt: datetime, granularity: str) -> str:
    """按粒度生成分桶键（Python 侧，跨 SQLite/Postgres 方言安全）。

    day   -> 2026-09-15    week  -> 2026-W38（ISO 周）
    month -> 2026-09       year  -> 2026
    键的字典序即时间序，便于直接 sorted()。
    """
    d = dt.date() if isinstance(dt, datetime) else dt
    if granularity == "week":
        iso = d.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    if granularity == "month":
        return f"{d.year:04d}-{d.month:02d}"
    if granularity == "year":
        return f"{d.year:04d}"
    return d.isoformat()


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
    granularity: str = Query("day"),
    days: int | None = Query(None, ge=1, le=3650),
    user_id: str | None = Query(None),
    model_provider: str | None = Query(None),
):
    """四层核心指标 + 用户视角汇总（粒度驱动的窗口内）。"""
    granularity = _resolve_granularity(granularity)
    wdays = _resolve_window(granularity, days)
    conds = _round_filters(wdays, user_id, model_provider)
    cols = [
        AgentRoundMetrics.succeeded,
        AgentRoundMetrics.is_task,
        AgentRoundMetrics.task_success,
        AgentRoundMetrics.errored,
        AgentRoundMetrics.error_name,
        AgentRoundMetrics.duration_ms,
        AgentRoundMetrics.total_tokens,
        AgentRoundMetrics.input_tokens,
        AgentRoundMetrics.output_tokens,
        AgentRoundMetrics.reasoning_tokens,
        AgentRoundMetrics.cache_read_tokens,
        AgentRoundMetrics.cache_write_tokens,
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

    # 错误分类下钻（A1）：按 error_name 计数，None 归为 "unknown"。
    error_breakdown: dict[str, int] = {}
    for r in rows:
        if r.errored:
            key = r.error_name or "unknown"
            error_breakdown[key] = error_breakdown.get(key, 0) + 1

    # Token 拆分（A3）：分项求和，缺失项跳过。
    def _tok_sum(attr: str) -> int:
        return sum(getattr(r, attr) for r in rows if getattr(r, attr) is not None)

    token_split = {
        "input": _tok_sum("input_tokens"),
        "output": _tok_sum("output_tokens"),
        "reasoning": _tok_sum("reasoning_tokens"),
        "cache_read": _tok_sum("cache_read_tokens"),
        "cache_write": _tok_sum("cache_write_tokens"),
    }

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
            "error_breakdown": error_breakdown,
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
            "token_split": token_split,
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


@router.get("/tool-calls")
async def ux_tool_calls(
    days: int = Query(7, ge=1, le=365),
    user_id: str | None = Query(None),
    session_id: str | None = Query(None),
    only_failed: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """个体工具调用记录（最新在前），供 L3 过程与轨迹下钻排障。"""
    conds = [ToolCallMetrics.created_at >= _cutoff(days)]
    if user_id:
        conds.append(ToolCallMetrics.user_id == user_id)
    if session_id:
        conds.append(ToolCallMetrics.session_id == session_id)
    if only_failed:
        conds.append(ToolCallMetrics.is_error.is_(True))
    async with async_session() as db:
        total = (await db.execute(
            select(func.count(ToolCallMetrics.id)).where(*conds)
        )).scalar_one()
        rows = (await db.execute(
            select(ToolCallMetrics).where(*conds)
            .order_by(ToolCallMetrics.id.desc())
            .limit(limit).offset(offset)
        )).scalars().all()
        # 批量查用户名/工号（避免 N+1）。
        uids = {r.user_id for r in rows if r.user_id}
        users_map: dict[str, tuple[str | None, str | None]] = {}
        if uids:
            urows = (await db.execute(
                select(User.id, User.username, User.uid).where(User.id.in_(uids))
            )).all()
            users_map = {u.id: (u.username, u.uid) for u in urows}
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "tool_calls": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_name": users_map.get(r.user_id, (None, None))[0],
                "user_uid": users_map.get(r.user_id, (None, None))[1],
                "session_id": r.session_id,
                "round_seq": r.round_seq,
                "tool_name": r.tool_name,
                "status": r.status,
                "is_error": r.is_error,
                "error_text": r.error_text,
                "duration_ms": r.duration_ms,
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/llm")
async def ux_llm(
    days: int = Query(7, ge=1, le=365),
    user_id: str | None = Query(None),
    provider_id: str | None = Query(None),
):
    """LLM 代理上游指标（B1/B2）：按 provider 聚合状态码分布、TTFT/耗时分位、错误率。"""
    conds = [LLMProxyMetrics.created_at >= _cutoff(days)]
    if user_id:
        conds.append(LLMProxyMetrics.user_id == user_id)
    if provider_id:
        conds.append(LLMProxyMetrics.provider_id == provider_id)
    cols = [
        LLMProxyMetrics.provider_id,
        LLMProxyMetrics.status_code,
        LLMProxyMetrics.ttft_ms,
        LLMProxyMetrics.duration_ms,
        LLMProxyMetrics.is_sse,
        LLMProxyMetrics.upstream_error,
    ]
    async with async_session() as db:
        rows = (await db.execute(select(*cols).where(*conds))).all()

    # 按 provider 聚合。
    by_prov: dict[str, dict] = {}

    def _bucket(pid: str) -> dict:
        b = by_prov.get(pid)
        if b is None:
            b = {
                "calls": 0, "errors": 0, "status_counts": {},
                "ttfts": [], "durations": [], "sse_calls": 0,
            }
            by_prov[pid] = b
        return b

    for r in rows:
        b = _bucket(r.provider_id)
        b["calls"] += 1
        if r.upstream_error:
            b["errors"] += 1
        sc = str(r.status_code) if r.status_code is not None else "none"
        b["status_counts"][sc] = b["status_counts"].get(sc, 0) + 1
        if r.is_sse:
            b["sse_calls"] += 1
        if r.ttft_ms is not None:
            b["ttfts"].append(float(r.ttft_ms))
        if r.duration_ms is not None:
            b["durations"].append(float(r.duration_ms))

    providers = []
    total_calls = 0
    total_errors = 0
    for pid in sorted(by_prov.keys()):
        b = by_prov[pid]
        ttfts = sorted(b["ttfts"])
        durs = sorted(b["durations"])
        total_calls += b["calls"]
        total_errors += b["errors"]
        providers.append({
            "provider_id": pid,
            "calls": b["calls"],
            "errors": b["errors"],
            "error_rate": (b["errors"] / b["calls"]) if b["calls"] else None,
            "sse_calls": b["sse_calls"],
            "status_counts": b["status_counts"],
            "ttft_avg_ms": (sum(ttfts) / len(ttfts)) if ttfts else None,
            "ttft_p50_ms": _pct(ttfts, 50),
            "ttft_p90_ms": _pct(ttfts, 90),
            "ttft_p99_ms": _pct(ttfts, 99),
            "duration_avg_ms": (sum(durs) / len(durs)) if durs else None,
            "duration_p50_ms": _pct(durs, 50),
            "duration_p90_ms": _pct(durs, 90),
            "duration_p99_ms": _pct(durs, 99),
        })

    return {
        "window_days": days,
        "filters": {"user_id": user_id, "provider_id": provider_id},
        "totals": {
            "calls": total_calls,
            "errors": total_errors,
            "error_rate": (total_errors / total_calls) if total_calls else None,
        },
        "providers": providers,
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
        # 批量查用户名/工号（避免 N+1）。
        uids = {r.user_id for r in rows if r.user_id}
        users_map: dict[str, tuple[str | None, str | None]] = {}
        if uids:
            urows = (await db.execute(
                select(User.id, User.username, User.uid).where(User.id.in_(uids))
            )).all()
            users_map = {u.id: (u.username, u.uid) for u in urows}
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "rounds": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_name": users_map.get(r.user_id, (None, None))[0],
                "user_uid": users_map.get(r.user_id, (None, None))[1],
                "session_id": r.session_id,
                "round_seq": r.round_seq,
                "message_id": r.message_id,
                "is_task": r.is_task,
                "succeeded": r.succeeded,
                "task_success": r.task_success,
                "errored": r.errored,
                "error_text": r.error_text,
                "error_name": r.error_name,
                "error_status_code": r.error_status_code,
                "duration_ms": r.duration_ms,
                "total_tokens": r.total_tokens,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "reasoning_tokens": r.reasoning_tokens,
                "cache_read_tokens": r.cache_read_tokens,
                "cache_write_tokens": r.cache_write_tokens,
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
