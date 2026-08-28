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
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import async_session, get_db
from ..models import User
from ..services import user_config
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


def _normalize_tunnel_path(raw: str) -> str | None:
    """Resolve a proxied path to canonical, dot-free segments.

    Returns the slash-joined canonical path, or None when the path tries to
    escape above the root (`..` underflow). A plain `startswith` blacklist
    can be bypassed with dot segments — `./global/config` or
    `foo/../global/config` pass the check, and httpx then re-normalises the
    path on the wire into the blocked route.
    """
    stack: list[str] = []
    for seg in raw.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not stack:
                return None
            stack.pop()
        else:
            stack.append(seg)
    return "/".join(stack)


def _is_blocked(canonical: str) -> bool:
    """Segment-level blacklist match (no `global/configX` false positives)."""
    segs = canonical.split("/") if canonical else []
    for prefix in BLOCKED_PREFIXES:
        blocked = prefix.rstrip("/").split("/")
        if segs[: len(blocked)] == blocked:
            return True
    return False

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
    """Ensure the user's container is running; return (user, container password).

    Uses the controller's TTL-cached gate (P0-1b): this runs on EVERY
    proxied request, and the old uncached path cost 2 Docker inspects +
    2 DB queries per request.
    """
    running, password = await agent_controller.get_agent_gate(user.id)
    if not running or password is None:
        raise HTTPException(
            status_code=503,
            detail="Agent not running. Please start the agent first.",
        )
    return user, password


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

    # Canonicalise BEFORE the blacklist check, and forward the canonical
    # form — the blacklist and the wire path must see the same string.
    normalized = _normalize_tunnel_path(oc_path)
    if normalized is None:
        raise HTTPException(status_code=400, detail="Path escapes the tunnel root")
    if _is_blocked(normalized):
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
async def reload_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-inject the opencode.json and restart the container.

    Used after the developer edits credentials on the host machine, or after
    a user changes their own providers / MCP servers / active LLM selection.
    """
    config_json = await user_config.build_user_config_json(db, user.id)
    ok = await container_manager.reload_config(user.id, config_json)
    if not ok:
        raise HTTPException(status_code=503, detail="No container to reload")
    # The restart drops the upstream SSE connection; bring the pump back.
    await agent_controller.restart_pump(user.id)
    return {"reloaded": True, "source": describe_source()}


# ------------------------------------------------------------------
#  SSE one-time tickets (P1-5)
# ------------------------------------------------------------------

# EventSource cannot send an Authorization header. Previously the long-lived
# JWT rode along in the query string, where it leaks into proxy/access logs
# and browser history. The browser now exchanges its JWT for a short-lived,
# single-use ticket and puts that in the URL instead. In-memory store (the
# backend runs as a single worker — same assumption as the rate limiter).
SSE_TICKET_TTL = 60  # seconds
_sse_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (user_id, expires_at)


def _issue_sse_ticket(user_id: str) -> str:
    """Mint a one-time ticket bound to user_id, sweeping expired ones."""
    now = time.monotonic()
    for k in [k for k, (_, exp) in _sse_tickets.items() if exp < now]:
        _sse_tickets.pop(k, None)
    ticket = secrets.token_urlsafe(32)
    _sse_tickets[ticket] = (user_id, now + SSE_TICKET_TTL)
    return ticket


def _redeem_sse_ticket(ticket: str) -> str:
    """Consume a ticket and return its user_id; 401 when invalid/expired."""
    entry = _sse_tickets.pop(ticket, None)  # pop = single use
    if entry is None or entry[1] < time.monotonic():
        raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")
    return entry[0]


@router.post("/ticket")
async def issue_sse_ticket(user: User = Depends(get_current_user)):
    """Exchange the caller's JWT for a one-time, 60-second SSE ticket.

    The ticket is what the browser's EventSource then puts in its URL —
    so a leaked URL is only good for one stream attach within 60s, unlike
    the JWT which was valid for a full day.
    """
    return {"ticket": _issue_sse_ticket(user.id), "expires_in": SSE_TICKET_TTL}


# ------------------------------------------------------------------
#  SSE Event stream — relay container events to browser
# ------------------------------------------------------------------

@router.get("/events")
async def tunnel_events(
    ticket: str = Query(..., description="One-time ticket from POST /api/tunnel/ticket"),
    last_event_id: int = Query(0, alias="lastEventId"),
):
    """SSE stream: relay opencode's events from the container to the browser.

    EventSource cannot send an Authorization header, so the browser first
    exchanges its JWT for a one-time ticket (P1-5) and passes that here.
    Events are buffered in a 200-entry ring so a brief disconnect can replay
    what it missed via lastEventId.
    """
    user_id = _redeem_sse_ticket(ticket)
    # Same DB hit the old JWT path did, just keyed off the ticket.
    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")

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
