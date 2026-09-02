"""Workspace (project-scope) config & skills routes.

opencode merges configuration from multiple scopes (later overrides earlier):
  1. remote (.well-known)  2. global ~/.config/opencode/opencode.json
  3. OPENCODE_CONFIG env   4. project opencode.json (project root)
  5. .opencode directory   6. OPENCODE_CONFIG_CONTENT env

This platform manages two of them:
  - global  — host ~/.config/opencode (routers/config.py, injected into the
    container's /data/config/opencode on every container start)
  - project — the files inside the user's workspace volume (this router):
      /workspace/opencode.json              project config (highest std priority)
      /workspace/.opencode/skills/<name>/   project-scope skills

All operations go through the Docker archive API, so they work whether the
container is running or stopped. A zip import unpacks skill packages into
.opencode/skills/ as project-scope skills.

Endpoints:
  GET    /api/workspace/config            — project opencode.json (creates skeleton
                                             if missing, per requirement)
  PUT    /api/workspace/config            — save project opencode.json (JSON validated)
  GET    /api/workspace/skills            — list project-scope skills
  GET    /api/workspace/skills/{name}     — get one project skill
  POST   /api/workspace/skills/{name}     — create/update one project skill
  DELETE /api/workspace/skills/{name}     — delete one project skill
  POST   /api/workspace/skills/import     — upload a skill archive (.zip/.rar/
                                            .7z/.tar/.tar.gz/.tar.bz2/.tar.xz),
                                            unpack into .opencode/skills/
"""
import asyncio
import json
import logging
import tempfile
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel

from ..auth import get_current_user
from ..models import User
from ..services import host_config
from ..services.archive_extract import (
    ExtractLimits,
    SUPPORTED_EXTENSIONS,
    detect_format,
    extract_archive,
)
from ..services.agent_controller import agent_controller
from ..services.container_manager import container_manager
from ..services.host_config import _parse_skill_frontmatter, _validate_name
from ..services.opencode_config import _discover_builtin_plugins
from ..services.tunnel_relay import tunnel_relay

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspace", tags=["workspace"])

# opencode's project-scope locations, relative to the workspace root.
PROJECT_CONFIG_REL = "opencode.json"
PROJECT_SKILLS_REL = ".opencode/skills"

# Import limits for uploaded skill archives. The caps on extracted content
# guard against decompression bombs; MAX_UPLOAD_COMPRESSED caps the raw
# upload itself so huge bodies fail fast instead of buffering in memory.
MAX_IMPORT_FILES = 500
MAX_IMPORT_TOTAL = 20 * 1024 * 1024
MAX_IMPORT_FILE = 5 * 1024 * 1024
MAX_UPLOAD_COMPRESSED = 50 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
IMPORT_LIMITS = ExtractLimits(
    max_files=MAX_IMPORT_FILES,
    max_file_size=MAX_IMPORT_FILE,
    max_total_size=MAX_IMPORT_TOTAL,
)
SUPPORTED_IMPORT_MESSAGE = "不支持的压缩包格式，支持的格式: " + ", ".join(SUPPORTED_EXTENSIONS)


class ProjectConfigSave(BaseModel):
    content: str  # raw JSON text of the project opencode.json


class SkillCreate(BaseModel):
    content: str  # raw SKILL.md text with frontmatter


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------

async def _require_container(user: User) -> None:
    # Docker SDK calls block; keep them off the event loop (P0-1a).
    if await asyncio.to_thread(container_manager.get_container, user.id) is None:
        raise HTTPException(
            status_code=409,
            detail="Agent 容器尚未创建，请先启动 Agent 再管理项目级配置",
        )


