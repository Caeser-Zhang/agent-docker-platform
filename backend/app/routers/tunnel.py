"""Tunnel routes — the data plane.

The platform implements **no** agent logic. Everything the browser asks for is
served by `opencode serve` running inside the user's own container; this module
is the authenticated reverse proxy in between:

    Browser ──JWT──▶ FastAPI ──agent-net──▶ http://agent-{uid}:4096/<opencode path>

Endpoints:
  ANY /api/tunnel/oc/{path}   transparent proxy onto opencode's HTTP API
  GET /api/tunnel/events      SSE fan-out of opencode's GET /api/event
  GET /api/tunnel/providers   convenience view over opencode's GET /config

Everything the frontend needs (sessions, prompts, model switching, agents,
files) goes through /oc/ using opencode's real routes and payloads, so the
platform never has to be updated when opencode adds a capability.
"""
import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import get_current_user, get_current_user_from_token
from ..models import User
from ..services.agent_controller import agent_controller
from ..services.container_manager import container_manager
from ..services.opencode_config import describe_source
from ..services.sse_pump import sse_pump_manager
from ..services.tunnel_relay import tunnel_relay

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tunnel", tags=["tunnel"])

# Proxied verbs. opencode uses all of these across its API surface.
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Paths the browser must never be able to reach through the tunnel: they would
# let a user rewrite the injected credentials or shut the server down from
# inside the sandbox.
BLOCKED_PREFIXES = (
    "global/dispose",
    "instance/dispose",
    "global/upgrade",
    "global/config",
    "auth/",
)

# Hop-by-hop and length headers must not be copied onto our own response.
STRIPPED_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "server",
    "date",
}


async def _require_agent(user: User) -> tuple[User, str]:
    """Ensure the user's container is running; return (user, container password)."""
    status = await agent_controller.get_status(user.id)
    if not status["running"]:
        raise HTTPException(
            status_code=503,
            detail="Agent not running. Please start the agent first.",
        )

    from sqlalchemy import select

    from ..database import async_session
    from ..models import AgentContainer

    async with async_session() as db:
        result = await db.execute(
            select(AgentContainer).where(AgentContainer.user_id == user.id)
        )
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=503, detail="Agent container record not found")

    return user, record.password_enc


# ------------------------------------------------------------------
#  Transparent proxy onto the container's opencode server
# ------------------------------------------------------------------

