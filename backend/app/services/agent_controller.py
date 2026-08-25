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
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
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

    def __init__(self):
        # user_id -> in-flight background start task (see request_start).
        # Lets get_status() report the live startup phase and keeps
        # concurrent start requests from spawning duplicate work.
        self._start_tasks: dict[str, asyncio.Task] = {}
        # user_id -> live startup phase (creating / starting / warming).
        # In-memory only — get_status() surfaces it to the polling UI while
        # the background task advances; cleared when the task finishes.
        self._start_phases: dict[str, str] = {}

    # ------------------------------------------------------------------
    #  Start — the main entry point when a user enters the platform
    #  request_start — non-blocking entry, spawns the background task
    #  start_for_user — actual startup flow (creating → starting → warming)
    # ------------------------------------------------------------------

    async def request_start(self, user_id: str, workspace: str | None = None) -> dict:
        """Kick off startup in the background and return the phase immediately.

        POST /api/agent/start uses this so the UI never blocks on the full
        container boot (typically ~5-10s). The browser polls
        GET /api/agent/status, which reports the live phase while the
        background task advances: creating → starting → warming → running.

        Concurrent callers (page warmup + button click, two tabs, ...) share
        one task via _start_tasks, so the flow never runs twice per user.
        """
        existing = self._start_tasks.get(user_id)
        if existing and not existing.done():
            record = await self._get_container_record(user_id)
            return {
                "running": False,
                "healthy": False,
                "status": self._start_phases.get(user_id, "starting"),
                "container_name": record.container_name if record else None,
                "workspace": None,
                "message": "Agent startup already in progress",
            }

        record = await self._get_container_record(user_id)
        if record and record.status == "running":
            # Already up — start_for_user's fast path also re-attaches a
            # missing SSE pump, so call it directly instead of spawning.
            return await self.start_for_user(user_id, workspace)

        # Re-check after the awaits above: a concurrent caller may have
        # spawned the task while we were reading the record.
        existing = self._start_tasks.get(user_id)
        if existing and not existing.done():
            return {
                "running": False,
                "healthy": False,
                "status": self._start_phases.get(user_id, "starting"),
                "container_name": record.container_name if record else None,
                "workspace": None,
                "message": "Agent startup already in progress",
            }

        self._start_phases[user_id] = "creating"
        self._start_tasks[user_id] = asyncio.create_task(
            self._run_start(user_id, workspace)
        )
        return {
            "running": False,
            "healthy": False,
            "status": "creating",
            "container_name": record.container_name if record else None,
            "workspace": None,
            "message": "Agent startup initiated",
        }

    async def _run_start(self, user_id: str, workspace: str | None):
        """Background wrapper around start_for_user; cleans up tracking."""
        try:
            await self.start_for_user(user_id, workspace)
        except Exception:
            # start_for_user handles its own errors; this only guards the
            # fast-path / record reads that happen outside its try block.
            logger.exception("Background start for user %s crashed", user_id)
            await self._update_status(user_id, "failed", error="background start crashed")
        finally:
            self._start_tasks.pop(user_id, None)
            self._start_phases.pop(user_id, None)

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
            # Gate on Docker's running state, NOT is_healthy(): the image's
            # HEALTHCHECK has a 45s start-period during which health reads
            # "starting" even though opencode is serving (our own probe
            # already verified it before the DB row was marked running).
            # Using is_healthy() here would trigger a full re-create on
            # every start request within the first ~15s of container life.
            if container_manager.get_container_status(user_id) == "running":
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
        self._start_phases[user_id] = "creating"
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
            self._start_phases[user_id] = "starting"
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
            # are still initialising in the background. If a session is
            # created during that window, its first model-resolve (title
            # generation) races with provider init and fails with "Model
            # unavailable", poisoning that session's runner. Absorb the race
            # with a throwaway session — it doubles as a real readiness
            # signal (the warmup session's title is a model call, so once it
            # appears the providers are live), typically ready in 1-2s
            # instead of a fixed 5s sleep.
            self._start_phases[user_id] = "warming"
            await self._warmup_session(user_id, password)

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
        finally:
            # start_for_user is also invoked directly (POST /start with
            # wait:true, and the fast-path forward below) — without the
            # _run_start wrapper whose finally clears the phase. A leaked
            # "warming" phase makes GET /agent/status report warming forever,
            # so the UI shows an endless "starting" spinner.
            self._start_phases.pop(user_id, None)

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

    async def destroy_for_user(self, user_id: str) -> dict:
        """Destroy the user's container and volumes.

        Per design section 3.2 — should backup volumes before calling this.

        Idempotent and tolerant of drift: when the Docker container is
        already gone but a DB record (and possibly orphaned volumes)
        remains, this still cleans them up and removes the record, so the
        admin panel row disappears instead of lingering as a zombie.
        """
        record = await self._get_container_record(user_id)
        has_container = container_manager.get_container(user_id) is not None
        if not has_container and record is None:
            return {"ok": False, "message": "No container or record for this user"}

        await sse_pump_manager.stop_pump(user_id)
        await container_manager.destroy_container(user_id)
        await self._delete_record(user_id)
        return {"ok": True, "message": "Container and volumes destroyed"}

    # ------------------------------------------------------------------
    #  Restart — in-place container restart with pump re-attach
    # ------------------------------------------------------------------

    async def restart_for_user(self, user_id: str) -> dict:
        """Restart the user's container and re-attach the SSE pump.

        Used by the admin panel: docker restart preserves the container
        object (env/mounts/labels), then we re-probe health and rebuild the
        pump because the upstream /api/event connection dies with the old
        process. Manual restarts are counted in restart_count.
        """
        record = await self._get_container_record(user_id)
        if not record:
            return {"ok": False, "message": "No container record for this user"}

        await sse_pump_manager.stop_pump(user_id)
        await self._update_status(user_id, "starting")

        restarted = await container_manager.restart_container(user_id)

        async with async_session() as db:
            await db.execute(
                update(AgentContainer)
                .where(AgentContainer.user_id == user_id)
                .values(restart_count=AgentContainer.restart_count + 1)
            )
            await db.commit()

        if not restarted:
            await self._update_status(user_id, "failed", error="Docker restart failed")
            return {"ok": False, "message": "Docker restart failed (container absent?)"}

        if not await self._health_probe(user_id, record.password_enc):
            await self._update_status(user_id, "failed", error="Health probe failed after restart")
            return {"ok": False, "message": "Restarted, but health probe failed"}

        await sse_pump_manager.start_pump(
            user_id,
            container_manager.get_container_url(user_id),
            ("opencode", record.password_enc),
        )
        await self._update_status(user_id, "running", error=None)
        return {"ok": True, "message": "Container restarted"}

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
        """Get the current agent status for a user.

        Merges three sources: the DB record, Docker's live state, and — when
        a background start is in flight — the live startup phase. The phase
        matters because the DB record alone flip-flops during boot (docker
        state lags the flow, so a "creating" row reads back as "stopped").

        Note on `healthy`: it mirrors Docker's HEALTHCHECK, which has a 45s
        start-period — it stays False for a while even when opencode is
        serving. Readiness should be judged from `status == "running"`
        (only set after our own probe + warmup succeeded), not `healthy`.
        """
        record = await self._get_container_record(user_id)
        phase = self._start_phases.get(user_id)

        if not record and not phase:
            return {
                "running": False,
                "healthy": False,
                "status": "absent",
                "container_name": None,
                "workspace": None,
            }

        # Check actual container state
        is_healthy = container_manager.is_healthy(user_id)
        is_running = container_manager.get_container_status(user_id) == "running"

        return {
            "running": is_running,
            "healthy": is_healthy,
            "status": phase or (record.status if is_running else "stopped"),
            "container_name": record.container_name if record else None,
            "workspace": None,
        }

    # ------------------------------------------------------------------
    #  Health probe — wait for container to become ready
    # ------------------------------------------------------------------

    async def _health_probe(self, user_id: str, password: str) -> bool:
        """Probe opencode's own GET /api/health until it responds or times out.

        opencode reports `{"healthy": true}` as soon as the HTTP server is
        listening, which happens ~2s after container start. Poll fast at
        first (a fixed 2s interval can waste ~2s when the server comes up
        between probes), then back off to 2s for the long tail.
        """
        deadline = time.time() + settings.startup_timeout
        interval = 0.5
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
            await asyncio.sleep(interval)
            interval = min(interval * 1.6, 2.0)
        return False

    # ------------------------------------------------------------------
    #  Warmup — absorb the first-session model race
    # ------------------------------------------------------------------

    async def _warmup_session(self, user_id: str, password: str) -> None:
        """Create a throwaway session so the user's first session starts clean.

        Mirrors the warmup step in scripts/e2e.py. opencode serve mode does
        NOT auto-resolve the default model from config — it must be passed
        explicitly at session creation. And the FIRST session on a fresh
        container triggers a title-generation call whose model resolution
        races with provider initialisation; when it loses, that session's
        runner is poisoned with "Model unavailable" and silently drops
        prompts. Creating a discarded session lets the race burn out there.

        Readiness signal: the warmup session's title is itself produced by
        a model call, so once it appears the providers are initialised. We
        poll for it (up to 5s, matching the old fixed sleep as worst case)
        and typically return in 1-2s.
        """
        try:
            # Resolve the default model ("providerID/modelID") from /config.
            cfg = await tunnel_relay.http_request(
                user_id=user_id, method="GET", path="/config",
                password=password, timeout=10,
            )
            default_model = None
            if cfg["status"] == 200 and isinstance(cfg["body"], dict):
                default_model = cfg["body"].get("model")

            session_body: dict = {"agent": "build", "location": {"directory": "/workspace"}}
            if default_model and "/" in default_model:
                pid, mid = default_model.split("/", 1)
                session_body["model"] = {"providerID": pid, "id": mid}

            resp = await tunnel_relay.http_request(
                user_id=user_id,
                method="POST",
                path="/api/session",
                raw_body=json.dumps(session_body).encode(),
                password=password,
                timeout=30,
                headers={"content-type": "application/json"},
            )
            session_id = None
            if resp["status"] == 200 and isinstance(resp["body"], dict):
                session_id = (resp["body"].get("data") or {}).get("id")
            if not session_id:
                logger.warning(
                    "Warmup session creation failed for %s: HTTP %s — settling 5s instead",
                    user_id, resp["status"],
                )
                await asyncio.sleep(5)
                return

            # Poll the warmup session until its title appears (a successful
            # model call ⇒ providers ready). Cap at 5s: if title generation
            # lost the race, no title ever arrives and we just wait out the
            # same budget the old fixed sleep used.
            deadline = time.time() + 5
            titled = False
            while time.time() < deadline:
                detail = await tunnel_relay.http_request(
                    user_id=user_id, method="GET",
                    path=f"/api/session/{session_id}",
                    password=password, timeout=5,
                )
                if detail["status"] == 200 and isinstance(detail["body"], dict):
                    if (detail["body"].get("data") or {}).get("title"):
                        titled = True
                        break
                await asyncio.sleep(0.5)
            if titled:
                logger.info("Warmup session titled — providers ready for %s", user_id)
            else:
                logger.info("Warmup session for %s untitled after 5s — proceeding", user_id)

            # The warmup session is disposable: delete it so it doesn't
            # linger in the user's session list. Best effort only — a
            # failed delete never blocks startup.
            # NOTE: deletion only exists on the legacy route (no /api
            # prefix — same one the frontend's deleteSession uses); the
            # v2 path falls through to the SPA and deletes nothing.
            try:
                await tunnel_relay.http_request(
                    user_id=user_id, method="DELETE",
                    path=f"/session/{session_id}",
                    password=password, timeout=5,
                )
                logger.info("Warmup session %s deleted for %s", session_id, user_id)
            except Exception as e:
                logger.debug("Warmup session delete failed for %s: %s", user_id, e)
        except Exception as e:
            # Best effort only: a failed warmup must never block startup.
            logger.warning("Warmup for %s failed (%s) — settling 5s instead", user_id, e)
            await asyncio.sleep(5)

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
            # Gate on Docker's running state, not is_healthy(): containers
            # created before the authed healthcheck fix read "unhealthy"
            # forever (curl 401), which used to mark live containers as
            # stopped on every backend restart and killed their pumps.
            if container_manager.get_container_status(user_id) == "running":
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

    async def _delete_record(self, user_id: str):
        """Remove the user's container record (after destroy).

        Safe to call unconditionally: start_for_user upserts a fresh record
        when the user launches a new container.
        """
        async with async_session() as db:
            await db.execute(
                delete(AgentContainer).where(AgentContainer.user_id == user_id)
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
