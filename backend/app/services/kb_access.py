"""Knowledge-base access control — the single source of truth for the whitelist.

Three consumers share this module so the permission decision can never fork:

  * ``routers/kb_proxy.py``   — agent containers reaching the fastk server
  * ``routers/fastk.py``      — the browser's citation-badge chunk/image lookup
  * ``routers/kb_keys.py``    — the admin management surface

Two orthogonal questions are answered here, and callers must not conflate them:

  1. **granted** — is this user on the whitelist for this database? (kb_grants)
     This is the enforcement point. Denial is a 403 with guidance.
  2. **api_key** — which credential does the platform inject when forwarding?
     (kb_keys, Fernet-decrypted) A missing credential is an operator error,
     not a user error, and surfaces as a 500.

The proxy token handed to agent containers is ``encrypt_secret("kbproxy:<uid>")``
— stateless, unforgeable without ``AGENT_SECRET_KEY``, and worth nothing beyond
the bearer's own grants. Real keys never enter a container.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..crypto import decrypt_secret, encrypt_secret
from ..models import KbGrant, KbKey

PROXY_TOKEN_PREFIX = "kbproxy:"


def issue_proxy_token(user_id: str) -> str:
    """Mint the per-container fastk credential (NOT a real API key)."""
    return encrypt_secret(f"{PROXY_TOKEN_PREFIX}{user_id}")


def verify_proxy_token(token: str | None) -> str | None:
    """Return the user_id a proxy token was issued to, or None if invalid."""
    if not token:
        return None
    plain = decrypt_secret(token)
    if not plain.startswith(PROXY_TOKEN_PREFIX):
        return None
    user_id = plain[len(PROXY_TOKEN_PREFIX):]
    return user_id or None


def catalog_headers() -> dict[str, str]:
    """Headers for reading the server's GLOBAL database listing.

    ``GET /fastk/api/databases/`` is server-wide, so the per-database keys in
    kb_keys do not apply to it — ``AGENT_KB_CATALOG_KEY`` is the credential the
    server accepts there. Empty (the default) means the endpoint needs no key,
    and no header is sent at all.

    Shared by the only two catalog readers (:mod:`routers.kb_proxy` and
    ``routers.kb_keys.my_catalog``) so they cannot drift apart. Note what this
    key does NOT do: it buys descriptions, never access. The list a user or an
    agent sees is still built from kb_grants, and this key must never reach a
    container — one that holds it could enumerate and read every database
    straight off the host gateway, whitelist notwithstanding.
    """
    return {"X-API-Key": settings.kb_catalog_key} if settings.kb_catalog_key else {}


def denial_message(kb_name: str) -> str:
    """403 text shown by the CLI and read by the agent.

    Deliberately actionable: it tells the agent what to run next
    (``fastk databases``) and who to contact for the missing grant, so the
    agent relays a constructive answer instead of a bare error. The contact
    list comes from ``AGENT_KB_ADMIN_CONTACT`` — never hardcoded here.
    """
    return (
        f"无权限访问知识库 '{kb_name}'。可运行 fastk databases 查看你当前可访问的知识库；"
        f"如需开通 '{kb_name}'，请联系管理员{settings.kb_admin_contact}。"
    )


@dataclass
class KbAccess:
    """Outcome of one whitelist lookup."""

    granted: bool
    api_key: str | None  # decrypted; None when absent or undecryptable


async def resolve_access(db: AsyncSession, kb_name: str, user_id: str) -> KbAccess:
    """Look up (grant, credential) for one user + physical database name."""
    grant = (
        await db.execute(
            select(KbGrant).where(
                KbGrant.user_id == user_id,
                KbGrant.kb_name == kb_name,
                KbGrant.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        return KbAccess(granted=False, api_key=None)

    row = (await db.execute(select(KbKey).where(KbKey.kb_name == kb_name))).scalar_one_or_none()
    api_key = decrypt_secret(row.api_key_enc) if row else ""
    return KbAccess(granted=True, api_key=api_key or None)


async def granted_kbs(db: AsyncSession, user_id: str) -> list[str]:
    """Physical database names this user may read (used to filter the catalog)."""
    rows = await db.execute(
        select(KbGrant.kb_name).where(
            KbGrant.user_id == user_id, KbGrant.revoked_at.is_(None)
        )
    )
    return sorted(rows.scalars().all())
