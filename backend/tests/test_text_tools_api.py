"""AI 文本润色接口契约（POST /api/text/polish）。

锁死的核心性质：
  * **平台直连上游**：请求必须打到「容器同款模型」的真实 baseURL +
    ``chat/completions``，Authorization 用宿主/用户自己的 apiKey，绝不经
    opencode（无会话副作用）。
  * **模型选择优先级**：前端传的当前会话模型 → 宿主 ``model`` →
    ``small_model`` → 任一 provider 首个模型；用户自有 provider 覆盖宿主同名项。
  * **输出净化**：剥整体 ``` 围栏，兼容 content 为分片数组。
  * **边界**：空文本 / 超长 400，超频 429，上游 4xx/5xx 与网络异常统一 502，
    无可用凭据 503，未登录 401。
  * **指标隔离**：``record_llm_call(method="polish")``，不混进对话轮次统计。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from app.routers import text_tools as tt

HOST_CONFIG = {
    "model": "anthropic/claude-sonnet-4",
    "small_model": "anthropic/claude-haiku",
    "provider": {
        "anthropic": {
            "npm": "@ai-sdk/anthropic",
            "options": {
                "baseURL": "https://api.example.com/v1",
                "apiKey": "sk-host",
            },
            "models": {"claude-sonnet-4": {}, "claude-haiku": {}},
        }
    },
}


def ok_body(content="润色后的文本"):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ------------------------------------------------------------------
#  fixtures
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """宿主配置固定 + 清空限流单例 + 指标改成内存记录（不落真库）。"""
    monkeypatch.setattr(tt, "load_source_config", lambda: (HOST_CONFIG, "test"))
    tt._polish_limiter._hits.clear()

    metrics: list[dict] = []

    def fake_record(**kwargs):
        metrics.append(kwargs)

    monkeypatch.setattr(tt, "record_llm_call", fake_record)
    return metrics


@pytest.fixture
def metrics(_isolated):
    return _isolated


@pytest.fixture
def client(app_client_factory):
    return app_client_factory([tt.router], user_id="u1", username="alice")


def bare_client() -> httpx.AsyncClient:
    """不覆盖 get_current_user 的裸应用，用于验证登录门禁。"""
    app = FastAPI()
    app.include_router(tt.router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def upstream_request(calls: list[httpx.Request]) -> tuple[str, dict, str]:
    """返回 (url, body, authorization) —— 断言「打到哪、带了什么」。"""
    assert len(calls) == 1
    req = calls[0]
    return (
        str(req.url),
        json.loads(req.content.decode()),
        req.headers.get("Authorization", ""),
    )


# ------------------------------------------------------------------
#  正常路径
# ------------------------------------------------------------------
async def test_polish_ok_hits_container_model_directly(client, mock_httpx, metrics):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "这个功能很好用"})
    assert r.status_code == 200
    data = r.json()
    assert data["text"] == "润色后的文本"
    assert data["model"] == "anthropic/claude-sonnet-4"
    assert data["elapsed_ms"] >= 0

    url, body, auth = upstream_request(calls)
    assert url == "https://api.example.com/v1/chat/completions"
    assert auth == "Bearer sk-host"
    assert body["model"] == "claude-sonnet-4"
    assert body["stream"] is False
    assert body["temperature"] == 0.5
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {
        "role": "user",
        "content": tt._wrap_draft("这个功能很好用"),
    }
    # 任务型草稿会被增强成完整指令，输出可达原文数倍；另加固定余量与推理额度
    assert body["max_tokens"] == (
        len("这个功能很好用") * 4 + 512 + tt.POLISH_REASONING_RESERVE
    )

    assert metrics and metrics[0]["method"] == "polish"
    assert metrics[0]["status_code"] == 200
    assert metrics[0]["upstream_error"] is False
    assert metrics[0]["user_id"] == "u1"


async def test_polish_uses_model_from_request(client, mock_httpx):
    """前端传当前会话选中模型时，必须原样采用（不被宿主 model 覆盖）。"""
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post(
            "/api/text/polish",
            json={
                "text": "hi",
                "model": {"providerID": "anthropic", "id": "claude-haiku"},
            },
        )
    assert r.status_code == 200
    assert r.json()["model"] == "anthropic/claude-haiku"
    _, body, _ = upstream_request(calls)
    assert body["model"] == "claude-haiku"


async def test_polish_caps_max_tokens(client, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "长" * 7000})
    assert r.status_code == 200
    _, body, _ = upstream_request(calls)
    assert body["max_tokens"] == 8192


async def test_user_provider_overrides_host_entry(client, mock_httpx, monkeypatch):
    """用户自有 provider 才是其容器实际生效的配置，同名时优先。"""

    async def fake_map(db, user_id):
        return {
            "anthropic": {
                "npm": "@ai-sdk/anthropic",
                "options": {
                    "baseURL": "https://user.example.com/v1",
                    "apiKey": "sk-user",
                },
                # 用户侧 models 存成列表（[{id,name}] 或 ["id"]），两种都要吃下
                "models": [{"id": "user-model", "name": "用户模型"}],
            }
        }

    monkeypatch.setattr(tt.user_config, "build_user_provider_map", fake_map)
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 200
    assert r.json()["model"] == "anthropic/user-model"
    url, body, auth = upstream_request(calls)
    assert url == "https://user.example.com/v1/chat/completions"
    assert auth == "Bearer sk-user"
    assert body["model"] == "user-model"


async def test_falls_back_to_small_model(client, mock_httpx, monkeypatch):
    source = {"small_model": "anthropic/claude-haiku", "provider": HOST_CONFIG["provider"]}
    monkeypatch.setattr(tt, "load_source_config", lambda: (source, "test"))
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 200
    assert r.json()["model"] == "anthropic/claude-haiku"
    _, body, _ = upstream_request(calls)
    assert body["model"] == "claude-haiku"


# ------------------------------------------------------------------
#  输出净化
# ------------------------------------------------------------------
async def test_strips_wrapping_code_fence(client, mock_httpx):
    mock_httpx(
        lambda request: httpx.Response(200, json=ok_body("```text\n润色结果\n```"))
    )
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.json()["text"] == "润色结果"


async def test_keeps_inner_code_block(client, mock_httpx):
    """正文本身就是代码块时不能见 ``` 就删。"""
    raw = "示例：\n```python\nprint(1)\n```\n以上。"
    mock_httpx(lambda request: httpx.Response(200, json=ok_body(raw)))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.json()["text"] == raw


async def test_accepts_content_parts(client, mock_httpx):
    body = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "分段"}, "拼接结果"]}}
        ]
    }
    mock_httpx(lambda request: httpx.Response(200, json=body))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.json()["text"] == "分段拼接结果"


# ------------------------------------------------------------------
#  「润色而非作答」—— 草稿是素材，不是给模型的指令
# ------------------------------------------------------------------
async def test_imperative_draft_is_wrapped_not_executed(client, mock_httpx):
    """祈使句草稿必须被边界标记包住，让模型知道这是待改写的素材。"""
    draft = "请调研「AI Agent 记忆」领域，帮我整理出 Top 3 方案并做简要介绍。"
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post("/api/text/polish", json={"text": draft})
    assert r.status_code == 200
    _, body, _ = upstream_request(calls)
    user_msg = body["messages"][-1]["content"]
    assert draft in user_msg
    assert user_msg.startswith("【待润色草稿开始】")
    assert user_msg.endswith("【待润色草稿结束】")
    # 系统提示必须明确禁止作答，否则模型会把这个提问当成任务去执行
    system_msg = body["messages"][0]["content"]
    assert "不是给你的指令" in system_msg
    assert "绝不回答" in system_msg
    # 且必须区分两种草稿：任务型增强成完整指令，陈述型不扩写
    assert "任务型草稿" in system_msg
    assert "陈述型草稿" in system_msg


async def test_braces_in_draft_survive_wrapping(client, mock_httpx):
    """草稿里的 {} 很常见（JSON、占位符），包装不能用 str.format。"""
    draft = '把 {"name": "{user}"} 这段改得更清楚'
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post("/api/text/polish", json={"text": draft})
    assert r.status_code == 200
    _, body, _ = upstream_request(calls)
    assert draft in body["messages"][-1]["content"]


# ------------------------------------------------------------------
#  输入边界
# ------------------------------------------------------------------
async def test_blank_text_is_400(client, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r1 = await client.post("/api/text/polish", json={"text": ""})
        r2 = await client.post("/api/text/polish", json={"text": "   \n  "})
    assert r1.status_code == r2.status_code == 400
    assert calls == []


async def test_oversized_text_is_400(client, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post(
            "/api/text/polish", json={"text": "x" * (tt.MAX_TEXT_CHARS + 1)}
        )
    assert r.status_code == 400
    assert str(tt.MAX_TEXT_CHARS) in r.json()["detail"]
    assert calls == []


async def test_rate_limit_is_429(client, mock_httpx):
    mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        for _ in range(tt.POLISH_RATE_LIMIT):
            assert (
                await client.post("/api/text/polish", json={"text": "hi"})
            ).status_code == 200
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 429
    assert "润色太频繁" in r.json()["detail"]


async def test_unauthenticated_is_401(mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with bare_client() as c:
        r = await c.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 401
    assert calls == []


# ------------------------------------------------------------------
#  上游 / 配置异常
# ------------------------------------------------------------------
async def test_upstream_5xx_is_502(client, mock_httpx, metrics):
    mock_httpx(
        lambda request: httpx.Response(
            500, json={"error": {"message": "model overloaded"}}
        )
    )
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 502
    assert "model overloaded" in r.json()["detail"]
    assert metrics[0]["status_code"] == 500
    assert metrics[0]["upstream_error"] is True


async def test_upstream_unreachable_is_502(client, mock_httpx, metrics):
    def boom(request):
        raise httpx.ConnectError("connection refused")

    mock_httpx(boom)
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 502
    assert metrics[0]["status_code"] == 502
    assert metrics[0]["upstream_error"] is True


async def test_empty_upstream_content_is_502(client, mock_httpx):
    """正文为空且不是被截断 —— 重试也没用，只打一次上游就报错。"""
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body("   ")))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 502
    assert len(calls) == 1


async def test_truncated_output_retries_with_bigger_budget(client, mock_httpx):
    """推理模型把预算全花在思考上时，放宽 max_tokens 再试一次应能拿到正文。"""
    responses = [
        httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
                ]
            },
        ),
        httpx.Response(200, json=ok_body("重试后的润色结果")),
    ]
    calls = mock_httpx(lambda request: responses.pop(0))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 200
    assert r.json()["text"] == "重试后的润色结果"
    assert len(calls) == 2
    budgets = [json.loads(c.content.decode())["max_tokens"] for c in calls]
    assert budgets[1] > budgets[0]


async def test_truncated_twice_reports_truncation(client, mock_httpx):
    def truncated(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
                ]
            },
        )

    calls = mock_httpx(truncated)
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 502
    assert "截断" in r.json()["detail"]
    assert len(calls) == tt.POLISH_MAX_ATTEMPTS


async def test_error_body_with_http_200_is_surfaced(client, mock_httpx):
    """部分网关用 200 包错误体（配额/审核），必须把原文透出而不是含糊报错。"""
    calls = mock_httpx(
        lambda request: httpx.Response(
            200, json={"error": {"message": "quota exceeded"}}
        )
    )
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 502
    assert "quota exceeded" in r.json()["detail"]
    assert len(calls) == 1


async def test_legacy_choices_text_is_accepted(client, mock_httpx):
    body = {"choices": [{"text": "老式返回", "finish_reason": "stop"}]}
    mock_httpx(lambda request: httpx.Response(200, json=body))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.json()["text"] == "老式返回"


async def test_no_configured_provider_is_503(client, mock_httpx, monkeypatch):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    monkeypatch.setattr(tt, "load_source_config", lambda: ({}, "test"))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 503
    assert calls == []


async def test_provider_without_api_key_is_503(client, mock_httpx, monkeypatch):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    source = {
        "model": "anthropic/claude-sonnet-4",
        "provider": {
            "anthropic": {
                "options": {"baseURL": "https://api.example.com/v1"},
                "models": {"claude-sonnet-4": {}},
            }
        },
    }
    monkeypatch.setattr(tt, "load_source_config", lambda: (source, "test"))
    async with client:
        r = await client.post("/api/text/polish", json={"text": "hi"})
    assert r.status_code == 503
    assert "密钥" in r.json()["detail"]
    assert calls == []


async def test_unknown_provider_is_503(client, mock_httpx):
    calls = mock_httpx(lambda request: httpx.Response(200, json=ok_body()))
    async with client:
        r = await client.post(
            "/api/text/polish",
            json={"text": "hi", "model": {"providerID": "ghost", "id": "m"}},
        )
    assert r.status_code == 503
    assert "ghost" in r.json()["detail"]
    assert calls == []
