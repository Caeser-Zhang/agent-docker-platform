"""Access control for the shared OpenViking memory service.

Mirrors :mod:`app.services.kb_access` — same three invariants, different
upstream:

  * The OpenViking **root** key never leaves the backend and never enters a
    container. It only buys the Admin API (create account, register user,
    realign key); in ``api_key`` auth mode it is path-restricted to the admin
    plane and ``X-OpenViking-*`` identity headers are stripped, so it cannot
    read anybody's memories.
  * A container receives an opaque, encrypted **proxy token**
    (``ovproxy:<user_id>``) — stateless, unforgeable without
    ``AGENT_SECRET_KEY``, worth nothing beyond the bearer's own memory scope.
  * The real per-user OpenViking API key is injected by ``routers/ov_proxy.py``
    at forward time.

The per-user key is **derived, not stored** — hence no new DB column (plan §4.3.2).
OpenViking's ``register_user`` accepts a ``seed`` and computes the key as a pure
function of it (``sha256(f"{user_id}\\0{seed}")``, packed into
``base64url(account).base64url(user).base64url(secret)``), so the backend can
reproduce it locally at any time. The seed is an HMAC of ``AGENT_SECRET_KEY``,
which makes every key stable across restarts and replicas yet unlinkable to any
other platform secret. Losing the keystore volume therefore costs nothing but a
re-registration round trip.

Two in-process caches keep the hot path free of HTTP calls:
``_registered_users`` (positive, so registration happens once per user per
process) and ``_override_keys`` (a loud fallback for the read-back mismatch in
plan §7 R11). Both are safe to lose — everything they hold is re-derivable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from urllib.parse import quote

import httpx

from ..config import settings
from ..crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

PROXY_TOKEN_PREFIX = "ovproxy:"

# Domain-separation tag for the HMAC seed. Bumping it rotates every user's
# OpenViking key at once, so treat it as part of the key material.
_SEED_CONTEXT = "openviking-user-key-v1"

# Admin calls are short and rare (once per user per process); the memory-service
# data plane is proxied separately in routers/ov_proxy.py with its own timeouts.
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

# ``ALREADY_EXISTS`` maps to 409 (openviking/server/models.py).
_CONFLICT = 409


class OvUnavailable(RuntimeError):
    """OpenViking is disabled, unconfigured, or unreachable — proxy as 503."""


class OvAdminError(RuntimeError):
    """The Admin API rejected a key-management call."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
#  Container-facing proxy token (identical shape to kb_access)
# ---------------------------------------------------------------------------


def issue_proxy_token(user_id: str) -> str:
    """Mint the per-container memory-service credential (NOT a real API key)."""
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


# ---------------------------------------------------------------------------
#  Key derivation
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """True when the memory service is switched on AND admin-usable."""
    return bool(settings.openviking_enabled and settings.openviking_root_api_key)


def user_seed(user_id: str) -> str:
    """The deterministic seed handed to OpenViking for one user's key."""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"{_SEED_CONTEXT}\0{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _b64url(segment: str) -> str:
    return base64.urlsafe_b64encode(segment.encode("utf-8")).decode("utf-8").rstrip("=")


def derive_user_key(user_id: str) -> str:
    """Recompute a user's OpenViking API key locally — no round trip.

    Replicates ``openviking.server.api_keys.new.generate_api_key`` with a seed:
    ``secret = sha256(f"{user_id}\\0{seed}").hexdigest()``, then
    ``base64url(account_id).base64url(user_id).base64url(secret)`` with padding
    stripped. Verified against the server's read-back on registration.
    """
    secret = hashlib.sha256(
        f"{user_id}\0{user_seed(user_id)}".encode("utf-8")
    ).hexdigest()
    return ".".join(
        [
            _b64url(settings.openviking_account_id),
            _b64url(user_id),
            _b64url(secret),
        ]
    )


# ---------------------------------------------------------------------------
#  Admin API transport
# ---------------------------------------------------------------------------

_account_ready = False
_registered_users: set[str] = set()
_override_keys: dict[str, str] = {}


def _account_path(suffix: str = "") -> str:
    account = quote(settings.openviking_account_id, safe="")
    return f"/api/v1/admin/accounts/{account}{suffix}"


def _error_message(status_code: int, payload: dict) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if message:
            return str(message)
    return f"HTTP {status_code}"


async def _admin_post(path: str, body: dict) -> tuple[int, dict]:
    """POST to the Admin API with the root key. Returns (status, json)."""
    if not enabled():
        raise OvUnavailable("OpenViking memory service is disabled or has no root key")
    headers = {"Authorization": f"Bearer {settings.openviking_root_api_key}"}
    url = f"{settings.openviking_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise OvUnavailable(f"cannot reach OpenViking at {url}: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


async def _admin_get(path: str) -> tuple[int, dict]:
    """GET from the Admin API with the root key. Returns (status, json)."""
    if not enabled():
        raise OvUnavailable("OpenViking memory service is disabled or has no root key")
    headers = {"Authorization": f"Bearer {settings.openviking_root_api_key}"}
    url = f"{settings.openviking_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise OvUnavailable(f"cannot reach OpenViking at {url}: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


