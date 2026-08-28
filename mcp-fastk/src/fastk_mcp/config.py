"""Configuration models and TOML loading for fastk-mcp.

A config file declares one or more named remote FastK connections and an
optional ``default_database``. Example (see ``fastk-mcp.toml.example``)::

    default_database = "prod"

    [[databases]]
    name = "prod"
    base_url = "http://localhost:8000"
    db_name = "my-knowledge-base"
    api_key = ""
    timeout = 30.0

    [databases.search]
    topk = 10
    use_rrf = false
    rrf_k = 60
    alpha = 0.5
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - python 3.10
    import tomli as tomllib


@dataclass
class SearchDefaults:
    """Search defaults applied when a tool call omits a parameter."""

    topk: int = 10
    use_rrf: bool = False
    rrf_k: int = 60
    alpha: float = 0.5
    threshold: float | None = None
    # Progressive funnel recall: candidates = max(topk * funnel_ratio, funnel_min).
    funnel_ratio: int = 5
    funnel_min: int = 50


@dataclass
class DatabaseConfig:
    """A single named remote FastK connection."""

    name: str
    base_url: str
    db_name: str
    api_key: str | None = None
    timeout: float = 30.0
    description: str = ""
    search: SearchDefaults = field(default_factory=SearchDefaults)


@dataclass
class ServerConfig:
    """Top-level server configuration."""

    databases: list[DatabaseConfig] = field(default_factory=list)
    default_database: str | None = None

    def get(self, name: str | None = None) -> DatabaseConfig:
        name = name or self.default_database
        if name is None:
            raise ValueError("No database specified and no default_database configured.")
        for db in self.databases:
            if db.name == name:
                return db
        raise KeyError(f"Unknown database '{name}'. Available: {self.names()}")

    def names(self) -> list[str]:
        return [db.name for db in self.databases]


def load_config(path: str | Path) -> ServerConfig:
    """Load a TOML config file into a :class:`ServerConfig`."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    databases = [_parse_database(raw) for raw in data.get("databases", [])]
    if not databases:
        raise ValueError(f"No '[[databases]]' entries found in {path}")
    return ServerConfig(
        databases=databases,
        default_database=data.get("default_database"),
    )


def _parse_database(raw: dict) -> DatabaseConfig:
    search_raw = raw.get("search") or {}
    search = SearchDefaults(
        topk=int(search_raw.get("topk", 10)),
        use_rrf=bool(search_raw.get("use_rrf", False)),
        rrf_k=int(search_raw.get("rrf_k", 60)),
        alpha=float(search_raw.get("alpha", 0.5)),
        threshold=search_raw.get("threshold"),
        funnel_ratio=int(search_raw.get("funnel_ratio", 5)),
        funnel_min=int(search_raw.get("funnel_min", 50)),
    )
    return DatabaseConfig(
        name=str(raw["name"]),
        base_url=str(raw["base_url"]),
        db_name=str(raw["db_name"]),
        api_key=raw.get("api_key") or None,
        timeout=float(raw.get("timeout", 30.0)),
        description=str(raw.get("description", "")),
        search=search,
    )


def config_from_env() -> ServerConfig | None:
    """Build a single-database :class:`ServerConfig` from environment variables.

    When ``FASTK_MCP_BASE_URL`` and ``FASTK_MCP_DB_NAME`` are both set,
    no TOML file is needed. Returns ``None`` if the required vars are absent.
    """
    base_url = os.environ.get("FASTK_MCP_BASE_URL")
    db_name = os.environ.get("FASTK_MCP_DB_NAME")
    if not base_url or not db_name:
        return None
    return ServerConfig(
        databases=[
            DatabaseConfig(
                name="default",
                base_url=base_url,
                db_name=db_name,
                api_key=os.environ.get("FASTK_MCP_API_KEY") or None,
                timeout=float(os.environ.get("FASTK_MCP_TIMEOUT", "30.0")),
                description=os.environ.get("FASTK_MCP_DESCRIPTION", ""),
            )
        ],
        default_database="default",
    )