"""Agent Controller — the control plane for container lifecycle management.

Implements the state machine from design section 3.1:
  Absent → Creating → Starting → Running → Idle → Stopped → Destroyed
                                       ↘ Failed ↗

Responsibilities:
  - Start/stop/destroy containers for users
  - Health probing (platform-level, complements Docker HEALTHCHECK)
  - State persistence to the agent_containers table
  - SSE pump lifecycle management
  - Idempotent operations (safe to call multiple times)
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import async_session
from ..models import AgentContainer, User
from .container_manager import container_manager
from .sse_pump import sse_pump_manager
from .tunnel_relay import tunnel_relay

logger = logging.getLogger(__name__)


class AgentController:
    """Orchestrates container lifecycle with DB-backed state.

    Replaces the in-memory dict approach — state survives platform restarts.
    On startup, recover() scans the DB and re-attaches to running containers.
    """

    # ------------------------------------------------------------------
    #  Start — the main entry point when a user enters the platform
    # ------------------------------------------------------------------

    async def start_for_user(self, user_id: str, workspace: str | None = None) -> dict:
        """Ensure the user's agent container is running.

        Flow (design section 8.1):
          1. Check DB for existing container record
          2. If running → return immediately
          3. If absent/stopped → ensure_container (create or start)
          4. Health probe (≤60s)
          5. Start SSE pump
          6. Update DB status → running
        """
        # Check if already running
        record = await self._get_container_record(user_id)
        if record and record.status == "running":
            # Verify it's actually healthy
            if container_manager.is_healthy(user_id):
                # The container survived, but the pump may not have: it dies if
                # the backend was restarted between recover() runs or if the
                # upstream stream was closed. Without it the browser gets a
                # healthy status and a permanently silent event stream.
                if sse_pump_manager.get_bus(user_id) is None:
                    logger.info("Container for %s healthy but pump missing — re-attaching", user_id)
                    await sse_pump_manager.start_pump(
                        user_id,
                        container_manager.get_container_url(user_id),
                        ("opencode", record.password_enc),
                    )
                return {
                    "running": True,
                    "healthy": True,
                    "status": "running",
                    "container_name": record.container_name,
                    "message": "Agent is already running",
                }

        # Update status → creating
        await self._update_status(user_id, "creating")

        try:
            # Ensure the Docker container exists and is started (idempotent)
            container, password = await container_manager.ensure_container(user_id)
            container_name = container.name

            # Persist or update the container record
            ws_vol = container_manager._workspace_volume_name(user_id)
            data_vol = container_manager._data_volume_name(user_id)
            await self._upsert_record(
                user_id=user_id,
                container_name=container_name,
                password=password,
                workspace_volume=ws_vol,
                data_volume=data_vol,
                status="starting",
            )

            # Health probe — wait for the container to be ready
            await self._update_status(user_id, "starting")
            healthy = await self._health_probe(user_id, password)

            if not healthy:
                await self._update_status(user_id, "failed", error="Health probe failed within 120s")
                return {
                    "running": False,
                    "healthy": False,
                    "status": "failed",
                    "container_name": container_name,
                    "message": "Agent failed to start within timeout",
                }

            # opencode's HTTP server is up, but the @ai-sdk provider packages
            # are still initialising in the background. If a session is created
            # during that window, the first model-resolve (title generation)
            # races with provider init and fails with "Model unavailable",
            # poisoning that session's runner. A short settle delay lets the
            # provider finish loading before the user starts chatting.
            await asyncio.sleep(5)

            # Start SSE pump
            base_url = container_manager.get_container_url(user_id)
            await sse_pump_manager.start_pump(user_id, base_url, ("opencode", password))

            # Update DB → running
            now = datetime.now(timezone.utc)
            await self._update_status(
                user_id, "running",
                started_at=now,
                last_activity=now,
                error=None,
            )

            return {
                "running": True,
                "healthy": True,
                "status": "running",
                "container_name": container_name,
                "message": "Agent started successfully",
            }

        except Exception as e:
            logger.error("Failed to start agent for user %s: %s", user_id, e)
            await self._update_status(user_id, "failed", error=str(e))
            return {
                "running": False,
                "healthy": False,
                "status": "failed",
                "container_name": None,
                "message": f"Failed to start agent: {str(e)}",
            }

    # ------------------------------------------------------------------
    #  Stop — graceful shutdown (preserves volumes)
    # ------------------------------------------------------------------

    async def stop_for_user(self, user_id: str):
        """Stop the user's agent container gracefully.

        Flow (design section 6.4):
          1. Cancel SSE pump
          2. Stop the container (10s grace period)
          3. Update DB status → stopped
        """
        # Stop SSE pump first
        await sse_pump_manager.stop_pump(user_id)

        # Gracefully stop the container
        await container_manager.stop_container(user_id, timeout=10)

        # Update DB
        await self._update_status(user_id, "stopped")

        return {"running": False, "message": "Agent stopped"}

    # ------------------------------------------------------------------
    #  Destroy — irreversible (removes container + volumes)
    # ------------------------------------------------------------------

    async def destroy_for_user(self, user_id: str):
        """Destroy the user's container and volumes.

        Per design section 3.2 — should backup volumes before calling this.
        """
        await sse_pump_manager.stop_pump(user_id)
        await container_manager.destroy_container(user_id)
        await self._update_status(user_id, "destroyed")

    # ------------------------------------------------------------------
    #  Pump re-attach — after an out-of-band container restart
    # ------------------------------------------------------------------

    async def restart_pump(self, user_id: str) -> bool:
        """Re-attach the SSE pump after the container was restarted.

        Restarting the container (e.g. to pick up new opencode credentials)
        kills the upstream /api/event connection, so the pump has to be rebuilt
        against the fresh container.
        """
        record = await self._get_container_record(user_id)
        if not record:
            return False
        password = record.password_enc
        if not await self._health_probe(user_id, password):
            logger.warning("Container for %s did not come back healthy", user_id)
            return False
        base_url = container_manager.get_container_url(user_id)
        await sse_pump_manager.start_pump(user_id, base_url, ("opencode", password))
        await self._update_status(user_id, "running", error=None)
        return True

    # ------------------------------------------------------------------
    #  Status query
    # ------------------------------------------------------------------

    async def get_status(self, user_id: str) -> dict:
        """Get the current agent status for a user."""
        record = await self._get_container_record(user_id)
        if not record:
            return {
                "running": False,
                "healthy": False,
                "status": "absent",
                "container_name": None,
                "workspace": None,
            }

        # Check actual container health
        is_healthy = container_manager.is_healthy(user_id)
        is_running = container_manager.get_container_status(user_id) == "running"

        return {
            "running": is_running,
            "healthy": is_healthy,
            "status": record.status if is_running else "stopped",
            "container_name": record.container_name,
            "workspace": None,
        }

    # ------------------------------------------------------------------
    #  Health probe — wait for container to become ready
    # ------------------------------------------------------------------

    async def _health_probe(self, user_id: str, password: str) -> bool:
        """Probe opencode's own GET /api/health until it responds or times out.

        opencode reports `{"healthy": true}` as soon as the HTTP server is
        listening, which happens ~2s after container start.
        """
        deadline = time.time() + settings.startup_timeout
        while time.time() < deadline:
            result = await tunnel_relay.http_request(
                user_id=user_id,
                method="GET",
                path="/api/health",
                password=password,
                timeout=3,
            )
            if result["status"] == 200:
                logger.info("Container for user %s is healthy", user_id)
                return True
            await asyncio.sleep(2)
        return False

    # ------------------------------------------------------------------
    #  Recovery — re-attach to running containers on platform restart
    # ------------------------------------------------------------------

    async def recover(self):
        """Scan DB for running containers and re-attach SSE pumps.

        Called on platform startup. This is the key improvement over the
        in-memory dict approach — platform restarts no longer lose state.
        """
        logger.info("Scanning for running containers to recover...")
        async with async_session() as db:
            result = await db.execute(
                select(AgentContainer).where(
                    AgentContainer.status.in_(["running", "starting", "idle"])
                )
            )
            records = result.scalars().all()

        recovered = 0
        for record in records:
            user_id = record.user_id
            if container_manager.is_healthy(user_id):
                # Container is still running — re-attach SSE pump
                base_url = container_manager.get_container_url(user_id)
                password = record.password_enc  # In production this would be decrypted
                await sse_pump_manager.start_pump(user_id, base_url, ("opencode", password))
                await self._update_status(user_id, "running")
                recovered += 1
                logger.info("Recovered container for user %s", user_id)
            else:
                # Container was running but is now gone — mark as stopped
                await self._update_status(user_id, "stopped")
                logger.warning("Container for user %s was lost — marked stopped", user_id)

        logger.info("Recovery complete: %d containers re-attached", recovered)

    # ------------------------------------------------------------------
    #  Idle reclaim — background task to stop idle containers
    # ------------------------------------------------------------------

    async def idle_reclaim_loop(self):
        """Background task: stop containers idle for more than idle_threshold.

        Runs every idle_reclaim_interval seconds (default 5 minutes).
        Per design section 8.3.
        """
        logger.info("Idle reclaim task started (interval=%ds, threshold=%ds)",
                     settings.idle_reclaim_interval, settings.idle_threshold)
        while True:
            await asyncio.sleep(settings.idle_reclaim_interval)
            try:
                now = datetime.now(timezone.utc)
                async with async_session() as db:
                    result = await db.execute(
                        select(AgentContainer).where(
                            AgentContainer.status == "running"
                        )
                    )
                    records = result.scalars().all()

                for record in records:
                    if record.last_activity:
                        idle_seconds = (now - record.last_activity.replace(tzinfo=timezone.utc)).total_seconds()
                        if idle_seconds > settings.idle_threshold:
                            logger.info("Reclaiming idle container for user %s (idle %ds)",
                                       record.user_id, int(idle_seconds))
                            await self.stop_for_user(record.user_id)
            except Exception as e:
                logger.error("Idle reclaim error: %s", e)

    # ------------------------------------------------------------------
    #  DB helpers
    # ------------------------------------------------------------------

    async def _get_container_record(self, user_id: str) -> AgentContainer | None:
        async with async_session() as db:
            result = await db.execute(
                select(AgentContainer).where(AgentContainer.user_id == user_id)
            )
            return result.scalar_one_or_none()

    async def _update_status(
        self,
        user_id: str,
        status: str,
        started_at: datetime | None = None,
        last_activity: datetime | None = None,
        error: str | None = None,
    ):
        async with async_session() as db:
            values = {"status": status}
            if started_at:
                values["started_at"] = started_at
            if last_activity:
                values["last_activity"] = last_activity
            if error is not None:
                values["last_error"] = error

            await db.execute(
                update(AgentContainer)
                .where(AgentContainer.user_id == user_id)
                .values(**values)
            )
            await db.commit()

    async def _upsert_record(
        self,
        user_id: str,
        container_name: str,
        password: str,
        workspace_volume: str,
        data_volume: str,
        status: str,
    ):
        """Insert or update the container record in the DB."""
        async with async_session() as db:
            existing = await db.execute(
                select(AgentContainer).where(AgentContainer.user_id == user_id)
            )
            record = existing.scalar_one_or_none()

            if record:
                record.container_name = container_name
                record.password_enc = password  # In production: encrypt this
                record.image = settings.agent_image
                record.workspace_volume = workspace_volume
                record.data_volume = data_volume
                record.status = status
            else:
                record = AgentContainer(
                    user_id=user_id,
                    container_name=container_name,
                    password_enc=password,  # In production: encrypt this
                    image=settings.agent_image,
                    workspace_volume=workspace_volume,
                    data_volume=data_volume,
                    status=status,
                )
                db.add(record)
            await db.commit()

    async def update_activity(self, user_id: str):
        """Update last_activity timestamp (called on each prompt)."""
        await self._update_status(
            user_id, "running",
            last_activity=datetime.now(timezone.utc),
        )


# Global singleton
agent_controller = AgentController()
