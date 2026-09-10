"""Project space routes — project-scoped session management.

A "project" is a directory inside the user's workspace volume plus a display
name recorded on the platform. The platform stores ONLY the project roster
(the ``projects`` table); session membership is never persisted here because
opencode already stamps every session with its ``location.directory``. The
frontend groups sessions by matching that directory against the roster, so
there is no mapping table to keep in sync.

Two creation modes:
  - create: platform makes a fresh directory at /workspace/projects/{name}
            — the directory name IS the (sanitised) project name, so the
            label users see matches what they find in the file browser
  - bind:   user picks an existing workspace directory (from the file
            browser listing); the project name is derived from the
            directory's last segment; historical sessions in that directory
            become visible under the project automatically

Deletion removes the DB row AND the sessions whose directory matches the
project (via opencode's HTTP API, which requires the container running).
The directory itself is never touched — files stay visible in the workspace
browser.

Renaming is deliberately NOT supported: a session's location.directory is
immutable, so moving the directory would orphan its sessions, and a
display-name-only rename would break the name == directory-name invariant.
To "rename", delete the project (sessions go, files stay) and re-bind.

Endpoints:
  GET    /api/projects            — list the user's projects
  POST   /api/projects            — create (mode: "create" | "bind")
  DELETE /api/projects/{id}       — unbind + delete the project's sessions
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..models import Project, User
from ..services.agent_controller import agent_controller
from ..services.container_manager import container_manager
from ..services.tunnel_relay import tunnel_relay

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["projects"])

# New project directories live under <workspace>/projects/{name}.
PROJECTS_ROOT_REL = "projects"
MAX_NAME_LEN = 50
MAX_DIR_LEN = 400

# Characters that must never reach the filesystem even though they are legal
# in a display name (':' is also rejected by _clean_rel_dir for bind mode).
_FORBIDDEN_DIR_CHARS = set('/:*?"<>|\\')


class ProjectCreate(BaseModel):
    # Required for mode="create"; ignored for mode="bind" (the name is
    # derived from the directory's last segment).
    name: str | None = None
    mode: str = "create"           # "create" | "bind"
    # Workspace-relative path (e.g. "docs/site"); required for mode="bind".
    directory: str | None = None


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------

def _serialize(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "directory": p.directory,
        "origin": p.origin,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


def _clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"项目名称不能超过 {MAX_NAME_LEN} 个字符")
    if any(c in name for c in "/\\\r\n\t") or any(ord(c) < 32 for c in name):
        raise HTTPException(status_code=400, detail="项目名称包含非法字符")
    return name


def _dir_name_from(name: str) -> str:
    """Filesystem-safe directory name for a project (create mode).

    Non-ASCII (e.g. Chinese) is kept as-is — ext4 and the Docker archive API
    handle it fine. Only path-hostile characters are replaced, and leading /
    trailing dots and spaces are stripped (a bare "." or a hidden dir must
    never be produced).
    """
    cleaned = "".join("_" if c in _FORBIDDEN_DIR_CHARS else c for c in name)
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned:
        raise HTTPException(
            status_code=400, detail="项目名称无法转换为目录名，请使用包含文字或数字的名称"
        )
    return cleaned


def _clean_rel_dir(raw: str) -> str:
    """Validate a workspace-relative directory for bind mode.

    Rejects the workspace root itself (would blur the global/project split),
    hidden directories (.opencode etc.) and any traversal attempt.
    """
    rel = (raw or "").strip().replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]
    if not parts:
        raise HTTPException(status_code=400, detail="不能绑定 workspace 根目录，请选择一个子目录")
    if any(p in (".", "..") for p in parts) or ":" in rel:
        raise HTTPException(status_code=400, detail="非法目录路径")
    if any(p.startswith(".") for p in parts):
        raise HTTPException(status_code=400, detail="不能绑定隐藏目录")
    if len(rel) > MAX_DIR_LEN:
        raise HTTPException(status_code=400, detail="目录路径过长")
    return rel


async def _get_owned(db: AsyncSession, user: User, project_id: str) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def _check_conflicts(
    db: AsyncSession, user: User, name: str, directory: str
) -> None:
    dup_name = await db.execute(
        select(Project).where(
            Project.user_id == user.id,
            Project.name == name,
        )
    )
    if dup_name.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"项目名称「{name}」已存在")
    dup_dir = await db.execute(
        select(Project).where(
            Project.user_id == user.id,
            Project.directory == directory,
        )
    )
    if dup_dir.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="该目录已绑定为项目")


# ------------------------------------------------------------------
#  Routes
# ------------------------------------------------------------------

@router.get("")
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The user's project roster, newest first. Session counts / grouping
    are derived client-side from session.location.directory."""
    result = await db.execute(
        select(Project)
        .where(Project.user_id == user.id)
        .order_by(Project.created_at.desc())
    )
    return {"projects": [_serialize(p) for p in result.scalars().all()]}


