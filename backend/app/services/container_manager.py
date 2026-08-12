"""Container Manager — real Docker SDK integration for per-user container lifecycle.

Each user gets a hardened Docker container whose single process is
`opencode serve`. That server *is* the agent: it owns the agent loop, the LLM
calls, the tool execution and the session store. The platform contributes
lifecycle management and configuration injection — nothing else.

Key design decisions (from agent-docker-design.md):
  - Containers join the `agent-net` bridge network (no port mapping to host)
  - Root filesystem is read-only; only /workspace, /data, /tmp are writable
  - Non-root user (uid 1000), all capabilities dropped, no-new-privileges
  - Per-user named volumes for workspace and opencode state
  - Deterministic container names (agent-{user_id}) for idempotent operations
  - Health check hits opencode's own GET /api/health
  - The sanitized host opencode.json is copied into the container's config
    volume before every start, so credential/provider edits take effect on
    restart without rebuilding the image
"""
import asyncio
import io
import json
import logging
import secrets
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import NotFound, APIError
from docker.models.containers import Container
from docker.models.networks import Network
from docker.models.volumes import Volume

from ..config import settings
from .opencode_config import build_container_config_json

logger = logging.getLogger(__name__)

# Where the entrypoint expects the injected config (must match agent-image).
CONTAINER_CONFIG_DIR = "/data/config/opencode"
CONTAINER_CONFIG_PATH = f"{CONTAINER_CONFIG_DIR}/opencode.json"


