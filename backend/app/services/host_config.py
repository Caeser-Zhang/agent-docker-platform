"""Host opencode.json management — MCP, Provider, Skill CRUD.

The host opencode.json at ~/.config/opencode/opencode.json is the single source
of truth for provider credentials, MCP servers, and agent configuration.

This module provides read/write operations on that file. Every write is:
  1. Read the current file
  2. Merge the change
  3. Write back atomically
  4. Optionally reload into running containers (caller's responsibility)

Skills are managed as files under ~/.config/opencode/skills/<name>/SKILL.md,
not as JSON keys, so they get their own file-level operations.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# Where skill files live on the host.
SKILLS_DIR = Path(settings.opencode_config_source).parent / "skills"

# Name validation for MCP servers and skills (opencode requirement).
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _validate_name(name: str) -> str:
    if not name or len(name) > 64 or not _NAME_RE.match(name):
        raise ValueError(f"Invalid name '{name}': must be 1-64 lowercase alphanumeric with single hyphens")
    return name


def _read_host_config() -> dict:
    """Read the raw host opencode.json (no sanitization)."""
    path = Path(settings.opencode_config_source)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_host_config(config: dict) -> None:
    """Write the host opencode.json atomically."""
    path = Path(settings.opencode_config_source)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("Host opencode.json updated: %s", path)


# ------------------------------------------------------------------
#  Provider CRUD
# ------------------------------------------------------------------

def list_providers_raw() -> dict[str, dict]:
    """Return the raw provider section from the host config."""
    return _read_host_config().get("provider") or {}


def get_provider(provider_id: str) -> dict | None:
    return list_providers_raw().get(provider_id)


def upsert_provider(provider_id: str, config: dict) -> dict:
    """Create or update a provider in the host config."""
    config_data = _read_host_config()
    providers = config_data.setdefault("provider", {})
    providers[provider_id] = config
    _write_host_config(config_data)
    return config


def delete_provider(provider_id: str) -> bool:
    config_data = _read_host_config()
    providers = config_data.get("provider") or {}
    if provider_id not in providers:
        return False
    providers.pop(provider_id)
    _write_host_config(config_data)
    return True


# ------------------------------------------------------------------
#  MCP server CRUD
# ------------------------------------------------------------------

def list_mcp_servers() -> dict[str, dict]:
    """Return the raw mcp section from the host config."""
    return _read_host_config().get("mcp") or {}


def get_mcp_server(name: str) -> dict | None:
    return list_mcp_servers().get(name)


def upsert_mcp_server(name: str, config: dict) -> dict:
    """Create or update an MCP server entry.

    For local servers:
      {"type": "local", "command": ["npx", "-y", "pkg"], "enabled": true}
    For remote servers:
      {"type": "remote", "url": "https://...", "enabled": true, "headers": {}}
    """
    _validate_name(name)
    if config.get("type") not in ("local", "remote"):
        raise ValueError("MCP type must be 'local' or 'remote'")
    if config.get("type") == "local" and not config.get("command"):
        raise ValueError("Local MCP server requires 'command' array")
    if config.get("type") == "remote" and not config.get("url"):
        raise ValueError("Remote MCP server requires 'url'")

    config_data = _read_host_config()
    mcp = config_data.setdefault("mcp", {})
    mcp[name] = config
    _write_host_config(config_data)
    return config


def delete_mcp_server(name: str) -> bool:
    config_data = _read_host_config()
    mcp = config_data.get("mcp") or {}
    if name not in mcp:
        return False
    mcp.pop(name)
    _write_host_config(config_data)
    return True


def toggle_mcp_server(name: str, enabled: bool) -> dict | None:
    """Enable or disable an MCP server without removing it."""
    config_data = _read_host_config()
    mcp = config_data.get("mcp") or {}
    if name not in mcp:
        return None
    mcp[name]["enabled"] = enabled
    _write_host_config(config_data)
    return mcp[name]


# ------------------------------------------------------------------
#  Skill CRUD (file-based)
# ------------------------------------------------------------------

def list_skills() -> list[dict]:
    """List all skills from the skills directory."""
    if not SKILLS_DIR.is_dir():
        return []
    skills = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        meta = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
        if meta:
            skills.append({
                "name": meta.get("name", skill_dir.name),
                "description": meta.get("description", ""),
                "dir": skill_dir.name,
            })
    return skills


def get_skill(name: str) -> dict | None:
    """Get a single skill's metadata and content."""
    skill_dir = SKILLS_DIR / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    content = skill_md.read_text(encoding="utf-8")
    meta = _parse_skill_frontmatter(content)
    return {
        "name": meta.get("name", name) if meta else name,
        "description": meta.get("description", "") if meta else "",
        "content": content,
        "dir": name,
    }


def upsert_skill(name: str, content: str) -> dict:
    """Create or update a skill SKILL.md file."""
    _validate_name(name)
    # Validate frontmatter contains required fields
    meta = _parse_skill_frontmatter(content)
    if not meta or not meta.get("name") or not meta.get("description"):
        raise ValueError("SKILL.md must contain frontmatter with 'name' and 'description' fields")

    skill_dir = SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    logger.info("Skill '%s' saved to %s", name, skill_dir / "SKILL.md")
    return {
        "name": meta["name"],
        "description": meta["description"],
        "dir": name,
    }


def delete_skill(name: str) -> bool:
    """Delete a skill directory."""
    import shutil
    skill_dir = SKILLS_DIR / name
    if not skill_dir.is_dir():
        return False
    shutil.rmtree(skill_dir)
    logger.info("Skill '%s' deleted from %s", name, skill_dir)
    return True


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------

def _parse_skill_frontmatter(content: str) -> dict[str, str] | None:
    """Parse YAML frontmatter from SKILL.md content.

    Only extracts top-level key: value pairs. Does not handle nested structures.
    """
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None
    frontmatter = content[3:end].strip()
    result: dict[str, str] = {}
    for line in frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
    return result if result else None
