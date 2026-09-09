"""Shared PPTX template library API — one physical copy, served to everyone.

Two routers live in this module:

    router        /api/library/*        any signed-in user  (read-only)
    admin_router  /api/admin/library/*  role="admin"        (ingest/edit/delete)

The bytes never travel through these endpoints in the normal flow. Templates
live on the ``agent-pptx-lib`` named volume, mounted rw into the backend and ro
into every user container (see ``services/pptx_library.py``), so the catalogue
responses hand the frontend a *container-side path* that gets injected into the
prompt — the agent then reads the deck straight off its own mount. ``/file``
and ``/thumb`` exist for the gallery and for admin inspection.
"""
import asyncio
import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..auth import get_current_user, require_admin
from ..config import settings
from ..models import User
from ..services.pptx_library import (
    EDITABLE_FIELDS,
    MAX_THUMB_BYTES,
    MAX_UPLOAD_BYTES,
    PptxNormalizeError,
    shared_library,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/library", tags=["library"], dependencies=[Depends(get_current_user)]
)
admin_router = APIRouter(
    prefix="/api/admin/library", tags=["library"], dependencies=[Depends(require_admin)]
)

UPLOAD_CHUNK_SIZE = 1024 * 1024
PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------
def _container_path(template_id: str) -> str:
    """Absolute path of the deck *inside a user container* (the ro mount)."""
    return f"{settings.pptx_library_dir}/files/{template_id}.pptx"


def _styles_dir() -> str:
    return f"{settings.pptx_library_dir}/styles"


def _card(record: dict) -> dict:
    """Gallery-facing subset — enough to pick a template, without the report."""
    template_id = record.get("id") or ""
    return {
        "id": template_id,
        "name": record.get("name"),
        "name_zh": record.get("name_zh"),
        "description": record.get("description"),
        "tags": record.get("tags") or [],
        "enabled": record.get("enabled"),
        "license": record.get("license"),
        "source": record.get("source"),
        "slides": record.get("slides"),
        "aspect": record.get("aspect"),
        "layouts": record.get("layouts"),
        "content_layout": record.get("content_layout"),
        "page_type_summary": record.get("page_type_summary"),
        "palette": record.get("palette"),
        "palette_hint": record.get("palette_hint"),
        "recipe": record.get("recipe"),
        "fonts": record.get("fonts"),
        "has_chart_part": record.get("has_chart_part"),
        "animated_slides": record.get("animated_slides"),
        "size_bytes": record.get("size_bytes"),
        "has_thumb": record.get("has_thumb"),
        "must_replace": record.get("must_replace") or [],
        "created_at": record.get("created_at"),
        # What the frontend injects into the prompt.
        "path": _container_path(template_id),
        "thumbUrl": f"/api/library/templates/{template_id}/thumb",
    }


def _detail(record: dict) -> dict:
    return {**record, "path": _container_path(record.get("id") or ""),
            "thumbUrl": f"/api/library/templates/{record.get('id')}/thumb"}


def _download_name(record: dict) -> str:
    base = _UNSAFE_FILENAME_RE.sub("_", (record.get("name") or "template").strip())
    return f"{(base or 'template')[:60]}-{record.get('id')}.pptx"


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """Stream in chunks so an oversized body is rejected before it is buffered."""
    buf = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_SIZE):
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(
                status_code=413, detail=f"文件过大（超过 {cap // 1024 // 1024}MB 上限）"
            )
    return bytes(buf)


def _split_form_list(raw: str | None) -> list[str] | None:
    """Form fields cannot carry arrays, so accept comma / newline separated text."""
    if raw is None:
        return None
    return [item.strip() for item in re.split(r"[,\n\r]+", raw) if item.strip()]


def _require_template(template_id: str, *, allow_samples: bool) -> dict:
    record = shared_library.get(template_id, allow_samples=allow_samples)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return record


# ------------------------------------------------------------------
#  User routes (read-only)
# ------------------------------------------------------------------
@router.get("/templates")
async def list_templates():
    """Enabled, non-sample templates — what the picker offers a user."""
    records = await asyncio.to_thread(
        shared_library.list_templates,
        enabled_only=True,
        allow_samples=settings.pptx_library_allow_samples,
    )
    return {"templates": [_card(r) for r in records], "count": len(records)}


