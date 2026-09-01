"""fastk knowledge-base proxy — chunk lookup and image relay.

The frontend renders `[[chunk:<db>/<chunk_id>]]` citation badges inside
assistant messages. Clicking a badge calls `GET /api/fastk/chunk` here; this
router forwards the lookup to the platform fastk server (fastdb serve fastapi,
`/fastk/api` prefix on the host) using a `chunk_id == '...'` filter query, and
relays attached chunk images through `/api/fastk/chunk-image` so the browser
never talks to the fastk server directly.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..auth import get_current_user
from ..config import settings

router = APIRouter(prefix="/api/fastk", tags=["fastk"])

# Same closed allowlist as the fastk server side: physical db names and hex
# chunk ids only. chunk_id is embedded into a filter expression, so anything
# outside this pattern must be rejected before it reaches the server.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_TIMEOUT = httpx.Timeout(15.0)


def _check_params(db: str, chunk_id: str) -> None:
    if not _NAME_RE.fullmatch(db):
        raise HTTPException(status_code=400, detail="无效的数据库名")
    if not _NAME_RE.fullmatch(chunk_id):
        raise HTTPException(status_code=400, detail="无效的 chunk_id")


def _api_base(db: str) -> str:
    """fastk server base for one database, e.g. .../fastk/api/databases/<db>."""
    return f"{settings.fastk_server_url.rstrip('/')}/fastk/api/databases/{db}"


@router.get("/chunk")
async def get_chunk(
    db: str = Query(...),
    chunk_id: str = Query(...),
    _user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    """Fetch one chunk's full content (text + metadata) by chunk_id."""
    _check_params(db, chunk_id)
    body = {"filter": f"chunk_id == '{chunk_id}'", "limit": 1}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(f"{_api_base(db)}/query", json=body)
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="fastk 服务不可达")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"fastk 服务返回 {resp.status_code}")
    results = resp.json().get("results") or []
    if not results:
        raise HTTPException(status_code=404, detail="未找到该内容块")
    chunk = results[0]
    # Relative image URL for the browser; served through this router only.
    if str(chunk.get("image_path") or "").strip():
        chunk["image_url"] = f"/api/fastk/chunk-image?db={db}&chunk_id={chunk_id}"
    else:
        chunk["image_url"] = None
    return chunk


@router.get("/chunk-image")
async def get_chunk_image(
    db: str = Query(...),
    chunk_id: str = Query(...),
    _user: Any = Depends(get_current_user),
) -> Response:
    """Relay the image attached to a chunk from the fastk server."""
    _check_params(db, chunk_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{_api_base(db)}/images", params={"chunk_id": chunk_id}
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="fastk 服务不可达")
    if resp.status_code != 200:
        detail = "图片获取失败" if resp.status_code != 404 else "该内容块没有图片"
        raise HTTPException(status_code=resp.status_code, detail=detail)
    media_type = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, media_type=media_type)
