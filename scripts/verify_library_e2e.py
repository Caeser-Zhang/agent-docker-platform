#!/usr/bin/env python3
"""End-to-end verification of the shared PPTX template library (t10).

Why this exists: the library is the piece that must stay O(1) — one physical
copy on the ``agent-pptx-lib`` volume, mounted rw into the backend and ro into
every user container. Nothing may leak into a workspace, and unlicensed
development samples must stay invisible (and un-fetchable) for normal users.

Runs INSIDE the backend container (stdlib only for the HTTP part):

    docker cp scripts/verify_library_e2e.py agent-docker-demo-backend-1:/tmp/
    docker exec agent-docker-demo-backend-1 python -u /tmp/verify_library_e2e.py

Add ``--with-container`` to also start the test user's agent container and
verify the read-only mount from inside it (needs the docker SDK, which the
backend image already has):

    docker exec agent-docker-demo-backend-1 python -u /tmp/verify_library_e2e.py --with-container

If the admin password is unknown, hand the script an already-minted admin JWT
instead (``auth.create_access_token`` — see ``.tmp/mint_caesar.sh``):

    docker exec -e LIBRARY_E2E_ADMIN_TOKEN="$TOK" ... python -u /tmp/verify_library_e2e.py

Covered dimensions:
  1. 目录服务    — /api/library/templates + /styles 只读可用，无需容器在跑
  2. 种子入库    — POST /api/admin/library/seed（含 samples/）并按内容哈希幂等
  3. 规范化验收  — 剥页 / 图片瘦身 / 体积基线 / zip 结构完整 / 厂商残留清零
  4. O(1) 存储   — stats copies==1，卡片 path 指向共享卷而非工作区
  5. 两段式缩略图 — 未生成 404 → PUT png → GET image/png
  6. 合规与权限  — sample 对用户不可见且不可下载；非管理员访问 admin 路由 403
  7. 上下架      — PATCH enabled 立刻反映到 enabled 列表
  8. 容器侧挂载  — （--with-container）/library/pptx 只读、可读、不可写

The script is additive and re-runnable: seeding is a no-op by content hash, the
upload probe re-sends the *original* seed bytes (so it de-duplicates instead of
minting a second record), the probe thumbnail is removed again at the end, and
the enabled flag is restored in a finally block.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

BASE = os.environ.get("LIBRARY_E2E_BASE", "http://localhost:8000")
ADMIN_USER = os.environ.get("LIBRARY_E2E_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("LIBRARY_E2E_ADMIN_PASS", "admin123")
# 已有管理员令牌时优先使用（本机用 .tmp/mint_caesar.sh 之类的脚本铸取），
# 免得为了跑验证去改任何人的口令。
ADMIN_TOKEN = os.environ.get("LIBRARY_E2E_ADMIN_TOKEN", "").strip()
USERNAME = os.environ.get("LIBRARY_E2E_USER", "libe2e")
PASSWORD = os.environ.get("LIBRARY_E2E_PASS", "lib-E2E-pass-2026")

# Mirrors config.settings — asserted against /api/admin/library/stats.
LIB_DIR = "/library/pptx"
LIB_VOLUME = "agent-pptx-lib"
# Read-only repo seed dir inside the backend (settings.pptx_library_seed_dir).
# Used to re-upload the *original* bytes: uploading the already-normalized
# output instead could mint a second record if the pipeline were not
# byte-idempotent, which would pollute the shared volume.
SEED_DIR = "/library-seed"
THUMB_MAX = (640, 360)  # services/pptx_library.py THUMB_SIZE

# The two real decks dropped into library/pptx-templates/samples/.
BROWN_KEY = "棕色"
BLUE_KEY = "蓝色"
# 验收基线：棕色商务风模板 13 页剥掉 2 页推广页 → 11 页，瘦身后 ≤ 2.6MB。
BROWN_MAX_BYTES = int(2.6 * 1024 * 1024)
VENDOR_MARKERS = (
    "islide", "islide.cn", "docer", "稻壳", "officeplus",
    "51ppt", "演界网", "熊猫办公", "图网",
)

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(cond: bool, msg: str) -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {msg}", flush=True)
    else:
        FAILED += 1
        print(f"  FAIL  {msg}", flush=True)
    return bool(cond)


def skip(msg: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {msg}", flush=True)


# ---------------------------------------------------------------------------
#  HTTP helpers (stdlib only, same shape as verify_pptx_e2e.py)
# ---------------------------------------------------------------------------
def http(method: str, path: str, body=None, token: str | None = None, timeout: int = 120):
    """JSON request/response. Returns (status, parsed-body)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def http_bytes(method: str, path: str, token: str | None = None,
               body: bytes | None = None, content_type: str | None = None,
               timeout: int = 180):
    """Binary request/response. Returns (status, content-type, payload bytes)."""
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def multipart(fields: dict, file_field: str, filename: str, data: bytes,
              file_type: str) -> tuple[bytes, str]:
    """Minimal multipart/form-data encoder (the upload routes are form-based)."""
    boundary = f"----libraryE2E{secrets.token_hex(8)}"
    buf = io.BytesIO()
    for key, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {file_type}\r\n\r\n".encode()
    )
    buf.write(data)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
