"""Shared PPTX template library — ingest, normalize, index, serve.

Storage model (the whole point: O(1), not O(N))
-----------------------------------------------
Templates and style presets live on ONE named Docker volume
(``agent-pptx-lib``). The backend mounts it read-write and is the only writer;
every per-user agent container mounts the very same volume read-only at
``/library/pptx`` (see ``container_manager._build_run_kwargs``). Nothing is
copied or seeded into a user's workspace or ``/data`` volume, so adding users
costs zero extra bytes no matter how large the library grows — and users
cannot modify platform assets even if they wanted to.

On-disk layout::

    /library/pptx/index.json          # the catalogue (atomic replace)
    /library/pptx/files/{id}.pptx     # normalized decks, one physical copy
    /library/pptx/thumbs/{id}.png     # first-slide previews
    /library/pptx/styles/*.json       # palettes / recipes / typography

Every file is chmod 0644 and every directory 0755. The backend runs as root
while containers run as uid 1000 on a *read-only* mount, so a container can
never fix bad permissions itself — they must be right at write time.

Ingest pipeline (:func:`normalize_pptx`)
----------------------------------------
Third-party decks are dirty. Before joining the library a template goes
through: promo-slide removal → orphan media cleanup → image slimming →
residual placeholder-text scan → metadata extraction → structural validation
→ atomic write → re-validation from disk.

Image slimming matters more than it looks. The library is O(1), but every
*generated* deck is O(N) and inherits the template's media: a 10 MB template
becomes ~3 GB across 300 users, a 2.5 MB template becomes ~750 MB.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import posixpath
import re
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml.ElementTree import fromstring as safe_fromstring
from PIL import Image

from ..config import settings
from . import pptx_styles

logger = logging.getLogger(__name__)

# --- Limits -----------------------------------------------------------------
MAX_UPLOAD_BYTES = 25 * 1024 * 1024          # 25 MB raw upload cap
MAX_THUMB_BYTES = 2 * 1024 * 1024            # 2 MB preview cap
MAX_UNCOMPRESSED_BYTES = 1 << 31             # zip-bomb guard (2 GiB)
MAX_COMPRESSION_RATIO = 200                  # zip-bomb guard

MAX_MEDIA_EDGE = 1600                        # downsample anything bigger
MIN_SLIM_EDGE = 100                          # never touch icons smaller than this
JPEG_QUALITY = 80
THUMB_SIZE = (640, 360)

INDEX_VERSION = 1

# --- Promo-page detection ---------------------------------------------------
# A slide is dropped when it hits a STRONG pattern (a known template-vendor
# brand), or when it hits >= 2 distinct WEAK patterns (generic "get more
# templates" marketing). Two tiers keep false positives off real content.
PROMO_STRONG_PATTERNS: tuple[str, ...] = (
    r"islide", r"officeplus", r"docer", r"稻壳", r"演界网", r"熊猫办公",
    r"51\s*ppt", r"第一\s*ppt", r"觅知", r"优品\s*ppt", r"retina\s*ppt",
)
PROMO_WEAK_PATTERNS: tuple[str, ...] = (
    r"扫码", r"公众号", r"更多模板", r"模板下载", r"版权声明", r"仅供学习",
    r"powered\s+by", r"免责声明", r"素材来源", r"更多精彩", r"授权使用",
)
PROMO_WEAK_THRESHOLD = 2

# --- Vendor branding scrub --------------------------------------------------
# Dropping the promo *slides* is not enough: the vendor's name also lives in the
# theme (``name="Designed by iSlide"``), document properties (``<Company>``),
# visible layout text runs (``<a:t>iSlide</a:t>``) and add-in tag parts
# (``ppt/tags/*.xml``). Left alone a "clean" template still advertises its
# origin. ``_scrub_branding`` neutralizes these without touching structure.
# Phrase rules run longest-first, then any bare token is blanked.
VENDOR_BRAND_TOKENS: tuple[str, ...] = ("islide",)
VENDOR_BRAND_PHRASES: tuple[tuple[str, str], ...] = (
    ("Designed by iSlide", "Template"),
    ("iSlide PowerPoint Template", "PowerPoint Template"),
)
# Parts whose human-readable strings get scrubbed. Namespace URIs and structural
# keywords never contain a brand token, so a literal replace here is safe.
_BRAND_SCRUB_PREFIXES: tuple[str, ...] = (
    "docProps/", "ppt/theme/", "ppt/slides/", "ppt/slideLayouts/",
    "ppt/slideMasters/", "ppt/notesSlides/", "ppt/notesMasters/",
)

# --- Residual placeholder text (reported, never silently rewritten) ---------
RESIDUAL_TEXT_PATTERNS: tuple[str, ...] = (
    r"Presenter name", r"Click to add", r"Click to edit", r"Type to add",
    r"Insert text", r"Lorem ipsum",
    r"单击此处添加", r"点击此处添加", r"在此处键入", r"请输入", r"添加标题",
    r"20\s*XX", r"XX\s*[\.年月日]", r"\bXXX+\b",
    r"-\d+\s*px", r"\{\{.*?\}\}", r"\[.*?填写.*?\]",
)

# --- XML surgery helpers ----------------------------------------------------
# Namespace prefixes are matched as ``\w+:`` rather than hardcoded ``a:``/``p:``
# — OOXML only fixes the namespace URIs, not the prefixes, and a generator is
# free to pick its own.
_A_T_RE = re.compile(r"<\w+:t(?:\s[^>]*)?>(.*?)</\w+:t>", re.S)
_REL_TAG_RE = re.compile(r"<Relationship\b[^>]*/>|<Relationship\b[^>]*>.*?</Relationship>", re.S)
_SLD_ID_RE = re.compile(r"<\w+:sldId\b[^>]*/>|<\w+:sldId\b[^>]*>.*?</\w+:sldId>", re.S)
_OVERRIDE_RE = re.compile(r"<Override\b[^>]*/>")
_TYPES_OPEN_RE = re.compile(r"<Types\b[^>]*>")
_SLDSZ_RE = re.compile(r"<\w+:sldSz\b[^>]*\bcx\s*=\s*\"(\d+)\"[^>]*\bcy\s*=\s*\"(\d+)\"")
_CSLD_NAME_RE = re.compile(r"<\w+:cSld\b[^>]*\bname\s*=\s*\"([^\"]*)\"")
_PH_TYPE_RE = re.compile(r"<\w+:ph\b[^>]*\btype\s*=\s*\"([^\"]*)\"")
_SRGB_RE = re.compile(r"<\w+:srgbClr\b[^>]*\bval\s*=\s*\"([0-9A-Fa-f]{6})\"")
_TYPEFACE_RE = re.compile(r"\btypeface\s*=\s*\"([^\"]+)\"")
_TARGET_ATTR_RE = re.compile(r"\bTarget\s*=\s*\"([^\"]*)\"")
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "tif", "tiff"}
IMAGE_CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                       "bmp": "image/bmp", "tif": "image/tiff", "tiff": "image/tiff"}

# Fields an admin may edit after ingest. Everything else is derived.
EDITABLE_FIELDS = frozenset({
    "name", "name_zh", "description", "tags", "palette", "recipe",
    "must_replace", "license", "source", "enabled", "content_layout_note",
})


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*=\s*\"([^\"]*)\"", tag)
    return m.group(1) if m else None


def _unescape(text: str) -> str:
    for entity, char in _XML_ENTITIES.items():
        text = text.replace(entity, char)
    return text


def _decode(data: bytes) -> str | None:
    """Strict UTF-8 decode; None when a part isn't valid UTF-8 (leave it alone)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _extract_text(xml: str) -> str:
    return " ".join(_unescape(m.group(1)).strip() for m in _A_T_RE.finditer(xml))


