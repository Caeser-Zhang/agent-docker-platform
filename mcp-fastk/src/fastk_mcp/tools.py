"""Tool implementations for the fastk-mcp server.

Each tool returns a Markdown string (the display layer) so an MCP client / LLM
can consume it directly. Errors are returned as strings rather than raised so
that a transient network/remote error surfaces as a readable tool result.
"""

from __future__ import annotations

from . import display
from .client import ClientPool, RemoteFastDB
from .config import DatabaseConfig, ServerConfig


def _resolve(pool: ClientPool, database: str | None) -> tuple[DatabaseConfig, RemoteFastDB]:
    db = pool.database(database)
    return db, pool.get(database)


def _error(exc: Exception) -> str:
    return f"_Error_ `{type(exc).__name__}`: {exc}"


def _diversify(results: list[dict], topk: int, per_file: int) -> list[dict]:
    """Diversity-aware trim: keep at most ``per_file`` chunks per path, in score order."""
    seen: dict[str, int] = {}
    selected: list[dict] = []
    for result in results:
        path = str(result.get("path") or "(unknown)")
        if seen.get(path, 0) >= per_file:
            continue
        selected.append(result)
        seen[path] = seen.get(path, 0) + 1
        if len(selected) >= topk:
            break
    return selected


def list_databases(config: ServerConfig) -> str:
    """List configured databases."""
    lines = ["## Configured databases", ""]
    for db in config.databases:
        marker = " (default)" if db.name == config.default_database else ""
        suffix = f" — {db.description}" if db.description else ""
        lines.append(f"- **{db.name}**{marker}: `{db.db_name}` @ {db.base_url}{suffix}")
    if not config.databases:
        lines.append("_No databases configured._")
    return "\n".join(lines) + "\n"


def list_files(
    pool: ClientPool,
    database: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    try:
        db, client = _resolve(pool, database)
        files = client.list_files(limit=limit, offset=offset)
        return display.format_list_files(files, db_label=db.name)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def get_stats(pool: ClientPool, database: str | None = None) -> str:
    try:
        db, client = _resolve(pool, database)
        return display.format_stats(client.get_stats(), db_label=db.name)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def toc(pool: ClientPool, database: str | None = None, file_path: str = "") -> str:
    try:
        db, client = _resolve(pool, database)
        return display.format_toc(client.toc(file_path=file_path), file_path=file_path)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def search(
    pool: ClientPool,
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
    try:
        db, client = _resolve(pool, database)
        sd = db.search
        topk = topk if topk is not None else sd.topk

        if progressive:
            recall = max(topk * sd.funnel_ratio, sd.funnel_min)
            results = client.search(
                query=query,
                topk=recall,
                use_rrf=use_rrf,
                rrf_k=rrf_k,
                alpha=alpha,
                threshold=threshold,
                filters=filters,
            )
            per_file = max(1, topk // 3)
            results = _diversify(results, topk, per_file)
        else:
            results = client.search(
                query=query,
                topk=topk,
                use_rrf=use_rrf,
                rrf_k=rrf_k,
                alpha=alpha,
                threshold=threshold,
                filters=filters,
            )

        return display.format_search_results(results, title=f"Search: {query}")
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def query(
    pool: ClientPool,
    database: str | None = None,
    filters: str = "",
    limit: int = 1000,
) -> str:
    try:
        db, client = _resolve(pool, database)
        results = client.query(filters=filters, limit=limit)
        return display.format_search_results(results, title=f"Query: {filters}")
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def grep(
    pool: ClientPool,
    database: str | None = None,
    text_pattern: str = "",
    path_pattern: str | None = None,
    topk: int | None = None,
    filters: str | None = None,
) -> str:
    try:
        db, client = _resolve(pool, database)
        topk = topk if topk is not None else db.search.topk
        results = client.grep(
            text_pattern=text_pattern,
            path_pattern=path_pattern,
            topk=topk,
            filters=filters,
        )
        return display.format_search_results(results, title=f"Grep: {text_pattern}")
    except Exception as exc:  # noqa: BLE001
        return _error(exc)