async def _list_project_skills(user_id: str) -> list[dict]:
    """List project-scope skills from .opencode/skills/<name>/SKILL.md."""
    tree = await asyncio.to_thread(
        container_manager.read_workspace_tree, user_id, PROJECT_SKILLS_REL
    )
    if not tree:
        return []
    skills: list[dict] = []
    for rel, content in sorted(tree.items()):
        parts = rel.split("/")
        # Only top-level skill dirs: <name>/SKILL.md
        if len(parts) != 2 or parts[1] != "SKILL.md":
            continue
        text = content.decode("utf-8", errors="replace")
        meta = _parse_skill_frontmatter(text) or {}
        skills.append({
            "name": meta.get("name") or parts[0],
            "description": meta.get("description", ""),
            "dir": parts[0],
            "scope": "project",
        })
    return skills


async def _list_builtin_skills(user_id: str) -> list[dict]:
    """List plugin-registered skills from the running container's opencode.

    Built-in plugin skills live inside the plugin's pre-baked node_modules
    tree in the read-only agent image, so the backend cannot see them on the
    host — only the opencode server inside the container knows them. Query
    its native GET /skill through the relay and keep the entries whose
    ``location`` falls under a built-in plugin path. Returns [] whenever the
    container is not running or the call fails (the file-scan scopes still
    cover global/project then).
    """
    running, password = await agent_controller.get_agent_gate(user_id)
    if not running or not password:
        return []
    plugin_prefixes = [p.rstrip("/") + "/" for p in _discover_builtin_plugins()]
    if not plugin_prefixes:
        return []
    try:
        resp = await tunnel_relay.http_request(
            user_id, "GET", "/skill", password=password, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Native skill listing via relay failed: %s", exc)
        return []
    if resp.get("status") != 200 or not isinstance(resp.get("body"), list):
        return []
    skills: list[dict] = []
    for s in resp["body"]:
        location = s.get("location") or ""
        if not any(location.startswith(p) for p in plugin_prefixes):
            continue
        if s.get("name"):
            skills.append({
                "name": s["name"],
                "description": s.get("description", ""),
                "dir": location,
                "scope": "builtin",
            })
    return skills


async def _list_all_skills(user_id: str) -> list[dict]:
    """Global (host) + project (workspace) + built-in plugin skills, tagged
    by scope."""
    out: list[dict] = []
    try:
        for s in host_config.list_skills():
            out.append({
                "name": s["name"],
                "description": s.get("description", ""),
                "dir": s["name"],
                "scope": "global",
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Global skill listing failed: %s", exc)
    out.extend(await _list_project_skills(user_id))
    # Plugin skills are last-resort: on a name clash the file-based (and thus
    # platform-manageable) entry wins.
    existing = {s["name"] for s in out}
    out.extend(
        s for s in await _list_builtin_skills(user_id) if s["name"] not in existing
    )
    return out


def _safe_upload_name(filename: str) -> str:
    """Sanitise an upload filename to a plain basename."""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="无效的文件名")
    return name


async def _read_upload_capped(file: UploadFile, cap: int) -> bytes:
    """Stream the upload in fixed-size chunks so an oversized body is rejected
    as soon as it crosses the cap instead of being buffered into memory."""
    buf = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_SIZE):
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(
                status_code=413,
                detail=f"压缩包过大（超过 {cap // 1024 // 1024}MB 上限）",
            )
    return bytes(buf)


def _extract_skills_from_archive(
    data: bytes, filename: str
) -> list[tuple[str, dict[str, bytes]]]:
    """Detect the skill layout inside an archive and normalise to [(name, files)].

    Accepts .zip / .rar / .7z / .tar / .tar.gz / .tar.bz2 / .tar.xz.
    Supported layouts (mirrors how people actually package skills):
      1. bare:       SKILL.md (+ resources) at the archive root
      2. wrapped:    <skill-name>/SKILL.md (+ resources)
      3. multi:      <name-a>/SKILL.md, <name-b>/SKILL.md, ...
    """
    fmt = detect_format(filename, data[:512])
    if fmt is None:
        raise ValueError(SUPPORTED_IMPORT_MESSAGE)
    entries = extract_archive(data, fmt, IMPORT_LIMITS)

    # Group entries by top-level segment.
    groups: dict[str, dict[str, bytes]] = {}
    for path, content in entries.items():
        top, _, rest = path.partition("/")
        if rest:
            groups.setdefault(top, {})[rest] = content
        else:
            groups.setdefault("", {})[top] = content

    skills: list[tuple[str | None, dict[str, bytes]]] = []
    root_files = groups.pop("", None)
    if root_files is not None:
        if "SKILL.md" not in root_files:
            raise ValueError("压缩包根目录缺少 SKILL.md（裸 skill 结构要求根目录含 SKILL.md）")
        skills.append((None, root_files))
    for dir_name, files in groups.items():
        if "SKILL.md" not in files:
            raise ValueError(f"目录 '{dir_name}' 缺少 SKILL.md")
        skills.append((dir_name, files))
    if not skills:
        raise ValueError("未在压缩包中找到有效的 skill（缺少 SKILL.md）")

    # Resolve names: frontmatter `name` wins, falls back to the directory name.
    resolved: list[tuple[str, dict[str, bytes]]] = []
    seen: set[str] = set()
    for dir_name, files in skills:
        text = files["SKILL.md"].decode("utf-8", errors="replace")
        meta = _parse_skill_frontmatter(text) or {}
        label = dir_name if dir_name else "压缩包根目录"
        if not meta.get("description"):
            raise ValueError(f"{label} 的 SKILL.md 缺少必填 frontmatter 字段 'description'")
        name = (meta.get("name") or dir_name or "").strip()
        try:
            _validate_name(name)
        except ValueError as exc:
            raise ValueError(f"{label} 的 skill 名称无效: {exc}") from exc
        if name in seen:
            raise ValueError(f"压缩包内存在重复的 skill 名称: {name}")
        seen.add(name)
        resolved.append((name, files))
    return resolved


# ------------------------------------------------------------------
#  Project config (workspace opencode.json)
# ------------------------------------------------------------------

@router.get("/config")
async def get_project_config(user: User = Depends(get_current_user)):
    """Read the project-scope opencode.json, creating a skeleton if absent."""
    await _require_container(user)
    raw = await asyncio.to_thread(
        container_manager.read_workspace_file, user.id, PROJECT_CONFIG_REL
    )
    created = False
    if raw is None:
        # Requirement: materialise the project config file when missing so the
        # user always has something editable in the workspace.
        skeleton = '{\n  "$schema": "https://opencode.ai/config.json"\n}\n'
        if not await asyncio.to_thread(
            container_manager.write_workspace_files,
            user.id, {PROJECT_CONFIG_REL: skeleton.encode("utf-8")},
        ):
            raise HTTPException(status_code=500, detail="无法在工作空间创建项目级配置文件")
        raw = skeleton.encode("utf-8")
        created = True

    text = raw.decode("utf-8", errors="replace")
    valid = True
    parsed: dict | None = {}
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        valid = False
    return {
        "scope": "project",
        "exists": True,
        "created": created,
        "valid": valid,
        "content": text,
        "config": parsed,
    }


@router.put("/config")
async def save_project_config(body: ProjectConfigSave, user: User = Depends(get_current_user)):
    """Save the project-scope opencode.json (full-file replacement)."""
    await _require_container(user)
    try:
        parsed = json.loads(body.content) if body.content.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("顶层必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"无效的 JSON: {exc}")
    if len(body.content) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="配置文件超过 1MB 上限")

    ok = await asyncio.to_thread(
        container_manager.write_workspace_files,
        user.id, {PROJECT_CONFIG_REL: body.content.encode("utf-8")},
    )
    if not ok:
        raise HTTPException(status_code=500, detail="写入工作空间失败")
    logger.info("Project opencode.json saved by %s", user.username)
    return {"status": "ok", "message": "已保存，重启 Agent 后生效（点击「重载到容器」）"}


# ------------------------------------------------------------------
#  Chat attach: merged skill list + file upload into the workspace
# ------------------------------------------------------------------

@router.get("/skills/all")
async def list_all_skills(user: User = Depends(get_current_user)):
    """Global + project + built-in plugin skills in one list, tagged with
    `scope` — feeds the input-box skill picker in the chat UI."""
    await _require_container(user)
    return {"skills": await _list_all_skills(user.id)}


# Upload limits for chat attachments (kept modest; the workspace volume is
# per-user and ephemeral anyway).
MAX_UPLOAD_FILE = 10 * 1024 * 1024

# Chat attachments land in tmp/ — session-scoped scratch files that are
# deliberately separated from the user's project files. They stay inside the
# workspace so the agent (and @-references) can reach them with relative
# paths, but they never pollute the project root.
UPLOADS_REL = "tmp"


@router.post("/files/upload")
async def upload_chat_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Store a chat attachment in the workspace volume and return the
    container-relative path, size and mime type.

    The frontend then references this path in the prompt (text part or file
    part), so the model sees a real file inside its own container instead of
    a browser-side blob.
    """
    await _require_container(user)
    name = _safe_upload_name(file.filename or "")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    if len(data) > MAX_UPLOAD_FILE:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_UPLOAD_FILE // 1024 // 1024}MB 上限"
        )

    rel = f"{UPLOADS_REL}/{name}"
    if not await asyncio.to_thread(
        container_manager.write_workspace_files, user.id, {rel: data}
    ):
        raise HTTPException(status_code=500, detail="写入工作空间失败")

    mime = file.content_type or "application/octet-stream"
    logger.info("Chat file '%s' (%d bytes, %s) uploaded by %s", rel, len(data), mime, user.username)
    return {
        "status": "ok",
        "path": rel,                # container-relative path, e.g. tmp/foo.png
        "absPath": f"/workspace/{rel}",
        "filename": name,
        "size": len(data),
        "mime": mime,
        "isImage": mime.startswith("image/"),
    }


# ------------------------------------------------------------------
#  Workspace file browser (tree + content for the preview pane)
# ------------------------------------------------------------------

# Extension → mime for the preview pane. The upload endpoint trusts the
# browser's content type, but workspace files written by the agent carry no
# such metadata, so derive it from the extension.
MIME_BY_EXT = {
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".js": "text/javascript",
    ".ts": "text/javascript",
    ".tsx": "text/javascript",
    ".py": "text/x-python",
    ".sh": "text/x-sh",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


@router.get("/files")
async def list_workspace_files(user: User = Depends(get_current_user)):
    """Flat listing of the workspace tree for the file-browser sidebar.

    Returns [{path, type, size}] with workspace-relative paths; the frontend
    assembles the tree. Heavy dirs (.git, node_modules, caches) are pruned.
    """
    await _require_container(user)
    entries = await asyncio.to_thread(container_manager.list_workspace, user.id)
    if entries is None:
        raise HTTPException(status_code=500, detail="无法读取工作空间目录")
    return {"files": entries}


@router.get("/file-content")
async def read_workspace_file_content(
    path: str,
    user: User = Depends(get_current_user),
):
    """Read one workspace file for the preview pane.

    Text-ish files come back as {type:"text", content, mime}; images as
    {type:"image", mime, base64}. Anything larger than 2MB is refused —
    previews are for reading, not for hauling binaries.
    """
    await _require_container(user)
    data = await asyncio.to_thread(container_manager.read_workspace_file, user.id, path)
    if data is None:
        raise HTTPException(status_code=404, detail="文件不存在或不可读")
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件过大（>2MB），无法预览")

    ext = "." + (path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else "")
    mime = MIME_BY_EXT.get(ext, "application/octet-stream")
    if mime.startswith("image/"):
        import base64
        return {
            "type": "image",
            "mime": mime,
            "base64": base64.b64encode(data).decode("ascii"),
        }
    # Binary detection: NUL byte in the first 4KB means "not previewable text".
    if b"\x00" in data[:4096]:
        return {"type": "binary", "mime": mime, "size": len(data)}
    return {
        "type": "text",
        "mime": mime,
        "content": data.decode("utf-8", errors="replace"),
    }


# ------------------------------------------------------------------
#  Workspace file download (batch zip)
# ------------------------------------------------------------------

# Download caps: unlike the 2MB preview cap this endpoint hauls real data
# out of the workspace, so the limits are generous but still bounded to
# keep a runaway request from buffering an unbounded archive in RAM.
MAX_DOWNLOAD_PATHS = 200
MAX_DOWNLOAD_TOTAL = 100 * 1024 * 1024


class WorkspaceDownloadRequest(BaseModel):
    paths: list[str]  # workspace-relative files or directories


@router.post("/files/download")
async def download_workspace_files(
    body: WorkspaceDownloadRequest,
    user: User = Depends(get_current_user),
):
    """Zip the requested workspace files/directories and return the archive.

    Each selected path keeps its workspace-relative location inside the zip
    (directories are walked recursively). Docker SDK reads and the zip build
    run in worker threads; the archive is spooled to a temp file past a
    threshold so near-cap selections do not have to live entirely in RAM.
    """
    await _require_container(user)

    # de-dup while keeping selection order
    paths = list(dict.fromkeys(p.strip() for p in body.paths if p and p.strip()))
    if not paths:
        raise HTTPException(status_code=400, detail="未选择任何下载路径")
    if len(paths) > MAX_DOWNLOAD_PATHS:
        raise HTTPException(
            status_code=400, detail=f"一次最多下载 {MAX_DOWNLOAD_PATHS} 个路径"
        )

    def _collect() -> tuple[dict[str, bytes] | None, str]:
        files: dict[str, bytes] = {}
        for rel in paths:
            data = container_manager.read_workspace_file(user.id, rel)
            if data is not None:
                files[rel] = data
                continue
            # Not a file — try a recursive directory walk ({} for empty dir).
            tree = container_manager.read_workspace_tree(user.id, rel)
            if tree is None:
                return None, rel  # neither file nor directory
            for sub, content in tree.items():
                files[f"{rel}/{sub}"] = content
        return files, ""

    files, missing = await asyncio.to_thread(_collect)
    if files is None:
        raise HTTPException(status_code=404, detail=f"路径不存在或不可读: {missing}")
    if not files:
        raise HTTPException(status_code=404, detail="所选路径没有可下载的文件")

    total = sum(len(v) for v in files.values())
    if total > MAX_DOWNLOAD_TOTAL:
        raise HTTPException(
            status_code=413,
            detail=(
                f"所选内容共 {total // 1024 // 1024}MB，"
                f"超过 {MAX_DOWNLOAD_TOTAL // 1024 // 1024}MB 下载上限"
            ),
        )

    def _zip() -> bytes:
        # SpooledTemporaryFile rolls over to disk past max_size, bounding the
        # backend's peak memory even for near-cap selections.
        with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as buf:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, content in sorted(files.items()):
                    zf.writestr(name, content)
            buf.seek(0)
            return buf.read()

    data = await asyncio.to_thread(_zip)
    logger.info(
        "Workspace download: %d path(s) → %d file(s), %.1fMB zipped for %s",
        len(paths), len(files), len(data) / 1024 / 1024, user.username,
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="workspace-files.zip"'},
    )


# ------------------------------------------------------------------
#  Project skills (workspace .opencode/skills/)
# ------------------------------------------------------------------

@router.get("/skills")
async def list_project_skills(user: User = Depends(get_current_user)):
    await _require_container(user)
    return {"skills": await _list_project_skills(user.id)}


@router.post("/skills/import")
async def import_skills_archive(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a skill archive (.zip/.rar/.7z/.tar[.gz/.bz2/.xz]) and unpack it
    into .opencode/skills/ (project scope).

    Must be defined BEFORE /skills/{name} so FastAPI matches the literal
    path "import" instead of capturing it as a {name} parameter.
    """
    await _require_container(user)
    data = await _read_upload_capped(file, MAX_UPLOAD_COMPRESSED)
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    try:
        # Extraction is CPU/IO bound over the whole payload; keep it off the
        # event loop so other requests stay responsive while it runs.
        skills = await asyncio.to_thread(
            _extract_skills_from_archive, data, file.filename or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Replace semantics: drop any previous copy of each imported skill first,
    # so stale files from an older version don't linger in the directory.
    for name, _files in skills:
        await asyncio.to_thread(
            container_manager.delete_workspace_path,
            user.id, f"{PROJECT_SKILLS_REL}/{name}",
        )

    written: dict[str, bytes] = {}
    for name, files in skills:
        for rel, content in files.items():
            written[f"{PROJECT_SKILLS_REL}/{name}/{rel}"] = content
    if not await asyncio.to_thread(container_manager.write_workspace_files, user.id, written):
        raise HTTPException(status_code=500, detail="写入工作空间失败")

    imported = [
        {
            "name": name,
            "description": (_parse_skill_frontmatter(files["SKILL.md"].decode("utf-8", "replace")) or {}).get("description", ""),
            "dir": name,
            "scope": "project",
            "fileCount": len(files),
        }
        for name, files in skills
    ]
    logger.info(
        "Imported %d project skill(s) from '%s' by %s: %s",
        len(imported), file.filename, user.username, [s["name"] for s in imported],
    )
    return {
        "status": "ok",
        "imported": imported,
        "message": f"已导入 {len(imported)} 个 skill，重启 Agent 后生效（点击「重载到容器」）",
    }


@router.get("/skills/{name}")
async def get_project_skill(name: str, user: User = Depends(get_current_user)):
    await _require_container(user)
    try:
        _validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raw = await asyncio.to_thread(
        container_manager.read_workspace_file,
        user.id, f"{PROJECT_SKILLS_REL}/{name}/SKILL.md",
    )
    if raw is None:
        raise HTTPException(status_code=404, detail=f"项目级 skill '{name}' 不存在")
    content = raw.decode("utf-8", errors="replace")
    meta = _parse_skill_frontmatter(content) or {}
    return {
        "scope": "project",
        "name": meta.get("name") or name,
        "description": meta.get("description", ""),
        "dir": name,
        "content": content,
    }


@router.post("/skills/{name}")
async def upsert_project_skill(
    name: str,
    body: SkillCreate,
    user: User = Depends(get_current_user),
):
    """Create or update a project-scope skill's SKILL.md."""
    await _require_container(user)
    try:
        _validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    meta = _parse_skill_frontmatter(body.content)
    if not meta or not meta.get("name") or not meta.get("description"):
        raise HTTPException(
            status_code=400,
            detail="SKILL.md 必须包含带 'name' 和 'description' 字段的 frontmatter",
        )
    if len(body.content) > 512 * 1024:
        raise HTTPException(status_code=400, detail="SKILL.md 超过 512KB 上限")

    ok = await asyncio.to_thread(
        container_manager.write_workspace_files,
        user.id, {f"{PROJECT_SKILLS_REL}/{name}/SKILL.md": body.content.encode("utf-8")},
    )
    if not ok:
        raise HTTPException(status_code=500, detail="写入工作空间失败")
    logger.info("Project skill '%s' upserted by %s", name, user.username)
    return {
        "status": "ok",
        "name": meta["name"],
        "description": meta["description"],
        "dir": name,
        "scope": "project",
    }


@router.delete("/skills/{name}")
async def delete_project_skill(name: str, user: User = Depends(get_current_user)):
    await _require_container(user)
    try:
        _validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    existing = await asyncio.to_thread(
        container_manager.read_workspace_file,
        user.id, f"{PROJECT_SKILLS_REL}/{name}/SKILL.md",
    )
    if existing is None:
        raise HTTPException(status_code=404, detail=f"项目级 skill '{name}' 不存在")
    if not await asyncio.to_thread(
        container_manager.delete_workspace_path, user.id, f"{PROJECT_SKILLS_REL}/{name}"
    ):
        raise HTTPException(status_code=500, detail="删除失败")
    logger.info("Project skill '%s' deleted by %s", name, user.username)
    return {"status": "ok"}
