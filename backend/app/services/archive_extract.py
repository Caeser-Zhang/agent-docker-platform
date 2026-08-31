"""Multi-format archive extraction for skill imports.

Detects the archive format from the upload's extension (falling back to
magic-byte sniffing for misnamed files) and normalises every supported
container to a flat ``{clean_relative_path: bytes}`` mapping. Path-traversal
protection and size limits are enforced while streaming members out, so a
decompression bomb can never exhaust memory.

Supported formats: .zip, .rar, .7z, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2,
.tar.xz/.txz
"""
from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class ArchiveFormat(str, Enum):
    ZIP = "zip"
    RAR = "rar"
    SEVEN_Z = "7z"
    TAR = "tar"  # plain tar and every compressed tar variant (gz/bz2/xz)


class ArchiveExtractError(ValueError):
    """Raised when an archive cannot be read or violates import limits."""


@dataclass(frozen=True)
class ExtractLimits:
    """Safety caps applied while extracting (decompression-bomb guard)."""

    max_files: int = 500
    max_file_size: int = 5 * 1024 * 1024
    max_total_size: int = 20 * 1024 * 1024


DEFAULT_LIMITS = ExtractLimits()

# Longest-first so ".tar.gz" wins over ".tar"/".gz" during endswith matching.
_EXT_TO_FORMAT: dict[str, ArchiveFormat] = {
    ".zip": ArchiveFormat.ZIP,
    ".rar": ArchiveFormat.RAR,
    ".7z": ArchiveFormat.SEVEN_Z,
    ".tar": ArchiveFormat.TAR,
    ".tar.gz": ArchiveFormat.TAR,
    ".tgz": ArchiveFormat.TAR,
    ".tar.bz2": ArchiveFormat.TAR,
    ".tbz2": ArchiveFormat.TAR,
    ".tbz": ArchiveFormat.TAR,
    ".tar.xz": ArchiveFormat.TAR,
    ".txz": ArchiveFormat.TAR,
}
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(
    sorted(_EXT_TO_FORMAT, key=len, reverse=True)
)

_RAR_MAGIC = b"Rar!\x1a\x07"
_7Z_MAGIC = b"7z\xbc\xaf\x27\x1c"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def format_from_extension(filename: str) -> ArchiveFormat | None:
    name = (filename or "").lower()
    for ext in SUPPORTED_EXTENSIONS:
        if name.endswith(ext):
            return _EXT_TO_FORMAT[ext]
    return None


def format_from_magic(head: bytes) -> ArchiveFormat | None:
    """Sniff the container type from the leading bytes of the payload."""
    if head.startswith(_RAR_MAGIC):
        return ArchiveFormat.RAR
    if head.startswith(_7Z_MAGIC):
        return ArchiveFormat.SEVEN_Z
    if any(head.startswith(m) for m in _ZIP_MAGICS):
        return ArchiveFormat.ZIP
    if head.startswith(b"\x1f\x8b"):  # gzip
        return ArchiveFormat.TAR
    if head.startswith(b"BZh") and head[3:4] in b"123456789":  # bzip2
        return ArchiveFormat.TAR
    if head.startswith(b"\xfd7zXZ\x00"):  # xz
        return ArchiveFormat.TAR
    if len(head) >= 262 and head[257:262] == b"ustar":  # plain tar
        return ArchiveFormat.TAR
    return None


def detect_format(filename: str, data: bytes) -> ArchiveFormat | None:
    """Pick the archive format: a supported extension is trusted outright
    (corrupt payloads then fail inside the extractor with a clear error);
    otherwise magic bytes decide, so a misnamed archive still imports."""
    fmt = format_from_extension(filename)
    if fmt is not None:
        return fmt
    return format_from_magic(data[:512])


def clean_member_name(name: str) -> str | None:
    """Normalise an archive member name to a safe relative path, or None."""
    name = name.replace("\\", "/")
    if not name or name.startswith("/") or ":" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


class _EntryCollector:
    """Accumulates extracted members while enforcing the safety caps."""

    def __init__(self, limits: ExtractLimits) -> None:
        self.limits = limits
        self.entries: dict[str, bytes] = {}
        self._total = 0

    def add(self, raw_name: str, declared_size: int, read: Callable[[], bytes]) -> None:
        clean = clean_member_name(raw_name)
        if clean is None:
            raise ArchiveExtractError(f"压缩包含不安全路径: {raw_name}")
        if declared_size > self.limits.max_file_size:
            raise ArchiveExtractError(
                f"单个文件超过 {self.limits.max_file_size // 1024 // 1024}MB 上限: {raw_name}"
            )
        content = read()
        self._total += len(content)
        if self._total > self.limits.max_total_size:
            raise ArchiveExtractError("压缩包解压总大小超过 20MB 上限")
        self.entries[clean] = content