#  pptx inspection (stdlib zipfile — no python-pptx inside the backend image)
# ---------------------------------------------------------------------------
SLIDE_PART_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")


def count_slides(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return len([n for n in zf.namelist() if SLIDE_PART_RE.match(n)])


def zip_is_valid(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.testzip() is None
    except zipfile.BadZipFile:
        return False


def scan_vendor_residue(data: bytes) -> list[str]:
    """Vendor/brand strings left behind in any XML part (case-insensitive)."""
    hits: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.endswith((".xml", ".rels")):
                continue
            text = zf.read(name).decode("utf-8", "ignore").lower()
            for marker in VENDOR_MARKERS:
                if marker.lower() in text:
                    hits.append(f"{name}→{marker}")
    return hits


def tiny_png(width: int = 32, height: int = 18, rgb=(31, 78, 121)) -> bytes:
    """A valid solid-colour PNG built with zlib+struct (stand-in for the
    browser-rendered first slide the admin page normally uploads)."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) straight out of the IHDR chunk, or None if not a PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


# ---------------------------------------------------------------------------
#  Steps
# ---------------------------------------------------------------------------
def auth() -> tuple[str, str, str] | None:
    """(admin_token, user_token, user_id)."""
    print("==> [1] 鉴权：管理员 + 普通用户", flush=True)
    if ADMIN_TOKEN:
        admin_token = ADMIN_TOKEN
        code, _ = http("GET", "/api/admin/library/stats", token=admin_token)
        if not check(code == 200,
                     f"LIBRARY_E2E_ADMIN_TOKEN 具备管理员权限（stats got {code}）"):
            return None
    else:
        code, body = http("POST", "/api/auth/login",
                          {"username": ADMIN_USER, "password": ADMIN_PASS})
        if code != 200 or not body.get("access_token"):
            print(f"  FAIL  admin login ({code}) {body} — 用 LIBRARY_E2E_ADMIN_USER/PASS "
                  f"或 LIBRARY_E2E_ADMIN_TOKEN 覆盖", flush=True)
            return None
        admin_token = body["access_token"]
        check(body.get("role") == "admin", f"管理员登录成功 role={body.get('role')}")

    # 先登录（重跑场景），失败再注册，注册被限流/重名则回落登录。
    code, reg = http("POST", "/api/auth/login",
                     {"username": USERNAME, "password": PASSWORD})
    if code != 200 or not reg.get("access_token"):
        code, reg = http("POST", "/api/auth/register",
                         {"username": USERNAME, "password": PASSWORD})
    if code != 200 or not reg.get("access_token"):
        code, reg = http("POST", "/api/auth/login",
                         {"username": USERNAME, "password": PASSWORD})
    if code != 200 or not reg.get("access_token"):
        print(f"  FAIL  user auth ({code}) {reg}", flush=True)
        return None
    check(reg.get("role") != "admin", f"普通用户登录成功 role={reg.get('role')}")
    return admin_token, reg["access_token"], reg.get("user_id", "")


def seed(admin_token: str) -> dict:
    print("==> [2] 种子入库：POST /api/admin/library/seed（allow_samples=true）", flush=True)
    code, res = http("POST", "/api/admin/library/seed", {"allow_samples": True},
                     token=admin_token, timeout=600)
    if not check(code == 200, f"seed 返回 200（got {code} {str(res)[:160]}）"):
        return {}
    print(f"      scanned={res.get('scanned')} ingested={res.get('ingested')} "
          f"skipped_existing={res.get('skipped_existing')} "
          f"samples_skipped={res.get('samples_skipped')} failed={res.get('failed')}", flush=True)
    check(not res.get("failed"), "没有入库失败的文件")
    check((res.get("scanned") or 0) > 0, "种子目录扫到了 .pptx")
    check((res.get("samples_skipped") or 0) == 0,
          "allow_samples=true 时 samples/ 未被跳过（两份真实模板参与入库）")
    return res


def stats_and_catalog(admin_token: str) -> tuple[dict, list[dict]]:
    print("==> [3] 管理员目录与 O(1) 存储统计", flush=True)
    stats_code, st = http("GET", "/api/admin/library/stats", token=admin_token)
    check(stats_code == 200, f"GET /stats 200（got {stats_code}）")
    if stats_code == 200:
        print(f"      root={st.get('root')} copies={st.get('copies')} "
              f"templates={st.get('templates')} enabled={st.get('enabled')} "
              f"with_thumb={st.get('with_thumb')} total={st.get('total_bytes')}B "
              f"raw={st.get('raw_input_bytes')}B saved={st.get('saved_bytes')}B "
              f"by_source={st.get('by_source')}", flush=True)
        check(st.get("copies") == 1,
              f"copies==1（一份物理副本被所有容器共享，got {st.get('copies')}）")
        check(st.get("root") == LIB_DIR, f"库根目录就是共享挂载点 {LIB_DIR}")
    code, body = http("GET", "/api/admin/library/templates", token=admin_token)
    cards = body.get("templates", []) if code == 200 else []
    check(code == 200 and len(cards) > 0, f"管理员能看到全部模板（{len(cards)} 个，含已下架）")
    return (st if stats_code == 200 else {}), cards


def find_card(cards: list[dict], keyword: str) -> dict | None:
    for card in cards:
        blob = f"{card.get('name') or ''} {card.get('name_zh') or ''}"
        if keyword in blob:
            return card
    return None


def verify_deck(admin_token: str, card: dict, *, max_bytes: int | None = None,
                expect_dropped: bool = True) -> bytes | None:
    """Per-template acceptance: metadata, normalization report, bytes on disk."""
    tid = card["id"]
    label = card.get("name_zh") or card.get("name") or tid
    print(f"      --- {label} ({tid[:12]}…) ---", flush=True)

    code, detail = http("GET", f"/api/admin/library/templates/{tid}", token=admin_token)
    if not check(code == 200, f"详情可读（got {code}）"):
        return None

    norm = detail.get("normalize") or {}
    slides_in, slides_out = norm.get("slides_in"), norm.get("slides_out")
    dropped = norm.get("dropped_slides") or 0
    print(f"      页数 {slides_in}→{slides_out} 剥页={dropped} "
          f"孤儿media={norm.get('orphan_media_removed')} 图片瘦身={norm.get('images_slimmed')} "
          f"体积 {norm.get('input_bytes')}→{norm.get('output_bytes')}B "
          f"(省 {norm.get('saved_bytes')}B)", flush=True)
    print(f"      画幅={detail.get('aspect')} 版式={detail.get('layouts')} "
          f"内容版式={detail.get('content_layout')} 页型={detail.get('page_type_summary')}", flush=True)
    print(f"      字体={detail.get('fonts')} 必须替换={detail.get('must_replace')}", flush=True)

    check(detail.get("path") == f"{LIB_DIR}/files/{tid}.pptx",
          f"卡片 path 指向共享卷（{detail.get('path')}），不进任何工作区")
    check(bool(detail.get("aspect")) and (detail.get("slides") or 0) > 0,
          f"元数据完整：slides={detail.get('slides')} aspect={detail.get('aspect')}")
    check(detail.get("slides") == slides_out,
          f"记录页数 == 规范化输出页数（{detail.get('slides')} vs {slides_out}）")
    if expect_dropped:
        check(dropped >= 1, f"推广页已剥离（dropped_slides={dropped}）")
        check(slides_out == slides_in - dropped,
              f"剥页数量与出入页数自洽（{slides_in}-{dropped}={slides_out}）")
    if max_bytes:
        check((detail.get("size_bytes") or 0) <= max_bytes,
              f"瘦身后体积 ≤ {max_bytes / 1024 / 1024:.1f}MB"
              f"（实际 {(detail.get('size_bytes') or 0) / 1024 / 1024:.2f}MB）")

    # 下载真实字节做结构与残留校验
    code, ctype, data = http_bytes("GET", f"/api/admin/library/templates/{tid}/file",
                                  token=admin_token)
    if not check(code == 200 and data[:4] == b"PK\x03\x04",
                 f"管理员可取回规范化后的 .pptx（{code}, {len(data)}B）"):
        return None
    check(len(data) == (detail.get("size_bytes") or -1),
          f"落盘字节数与记录一致（{len(data)}B）")
    check(zip_is_valid(data), "zip 结构完整（testzip 无损坏条目）")
    check(count_slides(data) == detail.get("slides"),
          f"包内 slide 部件数 == 记录页数（{count_slides(data)}）")
    residue = scan_vendor_residue(data)
    check(not residue, f"无厂商/品牌残留（命中：{residue[:5] or '无'}）")
    warnings = detail.get("warnings") or []
    if warnings:
        print(f"      warnings: {warnings}", flush=True)
    return data


def verify_idempotent(admin_token: str, card: dict) -> None:
    print("==> [5] 幂等：重跑 seed + 用原始字节重走上传入口", flush=True)
    code, res = http("POST", "/api/admin/library/seed", {"allow_samples": True},
                     token=admin_token, timeout=600)
    check(code == 200 and res.get("ingested") == 0,
          f"第二次 seed 不新增（ingested={res.get('ingested')} "
          f"skipped_existing={res.get('skipped_existing')}）")

    # 上传入口的幂等：用种子目录里的**原始**字节（record.name == 文件名 stem）。
    # 不回传规范化产物 —— 那会在流水线不是字节级幂等时凭空多出一条记录。
    tid = card["id"]
    stem = card.get("name") or ""
    origin = None
    for candidate in (f"{SEED_DIR}/samples/{stem}.pptx", f"{SEED_DIR}/{stem}.pptx"):
        if os.path.isfile(candidate):
            origin = candidate
            break
    if origin is None:
        skip(f"容器内读不到原始种子文件（{SEED_DIR}/{stem}.pptx），跳过上传幂等校验")
        return
    with open(origin, "rb") as fh:
        data = fh.read()
    print(f"      原始字节 {origin} = {len(data)}B（规范化后 {card.get('size_bytes')}B）", flush=True)

    before = http("GET", "/api/admin/library/stats", token=admin_token)[1].get("templates")
    payload, ctype = multipart({"source": "sample", "license": "sample"},
                               "file", f"{stem}.pptx", data,
                               "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    code, _, raw = http_bytes("POST", "/api/admin/library/templates", token=admin_token,
                              body=payload, content_type=ctype, timeout=600)
    try:
        body = json.loads(raw.decode("utf-8", "ignore") or "{}")
    except json.JSONDecodeError:
        body = {}
    check(code == 200 and body.get("created") is False,
          f"同一份原始字节再上传 → created=false（内容哈希去重，got {code}）")
    check((body.get("template") or {}).get("id") == tid,
          f"去重后仍是同一个模板 id（{(body.get('template') or {}).get('id')}）")

    after = http("GET", "/api/admin/library/stats", token=admin_token)[1].get("templates")
    check(before == after, f"库内模板数没有因为重传而增长（{before} → {after}）")


def verify_upload_guards(admin_token: str) -> None:
    print("==> [6] 上传入口的边界校验", flush=True)
    payload, ctype = multipart({}, "file", "notes.txt", b"hello", "text/plain")
    code, res, _ = http_bytes("POST", "/api/admin/library/templates", token=admin_token,
                              body=payload, content_type=ctype)
    check(code == 400, f"非 .pptx 文件名被拒（got {code}）")

    payload, ctype = multipart({}, "file", "fake.pptx", b"not a zip at all",
                               "application/octet-stream")
    code, res, _ = http_bytes("POST", "/api/admin/library/templates", token=admin_token,
                              body=payload, content_type=ctype)
    check(code == 400, f"伪 pptx（非 ZIP）被拒（got {code}）")


def verify_user_side(user_token: str, admin_token: str, cards: list[dict],
                     allow_samples_on: bool) -> None:
    print("==> [7] 用户侧目录与合规隔离", flush=True)
    code, body = http("GET", "/api/library/templates", token=user_token)
    user_cards = body.get("templates", []) if code == 200 else []
    check(code == 200, f"用户可读模板目录（{len(user_cards)} 个上架模板）")
    check(all(c.get("enabled") for c in user_cards), "用户目录只含已上架模板")

    samples = [c for c in cards if c.get("source") == "sample"]
    if not samples:
        skip("库里没有 sample 素材，跳过合规隔离校验")
        return
    sample_ids = {c["id"] for c in samples}
    leaked = [c["id"] for c in user_cards if c["id"] in sample_ids]
    if allow_samples_on:
        # 开关打开时"对用户可见"是运营者的明确选择，不算泄漏；
        # 反过来校验可见性一致（上架的 sample 都该出现）。
        visible = sorted(c["id"] for c in user_cards if c["id"] in sample_ids)
        expected = sorted(c["id"] for c in samples if c.get("enabled"))
        check(visible == expected,
              f"AGENT_PPTX_LIBRARY_ALLOW_SAMPLES 已开：上架 sample 对用户可见"
              f"（{len(visible)}/{len(expected)}）")
        return
    check(not leaked, f"未开样例开关时，用户目录看不到 sample（泄漏 {leaked or '无'}）")
    sid = samples[0]["id"]
    for path, what in ((f"/api/library/templates/{sid}", "详情"),
                       (f"/api/library/templates/{sid}/file", "字节"),
                       (f"/api/library/templates/{sid}/thumb", "缩略图")):
        code = http("GET", path, token=user_token)[0] if what == "详情" \
            else http_bytes("GET", path, token=user_token)[0]
        check(code == 404, f"用户拿不到 sample 的{what}（{code}）")


def verify_styles(user_token: str) -> dict:
    print("==> [8] 预定义风格：GET /api/library/styles", flush=True)
    code, st = http("GET", "/api/library/styles", token=user_token)
    if not check(code == 200, f"styles 200（got {code}）"):
        return {}
    palettes, recipes = st.get("palettes") or [], st.get("recipes") or []
    print(f"      palettes={len(palettes)} recipes={len(recipes)} dir={st.get('dir')} "
          f"units={st.get('units')}", flush=True)
    check(len(palettes) > 0 and len(recipes) > 0, "调色板与配方都已结构化落盘")
    check(st.get("dir") == f"{LIB_DIR}/styles",
          f"styles 目录同样在共享卷内（{st.get('dir')}），agent 本地可读")
    check(all(len(p.get("colors") or []) >= 3 for p in palettes), "每个调色板都带可用色值")
    return st


def verify_thumb_flow(admin_token: str, cards: list[dict]) -> None:
    print("==> [9] 两段式缩略图：404 → PUT png → GET image/png", flush=True)
    target = next((c for c in cards if not c.get("has_thumb")), None)
    if target is None:
        skip("所有模板都已有缩略图（重跑场景），跳过 404 分支")
        target = cards[0]
    tid = target["id"]
    had_thumb = bool(target.get("has_thumb"))

    original: bytes | None = None
    if had_thumb:
        code, _, original = http_bytes("GET", f"/api/admin/library/templates/{tid}/thumb",
                                      token=admin_token)
        if code != 200:
            original = None
    else:
        code, _, _ = http_bytes("GET", f"/api/admin/library/templates/{tid}/thumb",
                               token=admin_token)
        check(code == 404, f"未生成缩略图时 404（got {code}）—— 前端据此退化为色块占位")

    png = tiny_png()
    payload, ctype = multipart({}, "file", "slide1.png", png, "image/png")
    code, _, raw = http_bytes("PUT", f"/api/admin/library/templates/{tid}/thumb",
                              token=admin_token, body=payload, content_type=ctype)
    check(code == 200, f"PUT 缩略图成功（got {code} {raw[:120].decode('utf-8', 'replace')}）")

    code, got_ct, data = http_bytes("GET", f"/api/admin/library/templates/{tid}/thumb",
                                    token=admin_token)
    dims = png_size(data)
    # 后端会用 Pillow 重编码并限制到 THUMB_SIZE，所以不做字节级比对。
    check(code == 200 and got_ct.startswith("image/png") and dims is not None,
          f"GET 回一张合法 PNG（{code} {got_ct} {len(data)}B dims={dims}）")
    if dims:
        check(dims[0] <= THUMB_MAX[0] and dims[1] <= THUMB_MAX[1],
              f"缩略图被限制在 {THUMB_MAX} 以内（实际 {dims}）")

    code, _, _ = http_bytes("PUT", f"/api/admin/library/templates/{secrets.token_hex(16)}/thumb",
                            token=admin_token, body=payload, content_type=ctype)
    check(code == 404, f"给不存在的模板传缩略图 → 404（got {code}）")

    code, detail = http("GET", f"/api/admin/library/templates/{tid}", token=admin_token)
    check(code == 200 and detail.get("has_thumb") is True, "记录里 has_thumb 翻成 true")

    # 复原：纯色占位图不能留在库里冒充管理员渲染的真实首页预览。
    restore_thumb(tid, had_thumb, original)


def restore_thumb(tid: str, had_thumb: bool, original: bytes | None) -> None:
    """Undo the probe thumbnail.

    Runs inside the backend, where the library volume is mounted rw, so the
    exact prior state can be put back: either the original PNG bytes, or (when
    there was none) removing the file and flipping ``has_thumb`` back to false
    in ``index.json``. The backend re-reads the index on every request, so the
    edit is visible immediately.
    """
    thumb = f"{LIB_DIR}/thumbs/{tid}.png"
    index = f"{LIB_DIR}/index.json"
    if not os.path.isdir(LIB_DIR):
        skip(f"脚本不在 backend 容器内（看不到 {LIB_DIR}），无法复原缩略图状态")
        return
    try:
        if had_thumb:
            if original is not None:
                with open(thumb, "wb") as fh:
                    fh.write(original)
            print(f"      已复原原有缩略图（{len(original or b'')}B）", flush=True)
            return
        os.unlink(thumb)
        with open(index, encoding="utf-8") as fh:
            doc = json.load(fh)
        for record in doc.get("templates", []):
            if record.get("id") == tid:
                record["has_thumb"] = False
        tmp = f"{index}.e2e-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, index)
        print("      已移除占位缩略图并把 has_thumb 复原为 false", flush=True)
    except OSError as exc:
        check(False, f"复原缩略图状态失败：{exc}")


def verify_toggle(admin_token: str, card: dict) -> None:
    print("==> [10] 上下架：PATCH enabled", flush=True)
    tid = card["id"]
    try:
        code, res = http("PATCH", f"/api/admin/library/templates/{tid}",
                         {"enabled": False}, token=admin_token)
        check(code == 200 and res.get("template", {}).get("enabled") is False,
              f"下架成功（got {code}）")
        code, body = http("GET", "/api/admin/library/templates?enabled=true", token=admin_token)
        ids = {c["id"] for c in body.get("templates", [])}
        check(tid not in ids, "下架后不再出现在 enabled=true 列表")
        code, body = http("GET", "/api/library/templates", token=admin_token)
        check(tid not in {c["id"] for c in body.get("templates", [])},
              "下架后用户目录里也立刻消失")
    finally:
        # 无论前面断言是否失败，都要把上架状态复原，避免脚本留下副作用。
        code, res = http("PATCH", f"/api/admin/library/templates/{tid}",
                         {"enabled": True}, token=admin_token)
        check(code == 200 and res.get("template", {}).get("enabled") is True, "复原上架成功")


def verify_permissions(user_token: str) -> None:
    print("==> [11] 权限：普通用户不得触碰 admin 路由", flush=True)
    for path in ("/api/admin/library/stats", "/api/admin/library/templates"):
        code, _ = http("GET", path, token=user_token)
        check(code == 403, f"GET {path} → 403（got {code}）")
    code, _ = http("POST", "/api/admin/library/seed", {"allow_samples": True}, token=user_token)
    check(code == 403, f"POST /api/admin/library/seed → 403（got {code}）")
    code, _ = http("GET", "/api/library/templates")
    check(code in (401, 403), f"未登录访问用户目录被拒（got {code}）")


def verify_container_mount(user_token: str, user_id: str, card: dict) -> None:
    print("==> [12] 容器侧只读挂载（--with-container）", flush=True)
    code, st = http("POST", "/api/agent/start", {"wait": True}, token=user_token, timeout=600)
    if not check(code == 200 and st.get("running") is True,
                 f"测试用户容器已运行（status={st.get('status')} err={st.get('error')}）"):
        return

    try:
        import docker
    except ImportError:
        skip("backend 容器里没有 docker SDK，跳过挂载校验")
        return
    client = docker.from_env()
    name = st.get("container_name") or f"agent-{user_id}"
    try:
        ctr = client.containers.get(name)
    except Exception as exc:  # noqa: BLE001
        check(False, f"找不到容器 {name}: {exc}")
        return
    attrs = ctr.attrs
    mounts = attrs.get("Mounts") or []
    lib = next((m for m in mounts if m.get("Destination") == LIB_DIR), None)
    if not check(lib is not None, f"容器挂了 {LIB_DIR}（mounts={[m.get('Destination') for m in mounts]}）"):
        return
    check(lib.get("Name") == LIB_VOLUME, f"挂的就是共享卷 {LIB_VOLUME}（got {lib.get('Name')}）")
    check(lib.get("Mode") == "ro" and lib.get("RW") is False,
          f"以只读方式挂载（Mode={lib.get('Mode')} RW={lib.get('RW')}）")

    env = (attrs.get("Config") or {}).get("Env") or []
    check(any(e == f"PPTX_LIBRARY_DIR={LIB_DIR}" for e in env), "容器内注入了 PPTX_LIBRARY_DIR")

    tid, size = card["id"], card.get("size_bytes")
    path = f"{LIB_DIR}/files/{tid}.pptx"
    probe = (
        "import zipfile,os,re;"
        f"p={path!r};"
        "z=zipfile.ZipFile(p);"
        "r=re.compile(r'^ppt/slides/slide\\d+\\.xml$');"
        "n=len([x for x in z.namelist() if r.match(x)]);"
        "print(n, os.path.getsize(p))"
    )
    rc, out = ctr.exec_run(["python3", "-c", probe])
    text = (out or b"").decode("utf-8", "ignore").strip()
    check(rc == 0 and text.split()[:2] == [str(card.get("slides")), str(size)],
          f"容器内可直接读取并解析该模板（rc={rc} out={text!r} "
          f"期望 {card.get('slides')} {size}）")

    rc, out = ctr.exec_run(
        ["sh", "-c", f"cp {path} {LIB_DIR}/files/probe-{secrets.token_hex(4)}.pptx"])
    check(rc != 0, f"容器内不可写共享卷（rc={rc}）—— 字节无法被复制进用户空间")

    rc, out = ctr.exec_run(["sh", "-c", f"cp {path} /workspace/probe.pptx && ls -l /workspace/probe.pptx"])
    if rc == 0:
        ctr.exec_run(["rm", "-f", "/workspace/probe.pptx"])
    check(rc == 0, "agent 需要时仍能把模板复制到工作区自行加工（读权限不受限）")


def main(argv: list[str]) -> int:
    with_container = "--with-container" in argv

    creds = auth()
    if creds is None:
        return 1
    admin_token, user_token, user_id = creds

    seed(admin_token)
    stats, cards = stats_and_catalog(admin_token)
    if not cards:
        print("  FAIL  库里没有任何模板，后续校验无法进行", flush=True)
        return 1

    print("==> [4] 两份真实模板的规范化验收", flush=True)
    brown = find_card(cards, BROWN_KEY)
    blue = find_card(cards, BLUE_KEY)
    check(brown is not None, f"找到「{BROWN_KEY}」模板（samples/ 已被 seed 纳入）")
    check(blue is not None, f"找到「{BLUE_KEY}」模板")
    if brown:
        verify_deck(admin_token, brown, max_bytes=BROWN_MAX_BYTES)
    if blue:
        verify_deck(admin_token, blue, expect_dropped=False)

    target = brown or cards[0]
    verify_idempotent(admin_token, target)
    verify_upload_guards(admin_token)

    # 服务端是否开了样例开关，从用户目录能否看到 sample 反推（脚本不读 .env）。
    code, body = http("GET", "/api/library/templates", token=admin_token)
    allow_samples_on = any(c.get("source") == "sample" for c in body.get("templates", []))
    verify_user_side(user_token, admin_token, cards, allow_samples_on)
    verify_styles(user_token)
    verify_thumb_flow(admin_token, cards)
    verify_toggle(admin_token, blue or cards[0])
    verify_permissions(user_token)
    if with_container:
        verify_container_mount(user_token, user_id, target)
    else:
        skip("容器侧挂载校验（加 --with-container 开启）")

    print(f"\n==> RESULT: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped", flush=True)
    print(f"    库占用 {stats.get('total_bytes', 0) / 1024 / 1024:.2f}MB / "
          f"{stats.get('templates', 0)} 个模板 / copies={stats.get('copies')}"
          f"（原始 {stats.get('raw_input_bytes', 0) / 1024 / 1024:.2f}MB，"
          f"省 {stats.get('saved_bytes', 0) / 1024 / 1024:.2f}MB）", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    started = time.time()
    code = main(sys.argv[1:])
    print(f"    耗时 {time.time() - started:.1f}s", flush=True)
    sys.exit(code)
