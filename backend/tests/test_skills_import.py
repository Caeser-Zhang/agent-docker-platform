"""Unit tests for multi-format skill archive import (.zip/.rar/.7z/.tar*).

Two layers are covered:
  - app.services.archive_extract: format detection (extension + magic bytes),
    safe extraction per container format, and decompression-bomb limits.
  - POST /api/workspace/skills/import: end-to-end import behaviour for every
    supported format with the container manager faked out.
"""
import io
import sys
import tarfile
import types
import zipfile
from types import SimpleNamespace

import httpx
import py7zr
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.auth import get_current_user
from app.routers import workspace
from app.services import archive_extract as ae
from app.services.container_manager import container_manager

# ------------------------------------------------------------------
#  Helpers: in-memory archive builders & skill payloads
# ------------------------------------------------------------------

def skill_md(name: str = "demo", desc: str = "A demo skill") -> bytes:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n".encode()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def tar_bytes(files: dict[str, bytes], mode: str = "w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def sevenz_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with py7zr.SevenZipFile(buf, mode="w") as zf:
        for name, data in files.items():
            zf.writestr(data, name)
    return buf.getvalue()


def rar_info(filename: str, data: bytes, is_dir: bool = False):
    info = SimpleNamespace(filename=filename, data=data, file_size=len(data))
    info.is_dir = lambda: is_dir
    return info


# ------------------------------------------------------------------
#  Format detection
# ------------------------------------------------------------------

def test_detect_format_by_extension():
    cases = {
        "a.zip": ae.ArchiveFormat.ZIP,
        "a.rar": ae.ArchiveFormat.RAR,
        "a.7z": ae.ArchiveFormat.SEVEN_Z,
        "a.tar": ae.ArchiveFormat.TAR,
        "a.tar.gz": ae.ArchiveFormat.TAR,
        "a.tgz": ae.ArchiveFormat.TAR,
        "a.tar.bz2": ae.ArchiveFormat.TAR,
        "a.tbz2": ae.ArchiveFormat.TAR,
        "a.tar.xz": ae.ArchiveFormat.TAR,
        "a.txz": ae.ArchiveFormat.TAR,
    }
    for filename, expected in cases.items():
        assert ae.format_from_extension(filename) is expected


def test_detect_format_magic_fallback_for_misnamed_files():
    assert ae.detect_format("archive.dat", b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32) is ae.ArchiveFormat.RAR
    assert ae.detect_format("archive.dat", b"PK\x03\x04rest-of-zip") is ae.ArchiveFormat.ZIP
    assert ae.detect_format("archive.dat", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 26) is ae.ArchiveFormat.SEVEN_Z
    assert ae.detect_format("archive.dat", b"\x1f\x8b\x08\x00gzip-body") is ae.ArchiveFormat.TAR
    assert ae.detect_format("archive.dat", b"BZh91AY&SYbzip2-body") is ae.ArchiveFormat.TAR
    assert ae.detect_format("archive.dat", b"\xfd7zXZ\x00xz-body") is ae.ArchiveFormat.TAR


def test_detect_format_unknown_returns_none():
    assert ae.detect_format("program.exe", b"MZ\x90\x00" + b"\x00" * 32) is None
    assert ae.detect_format("notes.txt", b"plain text, no archive magic") is None
    assert ae.detect_format("empty.bin", b"") is None


# ------------------------------------------------------------------
#  Extraction per format
# ------------------------------------------------------------------

def test_extract_zip_returns_flat_clean_entries():
    data = zip_bytes({"skill-a/SKILL.md": b"x", "skill-a/ref.md": b"y"})
    assert ae.extract_archive(data, ae.ArchiveFormat.ZIP) == {
        "skill-a/SKILL.md": b"x",
        "skill-a/ref.md": b"y",
    }


@pytest.mark.parametrize("mode", ["w", "w:gz", "w:bz2", "w:xz"])
def test_extract_tar_family(mode):
    data = tar_bytes({"skill-t/SKILL.md": b"x"}, mode=mode)
    assert ae.extract_archive(data, ae.ArchiveFormat.TAR) == {"skill-t/SKILL.md": b"x"}