def _adopt_returned_key(user_id: str, returned: str | None) -> None:
    """Read-back check for plan §7 R11.

    ``register_user`` echoes the key it minted, which *must* equal our local
    derivation. If upstream ever changes the algorithm, honour what the server
    actually issued (for this process only) and log loudly, instead of handing
    the container a key that 401s on every call.
    """
    if not returned:
        return
    if returned != derive_user_key(user_id):
        logger.error(
            "OpenViking key for user %s differs from the local derivation; using "
            "the server-issued value for this process only. Fix by aligning "
            "derive_user_key() with upstream generate_api_key().",
            user_id,
        )
        _override_keys[user_id] = returned


def _result_key(payload: dict) -> str | None:
    result = payload.get("result")
    return result.get("user_key") if isinstance(result, dict) else None


async def ensure_account() -> None:
    """Idempotently create the platform's single OpenViking account."""
    global _account_ready
    if _account_ready:
        return
    status, payload = await _admin_post(
        "/api/v1/admin/accounts",
        {
            "account_id": settings.openviking_account_id,
            "admin_user_id": settings.openviking_admin_user_id,
            "seed": user_seed(settings.openviking_admin_user_id),
        },
    )
    if status == _CONFLICT:
        logger.info("OpenViking account %s already exists", settings.openviking_account_id)
    elif 200 <= status < 300:
        logger.info("OpenViking account %s created", settings.openviking_account_id)
    else:
        raise OvAdminError(status, _error_message(status, payload))
    _account_ready = True


async def _register_user(user_id: str) -> None:
    await ensure_account()
    status, payload = await _admin_post(
        f"{_account_path()}/users",
        {"user_id": user_id, "role": "user", "seed": user_seed(user_id)},
    )
    if 200 <= status < 300:
        _adopt_returned_key(user_id, _result_key(payload))
        return
    if status == _CONFLICT:
        # Returning user, or a concurrent first request that won the race. Both
        # are success: the key is derived, so we already have it.
        return
    raise OvAdminError(status, _error_message(status, payload))


async def _realign_user_key(user_id: str) -> None:
    """Force the server's stored key back onto our derivation.

    Only reached after upstream rejected a derived key, i.e. the old value is
    already useless — the keystore volume was restored, or the user was
    registered under a different ``AGENT_SECRET_KEY``. Regenerating from the
    same deterministic seed makes server and derivation agree again, so this
    costs the caller nothing.
    """
    status, payload = await _admin_post(
        f"{_account_path()}/users/{quote(user_id, safe='')}/key",
        {"seed": user_seed(user_id)},
    )
    if not 200 <= status < 300:
        raise OvAdminError(status, _error_message(status, payload))
    _adopt_returned_key(user_id, _result_key(payload))


# ---------------------------------------------------------------------------
#  Entry point used by routers/admin.py (memory-access listing)
# ---------------------------------------------------------------------------


async def list_ov_users_with_keys() -> list[dict]:
    """All users registered on the account, with their plaintext API keys.

    Read-back via the Admin API (``include_credentials=true``) rather than
    local derivation on purpose: listing must NOT lazily register platform
    users that never used the memory service. Each row is
    ``{"user_id", "role", "api_key"}``; ``api_key`` is None only if upstream
    withheld it despite the flag.
    """
    status, payload = await _admin_get(f"{_account_path()}/users?include_credentials=true")
    if not 200 <= status < 300:
        raise OvAdminError(status, _error_message(status, payload))
    result = payload.get("result")
    if not isinstance(result, list):
        raise OvAdminError(status, "unexpected Admin API response shape for user listing")
    rows: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        user_id = item.get("user_id") or item.get("id")
        if not user_id:
            continue
        rows.append({
            "user_id": str(user_id),
            "role": item.get("role"),
            "api_key": item.get("api_key") or item.get("user_key") or None,
        })
    return rows


# ---------------------------------------------------------------------------
#  Entry points used by routers/ov_proxy.py
# ---------------------------------------------------------------------------


async def user_api_key(user_id: str) -> str:
    """The credential to inject when forwarding this user to OpenViking.

    Registers lazily on first use, then serves from the local derivation.
    """
    if user_id not in _registered_users:
        await _register_user(user_id)
        _registered_users.add(user_id)
    return _override_keys.get(user_id) or derive_user_key(user_id)


async def reauthorize(user_id: str) -> str:
    """Recover after upstream answered 401/403 for a key we believe is valid."""
    invalidate(user_id)
    try:
        await _realign_user_key(user_id)
    except OvAdminError as exc:
        if exc.status_code != 404:
            raise
        # The user is gone from the keystore entirely — register from scratch.
        await _register_user(user_id)
    _registered_users.add(user_id)
    return _override_keys.get(user_id) or derive_user_key(user_id)


def invalidate(user_id: str) -> None:
    """Drop cached state for one user (e.g. after an upstream auth failure)."""
    _registered_users.discard(user_id)
    _override_keys.pop(user_id, None)
