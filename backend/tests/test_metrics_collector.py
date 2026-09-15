"""UX 指标采集：RoundAggregator 结算口径 + 幂等持久化 + tap/回补双通路。

这里的断言就是「什么算一个成功回合」的规格：成功 = 有 assistant 消息、未报错、
``time.completed`` 存在；工具 part 全量替换（同一 part 多次 updated 只计一次）；
任务轨信号只来自 ``todo.updated``（故回补行 is_task=False）。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from app.models import AgentRoundMetrics, LLMProxyMetrics, ToolCallMetrics
from app.services import metrics_collector as mc
from app.services.metrics_collector import (
    RoundAggregator,
    _err_fields,
    _sum_tokens,
    _token_split,
    backfill_session,
    metrics_collector,
    record_llm_call,
)


@pytest.fixture(autouse=True)
def _isolated(db_factory, monkeypatch):
    """落库指向临时库，并清空全局单例的内存态（否则测试之间会串回合）。"""
    monkeypatch.setattr(mc, "async_session", db_factory)
    metrics_collector._aggregators.clear()
    metrics_collector._mid_to_sid.clear()
    metrics_collector._writes_since_prune = 0


def _user(mid: str = "mu1", created: int = 1_000) -> tuple[str, dict]:
    return ("message.updated", {"info": {"id": mid, "role": "user", "time": {"created": created}}})


def _assistant(mid: str = "ma1", created: int = 1_500, completed: int | None = 4_000, **extra) -> tuple[str, dict]:
    time = {"created": created}
    if completed is not None:
        time["completed"] = completed
    return ("message.updated", {"info": {"id": mid, "role": "assistant", "time": time, **extra}})


def _tool(pid: str, name: str = "bash", status: str = "completed", error: str | None = None) -> tuple[str, dict]:
    state: dict = {"status": status}
    if error:
        state["error"] = {"message": error}
    return ("message.part.updated", {"part": {"id": pid, "type": "tool", "tool": name, "state": state}})


def _feed(agg: RoundAggregator, *events: tuple[str, dict]):
    for type_, data in events:
        agg.consume(type_, data)


# --------------------------------------------------------------- 结算口径


def test_round_settles_on_idle_with_full_dimensions():
    agg = RoundAggregator("u1", "ses1")
    _feed(
        agg,
        _user(),
        _assistant(
            agent="build",
            model={"providerID": "anthropic", "modelID": "claude-x"},
            cost=0.021,
            tokens={"input": 10, "output": 5, "cache": {"read": 3, "write": 1}},
        ),
        _tool("p1", "bash", "completed"),
        _tool("p2", "read", "error", error="ENOENT"),
    )
    settled = agg.consume("session.idle", {"sessionID": "ses1"})

    assert settled is not None
    row, tools = settled
    assert row["user_id"] == "u1" and row["session_id"] == "ses1"
    assert row["round_seq"] == 1
    assert row["message_id"] == "ma1"          # 自然键取 assistant 消息 id
    assert row["succeeded"] is True
    assert row["errored"] is False
    assert row["duration_ms"] == 2_500         # assistant completed - created
    assert row["total_tokens"] == 19           # 递归求和含 cache 嵌套
    assert row["cost"] == 0.021
    assert (row["model_provider"], row["model_id"], row["agent"]) == ("anthropic", "claude-x", "build")
    assert row["is_task"] is False and row["task_success"] is None
    assert (row["tool_calls"], row["tool_errors"]) == (2, 1)

    assert sorted(t["tool_name"] for t in tools) == ["bash", "read"]
    err = next(t for t in tools if t["is_error"])
    assert err["status"] == "error" and err["error_text"] == "ENOENT"


def test_model_read_from_flat_v1_fields():
    """opencode v1.18.16 的 assistant info 用扁平 providerID/modelID（无嵌套 model）。"""
    agg = RoundAggregator("u1", "ses1")
    _feed(
        agg,
        _user(),
        _assistant(providerID="openai", modelID="gpt-4o", agent="build"),
    )
    row, _ = agg.consume("session.idle", {"sessionID": "ses1"})
    assert (row["model_provider"], row["model_id"], row["agent"]) == ("openai", "gpt-4o", "build")


def test_tool_part_updates_replace_rather_than_accumulate():
    """同一 part 先 pending 后 error —— 只算一次调用、一次错误。"""
    agg = RoundAggregator("u1", "ses1")
    _feed(agg, _user(), _assistant(), _tool("p1", status="pending"), _tool("p1", status="error", error="boom"))
    row, tools = agg.consume("session.idle", {})

    assert (row["tool_calls"], row["tool_errors"]) == (1, 1)
    assert tools[0]["status"] == "error" and tools[0]["error_text"] == "boom"


def test_session_error_marks_round_failed():
    agg = RoundAggregator("u1", "ses1")
    _feed(agg, _user(), _assistant())
    agg.consume("session.error", {"error": {"message": "model overloaded"}})
    row, _ = agg.consume("session.idle", {})

    assert row["succeeded"] is False
    assert row["errored"] is True
    assert row["error_text"] == "model overloaded"


def test_assistant_error_field_alone_fails_the_round():
    agg = RoundAggregator("u1", "ses1")
    _feed(agg, _user(), _assistant(completed=None, error="AbortError"))
    row, _ = agg.consume("session.idle", {})

    assert row["succeeded"] is False and row["errored"] is True
    assert row["error_text"] == "AbortError"
    assert row["duration_ms"] is None          # 未完成 → 无耗时


def test_todo_signal_drives_the_task_track():
    ok = RoundAggregator("u1", "s_ok")
    _feed(ok, _user(), _assistant())
    ok.consume("todo.updated", {"todos": [{"status": "completed"}, {"status": "completed"}]})
    row, _ = ok.consume("session.idle", {})
    assert row["is_task"] is True and row["task_success"] is True

    bad = RoundAggregator("u1", "s_bad")
    _feed(bad, _user(), _assistant())
    bad.consume("todo.updated", {"todos": [{"status": "completed"}, {"status": "in_progress"}]})
    row, _ = bad.consume("session.idle", {})
    assert row["is_task"] is True and row["task_success"] is False


def test_round_without_assistant_message_gets_synthetic_key():
    agg = RoundAggregator("u1", "ses1")
    agg.consume(*_user(mid="mu9"))
    row, tools = agg.consume("session.idle", {})

    assert row["message_id"] == "user:mu9"     # 兜底键仍确定、非空 → 回补幂等
    assert row["succeeded"] is False
    assert tools == []


def test_empty_round_and_stale_idle_are_discarded():
    agg = RoundAggregator("u1", "ses1")
    assert agg.consume("session.idle", {}) is None      # 挂载时没有进行中的回合
    _feed(agg, _user(), _assistant())
    assert agg.consume("session.idle", {}) is not None
    assert agg.consume("session.idle", {}) is None      # 重复 idle 不再结算


def test_round_seq_increments_across_rounds():
    agg = RoundAggregator("u1", "ses1")
    seqs = []
    for i in range(3):
        _feed(agg, _user(mid=f"mu{i}"), _assistant(mid=f"ma{i}"))
        row, _ = agg.consume("session.idle", {})
        seqs.append(row["round_seq"])
    assert seqs == [1, 2, 3]


def test_sum_tokens_ignores_bools_and_non_dicts():
    assert _sum_tokens({"a": 1, "b": 2.5, "flag": True, "nested": {"c": 3}}) == 6
    assert _sum_tokens({}) is None
    assert _sum_tokens("nope") is None


# --------------------------------------------------------------- 持久化


async def test_persist_is_idempotent_on_the_natural_key(db_factory):
    agg = RoundAggregator("u1", "ses1")
    _feed(agg, _user(), _assistant(), _tool("p1"))
    row, tools = agg.consume("session.idle", {})

    assert await metrics_collector.persist(row, tools, "tap") is True
    # 回补重放同一回合（自然键相同）→ 跳过，不写重、不写脏工具行。
    assert await metrics_collector.persist(row, tools, "backfill") is False

    async with db_factory() as db:
        rounds = (await db.execute(select(AgentRoundMetrics))).scalars().all()
        tool_rows = (await db.execute(select(ToolCallMetrics))).scalars().all()
    assert len(rounds) == 1 and rounds[0].source == "tap"
    assert len(tool_rows) == 1 and tool_rows[0].round_seq == 1


async def test_tap_observer_writes_rows_and_resolves_tool_session_by_message_id(db_factory):
    """tool part 事件不带 sessionID —— 靠 assistant messageID 反查所属会话。"""
    events = [
        {"type": "message.updated", "properties": {"info": {
            "id": "mu1", "role": "user", "sessionID": "ses1", "time": {"created": 1_000}}}},
        {"type": "message.updated", "properties": {"info": {
            "id": "ma1", "role": "assistant", "sessionID": "ses1", "agent": "build",
            "model": {"providerID": "anthropic", "modelID": "claude-x"},
            "time": {"created": 1_500, "completed": 4_000}}}},
        {"type": "message.part.updated", "properties": {"part": {
            "id": "p1", "messageID": "ma1", "type": "tool", "tool": "bash",
            "state": {"status": "completed"}}}},
        {"type": "message.part.delta", "properties": {"noise": True}},   # 高频无关事件
        {"type": "todo.updated", "properties": {"sessionID": "ses1", "todos": [{"status": "completed"}]}},
        {"type": "session.idle", "properties": {"sessionID": "ses1"}},
    ]
    for e in events:
        metrics_collector.observe("u1", e)      # 永不抛异常，落库走后台任务
    await asyncio.sleep(0.05)

    async with db_factory() as db:
        row = (await db.execute(select(AgentRoundMetrics))).scalar_one()
        tool_rows = (await db.execute(select(ToolCallMetrics))).scalars().all()

    assert row.session_id == "ses1" and row.message_id == "ma1"
    assert row.succeeded is True and row.is_task is True and row.task_success is True
    assert row.source == "tap" and row.model_provider == "anthropic"
    assert len(tool_rows) == 1 and tool_rows[0].tool_name == "bash"


def test_observe_swallows_malformed_events():
    for event in (
        {},
        {"type": "message.updated"},
        {"type": "message.updated", "properties": {"info": "not-a-dict"}},
        {"type": "message.part.updated", "properties": {"part": {"type": "tool"}}},
        {"type": "session.idle", "properties": {}},
    ):
        metrics_collector.observe("u1", event)   # 不抛即为通过


# --------------------------------------------------------------- 历史回补

_HISTORY = [
    {"info": {"id": "mu1", "role": "user", "sessionID": "ses1", "time": {"created": 1_000}},
     "parts": [{"id": "t1", "type": "text", "text": "帮我改一下"}]},
    {"info": {"id": "ma1", "role": "assistant", "sessionID": "ses1", "agent": "build",
              "model": {"providerID": "anthropic", "modelID": "claude-x"},
              "time": {"created": 1_200, "completed": 3_000}},
     "parts": [{"id": "p1", "type": "tool", "tool": "read", "state": {"status": "completed"}},
               {"id": "p2", "type": "tool", "tool": "bash", "state": {"status": "error"}}]},
]


async def test_backfill_replays_rest_history_and_is_rerunnable(mock_httpx, db_factory):
    recorded = mock_httpx(lambda req: httpx.Response(200, json=_HISTORY))

    first = await backfill_session("u1", "ses1", "http://agent:8000", ("opencode", "pw"))
    assert first == {"session_id": "ses1", "records": 2, "inserted": 1}
    assert recorded[0].url.path == "/session/ses1/message"

    # 重复回补：读到的回合数不变，新增为 0（幂等）。
    second = await backfill_session("u1", "ses1", "http://agent:8000", ("opencode", "pw"))
    assert second["inserted"] == 0

    async with db_factory() as db:
        row = (await db.execute(select(AgentRoundMetrics))).scalar_one()
        tool_count = (await db.execute(select(func.count(ToolCallMetrics.id)))).scalar_one()

    assert row.source == "backfill" and row.message_id == "ma1"
    assert row.succeeded is True and row.duration_ms == 1_800
    # REST 历史里没有 todo 状态 → 任务轨信号缺失（口径见模块 docstring）。
    assert row.is_task is False and row.task_success is None
    assert (row.tool_calls, row.tool_errors) == (2, 1)
    assert tool_count == 2


async def test_backfill_accepts_envelope_and_tolerates_junk(mock_httpx):
    mock_httpx(lambda req: httpx.Response(200, json={"data": _HISTORY, "cursor": {}}))
    assert (await backfill_session("u1", "s2", "http://a", ("o", "p")))["inserted"] == 1

    mock_httpx(lambda req: httpx.Response(200, json=[None, "junk", {"info": "bad"}, {"no_info": 1}]))
    res = await backfill_session("u1", "s3", "http://a", ("o", "p"))
    assert res == {"session_id": "s3", "records": 4, "inserted": 0}


# ------------------------------------------------- A1 错误分类 / A3 token 拆分


def test_err_fields_extracts_name_message_and_status_code():
    """opencode 错误联合：name 判别、详情在 data.{message,statusCode}。"""
    assert _err_fields({"name": "APIError", "data": {"message": "rate limited", "statusCode": 429}}) == (
        "APIError", "rate limited", 429,
    )
    # 纯字符串错误（工具 state.error）→ 只有 message。
    assert _err_fields("ENOENT") == (None, "ENOENT", None)
    # 空 / 非 dict。
    assert _err_fields(None) == (None, None, None)
    assert _err_fields({}) == (None, None, None)


def test_error_classification_flows_into_round_row():
    agg = RoundAggregator("u1", "ses1")
    _feed(agg, _user(), _assistant(completed=None))
    agg.consume("session.error", {"error": {"name": "APIError", "data": {"message": "boom", "statusCode": 500}}})
    row, _ = agg.consume("session.idle", {})
    assert row["errored"] is True and row["succeeded"] is False
    assert row["error_name"] == "APIError"
    assert row["error_status_code"] == 500
    assert row["error_text"] == "boom"


def test_token_split_extracts_components_and_skips_missing():
    split = _token_split({"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 1}})
    assert split == {
        "input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 2,
        "cache_read_tokens": 3, "cache_write_tokens": 1,
    }
    # 缺失项不出现在结果里（区分「未上报」与「0」）。
    assert _token_split({"input": 7}) == {"input_tokens": 7}
    assert _token_split("nope") == {}


def test_sum_tokens_prefers_explicit_total_without_double_counting():
    # 显式 total 存在时直接取用，绝不与分项再累加。
    assert _sum_tokens({"total": 100, "input": 10, "output": 5}) == 100
    # total 缺失时求和各分项叶子（含 cache 嵌套），跳过 total 键。
    assert _sum_tokens({"input": 10, "output": 5, "cache": {"read": 3, "write": 1}}) == 19


def test_token_split_columns_populated_on_round_row():
    agg = RoundAggregator("u1", "ses1")
    _feed(
        agg,
        _user(),
        _assistant(tokens={"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 1}}),
    )
    row, _ = agg.consume("session.idle", {})
    assert row["total_tokens"] == 21
    assert row["input_tokens"] == 10 and row["output_tokens"] == 5
    assert row["reasoning_tokens"] == 2
    assert row["cache_read_tokens"] == 3 and row["cache_write_tokens"] == 1


# ------------------------------------------------- A2 单次工具耗时


def test_tool_duration_from_state_time():
    """completed/error 态的 state.time.{start,end} → duration_ms（仅实时 tap 有）。"""
    agg = RoundAggregator("u1", "ses1")
    _feed(agg, _user(), _assistant())
    agg.consume("message.part.updated", {"part": {
        "id": "p1", "type": "tool", "tool": "bash",
        "state": {"status": "completed", "time": {"start": 1_000, "end": 1_750}},
    }})
    # 无 time 字段（回补形状）→ duration_ms=None。
    agg.consume("message.part.updated", {"part": {
        "id": "p2", "type": "tool", "tool": "read", "state": {"status": "completed"},
    }})
    _, tools = agg.consume("session.idle", {})
    by_name = {t["tool_name"]: t for t in tools}
    assert by_name["bash"]["duration_ms"] == 750
    assert by_name["read"]["duration_ms"] is None


# ------------------------------------------------- B1/B2 LLM 代理指标


async def test_record_llm_call_persists_row(db_factory):
    record_llm_call(
        user_id="u1", provider_id="deepseek", method="POST",
        status_code=200, ttft_ms=120, duration_ms=3_400, is_sse=True, upstream_error=False,
    )
    await asyncio.sleep(0.05)
    async with db_factory() as db:
        row = (await db.execute(select(LLMProxyMetrics))).scalar_one()
    assert row.provider_id == "deepseek" and row.user_id == "u1"
    assert row.status_code == 200 and row.upstream_error is False
    assert row.ttft_ms == 120 and row.duration_ms == 3_400 and row.is_sse is True


async def test_record_llm_call_upstream_error_and_null_ttft(db_factory):
    """连接失败路径：status=502、upstream_error=True、ttft=None。"""
    record_llm_call(
        user_id=None, provider_id="openai", method="POST",
        status_code=502, ttft_ms=None, duration_ms=15, is_sse=False, upstream_error=True,
    )
    await asyncio.sleep(0.05)
    async with db_factory() as db:
        row = (await db.execute(select(LLMProxyMetrics))).scalar_one()
    assert row.user_id is None and row.status_code == 502
    assert row.upstream_error is True and row.ttft_ms is None