def test_extract_7z():
    data = sevenz_bytes({"skill-7/SKILL.md": b"x", "skill-7/assets/n.txt": b"y"})
    assert ae.extract_archive(data, ae.ArchiveFormat.SEVEN_Z) == {
        "skill-7/SKILL.md": b"x",
        "skill-7/assets/n.txt": b"y",
    }


def test_extract_empty_zip_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("only-a-dir/", b"")
    with pytest.raises(ae.ArchiveExtractError, match="压缩包为空"):
        ae.extract_archive(buf.getvalue(), ae.ArchiveFormat.ZIP)


@pytest.mark.parametrize("fmt", [ae.ArchiveFormat.ZIP, ae.ArchiveFormat.TAR])
def test_extract_corrupt_archive_raises(fmt):
    with pytest.raises(ae.ArchiveExtractError):
        ae.extract_archive(b"definitely-not-an-archive" * 16, fmt)


def test_extract_corrupt_7z_raises():
    with pytest.raises(ae.ArchiveExtractError):
        ae.extract_archive(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 64, ae.ArchiveFormat.SEVEN_Z)


# ------------------------------------------------------------------
#  Path-traversal & limits (bomb protection)
# ------------------------------------------------------------------

def test_extract_zip_rejects_path_traversal():
    data = zip_bytes({"../evil.txt": b"x"})
    with pytest.raises(ae.ArchiveExtractError, match="不安全路径"):
        ae.extract_archive(data, ae.ArchiveFormat.ZIP)


def test_extract_tar_rejects_absolute_path():
    data = tar_bytes({"/abs/evil.txt": b"x"})
    with pytest.raises(ae.ArchiveExtractError, match="不安全路径"):
        ae.extract_archive(data, ae.ArchiveFormat.TAR)


def test_extract_rejects_windows_drive_path():
    data = zip_bytes({"C:/evil/SKILL.md": b"x"})
    with pytest.raises(ae.ArchiveExtractError, match="不安全路径"):
        ae.extract_archive(data, ae.ArchiveFormat.ZIP)


def test_extract_rejects_too_many_files():
    files = {f"f{i}.txt": b"x" for i in range(6)}
    limits = ae.ExtractLimits(max_files=5, max_file_size=1024, max_total_size=1024)
    with pytest.raises(ae.ArchiveExtractError, match="文件数超过上限"):
        ae.extract_archive(zip_bytes(files), ae.ArchiveFormat.ZIP, limits)


def test_extract_rejects_oversized_member():
    limits = ae.ExtractLimits(max_files=10, max_file_size=8, max_total_size=1024)
    data = zip_bytes({"big.bin": b"0123456789"})
    with pytest.raises(ae.ArchiveExtractError, match="单个文件超过"):
        ae.extract_archive(data, ae.ArchiveFormat.ZIP, limits)


def test_extract_rejects_total_bomb():
    limits = ae.ExtractLimits(max_files=10, max_file_size=1024, max_total_size=12)
    data = zip_bytes({f"f{i}.txt": b"0123456789" for i in range(3)})
    with pytest.raises(ae.ArchiveExtractError, match="总大小"):
        ae.extract_archive(data, ae.ArchiveFormat.ZIP, limits)


# ------------------------------------------------------------------
#  RAR extraction (fake rarfile backend; the real one needs unrar/bsdtar)
# ------------------------------------------------------------------

@pytest.fixture
def fake_rarfile(monkeypatch):
    """Inject a controllable rarfile module whose members the test populates."""
    members: list = []

    class FakeError(Exception):
        pass

    class FakeRarFile:
        def __init__(self, fileobj, mode="r"):
            fileobj.seek(0)
            fileobj.read()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def infolist(self):
            return members

        def read(self, info):
            return info.data

    mod = types.ModuleType("rarfile")
    mod.RarFile = FakeRarFile
    mod.Error = FakeError
    monkeypatch.setitem(sys.modules, "rarfile", mod)
    return members


