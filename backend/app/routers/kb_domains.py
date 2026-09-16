"""Knowledge-domain management — credentials, rosters and the user-side catalog.

A DOMAIN is one API key covering one-or-more fastk databases (the platform
owns the domain↔database mapping; the upstream server offers no key scoping —
see docs/KB_DOMAIN_DESIGN.md). Two routers live here because the two audiences
differ:

  ``router``       /api/admin/kb-domains — admin only: CRUD domains, attach or
                   detach databases, edit the private-domain roster (single
                   grant, pasted 工号 list, csv/xlsx import with a
                   preview→commit handshake) and flip key_type. Keys are
                   write-only: no endpoint ever returns one (only presence and
                   timestamps), so a stolen admin session cannot exfiltrate them.
  ``user_router``  /api/kb/my-domains — any logged-in user: the domains they
                   may access, each with its databases and their fastk
                   descriptions, for the aggregated domain-card display.

Type switches and roster edits take effect immediately: both the agent proxy
and the citation-badge endpoint re-read the tables per request, so no
container recreate is needed. Every write is audited under ``kbdomain.*``.
"""
from __future__ import annotations

import io
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user, require_admin
from ..config import settings
from ..crypto import encrypt_secret
from ..database import get_db
from ..models import KbDomain, KbDomainDb, KbDomainGrant, User
from ..services import kb_access
from ..services.audit import log_audit

logger = logging.getLogger(__name__)

# Catalog fetch timeout — the listing endpoint is cheap, so keep it short and
# fail soft (an unreachable fastk server must not break the whole panel).
_CATALOG_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

router = APIRouter(prefix="/api/admin", tags=["kb-admin"], dependencies=[Depends(require_admin)])
user_router = APIRouter(prefix="/api/kb", tags=["kb"])

# Physical database names come from the fastk registry; keep the same closed
# alphabet as routers/fastk.py so a name can never smuggle path segments.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Domain display names are human-facing: 1-50 chars, no control characters.
_DOMAIN_NAME_MAX = 50
_DESCRIPTION_MAX = 500
_KEY_TYPES = ("public", "private")

# Import limits — generous for a roster file, small enough to stay trivial.
_IMPORT_MAX_ROWS = 5000
_IMPORT_MAX_BYTES = 5 * 1024 * 1024
# Identifier split for pasted text: newlines, commas, semicolons, tabs, spaces.
_SPLIT_RE = re.compile(r"[\s,;，、]+")
# Header keywords that mark the 工号 column in csv/xlsx files (lowercased).
_HEADER_KEYWORDS = ("工号", "员工号", "员工编号", "uid", "emp", "no")

_PREVIEW_TTL_SECONDS = 600
# token -> {domain_id, user_ids, matched, unmatched, source, created_at}.
# In-memory on purpose: a preview is a transient UI handshake, not state worth
# persisting, and one backend replica serves the whole platform.
_previews: dict[str, dict] = {}


def _check_kb_name(kb_name: str) -> None:
    if not _NAME_RE.fullmatch(kb_name):
        raise HTTPException(status_code=400, detail="无效的知识库名")


def _check_domain_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > _DOMAIN_NAME_MAX:
        raise HTTPException(
            status_code=400, detail=f"领域名称必填，且不超过 {_DOMAIN_NAME_MAX} 字"
        )
    if any(ch.isspace() is False and ord(ch) < 32 for ch in name):
        raise HTTPException(status_code=400, detail="领域名称含非法控制字符")
    return name


async def _get_domain(db: AsyncSession, domain_id: str) -> KbDomain:
    domain = (
        await db.execute(select(KbDomain).where(KbDomain.id == domain_id))
    ).scalar_one_or_none()
    if domain is None:
        raise HTTPException(status_code=404, detail="知识领域不存在")
    return domain


async def _resolve_user(db: AsyncSession, username: str | None, uid: str | None) -> User:
    if not username and not uid:
        raise HTTPException(status_code=400, detail="需要提供 username 或 uid")
    stmt = select(User)
    if username:
        stmt = stmt.where(User.username == username)
    else:
        stmt = stmt.where(User.uid == uid)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


# ------------------------------------------------------------------- payloads


