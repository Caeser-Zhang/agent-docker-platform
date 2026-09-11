"""Knowledge-base credential & whitelist management.

Two routers live here because the two audiences differ:

  ``router``       /api/admin/kb-*  — admin only: record/rotate the per-database
                   API credentials and edit the user↔database whitelist. Keys are
                   write-only: no endpoint ever returns one (only presence and
                   timestamps), so a stolen admin session cannot exfiltrate them.
  ``user_router``  /api/kb/my-databases — any logged-in user: the database names
                   they may read. The frontend shows this up front so users know
                   what the agent can search without hitting a 403 first.

Grant/revoke take effect immediately: both the agent proxy and the citation
badge endpoint re-read kb_grants per request, so no container recreate is
needed. Every write is audited.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user, require_admin
from ..config import settings
from ..crypto import encrypt_secret
from ..database import get_db
from ..models import KbGrant, KbKey, User
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


def _check_name(kb_name: str) -> None:
    if not _NAME_RE.fullmatch(kb_name):
        raise HTTPException(status_code=400, detail="无效的知识库名")


class KbKeyBody(BaseModel):
    api_key: str = Field(..., min_length=1)


class KbGrantBody(BaseModel):
    kb_name: str
    # Either identifier resolves to the same user; uid (工号) is what admins
    # usually have on hand.
    username: str | None = None
    uid: str | None = None


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


# ------------------------------------------------------------------ credentials


@router.get("/kb-keys")
async def list_kb_keys(db: AsyncSession = Depends(get_db)):
    """All recorded credentials — names and timestamps only, never the key."""
    rows = (await db.execute(select(KbKey).order_by(KbKey.kb_name))).scalars().all()
    return {
        "items": [
            {
                "kb_name": r.kb_name,
                "has_api_key": bool(r.api_key_enc),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    }


@router.put("/kb-keys/{kb_name}")
async def put_kb_key(
    kb_name: str,
    body: KbKeyBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Record or rotate one database's credential (single-point key rotation)."""
    _check_name(kb_name)
    row = (await db.execute(select(KbKey).where(KbKey.kb_name == kb_name))).scalar_one_or_none()
    action = "kbkey.rotate" if row else "kbkey.create"
    if row is None:
        row = KbKey(kb_name=kb_name)
        db.add(row)
    row.api_key_enc = encrypt_secret(body.api_key)
    await db.commit()
    await log_audit(admin.id, action, {"kb_name": kb_name})
    return {"kb_name": kb_name, "action": action}


