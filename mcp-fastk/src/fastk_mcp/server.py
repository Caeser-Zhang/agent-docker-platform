"""FastMCP server exposing remote FastK knowledge-base tools.

The exposed tools form a "drill-down" progressive-search workflow::

    list_databases / list_files  ->  toc  ->  search / query / grep

``search`` additionally supports an internal *funnel* (``progressive=True``):
broad recall, then a diversity-aware trim so no single document dominates the
top results.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastmcp import FastMCP

from . import tools
from .client import ClientPool
from .config import ServerConfig, config_from_env, load_config

_FILTER_HELP = (
    "Pythonic filter expression, e.g. \"path == 'docs/guide.md'\", "
    "\"section.contains('简介')\", \"file_id in ['a','b']\", combined with and/or."
)


def create_server(config: ServerConfig) -> FastMCP:
    """Build a FastMCP server backed by the given :class:`ServerConfig`."""
    mcp = FastMCP(
        "fastk-mcp",
        instructions=(
            "Read-only search over remote FastK knowledge bases. "
            "Drill down: list files -> toc -> search/query/grep."
        ),
    )
    pool = ClientPool(config)

    @mcp.tool
    def list_databases() -> str:
        """List all configured remote FastK databases (name, target db, base URL)."""
        return tools.list_databases(config)

    @mcp.tool
    def list_files(
        database: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> str:
        """List documents (files) in a database with chunk counts and descriptions.

        Args:
            database: Configured database name; defaults to ``default_database``.
            limit: Max files to return. None = all files.
            offset: Number of files to skip.
        """
        return tools.list_files(pool, database=database, limit=limit, offset=offset)

    @mcp.tool
    def get_stats(database: str | None = None) -> str:
        """Return file and chunk counts for a database.

        Args:
            database: Configured database name; defaults to ``default_database``.
        """
        return tools.get_stats(pool, database=database)

    @mcp.tool
    def toc(database: str | None = None, file_path: str = "") -> str:
        """Return the table of contents (headings) for a specific document.

        Args:
            database: Configured database name; defaults to ``default_database``.
            file_path: Document path as shown by ``list_files``.
        """
        return tools.toc(pool, database=database, file_path=file_path)

    @mcp.tool
    def search(
        database: str | None = None,
        query: str = "",
        topk: int | None = None,
        use_rrf: bool | None = None,
        rrf_k: int | None = None,
        alpha: float | None = None,
        threshold: float | None = None,
        filters: str | None = None,
        progressive: bool = True,
    ) -> str:
        """Hybrid (dense + full-text) search, grouped by document.

        Args:
            database: Configured database name; defaults to ``default_database``.
            query: Search query text.
            topk: Number of results to return (default from config).
            use_rrf: Use RRF fusion instead of DBSF (default from config).
            rrf_k: RRF ranking parameter, only when ``use_rrf`` is True.
            alpha: Dense-score weight in DBSF (0.0-1.0).
            threshold: Minimum fused score (DBSF only).
            filters: """ + _FILTER_HELP + """
            progressive: Broad recall + diversity-aware trim (single doc won't dominate).
        """
        return tools.search(
            pool,
            database=database,
            query=query,
            topk=topk,
            use_rrf=use_rrf,
            rrf_k=rrf_k,
            alpha=alpha,
            threshold=threshold,
            filters=filters,
            progressive=progressive,
        )

    @mcp.tool
    def query(
        database: str | None = None,
        filters: str = "",
        limit: int = 1000,
    ) -> str:
        """Filter-only query (no vector search), grouped by document.

        Args:
            database: Configured database name; defaults to ``default_database``.
            filters: """ + _FILTER_HELP + """
            limit: Max results to return.
        """
        return tools.query(pool, database=database, filters=filters, limit=limit)

    @mcp.tool
    def grep(
        database: str | None = None,
        text_pattern: str = "",
        path_pattern: str | None = None,
        topk: int | None = None,
        filters: str | None = None,
    ) -> str:
        """Regex search over chunk text, grouped by document.

        Args:
            database: Configured database name; defaults to ``default_database``.
            text_pattern: Regex to match against chunk text.
            path_pattern: Optional glob pattern restricting file paths.
            topk: Max results to return (default from config).
            filters: """ + _FILTER_HELP + """
        """
        return tools.grep(
            pool,
            database=database,
            text_pattern=text_pattern,
            path_pattern=path_pattern,
            topk=topk,
            filters=filters,
        )

    return mcp


def _parse_args(argv: list[str]) -> tuple[str | None, int | None]:
    """Return (config_path, http_port) from CLI args.

    Supported forms: ``[config_path]``, ``--http [PORT] [config_path]``,
    ``[config_path] --http [PORT]``. ``http_port`` is None for stdio mode.
    """
    config_path: str | None = None
    http_port: int | None = None
    positional: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--http":
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                http_port = int(argv[i + 1])
                i += 2
                continue
            http_port = 8000
        else:
            positional.append(arg)
        i += 1
    if positional:
        config_path = positional[0]
    return config_path, http_port


def _load_config(config_path: str | None = None) -> ServerConfig:
    # 1. Explicit config path argument.
    if config_path is not None:
        return load_config(config_path)
    # 2. FASTK_MCP_CONFIG env var.
    env_path = os.environ.get("FASTK_MCP_CONFIG")
    if env_path:
        return load_config(env_path)
    # 3. Single-connection env vars (no file needed).
    env_config = config_from_env()
    if env_config is not None:
        return env_config
    # 4. Default file in the working directory.
    if Path("fastk-mcp.toml").exists():
        return load_config("fastk-mcp.toml")
    raise SystemExit(
        "No configuration found. Pass a config path as an argument, set "
        "FASTK_MCP_CONFIG, or set FASTK_MCP_BASE_URL + FASTK_MCP_DB_NAME."
    )


def main() -> None:
    config_path, http_port = _parse_args(sys.argv)
    # HTTP mode takes the port from --http flag or FASTK_MCP_HTTP_PORT env var.
    if http_port is None:
        env_port = os.environ.get("FASTK_MCP_HTTP_PORT")
        if env_port:
            http_port = int(env_port)
    mcp = create_server(_load_config(config_path))
    if http_port is not None:
        mcp.run(
            transport="http",
            host="0.0.0.0",
            port=http_port,
            show_banner=True,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()