def test_extract_rar_with_backend(fake_rarfile):
    fake_rarfile.append(rar_info("skill-r/SKILL.md", b"x"))
    fake_rarfile.append(rar_info("skill-r/assets/n.txt", b"y"))
    payload = b"Rar!\x1a\x07\x01\x00" + b"fake-rar-body"
    assert ae.extract_archive(payload, ae.ArchiveFormat.RAR) == {
        "skill-r/SKILL.md": b"x",
        "skill-r/assets/n.txt": b"y",
    }


def test_extract_rar_ignores_dir_entries(fake_rarfile):
    fake_rarfile.append(rar_info("skill-r/", b"", is_dir=True))
    fake_rarfile.append(rar_info("skill-r/SKILL.md", b"x"))
    payload = b"Rar!\x1a\x07\x00" + b"fake-rar-body"
    assert ae.extract_archive(payload, ae.ArchiveFormat.RAR) == {"skill-r/SKILL.md": b"x"}


def test_extract_corrupt_rar_raises():
    # Works with the real rarfile package: data without a valid RAR signature
    # is rejected whether or not an unrar/bsdtar backend is installed.
    with pytest.raises(ae.ArchiveExtractError, match="无效的 rar"):
        ae.extract_archive(b"this-is-not-a-rar-archive-at-all" * 8, ae.ArchiveFormat.RAR)


# ------------------------------------------------------------------
#  Endpoint: POST /api/workspace/skills/import
# ------------------------------------------------------------------

@pytest_asyncio.fixture
async def import_client(monkeypatch):
    """App with only the workspace router; docker-side calls are faked."""
    app = FastAPI()
    app.include_router(workspace.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u1", username="tester", role="user"
    )

    async def ok_container(user):
        return None

    monkeypatch.setattr(workspace, "_require_container", ok_container)

    calls = {"deleted": [], "written": {}}

    def fake_delete(user_id, path):
        calls["deleted"].append(path)
        return True

    def fake_write(user_id, files):
        calls["written"].update(files)
        return True

    monkeypatch.setattr(container_manager, "delete_workspace_path", fake_delete)
    monkeypatch.setattr(container_manager, "write_workspace_files", fake_write)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, calls


async def test_import_zip_wrapped_layout(import_client):
    client, calls = import_client
    blob = zip_bytes({"demo-skill/SKILL.md": skill_md(), "demo-skill/docs.md": b"# docs"})
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", blob, "application/zip")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert [s["name"] for s in body["imported"]] == ["demo"]
    assert body["imported"][0]["description"] == "A demo skill"
    assert body["imported"][0]["scope"] == "project"
    assert body["imported"][0]["fileCount"] == 2
    assert calls["written"] == {
        ".opencode/skills/demo/SKILL.md": skill_md(),
        ".opencode/skills/demo/docs.md": b"# docs",
    }


async def test_import_zip_bare_layout_uses_frontmatter_name(import_client):
    client, calls = import_client
    blob = zip_bytes({"SKILL.md": skill_md(name="bare-skill", desc="bare")})
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", blob, "application/zip")},
    )
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["imported"]] == ["bare-skill"]
    assert ".opencode/skills/bare-skill/SKILL.md" in calls["written"]


async def test_import_zip_multi_skill(import_client):
    client, _ = import_client
    blob = zip_bytes({
        "alpha/SKILL.md": skill_md(name="alpha", desc="a"),
        "beta/SKILL.md": skill_md(name="beta", desc="b"),
    })
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", blob, "application/zip")},
    )
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["imported"]] == ["alpha", "beta"]


