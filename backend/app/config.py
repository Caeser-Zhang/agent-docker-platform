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
    agent_image: str = "agent-demo:1.3.0"
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

    # Base URL agent containers use to reach the platform-managed fastk-mcp
    # service (mcp-fastk/ in this repo; docker-compose runs it on agent-net).
    # The fastk builtin MCP manifest (agent-image/builtin-mcp/fastk) resolves
    # ${FASTK_MCP_URL} against this setting, so every user container gets the
    # knowledge-base search tools injected as a remote MCP server.
    fastk_mcp_url: str = "http://fastk-mcp:8001/mcp"

    # Root URL of the fastk REST server (fastdb serve fastapi, run on the
    # Docker/WSL host). Injected into every user container as FASTDB_BASE_URL
    # so the built-in fastk CLI (agent-image/builtin-tools/fastk-cli) — used by
    # the fastk-search / fastk-analyze skills — hits this server instead of a
    # possibly absent local one. The server serves the /fastk/api prefix.
    fastk_server_url: str = "http://host.docker.internal:8000"

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

    # --- Lifecycle ---
    health_check_interval: int = 10  # seconds
    startup_timeout: int = 120  # seconds — cold start (SDK copy + init)
    idle_threshold: int = 30 * 60  # 30 minutes in seconds
    idle_reclaim_interval: int = 5 * 60  # 5 minutes
    max_restart_per_hour: int = 5

    # --- Workspace ---
    workspace_base: str = "/tmp/agent-workspaces"

    # P1-6: destroy-time workspace backups land here (must live on the
    # backend-data volume so tar.gz exports survive backend recreation).
    backup_dir: str = "/app/data/backups"

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
