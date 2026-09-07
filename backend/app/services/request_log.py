"""Request access log — one row per tunnel-proxied call, in request_logs.

opencode 1.x emits no HTTP request logs of its own, so the platform records
them at the tunnel proxy (the single choke point every browser→container
request passes through). Same never-break-the-observed-call semantics as
audit.py: a failed write logs a warning and nothing else.
"""
import logging

from sqlalchemy import delete, select

from ..database import async_session
from ..models import RequestLog

logger = logging.getLogger(__name__)

# Global retention cap. Pruning runs on every PRUNE_EVERY-th write to keep
# steady-state overhead near zero; rows are cheap but unbounded growth is not.
MAX_ROWS = 20_000
PRUNE_EVERY = 200

_writes_since_prune = 0


async def record_request(
    user_id: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
) -> None:
    """Persist one proxied request. Never raises."""
    global _writes_since_prune
    try:
        async with async_session() as db:
            db.add(RequestLog(
                user_id=user_id,
                method=method,
                path=path[:500],
                status_code=status_code,
                duration_ms=duration_ms,
            ))
            await db.commit()
        _writes_since_prune += 1
        if _writes_since_prune >= PRUNE_EVERY:
            _writes_since_prune = 0
            await prune()
    except Exception:  # noqa: BLE001
        logger.warning("Request log write failed (%s %s)", method, path, exc_info=True)


async def prune() -> None:
    """Keep only the newest MAX_ROWS rows (by id, i.e. insert order)."""
    try:
        async with async_session() as db:
            keep_ids = select(RequestLog.id).order_by(
                RequestLog.id.desc()
            ).limit(MAX_ROWS).scalar_subquery()
            await db.execute(delete(RequestLog).where(RequestLog.id.not_in(keep_ids)))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Request log prune failed", exc_info=True)


async def list_request_logs(user_id: str, limit: int = 200) -> list[dict]:
    """Most recent request logs for one user, newest first."""
    async with async_session() as db:
        rows = (await db.execute(
            select(RequestLog)
            .where(RequestLog.user_id == user_id)
            .order_by(RequestLog.id.desc())
            .limit(limit)
        )).scalars().all()
    return [
        {
            "id": r.id,
            "method": r.method,
            "path": r.path,
            "status_code": r.status_code,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
