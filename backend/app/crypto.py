"""Field-level encryption for sensitive user content.

API keys, MCP headers and MCP environment variables are stored encrypted at
rest using Fernet (AES-128-CBC + HMAC-SHA256, authenticated). The key is
derived from ``settings.secret_key`` via SHA-256, so the same secret key always
decrypts the same data. Rotating ``AGENT_SECRET_KEY`` invalidates previously
encrypted fields — encrypted columns must be treated as opaque tokens.

Callers only ever write these values (encrypt) and read them back when the
content needs to be used; the HTTP API masks them, exposing only presence flags
(e.g. ``hasApiKey``), consistent with the existing global config endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str | None) -> str:
    """Encrypt a plaintext secret. Blank/None is stored as '' (means "none")."""
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str | None) -> str:
    """Decrypt a secret token. Returns '' for blank tokens or a bad key."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def decrypt_password_compat(token: str | None) -> str:
    """Decrypt a stored container password, tolerating legacy plaintext rows.

    Rows written before P1-5 hold the raw BasicAuth password in
    ``agent_containers.password_enc``. Decryption fails for those, in which
    case the stored value is returned as-is; the next container lifecycle
    event re-encrypts it. Returns '' only for blank tokens.
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return token


def encrypt_json(value: dict[str, Any] | None) -> str:
    """Encrypt a JSON-serialisable dict (headers / environment)."""
    if not value:
        return ""
    return _fernet().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")


def decrypt_json(token: str | None) -> dict[str, Any]:
    """Decrypt a JSON dict token back into a dict ({} on blank / corruption)."""
    if not token:
        return {}
    try:
        data = json.loads(_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}