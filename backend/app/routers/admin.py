"""Admin routes — platform-wide Docker container management (admin role only).

Every endpoint requires role="admin" (checked against the database, not the
JWT, so demotion takes effect on the next request). The admin panel in the
frontend is the only consumer of these routes.

Endpoints:
  GET  /api/admin/overview                       — platform statistics
  GET  /api/admin/containers                     — all users' containers
                                                    (DB records merged with
                                                    live Docker state + stats)
  GET  /api/admin/containers/{user_id}/logs      — container logs
  POST /api/admin/containers/{user_id}/restart   — restart in place
  POST /api/admin/containers/{user_id}/stop      — graceful stop (keeps volumes)
  POST /api/admin/containers/{user_id}/destroy   — remove container AND volumes
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from ..auth import require_admin
from ..config import settings
from ..database import async_session
from ..models import AgentContainer, User
from ..services.agent_controller import agent_controller
from ..services.container_manager import container_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/overview")
async def admin_overview():
    """Platform-wide statistics for the admin dashboard."""
    async with async_session() as db:
        users = (await db.execute(select(User))).scalars().all()
        records = (await db.execute(select(AgentContainer))).scalars().all()

    by_status: dict[str, int] = {}
    for r in records:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    docker_list = await asyncio.to_thread(container_manager.list_all_containers)
    docker_running = sum(1 for c in docker_list if c["status"] == "running")

    return {
        "users": {
            "total": len(users),
            "admins": sum(1 for u in users if u.role == "admin"),
        },
        "containers": {
            "records": len(records),
            "by_status": by_status,
            "docker_total": len(docker_list),
            "docker_running": docker_running,
        },
        "platform": {
            "image": settings.agent_image,
            "network": settings.agent_network,
            "port": settings.agent_port,
            "cpu_limit": settings.container_cpu_limit,
            "memory_limit": settings.container_memory_limit,
        },
    }


@router.get("/containers")
async def admin_containers(stats: bool = Query(True, description="Include CPU/memory sampling (slower)")):
    """List every user container, merging DB records with live Docker state.

    Rows come from both sources:
      - Docker containers labelled managed-by=agent-platform (ground truth)
      - agent_containers DB records (adds username, last activity, errors);
        records without a Docker container appear as docker_status="absent"
    """
    async with async_session() as db:
        users = (await db.execute(select(User))).scalars().all()
        records = (await db.execute(select(AgentContainer))).scalars().all()

    user_by_id = {u.id: u for u in users}
    record_by_user = {r.user_id: r for r in records}

    docker_list = await asyncio.to_thread(container_manager.list_all_containers)

    rows: list[dict] = []
    seen_users: set[str] = set()

    for dc in docker_list:
        uid = dc["user_id"]
        seen_users.add(uid)
        rec = record_by_user.get(uid)
        user = user_by_id.get(uid)
        rows.append({
            "user_id": uid,
            "username": user.username if user else None,
            "container_name": dc["name"],
            "db_status": rec.status if rec else "unmanaged",
            "docker_status": dc["status"],
            "health": dc.get("health"),
            "image": dc.get("image") or (rec.image if rec else settings.agent_image),
            "started_at": dc.get("started_at"),
            "last_activity": rec.last_activity.isoformat() if rec and rec.last_activity else None,
            "restart_count": rec.restart_count if rec else 0,
            "last_error": rec.last_error if rec else None,
        })

    # DB records whose container no longer exists in Docker (stopped and
    # removed, or destroyed) — still relevant for the admin view.
    for uid, rec in record_by_user.items():
        if uid in seen_users:
            continue
        user = user_by_id.get(uid)
        rows.append({
            "user_id": uid,
            "username": user.username if user else None,
            "container_name": rec.container_name,
            "db_status": rec.status,
            "docker_status": "absent",
            "health": None,
            "image": rec.image,
            "started_at": rec.started_at.isoformat() if rec.started_at else None,
            "last_activity": rec.last_activity.isoformat() if rec.last_activity else None,
            "restart_count": rec.restart_count,
            "last_error": rec.last_error,
        })

    # Optional live resource sampling — parallelised because each sample
    # blocks ~1-2s waiting for the daemon's second reading.
    if stats:
        running_rows = [r for r in rows if r["docker_status"] == "running"]
        if running_rows:
            samples = await asyncio.gather(*[
                asyncio.to_thread(container_manager.get_container_stats, r["user_id"])
                for r in running_rows
            ])
            for row, sample in zip(running_rows, samples):
                row["stats"] = sample

    rows.sort(key=lambda r: (r["username"] is None, r["username"] or "", r["container_name"]))
    return {"containers": rows}


def _require_record(user_id: str) -> AgentContainer:
    """404 when the user has no container record at all."""
    # Synchronous pre-check via the container manager: the Docker daemon is
    # the ground truth for "does anything exist to operate on".
    container = container_manager.get_container(user_id)
    if container is None:
        raise HTTPException(status_code=404, detail="No Docker container for this user")
    return container


@router.get("/containers/{user_id}/logs")
async def admin_container_logs(user_id: str, tail: int = Query(200, ge=1, le=2000)):
    """Fetch the last `tail` lines of a user's container logs."""
    _require_record(user_id)
    logs = await asyncio.to_thread(container_manager.get_container_logs, user_id, tail)
    return {"user_id": user_id, "tail": tail, "logs": logs}


@router.post("/containers/{user_id}/restart")
async def admin_restart_container(user_id: str):
    """Restart the container in place and re-attach its SSE pump."""
    _require_record(user_id)
    result = await agent_controller.restart_for_user(user_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Restart failed"))
    return result


@router.post("/containers/{user_id}/stop")
async def admin_stop_container(user_id: str):
    """Gracefully stop the container (volumes are preserved)."""
    _require_record(user_id)
    result = await agent_controller.stop_for_user(user_id)
    return {"ok": True, "message": result.get("message", "Container stopped")}


@router.post("/containers/{user_id}/destroy")
async def admin_destroy_container(user_id: str):
    """Destroy the container AND its volumes — irreversible."""
    _require_record(user_id)
    await agent_controller.destroy_for_user(user_id)
    logger.warning("Admin destroyed container and volumes for user %s", user_id)
    return {"ok": True, "message": "Container and volumes destroyed"}
