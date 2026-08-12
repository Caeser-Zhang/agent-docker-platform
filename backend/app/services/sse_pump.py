"""SSE Pump — continuously reads SSE events from a container and pushes to subscribers.

Each running container gets one pump task that:
  1. Connects to opencode's GET /api/event SSE endpoint inside the container
  2. Parses incoming events (opencode emits `session.next.*`, `server.connected`, ...)
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
    healthy: bool = False
    _events: deque = field(default_factory=lambda: deque(maxlen=200))
    _subscribers: set = field(default_factory=set)
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
                healthy=True,
            )
            bus._pump_task = asyncio.create_task(self._pump_loop(bus))
            self._buses[user_id] = bus
            logger.info("SSE pump started for user %s", user_id)

    async def stop_pump(self, user_id: str):
        """Stop the SSE pump for a user."""
        async with self._lock:
            bus = self._buses.pop(user_id, None)
            if bus:
                bus.healthy = False
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
        """
        url = f"{bus.base_url}/api/event"
        while True:
            if not bus.healthy:
                await asyncio.sleep(2)
                continue
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
                            await asyncio.sleep(3)
                            continue
                        event_data = ""
                        async for line in resp.aiter_lines():
                            line = line.rstrip("\r\n")
                            if line.startswith("data:"):
                                event_data = line.split(":", 1)[1].strip()
                            elif line == "" and event_data:
                                try:
                                    evt = json.loads(event_data)
                                    bus.push_event(evt)
                                except json.JSONDecodeError:
                                    pass
                                event_data = ""
                # Stream ended cleanly (opencode restarted / client closed it).
                # Back off briefly so a flapping server can't spin this loop.
                await asyncio.sleep(1)
            except httpx.ConnectError:
                bus.healthy = False
                logger.warning("SSE connection lost for user %s — will retry", bus.user_id)
                await asyncio.sleep(3)
                # Try to reconnect
                try:
                    async with httpx.AsyncClient(auth=bus.auth, timeout=3) as c:
                        r = await c.get(f"{bus.base_url}/api/health")
                        if r.status_code == 200:
                            bus.healthy = True
                            logger.info("SSE reconnected for user %s", bus.user_id)
                except Exception:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SSE pump error for user %s: %s", bus.user_id, e)
                await asyncio.sleep(3)

    async def stop_all(self):
        """Stop all SSE pumps (called on shutdown)."""
        user_ids = list(self._buses.keys())
        for uid in user_ids:
            await self.stop_pump(uid)


# Global singleton
sse_pump_manager = SSEPumpManager()
