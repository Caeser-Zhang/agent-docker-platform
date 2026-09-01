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
import gzip
import hashlib
import io
import json
import logging
import posixpath
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
from .host_config import SKILLS_DIR
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

    @staticmethod
    def _tar_from_files(files: dict[str, bytes]) -> bytes:
        """Pack {relative-path: content} into a tar stream for put_archive.

        Explicit directory entries are emitted for every parent path, so the
        tree materialises even when the destination directory is fresh.
        """
        dirs: set[str] = set()
        for name in files:
            parts = name.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for d in sorted(dirs):
                info = tarfile.TarInfo(name=f"{d}/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.uid = 1000
                info.gid = 1000
                tar.addfile(info)
            for name, payload in sorted(files.items()):
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                info.mode = 0o644
                info.uid = 1000
                info.gid = 1000
                tar.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    def inject_opencode_config(
        self, container: Container, config_json: str | None = None
    ) -> bool:
        """Copy the sanitized opencode.json into the container's volume.

        `put_archive` is the Docker `cp` primitive and works on a created-but-
        not-yet-started container, so the config is already in place the moment
        opencode boots. It also writes straight through the read-only rootfs
        restriction because /data is a volume.

        ``config_json`` is the pre-built document produced by the async layer
        (a user's DB-backed config). When omitted — e.g. a legacy caller that
        only knows about the host baseline — the host config is built here.
        """
        try:
            if config_json is None:
                config_json = build_container_config_json()
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not build opencode config: %s", exc)
            return False

        archive = self._config_tar(config_json)
        injected = False
        try:
            container.put_archive(CONTAINER_CONFIG_DIR, archive)
            injected = True
        except NotFound:
            # The volume is empty on first creation, so /data/config/opencode
            # may not exist yet. Create it with a throwaway run against the same
            # volume, then retry.
            logger.info("Config dir missing in %s — creating it", container.name)
            if self._bootstrap_config_dir(container):
                try:
                    container.put_archive(CONTAINER_CONFIG_DIR, archive)
                    injected = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Config injection retry failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Config injection failed for %s: %s", container.name, exc)

        if injected:
            logger.info(
                "Injected opencode config into %s (%d bytes)", container.name, len(config_json)
            )

        # Global skills live beside the config file (host skills dir → the
        # container's XDG skills dir). Injected independently so a skills
        # failure can never block the (critical) config injection.
        self._inject_global_skills(container)
        return injected

    @staticmethod
    def _global_skills_tar() -> bytes | None:
        """Pack the host skills directory tree into a tar rooted at skills/.

        opencode discovers global skills under XDG_CONFIG_HOME/opencode/skills,
        which inside the container is exactly CONTAINER_CONFIG_DIR/skills.
        """
        files: dict[str, bytes] = {}
        if SKILLS_DIR.is_dir():
            for path in sorted(SKILLS_DIR.rglob("*")):
                if path.is_file():
                    rel = path.relative_to(SKILLS_DIR).as_posix()
                    files[f"skills/{rel}"] = path.read_bytes()
        if not files:
            return None
        return ContainerManager._tar_from_files(files)

    def _inject_global_skills(self, container: Container) -> bool:
        skills_archive = self._global_skills_tar()
        if skills_archive is None:
            return False
        try:
            container.put_archive(CONTAINER_CONFIG_DIR, skills_archive)
            logger.info("Injected global skills into %s", container.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Global skills injection failed for %s: %s", container.name, exc)
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
                # fastk CLI (fastk-search / fastk-analyze skills) targets the
                # platform fastk REST server on the host.
                "FASTDB_BASE_URL": settings.fastk_server_url,
                "FASTK_DEFAULT_DB": "global",
            },
            # --- Restart policy ---
            "restart_policy": {"Name": "unless-stopped"},
            # --- Health check (opencode's own endpoint) ---
            # /api/health sits behind opencode's BasicAuth — without
            # credentials curl gets a 401 and every container reads
            # "unhealthy" forever.
            "healthcheck": {
                "test": ["CMD-SHELL", f'curl -fsS -u "opencode:$OPENCODE_SERVER_PASSWORD" http://127.0.0.1:{port}/api/health || exit 1'],
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

    def _image_stale(self, client, container) -> bool:
        """True when the container's image differs from the configured agent image.

        Compares image digests (tags can be re-pointed), and treats a missing
        configured image as "not stale" so a bad AGENT_AGENT_IMAGE value never
        destroys working containers.
        """
        try:
            wanted = client.images.get(settings.agent_image).id
            return container.attrs["Image"] != wanted
        except Exception:
            return False

    async def ensure_container(
        self, user_id: str, config_json: str | None = None
    ) -> tuple[Container, str, bool]:
        """Idempotently ensure a container exists and is running for the user.

        Returns (container, password, config_ok).
        - If container exists and is running → return it
        - If container exists but stopped → start it
        - If container doesn't exist → create + start it
        - config_ok is False when the sanitized opencode.json could not be
          injected — the caller (AgentController.start_for_user) then fails
          the start instead of leaving a "zombie healthy" container whose
          LLM calls are all broken.

        Implements the ensure_container pattern from design section 3.4.

        ``config_json`` is the pre-built user config produced by the async
        layer; when omitted the host baseline is built inside the sync thread.

        The Docker SDK work runs in a worker thread: container creation
        blocks for ~1-2s, and doing that on the event loop would stall the
        status polls (and any concurrent /start call) that the async start
        flow depends on.
        """
        async with self._lock:
            return await asyncio.to_thread(self._ensure_container_sync, user_id, config_json)

    def _ensure_container_sync(
        self, user_id: str, config_json: str | None = None
    ) -> tuple[Container, str, bool]:
        client = self._get_client()
        self.ensure_network()

        container_name = f"agent-{user_id}"
        ws_volume_name = self._workspace_volume_name(user_id)
        data_volume_name = self._data_volume_name(user_id)

        # Ensure volumes exist
        ws_volume = self._ensure_volume(ws_volume_name)
        data_volume = self._ensure_volume(data_volume_name)

        # Try to find existing container
        container = None
        try:
            container = client.containers.get(container_name)
        except NotFound:
            pass  # Need to create

        if container is not None:
            password = self._extract_password(container) or secrets.token_urlsafe(32)
            if container.status == "running":
                logger.info("Container %s already running", container_name)
                # A running container was validated on its previous start:
                # a config-injection failure would have stopped it then.
                return container, password, True
            # Image upgrade: start/restart reuse the image the container was
            # CREATED with — after an agent image rebuild (new plugins, new
            # opencode version) stale containers would keep the old rootfs
            # forever. Recreate from the current image; the named workspace/
            # data volumes are re-attached, so user data survives.
            if self._image_stale(client, container):
                # Read the image name from attrs — `container.image` issues an
                # extra API call that 404s when the old image was pruned by a
                # rebuild (same guard as the status endpoint below).
                old = (container.attrs.get("Config") or {}).get("Image") or container.image_id
                logger.info(
                    "Container %s runs stale image %s — recreating on %s",
                    container_name, old, settings.agent_image,
                )
                container.remove(force=True)
            else:
                logger.info("Container %s exists but stopped — refreshing config and starting", container_name)
                # Pick up any provider/credential edits the user made, plus the
                # user's DB-backed providers / active LLM selection.
                config_ok = self.inject_opencode_config(container, config_json)
                if not config_ok:
                    logger.error("Config injection failed for %s before start", container_name)
                container.start()
                container.reload()
                return container, password, config_ok

        # Create new container
        password = secrets.token_urlsafe(32)
        logger.info("Creating container %s", container_name)

        run_kwargs = self._build_run_kwargs(user_id, password, ws_volume_name, data_volume_name)
        container = client.containers.create(**run_kwargs)

        # Config must land before the first start; the entrypoint falls back
        # to the image default if this fails. Propagate config_ok so the
        # caller can fail the start instead of leaving a misconfigured
        # (zombie healthy) container running.
        config_ok = self.inject_opencode_config(container, config_json)
        if not config_ok:
            logger.error("Config injection failed for new container %s", container_name)

        container.start()
        container.reload()

        logger.info("Container %s created and started (opencode serve)", container_name)
        return container, password, config_ok

    async def reload_config(self, user_id: str, config_json: str | None = None) -> bool:
        """Re-inject the opencode.json and restart the user's container.

        This is how a credential or provider change on the developer's machine
        reaches a running agent. ``config_json`` carries the pre-built user
        config; when omitted the host baseline is built inside the sync thread.
        """
        async with self._lock:
            return await asyncio.to_thread(self._reload_config_sync, user_id, config_json)

    def _reload_config_sync(self, user_id: str, config_json: str | None = None) -> bool:
        container = self.get_container(user_id)
        if container is None:
            return False
        try:
            container.stop(timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stop before config reload failed: %s", exc)
        ok = self.inject_opencode_config(container, config_json)
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

    async def restart_container(
        self, user_id: str, timeout: int = 10, config_json: str | None = None
    ) -> bool:
        """Restart a user's container in place via `docker restart`.

        Unlike stop+start this preserves the container object (env, mounts,
        labels), so the opencode password embedded in the env stays valid.
        ``config_json`` carries the pre-built user config; when omitted the
        host baseline is built inside the sync thread.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._restart_container_sync, user_id, timeout, config_json
            )

    def _restart_container_sync(
        self, user_id: str, timeout: int, config_json: str | None = None
    ) -> bool:
        client = self._get_client()
        container_name = f"agent-{user_id}"
        try:
            container = client.containers.get(container_name)
            # Re-inject the config before restarting: `docker restart`
            # alone keeps whatever config file the container was created
            # with, so provider/MCP edits on the host would never reach
            # containers that only ever get restarted (never stopped).
            # put_archive writes straight into the /data volume, so it
            # works on a running container too.
            if not self.inject_opencode_config(container, config_json):
                # Restart keeps the previously injected (working) config,
                # so this is not fatal — but never fail silently.
                logger.warning(
                    "Config re-injection failed before restart of %s — "
                    "container restarts with its previous config",
                    container_name,
                )
            container.restart(timeout=timeout)
            container.reload()
            logger.info("Container %s restarted", container_name)
            return True
        except NotFound:
            logger.info("Container %s not found — nothing to restart", container_name)
            return False
        except Exception as e:
            logger.error("Failed to restart container %s: %s", container_name, e)
            return False

    async def recreate_container(self, user_id: str) -> dict:
        """Remove the container object so the next start recreates it.

        Only the container is discarded — the named workspace/data volumes
        are preserved, so user data survives. This is how a running container
        ever picks up a rebuilt image: an in-place restart keeps the rootfs
        the container was created with.
        """
        async with self._lock:
            return await asyncio.to_thread(self._recreate_container_sync, user_id)

    def _recreate_container_sync(self, user_id: str) -> dict:
        client = self._get_client()
        container_name = f"agent-{user_id}"
        try:
            container = client.containers.get(container_name)
        except NotFound:
            logger.info("Container %s not found — nothing to recreate", container_name)
            return {"ok": True, "removed": False, "old_image": None}
        # Read from attrs: `container.image` 404s when the old image was
        # pruned by a rebuild (same guard as list_all_containers).
        old_image = (container.attrs.get("Config") or {}).get("Image") or container.attrs.get("Image") or ""
        container.remove(force=True)
        logger.info(
            "Container %s removed for recreate (was image %s; volumes kept)",
            container_name, old_image,
        )
        return {"ok": True, "removed": True, "old_image": old_image}

    async def stop_container(self, user_id: str, timeout: int = 10):
        """Gracefully stop a user's container (preserves volumes)."""
        async with self._lock:
            await asyncio.to_thread(self._stop_container_sync, user_id, timeout)

    def _stop_container_sync(self, user_id: str, timeout: int):
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

    async def destroy_container(self, user_id: str) -> dict:
        """Destroy a user's container AND its volumes (irreversible).

        Per design section 3.2 — triggered after M days of inactivity or
        user account deletion. The workspace volume is exported to a
        tar.gz under settings.backup_dir first (P1-6).

        Returns backup metadata ({} when there was no volume to back up
        or the backup failed — backup is soft-fail, never blocks cleanup).
        """
        async with self._lock:
            return await asyncio.to_thread(self._destroy_container_sync, user_id)

    def _destroy_container_sync(self, user_id: str) -> dict:
        client = self._get_client()
        container_name = f"agent-{user_id}"
        try:
            container = client.containers.get(container_name)
            container.remove(force=True)
            logger.info("Container %s destroyed", container_name)
        except NotFound:
            pass

        # P1-6: tar up the workspace volume before removing it — destroy is
        # irreversible, so this is the last chance to save user data.
        backup_meta = self._backup_workspace_sync(user_id)

        # Remove volumes
        for vol_name in [self._workspace_volume_name(user_id), self._data_volume_name(user_id)]:
            try:
                vol = client.volumes.get(vol_name)
                vol.remove(force=True)
                logger.info("Volume %s removed", vol_name)
            except (NotFound, Exception):
                pass
        return backup_meta

    def _backup_workspace_sync(self, user_id: str) -> dict:
        """Export the user's workspace volume to {backup_dir}/{user_id}/{ts}.tar.gz.

        P1-6. Mounts the volume read-only into a throwaway (created-but-never-
        started) container and streams `get_archive` output through gzip, so
        neither the user container's presence nor its state matters.

        Soft-fails: any problem logs a warning and returns {} — a failed
        backup must never block the irreversible cleanup it precedes.
        """
        client = self._get_client()
        vol_name = self._workspace_volume_name(user_id)
        try:
            client.volumes.get(vol_name)
        except NotFound:
            return {}  # nothing to back up
        except Exception as exc:  # noqa: BLE001
            logger.warning("Backup pre-check failed for %s: %s", vol_name, exc)
            return {}

        holder: Container | None = None
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest_dir = Path(settings.backup_dir) / user_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"workspace-{ts}.tar.gz"

            # Created-but-not-running container: the daemon still reads the
            # mounted volume via the archive API. Never started, so the
            # command is irrelevant.
            holder = client.containers.create(
                image=settings.agent_image,
                entrypoint=["/bin/sh", "-c"],
                command=["true"],
                volumes={vol_name: {"bind": settings.agent_workdir, "mode": "ro"}},
                user="1000:1000",
                labels={"purpose": "workspace-backup", "temporary": "true"},
            )
            bits, _stat = holder.get_archive(settings.agent_workdir)

            raw_size = 0
            sha = hashlib.sha256()
            with gzip.open(dest, "wb", compresslevel=6) as gz:
                for chunk in bits:
                    gz.write(chunk)
                    raw_size += len(chunk)
                    sha.update(chunk)

            meta = {
                "path": str(dest),
                "volume": vol_name,
                "size_bytes": dest.stat().st_size,
                "raw_bytes": raw_size,
                "sha256": sha.hexdigest(),
                "created_at": ts,
            }
            logger.info(
                "Workspace backup for %s written to %s (%d bytes compressed)",
                user_id, dest, meta["size_bytes"],
            )
            return meta
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace backup before destroy failed for %s: %s", user_id, exc)
            return {}
        finally:
            if holder is not None:
                try:
                    holder.remove(force=True)
                except Exception:  # noqa: BLE001
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

    def get_container_stats(self, user_id: str) -> dict | None:
        """One-shot CPU/memory sample for a running container.

        Blocking (~1-2s — the daemon takes two samples to compute CPU%), so
        callers in async context should wrap it in asyncio.to_thread.
        Returns None when the container is missing or not running.
        """
        container = self.get_container(user_id)
        if container is None or container.status != "running":
            return None
        try:
            stats = container.stats(stream=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stats sampling failed for agent-%s: %s", user_id, exc)
            return None

        cpu = stats.get("cpu_stats", {})
        pre = stats.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get(
            "cpu_usage", {}
        ).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        online = cpu.get("online_cpus") or len(
            cpu.get("cpu_usage", {}).get("percpu_usage") or [1]
        )
        cpu_percent = 0.0
        if sys_delta > 0 and cpu_delta >= 0:
            cpu_percent = round((cpu_delta / sys_delta) * online * 100, 2)

        mem = stats.get("memory_stats", {})
        mem_usage = mem.get("usage", 0)
        mem_limit = mem.get("limit", 0)

        return {
            "cpu_percent": cpu_percent,
            "mem_usage_mb": round(mem_usage / 1024 / 1024, 1) if mem_usage else 0.0,
            "mem_limit_mb": round(mem_limit / 1024 / 1024, 1) if mem_limit else 0.0,
            "mem_percent": round(mem_usage / mem_limit * 100, 1) if mem_limit else 0.0,
            "pids": stats.get("pids_stats", {}).get("current"),
        }

    # ------------------------------------------------------------------
    #  Workspace file primitives (project-scope config & skills)
    # ------------------------------------------------------------------
    #
    # All methods take paths RELATIVE to the workspace root (the agent-ws
    # volume bind-mounted at /workspace) and work whether the container is
    # running or stopped — the Docker archive API does not require a live
    # process, and writes go through put_archive with explicit directory
    # entries so missing parents are materialised automatically.

    @staticmethod
    def _workspace_path(rel_path: str) -> str:
        """Resolve and validate a workspace-relative path to an absolute one."""
        rel = rel_path.strip().replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p]
        if not parts or any(p in ("", ".", "..") for p in parts) or ":" in rel:
            raise ValueError(f"Unsafe workspace path: {rel_path!r}")
        return f"{settings.agent_workdir}/{'/'.join(parts)}"

    def read_workspace_file(self, user_id: str, rel_path: str) -> bytes | None:
        """Read a single file from the user's workspace volume, or None."""
        container = self.get_container(user_id)
        if container is None:
            return None
        try:
            abs_path = self._workspace_path(rel_path)
        except ValueError:
            return None
        try:
            stream, _stat = container.get_archive(abs_path)
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
                member = tar.next()
                if member is None or not member.isfile():
                    return None
                handle = tar.extractfile(member)
                return handle.read() if handle else None
        except NotFound:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace read %s failed: %s", rel_path, exc)
            return None

    def read_workspace_tree(self, user_id: str, rel_dir: str) -> dict[str, bytes] | None:
        """Read every file under a workspace directory.

        Returns {path relative to rel_dir: content}, {} for an empty dir,
        or None when the directory does not exist.
        """
        container = self.get_container(user_id)
        if container is None:
            return None
        try:
            abs_dir = self._workspace_path(rel_dir)
        except ValueError:
            return None
        try:
            stream, _stat = container.get_archive(abs_dir)
        except NotFound:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace tree read %s failed: %s", rel_dir, exc)
            return None
        # get_archive tars the directory itself, so members start with its basename.
        root = posixpath.basename(abs_dir)
        files: dict[str, bytes] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name.lstrip("/")
                    prefix = f"{root}/"
                    if not name.startswith(prefix):
                        continue
                    handle = tar.extractfile(member)
                    if handle is not None:
                        files[name[len(prefix):]] = handle.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace tree parse %s failed: %s", rel_dir, exc)
            return None
        return files

    def write_workspace_files(self, user_id: str, files: dict[str, bytes]) -> bool:
        """Write files (workspace-relative path → content) into the workspace.

        Existing files at the same paths are overwritten; missing parent
        directories are created via explicit tar directory entries.
        """
        container = self.get_container(user_id)
        if container is None:
            return False
        if not files:
            return True
        try:
            for name in files:
                self._workspace_path(name)
        except ValueError as exc:
            logger.warning("Refusing unsafe workspace write: %s", exc)
            return False
        archive = self._tar_from_files(files)
        try:
            container.put_archive(settings.agent_workdir, archive)
            logger.info(
                "Wrote %d file(s) into workspace of agent-%s", len(files), user_id
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace write failed for agent-%s: %s", user_id, exc)
            return False

    def delete_workspace_path(self, user_id: str, rel_path: str) -> bool:
        """Delete a file or directory tree from the workspace volume."""
        container = self.get_container(user_id)
        if container is None:
            return False
        try:
            target = self._workspace_path(rel_path)
        except ValueError:
            return False
        if container.status == "running":
            try:
                result = container.exec_run(["rm", "-rf", "--", target], user="1000:1000")
                return result.exit_code == 0
            except Exception as exc:  # noqa: BLE001
                logger.warning("Workspace delete via exec failed: %s", exc)
                return False
        # Stopped container — run a throwaway container on the same volume.
        return self._run_on_workspace_volume(
            user_id, [f"rm -rf -- {target}"], purpose="workspace-cleanup"
        )

    def _run_on_workspace_volume(self, user_id: str, command: list[str], purpose: str) -> bool:
        """Run a one-shot command against the user's workspace volume."""
        client = self._get_client()
        try:
            client.containers.run(
                image=settings.agent_image,
                entrypoint=["/bin/sh", "-c"],
                command=command,
                volumes={
                    self._workspace_volume_name(user_id): {
                        "bind": settings.agent_workdir,
                        "mode": "rw",
                    }
                },
                user="1000:1000",
                remove=True,
                labels={"purpose": purpose, "temporary": "true"},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace volume command (%s) failed: %s", purpose, exc)
            return False

    # Directories that bloat the file tree without adding value for browsing.
    TREE_PRUNE = (".git", "node_modules", ".opencode/cache", ".cache")

    def list_workspace(self, user_id: str) -> list[dict] | None:
        """List the workspace tree as flat entries [{path, type, size}].

        Runs `find` inside the agent container when it is up, otherwise in a
        throwaway container on the same workspace volume (read-only). opencode
        itself has no directory-listing API in this version, so the platform
        provides its own.
        """
        container = self.get_container(user_id)
        if container is None:
            return None
        prune = " ".join(
            f"-name {d} -type d -prune -o" for d in self.TREE_PRUNE
        )
        cmd = (
            f"find {settings.agent_workdir} {prune} "
            r"-printf '%y\t%s\t%P\n'"
        )
        output: str | None = None
        if container.status == "running":
            try:
                result = container.exec_run(
                    ["/bin/sh", "-c", cmd], user="1000:1000"
                )
                if result.exit_code == 0:
                    output = result.output.decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Workspace listing via exec failed: %s", exc)
        if output is None:
            client = self._get_client()
            try:
                raw = client.containers.run(
                    image=settings.agent_image,
                    entrypoint=["/bin/sh", "-c"],
                    command=[cmd],
                    volumes={
                        self._workspace_volume_name(user_id): {
                            "bind": settings.agent_workdir,
                            "mode": "ro",
                        }
                    },
                    user="1000:1000",
                    remove=True,
                    labels={"purpose": "workspace-listing", "temporary": "true"},
                )
                output = raw.decode("utf-8", "replace") if raw else ""
            except Exception as exc:  # noqa: BLE001
                logger.warning("Workspace listing via throwaway failed: %s", exc)
                return None

        entries: list[dict] = []
        for line in output.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3:
                continue
            kind, size, path = fields
            if not path:
                continue  # the workspace root itself
            entries.append({
                "path": path,
                "type": "dir" if kind == "d" else "file",
                "size": int(size) if size.isdigit() else 0,
            })
        return entries

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
        """List all agent containers managed by the platform (any state).

        Returns one row per Docker container labelled managed-by=agent-platform,
        with live state (status, health, start time) straight from the daemon.
        """
        client = self._get_client()
        try:
            containers = client.containers.list(
                all=True,
                filters={"label": "managed-by=agent-platform"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Docker container listing failed: %s", exc)
            return []

        # ID of the configured agent image — the staleness baseline. A
        # missing image (bad AGENT_AGENT_IMAGE / not built yet) must never
        # flag working containers as stale, so treat it as "no baseline".
        try:
            current_image_id = client.images.get(settings.agent_image).id
        except Exception as exc:  # noqa: BLE001
            logger.warning("Configured image %s unavailable: %s", settings.agent_image, exc)
            current_image_id = None

        result = []
        for c in containers:
            state = c.attrs.get("State", {})
            # Read the image name from attrs: `c.image` issues an extra API
            # call that 404s when the image has been pruned while the
            # container still references it.
            image = (c.attrs.get("Config") or {}).get("Image") or settings.agent_image
            image_id = c.attrs.get("Image") or ""
            result.append({
                "name": c.name,
                "user_id": c.labels.get("user-id", ""),
                "status": c.status,
                "health": (state.get("Health") or {}).get("Status"),
                "image": image,
                "image_id": image_id,
                # Tags are re-pointable: only the image ID tells whether the
                # container really runs the freshly built image.
                "image_stale": bool(current_image_id and image_id and image_id != current_image_id),
                "started_at": self._normalize_docker_ts(state.get("StartedAt")),
            })
        return result

    @staticmethod
    def _normalize_docker_ts(ts: str | None) -> str | None:
        """Trim Docker's nanosecond timestamps to ISO-8601 microseconds."""
        if not ts:
            return None
        # e.g. "2026-08-20T03:14:59.123456789Z" → "...T03:14:59.123456Z"
        if "." in ts:
            head, rest = ts.split(".", 1)
            digits = "".join(ch for ch in rest if ch.isdigit())
            suffix = rest.lstrip("0123456789") or "Z"
            return f"{head}.{digits[:6]}{suffix}"
        return ts


# Global singleton — replaces the old OpencodeManager
container_manager = ContainerManager()