@router.delete("/kb-keys/{kb_name}")
async def delete_kb_key(
    kb_name: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Drop a credential and, with it, every grant that depended on it.

    The grants are deleted explicitly rather than relying on ON DELETE CASCADE:
    SQLite only honours it with foreign_keys=ON, which this project does not
    set, and a credential must never survive as a grant pointing at nothing.
    """
    _check_name(kb_name)
    row = (await db.execute(select(KbKey).where(KbKey.kb_name == kb_name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="该知识库凭据不存在")
    revoked = (
        await db.execute(
            select(KbGrant.user_id).where(
                KbGrant.kb_name == kb_name, KbGrant.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    # Physically drop every grant row (active or soft-deleted): the credential
    # they pointed at is gone, so keeping revoked history would be meaningless.
    await db.execute(delete(KbGrant).where(KbGrant.kb_name == kb_name))
    await db.delete(row)
    await db.commit()
    await log_audit(
        admin.id, "kbkey.delete", {"kb_name": kb_name, "revoked_users": sorted(revoked)}
    )
    return {"kb_name": kb_name, "revoked_users": sorted(revoked)}


# ---------------------------------------------------------------------- grants


@router.get("/kb-grants")
async def list_kb_grants(
    kb_name: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """The whitelist matrix, optionally narrowed to one database or one user."""
    stmt = (
        select(KbGrant, User.username, User.uid)
        .join(User, User.id == KbGrant.user_id)
        .where(KbGrant.revoked_at.is_(None))
    )
    if kb_name:
        stmt = stmt.where(KbGrant.kb_name == kb_name)
    if user_id:
        stmt = stmt.where(KbGrant.user_id == user_id)
    rows = (await db.execute(stmt.order_by(KbGrant.kb_name, User.username))).all()
    return {
        "items": [
            {
                "kb_name": g.kb_name,
                "user_id": g.user_id,
                "username": username,
                "uid": uid,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g, username, uid in rows
        ]
    }


@router.post("/kb-grants")
async def grant_kb(
    body: KbGrantBody,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Whitelist one user for one database — effective on their next request."""
    _check_name(body.kb_name)
    key = (await db.execute(select(KbKey).where(KbKey.kb_name == body.kb_name))).scalar_one_or_none()
    if key is None or not key.api_key_enc:
        raise HTTPException(
            status_code=400,
            detail=f"知识库 '{body.kb_name}' 尚未录入凭据，请先 PUT /api/admin/kb-keys/{body.kb_name}",
        )
    user = await _resolve_user(db, body.username, body.uid)
    existing = (
        await db.execute(
            select(KbGrant).where(KbGrant.user_id == user.id, KbGrant.kb_name == body.kb_name)
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(KbGrant(user_id=user.id, kb_name=body.kb_name))
        await db.commit()
    elif existing.revoked_at is not None:
        # Revive a soft-deleted row: the composite PK (user_id, kb_name) forbids
        # a second INSERT, so re-granting clears the stamp instead. created_at is
        # reset to now — this is a fresh grant, not the original one.
        existing.revoked_at = None
        existing.created_at = datetime.now(timezone.utc)
        await db.commit()
    await log_audit(
        admin.id,
        "kbgrant.grant",
        {"kb_name": body.kb_name, "user_id": user.id, "username": user.username},
    )
    return {"kb_name": body.kb_name, "user_id": user.id, "username": user.username}


@router.delete("/kb-grants/{user_id}/{kb_name}")
async def revoke_kb(
    user_id: str,
    kb_name: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Revoke access — also kills citation badges already rendered in old chats.

    Soft delete: the row is stamped with ``revoked_at`` rather than dropped, so
    the (user, database) history survives and a later re-grant revives it. All
    enforcement queries filter on ``revoked_at IS NULL``, so the effect is
    immediate and identical to a hard delete from the user's point of view.
    """
    _check_name(kb_name)
    row = (
        await db.execute(
            select(KbGrant).where(
                KbGrant.user_id == user_id,
                KbGrant.kb_name == kb_name,
                KbGrant.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="该授权不存在")
    row.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    await log_audit(admin.id, "kbgrant.revoke", {"kb_name": kb_name, "user_id": user_id})
    return {"kb_name": kb_name, "user_id": user_id}


# ------------------------------------------------------- permission matrix UI


@router.get("/kb-users")
async def list_kb_users(db: AsyncSession = Depends(get_db)):
    """Every user, for the matrix's row selector — identifiers only, no secrets."""
    rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
    return {
        "items": [
            {"user_id": u.id, "username": u.username, "uid": u.uid, "role": u.role}
            for u in rows
        ]
    }


@router.get("/kb-user-access")
async def kb_user_access(
    user_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """One user's authorised vs. still-unauthorised databases, split in two.

    ``granted``   — active whitelist rows (revoked_at IS NULL), newest info only.
    ``available`` — every recorded credential the user is NOT yet granted, so the
                    admin can offer exactly the set that a grant would add. Each
                    carries ``has_api_key`` because granting a credential-less
                    database is rejected upstream (nothing to inject).
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    granted_rows = (
        await db.execute(
            select(KbGrant)
            .where(KbGrant.user_id == user_id, KbGrant.revoked_at.is_(None))
            .order_by(KbGrant.kb_name)
        )
    ).scalars().all()
    granted_names = {g.kb_name for g in granted_rows}
    keys = (await db.execute(select(KbKey).order_by(KbKey.kb_name))).scalars().all()
    return {
        "user_id": user.id,
        "username": user.username,
        "granted": [
            {
                "kb_name": g.kb_name,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g in granted_rows
        ],
        "available": [
            {"kb_name": k.kb_name, "has_api_key": bool(k.api_key_enc)}
            for k in keys
            if k.kb_name not in granted_names
        ],
    }


# ------------------------------------------------------------------- user side


@user_router.get("/my-databases")
async def my_databases(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Databases the caller may read — names only, no credentials."""
    return {"databases": await kb_access.granted_kbs(db, user.id)}


@user_router.get("/my-catalog")
async def my_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's authorised databases with their descriptions, for display.

    Names come from the whitelist (kb_grants); descriptions live only in the
    fastk server's catalog, so we fetch the listing and keep the granted
    entries — the same filter the agent proxy applies (see
    :func:`routers.kb_proxy._filtered_catalog`). ``uri`` is dropped: it is a
    host-side storage path with no meaning to the browser.

    Fails soft: an unreachable fastk server yields the granted names with empty
    descriptions rather than an error, so the panel still shows *what* the user
    can access even when the catalog metadata is temporarily unavailable.
    """
    granted = await kb_access.granted_kbs(db, user.id)
    allowed = set(granted)
    descriptions: dict[str, str] = {}
    if allowed:
        try:
            async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT) as client:
                resp = await client.get(
                    f"{settings.fastk_server_url.rstrip('/')}/fastk/api/databases/"
                )
            if resp.status_code == 200:
                for entry in resp.json():
                    if isinstance(entry, dict) and entry.get("name") in allowed:
                        descriptions[entry["name"]] = entry.get("description") or ""
        except httpx.HTTPError as exc:
            logger.warning("my-catalog: fastk listing failed for user=%s: %s", user.id, exc)

    return {
        "databases": [
            {"name": name, "description": descriptions.get(name, "")}
            for name in granted
        ]
    }