@router.post("")
async def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a project: fresh directory named after the project (mode=create)
    or bind an existing workspace directory, taking its last path segment as
    the project name (mode=bind). Requires the container to have been created
    at least once; it may be stopped (Docker archive API)."""
    if body.mode not in ("create", "bind"):
        raise HTTPException(status_code=400, detail="mode 必须是 create 或 bind")

    # Docker SDK calls block; keep them off the event loop.
    if await asyncio.to_thread(container_manager.get_container, user.id) is None:
        raise HTTPException(
            status_code=409,
            detail="Agent 容器尚未创建，请先启动 Agent 再创建项目",
        )

    if body.mode == "create":
        # Directory name == project name (sanitised). Conflicts are caught by
        # _check_conflicts below: same user + same projects/ root means a
        # duplicate name and a duplicate directory are the same condition,
        # and distinct names collapsing to one dir_name (e.g. "a:b" vs "a_b")
        # hit the directory constraint.
        name = _clean_name(body.name or "")
        rel = f"{PROJECTS_ROOT_REL}/{_dir_name_from(name)}"
        directory = f"{settings.agent_workdir}/{rel}"
        await _check_conflicts(db, user, name, directory)
        # Materialise the directory: put_archive creates parent dirs, and the
        # placeholder makes the empty project visible in the file browser.
        ok = await asyncio.to_thread(
            container_manager.write_workspace_files, user.id, {f"{rel}/.keep": b""}
        )
        if not ok:
            raise HTTPException(status_code=500, detail="创建项目目录失败")
        origin = "created"
    else:
        rel = _clean_rel_dir(body.directory or "")
        # Name follows the directory so the invariant holds for binds too.
        name = _clean_name(rel.rsplit("/", 1)[-1])
        directory = f"{settings.agent_workdir}/{rel}"
        await _check_conflicts(db, user, name, directory)
        if not await asyncio.to_thread(
            container_manager.workspace_dir_exists, user.id, rel
        ):
            raise HTTPException(status_code=404, detail=f"目录不存在或不是文件夹: {rel}")
        origin = "bound"

    project = Project(user_id=user.id, name=name, directory=directory, origin=origin)
    db.add(project)
    try:
        await db.commit()
    except IntegrityError:
        # Race between the pre-check and the insert — the unique constraints
        # are the source of truth.
        await db.rollback()
        raise HTTPException(status_code=409, detail="项目名称或目录已被占用")
    await db.refresh(project)
    logger.info(
        "Project '%s' (%s) created by %s at %s",
        project.name, origin, user.username, project.directory,
    )
    return _serialize(project)


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unbind a project and delete its sessions.

    Strict order, no orphans: list the container's sessions, delete every one
    whose location.directory matches the project, and only then remove the DB
    row. Any failure aborts with the row intact so the call can be retried.
    The workspace directory itself is deliberately left untouched.

    Requires the container RUNNING — session deletion goes through
    opencode's HTTP API, which does not exist while the container is down.
    """
    project = await _get_owned(db, user, project_id)

    running, password = await agent_controller.get_agent_gate(user.id)
    if not running or password is None:
        raise HTTPException(
            status_code=409,
            detail="Agent 未在运行，请先启动 Agent 再删除项目（需要清理项目会话）",
        )

    result = await tunnel_relay.http_request(
        user_id=user.id, method="GET", path="/api/session",
        password=password, timeout=30,
    )
    if result.get("status") != 200 or not isinstance(result.get("body"), dict):
        raise HTTPException(
            status_code=502,
            detail=f"获取会话列表失败（opencode 返回 {result.get('status')}），项目未删除",
        )

    sessions = result["body"].get("data") or []
    targets = [
        s.get("id")
        for s in sessions
        if isinstance(s, dict)
        and (s.get("location") or {}).get("directory") == project.directory
        and s.get("id")
    ]

    # Legacy DELETE /session/{id} — same route the frontend uses; answers 200
    # with a bare `true`. 404 means the session is already gone: keep going.
    for sid in targets:
        r = await tunnel_relay.http_request(
            user_id=user.id, method="DELETE", path=f"/session/{sid}",
            password=password, timeout=30,
        )
        if r.get("status") not in (200, 204, 404):
            raise HTTPException(
                status_code=502,
                detail=f"删除会话 {sid} 失败（opencode 返回 {r.get('status')}），项目未删除，请重试",
            )

    await db.delete(project)
    await db.commit()
    logger.info(
        "Project '%s' deleted by %s — %d session(s) removed, directory kept: %s",
        project.name, user.username, len(targets), project.directory,
    )
    return {"deleted": True, "sessionsDeleted": len(targets)}