def _check_member_count(file_count: int, limits: ExtractLimits) -> None:
    if file_count == 0:
        raise ArchiveExtractError("压缩包为空")
    if file_count > limits.max_files:
        raise ArchiveExtractError(f"压缩包文件数超过上限 {limits.max_files}")


def extract_archive(
    data: bytes,
    fmt: ArchiveFormat,
    limits: ExtractLimits = DEFAULT_LIMITS,
) -> dict[str, bytes]:
    """Extract every regular file from the archive into {clean_rel_path: bytes}."""
    if fmt is ArchiveFormat.ZIP:
        entries = _extract_zip(data, limits)
    elif fmt is ArchiveFormat.RAR:
        entries = _extract_rar(data, limits)
    elif fmt is ArchiveFormat.SEVEN_Z:
        entries = _extract_7z(data, limits)
    else:
        entries = _extract_tar(data, limits)
    if not entries:
        raise ArchiveExtractError("压缩包为空")
    return entries


def _extract_zip(data: bytes, limits: ExtractLimits) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            _check_member_count(len(infos), limits)
            collector = _EntryCollector(limits)
            for info in infos:
                collector.add(info.filename, info.file_size, lambda i=info: zf.read(i))
    except zipfile.BadZipFile as exc:
        raise ArchiveExtractError(f"无效的 zip 压缩包: {exc}") from exc
    return collector.entries


def _extract_tar(data: bytes, limits: ExtractLimits) -> dict[str, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            files = [m for m in tf.getmembers() if m.isfile()]
            _check_member_count(len(files), limits)
            collector = _EntryCollector(limits)
            for member in files:
                def read(m=member):
                    fobj = tf.extractfile(m)
                    return fobj.read() if fobj else b""
                collector.add(member.name, member.size, read)
    except (tarfile.TarError, EOFError) as exc:
        raise ArchiveExtractError(f"无效的 tar 压缩包: {exc}") from exc
    return collector.entries


def _extract_7z(data: bytes, limits: ExtractLimits) -> dict[str, bytes]:
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - guaranteed by requirements
        raise ArchiveExtractError("服务器未安装 py7zr 组件，无法解析 .7z 压缩包") from exc
    try:
        with py7zr.SevenZipFile(io.BytesIO(data), mode="r") as zf:
            files = [i for i in zf.list() if not i.is_directory]
            _check_member_count(len(files), limits)
            # 7z headers declare exact uncompressed sizes; validate the total
            # BEFORE decompressing so a crafted bomb is rejected cheaply.
            declared_total = sum(i.uncompressed for i in files)
            if declared_total > limits.max_total_size:
                raise ArchiveExtractError("压缩包解压总大小超过 20MB 上限")
            blob = zf.readall()  # {filename: BytesIO}
            collector = _EntryCollector(limits)
            for info in files:
                fobj = blob.get(info.filename)
                if fobj is None:
                    continue
                collector.add(info.filename, info.uncompressed, fobj.read)
    except py7zr.Bad7zFile as exc:
        raise ArchiveExtractError(f"无效的 7z 压缩包: {exc}") from exc
    return collector.entries


def _extract_rar(data: bytes, limits: ExtractLimits) -> dict[str, bytes]:
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - guaranteed by requirements
        raise ArchiveExtractError("服务器未安装 rarfile 组件，无法解析 .rar 压缩包") from exc
    try:
        with rarfile.RarFile(io.BytesIO(data)) as rf:
            infos = [i for i in rf.infolist() if not i.is_dir()]
            _check_member_count(len(infos), limits)
            collector = _EntryCollector(limits)
            for info in infos:
                collector.add(info.filename, info.file_size, lambda i=info: rf.read(i))
    except rarfile.Error as exc:
        # Covers BadRarFile and RarCannotExec (unrar/bsdtar backend missing).
        raise ArchiveExtractError(
            f"无效的 rar 压缩包（或服务器缺少 unrar/bsdtar 解压工具）: {exc}"
        ) from exc
    return collector.entries
