"""In-memory sliding-window rate limiter.

Single-process by design — the backend runs as one uvicorn worker behind
nginx, so a process-local table is sufficient for auth throttling. Entries
age out lazily on access; a full sweep runs at most once a minute.
"""
import time
from collections import defaultdict


class SlidingWindowLimiter:
    """Fixed-key sliding window: at most ``limit`` hits per ``window`` seconds."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._last_sweep = 0.0

    def hit(self, key: str, limit: int, window: float) -> float:
        """Record one attempt for ``key``.

        Returns 0.0 when the attempt is allowed, otherwise the number of
        seconds until the oldest recorded hit ages out of the window.
        """
        now = time.monotonic()
        self._sweep(now)
        queue = self._hits[key]
        cutoff = now - window
        while queue and queue[0] < cutoff:
            queue.pop(0)
        if len(queue) >= limit:
            return max(0.0, window - (now - queue[0]))
        queue.append(now)
        return 0.0

    def reset(self, key: str) -> None:
        """Clear a key's history — called after a successful login."""
        self._hits.pop(key, None)

    def _sweep(self, now: float) -> None:
        # Opportunistic GC so abandoned keys (e.g. rotated IPs) don't grow
        # the table without bound.
        if now - self._last_sweep < 60.0:
            return
        self._last_sweep = now
        stale = [k for k, q in self._hits.items() if not q]
        for k in stale:
            self._hits.pop(k, None)


login_limiter = SlidingWindowLimiter()
register_limiter = SlidingWindowLimiter()