@router.get("/styles")
async def get_styles():
    """Predefined palettes / composition recipes / typography rules.

    The same payload is written to ``/library/pptx/styles/*.json`` inside the
    volume, so the agent can read it locally without an HTTP round-trip.
    """
    return {**shared_library.styles(), "dir": _styles_dir()}


@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    """Full catalogue record, including residual-text and validation details."""
    record = await asyncio.to_thread(
        _require_template, template_id, allow_samples=settings.pptx_library_allow_samples
    )
    return _detail(record)


@router.get("/templates/{template_id}/file")
async def download_template(template_id: str):
    """Serve the normalized deck (gallery preview / admin inspection).

    Honours the same samples gate as the catalogue: a sample deck is invisible
    AND un-fetchable for normal users unless the operator opted in — otherwise
    its content id (leaked from an admin screen) would still hand out bytes of
    unlicensed third-party material. Admins use /api/admin/library/* instead.
    """
    record = _require_template(
        template_id, allow_samples=settings.pptx_library_allow_samples
    )
    path = await asyncio.to_thread(shared_library.file_path, template_id)
    if path is None:
        raise HTTPException(status_code=404, detail="模板文件缺失")
    return FileResponse(
        path, media_type=PPTX_CONTENT_TYPE, filename=_download_name(record)
    )


@router.get("/templates/{template_id}/thumb")
async def get_thumb(template_id: str):
    """First-slide preview. 404 until an admin uploads one (two-stage flow)."""
    _require_template(template_id, allow_samples=settings.pptx_library_allow_samples)
    path = await asyncio.to_thread(shared_library.thumb_path, template_id)
    if path is None:
        raise HTTPException(status_code=404, detail="暂无缩略图")
    return FileResponse(path, media_type="image/png")


# ------------------------------------------------------------------
#  Admin routes
# ------------------------------------------------------------------
class TemplatePatch(BaseModel):
    """Only ``EDITABLE_FIELDS`` are honoured; derived metadata is recomputed."""
    name: str | None = None
    name_zh: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    palette: str | None = None
    recipe: str | None = None
    must_replace: list[str] | None = None
    license: str | None = None
    source: str | None = None
    enabled: bool | None = None
    content_layout_note: str | None = None


class SeedRequest(BaseModel):
    # samples/ holds development-period material with no redistribution licence,
    # so it stays out of the production index unless explicitly requested.
    allow_samples: bool = False


@admin_router.get("/stats")
async def admin_stats():
    return await asyncio.to_thread(shared_library.stats, allow_samples=True)


@admin_router.get("/templates")
async def admin_list_templates(
    enabled: bool | None = Query(None, description="不传则返回全部（含已下架）"),
):
    records = await asyncio.to_thread(
        shared_library.list_templates,
        enabled_only=enabled is True,
        allow_samples=True,
    )
    if enabled is False:
        records = [r for r in records if not r.get("enabled")]
    return {"templates": [_card(r) for r in records], "count": len(records)}


@admin_router.get("/templates/{template_id}")
async def admin_get_template(template_id: str):
    record = _require_template(template_id, allow_samples=True)
    return _detail(record)


@admin_router.get("/templates/{template_id}/file")
async def admin_download_template(template_id: str):
    """Admin-only bytes: inspection, and the source for the browser-side
    first-slide render that feeds PUT /thumb (the backend cannot render pptx).

    Kept off the user router so a sample deck stays un-fetchable for normal
    users even when its id is known.
    """
    record = _require_template(template_id, allow_samples=True)
    path = await asyncio.to_thread(shared_library.file_path, template_id)
    if path is None:
        raise HTTPException(status_code=404, detail="模板文件缺失")
    return FileResponse(
        path, media_type=PPTX_CONTENT_TYPE, filename=_download_name(record)
    )