def _resolve_target(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(base_dir, target)) if base_dir else posixpath.normpath(target)


def _rels_base_dir(rels_name: str) -> str:
    """``ppt/slides/_rels/slide1.xml.rels`` → ``ppt/slides``."""
    return posixpath.dirname(posixpath.dirname(rels_name))


def _parse_rels(xml: str) -> list[dict]:
    out = []
    for m in _REL_TAG_RE.finditer(xml):
        tag = m.group(0)
        target = _attr(tag, "Target")
        if target is None:
            continue
        out.append({
            "id": _attr(tag, "Id"),
            "type": _attr(tag, "Type") or "",
            "target": target,
            "external": (_attr(tag, "TargetMode") or "").lower() == "external",
            "raw": tag,
            "span": (m.start(), m.end()),
        })
    return out


# --- Report -----------------------------------------------------------------
@dataclass
class NormalizeReport:
    input_bytes: int = 0
    output_bytes: int = 0
    slides_in: int = 0
    slides_out: int = 0
    dropped_slides: list[dict] = field(default_factory=list)
    orphan_media_removed: list[str] = field(default_factory=list)
    images_slimmed: list[dict] = field(default_factory=list)
    vendor_tags_removed: list[str] = field(default_factory=list)
    branding_scrubbed: list[str] = field(default_factory=list)
    residual_texts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def saved_bytes(self) -> int:
        return max(0, self.input_bytes - self.output_bytes)

    def to_dict(self) -> dict:
        return {
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "saved_bytes": self.saved_bytes,
            "saved_percent": round(100 * self.saved_bytes / self.input_bytes, 1) if self.input_bytes else 0.0,
            "slides_in": self.slides_in,
            "slides_out": self.slides_out,
            "dropped_slides": self.dropped_slides,
            "orphan_media_removed": self.orphan_media_removed,
            "images_slimmed": self.images_slimmed,
            "vendor_tags_removed": self.vendor_tags_removed,
            "branding_scrubbed": self.branding_scrubbed,
            "residual_texts": self.residual_texts,
            "warnings": self.warnings,
            "metadata": self.metadata,
        }


class PptxNormalizeError(ValueError):
    """Raised when the input isn't a usable .pptx or fails validation."""


