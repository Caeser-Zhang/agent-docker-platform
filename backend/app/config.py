"""Application configuration — loaded from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Database ---
    # Absolute path matches the backend-data volume in docker-compose.yml
    # so the DB survives container recreation. database.py creates the parent
    # directory if it is missing (e.g. a freshly created volume).
    database_url: str = "sqlite+aiosqlite:////app/data/agent_demo.db"

    # --- Auth ---
    secret_key: str = "demo-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440
    # Comma-separated usernames that are granted the admin role (Docker
    # management panel). Promoted at startup and on login/register.
    admin_usernames: str = ""

    # --- Docker ---
    agent_image: str = "agent-demo:1.4.0"
    agent_network: str = "agent-net"
    agent_port: int = 4096
    container_cpu_limit: float = 2.0
    container_memory_limit: str = "2g"
    container_pids_limit: int = 200

    # --- opencode runtime (the container's only job is `opencode serve`) ---
    # The developer's own opencode.json is mounted read-only into the backend at
    # this path; it is sanitized and injected into every user container.
    opencode_config_source: str = "/host-opencode/opencode.json"
    # Loopback base URLs in that config are rewritten to this hostname so a
    # local LLM proxy running on the Docker host stays reachable.
    container_host_alias: str = "host.docker.internal"
    # Directory opencode treats as the project root inside the container.
    agent_workdir: str = "/workspace"
    # Base URL of the platform SearXNG instance. Injected into every user
    # container as SEARXNG_URL for the built-in web_search MCP server
    # (agent-image/builtin-mcp/web_search). Point it at an existing instance or
    # keep the default, which matches the searxng service in docker-compose.
    searxng_url: str = "http://searxng:8080"

    # Base URL agent containers use to reach the platform LLM proxy (this
    # app's /llm-proxy router). build_container_config rewrites every
    # provider's options.baseURL to "{llm_proxy_base}/{provider_id}" so SSE
    # tool-call deltas get normalized before opencode's ai-sdk sees them.
    # "backend" resolves on agent-net via the compose service name.
    llm_proxy_base: str = "http://backend:8000/llm-proxy"

    # DEPRECATED (P0④): fastk-mcp retired. The builtin MCP manifest was deleted
    # so this URL is no longer resolved into any container config. Kept for
    # reference / potential future re-enablement.
    fastk_mcp_url: str = "http://fastk-mcp:8001/mcp"

    # Root URL of the fastk REST server (fastdb serve fastapi, run on the
    # Docker/WSL host). The BACKEND talks to this directly — routers/kb_proxy.py
    # forwards here after injecting the per-database key, and routers/fastk.py
    # resolves citation badges through it. The server serves the /fastk/api
    # prefix. Agent containers must never use it (see kb_proxy_base).
    fastk_server_url: str = "http://host.docker.internal:8000"

    # Base URL injected into every user container as FASTDB_BASE_URL, so the
    # built-in fastk CLI (agent-image/builtin-tools/fastk-cli) reaches the fastk
    # server ONLY through this app's whitelist proxy. "backend" resolves on
    # agent-net via the compose service name. Containers get FASTK_API_KEY =
    # an opaque proxy token, never a real key.
    kb_proxy_base: str = "http://backend:8000"

    # Contact name(s) rendered into the "no permission" message the fastk proxy
    # returns to agent containers (and the citation-badge 403). Plain display
    # text — separate multiple admins with 、 or , . Changing it needs a backend
    # restart only; agent containers are unaffected.
    kb_admin_contact: str = "张智骁（工号 00899219）"

    # Credential for the fastk server's GLOBAL database listing
    # (GET /fastk/api/databases/). That endpoint is server-wide, so the
    # per-domain keys in kb_domains do not apply to it. Used by the catalog
    # readers — the agent proxy's catalog branch and /api/kb/my-domains — and
    # it only ever buys *descriptions*: the authorization boundary stays the
    # domain model, so holding this key does not widen what any user may read.
    # Empty means "the server needs no key here" and no header is sent.
    # BACKEND ONLY — never injected into an agent container, which would let
    # it enumerate and read every database straight off the host gateway.
    kb_catalog_key: str = ""

    # Directory containing built-in MCP server manifests (mounted read-only
    # into the backend from the agent image source). Each subdirectory has a
    # manifest.json declaring the server's mcp config; these are discovered
    # and injected into every user container.
    builtin_mcp_dir: str = "/builtin-mcp"

    # Directory containing built-in opencode plugin manifests (mounted
    # read-only into the backend from the agent image source). Each
    # subdirectory has a manifest.json pointing at the plugin's pre-baked
    # node_modules path inside the agent image; these are discovered and
    # injected into every user container's plugin array. The plugin trees
    # live in the read-only image, so users cannot remove them.
    builtin_plugins_dir: str = "/builtin-plugins"

    # Directory containing the built-in skills baked into the agent image
    # (agent-image/builtin-skills/<name>/SKILL.md). The image entrypoint
    # seeds them into every container's global skills dir on first boot; the
    # backend only needs the names to recognise them among the skills the
    # container's opencode reports (see visibility.list_builtin_skills).
    builtin_skills_dir: str = "/builtin-skills"

    # --- Lifecycle ---
    health_check_interval: int = 10  # seconds
    startup_timeout: int = 120  # seconds — cold start (SDK copy + init)
    idle_threshold: int = 30 * 60  # 30 minutes in seconds
    idle_reclaim_interval: int = 5 * 60  # 5 minutes
    max_restart_per_hour: int = 5

    # --- PPTX template library ---
    # Shared, single-copy asset store. The backend mounts the named volume
    # rw and is the only writer; every user container gets the same volume
    # read-only, so N users cost O(1) bytes (no per-user seeding/copying).
    # Named volume (not a bind mount) so it survives host path differences
    # and gets no project-name prefix (compose `name:` pins it).
    pptx_library_volume: str = "agent-pptx-lib"
    # Mount point of that volume in BOTH the backend and user containers.
    pptx_library_dir: str = "/library/pptx"
    # Read-only repo seed directory in the backend (./library/pptx-templates).
    # Ingested add-only at startup, de-duplicated by content sha12.
    pptx_library_seed_dir: str = "/library-seed"
    # Style presets (palettes/recipes JSON) live inside the library volume so
    # they are served from the same single copy.
    # samples/ under the seed dir holds development-period material with no
    # redistribution licence (third-party decks). It is ingested as
    # source="sample" and hidden from normal users unless the operator opts in
    # here — see library/pptx-templates/README.md.
    pptx_library_allow_samples: bool = False

    # --- Workspace ---
    workspace_base: str = "/tmp/agent-workspaces"

    # P1-6: destroy-time workspace backups land here (must live on the
    # backend-data volume so tar.gz exports survive backend recreation).
    backup_dir: str = "/app/data/backups"

    # 意见反馈截图附件落盘目录（容器内绝对路径，与 backup_dir 同在 backend-data
    # 卷内，故容器重建后元数据与文件都还在）。目录不在启动时强制创建——首次写入
    # 前 mkdir(parents=True, exist_ok=True)，避免只读卷导致启动失败。
    opinion_attachment_dir: str = "/app/data/opinion-attachments"

    # --- CORS ---
    cors_origins: list[str] = ["*"]

    class Config:
        env_prefix = "AGENT_"
        env_file = ".env"

    @property
    def admin_username_set(self) -> set[str]:
        """ADMIN_USERNAMES split into a de-duplicated username set."""
        return {u.strip() for u in self.admin_usernames.split(",") if u.strip()}


settings = Settings()

# P1-5: refuse to boot on known-weak signing keys. The JWT secret derives
# Fernet keys (crypto.py) too, so a hardcoded demo value would let anyone
# mint admin tokens AND decrypt stored container passwords / API keys.
_WEAK_SECRET_KEYS = {
    "demo-secret-key-change-in-production",
    "change-this-in-production",
    "secret",
    "changeme",
    "change-me",
}

if settings.secret_key.strip().lower() in _WEAK_SECRET_KEYS or len(settings.secret_key) < 32:
    raise RuntimeError(
        "AGENT_SECRET_KEY is missing, too short (<32 chars), or set to a "
        "known-weak default. Generate a strong value first, e.g.: "
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
