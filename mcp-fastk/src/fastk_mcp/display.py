"""Markdown formatters for FastK results.

Search/query/grep results are grouped by document (``path``), with per-chunk
score, section, chunk index, and a truncated text snippet. Files, tables of
contents, and stats are also rendered as compact Markdown.
"""

from __future__ import annotations

from typing import Any

MAX_TEXT_CHARS = 400


def _truncate(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def _score(result: dict) -> str:
    score = result.get("score")
    if score is None:
        return "-"
    try:
        return f"{float(score):.4f}"
    except (TypeError, ValueError):
        return str(score)


def _group_by_path(results: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for result in results:
        path = str(result.get("path") or "(unknown)")
        if path not in grouped:
            grouped[path] = []
            order.append(path)
        grouped[path].append(result)
    return [(path, grouped[path]) for path in order]


def format_search_results(
    results: list[dict],
    *,
    title: str = "Results",
    include_metadata: bool = False,
) -> str:
    """Render search/query/grep results grouped by document."""
    if not results:
        return f"## {title}\n\n_No results._\n"

    groups = _group_by_path(results)
    lines = [
        f"## {title}",
        "",
        f"{len(results)} result(s) across {len(groups)} file(s)",
        "",
    ]
    for path, chunks in groups:
        plural = "s" if len(chunks) != 1 else ""
        lines.append(f"### {path} ({len(chunks)} chunk{plural})")
        for chunk in chunks:
            loc_parts = []
            section = chunk.get("section")
            chunk_index = chunk.get("chunk_index")
            if section:
                loc_parts.append(f"section={section}")
            if chunk_index is not None:
                loc_parts.append(f"chunk={chunk_index}")
            loc = f" | `{' | '.join(loc_parts)}`" if loc_parts else ""
            lines.append(f"- **score** `{_score(chunk)}`{loc}")
            lines.append(f"  > {_truncate(chunk.get('text', ''))}")
            if include_metadata:
                meta = chunk.get("_metadata_dict")
                if meta:
                    lines.append(f"  _meta_: `{meta}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_list_files(files: list[dict], *, db_label: str = "") -> str:
    """Render a list of file records as a Markdown table."""
    heading = f"## Files"
    if db_label:
        heading = f"## Files — {db_label}"
    if not files:
        return f"{heading}\n\n_No files._\n"

    lines = [heading, "", f"{len(files)} file(s)", ""]
    lines.append("| path | chunks | description | updated_at |")
    lines.append("| --- | ---: | --- | --- |")
    for f in files:
        path = str(f.get("path") or "-").replace("|", "\\|")
        chunks = f.get("chunk_count", "-")
        desc = _truncate(f.get("description") or "-", 60).replace("|", "\\|")
        updated = str(f.get("updated_at") or "-")
        lines.append(f"| {path} | {chunks} | {desc} | {updated} |")
    return "\n".join(lines) + "\n"


def format_toc(entries: list[dict], *, file_path: str = "") -> str:
    """Render a table of contents as an indented list."""
    heading = f"## ToC: {file_path}" if file_path else "## ToC"
    if not entries:
        return f"{heading}\n\n_No table of contents._\n"

    lines = [heading, ""]
    for entry in entries:
        level = entry.get("level")
        try:
            level = int(level or 1)
        except (TypeError, ValueError):
            level = 1
        title = entry.get("title") or entry.get("text") or ""
        lines.append(f"{'  ' * max(level - 1, 0)}- {title}")
    return "\n".join(lines) + "\n"


def format_stats(stats: dict, *, db_label: str = "") -> str:
    """Render database stats as key/value lines."""
    heading = "## Stats"
    if db_label:
        heading = f"## Stats — {db_label}"
    if not stats:
        return f"{heading}\n\n_No stats._\n"

    lines = [heading, ""]
    for key, value in stats.items():
        lines.append(f"- **{key}**: {value}")
    return "\n".join(lines) + "\n"