# --- Load / validate --------------------------------------------------------
def _load_parts(data: bytes) -> dict[str, bytes]:
    """Unpack a .pptx into an ordered {part name: bytes} map, with safety checks."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise PptxNormalizeError(f"file too large ({len(data)} bytes > {MAX_UPLOAD_BYTES})")
    if not data.startswith(b"PK\x03\x04"):
        raise PptxNormalizeError("not a ZIP/OOXML package")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PptxNormalizeError(f"corrupt zip: {exc}") from exc

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        total = sum(i.file_size for i in infos)
        compressed = sum(i.compress_size for i in infos) or 1
        if total > MAX_UNCOMPRESSED_BYTES:
            raise PptxNormalizeError(f"uncompressed size too large ({total} bytes)")
        if total / compressed > MAX_COMPRESSION_RATIO:
            raise PptxNormalizeError(
                f"suspicious compression ratio {total / compressed:.0f}:1 (zip bomb?)"
            )
        parts: dict[str, bytes] = {}
        for info in infos:
            name = info.filename.replace("\\", "/").lstrip("/")
            if not name or name in parts:
                continue
            payload = zf.read(info)
            if name.endswith(".xml") or name.endswith(".rels"):
                # Parse with defusedxml: rejects entity expansion / external refs.
                try:
                    safe_fromstring(payload)
                except Exception as exc:  # noqa: BLE001
                    raise PptxNormalizeError(f"{name}: unsafe or malformed XML ({exc})") from exc
            parts[name] = payload

    for required in ("[Content_Types].xml", "ppt/presentation.xml", "_rels/.rels"):
        if required not in parts:
            raise PptxNormalizeError(f"missing required part {required} — not a .pptx")
    return parts


def _validate_parts(parts: dict[str, bytes]) -> list[str]:
    """Structural checks that catch the things PowerPoint reports as 'needs repair'."""
    warnings: list[str] = []
    ct = _decode(parts["[Content_Types].xml"]) or ""
    defaults = {m.group(1).lower() for m in re.finditer(r"<Default\b[^>]*\bExtension\s*=\s*\"([^\"]+)\"", ct, re.I)}
    overrides = {m.group(1).lstrip("/") for m in re.finditer(r"<Override\b[^>]*\bPartName\s*=\s*\"([^\"]+)\"", ct, re.I)}

    for name in parts:
        if name == "[Content_Types].xml":
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if name not in overrides and ext not in defaults:
            warnings.append(f"{name}: no [Content_Types] entry (PowerPoint may prompt to repair)")

    # Dangling relationships are the #1 cause of the repair prompt.
    for name, payload in parts.items():
        if not name.endswith(".rels"):
            continue
        xml = _decode(payload)
        if xml is None:
            continue
        base = _rels_base_dir(name)
        for rel in _parse_rels(xml):
            if rel["external"]:
                continue
            target = _resolve_target(base, rel["target"])
            if target not in parts:
                warnings.append(f"{name}: dangling relationship → {rel['target']}")

    pres = _decode(parts["ppt/presentation.xml"]) or ""
    sld_ids = re.findall(r"<\w+:sldId\b[^>]*\b\w+:id\s*=\s*\"([^\"]+)\"", pres)
    pres_rels = _parse_rels(_decode(parts["ppt/_rels/presentation.xml.rels"]) or "")
    known = {r["id"] for r in pres_rels}
    for rid in sld_ids:
        if rid not in known:
            warnings.append(f"presentation.xml: sldId r:id={rid} has no relationship")
    return warnings


# --- Image slimming ---------------------------------------------------------
def _has_real_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA", "PA"):
        alpha = img.getchannel("A")
        return alpha.getextrema() != (255, 255)
    if img.mode == "P":
        return "transparency" in img.info
    return False


def _slim_image(name: str, data: bytes) -> tuple[str, bytes, str] | None:
    """Return (new_name, new_bytes, action) or None to keep the image as-is.

    Rules: skip icons (< MIN_SLIM_EDGE on either side) and animated GIFs;
    downsample past MAX_MEDIA_EDGE; re-encode to JPEG q80 when the alpha
    channel carries no information; otherwise quantize to a 256-colour PNG.
    A change is only accepted when it actually shrinks the payload.
    """
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in IMAGE_EXTENSIONS:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001
        logger.debug("skip un-decodable image %s: %s", name, exc)
        return None
    width, height = img.size
    if width < MIN_SLIM_EDGE or height < MIN_SLIM_EDGE:
        return None

    action = ""
    if max(width, height) > MAX_MEDIA_EDGE:
        scale = MAX_MEDIA_EDGE / max(width, height)
        img = img.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                         Image.Resampling.LANCZOS)
        action = f"downsampled {width}x{height}→{img.size[0]}x{img.size[1]}"
        width, height = img.size

    transparent = _has_real_alpha(img)
    out_name, out_bytes = name, data
    if transparent:
        buf = io.BytesIO()
        img.convert("RGBA").quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(
            buf, format="PNG", optimize=True)
        candidate = buf.getvalue()
        if len(candidate) < len(data):
            out_bytes = candidate
            action = (action + "; " if action else "") + f"PNG quantized 256c ({width}x{height})"
    else:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY,
                                optimize=True, progressive=True)
        candidate = buf.getvalue()
        if len(candidate) < len(data):
            stem = name.rsplit(".", 1)[0]
            out_name = f"{stem}.jpg"
            out_bytes = candidate
            action = (action + "; " if action else "") + f"PNG→JPEG q{JPEG_QUALITY}"

    if out_bytes is data or (out_name == name and len(out_bytes) >= len(data)):
        return None
    return out_name, out_bytes, action


def _rewrite_rels_targets(parts: dict[str, bytes], renames: dict[str, str]) -> None:
    """Point every relationship at the renamed media part (path form preserved)."""
    for name in list(parts):
        if not name.endswith(".rels"):
            continue
        xml = _decode(parts[name])
        if xml is None:
            continue
        base = _rels_base_dir(name)
        changed = False

        def _sub(m: re.Match) -> str:
            nonlocal changed
            tag = m.group(0)
            if 'TargetMode="External"' in tag:
                return tag
            tm = re.search(r"\bTarget\s*=\s*\"([^\"]*)\"", tag)
            if not tm:
                return tag
            resolved = _resolve_target(base, tm.group(1))
            replacement = renames.get(resolved)
            if not replacement:
                return tag
            orig = tm.group(1)
            new_base = posixpath.basename(replacement)
            if new_base == posixpath.basename(orig):
                return tag
            # Keep the original target's directory form (../media/, /ppt/media/,
            # or same-dir) and swap only the filename — dropping the prefix would
            # leave a dangling relationship that PowerPoint reports as "repair".
            orig_dir = posixpath.dirname(orig)
            new_target = posixpath.join(orig_dir, new_base) if orig_dir else new_base
            changed = True
            return tag[:tm.start(1)] + new_target + tag[tm.end(1):]

        new_xml = _REL_TAG_RE.sub(_sub, xml)
        if changed:
            parts[name] = new_xml.encode("utf-8")


def _ensure_default_extension(parts: dict[str, bytes], ext: str) -> None:
    ct = _decode(parts["[Content_Types].xml"])
    if ct is None:
        return
    if re.search(rf"<Default\b[^>]*\bExtension\s*=\s*\"{re.escape(ext)}\"", ct, re.I):
        return
    m = _TYPES_OPEN_RE.search(ct)
    if not m:
        return
    entry = f'<Default Extension="{ext}" ContentType="{IMAGE_CONTENT_TYPES[ext]}"/>'
    parts["[Content_Types].xml"] = (ct[:m.end()] + entry + ct[m.end():]).encode("utf-8")


def _remove_parts(parts: dict[str, bytes], names: set[str]) -> None:
    for name in names:
        parts.pop(name, None)
        parts.pop(posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels"), None)
    if not names:
        return
    ct = _decode(parts.get("[Content_Types].xml", b""))
    if ct is None:
        return

    def _sub(m: re.Match) -> str:
        part_name = (_attr(m.group(0), "PartName") or "").lstrip("/")
        return "" if part_name in names else m.group(0)

    parts["[Content_Types].xml"] = _OVERRIDE_RE.sub(_sub, ct).encode("utf-8")


def _owns_rels(rels_name: str) -> str:
    """``ppt/_rels/presentation.xml.rels`` → ``ppt/presentation.xml``."""
    owner_dir = _rels_base_dir(rels_name)
    owner_base = posixpath.basename(rels_name)[:-len(".rels")]
    return posixpath.join(owner_dir, owner_base) if owner_dir else owner_base


def _neutralize_brand_text(txt: str) -> str:
    for phrase, repl in VENDOR_BRAND_PHRASES:
        txt = re.sub(re.escape(phrase), repl, txt, flags=re.I)
    for token in VENDOR_BRAND_TOKENS:
        txt = re.sub(re.escape(token), "", txt, flags=re.I)
    return txt


def _scrub_branding(parts: dict[str, bytes], report: NormalizeReport) -> None:
    """Strip the template vendor's name from metadata, theme, visible text and
    add-in tag parts — without any structural surgery that could break the deck.
    """
    lowered = tuple(t.lower() for t in VENDOR_BRAND_TOKENS)

    def _has_brand(txt: str) -> bool:
        low = txt.lower()
        return any(tok in low for tok in lowered)

    # 1. Drop vendor add-in tag parts, their inbound relationships, and any
    #    <p:tag> entries that referenced them (otherwise those rels dangle).
    drop_tags = {n for n in parts
                 if n.startswith("ppt/tags/") and _has_brand(_decode(parts[n]) or "")}
    if drop_tags:
        removed_rids: dict[str, set[str]] = {}
        for name in list(parts):
            if not name.endswith(".rels"):
                continue
            xml = _decode(parts[name])
            if xml is None:
                continue
            base = _rels_base_dir(name)
            gone: set[str] = set()

            def _rel_sub(m: re.Match, base=base, gone=gone) -> str:
                tag = m.group(0)
                if 'TargetMode="External"' in tag:
                    return tag
                tm = re.search(r"\bTarget\s*=\s*\"([^\"]*)\"", tag)
                if not tm or _resolve_target(base, tm.group(1)) not in drop_tags:
                    return tag
                rid = _attr(tag, "Id")
                if rid:
                    gone.add(rid)
                return ""

            new_xml = _REL_TAG_RE.sub(_rel_sub, xml)
            if gone:
                parts[name] = new_xml.encode("utf-8")
                removed_rids[_owns_rels(name)] = gone

        for owner, rids in removed_rids.items():
            oxml = _decode(parts.get(owner, b""))
            if oxml is None:
                continue

            def _tag_sub(m: re.Match, rids=rids) -> str:
                rid = _attr(m.group(0), "r:id") or _attr(m.group(0), "id")
                return "" if rid in rids else m.group(0)

            new_oxml = re.sub(r"<\w+:tag\b[^>]*/>", _tag_sub, oxml)
            if new_oxml != oxml:
                parts[owner] = new_oxml.encode("utf-8")

        _remove_parts(parts, drop_tags)
        report.vendor_tags_removed = sorted(drop_tags)

    # 2. Neutralize brand strings in metadata / theme / visible text runs.
    for name in list(parts):
        if not name.endswith(".xml") or not name.startswith(_BRAND_SCRUB_PREFIXES):
            continue
        txt = _decode(parts[name])
        if txt is None or not _has_brand(txt):
            continue
        new_txt = _neutralize_brand_text(txt)
        if new_txt != txt:
            parts[name] = new_txt.encode("utf-8")
            report.branding_scrubbed.append(name)


# --- Metadata extraction ----------------------------------------------------
def _layout_names(parts: dict[str, bytes]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, payload in parts.items():
        if not re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", name):
            continue
        xml = _decode(payload)
        if xml is None:
            continue
        m = _CSLD_NAME_RE.search(xml)
        out[name] = (m.group(1) if m else posixpath.basename(name))
    return out


def _classify_page(index: int, total: int, xml: str, layout: str, image_count: int) -> str:
    text = _extract_text(xml)
    lowered = f"{text} {layout}".lower()
    ph_types = set(_PH_TYPE_RE.findall(xml))
    is_first, is_last = index == 0, index == total - 1

    if any(k in lowered for k in ("谢谢", "感谢", "thanks", "thank you", "q&a", "q & a")):
        return "ending"
    if is_last and len(text) < 40:
        return "ending"
    if any(k in lowered for k in ("目录", "contents", "agenda", "content overview")):
        return "toc"
    if is_first or "ctrtitle" in ph_types or "title slide" in lowered or "标题" in layout:
        return "cover"
    if "section" in lowered or ("title" in ph_types and len(text) < 60 and image_count == 0):
        return "section"
    return "content"


def _extract_metadata(parts: dict[str, bytes], slide_parts: list[str]) -> dict:
    pres = _decode(parts["ppt/presentation.xml"]) or ""
    size_m = _SLDSZ_RE.search(pres)
    width_in = height_in = None
    aspect = "unknown"
    if size_m:
        width_in = round(int(size_m.group(1)) / 914400, 3)
        height_in = round(int(size_m.group(2)) / 914400, 3)
        ratio = width_in / height_in if height_in else 0
        aspect = "16:9" if abs(ratio - 16 / 9) < 0.02 else "4:3" if abs(ratio - 4 / 3) < 0.02 else f"{ratio:.2f}:1"

    layouts = _layout_names(parts)
    layout_of: dict[str, str] = {}
    for slide in slide_parts:
        rels_name = posixpath.join(posixpath.dirname(slide), "_rels", posixpath.basename(slide) + ".rels")
        xml = _decode(parts.get(rels_name, b""))
        if xml is None:
            continue
        for rel in _parse_rels(xml):
            if rel["external"]:
                continue
            resolved = _resolve_target(_rels_base_dir(rels_name), rel["target"])
            if resolved in layouts:
                layout_of[slide] = resolved
                break

    pages: list[dict] = []
    color_freq: dict[str, int] = {}
    typeface_freq: dict[str, int] = {}
    animated = 0
    total = len(slide_parts)
    for index, slide in enumerate(slide_parts):
        xml = _decode(parts.get(slide, b"")) or ""
        rels_name = posixpath.join(posixpath.dirname(slide), "_rels", posixpath.basename(slide) + ".rels")
        rels_xml = _decode(parts.get(rels_name, b"")) or ""
        images = sum(1 for r in _parse_rels(rels_xml)
                     if not r["external"]
                     and _resolve_target(_rels_base_dir(rels_name), r["target"]).startswith("ppt/media/"))
        text = _extract_text(xml)
        layout_part = layout_of.get(slide, "")
        pages.append({
            "index": index,
            "part": slide,
            "layout": layouts.get(layout_part, posixpath.basename(layout_part)) if layout_part else None,
            "type": _classify_page(index, total, xml, layouts.get(layout_part, ""), images),
            "title": (_A_T_RE.findall(xml) or [""])[0].strip()[:80],
            "text_chars": len(text),
            "images": images,
        })
        for colour in _SRGB_RE.findall(xml):
            color_freq[colour.upper()] = color_freq.get(colour.upper(), 0) + 1
        for face in _TYPEFACE_RE.findall(xml):
            if face and not face.startswith("+"):
                typeface_freq[face] = typeface_freq.get(face, 0) + 1
        if re.search(r"<\w+:timing\b", xml):
            animated += 1

    theme_fonts: dict[str, str | None] = {}
    theme = _decode(parts.get("ppt/theme/theme1.xml", b""))
    if theme:
        for role, tag in (("major", "majorFont"), ("minor", "minorFont")):
            block = re.search(rf"<\w+:{tag}>(.*?)</\w+:{tag}>", theme, re.S)
            if not block:
                continue
            latin = re.search(r"<\w+:latin\b[^>]*\btypeface\s*=\s*\"([^\"]*)\"", block.group(1))
            ea = re.search(r"<\w+:ea\b[^>]*\btypeface\s*=\s*\"([^\"]*)\"", block.group(1))
            theme_fonts[f"{role}_latin"] = (latin.group(1) if latin else None) or None
            theme_fonts[f"{role}_ea"] = (ea.group(1) if ea else None) or None

    palette_hint = [c for c, _ in sorted(color_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    content_pages = [p for p in pages if p["type"] == "content"]
    layout_freq: dict[str, int] = {}
    for page in content_pages:
        if page["layout"]:
            layout_freq[page["layout"]] = layout_freq.get(page["layout"], 0) + 1
    dominant_layout = max(layout_freq.items(), key=lambda kv: kv[1])[0] if layout_freq else None

    type_summary: dict[str, int] = {}
    for page in pages:
        type_summary[page["type"]] = type_summary.get(page["type"], 0) + 1

    content_types = (_decode(parts.get("[Content_Types].xml", b"")) or "").lower()
    has_chart = any(n.startswith("ppt/charts/") for n in parts) or "chart" in content_types
    return {
        "slides": total,
        "aspect": aspect,
        "slide_size_inches": [width_in, height_in],
        "layouts": len(layouts),
        "masters": sum(1 for n in parts if re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", n)),
        "page_types": pages,
        "page_type_summary": type_summary,
        "content_layout": dominant_layout,
        "content_layout_note": (
            f"Content slides use layout '{dominant_layout}' "
            f"({layout_freq[dominant_layout]}/{len(content_pages)} content pages)."
            if dominant_layout and content_pages else None
        ),
        "palette_hint": [f"#{c}" for c in palette_hint],
        "fonts": {
            "theme": theme_fonts,
            "used": [f for f, _ in sorted(typeface_freq.items(), key=lambda kv: -kv[1])[:6]],
        },
        "has_chart_part": has_chart,
        "embedded_fonts": sorted(n for n in parts if n.startswith("ppt/fonts/")),
        "media_count": sum(1 for n in parts if n.startswith("ppt/media/")),
        "animated_slides": animated,
    }


def _residual_scan(parts: dict[str, bytes], slide_parts: list[str]) -> list[dict]:
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    targets = list(slide_parts) + sorted(
        n for n in parts
        if re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", n)
        or re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", n)
    )
    compiled = [(p, re.compile(p, re.I)) for p in RESIDUAL_TEXT_PATTERNS]
    for name in targets:
        xml = _decode(parts.get(name, b""))
        if xml is None:
            continue
        for raw in _A_T_RE.findall(xml):
            text = _unescape(raw).strip()
            if not text or len(text) > 120:
                continue
            for pattern, rx in compiled:
                if rx.search(text) and (name, text) not in seen:
                    seen.add((name, text))
                    hits.append({"part": name, "pattern": pattern, "text": text})
                    break
    return hits


def _promo_score(text: str) -> tuple[list[str], list[str]]:
    strong = [p for p in PROMO_STRONG_PATTERNS if re.search(p, text, re.I)]
    weak = list({p for p in PROMO_WEAK_PATTERNS if re.search(p, text, re.I)})
    return strong, weak


# --- Repackage --------------------------------------------------------------
def _repackage(parts: dict[str, bytes]) -> bytes:
    """Rebuild the OPC package. ``[Content_Types].xml`` must be the first entry.

    Timestamps are pinned so identical input produces byte-identical output,
    which keeps the content-hash id stable across re-ingests.
    """
    head = [n for n in ("[Content_Types].xml", "_rels/.rels") if n in parts]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in head + [n for n in parts if n not in head]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, parts[name])
    return buf.getvalue()


def _slide_order(parts: dict[str, bytes]) -> list[tuple[str, str, tuple[int, int]]]:
    """[(rId, slide part name, span of the <p:sldId> tag)] in presentation order."""
    pres = _decode(parts["ppt/presentation.xml"])
    rels = _decode(parts["ppt/_rels/presentation.xml.rels"])
    if pres is None or rels is None:
        return []
    rid_to_target = {r["id"]: r["target"] for r in _parse_rels(rels) if not r["external"]}
    out = []
    for m in _SLD_ID_RE.finditer(pres):
        rid = _attr(m.group(0), "r:id") or _attr(m.group(0), "id")
        target = rid_to_target.get(rid or "")
        if target:
            out.append((rid, _resolve_target("ppt", target), (m.start(), m.end())))
    return out


# --- Public entry point -----------------------------------------------------
def normalize_pptx(
    data: bytes,
    *,
    optimize_images: bool = True,
    drop_promo: bool = True,
    prune_orphans: bool = True,
    scrub_branding: bool = True,
) -> tuple[bytes, NormalizeReport]:
    """Clean an untrusted .pptx and return (normalized bytes, report).

    Raises :class:`PptxNormalizeError` for anything that isn't a safe, valid
    presentation package.
    """
    report = NormalizeReport(input_bytes=len(data))
    parts = _load_parts(data)

    slides = _slide_order(parts)
    report.slides_in = len(slides)
    if not slides:
        raise PptxNormalizeError("presentation.xml declares no slides")

    # 1. Drop vendor promo / advertising slides.
    if drop_promo:
        dropped_parts: set[str] = set()
        drop_spans: list[tuple[int, int]] = []
        rel_ids: set[str] = set()
        for index, (rid, part, span) in enumerate(slides):
            xml = _decode(parts.get(part, b""))
            if xml is None:
                continue
            strong, weak = _promo_score(_extract_text(xml))
            if strong or len(weak) >= PROMO_WEAK_THRESHOLD:
                dropped_parts.add(part)
                drop_spans.append(span)
                rel_ids.add(rid or "")
                report.dropped_slides.append({
                    "index": index, "part": part,
                    "matched_strong": strong, "matched_weak": weak,
                })
        if dropped_parts:
            pres = _decode(parts["ppt/presentation.xml"]) or ""
            for start, end in sorted(drop_spans, reverse=True):
                pres = pres[:start] + pres[end:]
            parts["ppt/presentation.xml"] = pres.encode("utf-8")

            rels_name = "ppt/_rels/presentation.xml.rels"
            rels_xml = _decode(parts[rels_name]) or ""
            for rel in sorted((r for r in _parse_rels(rels_xml) if r["id"] in rel_ids),
                              key=lambda r: r["span"][0], reverse=True):
                rels_xml = rels_xml[:rel["span"][0]] + rels_xml[rel["span"][1]:]
            parts[rels_name] = rels_xml.encode("utf-8")

            # Notes slides hang off the dropped slide only — take them too.
            for part in list(dropped_parts):
                rels = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
                xml = _decode(parts.get(rels, b""))
                if xml is None:
                    continue
                for rel in _parse_rels(xml):
                    if rel["external"]:
                        continue
                    resolved = _resolve_target(_rels_base_dir(rels), rel["target"])
                    if resolved.startswith("ppt/notesSlides/"):
                        dropped_parts.add(resolved)
            _remove_parts(parts, dropped_parts)
        slides = _slide_order(parts)

    slide_parts = [part for _, part, _ in slides]
    report.slides_out = len(slide_parts)

    # 2. Drop media nothing references any more.
    if prune_orphans:
        referenced: set[str] = set()
        for name, payload in parts.items():
            if not name.endswith(".rels"):
                continue
            xml = _decode(payload)
            if xml is None:
                continue
            base = _rels_base_dir(name)
            for rel in _parse_rels(xml):
                if not rel["external"]:
                    referenced.add(_resolve_target(base, rel["target"]))
        orphans = {n for n in parts
                   if (n.startswith("ppt/media/") or n.startswith("ppt/embeddings/"))
                   and n not in referenced}
        if orphans:
            _remove_parts(parts, orphans)
            report.orphan_media_removed = sorted(orphans)

    # 3. Slim images. This is what keeps every generated deck (O(N)) small.
    if optimize_images:
        renames: dict[str, str] = {}
        used_names = set(parts)
        for name in sorted(n for n in parts if n.startswith("ppt/media/")):
            result = _slim_image(name, parts[name])
            if result is None:
                continue
            new_name, new_bytes, action = result
            if new_name in used_names and new_name != name:
                stem, ext = new_name.rsplit(".", 1)
                new_name = f"{stem}-{uuid.uuid4().hex[:4]}.{ext}"
            before = len(parts[name])
            report.images_slimmed.append({
                "part": name, "new_part": new_name, "action": action,
                "before_bytes": before, "after_bytes": len(new_bytes),
                "saved_bytes": before - len(new_bytes),
            })
            del parts[name]
            parts[new_name] = new_bytes
            used_names.discard(name)
            used_names.add(new_name)
            if new_name != name:
                renames[name] = new_name
        if renames:
            _rewrite_rels_targets(parts, renames)
            for ext in {n.rsplit(".", 1)[-1].lower() for n in renames.values()}:
                if ext in IMAGE_CONTENT_TYPES:
                    _ensure_default_extension(parts, ext)

    # 4. Scrub the vendor's branding from metadata / theme / text / tag parts.
    if scrub_branding:
        _scrub_branding(parts, report)

    # 5. Report leftover placeholder text (never rewritten silently).
    report.residual_texts = _residual_scan(parts, slide_parts)

    # 6. Metadata.
    report.metadata = _extract_metadata(parts, slide_parts)
    if report.metadata["animated_slides"]:
        report.warnings.append(
            f"{report.metadata['animated_slides']} slide(s) still carry <p:timing> animations"
        )

    # 7. Repackage + validate.
    output = _repackage(parts)
    report.output_bytes = len(output)
    report.warnings.extend(_validate_parts(parts))

    # 8. Re-validate the bytes we actually stored.
    try:
        with zipfile.ZipFile(io.BytesIO(output)) as zf:
            bad = zf.testzip()
            if bad:
                raise PptxNormalizeError(f"repackaged zip corrupt at {bad}")
    except zipfile.BadZipFile as exc:
        raise PptxNormalizeError(f"repackaged zip unreadable: {exc}") from exc
    return output, report


# --- Library ----------------------------------------------------------------
def _chmod_public(path: Path, mode: int = 0o644) -> None:
    """Make root-written files readable by uid 1000 inside the containers."""
    try:
        os.chmod(path, mode)
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning("chmod %o failed for %s: %s", mode, path, exc)


def content_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


class PptxLibrary:
    """Catalogue + blob store for the shared template library.

    All methods are synchronous and guarded by a lock; the routers call them
    through ``asyncio.to_thread`` so ingest (CPU-heavy) never blocks the loop.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.thumbs_dir = self.root / "thumbs"
        self.styles_dir = self.root / "styles"
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()

    # -- layout & index ------------------------------------------------------
    def ensure_layout(self) -> None:
        with self._lock:
            for directory in (self.root, self.files_dir, self.thumbs_dir, self.styles_dir):
                directory.mkdir(parents=True, exist_ok=True)
                _chmod_public(directory, 0o755)
            self._write_styles()
            if not self.index_path.exists():
                self._write_index({"version": INDEX_VERSION, "templates": []})

    def _write_styles(self) -> None:
        payload = pptx_styles.styles_payload()
        bundles = {
            "styles.json": payload,
            "palettes.json": {"version": payload["version"], "palettes": payload["palettes"]},
            "recipes.json": {"version": payload["version"], "recipes": payload["recipes"],
                             "selection_guide": payload["recipe_selection_guide"]},
            "typography.json": {"version": payload["version"], "typography": payload["typography"],
                                "rules": payload["rules"]},
        }
        for filename, body in bundles.items():
            path = self.styles_dir / filename
            blob = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            if path.exists() and path.read_bytes() == blob:
                continue
            self._atomic_write(path, blob)
            logger.info("Wrote style preset bundle %s (%d bytes)", path, len(blob))

    def _atomic_write(self, path: Path, blob: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_bytes(blob)
        _chmod_public(tmp, 0o644)
        os.replace(tmp, path)
        _chmod_public(path, 0o644)

    def _read_index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"version": INDEX_VERSION, "templates": []}

    def _write_index(self, index: dict) -> None:
        index["version"] = INDEX_VERSION
        index["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._atomic_write(self.index_path,
                           json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"))

    # -- queries -------------------------------------------------------------
    def _visible(self, record: dict, allow_samples: bool) -> bool:
        if record.get("source") == "sample" and not allow_samples:
            return False
        return True

    def list_templates(self, *, enabled_only: bool = True, allow_samples: bool = False) -> list[dict]:
        with self._lock:
            records = self._read_index().get("templates", [])
        out = [r for r in records if self._visible(r, allow_samples)]
        if enabled_only:
            out = [r for r in out if r.get("enabled")]
        return sorted(out, key=lambda r: (r.get("created_at") or "", r.get("id") or ""))

    def get(self, template_id: str, *, allow_samples: bool = False) -> dict | None:
        with self._lock:
            for record in self._read_index().get("templates", []):
                if record.get("id") == template_id:
                    return record if self._visible(record, allow_samples) else None
        return None

    def file_path(self, template_id: str) -> Path | None:
        path = self.files_dir / f"{template_id}.pptx"
        return path if path.is_file() else None

    def thumb_path(self, template_id: str) -> Path | None:
        path = self.thumbs_dir / f"{template_id}.png"
        return path if path.is_file() else None

    def styles(self) -> dict:
        return pptx_styles.styles_payload()

    # -- writes --------------------------------------------------------------
    def ingest(
        self,
        data: bytes,
        *,
        name: str | None = None,
        name_zh: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        palette: str | None = None,
        recipe: str | None = None,
        license: str = "internal",
        source: str = "upload",
        enabled: bool = True,
        optimize_images: bool = True,
        drop_promo: bool = True,
        must_replace: list[str] | None = None,
    ) -> dict:
        """Normalize + store a template. Returns the record plus its report.

        Idempotent: the same source bytes normalize to the same content id, so
        a repeated upload/seed is a no-op (``created`` is False).
        """
        source_sha = hashlib.sha256(data).hexdigest()
        normalized, report = normalize_pptx(
            data, optimize_images=optimize_images, drop_promo=drop_promo)
        template_id = content_id(normalized)
        meta = report.metadata

        with self._lock:
            self.ensure_layout()
            index = self._read_index()
            templates: list[dict] = index.setdefault("templates", [])
            for existing in templates:
                if existing.get("id") == template_id:
                    return {**existing, "created": False, "report": report.to_dict()}

            target = self.files_dir / f"{template_id}.pptx"
            self._atomic_write(target, normalized)

            record = {
                "id": template_id,
                "name": name or f"Template {template_id}",
                "name_zh": name_zh,
                "description": description,
                "tags": tags or [],
                "enabled": enabled,
                "license": license,
                "source": source,
                "source_sha256": source_sha,
                # --- derived (recomputed on every ingest, not admin-editable) ---
                "slides": meta["slides"],
                "aspect": meta["aspect"],
                "slide_size_inches": meta["slide_size_inches"],
                "layouts": meta["layouts"],
                "page_type_summary": meta["page_type_summary"],
                "page_types": meta["page_types"],
                "content_layout": meta["content_layout"],
                "content_layout_note": meta["content_layout_note"],
                "palette": palette,
                "palette_hint": meta["palette_hint"],
                "recipe": recipe,
                "fonts": meta["fonts"],
                "has_chart_part": meta["has_chart_part"],
                "embedded_fonts": meta["embedded_fonts"],
                "media_count": meta["media_count"],
                "animated_slides": meta["animated_slides"],
                "must_replace": must_replace if must_replace is not None
                else [h["text"] for h in report.residual_texts[:20]],
                "residual_texts": report.residual_texts,
                "warnings": report.warnings,
                "normalize": {
                    "slides_in": report.slides_in,
                    "slides_out": report.slides_out,
                    "input_bytes": report.input_bytes,
                    "output_bytes": report.output_bytes,
                    "saved_bytes": report.saved_bytes,
                    "dropped_slides": len(report.dropped_slides),
                    "orphan_media_removed": len(report.orphan_media_removed),
                    "images_slimmed": len(report.images_slimmed),
                },
                "size_bytes": len(normalized),
                "has_thumb": False,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            templates.append(record)
            self._write_index(index)
            logger.info(
                "Ingested template %s (%s): %d→%d slides, %d→%d bytes",
                template_id, record["name"], report.slides_in, report.slides_out,
                report.input_bytes, report.output_bytes,
            )
            return {**record, "created": True, "report": report.to_dict()}

    def find_by_source_sha(self, source_sha: str) -> dict | None:
        with self._lock:
            for record in self._read_index().get("templates", []):
                if record.get("source_sha256") == source_sha:
                    return record
        return None

    def update(self, template_id: str, patch: dict) -> dict | None:
        clean = {k: v for k, v in patch.items() if k in EDITABLE_FIELDS}
        if not clean:
            return self.get(template_id)
        with self._lock:
            index = self._read_index()
            for record in index.get("templates", []):
                if record.get("id") == template_id:
                    record.update(clean)
                    self._write_index(index)
                    return dict(record)
        return None

    def set_thumb(self, template_id: str, png: bytes) -> Path:
        """Store a first-slide preview. Re-encoded so size/format stay bounded."""
        if len(png) > MAX_THUMB_BYTES:
            raise PptxNormalizeError(f"thumbnail too large ({len(png)} bytes)")
        try:
            img = Image.open(io.BytesIO(png))
            img.load()
        except Exception as exc:  # noqa: BLE001
            raise PptxNormalizeError(f"unreadable thumbnail image: {exc}") from exc
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)

        with self._lock:
            if self.get(template_id, allow_samples=True) is None:
                raise PptxNormalizeError(f"unknown template {template_id}")
            self.ensure_layout()
            path = self.thumbs_dir / f"{template_id}.png"
            self._atomic_write(path, buf.getvalue())
            index = self._read_index()
            for record in index.get("templates", []):
                if record.get("id") == template_id:
                    record["has_thumb"] = True
                    self._write_index(index)
                    break
            return path

    def delete(self, template_id: str) -> bool:
        with self._lock:
            index = self._read_index()
            templates = index.get("templates", [])
            remaining = [r for r in templates if r.get("id") != template_id]
            if len(remaining) == len(templates):
                return False
            index["templates"] = remaining
            self._write_index(index)
            for path in (self.files_dir / f"{template_id}.pptx",
                         self.thumbs_dir / f"{template_id}.png"):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            logger.info("Deleted template %s from the library", template_id)
            return True

    def stats(self, *, allow_samples: bool = False) -> dict:
        with self._lock:
            records = [r for r in self._read_index().get("templates", [])
                       if self._visible(r, allow_samples)]
        by_source: dict[str, int] = {}
        for record in records:
            key = record.get("source") or "unknown"
            by_source[key] = by_source.get(key, 0) + 1
        return {
            "root": str(self.root),
            "templates": len(records),
            "enabled": sum(1 for r in records if r.get("enabled")),
            "with_thumb": sum(1 for r in records if r.get("has_thumb")),
            "total_bytes": sum(r.get("size_bytes") or 0 for r in records),
            "raw_input_bytes": sum((r.get("normalize") or {}).get("input_bytes") or 0 for r in records),
            "saved_bytes": sum((r.get("normalize") or {}).get("saved_bytes") or 0 for r in records),
            "by_source": by_source,
            # One physical copy shared by every container — this is the number
            # that does NOT grow with the user count.
            "copies": 1,
        }

    def seed_from_dir(self, seed_dir: str | Path, *, allow_samples: bool = False) -> dict:
        """Add-only ingest of every ``*.pptx`` in the repo seed directory.

        ``samples/`` is skipped unless the operator explicitly opts in — that
        material is a development-period sample with no redistribution licence.
        """
        base = Path(seed_dir)
        result = {"scanned": 0, "ingested": 0, "skipped_existing": 0, "failed": [], "samples_skipped": 0}
        if not base.is_dir():
            result["error"] = f"seed directory not found: {base}"
            return result

        candidates: list[tuple[Path, str]] = [(p, "seed") for p in sorted(base.glob("*.pptx"))]
        samples = sorted((base / "samples").glob("*.pptx"))
        if allow_samples:
            candidates += [(p, "sample") for p in samples]
        else:
            result["samples_skipped"] = len(samples)

        for path, source in candidates:
            result["scanned"] += 1
            try:
                data = path.read_bytes()
            except OSError as exc:
                result["failed"].append({"file": str(path), "error": str(exc)})
                continue
            sha = hashlib.sha256(data).hexdigest()
            if self.find_by_source_sha(sha):
                result["skipped_existing"] += 1
                continue
            try:
                record = self.ingest(
                    data,
                    name=path.stem,
                    source=source,
                    license="sample" if source == "sample" else "internal",
                    enabled=True,
                )
                if record.get("created"):
                    result["ingested"] += 1
                else:
                    result["skipped_existing"] += 1
            except (PptxNormalizeError, Exception) as exc:  # noqa: BLE001
                logger.warning("Seed ingest failed for %s: %s", path, exc)
                result["failed"].append({"file": str(path), "error": str(exc)})
        return result


# Process-wide singleton over the shared volume. It is a plain filesystem
# catalogue guarded by an RLock, so one instance serves every request.
shared_library = PptxLibrary(settings.pptx_library_dir)