@pytest.mark.parametrize("mode,ext,mime", [
    ("w", "skills.tar", "application/x-tar"),
    ("w:gz", "skills.tar.gz", "application/gzip"),
    ("w:bz2", "skills.tar.bz2", "application/x-bzip2"),
    ("w:xz", "skills.tar.xz", "application/x-xz"),
])
async def test_import_tar_family(import_client, mode, ext, mime):
    client, calls = import_client
    blob = tar_bytes({"tar-skill/SKILL.md": skill_md(name="tar-skill", desc="tar skill")}, mode=mode)
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": (ext, blob, mime)},
    )
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["imported"]] == ["tar-skill"]
    assert ".opencode/skills/tar-skill/SKILL.md" in calls["written"]


async def test_import_7z(import_client):
    client, calls = import_client
    blob = sevenz_bytes({"seven-skill/SKILL.md": skill_md(name="seven-skill", desc="7z skill")})
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.7z", blob, "application/x-7z-compressed")},
    )
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["imported"]] == ["seven-skill"]
    assert ".opencode/skills/seven-skill/SKILL.md" in calls["written"]


async def test_import_rar(import_client, fake_rarfile):
    client, calls = import_client
    fake_rarfile.append(rar_info("rar-skill/SKILL.md", skill_md(name="rar-skill", desc="rar skill")))
    fake_rarfile.append(rar_info("rar-skill/readme.txt", b"hi"))
    payload = b"Rar!\x1a\x07\x01\x00" + b"body-not-parsed-by-the-fake-backend"
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.rar", payload, "application/vnd.rar")},
    )
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["imported"]] == ["rar-skill"]
    assert ".opencode/skills/rar-skill/readme.txt" in calls["written"]


async def test_import_misnamed_archive_still_imports_via_magic(import_client):
    client, calls = import_client
    blob = tar_bytes({"sniff-skill/SKILL.md": skill_md(name="sniff-skill", desc="sniffed")}, mode="w:gz")
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.dat", blob, "application/octet-stream")},
    )
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["imported"]] == ["sniff-skill"]
    assert calls["written"]


async def test_import_replaces_existing_skill(import_client):
    client, calls = import_client
    blob = zip_bytes({"demo/SKILL.md": skill_md(name="demo", desc="v2")})
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", blob, "application/zip")},
    )
    assert resp.status_code == 200
    assert ".opencode/skills/demo" in calls["deleted"]


async def test_import_unsupported_format_rejected(import_client):
    client, calls = import_client
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("program.exe", b"MZ\x90\x00" + b"\x00" * 64, "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "不支持的压缩包格式" in resp.json()["detail"]
    assert calls["written"] == {}


async def test_import_corrupt_zip_rejected(import_client):
    client, _ = import_client
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", b"PK\x03\x04" + b"garbage" * 32, "application/zip")},
    )
    assert resp.status_code == 400
    assert "无效的 zip" in resp.json()["detail"]


async def test_import_empty_upload_rejected(import_client):
    client, _ = import_client
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", b"", "application/zip")},
    )
    assert resp.status_code == 400
    assert "上传的文件为空" in resp.json()["detail"]


async def test_import_missing_skill_md_rejected(import_client):
    client, calls = import_client
    blob = zip_bytes({"some-dir/readme.txt": b"no skill here"})
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", blob, "application/zip")},
    )
    assert resp.status_code == 400
    assert "SKILL.md" in resp.json()["detail"]
    assert calls["written"] == {}


async def test_import_duplicate_skill_names_rejected(import_client):
    client, _ = import_client
    blob = zip_bytes({
        "one/SKILL.md": skill_md(name="dup", desc="first"),
        "two/SKILL.md": skill_md(name="dup", desc="second"),
    })
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", blob, "application/zip")},
    )
    assert resp.status_code == 400
    assert "重复的 skill 名称" in resp.json()["detail"]


async def test_import_oversized_compressed_upload_rejected(import_client, monkeypatch):
    client, _ = import_client
    monkeypatch.setattr(workspace, "MAX_UPLOAD_COMPRESSED", 8)
    resp = await client.post(
        "/api/workspace/skills/import",
        files={"file": ("skills.zip", b"0123456789", "application/zip")},
    )
    assert resp.status_code == 413
    assert "压缩包过大" in resp.json()["detail"]