class ContainerManager:
    """Manages per-user Docker containers via the Docker SDK.

    All operations are idempotent: calling ensure_container twice with the same
    user_id will not create duplicate containers.
    """

    def __init__(self):
        self._client: docker.DockerClient | None = None
        self._network: Network | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    #  Docker client & network initialization
    # ------------------------------------------------------------------

    def _get_client(self) -> docker.DockerClient:
        """Lazily connect to the Docker daemon."""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def ensure_network(self) -> Network:
        """Create the agent-net bridge network if it doesn't exist.

        Per design section 2.3 — containers join this network and do NOT map
        ports to the host. The platform control layer accesses containers by
        name via Docker's built-in DNS.
        """
        client = self._get_client()
        try:
            network = client.networks.get(settings.agent_network)
            logger.info("agent-net network already exists")
            return network
        except NotFound:
            network = client.networks.create(
                settings.agent_network,
                driver="bridge",
                internal=False,  # allow egress to external services (LLM APIs etc.)
                labels={"managed-by": "agent-platform"},
            )
            logger.info("Created agent-net network")
            return network

    # ------------------------------------------------------------------
    #  Volume management
    # ------------------------------------------------------------------

    def _ensure_volume(self, name: str) -> Volume:
        """Create a named volume if it doesn't exist (idempotent)."""
        client = self._get_client()
        try:
            return client.volumes.get(name)
        except NotFound:
            return client.volumes.create(
                name=name,
                labels={"managed-by": "agent-platform"},
            )

    def _workspace_volume_name(self, user_id: str) -> str:
        return f"agent-ws-{user_id}"

    def _data_volume_name(self, user_id: str) -> str:
        return f"agent-data-{user_id}"

    # ------------------------------------------------------------------
    #  Configuration injection
    # ------------------------------------------------------------------

    @staticmethod
    def _config_tar(config_json: str) -> bytes:
        """Pack opencode.json into a tar stream for Docker's put_archive API."""
        payload = config_json.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="opencode.json")
            info.size = len(payload)
            info.mode = 0o600
            info.uid = 1000
            info.gid = 1000
            tar.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    def inject_opencode_config(self, container: Container) -> bool:
        """Copy the sanitized host opencode.json into the container's volume.

        `put_archive` is the Docker `cp` primitive and works on a created-but-
        not-yet-started container, so the config is already in place the moment
        opencode boots. It also writes straight through the read-only rootfs
        restriction because /data is a volume.
        """
        try:
            config_json = build_container_config_json()
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not build opencode config: %s", exc)
            return False

        archive = self._config_tar(config_json)
        try:
            container.put_archive(CONTAINER_CONFIG_DIR, archive)
            logger.info(
                "Injected opencode config into %s (%d bytes)", container.name, len(config_json)
            )
            return True
        except NotFound:
            # The volume is empty on first creation, so /data/config/opencode
            # may not exist yet. Create it with a throwaway run against the same
            # volume, then retry.
            logger.info("Config dir missing in %s — creating it", container.name)
            if self._bootstrap_config_dir(container):
                try:
                    container.put_archive(CONTAINER_CONFIG_DIR, archive)
                    logger.info("Injected opencode config into %s (retry ok)", container.name)
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Config injection retry failed: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Config injection failed for %s: %s", container.name, exc)
            return False

    def _bootstrap_config_dir(self, container: Container) -> bool:
        """mkdir -p the XDG config dir inside the container's data volume."""
        client = self._get_client()
        mounts = container.attrs.get("Mounts", [])
        data_volume = next(
            (m.get("Name") for m in mounts if m.get("Destination") == "/data" and m.get("Name")),
            None,
        )
        if not data_volume:
            return False
        try:
            client.containers.run(
                image=settings.agent_image,
                entrypoint=["/bin/sh", "-c"],
                command=[f"mkdir -p {CONTAINER_CONFIG_DIR} /data/share /data/cache /data/state"],
                volumes={data_volume: {"bind": "/data", "mode": "rw"}},
                user="1000:1000",
                remove=True,
                labels={"purpose": "config-bootstrap", "temporary": "true"},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Config dir bootstrap failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    #  Container lifecycle — core operations
    # ------------------------------------------------------------------

    def _build_run_kwargs(self, user_id: str, password: str, ws_volume: str, data_volume: str) -> dict:
        """Build the full docker create kwargs with all hardening parameters.

        Implements every security control from design section 5.3 and Appendix B.
        The command runs opencode's own headless server — the platform ships no
        agent code of its own into the container.
        """
        port = settings.agent_port
        return {
            "image": settings.agent_image,
            "name": f"agent-{user_id}",
            # ENTRYPOINT prepares the XDG tree then execs `opencode <cmd> --port`.
            "command": ["serve", "--hostname", "0.0.0.0", "--log-level", "INFO", "--print-logs"],
            "working_dir": settings.agent_workdir,
            # --- Network ---
            "network": settings.agent_network,
            # Let the container reach LLM proxies bound to loopback on the Docker
            # host; opencode_config rewrites 127.0.0.1 base URLs to this alias.
            "extra_hosts": {settings.container_host_alias: "host-gateway"},
            # --- User isolation ---
            "user": "1000:1000",
            # --- Filesystem isolation ---
            "read_only": True,
            # opencode's bun runtime and tool output need real scratch space.
            "tmpfs": {"/tmp": "size=256m", "/home/agent": "size=512m,uid=1000,gid=1000"},
            "volumes": {
                ws_volume: {"bind": settings.agent_workdir, "mode": "rw"},
                data_volume: {"bind": "/data", "mode": "rw"},
            },
            # --- Resource limits (cgroup) ---
            "cpu_quota": int(settings.container_cpu_limit * 100000),
            "cpu_period": 100000,
            "mem_limit": settings.container_memory_limit,
            "memswap_limit": settings.container_memory_limit,  # no swap
            "pids_limit": settings.container_pids_limit,
            "ulimits": [{"name": "nofile", "soft": 8192, "hard": 8192}],
            # --- Security hardening ---
            "cap_drop": ["ALL"],
            "security_opt": [
                "no-new-privileges:true",
                "apparmor=docker-default",
            ],
            # --- Environment ---
            "environment": {
                # opencode's own BasicAuth. The container has no published port,
                # so this is defence in depth behind the platform's JWT tunnel.
                "OPENCODE_SERVER_PASSWORD": password,
                "OPENCODE_SERVER_USERNAME": "opencode",
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_PORT": str(port),
                "HOME": "/home/agent",
                "XDG_CONFIG_HOME": "/data/config",
                "XDG_DATA_HOME": "/data/share",
                "XDG_CACHE_HOME": "/data/cache",
                "XDG_STATE_HOME": "/data/state",
                "AGENT_WORKDIR": settings.agent_workdir,
                "AGENT_USER_ID": user_id,
            },
            # --- Restart policy ---
            "restart_policy": {"Name": "unless-stopped"},
            # --- Health check (opencode's own endpoint) ---
            "healthcheck": {
                "test": ["CMD-SHELL", f"curl -fsS http://127.0.0.1:{port}/api/health || exit 1"],
                "interval": 15_000_000_000,   # 15s in nanoseconds
                "timeout": 5_000_000_000,     # 5s
                "retries": 4,
                "start_period": 45_000_000_000,  # 45s grace — opencode boots in ~2s
            },
            # --- Labels ---
            "labels": {
                "managed-by": "agent-platform",
                "user-id": user_id,
                "runtime": "opencode-serve",
            },
            "detach": True,
        }

    async def ensure_container(self, user_id: str) -> tuple[Container, str]:
        """Idempotently ensure a container exists and is running for the user.

        Returns (container, password).
        - If container exists and is running → return it
        - If container exists but stopped → start it
        - If container doesn't exist → create + start it

        Implements the ensure_container pattern from design section 3.4.
        """
        async with self._lock:
            client = self._get_client()
            self.ensure_network()

            container_name = f"agent-{user_id}"
            ws_volume_name = self._workspace_volume_name(user_id)
            data_volume_name = self._data_volume_name(user_id)

            # Ensure volumes exist
            ws_volume = self._ensure_volume(ws_volume_name)
            data_volume = self._ensure_volume(data_volume_name)

            # Try to find existing container
            try:
                container = client.containers.get(container_name)
                password = self._extract_password(container) or secrets.token_urlsafe(32)
                if container.status == "running":
                    logger.info("Container %s already running", container_name)
                    return container, password
                logger.info("Container %s exists but stopped — refreshing config and starting", container_name)
                # Pick up any provider/credential edits the user made on the host.
                self.inject_opencode_config(container)
                container.start()
                container.reload()
                return container, password

            except NotFound:
                pass  # Need to create

            # Create new container
            password = secrets.token_urlsafe(32)
            logger.info("Creating container %s", container_name)

            run_kwargs = self._build_run_kwargs(user_id, password, ws_volume_name, data_volume_name)
            container = client.containers.create(**run_kwargs)

            # Config must land before the first start; the entrypoint falls back
            # to the image default if this fails, so a failure is not fatal.
            self.inject_opencode_config(container)

            container.start()
            container.reload()

            logger.info("Container %s created and started (opencode serve)", container_name)
            return container, password

    async def reload_config(self, user_id: str) -> bool:
        """Re-inject the host opencode.json and restart the user's container.

        This is how a credential or provider change on the developer's machine
        reaches a running agent.
        """
        async with self._lock:
            container = self.get_container(user_id)
            if container is None:
                return False
            try:
                container.stop(timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Stop before config reload failed: %s", exc)
            ok = self.inject_opencode_config(container)
            container.start()
            container.reload()
            logger.info("Reloaded opencode config for %s (injected=%s)", user_id, ok)
            return ok

    def _extract_password(self, container: Container) -> str | None:
        """Extract the OPENCODE_SERVER_PASSWORD from a running container's env."""
        try:
            env_list = container.attrs.get("Config", {}).get("Env", [])
            for env in env_list:
                if env.startswith("OPENCODE_SERVER_PASSWORD="):
                    return env.split("=", 1)[1]
        except Exception:
            pass
        return None

    async def stop_container(self, user_id: str, timeout: int = 10):
        """Gracefully stop a user's container (preserves volumes)."""
        async with self._lock:
            client = self._get_client()
            container_name = f"agent-{user_id}"
            try:
                container = client.containers.get(container_name)
                container.stop(timeout=timeout)
                logger.info("Container %s stopped", container_name)
            except NotFound:
                logger.info("Container %s not found — nothing to stop", container_name)
            except Exception as e:
                logger.error("Failed to stop container %s: %s", container_name, e)

    async def destroy_container(self, user_id: str):
        """Destroy a user's container AND its volumes (irreversible).

        Per design section 3.2 — triggered after M days of inactivity or
        user account deletion. Volumes should be backed up before calling this.
        """
        async with self._lock:
            client = self._get_client()
            container_name = f"agent-{user_id}"
            try:
                container = client.containers.get(container_name)
                container.remove(force=True)
                logger.info("Container %s destroyed", container_name)
            except NotFound:
                pass

            # Remove volumes
            for vol_name in [self._workspace_volume_name(user_id), self._data_volume_name(user_id)]:
                try:
                    vol = client.volumes.get(vol_name)
                    vol.remove(force=True)
                    logger.info("Volume %s removed", vol_name)
                except (NotFound, Exception):
                    pass

    def get_container(self, user_id: str) -> Container | None:
        """Get the Docker container object for a user, or None."""
        client = self._get_client()
        try:
            return client.containers.get(f"agent-{user_id}")
        except NotFound:
            return None

    def get_container_ip(self, user_id: str) -> str | None:
        """Get the container's IP on the agent-net network."""
        container = self.get_container(user_id)
        if container is None:
            return None
        net_settings = container.attrs.get("NetworkSettings", {})
        networks = net_settings.get("Networks", {})
        net_info = networks.get(settings.agent_network, {})
        ip = net_info.get("IPAddress")
        if ip:
            return ip
        # Fallback: use container name as hostname (Docker DNS)
        return f"agent-{user_id}"

    def get_container_url(self, user_id: str) -> str:
        """Get the HTTP base URL for a user's container.

        Uses Docker's built-in DNS: http://agent-{user_id}:{port}
        """
        return f"http://agent-{user_id}:{settings.agent_port}"

    def is_healthy(self, user_id: str) -> bool:
        """Check if the container is running and healthy."""
        container = self.get_container(user_id)
        if container is None:
            return False
        if container.status != "running":
            return False
        health = container.attrs.get("State", {}).get("Health", {})
        if health:
            return health.get("Status") == "healthy"
        # No healthcheck configured — treat running as healthy
        return container.status == "running"

    def get_container_status(self, user_id: str) -> str:
        """Get the raw container status string."""
        container = self.get_container(user_id)
        if container is None:
            return "absent"
        return container.status  # 'created', 'running', 'paused', 'exited', etc.

    # ------------------------------------------------------------------
    #  Diagnostics
    # ------------------------------------------------------------------

    def get_container_logs(self, user_id: str, tail: int = 100) -> str:
        """Fetch recent container logs for debugging."""
        container = self.get_container(user_id)
        if container is None:
            return ""
        try:
            return container.logs(tail=tail).decode(errors="replace")
        except Exception:
            return ""

    def list_all_containers(self) -> list[dict]:
        """List all agent containers managed by the platform."""
        client = self._get_client()
        containers = client.containers.list(
            all=True,
            filters={"label": "managed-by=agent-platform"},
        )
        result = []
        for c in containers:
            result.append({
                "name": c.name,
                "user_id": c.labels.get("user-id", ""),
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else str(c.image.id),
            })
        return result


# Global singleton — replaces the old OpencodeManager
container_manager = ContainerManager()