class DomainCreateBody(BaseModel):
    name: str
    description: str = ""
    key_type: str = "private"
    api_key: str | None = None


class DomainUpdateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    # Flipping key_type additionally requires confirm_name == current domain
    # name, so a stray toggle cannot silently open or close a domain.
    key_type: str | None = None
    confirm_name: str | None = None
    api_key: str | None = None


class DomainDbBody(BaseModel):
    kb_name: str


class DomainMemberBody(BaseModel):
    # Either identifier resolves to the same user; uid (工号) is what admins
    # usually have on hand.
    username: str | None = None
    uid: str | None = None


class ImportCommitBody(BaseModel):
    preview_token: str = Field(..., min_length=1)


# ------------------------------------------------------------------ domains


@router.get("/kb-domains")
async def list_domains(db: AsyncSession = Depends(get_db)):
    """All domains with counts — names and presence flags only, never the key."""
    domains = (await db.execute(select(KbDomain).order_by(KbDomain.created_at))).scalars().all()
    db_counts = dict(
        (await db.execute(
            select(KbDomainDb.domain_id, func.count()).group_by(KbDomainDb.domain_id)
        )).all()
    )
    member_counts = dict(
        (await db.execute(
            select(KbDomainGrant.domain_id, func.count())
            .where(KbDomainGrant.revoked_at.is_(None))
            .group_by(KbDomainGrant.domain_id)
        )).all()
    )
    return {
        "items": [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "key_type": d.key_type,
                "has_api_key": bool(d.api_key_enc),
                "db_count": db_counts.get(d.id, 0),
                "member_count": member_counts.get(d.id, 0),
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            }
            for d in domains
        ]
    }


