"""Application configuration — loaded from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./agent_demo.db"

    # --- Auth ---
    secret_key: str = "demo-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440

    # --- Docker ---
    agent_image: str = "agent-demo:1.0.0"
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

    # --- Lifecycle ---
    health_check_interval: int = 10  # seconds
    startup_timeout: int = 120  # seconds — cold start (SDK copy + init)
    idle_threshold: int = 30 * 60  # 30 minutes in seconds
    idle_reclaim_interval: int = 5 * 60  # 5 minutes
    max_restart_per_hour: int = 5

    # --- Workspace ---
    workspace_base: str = "/tmp/agent-workspaces"

    # --- CORS ---
    cors_origins: list[str] = ["*"]

    class Config:
        env_prefix = "AGENT_"
        env_file = ".env"


settings = Settings()
