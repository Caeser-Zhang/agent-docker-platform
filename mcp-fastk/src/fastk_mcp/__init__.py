"""fastk-mcp: MCP server for remote FastK knowledge bases."""

from .client import ClientPool
from .config import DatabaseConfig, SearchDefaults, ServerConfig, config_from_env, load_config
from .server import create_server

__all__ = [
    "ClientPool",
    "DatabaseConfig",
    "SearchDefaults",
    "ServerConfig",
    "config_from_env",
    "create_server",
    "load_config",
]

__version__ = "0.1.0"