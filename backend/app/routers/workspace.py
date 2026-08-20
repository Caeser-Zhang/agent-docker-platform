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
  POST   /api/workspace/skills/import     — upload a .zip, unpack into .opencode/skills/
"""
import io
import json
import logging
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..auth import get_current_user
from ..models import User
from ..services import host_config
from ..services.container_manager import container_manager
from ..services.host_config import _parse_skill_frontmatter, _validate_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspace", tags=["workspace"])

# opencode's project-scope locations, relative to the workspace root.
PROJECT_CONFIG_REL = "opencode.json"
PROJECT_SKILLS_REL = ".opencode/skills"

# Import limits for uploaded skill zips.
MAX_IMPORT_FILES = 500
MAX_IMPORT_TOTAL = 20 * 1024 * 1024
MAX_IMPORT_FILE = 5 * 1024 * 1024


class ProjectConfigSave(BaseModel):
    content: str  # raw JSON text of the project opencode.json


class SkillCreate(BaseModel):
    content: str  # raw SKILL.md text with frontmatter


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------

def _require_container(user: User) -> None:
    if container_manager.get_container(user.id) is None:
        raise HTTPException(
            status_code=409,
            detail="Agent 容器尚未创建，请先启动 Agent 再管理项目级配置",
        )


def _list_project_skills(user_id: str) -> list[dict]:
    """List project-scope skills from .opencode/skills/<name>/SKILL.md."""
    tree = container_manager.read_workspace_tree(user_id, PROJECT_SKILLS_REL)
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


def _list_all_skills(user_id: str) -> list[dict]:
    """Global (host) + project (workspace) skills, tagged by scope."""
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
    out.extend(_list_project_skills(user_id))
    return out


def _safe_upload_name(filename: str) -> str:
    """Sanitise an upload filename to a plain basename."""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="无效的文件名")
    return name


def _clean_zip_name(name: str) -> str | None:
    """Normalise a zip member name to a safe relative path, or None."""
    name = name.replace("\\", "/")
    if not name or name.startswith("/") or ":" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _extract_skills_from_zip(data: bytes) -> list[tuple[str, dict[str, bytes]]]:
    """Detect the skill layout inside a zip and normalise to [(name, files)].

    Supported layouts (mirrors how people actually package skills):
      1. bare:       SKILL.md (+ resources) at the zip root
      2. wrapped:    <skill-name>/SKILL.md (+ resources)
      3. multi:      <name-a>/SKILL.md, <name-b>/SKILL.md, ...
    """
    entries: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if not infos:
                raise ValueError("压缩包为空")
            if len(infos) > MAX_IMPORT_FILES:
                raise ValueError(f"压缩包文件数超过上限 {MAX_IMPORT_FILES}")
            for info in infos:
                clean = _clean_zip_name(info.filename)
                if clean is None:
                    raise ValueError(f"压缩包含不安全路径: {info.filename}")
                if info.file_size > MAX_IMPORT_FILE:
                    raise ValueError(f"单个文件超过 {MAX_IMPORT_FILE // 1024 // 1024}MB 上限: {info.filename}")
                content = zf.read(info)
                total += len(content)
                if total > MAX_IMPORT_TOTAL:
                    raise ValueError("压缩包解压总大小超过 20MB 上限")
                entries[clean] = content
    except zipfile.BadZipFile as exc:
        raise ValueError(f"无效的 zip 压缩包: {exc}") from exc

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
    _require_container(user)
    raw = container_manager.read_workspace_file(user.id, PROJECT_CONFIG_REL)
    created = False
    if raw is None:
        # Requirement: materialise the project config file when missing so the
        # user always has something editable in the workspace.
        skeleton = '{\n  "$schema": "https://opencode.ai/config.json"\n}\n'
        if not container_manager.write_workspace_files(
            user.id, {PROJECT_CONFIG_REL: skeleton.encode("utf-8")}
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
    _require_container(user)
    try:
        parsed = json.loads(body.content) if body.content.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("顶层必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"无效的 JSON: {exc}")
    if len(body.content) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="配置文件超过 1MB 上限")

    ok = container_manager.write_workspace_files(
        user.id, {PROJECT_CONFIG_REL: body.content.encode("utf-8")}
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
    """Global + project skills in one list, tagged with `scope` —
    feeds the input-box skill picker in the chat UI."""
    _require_container(user)
    return {"skills": _list_all_skills(user.id)}


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
    _require_container(user)
    name = _safe_upload_name(file.filename or "")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    if len(data) > MAX_UPLOAD_FILE:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_UPLOAD_FILE // 1024 // 1024}MB 上限"
        )

    rel = f"{UPLOADS_REL}/{name}"
    if not container_manager.write_workspace_files(user.id, {rel: data}):
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
    _require_container(user)
    entries = container_manager.list_workspace(user.id)
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
    _require_container(user)
    data = container_manager.read_workspace_file(user.id, path)
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
#  Project skills (workspace .opencode/skills/)
# ------------------------------------------------------------------

@router.get("/skills")
async def list_project_skills(user: User = Depends(get_current_user)):
    _require_container(user)
    return {"skills": _list_project_skills(user.id)}


@router.post("/skills/import")
async def import_skills_zip(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a skill .zip and unpack it into .opencode/skills/ (project scope).

    Must be defined BEFORE /skills/{name} so FastAPI matches the literal
    path "import" instead of capturing it as a {name} parameter.
    """
    _require_container(user)
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 格式的 skill 压缩包")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    try:
        skills = _extract_skills_from_zip(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Replace semantics: drop any previous copy of each imported skill first,
    # so stale files from an older version don't linger in the directory.
    for name, _files in skills:
        container_manager.delete_workspace_path(user.id, f"{PROJECT_SKILLS_REL}/{name}")

    written: dict[str, bytes] = {}
    for name, files in skills:
        for rel, content in files.items():
            written[f"{PROJECT_SKILLS_REL}/{name}/{rel}"] = content
    if not container_manager.write_workspace_files(user.id, written):
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
    _require_container(user)
    try:
        _validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raw = container_manager.read_workspace_file(
        user.id, f"{PROJECT_SKILLS_REL}/{name}/SKILL.md"
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
    _require_container(user)
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

    ok = container_manager.write_workspace_files(
        user.id, {f"{PROJECT_SKILLS_REL}/{name}/SKILL.md": body.content.encode("utf-8")}
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
    _require_container(user)
    try:
        _validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    existing = container_manager.read_workspace_file(
        user.id, f"{PROJECT_SKILLS_REL}/{name}/SKILL.md"
    )
    if existing is None:
        raise HTTPException(status_code=404, detail=f"项目级 skill '{name}' 不存在")
    if not container_manager.delete_workspace_path(user.id, f"{PROJECT_SKILLS_REL}/{name}"):
        raise HTTPException(status_code=500, detail="删除失败")
    logger.info("Project skill '%s' deleted by %s", name, user.username)
    return {"status": "ok"}