@router.post("/kb-domains")
async def create_domain(
    body: DomainCreateBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Create an empty domain; databases and roster are attached afterwards."""
    name = _check_domain_name(body.name)
    if body.key_type not in _KEY_TYPES:
        raise HTTPException(status_code=400, detail="key_type 必须是 public 或 private")
    if len(body.description or "") > _DESCRIPTION_MAX:
        raise HTTPException(status_code=400, detail=f"领域描述不超过 {_DESCRIPTION_MAX} 字")
    clash = (await db.execute(select(KbDomain).where(KbDomain.name == name))).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status_code=400, detail=f"领域名称 '{name}' 已存在")

    domain = KbDomain(
        name=name,
        description=(body.description or "").strip(),
        key_type=body.key_type,
        api_key_enc=encrypt_secret(body.api_key) if body.api_key else "",
    )
    db.add(domain)
    await db.commit()
    await log_audit(admin.id, "kbdomain.create", {"domain_id": domain.id, "name": domain.name})
    return {"id": domain.id, "name": domain.name, "key_type": domain.key_type}


@router.get("/kb-domains/{domain_id}")
async def get_domain(domain_id: str, db: AsyncSession = Depends(get_db)):
    """One domain in full: metadata, attached databases, active roster."""
    domain = await _get_domain(db, domain_id)
    dbs = (
        await db.execute(
            select(KbDomainDb).where(KbDomainDb.domain_id == domain_id).order_by(KbDomainDb.kb_name)
        )
    ).scalars().all()
    member_rows = (
        await db.execute(
            select(KbDomainGrant, User.username, User.uid)
            .join(User, User.id == KbDomainGrant.user_id)
            .where(KbDomainGrant.domain_id == domain_id, KbDomainGrant.revoked_at.is_(None))
            .order_by(User.username)
        )
    ).all()
    total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    return {
        "id": domain.id,
        "name": domain.name,
        "description": domain.description,
        "key_type": domain.key_type,
        "has_api_key": bool(domain.api_key_enc),
        "created_at": domain.created_at.isoformat() if domain.created_at else None,
        "updated_at": domain.updated_at.isoformat() if domain.updated_at else None,
        "databases": [{"kb_name": r.kb_name} for r in dbs],
        "members": [
            {
                "user_id": g.user_id,
                "username": username,
                "uid": uid,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g, username, uid in member_rows
        ],
        # Impact figures for the type-switch confirmation dialog.
        "total_users": total_users,
        "active_member_count": len(member_rows),
    }


@router.put("/kb-domains/{domain_id}")
async def update_domain(
    domain_id: str,
    body: DomainUpdateBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Rename, re-describe, rotate the key, or flip key_type (with confirm)."""
    domain = await _get_domain(db, domain_id)
    touched = False

    if body.name is not None:
        name = _check_domain_name(body.name)
        if name != domain.name:
            clash = (
                await db.execute(select(KbDomain).where(KbDomain.name == name))
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=400, detail=f"领域名称 '{name}' 已存在")
            domain.name = name
            touched = True

    if body.description is not None:
        if len(body.description) > _DESCRIPTION_MAX:
            raise HTTPException(status_code=400, detail=f"领域描述不超过 {_DESCRIPTION_MAX} 字")
        domain.description = body.description.strip()
        touched = True

    if body.api_key is not None:
        if not body.api_key:
            raise HTTPException(status_code=400, detail="api_key 不能为空")
        domain.api_key_enc = encrypt_secret(body.api_key)
        touched = True

    switched_to = None
    if body.key_type is not None and body.key_type != domain.key_type:
        if body.key_type not in _KEY_TYPES:
            raise HTTPException(status_code=400, detail="key_type 必须是 public 或 private")
        if (body.confirm_name or "") != domain.name:
            raise HTTPException(
                status_code=400,
                detail="切换 Key 类型需要 confirm_name 与当前领域名称完全一致",
            )
        switched_to = body.key_type
        domain.key_type = switched_to
        touched = True

    if touched:
        await db.commit()

    if switched_to is None:
        if touched:
            await log_audit(
                admin.id, "kbdomain.update", {"domain_id": domain.id, "name": domain.name}
            )
    else:
        # public→private strands everyone NOT on the roster; private→public
        # opens the domain to everyone. Either way the roster rows survive,
        # so flipping back restores the previous private membership.
        total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        active = (
            await db.execute(
                select(func.count())
                .select_from(KbDomainGrant)
                .where(KbDomainGrant.domain_id == domain_id, KbDomainGrant.revoked_at.is_(None))
            )
        ).scalar_one()
        affected = (
            max(total_users - active, 0) if switched_to == "private" else total_users
        )
        await log_audit(
            admin.id,
            "kbdomain.type_switch",
            {
                "domain_id": domain.id,
                "name": domain.name,
                "from": "public" if switched_to == "private" else "private",
                "to": switched_to,
                "affected_users": affected,
            },
        )

    return {"id": domain.id, "name": domain.name, "key_type": domain.key_type}


@router.delete("/kb-domains/{domain_id}")
async def delete_domain(
    domain_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Drop a domain and, with it, every database link and roster row.

    Grants and links are deleted explicitly rather than relying on ON DELETE
    CASCADE: SQLite only honours it with foreign_keys=ON, which this project
    does not set, and a domain must never survive as dangling references.
    """
    domain = await _get_domain(db, domain_id)
    members = (
        await db.execute(
            select(KbDomainGrant.user_id).where(
                KbDomainGrant.domain_id == domain_id, KbDomainGrant.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    await db.execute(delete(KbDomainGrant).where(KbDomainGrant.domain_id == domain_id))
    await db.execute(delete(KbDomainDb).where(KbDomainDb.domain_id == domain_id))
    await db.delete(domain)
    await db.commit()
    await log_audit(
        admin.id,
        "kbdomain.delete",
        {"domain_id": domain_id, "name": domain.name, "affected_members": len(members)},
    )
    return {"id": domain_id, "affected_members": len(members)}


# ------------------------------------------------------------ domain databases


@router.get("/kb-domains/{domain_id}/dbs")
async def list_domain_dbs(domain_id: str, db: AsyncSession = Depends(get_db)):
    await _get_domain(db, domain_id)
    rows = (
        await db.execute(
            select(KbDomainDb).where(KbDomainDb.domain_id == domain_id).order_by(KbDomainDb.kb_name)
        )
    ).scalars().all()
    return {"items": [{"kb_name": r.kb_name} for r in rows]}


@router.post("/kb-domains/{domain_id}/dbs")
async def add_domain_db(
    domain_id: str,
    body: DomainDbBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Attach a physical database to this domain.

    A database belongs to exactly ONE domain (kb_domain_dbs.kb_name UNIQUE):
    that is what makes resolve_access's reverse lookup unambiguous. Attaching
    an already-claimed database is a 409 naming the owning domain, so the
    admin knows where to detach it first. Re-attaching to the same domain is
    an idempotent no-op.
    """
    _check_kb_name(body.kb_name)
    domain = await _get_domain(db, domain_id)
    existing = (
        await db.execute(select(KbDomainDb).where(KbDomainDb.kb_name == body.kb_name))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.domain_id == domain_id:
            return {"domain_id": domain_id, "kb_name": body.kb_name, "added": False}
        owner = (
            await db.execute(select(KbDomain).where(KbDomain.id == existing.domain_id))
        ).scalar_one_or_none()
        owner_name = owner.name if owner else existing.domain_id
        raise HTTPException(
            status_code=409, detail=f"知识库 '{body.kb_name}' 已属于领域 '{owner_name}'"
        )
    db.add(KbDomainDb(domain_id=domain_id, kb_name=body.kb_name))
    await db.commit()
    await log_audit(
        admin.id,
        "kbdomain.db_add",
        {"domain_id": domain_id, "name": domain.name, "kb_name": body.kb_name},
    )
    return {"domain_id": domain_id, "kb_name": body.kb_name, "added": True}


@router.delete("/kb-domains/{domain_id}/dbs/{kb_name}")
async def remove_domain_db(
    domain_id: str,
    kb_name: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Detach a database — access through this domain stops on the next request."""
    _check_kb_name(kb_name)
    domain = await _get_domain(db, domain_id)
    row = (
        await db.execute(
            select(KbDomainDb).where(
                KbDomainDb.domain_id == domain_id, KbDomainDb.kb_name == kb_name
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="该领域未关联此知识库")
    await db.delete(row)
    await db.commit()
    await log_audit(
        admin.id,
        "kbdomain.db_remove",
        {"domain_id": domain_id, "name": domain.name, "kb_name": kb_name},
    )
    return {"domain_id": domain_id, "kb_name": kb_name}


# ------------------------------------------------------------------- members


async def _require_private(db: AsyncSession, domain_id: str) -> KbDomain:
    domain = await _get_domain(db, domain_id)
    if domain.key_type == "public":
        raise HTTPException(
            status_code=400, detail="公共领域默认全员可访问，无需维护成员名单；如需名单请先切换为私有领域"
        )
    return domain


@router.get("/kb-domains/{domain_id}/members")
async def list_domain_members(domain_id: str, db: AsyncSession = Depends(get_db)):
    """The active private-domain roster (empty by definition for public)."""
    await _get_domain(db, domain_id)
    rows = (
        await db.execute(
            select(KbDomainGrant, User.username, User.uid)
            .join(User, User.id == KbDomainGrant.user_id)
            .where(KbDomainGrant.domain_id == domain_id, KbDomainGrant.revoked_at.is_(None))
            .order_by(User.username)
        )
    ).all()
    return {
        "items": [
            {
                "user_id": g.user_id,
                "username": username,
                "uid": uid,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g, username, uid in rows
        ]
    }


async def _grant_member(db: AsyncSession, domain_id: str, user_id: str) -> bool:
    """Insert-or-revive one roster row. Returns True when a row was written."""
    existing = (
        await db.execute(
            select(KbDomainGrant).where(
                KbDomainGrant.user_id == user_id, KbDomainGrant.domain_id == domain_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(KbDomainGrant(user_id=user_id, domain_id=domain_id))
        return True
    if existing.revoked_at is not None:
        # Revive a soft-deleted row: the composite PK forbids a second INSERT,
        # so re-granting clears the stamp. created_at is reset — fresh grant.
        existing.revoked_at = None
        existing.created_at = datetime.now(timezone.utc)
        return True
    return False  # already active — idempotent skip


@router.post("/kb-domains/{domain_id}/members")
async def grant_domain_member(
    domain_id: str,
    body: DomainMemberBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Whitelist one user for one private domain — effective on their next request."""
    domain = await _require_private(db, domain_id)
    user = await _resolve_user(db, body.username, body.uid)
    await _grant_member(db, domain_id, user.id)
    await db.commit()
    await log_audit(
        admin.id,
        "kbdomain.member_grant",
        {"domain_id": domain_id, "name": domain.name, "user_id": user.id, "username": user.username},
    )
    return {"domain_id": domain_id, "user_id": user.id, "username": user.username}


@router.delete("/kb-domains/{domain_id}/members/{user_id}")
async def revoke_domain_member(
    domain_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Revoke access — also kills citation badges already rendered in old chats.

    Soft delete: the row is stamped with ``revoked_at`` rather than dropped, so
    the (user, domain) history survives and a later re-grant revives it.
    """
    domain = await _get_domain(db, domain_id)
    row = (
        await db.execute(
            select(KbDomainGrant).where(
                KbDomainGrant.user_id == user_id,
                KbDomainGrant.domain_id == domain_id,
                KbDomainGrant.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="该授权不存在")
    row.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    await log_audit(
        admin.id,
        "kbdomain.member_revoke",
        {"domain_id": domain_id, "name": domain.name, "user_id": user_id},
    )
    return {"domain_id": domain_id, "user_id": user_id}


# -------------------------------------------------------------------- import
#
# Three entry points, one core: pasted text, .csv and .xlsx all reduce to a
# list of identifiers (工号 first, username as fallback) which is resolved
# against the users table. Nothing is written until the admin confirms the
# preview — the preview→commit handshake keeps a mis-parsed column from
# silently granting the wrong roster.


def _extract_identifiers_from_text(text_: str) -> list[str]:
    return [t for t in (p.strip() for p in _SPLIT_RE.split(text_)) if t]


def _extract_table_values(rows: list[list], column: int | None) -> tuple[list[str], dict]:
    """Pull identifiers out of a parsed csv/xlsx sheet.

    ``column=None`` triggers header detection: if the first row contains a
    cell whose text hits one of ``_HEADER_KEYWORDS``, that column is used and
    the header row is skipped; otherwise column 0 is read from row 0. An
    explicit ``column`` (the admin re-picked a column in the preview UI)
    overrides detection: that column is read from row 0, except when the
    auto-detected header sits on the very same column — then the header row
    is still skipped so its label never leaks into the roster.
    """
    if not rows:
        return [], {"header_detected": False, "column_index": column or 0, "columns": []}

    def _cell(row: list, idx: int) -> str:
        v = row[idx] if idx < len(row) else None
        return str(v).strip() if v is not None else ""

    detected_idx = None
    for i, c in enumerate(rows[0]):
        s = str(c).strip().lower() if c is not None else ""
        if s and any(k in s for k in _HEADER_KEYWORDS):
            detected_idx = i
            break

    if column is None:
        if detected_idx is not None:
            idx, data_rows, header_detected = detected_idx, rows[1:], True
        else:
            idx, data_rows, header_detected = 0, rows, False
    else:
        idx = column
        if detected_idx == column:
            data_rows, header_detected = rows[1:], True
        else:
            data_rows, header_detected = rows, False

    values: list[str] = []
    for r in data_rows:
        v = _cell(r, idx)
        if v:
            values.append(v)
    columns = [
        {"index": i, "label": _cell(rows[0], i) or f"第{i + 1}列"}
        for i in range(max((len(r) for r in rows[:5]), default=0))
    ]
    return values, {
        "header_detected": header_detected,
        "column_index": idx,
        "columns": columns,
    }


def _parse_csv(data: bytes, column: int | None) -> tuple[list[str], dict]:
    import csv as _csv

    try:
        text_ = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text_ = data.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="CSV 编码无法识别（支持 UTF-8 / GBK）")
    rows = [row for row in _csv.reader(io.StringIO(text_))]
    return _extract_table_values(rows, column)


def _parse_xlsx(data: bytes, column: int | None) -> tuple[list[str], dict]:
    try:
        import openpyxl
    except ImportError:  # pragma: no cover — dependency is pinned in requirements
        raise HTTPException(status_code=500, detail="服务端缺少 openpyxl，无法解析 xlsx")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="xlsx 文件无法解析")
    try:
        ws = wb.worksheets[0]
        rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return _extract_table_values(rows, column)


async def _resolve_identifiers(
    db: AsyncSession, identifiers: list[str]
) -> tuple[list[User], list[str]]:
    """Match identifiers against users: uid (工号) first, username as fallback.

    Order-preserving de-duplication happens before the query. Identifiers that
    match nobody are returned verbatim for the preview's unmatched list —
    importing NEVER auto-creates users.
    """
    seen: set[str] = set()
    unique = [t for t in identifiers if not (t in seen or seen.add(t))]
    users: list[User] = []
    matched_ids: set[str] = set()
    unmatched: list[str] = []
    for ident in unique:
        user = (await db.execute(select(User).where(User.uid == ident))).scalar_one_or_none()
        if user is None:
            user = (
                await db.execute(select(User).where(User.username == ident))
            ).scalar_one_or_none()
        if user is None or user.id in matched_ids:
            if user is None:
                unmatched.append(ident)
            continue
        matched_ids.add(user.id)
        users.append(user)
    return users, unmatched


def _prune_previews() -> None:
    cutoff = time.time() - _PREVIEW_TTL_SECONDS
    for token in [t for t, e in _previews.items() if e["created_at"] < cutoff]:
        _previews.pop(token, None)


@router.post("/kb-domains/{domain_id}/import/preview")
async def import_preview(
    domain_id: str,
    text: str | None = Form(default=None),
    column: int | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Parse a roster source WITHOUT writing anything; return a preview + token.

    Accepts either pasted ``text`` (split on newlines/commas/semicolons/
    spaces/tabs) or an uploaded ``file`` (.csv / .xlsx). For files the 工号
    column is auto-detected via header keywords; the admin can re-pick a
    column by resubmitting with ``column``. The commit step requires the
    returned ``preview_token`` — identifiers never become grants unreviewed.
    """
    domain = await _require_private(db, domain_id)

    source = "text"
    file_meta: dict = {}
    if file is not None and (file.filename or ""):
        data = await file.read()
        if len(data) > _IMPORT_MAX_BYTES:
            raise HTTPException(status_code=400, detail="文件超过 5MB 上限")
        name_lower = (file.filename or "").lower()
        if name_lower.endswith(".xlsx"):
            source = "xlsx"
            identifiers, file_meta = _parse_xlsx(data, column)
        elif name_lower.endswith(".csv"):
            source = "csv"
            identifiers, file_meta = _parse_csv(data, column)
        else:
            raise HTTPException(status_code=400, detail="仅支持 .csv 或 .xlsx 文件")
        file_meta["filename"] = file.filename
    elif text is not None and text.strip():
        identifiers = _extract_identifiers_from_text(text)
    else:
        raise HTTPException(status_code=400, detail="需要提供 text 或 file")

    if not identifiers:
        raise HTTPException(status_code=400, detail="未解析到任何标识符")
    if len(identifiers) > _IMPORT_MAX_ROWS:
        raise HTTPException(status_code=400, detail=f"标识符数量超过 {_IMPORT_MAX_ROWS} 上限")

    users, unmatched = await _resolve_identifiers(db, identifiers)
    if not users:
        already_ids: set[str] = set()
    else:
        rows = await db.execute(
            select(KbDomainGrant.user_id).where(
                KbDomainGrant.domain_id == domain_id,
                KbDomainGrant.user_id.in_([u.id for u in users]),
                KbDomainGrant.revoked_at.is_(None),
            )
        )
        already_ids = set(rows.scalars().all())

    already = [u for u in users if u.id in already_ids]
    to_grant = [u for u in users if u.id not in already_ids]

    _prune_previews()
    token = secrets.token_urlsafe(24)
    _previews[token] = {
        "domain_id": domain_id,
        "user_ids": [u.id for u in to_grant],
        "source": source,
        "created_at": time.time(),
    }

    def _u(user: User) -> dict:
        return {"user_id": user.id, "username": user.username, "uid": user.uid}

    return {
        "preview_token": token,
        "source": source,
        "source_meta": file_meta,
        "matched": [_u(u) for u in to_grant],
        "already": [_u(u) for u in already],
        "unmatched": unmatched,
        "domain_id": domain_id,
        "domain_name": domain.name,
    }


@router.post("/kb-domains/{domain_id}/import/commit")
async def import_commit(
    domain_id: str,
    body: ImportCommitBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Write the previewed roster (incremental append; existing members skipped).

    The token is single-use and bound to this exact domain — a replay or a
    cross-domain substitution is rejected. Expired tokens (>10 min) are gone.
    """
    domain = await _require_private(db, domain_id)
    entry = _previews.pop(body.preview_token, None)
    if entry is None:
        raise HTTPException(status_code=400, detail="预览已过期或无效，请重新导入")
    if entry["domain_id"] != domain_id:
        raise HTTPException(status_code=400, detail="预览令牌与目标领域不一致")
    if time.time() - entry["created_at"] > _PREVIEW_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="预览已过期，请重新导入")

    granted = 0
    skipped = 0
    for user_id in entry["user_ids"]:
        if await _grant_member(db, domain_id, user_id):
            granted += 1
        else:
            skipped += 1  # became active between preview and commit
    await db.commit()
    await log_audit(
        admin.id,
        "kbdomain.member_import",
        {
            "domain_id": domain_id,
            "name": domain.name,
            "source": entry["source"],
            "granted": granted,
            "skipped": skipped,
        },
    )
    return {"domain_id": domain_id, "granted": granted, "skipped": skipped}


# ------------------------------------------------------- permission matrix UI


@router.get("/kb-users")
async def list_kb_users(db: AsyncSession = Depends(get_db)):
    """Every user, for the member picker — identifiers only, no secrets."""
    rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
    return {
        "items": [
            {"user_id": u.id, "username": u.username, "uid": u.uid, "role": u.role}
            for u in rows
        ]
    }


# ------------------------------------------------------------------- user side


@user_router.get("/my-domains")
async def my_domains(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's accessible domains, aggregated for the domain-card display.

    One entry per domain the user may access — every PUBLIC domain that has at
    least one database, plus every PRIVATE domain with an active grant AND at
    least one database (empty domains are hidden from users; the admin UI
    still shows them). Database descriptions live only in the fastk server's
    catalog, so the listing is fetched (with ``AGENT_KB_CATALOG_KEY``, which
    buys descriptions, never access) and filtered down to the databases the
    domain model says the user may see.

    Fails soft: an unreachable fastk server yields empty descriptions rather
    than an error, so the panel still shows *what* the user can access.
    """
    allowed = set(await kb_access.granted_kbs(db, user.id))
    items: list[dict] = []
    if allowed:
        rows = (
            await db.execute(
                select(KbDomain, KbDomainDb.kb_name)
                .join(KbDomainDb, KbDomainDb.domain_id == KbDomain.id)
                .order_by(KbDomain.created_at, KbDomainDb.kb_name)
            )
        ).all()
        by_domain: dict[str, tuple[KbDomain, list[str]]] = {}
        for domain, kb_name in rows:
            if kb_name not in allowed:
                continue
            entry = by_domain.setdefault(domain.id, (domain, []))
            entry[1].append(kb_name)

        descriptions: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT) as client:
                resp = await client.get(
                    f"{settings.fastk_server_url.rstrip('/')}/fastk/api/databases/",
                    headers=kb_access.catalog_headers(),
                )
            if resp.status_code == 200:
                for entry_ in resp.json():
                    if isinstance(entry_, dict) and entry_.get("name") in allowed:
                        descriptions[entry_["name"]] = entry_.get("description") or ""
            else:
                # A 401/403 here means AGENT_KB_CATALOG_KEY is missing or stale.
                logger.warning(
                    "my-domains: fastk listing returned %d for user=%s (descriptions omitted)",
                    resp.status_code, user.id,
                )
        except httpx.HTTPError as exc:
            logger.warning("my-domains: fastk listing failed for user=%s: %s", user.id, exc)

        for domain, names in by_domain.values():
            items.append(
                {
                    "id": domain.id,
                    "name": domain.name,
                    "description": domain.description,
                    "key_type": domain.key_type,
                    "databases": [
                        {"name": n, "description": descriptions.get(n, "")} for n in names
                    ],
                }
            )
    return {"domains": items}
