"""用户体验指标采集 — 服务端 SSE tap（方案 A）+ 历史回补（方案 A）。

平台本身不实现任何 agent 逻辑，只是 opencode 的鉴权反向代理；每容器一个
SSE Pump 在 ``ContainerEventBus.push_event()`` 处订阅并转发容器 ``/event``
的**全部**事件。这里是服务端唯一的「上帝视角」，也是不可被用户端篡改的
指标采集点。

设计要点（见最终补充设计）：
  * RoundAggregator 按 (user_id, session_id) 累积事件，收到 ``session.idle``
    时结算出**一行** AgentRoundMetrics + N 行 ToolCallMetrics。
  * **实时 tap 与离线回补共用同一个 RoundAggregator**，只是事件来源不同
    （SSE 流 vs REST ``/session/{id}/message`` 重放），保证成功率/耗时口径唯一。
  * 幂等自然键 = (user_id, session_id, message_id)，其中 message_id 为 assistant
    消息 id（opencode 内全局唯一、tap 与回补一致）；无 assistant 消息的回合用
    ``user:{user_message_id}`` 合成兜底。回补因此可与实时数据重复触发而不写重。
  * observer 与持久化遵循 request_log 的「绝不拖垮被观测调用」语义：任何异常
    只记 warning。

数据可得性约束（已在设计阶段坐实）：
  * tool part 的 ``state.time.start/end`` 仅**实时 tap** 可得（ToolStateCompleted/
    Error 均 required），故采集单次工具耗时 duration_ms；REST 回补的
    SessionMessageToolState* **无** time 字段，回补行 duration_ms=None。
  * ``tokens`` 原样存整个 dict（防 cache.read/write 等未列字段丢失），另存冗余
    ``total_tokens`` 供聚合，并拆分出 input/output/reasoning/cache 冗余列。
    ``total_tokens`` 优先取上游显式 ``total``，缺失时才求和明细叶子，避免把
    ``total`` 与各分项重复累加。
  * 错误按 opencode 错误联合的 ``name`` 分类（error_name），并保留 data.message
    详情与 APIError 的 data.statusCode（error_status_code）供错误率下钻。
  * 回补依赖 REST 消息列表，其中**不含** todo 状态，故回补行的 is_task=False、
    task_success=None（任务轨信号仅实时 tap 可得）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any, Optional

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ..database import async_session
from ..models import AgentRoundMetrics, LLMProxyMetrics, ToolCallMetrics

logger = logging.getLogger(__name__)

# tap 只关心这几类事件；其余（尤其高频的 message.part.delta）快速忽略。
_RELEVANT_EVENTS = frozenset({
    "message.updated",
    "message.part.updated",
    "todo.updated",
    "session.idle",
    "session.error",
})

# 保留窗口 —— 复用 request_log 的 MAX_ROWS/PRUNE_EVERY 模式，稳态开销近零。
_MAX_ROUND_ROWS = 200_000
_MAX_TOOL_ROWS = 1_000_000
_MAX_LLM_ROWS = 500_000
_PRUNE_EVERY = 500


# ---------------------------------------------------------------------------
# 字段解析辅助（防御式，绝不假设字段一定存在）
# ---------------------------------------------------------------------------

def _payload(event: dict) -> dict:
    """opencode V1 事件负载在 ``properties``，V2 在 ``data`` —— 两者都接受。"""
    p = event.get("properties")
    if isinstance(p, dict):
        return p
    d = event.get("data")
    if isinstance(d, dict):
        return d
    return {}


def _model_parts(model: Any) -> tuple[Optional[str], Optional[str]]:
    """从嵌套 ModelRef 提取 (provider, model_id)。兼容 {providerID,id} 与 {providerID,modelID}。"""
    if not isinstance(model, dict):
        return None, None
    provider = model.get("providerID") or model.get("provider")
    mid = model.get("modelID") or model.get("id")
    return (str(provider)[:128] if provider else None,
            str(mid)[:128] if mid else None)


def _model_from_info(info: dict) -> tuple[Optional[str], Optional[str]]:
    """从 assistant message ``info`` 提取 (provider, model_id)。

    opencode v1.18.16 的 AssistantMessage 用**扁平** ``providerID`` / ``modelID``
    字段（见 opencode-api.json 的 AssistantMessage schema：两者均 required，且
    ``additionalProperties:false`` —— 根本没有嵌套 ``model`` 对象）。只有旧版 /
    V2 ``session.next.*`` 事件才把模型放在嵌套 ``model`` ModelRef 里。这里优先读
    扁平字段，读不到再回退嵌套 ModelRef，两代格式都兼容。
    """
    prov = info.get("providerID") or info.get("provider")
    mid = info.get("modelID")
    if prov or mid:
        return (str(prov)[:128] if prov else None,
                str(mid)[:128] if mid else None)
    return _model_parts(info.get("model"))


def _sum_tokens(tokens: Any) -> Optional[int]:
    """回合总 token 数。

    优先取上游显式 ``total``（opencode tokens schema 里 total 可选）；缺失时才
    递归求和各分项叶子（input/output/reasoning/cache.read/cache.write）。**不**把
    total 与分项一起累加，避免重复计数。
    """
    if not isinstance(tokens, dict):
        return None
    explicit = tokens.get("total")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return int(explicit)
    total = 0
    found = False
    for k, v in tokens.items():
        if k == "total" or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            total += int(v)
            found = True
        elif isinstance(v, dict):
            sub = _sum_tokens(v)
            if sub is not None:
                total += sub
                found = True
    return total if found else None


def _token_split(tokens: Any) -> dict:
    """从 tokens dict 抽取 input/output/reasoning/cache.read/cache.write 分项。

    缺失项返回 None（而非 0），以便看板区分「未上报」与「确实为 0」。
    """
    if not isinstance(tokens, dict):
        return {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    out = {
        "input_tokens": _to_int(tokens.get("input")),
        "output_tokens": _to_int(tokens.get("output")),
        "reasoning_tokens": _to_int(tokens.get("reasoning")),
        "cache_read_tokens": _to_int(cache.get("read")),
        "cache_write_tokens": _to_int(cache.get("write")),
    }
    return {k: v for k, v in out.items() if v is not None}


def _err_fields(error: Any) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """把 opencode 错误联合规整成 (name, message, status_code)。

    错误形如 ``{"name": "APIError", "data": {"message": ..., "statusCode": 429,
    "isRetryable": true}}``（见 opencode-api.json 错误 schema：``name`` 判别、详情在
    ``data``）。旧代码只取 ``error.get("message")``（顶层无此键）→ 只落到 name，
    丢了 data.message 与 statusCode。这里三者都抽出。工具错误的 ``state.error``
    是纯字符串，走字符串分支。
    """
    if not error:
        return None, None, None
    if isinstance(error, str):
        return None, error[:2000], None
    if isinstance(error, dict):
        name = error.get("name")
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        msg = data.get("message") or error.get("message") or error.get("text")
        status = _to_int(data.get("statusCode")) or _to_int(error.get("statusCode"))
        if msg:
            text = str(msg)[:2000]
        elif name:
            text = str(name)[:2000]
        else:
            text = json.dumps(error, ensure_ascii=False)[:2000]
        return (str(name)[:64] if name else None, text, status)
    return None, str(error)[:2000], None


def _err_text(error: Any) -> Optional[str]:
    """把 error 规整成截断后的字符串（保留 data.message 详情）。"""
    return _err_fields(error)[1]


def _to_float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


# ---------------------------------------------------------------------------
# RoundAggregator —— 实时 tap 与离线回补共用的回合结算器
# ---------------------------------------------------------------------------

class RoundAggregator:
    """按单个 (user_id, session_id) 累积事件，``session.idle`` 时结算一个回合。

    一个回合 = 一次 user prompt → session.idle。``consume()`` 喂入一个事件，
    若触发结算则返回 ``(round_dict, tool_dicts)``，否则返回 None。
    """

    def __init__(self, user_id: str, session_id: str) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.round_seq = 0
        self._round: Optional[dict] = None

    def _new_round(self) -> dict:
        return {
            "user_message_id": None,
            "assistant_message_id": None,
            "created_ms": None,
            "completed_ms": None,
            "errored": False,
            "error_text": None,
            "error_name": None,
            "error_status_code": None,
            "agent": None,
            "model_provider": None,
            "model_id": None,
            "cost": None,
            "tokens": None,
            "total_tokens": None,
            "token_split": {},
            "tools": {},          # part_id -> tool 记录（full-replace）
            "is_task": False,
            "task_success": None,
        }

    def consume(self, type_: str, data: dict) -> Optional[tuple[dict, list[dict]]]:
        if type_ == "message.updated":
            return self._on_message(data)
        if type_ == "message.part.updated":
            self._on_part(data)
            return None
        if type_ == "todo.updated":
            self._on_todo(data)
            return None
        if type_ == "session.error":
            if self._round is not None:
                self._round["errored"] = True
                name, msg, status = _err_fields(data.get("error"))
                if not self._round["error_text"]:
                    self._round["error_text"] = msg or "session error"
                if not self._round["error_name"]:
                    self._round["error_name"] = name
                if self._round["error_status_code"] is None:
                    self._round["error_status_code"] = status
            return None
        if type_ == "session.idle":
            return self._settle()
        return None

    # -- 各事件处理 ---------------------------------------------------------

    def _on_message(self, data: dict) -> Optional[tuple[dict, list[dict]]]:
        info = data.get("info")
        if not isinstance(info, dict) or not info.get("id"):
            return None
        role = info.get("role")
        if role == "user":
            # 新回合开始；丢弃任何未结算的陈旧回合（上一回合 idle 丢失的边缘情况）。
            self._round = self._new_round()
            self._round["user_message_id"] = str(info.get("id"))
            self._round["created_ms"] = _to_int((info.get("time") or {}).get("created"))
            return None
        if role == "assistant":
            if self._round is None:
                # 中途挂载（tap 在回合进行中才 attach）——补开一个回合。
                self._round = self._new_round()
            r = self._round
            r["assistant_message_id"] = str(info.get("id"))
            t = info.get("time") or {}
            if t.get("created") is not None:
                r["created_ms"] = _to_int(t.get("created"))
            if t.get("completed") is not None:
                r["completed_ms"] = _to_int(t.get("completed"))
            if info.get("error"):
                r["errored"] = True
                name, msg, status = _err_fields(info.get("error"))
                r["error_text"] = msg
                r["error_name"] = name
                r["error_status_code"] = status
            if info.get("agent"):
                r["agent"] = str(info.get("agent"))[:128]
            prov, mid = _model_from_info(info)
            if prov:
                r["model_provider"] = prov
            if mid:
                r["model_id"] = mid
            cost = _to_float(info.get("cost"))
            if cost is not None:
                r["cost"] = cost
            toks = info.get("tokens")
            if isinstance(toks, dict):
                r["tokens"] = toks
                r["total_tokens"] = _sum_tokens(toks)
                r["token_split"] = _token_split(toks)
        return None

    def _on_part(self, data: dict) -> None:
        part = data.get("part")
        if not isinstance(part, dict) or part.get("type") != "tool":
            return
        if self._round is None:
            return
        pid = part.get("id")
        if not pid:
            return
        st = part.get("state") or {}
        status = str(st.get("status") or "pending")[:32]
        is_error = status == "error"
        # 单次工具耗时（A2）：completed/error 态的 state.time.{start,end}（tap 才有）。
        t = st.get("time") if isinstance(st.get("time"), dict) else {}
        start = _to_int(t.get("start"))
        end = _to_int(t.get("end"))
        tool_ms = (end - start) if (start is not None and end is not None and end >= start) else None
        # full-replace：同一 part 多次 updated 只保留最新状态。
        self._round["tools"][str(pid)] = {
            "tool_name": str(part.get("tool") or "tool")[:128],
            "status": status,
            "is_error": is_error,
            "error_text": _err_text(st.get("error")) if is_error else None,
            "duration_ms": tool_ms,
        }

    def _on_todo(self, data: dict) -> None:
        if self._round is None:
            self._round = self._new_round()
        self._round["is_task"] = True
        todos = data.get("todos")
        if isinstance(todos, list) and todos:
            statuses = [t.get("status") if isinstance(t, dict) else None for t in todos]
            # 任务成功信号：todos 全部 completed（存在 pending/in_progress/cancelled 即未成功）。
            self._round["task_success"] = all(s == "completed" for s in statuses)

    # -- 结算 ---------------------------------------------------------------

    def _settle(self) -> Optional[tuple[dict, list[dict]]]:
        r = self._round
        if r is None:
            return None
        self._round = None
        if not r["assistant_message_id"] and not r["user_message_id"]:
            return None  # 空回合，丢弃
        self.round_seq += 1
        message_id = r["assistant_message_id"] or f"user:{r['user_message_id']}"

        duration = None
        if r["created_ms"] is not None and r["completed_ms"] is not None:
            d = r["completed_ms"] - r["created_ms"]
            duration = d if d >= 0 else None

        # 成功 = 有 assistant 消息、未报错、且已完成（time.completed 存在）。
        succeeded = bool(r["assistant_message_id"]) and not r["errored"] and r["completed_ms"] is not None

        tools = list(r["tools"].values())
        round_dict = {
            "user_id": self.user_id,
            "session_id": self.session_id[:255],
            "round_seq": self.round_seq,
            "message_id": message_id[:255],
            "is_task": r["is_task"],
            "succeeded": succeeded,
            "task_success": r["task_success"] if r["is_task"] else None,
            "errored": r["errored"],
            "error_text": r["error_text"],
            "error_name": r["error_name"],
            "error_status_code": r["error_status_code"],
            "duration_ms": duration,
            "tokens": json.dumps(r["tokens"], ensure_ascii=False) if r["tokens"] else None,
            "total_tokens": r["total_tokens"],
            "cost": r["cost"],
            "tool_calls": len(tools),
            "tool_errors": sum(1 for t in tools if t["is_error"]),
            "model_provider": r["model_provider"],
            "model_id": r["model_id"],
            "agent": r["agent"],
        }
        round_dict.update(r["token_split"])
        tool_dicts = [
            {
                "user_id": self.user_id,
                "session_id": self.session_id[:255],
                "round_seq": self.round_seq,
                "tool_name": t["tool_name"],
                "status": t["status"],
                "is_error": t["is_error"],
                "error_text": t["error_text"],
                "duration_ms": t.get("duration_ms"),
            }
            for t in tools
        ]
        return round_dict, tool_dicts


# ---------------------------------------------------------------------------
# MetricsCollector —— observer 挂载、内存态聚合、幂等持久化
# ---------------------------------------------------------------------------

class MetricsCollector:
    """全局单例：挂在每个 ContainerEventBus 上，实时结算并落库。"""

    MAX_AGGREGATORS = 2000

    def __init__(self) -> None:
        # (user_id, session_id) -> RoundAggregator；FIFO 淘汰防泄漏。
        self._aggregators: "OrderedDict[tuple[str, str], RoundAggregator]" = OrderedDict()
        # (user_id, assistant_message_id) -> session_id：tool part 无 sessionID，
        # 靠 messageID 反查所属会话。
        self._mid_to_sid: "OrderedDict[tuple[str, str], str]" = OrderedDict()
        self._writes_since_prune = 0

    # -- observer 入口（同步，绝不抛异常）------------------------------------

    def observe(self, user_id: str, event: dict) -> None:
        """挂在 ContainerEventBus.push_event 上的 observer。永不抛异常。"""
        try:
            self.on_event(user_id, event)
        except Exception:  # noqa: BLE001
            logger.warning("metrics tap on_event failed", exc_info=True)

    def on_event(self, user_id: str, event: dict) -> None:
        type_ = event.get("type")
        if type_ not in _RELEVANT_EVENTS:
            return
        data = _payload(event)

        if type_ == "message.updated":
            info = data.get("info")
            if not isinstance(info, dict):
                return
            sid = info.get("sessionID")
            mid = info.get("id")
            if sid and mid:
                self._remember_mid(user_id, str(mid), str(sid))
            if not sid:
                return
            self._feed(user_id, str(sid), type_, data)

        elif type_ == "message.part.updated":
            part = data.get("part")
            if not isinstance(part, dict):
                return
            sid = data.get("sessionID") or part.get("sessionID")
            if not sid:
                mid = part.get("messageID")
                if mid:
                    sid = self._mid_to_sid.get((user_id, str(mid)))
            if not sid:
                return
            self._feed(user_id, str(sid), type_, data)

        else:  # todo.updated / session.idle / session.error
            sid = data.get("sessionID")
            if not sid:
                return
            self._feed(user_id, str(sid), type_, data)

    def _feed(self, user_id: str, session_id: str, type_: str, data: dict) -> None:
        agg = self._get_aggregator(user_id, session_id)
        settled = agg.consume(type_, data)
        if settled:
            self._schedule_persist(settled[0], settled[1], "tap")

    def _get_aggregator(self, user_id: str, session_id: str) -> RoundAggregator:
        key = (user_id, session_id)
        agg = self._aggregators.get(key)
        if agg is None:
            agg = RoundAggregator(user_id, session_id)
            self._aggregators[key] = agg
            while len(self._aggregators) > self.MAX_AGGREGATORS:
                self._aggregators.popitem(last=False)
        else:
            self._aggregators.move_to_end(key)
        return agg

    def _remember_mid(self, user_id: str, mid: str, sid: str) -> None:
        key = (user_id, mid)
        self._mid_to_sid[key] = sid
        self._mid_to_sid.move_to_end(key)
        while len(self._mid_to_sid) > self.MAX_AGGREGATORS * 4:
            self._mid_to_sid.popitem(last=False)

    def _schedule_persist(self, round_dict: dict, tool_dicts: list[dict], source: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.persist(round_dict, tool_dicts, source))
        except RuntimeError:
            # 无运行中的事件循环（理论上 tap 场景不会发生）——丢弃，回补可补回。
            logger.debug("no running loop to persist metrics; skipping")

    # -- 幂等持久化 ---------------------------------------------------------

    async def persist(self, round_dict: dict, tool_dicts: list[dict], source: str) -> bool:
        """幂等写一行回合指标 + 其工具行。已存在（自然键冲突）则整回合跳过。

        返回是否实际写入。任何异常只记 warning，绝不向上抛。
        """
        row = {**round_dict, "source": source}
        try:
            async with async_session() as db:
                dup = await db.execute(
                    select(AgentRoundMetrics.id).where(
                        AgentRoundMetrics.user_id == row["user_id"],
                        AgentRoundMetrics.session_id == row["session_id"],
                        AgentRoundMetrics.message_id == row["message_id"],
                    ).limit(1)
                )
                if dup.scalar_one_or_none() is not None:
                    return False
                db.add(AgentRoundMetrics(**row))
                for t in tool_dicts:
                    db.add(ToolCallMetrics(**{**t, "source": source}))
                await db.commit()
            self._writes_since_prune += 1
            if self._writes_since_prune >= _PRUNE_EVERY:
                self._writes_since_prune = 0
                await self.prune()
            return True
        except IntegrityError:
            # 并发下（tap 与回补同时）自然键撞车 —— 视为已存在，非错误。
            return False
        except Exception:  # noqa: BLE001
            logger.warning("metrics persist failed", exc_info=True)
            return False

    async def prune(self) -> None:
        """保留最新 N 行（按 id 即插入序），控制两张 metrics 表体积。"""
        try:
            async with async_session() as db:
                keep_rounds = select(AgentRoundMetrics.id).order_by(
                    AgentRoundMetrics.id.desc()
                ).limit(_MAX_ROUND_ROWS).scalar_subquery()
                await db.execute(
                    delete(AgentRoundMetrics).where(AgentRoundMetrics.id.not_in(keep_rounds))
                )
                keep_tools = select(ToolCallMetrics.id).order_by(
                    ToolCallMetrics.id.desc()
                ).limit(_MAX_TOOL_ROWS).scalar_subquery()
                await db.execute(
                    delete(ToolCallMetrics).where(ToolCallMetrics.id.not_in(keep_tools))
                )
                keep_llm = select(LLMProxyMetrics.id).order_by(
                    LLMProxyMetrics.id.desc()
                ).limit(_MAX_LLM_ROWS).scalar_subquery()
                await db.execute(
                    delete(LLMProxyMetrics).where(LLMProxyMetrics.id.not_in(keep_llm))
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("metrics prune failed", exc_info=True)

    # -- LLM 代理上游指标（B1/B2）-------------------------------------------

    async def persist_llm_call(self, row: dict) -> bool:
        """幂等写一行 LLM 代理上游指标。异常只记 warning，绝不向上抛。"""
        try:
            async with async_session() as db:
                db.add(LLMProxyMetrics(**row))
                await db.commit()
            self._writes_since_prune += 1
            if self._writes_since_prune >= _PRUNE_EVERY:
                self._writes_since_prune = 0
                await self.prune()
            return True
        except Exception:  # noqa: BLE001
            logger.warning("llm proxy metrics persist failed", exc_info=True)
            return False


# 全局单例
metrics_collector = MetricsCollector()


def record_llm_call(
    *,
    user_id: Optional[str],
    provider_id: str,
    method: str = "POST",
    status_code: Optional[int],
    ttft_ms: Optional[int],
    duration_ms: Optional[int],
    is_sse: bool,
    upstream_error: bool,
) -> None:
    """火忘记录一次 LLM 代理转发（B1 状态码 + B2 TTFT/耗时）。

    遵循「绝不拖垮被观测调用」：无运行循环时静默丢弃，持久化异常只记 warning。
    """
    row = {
        "user_id": str(user_id)[:36] if user_id else None,
        "provider_id": str(provider_id)[:128],
        "method": str(method)[:10],
        "status_code": status_code,
        "ttft_ms": ttft_ms,
        "duration_ms": duration_ms,
        "is_sse": bool(is_sse),
        "upstream_error": bool(upstream_error),
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(metrics_collector.persist_llm_call(row))
    except RuntimeError:
        logger.debug("no running loop to record llm call; skipping")
    except Exception:  # noqa: BLE001
        logger.warning("record_llm_call failed", exc_info=True)


# ---------------------------------------------------------------------------
# 历史回补（方案 A）—— 复用同一个 RoundAggregator 离线重放
# ---------------------------------------------------------------------------

async def backfill_session(
    user_id: str,
    session_id: str,
    base_url: str,
    auth: tuple[str, str],
    timeout: float = 30.0,
) -> dict:
    """经 tunnel 拉取会话全量消息，用同一 RoundAggregator 离线重放，幂等落库。

    REST ``GET /session/{id}/message`` 返回**裸数组** ``[{info, parts}]``
    （legacy surface，非 {data,cursor} 信封）。重放时把每条消息转成与实时 tap
    完全相同的合成事件喂给 aggregator，assistant 消息完成后手动触发一次
    ``session.idle`` 结算（REST 流没有 idle/todo 事件）。

    返回 {session_id, records, inserted}。容器必须处于运行态。
    """
    url = f"{base_url}/session/{session_id}/message"
    async with httpx.AsyncClient(auth=auth, timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.json()

    if isinstance(body, list):
        records = body
    elif isinstance(body, dict) and isinstance(body.get("data"), list):
        records = body["data"]
    else:
        records = []

    agg = RoundAggregator(user_id, session_id)
    inserted = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        info = rec.get("info")
        parts = rec.get("parts")
        if not isinstance(info, dict):
            continue
        agg.consume("message.updated", {"info": info})
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, dict):
                    agg.consume("message.part.updated", {"part": p})
        # assistant 完成（有 completed 或 error）→ 结算该回合。
        completed = (info.get("time") or {}).get("completed") if isinstance(info.get("time"), dict) else None
        if info.get("role") == "assistant" and (completed or info.get("error")):
            settled = agg.consume("session.idle", {"sessionID": session_id})
            if settled and await metrics_collector.persist(settled[0], settled[1], "backfill"):
                inserted += 1
    return {"session_id": session_id, "records": len(records), "inserted": inserted}
