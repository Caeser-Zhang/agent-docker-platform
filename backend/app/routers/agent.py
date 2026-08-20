"""Agent routes — container lifecycle management (control plane).

Endpoints:
  GET  /api/agent/status   — check if agent is running
  POST /api/agent/start    — start the agent container
  POST /api/agent/stop     — stop the agent container
  GET  /api/agent/logs     — fetch container logs

(Platform-wide container listing moved to /api/admin/containers — admin only.)
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..models import User
from ..schemas import StartAgentRequest, AgentStatusResponse
from ..config import settings
from ..services.agent_controller import agent_controller
from ..services.container_manager import container_manager
from ..services.opencode_config import describe_source

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/runtime")
async def agent_runtime(user: User = Depends(get_current_user)):
    """Describe what actually runs inside the container.

    Makes the architecture legible in the UI: the platform starts nothing but
    `opencode serve`, and this is the configuration it was handed.
    """
    return {
        "runtime": "opencode serve",
        "image": settings.agent_image,
        "port": settings.agent_port,
        "workdir": settings.agent_workdir,
        "network": settings.agent_network,
        "config": describe_source(),
    }


@router.get("/status", response_model=AgentStatusResponse)
async def agent_status(user: User = Depends(get_current_user)):
    """Check if the user's agent container is running and healthy."""
    return await agent_controller.get_status(user.id)


@router.post("/start", response_model=AgentStatusResponse)
async def start_agent(req: StartAgentRequest, user: User = Depends(get_current_user)):
    """Start the agent container for this user.

    Creates and starts a hardened Docker container if one doesn't exist,
    or restarts an existing stopped container. Idempotent.
    """
    result = await agent_controller.start_for_user(user.id, req.workspace)
    return AgentStatusResponse(**result)


@router.post("/stop", response_model=AgentStatusResponse)
async def stop_agent(user: User = Depends(get_current_user)):
    """Stop the agent container gracefully (preserves volumes)."""
    result = await agent_controller.stop_for_user(user.id)
    return AgentStatusResponse(
        running=False,
        status="stopped",
        message=result.get("message", "Agent stopped"),
    )


@router.get("/logs")
async def agent_logs(user: User = Depends(get_current_user)):
    """Fetch recent container logs for debugging."""
    logs = container_manager.get_container_logs(user.id, tail=100)
    return {"logs": logs}