@admin_router.post("/templates")
async def admin_ingest_template(
    file: UploadFile = File(..., description="待入库的 .pptx"),
    name: str | None = Form(None),
    name_zh: str | None = Form(None),
    description: str | None = Form(None),
    tags: str | None = Form(None, description="逗号或换行分隔"),
    palette: str | None = Form(None),
    recipe: str | None = Form(None),
    license: str = Form("internal"),
    source: str = Form("upload"),
    enabled: bool = Form(True),
    optimize_images: bool = Form(True, description="图片瘦身（PNG→JPEG / 量化 / 降采样）"),
    drop_promo: bool = Form(True, description="剥离推广页并清洗品牌残留"),
    must_replace: str | None = Form(None, description="逗号或换行分隔"),
    admin: User = Depends(require_admin),
):
    """Normalize + store a template. Idempotent by content hash.

    Returns the normalization report so the admin can eyeball what was dropped
    (promo slides, slimmed images, vendor tags) before the deck goes live.
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".pptx"):
        raise HTTPException(status_code=400, detail="仅支持 .pptx 文件")

    data = await _read_capped(file, MAX_UPLOAD_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    if not data.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=400, detail="不是有效的 pptx（ZIP）文件")

    try:
        result = await asyncio.to_thread(
            lambda: shared_library.ingest(
                data,
                name=name or None,
                name_zh=name_zh,
                description=description,
                tags=_split_form_list(tags),
                palette=palette,
                recipe=recipe,
                license=license,
                source=source,
                enabled=enabled,
                optimize_images=optimize_images,
                drop_promo=drop_promo,
                must_replace=_split_form_list(must_replace),
            )
        )
    except PptxNormalizeError as exc:
        raise HTTPException(status_code=422, detail=f"入库失败：{exc}") from exc

    created = bool(result.get("created"))
    report = result.get("report") or {}
    record = {k: v for k, v in result.items() if k not in ("created", "report")}
    logger.info(
        "Admin %s %s template %s (%s)",
        admin.username, "ingested" if created else "re-uploaded",
        record.get("id"), filename,
    )
    return {
        "status": "ok",
        "created": created,
        "template": _card(record),
        "detail": _detail(record),
        "report": report,
    }


@admin_router.patch("/templates/{template_id}")
async def admin_update_template(template_id: str, patch: TemplatePatch):
    """Edit catalogue metadata or toggle a template on/off (下架 keeps the file)."""
    _require_template(template_id, allow_samples=True)
    body = {k: v for k, v in patch.model_dump(exclude_none=True).items()
            if k in EDITABLE_FIELDS}
    if not body:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    record = await asyncio.to_thread(shared_library.update, template_id, body)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"status": "ok", "template": _card(record)}


@admin_router.get("/templates/{template_id}/thumb")
async def admin_get_thumb(template_id: str):
    """Admin-side preview — works for sample decks the user router hides."""
    _require_template(template_id, allow_samples=True)
    path = await asyncio.to_thread(shared_library.thumb_path, template_id)
    if path is None:
        raise HTTPException(status_code=404, detail="暂无缩略图")
    return FileResponse(path, media_type="image/png")


@admin_router.put("/templates/{template_id}/thumb")
async def admin_set_thumb(
    template_id: str,
    file: UploadFile = File(..., description="首页预览图（png/jpg）"),
):
    """Store a first-slide preview.

    The backend cannot render pptx, so the admin page renders slide 1 in the
    browser (pptx-wasm) and uploads the raster here.
    """
    _require_template(template_id, allow_samples=True)
    data = await _read_capped(file, MAX_THUMB_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="上传的图片为空")
    try:
        await asyncio.to_thread(shared_library.set_thumb, template_id, data)
    except PptxNormalizeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "has_thumb": True,
            "thumbUrl": f"/api/library/templates/{template_id}/thumb"}


@admin_router.delete("/templates/{template_id}")
async def admin_delete_template(template_id: str, admin: User = Depends(require_admin)):
    _require_template(template_id, allow_samples=True)
    if not await asyncio.to_thread(shared_library.delete, template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    logger.info("Admin %s deleted template %s", admin.username, template_id)
    return {"status": "ok", "deleted": template_id}


@admin_router.post("/seed")
async def admin_seed(body: SeedRequest | None = None):
    """Re-run the add-only ingest of the repo seed directory.

    De-duplicated by source sha, so this is safe to call repeatedly; it is also
    run automatically at backend startup.
    """
    allow_samples = body.allow_samples if body else settings.pptx_library_allow_samples
    result = await asyncio.to_thread(
        shared_library.seed_from_dir,
        settings.pptx_library_seed_dir,
        allow_samples=allow_samples,
    )
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return {"status": "ok", **result}
