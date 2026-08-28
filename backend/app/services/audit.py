"""P1-6: audit event recording — durable lifecycle trail in the audit_events table.

Kept separate from agent_controller so any module can record events without
importing the controller (avoids circular imports). Fire-and-forget semantics:
auditing must never break the lifecycle operation it observes.
"""
import json
import logging

from sqlalchemy import select

from ..database import async_session
from ..models import AuditEvent

logger = logging.getLogger(__name__)


async def log_audit(user_id: str | None, action: str, detail: dict | None = None) -> None:
    """Persist one audit event. Never raises — failures only log a warning."""
    try:
        async with async_session() as db:
            db.add(AuditEvent(
                user_id=user_id,
                action=action,
                detail=json.dumps(detail or {}, ensure_ascii=False, default=str),
            ))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Audit log write failed (%s / %s)", user_id, action, exc_info=True)


async def list_audit(limit: int = 100) -> list[dict]:
    """Most recent audit events, newest first (admin panel consumption)."""
    async with async_session() as db:
        rows = (await db.execute(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
        )).scalars().all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "action": r.action,
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
