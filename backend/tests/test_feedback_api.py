"""点赞/点踩反馈路由契约（任务一）。

被锁死的四条性质：投票**不可取消**（重提幂等返回 already=true 且不改写既有
verdict）、原因码走**白名单**（非法码静默丢弃）、服务端**二次截断**（不信任
前端的长度约束）、提交有**轻限流**（防刷，不影响正常人工点击）。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models import MessageFeedback
from app.routers import feedback as fb


@pytest.fixture(autouse=True)
def _isolated(db_factory, monkeypatch):
    monkeypatch.setattr(fb, "async_session", db_factory)
    fb._feedback_limiter._hits.clear()   # 模块级单例，跨测试会串味


@pytest.fixture
def client(app_client_factory):
    return app_client_factory([fb.router], user_id="u1", username="alice")


def body(**over) -> dict:
    payload = {
        "session_id": "ses1",
        "message_id": "ma1",
        "verdict": "up",
        "context": {"user_text": "帮我改一下", "assistant_text": "好的"},
    }
    payload.update(over)
    return payload


async def rows(db_factory) -> list[MessageFeedback]:
    async with db_factory() as db:
        return list((await db.execute(select(MessageFeedback).order_by(MessageFeedback.id))).scalars().all())


# ------------------------------------------------------------------ 提交与幂等


async def test_thumbs_up_is_recorded_with_context_snapshot(client, db_factory):
    async with client as c:
        r = await c.post("/api/feedback", json=body())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": False, "verdict": "up", "message_id": "ma1"}

    (row,) = await rows(db_factory)
    assert (row.user_id, row.session_id, row.verdict) == ("u1", "ses1", "up")
    assert json.loads(row.context) == {"user_text": "帮我改一下", "assistant_text": "好的"}
    assert row.context_truncated is False
    assert row.reason_codes == "[]" and row.reason_text is None


async def test_vote_cannot_be_changed_or_duplicated(client, db_factory):
    async with client as c:
        await c.post("/api/feedback", json=body(verdict="up"))
        r = await c.post("/api/feedback", json=body(verdict="down", reason_codes=["wrong_answer"]))
    assert r.json() == {"ok": True, "already": True, "verdict": "up", "message_id": "ma1"}

    (row,) = await rows(db_factory)     # 仍只有一行，且原始 verdict/原因未被改写
    assert row.verdict == "up" and row.reason_codes == "[]"


async def test_same_message_id_from_another_user_is_a_separate_vote(app_client_factory, db_factory):
    """自然键是 (user_id, message_id) —— 不同用户评价同一条回复互不覆盖。"""
    async with app_client_factory([fb.router], user_id="u1") as c1:
        await c1.post("/api/feedback", json=body(verdict="up"))
    async with app_client_factory([fb.router], user_id="u2") as c2:
        r = await c2.post("/api/feedback", json=body(verdict="down", reason_codes=["too_verbose"]))

    assert r.json()["already"] is False
    stored = await rows(db_factory)
    assert {(r_.user_id, r_.verdict) for r_ in stored} == {("u1", "up"), ("u2", "down")}


async def test_invalid_verdict_is_rejected(client):
    async with client as c:
        assert (await c.post("/api/feedback", json=body(verdict="maybe"))).status_code == 422
        assert (await c.post("/api/feedback", json=body(message_id=""))).status_code == 422


# ------------------------------------------------------------------ 原因码与截断


async def test_down_reasons_are_whitelisted_and_deduped(client, db_factory):
    async with client as c:
        await c.post("/api/feedback", json=body(
            verdict="down",
            reason_codes=["wrong_answer", "not_a_code", "wrong_answer", "other"],
            reason_text="  忽略了只改前端的约束  ",
        ))
    (row,) = await rows(db_factory)
    assert json.loads(row.reason_codes) == ["wrong_answer", "other"]
    assert row.reason_text == "忽略了只改前端的约束"


async def test_up_ignores_reasons(client, db_factory):
    async with client as c:
        await c.post("/api/feedback", json=body(verdict="up", reason_codes=["wrong_answer"], reason_text="x"))
    (row,) = await rows(db_factory)
    assert row.reason_codes == "[]" and row.reason_text is None


async def test_reason_text_is_truncated_server_side(client, db_factory):
    async with client as c:
        await c.post("/api/feedback", json=body(verdict="down", reason_codes=["other"], reason_text="长" * 900))
    (row,) = await rows(db_factory)
    assert len(row.reason_text) == 500


async def test_oversized_context_is_truncated_and_flagged(client, db_factory):
    async with client as c:
        r = await c.post("/api/feedback", json=body(context={"assistant_text": "x" * 80_000}))
    assert r.status_code == 200

    (row,) = await rows(db_factory)
    assert row.context_truncated is True
    assert len(row.context) == 60_000     # 落库串不再是合法 JSON，明细端点会返回 {}


async def test_unserializable_context_degrades_to_empty_object(client, db_factory):
    async with client as c:
        await c.post("/api/feedback", json=body(context={"ok": "fine"}))
    (row,) = await rows(db_factory)
    assert row.context == '{"ok": "fine"}' and row.context_truncated is False


# ------------------------------------------------------------------ 限流


async def test_rate_limit_kicks_in_after_30_submissions_per_minute(client):
    async with client as c:
        for i in range(30):
            assert (await c.post("/api/feedback", json=body(message_id=f"m{i}"))).status_code == 200
        r = await c.post("/api/feedback", json=body(message_id="m30"))
    assert r.status_code == 429
    assert "retry in" in r.json()["detail"]


# ------------------------------------------------------------------ 回填


async def test_session_feedback_returns_only_own_minimal_votes(app_client_factory, db_factory):
    async with app_client_factory([fb.router], user_id="u1") as c:
        await c.post("/api/feedback", json=body(message_id="ma1", verdict="up"))
        await c.post("/api/feedback", json=body(
            message_id="ma2", verdict="down", reason_codes=["tool_failure"],
            context={"assistant_text": "秘密" * 100},
        ))
        await c.post("/api/feedback", json=body(session_id="ses2", message_id="ma3"))
        mine = (await c.get("/api/feedback/session/ses1")).json()

    assert mine["session_id"] == "ses1"
    assert {f["message_id"]: f["verdict"] for f in mine["feedback"]} == {"ma1": "up", "ma2": "down"}
    assert "秘密" not in json.dumps(mine, ensure_ascii=False)   # 回填不带 context 快照
    assert set(mine["feedback"][0]) == {"message_id", "verdict", "created_at"}

    # 别人的会话看不到我的投票
    async with app_client_factory([fb.router], user_id="u2") as other:
        empty = (await other.get("/api/feedback/session/ses1")).json()
    assert empty["feedback"] == []

    assert len(await rows(db_factory)) == 3


async def test_session_feedback_limit_is_bounded(client):
    async with client as c:
        assert (await c.get("/api/feedback/session/ses1", params={"limit": 0})).status_code == 422
        assert (await c.get("/api/feedback/session/ses1", params={"limit": 5000})).status_code == 422
        assert (await c.get("/api/feedback/session/ses1")).status_code == 200


async def test_duplicate_race_falls_back_to_already(db_factory, monkeypatch, app_client_factory):
    """并发撞唯一索引（IntegrityError）降级为 already=true，不吐 500。

    走的是「查无既有行 → insert → commit 抛错」这条路径，所以 message_id 必须
    是库里还不存在的；commit 用代理类拦掉（直接给 AsyncSession 实例打补丁不可靠）。
    """
    from sqlalchemy.exc import IntegrityError

    class RacingSession:
        """委托真实 session，只把 commit 换成抛 IntegrityError（模拟自然键撞车）。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):          # add / execute / rollback …
            return getattr(self._inner, name)

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

        async def commit(self):
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(fb, "async_session", lambda: RacingSession(db_factory()))
    client = app_client_factory([fb.router], user_id="u1")
    async with client as c:
        r = await c.post("/api/feedback", json=body(message_id="ma-race", verdict="down"))

    assert r.status_code == 200
    assert r.json() == {"ok": True, "already": True, "verdict": "down", "message_id": "ma-race"}
    assert len(await rows(db_factory)) == 0    # insert 已回滚，没留下半行脏数据

