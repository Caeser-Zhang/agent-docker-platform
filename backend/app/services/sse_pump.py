"""SSE Pump — continuously reads SSE events from a container and pushes to subscribers.

Each running container gets one pump task that:
  1. Connects to opencode's GET /global/event SSE endpoint inside the container
  2. Unwraps each envelope and parses the event (including `message.part.delta`
     streaming chunks)
  3. Pushes them to all subscribed browser clients via asyncio.Queue
  4. Reconnects automatically on disconnect

One pump per container rather than one upstream connection per browser tab:
opencode's event stream is global to the server, so fanning out here keeps the
container's connection count at exactly one and gives us a replay buffer for
browser reconnects.
"""
import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ContainerEventBus:
    """Per-container event bus: ring buffer + subscriber queues."""

    user_id: str
    base_url: str
    auth: tuple[str, str]
    _events: deque = field(default_factory=lambda: deque(maxlen=200))
    _subscribers: set = field(default_factory=set)
    # Server-side observers tapped on every event (metrics collection). Each is
    # called with (user_id, event_with_seq) and MUST NOT raise into the pump —
    # exceptions are swallowed per-observer so a bad tap can never stall the fan-out.
    _observers: list = field(default_factory=list)
    _pump_task: asyncio.Task | None = None
    # Monotonic sequence. It must NOT be derived from len(self._events): the
    # deque is bounded, so once it is full len() stops growing and every event
    # would be handed out the same id, breaking lastEventId replay.
    _seq: int = 0

    def push_event(self, event: dict):
        """Push an event to the ring buffer and all subscribers."""
        self._seq += 1
        seq = self._seq
        # opencode's own event id (evt_...) is preserved as `event_id`; `id` is
        # the platform's replay cursor.
        event_with_seq = {**event, "event_id": event.get("id"), "id": seq}
        self._events.append(event_with_seq)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event_with_seq)
            except asyncio.QueueFull:
                pass  # drop if subscriber is too slow
        # Server-side taps (e.g. UX metrics collector). Never let one break the fan-out.
        for obs in list(self._observers):
            try:
                obs(self.user_id, event_with_seq)
            except Exception:  # noqa: BLE001
                logger.warning("SSE observer failed for user %s", self.user_id, exc_info=True)

    def replay_after(self, last_id: int) -> list[dict]:
        """Return events with id > last_id (for reconnection)."""
        return [e for e in self._events if e["id"] > last_id]

    async def subscribe(self) -> asyncio.Queue:
        """Subscribe to events. Returns a queue that receives event dicts."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)


class SSEPumpManager:
    """Manages SSE pump tasks for all running containers."""

    def __init__(self):
        self._buses: dict[str, ContainerEventBus] = {}
        self._lock = asyncio.Lock()

    async def start_pump(self, user_id: str, base_url: str, auth: tuple[str, str]):
        """Start an SSE pump for a container. Idempotent — stops existing first.

        NOTE: stop_pump() acquires self._lock internally. We must NOT hold the
        lock here while calling it, otherwise asyncio.Lock (non-reentrant) deadlocks
        and the caller (agent start) hangs forever.
        """
        # Stop any existing pump first (stop_pump manages its own lock).
        await self.stop_pump(user_id)

        async with self._lock:
            bus = ContainerEventBus(
                user_id=user_id,
                base_url=base_url,
                auth=auth,
            )
            # Register the UX metrics tap (local import avoids a circular dep).
            from .metrics_collector import metrics_collector
            bus._observers.append(metrics_collector.observe)
            bus._pump_task = asyncio.create_task(self._pump_loop(bus))
            self._buses[user_id] = bus
            logger.info("SSE pump started for user %s", user_id)

    async def stop_pump(self, user_id: str):
        """Stop the SSE pump for a user."""
        async with self._lock:
            bus = self._buses.pop(user_id, None)
            if bus:
                if bus._pump_task and not bus._pump_task.done():
                    bus._pump_task.cancel()
                    try:
                        await asyncio.wait_for(bus._pump_task, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                logger.info("SSE pump stopped for user %s", user_id)

    def get_bus(self, user_id: str) -> ContainerEventBus | None:
        return self._buses.get(user_id)

    async def _pump_loop(self, bus: ContainerEventBus):
        """Continuously read SSE from the container and push to subscribers.

        Reconnects automatically on connection drops. This is the core
        event relay between the container and all browser clients.

        The loop exits only via task cancellation (stop_pump / shutdown).
        Every failure path — connect error, non-200 status, mid-stream
        protocol error, clean EOF — funnels into a single bounded
        exponential backoff at the bottom, so the connection is always
        retried. (An earlier version gated the loop on a `healthy` flag
        with `continue` at the top; once an error cleared the flag the
        probe lived in an unreachable branch and the loop spun forever
        without ever reconnecting.)
        """
        # opencode v1.18.x exposes two event surfaces:
        #   /event        — instance-scoped. The instance is resolved from the
        #                   `directory` query param / `x-opencode-directory`
        #                   header, defaulting to the server's cwd (/workspace).
        #                   A session created in a project subdirectory belongs
        #                   to a *different* instance, so its bus-local events
        #                   (notably `message.part.delta`) never reach a
        #                   subscriber on the /workspace instance — project
        #                   sessions appeared to stream nothing while global
        #                   ones worked.
        #   /global/event — server-wide fan-out of every instance's bus, each
        #                   event wrapped as {directory, project, payload}.
        # One container hosts many project directories, so this is the only
        # surface that sees all of them.
        url = f"{bus.base_url}/global/event"
        backoff = 1.0
        max_backoff = 15.0
        while True:
            received = False
            try:
                async with httpx.AsyncClient(
                    auth=bus.auth,
                    timeout=httpx.Timeout(None, connect=5),
                ) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            logger.warning(
                                "SSE upstream returned %s for user %s",
                                resp.status_code, bus.user_id,
                            )
                        else:
                            event_data = ""
                            async for line in resp.aiter_lines():
                                received = True
                                line = line.rstrip("\r\n")
                                if line.startswith("data:"):
                                    event_data = line.split(":", 1)[1].strip()
                                elif line == "" and event_data:
                                    raw, event_data = event_data, ""
                                    try:
                                        envelope = json.loads(raw)
                                    except json.JSONDecodeError:
                                        continue
                                    if not isinstance(envelope, dict):
                                        continue
                                    # /global/event wraps every event as
                                    # {directory, project, workspace, payload};
                                    # subscribers want the bare Event, which is
                                    # shaped exactly like the old /event one.
                                    event = envelope.get("payload")
                                    if isinstance(event, dict):
                                        bus.push_event(event)
            except asyncio.CancelledError:
                logger.info("SSE pump cancelled for user %s", bus.user_id)
                raise
            except Exception as e:
                # ConnectError / ReadError / RemoteProtocolError / ... — all
                # mean the upstream is unreachable right now. Retry later.
                logger.warning(
                    "SSE connection lost for user %s (%s: %s) — retrying in %.1fs",
                    bus.user_id, type(e).__name__, e, backoff,
                )
            # Real traffic flowed on this connection before it ended, so the
            # server was alive — reconnect fast instead of growing the backoff.
            # opencode severs the stream ~every second during agent activity,
            # so a 1s sleep here would drop every event emitted in the gap;
            # 0.2s keeps the loss window minimal.
            if received:
                backoff = 0.2
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def stop_all(self):
        """Stop all SSE pumps (called on shutdown)."""
        user_ids = list(self._buses.keys())
        for uid in user_ids:
            await self.stop_pump(uid)


# Global singleton
sse_pump_manager = SSEPumpManager()
