"""管理员 UX 指标看板路由（任务二）。

被锁死的性质：**admin 门禁**（非 admin 一律 403）、四层指标口径（L1 结果 /
L2 效率 / L3 过程 / L4 满意度）、时间窗与 user 维度过滤、趋势按天分桶、工具
准确率走 SQL GROUP BY、明细下钻的分页与 only_failed、回补的容器前置校验
（无记录 404 / 非 running 409 / 上游异常 502）。

采集器本身的结算口径在 test_metrics_collector.py 里，这里只验查询与聚合。
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AgentContainer, AgentRoundMetrics, MessageFeedback, ToolCallMetrics
from app.routers import admin_ux as ux

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolated(db_factory, monkeypatch):
    monkeypatch.setattr(ux, "async_session", db_factory)


@pytest.fixture
def admin(app_client_factory):
    return app_client_factory([ux.router], user_id="admin1", role="admin")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def seed_round(db_factory, **kw) -> AgentRoundMetrics:
    """种一条回合明细行（message_id 自动唯一）。"""
    defaults = dict(
        user_id="u1", session_id="ses1", round_seq=0,
        message_id=f"m{next(_seq)}",
        is_task=False, succeeded=True, task_success=None, errored=False, error_text=None,
        duration_ms=1000, total_tokens=100, cost=0.01,
        tool_calls=0, tool_errors=0,
        model_provider="anthropic", model_id="claude-sonnet", agent="build",
        source="tap", created_at=_now(),
    )
    defaults.update(kw)
    row = AgentRoundMetrics(**defaults)
    async with db_factory() as db:
        db.add(row)
        await db.commit()
    return row


async def seed_feedback(db_factory, **kw) -> MessageFeedback:
    defaults = dict(
        user_id="u1", session_id="ses1", message_id=f"f{next(_seq)}",
        verdict="up", reason_codes="[]", reason_text=None, turn_errored=False,
        context="{}", context_truncated=False, created_at=_now(),
    )
    defaults.update(kw)
    row = MessageFeedback(**defaults)
    async with db_factory() as db:
        db.add(row)
        await db.commit()
    return row


async def seed_tool(db_factory, **kw) -> ToolCallMetrics:
    defaults = dict(
        user_id="u1", session_id="ses1", round_seq=0,
        tool_name="read", status="completed", is_error=False,
        error_text=None, source="tap", created_at=_now(),
    )
    defaults.update(kw)
    row = ToolCallMetrics(**defaults)
    async with db_factory() as db:
        db.add(row)
        await db.commit()
    return row


async def seed_container(db_factory, *, user_id="u1", status="running") -> AgentContainer:
    row = AgentContainer(
        user_id=user_id, container_name=f"agent-{user_id}", status=status,
        password_enc="pw", image="opencode:latest",
        workspace_volume=f"ws-{user_id}", data_volume=f"data-{user_id}",
    )
    async with db_factory() as db:
        db.add(row)
        await db.commit()
    return row


# ------------------------------------------------------------------ 门禁


async def test_every_endpoint_requires_admin(app_client_factory):
    """看板是 admin 专属 —— 普通用户即使带着合法身份也必须 403。"""
    user_client = app_client_factory([ux.router], user_id="u1", role="user")
    async with user_client as c:
        for path in ("/overview", "/trends", "/tools", "/rounds", "/feedback"):
            r = await c.get(f"/api/admin/ux{path}")
            assert r.status_code == 403, path
            assert r.json()["detail"] == "Admin privileges required"
        assert (await c.post("/api/admin/ux/backfill",
                             json={"user_id": "u1", "session_id": "s"})).status_code == 403


# ------------------------------------------------------------------ L1-L4 汇总


async def test_overview_aggregates_all_four_layers(admin, db_factory):
    await seed_round(db_factory, succeeded=True, is_task=True, task_success=True,
                     duration_ms=1000, total_tokens=100, cost=0.01, tool_calls=2, tool_errors=1)
    await seed_round(db_factory, round_seq=1, succeeded=True, is_task=True, task_success=False,
                     duration_ms=3000, total_tokens=300, cost=0.03, tool_calls=1, tool_errors=0)
    await seed_round(db_factory, round_seq=2, succeeded=False, errored=True, error_text="boom",
                     duration_ms=None, total_tokens=50, cost=None, tool_calls=0, tool_errors=0)
    await seed_round(db_factory, round_seq=3, succeeded=True, is_task=False,
                     duration_ms=2000, total_tokens=200, cost=0.02, tool_calls=3, tool_errors=0)

    await seed_feedback(db_factory, verdict="up")
    await seed_feedback(db_factory, verdict="up")
    await seed_feedback(db_factory, verdict="down", reason_codes='["wrong_answer", "other"]')
    await seed_feedback(db_factory, verdict="down", reason_codes='["wrong_answer"]')

    async with admin as c:
        r = await c.get("/api/admin/ux/overview")
    assert r.status_code == 200
    d = r.json()

    assert d["window_days"] == 30
    assert d["filters"] == {"user_id": None, "model_provider": None}

    # L1 结果层：回合成功率 3/4、错误率 1/4、任务轨只看 is_task 的两条
    assert d["l1_outcome"] == {
        "rounds_total": 4, "round_success_rate": 0.75, "error_rate": 0.25,
        "task_rounds": 2, "task_success_rate": 0.5,
    }

    # L2：duration_ms 为 None 的错误回合不进分位数样本（[1000,2000,3000]）
    l2 = d["l2_efficiency"]
    assert l2["duration_avg_ms"] == 2000.0
    assert l2["duration_p50_ms"] == 2000.0
    assert l2["duration_p90_ms"] == pytest.approx(2800.0)
    assert l2["duration_p99_ms"] == pytest.approx(2980.0)
    assert l2["total_cost"] == pytest.approx(0.06)
    assert l2["avg_cost"] == pytest.approx(0.015)

    # L3：工具准确率 = 1 - errors/calls；token 效率按成功回合摊
    assert d["l3_process"] == {
        "tool_calls": 6, "tool_errors": 1, "tool_accuracy": pytest.approx(5 / 6),
        "total_tokens": 650, "avg_tokens_per_round": 162.5,
        "tokens_per_success": pytest.approx(650 / 3),
    }

    # L4：满意度 + 点踩原因码多码计数
    assert d["l4_satisfaction"] == {
        "thumbs_up": 2, "thumbs_down": 2, "total": 4, "satisfaction_rate": 0.5,
        "down_reasons": {"wrong_answer": 2, "other": 1},
    }


async def test_overview_on_empty_window_returns_none_instead_of_zero_division(admin):
    """没有任何数据时比率必须是 null（前端渲染「暂无数据」），不能是 0 也不能 500。"""
    async with admin as c:
        d = (await c.get("/api/admin/ux/overview")).json()

    assert d["l1_outcome"] == {
        "rounds_total": 0, "round_success_rate": None, "error_rate": None,
        "task_rounds": 0, "task_success_rate": None,
    }
    assert d["l2_efficiency"]["duration_p50_ms"] is None
    assert d["l2_efficiency"]["avg_cost"] is None
    assert d["l3_process"]["tool_accuracy"] is None
    assert d["l3_process"]["tokens_per_success"] is None
    assert d["l4_satisfaction"] == {
        "thumbs_up": 0, "thumbs_down": 0, "total": 0,
        "satisfaction_rate": None, "down_reasons": {},
    }


async def test_overview_filters_by_user_model_and_window(admin, db_factory):
    await seed_round(db_factory, user_id="u1", total_tokens=100)                 # 命中
    await seed_round(db_factory, user_id="u2", total_tokens=999)                 # 别人
    await seed_round(db_factory, user_id="u1", model_provider="openai", total_tokens=888)
    await seed_round(db_factory, user_id="u1", total_tokens=777,
                     created_at=_now() - timedelta(days=40))                      # 窗外
    await seed_feedback(db_factory, user_id="u1", verdict="up")
    await seed_feedback(db_factory, user_id="u2", verdict="up")
    await seed_feedback(db_factory, user_id="u1", verdict="up",
                        created_at=_now() - timedelta(days=40))                   # 窗外

    async with admin as c:
        d = (await c.get("/api/admin/ux/overview",
                         params={"user_id": "u1", "model_provider": "anthropic", "days": 30})).json()
        assert d["filters"] == {"user_id": "u1", "model_provider": "anthropic"}
        assert d["l1_outcome"]["rounds_total"] == 1
        assert d["l3_process"]["total_tokens"] == 100
        # 反馈只按 user_id + 时间窗过滤（模型维度不在反馈表上）
        assert d["l4_satisfaction"]["thumbs_up"] == 1

        assert (await c.get("/api/admin/ux/overview", params={"days": 0})).status_code == 422
        assert (await c.get("/api/admin/ux/overview", params={"days": 9999})).status_code == 422


# ------------------------------------------------------------------ 趋势


async def test_trends_buckets_by_day_and_keeps_days_sorted(admin, db_factory):
    today, yesterday = _now(), _now() - timedelta(days=1)
    await seed_round(db_factory, succeeded=True, duration_ms=1000, tool_calls=2, tool_errors=0,
                     created_at=yesterday)
    await seed_round(db_factory, succeeded=False, errored=True, duration_ms=500,
                     tool_calls=2, tool_errors=2, created_at=today)
    await seed_round(db_factory, succeeded=True, duration_ms=3000, created_at=today)
    await seed_feedback(db_factory, verdict="up", created_at=today)
    await seed_feedback(db_factory, verdict="down", created_at=today)

    async with admin as c:
        d = (await c.get("/api/admin/ux/trends")).json()

    series = d["series"]
    assert [p["date"] for p in series] == [yesterday.date().isoformat(), today.date().isoformat()]

    y, t = series
    assert (y["rounds"], y["success_rate"], y["duration_avg_ms"]) == (1, 1.0, 1000.0)
    assert (y["thumbs_up"], y["thumbs_down"], y["satisfaction_rate"]) == (0, 0, None)
    assert y["tool_accuracy"] == 1.0

    assert t["rounds"] == 2 and t["success_rate"] == 0.5
    assert t["duration_avg_ms"] == 1750.0          # (500 + 3000) / 2
    assert t["tool_accuracy"] == 0.0               # 2 次调用全错
    assert t["satisfaction_rate"] == 0.5
    assert (t["thumbs_up"], t["thumbs_down"]) == (1, 1)


async def test_trends_is_empty_when_no_data(admin):
    async with admin as c:
        assert (await c.get("/api/admin/ux/trends")).json() == {"window_days": 30, "series": []}


# ------------------------------------------------------------------ 工具准确率


async def test_tools_ranks_by_call_count_with_accuracy(admin, db_factory):
    for _ in range(3):
        await seed_tool(db_factory, tool_name="read", is_error=False)
    await seed_tool(db_factory, tool_name="read", is_error=True, status="error",
                    error_text="ENOENT")
    for _ in range(2):                       # 调用数各不相同，避开 GROUP BY 并列时的排序歧义
        await seed_tool(db_factory, tool_name="bash", is_error=False)
    await seed_tool(db_factory, tool_name="write", user_id="u2", is_error=True, status="error")

    async with admin as c:
        d = (await c.get("/api/admin/ux/tools")).json()
        only_u1 = (await c.get("/api/admin/ux/tools", params={"user_id": "u1"})).json()

    assert d["tools"] == [
        {"tool_name": "read", "calls": 4, "errors": 1, "accuracy": 0.75},
        {"tool_name": "bash", "calls": 2, "errors": 0, "accuracy": 1.0},
        {"tool_name": "write", "calls": 1, "errors": 1, "accuracy": 0.0},
    ]
    assert {t["tool_name"] for t in only_u1["tools"]} == {"read", "bash"}


async def test_tools_respects_limit(admin, db_factory):
    for name in ("a", "b", "c"):
        await seed_tool(db_factory, tool_name=name)
    async with admin as c:
        d = (await c.get("/api/admin/ux/tools", params={"limit": 2})).json()
        assert (await c.get("/api/admin/ux/tools", params={"limit": 0})).status_code == 422
    assert len(d["tools"]) == 2


# ------------------------------------------------------------------ 回合明细下钻


async def test_rounds_pagination_ordering_and_only_failed(admin, db_factory):
    r1 = await seed_round(db_factory, session_id="ses1", succeeded=True)
    r2 = await seed_round(db_factory, session_id="ses1", round_seq=1, succeeded=False,
                          errored=True, error_text="boom")
    r3 = await seed_round(db_factory, session_id="ses2", succeeded=False)
    await seed_round(db_factory, session_id="ses2", succeeded=True,
                     created_at=_now() - timedelta(days=40))

    async with admin as c:
        page = (await c.get("/api/admin/ux/rounds", params={"days": 30, "limit": 2})).json()
        second = (await c.get("/api/admin/ux/rounds",
                              params={"days": 30, "limit": 2, "offset": 2})).json()
        failed = (await c.get("/api/admin/ux/rounds",
                              params={"days": 30, "only_failed": "true"})).json()
        one_session = (await c.get("/api/admin/ux/rounds",
                                    params={"days": 30, "session_id": "ses1"})).json()

    assert page["total"] == 3                      # 窗外那条不计入总数
    assert [r["id"] for r in page["rounds"]] == [r3.id, r2.id]     # 最新在前
    assert [r["id"] for r in second["rounds"]] == [r1.id]
    assert page["limit"] == 2 and page["offset"] == 0
    assert second["offset"] == 2 and second["total"] == 3

    assert failed["total"] == 2
    assert all(r["succeeded"] is False for r in failed["rounds"])
    assert {r["session_id"] for r in one_session["rounds"]} == {"ses1"}
    assert one_session["total"] == 2

    sample = page["rounds"][0]
    assert {"id", "user_id", "session_id", "round_seq", "message_id", "is_task", "succeeded",
            "task_success", "errored", "error_text", "duration_ms", "total_tokens", "cost",
            "tool_calls", "tool_errors", "model_provider", "model_id", "agent", "source",
            "created_at"} <= set(sample)


# ------------------------------------------------------------------ 反馈明细


async def test_feedback_detail_deserializes_codes_and_context(admin, db_factory):
    await seed_feedback(db_factory, verdict="down", reason_codes='["too_verbose", "other"]',
                        reason_text="太啰嗦了", turn_errored=True,
                        context=json.dumps({"user_text": "改一下", "assistant_text": "好的"},
                                           ensure_ascii=False),
                        model_provider="anthropic", model_id="claude-sonnet", agent="build")
    # 脏数据（截断的 context、非法 reason_codes）不能让端点炸，降级成 {} / []
    await seed_feedback(db_factory, verdict="down", reason_codes="not-json",
                        context="{被截断的 JSON", context_truncated=True)

    async with admin as c:
        d = (await c.get("/api/admin/ux/feedback", params={"verdict": "down"})).json()
        bad = await c.get("/api/admin/ux/feedback", params={"verdict": "meh"})

    assert bad.status_code == 422
    assert d["total"] == 2 and len(d["feedback"]) == 2
    newest = d["feedback"][0]                      # id desc → 脏数据那条
    assert newest["context"] == {} and newest["reason_codes"] == []
    assert newest["context_truncated"] is True

    good = d["feedback"][1]
    assert good["reason_codes"] == ["too_verbose", "other"]
    assert good["reason_text"] == "太啰嗦了"
    assert good["turn_errored"] is True
    assert good["context"] == {"user_text": "改一下", "assistant_text": "好的"}
    assert good["user_id"] == "u1" and good["session_id"] == "ses1"
    assert good["created_at"] is not None


# ------------------------------------------------------------------ 回补


async def test_backfill_rejects_unknown_user(admin):
    async with admin as c:
        r = await c.post("/api/admin/ux/backfill", json={"user_id": "ghost", "session_id": "s"})
    assert r.status_code == 404
    assert r.json()["detail"] == "No container record for this user"


async def test_backfill_requires_running_container(admin, db_factory):
    await seed_container(db_factory, user_id="u1", status="stopped")
    async with admin as c:
        r = await c.post("/api/admin/ux/backfill", json={"user_id": "u1", "session_id": "s"})
    assert r.status_code == 409
    assert "status=stopped" in r.json()["detail"]


async def test_backfill_forwards_container_credentials_and_returns_envelope(
    admin, db_factory, monkeypatch
):
    await seed_container(db_factory, user_id="u1", status="running")
    seen = {}

    async def fake_backfill(user_id, session_id, base_url, auth):
        seen.update(user_id=user_id, session_id=session_id, base_url=base_url, auth=auth)
        return {"session_id": session_id, "records": 5, "inserted": 2}

    monkeypatch.setattr(ux, "backfill_session", fake_backfill)
    async with admin as c:
        r = await c.post("/api/admin/ux/backfill", json={"user_id": "u1", "session_id": "ses1"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "session_id": "ses1", "records": 5, "inserted": 2}
    assert seen["user_id"] == "u1" and seen["session_id"] == "ses1"
    assert seen["base_url"].startswith("http://agent-u1:")
    assert seen["auth"][0] == "opencode" and seen["auth"][1] == "pw"


async def test_backfill_maps_upstream_failure_to_502(admin, db_factory, monkeypatch):
    await seed_container(db_factory, user_id="u1", status="running")

    async def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ux, "backfill_session", boom)
    async with admin as c:
        r = await c.post("/api/admin/ux/backfill", json={"user_id": "u1", "session_id": "ses1"})
    assert r.status_code == 502
    assert "connection refused" in r.json()["detail"]


async def test_backfill_validates_payload(admin):
    async with admin as c:
        assert (await c.post("/api/admin/ux/backfill",
                             json={"user_id": "", "session_id": "s"})).status_code == 422
        assert (await c.post("/api/admin/ux/backfill", json={"user_id": "u1"})).status_code == 422