@router.api_route(
    "/oc/{oc_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_opencode(oc_path: str, req: Request, user: User = Depends(get_current_user)):
    """Forward the request verbatim to opencode inside the user's container.

    The path segment after /oc/ is opencode's own path, e.g.
      /api/tunnel/oc/api/session                    -> POST /api/session
      /api/tunnel/oc/api/session/ses_x/prompt       -> POST /api/session/ses_x/prompt
      /api/tunnel/oc/api/session/ses_x/model        -> POST /api/session/ses_x/model
      /api/tunnel/oc/config                         -> GET  /config
    """
    method = req.method.upper()
    if method not in ALLOWED_METHODS:
        raise HTTPException(status_code=405, detail=f"Method {method} not allowed")

    normalized = oc_path.lstrip("/")
    if any(normalized.startswith(prefix) for prefix in BLOCKED_PREFIXES):
        raise HTTPException(status_code=403, detail=f"Path /{normalized} is not proxied")

    user, password = await _require_agent(user)

    # Raw bytes passthrough — do NOT parse and re-serialize the body.
    # opencode's session runner is sensitive to exact body content; the
    # previous json.loads → json= round-trip was the root cause of
    # "Model unavailable" errors through the proxy.
    raw_body = await req.body()
    raw_body = raw_body if raw_body else None

    # Forward content-type so opencode knows how to parse the body.
    fwd_headers = {"content-type": req.headers.get("content-type", "application/json")}

    params = dict(req.query_params)
    # Prompts can legitimately run for minutes; everything else should be quick.
    timeout = 300.0 if normalized.endswith("/prompt") else 60.0

    result = await tunnel_relay.http_request(
        user_id=user.id,
        method=method,
        path=f"/{normalized}",
        raw_body=raw_body,
        params=params or None,
        password=password,
        timeout=timeout,
        headers=fwd_headers,
    )

    # Any interaction keeps the container out of the idle reclaimer.
    if method != "GET":
        await agent_controller.update_activity(user.id)

    payload = result.get("body")
    status = result.get("status", 502)
    if isinstance(payload, (dict, list)):
        return JSONResponse(status_code=status, content=payload)
    return Response(
        status_code=status,
        content=payload if isinstance(payload, (str, bytes)) else "",
        media_type=result.get("headers", {}).get("content-type", "text/plain"),
    )


# ------------------------------------------------------------------
#  Provider / model catalogue — for the "switch LLM" bar
# ------------------------------------------------------------------

@router.get("/providers")
async def list_providers(user: User = Depends(get_current_user)):
    """Flatten opencode's effective config into provider/model options.

    Source of truth is the container's own `GET /config`, i.e. exactly what
    opencode will use when it makes the call — not a platform-side copy.
    """
    user, password = await _require_agent(user)
    result = await tunnel_relay.http_request(
        user_id=user.id, method="GET", path="/config", password=password, timeout=15,
    )
    if result.get("status") != 200 or not isinstance(result.get("body"), dict):
        return {
            "providers": [],
            "default": None,
            "error": f"opencode /config returned {result.get('status')}",
        }

    config = result["body"]
    providers = []
    for provider_id, entry in sorted((config.get("provider") or {}).items()):
        models = entry.get("models") or {}
        providers.append({
            "id": provider_id,
            "name": entry.get("name") or provider_id,
            # baseURL is useful context in the UI (e.g. "via local proxy") and
            # contains no secret; apiKey is deliberately never returned.
            "baseURL": (entry.get("options") or {}).get("baseURL"),
            "models": [
                {"id": model_id, "name": (spec or {}).get("name") or model_id}
                for model_id, spec in sorted(models.items())
            ],
        })

    return {
        "providers": providers,
        "default": config.get("model"),
        "smallModel": config.get("small_model"),
        "source": describe_source(),
    }


@router.post("/config/reload")
async def reload_config(user: User = Depends(get_current_user)):
    """Re-inject the host opencode.json and restart the container.

    Used after the developer edits credentials on the host machine.
    """
    ok = await container_manager.reload_config(user.id)
    if not ok:
        raise HTTPException(status_code=503, detail="No container to reload")
    # The restart drops the upstream SSE connection; bring the pump back.
    await agent_controller.restart_pump(user.id)
    return {"reloaded": True, "source": describe_source()}


# ------------------------------------------------------------------
#  SSE Event stream — relay container events to browser
# ------------------------------------------------------------------

@router.get("/events")
async def tunnel_events(
    token: str = Query(..., description="JWT token (EventSource can't send headers)"),
    last_event_id: int = Query(0, alias="lastEventId"),
):
    """SSE stream: relay opencode's events from the container to the browser.

    EventSource cannot send an Authorization header, so the JWT arrives as a
    query parameter. Events are buffered in a 200-entry ring so a brief
    disconnect can replay what it missed via lastEventId.
    """
    user = await get_current_user_from_token(token)

    bus = sse_pump_manager.get_bus(user.id)
    if not bus:
        async def no_agent():
            yield 'data: {"type":"agent.disconnected","data":{"message":"Agent not running"}}\n\n'
        return StreamingResponse(no_agent(), media_type="text/event-stream")

    queue = await bus.subscribe()

    async def event_stream():
        try:
            for event in bus.replay_after(last_event_id):
                yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    # Keep proxies and the browser from timing the stream out.
                    yield ": keep-alive\n\n"
                    continue
                yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
            "connection": "keep-alive",
        },
    )
