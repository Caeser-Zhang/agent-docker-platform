"""意见反馈路由契约（设计文档 §8.1）。

锁死的核心性质：
  * 提交侧——分类白名单、内容长度、张数、限流、截图五道防线（5MB / 解码炸弹 /
    Pillow 重编码 PNG / 服务端随机命名 / 事务原子性）。
  * 管理侧——**权限边界即 router 边界**：普通用户访问任何 ``/api/admin/opinions/*``
    一律 403（D35：截图字节仅管理员可读）；管理员看板筛选 / 统计 / 改状态 / 转心愿。
  * 转心愿双通道幂等竞争（§6.8）：管理员通道与用户自助通道靠
    ``UPDATE ... WHERE linked_wish_id IS NULL`` + rowcount 收敛，恰好一方成功。

并发用例（#25）不依赖行锁，靠条件 UPDATE 的 rowcount 判定，SQLite 与 PostgreSQL
语义一致，可直接测（§10）。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import func, select

import app.models  # noqa: F401  (在 Base.metadata 上注册表)
from app.models import OpinionAttachment, OpinionFeedback, Wish
from app.routers import opinion_feedback as ofb
from app.routers import wishes


# ------------------------------------------------------------------
#  fixtures
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated(db_factory, monkeypatch):
    """每个用例独立库 + 清空模块级限流单例（跨测试会串味）。"""
    monkeypatch.setattr(ofb, "async_session", db_factory)
    monkeypatch.setattr(wishes, "async_session", db_factory)
    ofb._opinion_limiter._hits.clear()
    wishes._wish_create_limiter._hits.clear()
    wishes._wish_action_limiter._hits.clear()


@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    """把截图落盘目录指到 pytest 临时目录，否则会往 /app/data 写（§8.3）。"""
    d = tmp_path / "opinion-attachments"
    monkeypatch.setattr(ofb.settings, "opinion_attachment_dir", str(d))
    return d


@pytest.fixture
def user_client(app_client_factory):
    return app_client_factory([ofb.router], user_id="u1", username="alice")


@pytest.fixture
def admin_client(app_client_factory):
    return app_client_factory(
        [ofb.admin_router], user_id="admin1", username="root", role="admin"
    )


# ------------------------------------------------------------------
#  helpers
# ------------------------------------------------------------------
def png(size=(64, 64), color="red", fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


async def submit(client, *, category="bug", content="导出按钮点击后无响应", images=None):
    """multipart 提交（沿用 §8.1 的 files= 惯例）。无图时退化为普通表单。"""
    files = [
        ("images", (f"shot{i}.png", b, "image/png"))
        for i, b in enumerate(images or [])
    ]
    data = {"category": category, "content": content}
    if files:
        return await client.post("/api/opinions", data=data, files=files)
    return await client.post("/api/opinions", data=data)


async def seed_feedback(
    db_factory,
    *,
    user_id="u1",
    category="bug",
    content="一条反馈内容",
    status="open",
    name="alice",
    uid="00899219",
    linked_wish_id=None,
    created_at=None,
) -> int:
    async with db_factory() as db:
        fb = OpinionFeedback(
            user_id=user_id,
            category=category,
            name=name,
            uid=uid,
            content=content,
            status=status,
            linked_wish_id=linked_wish_id,
        )
        if created_at is not None:
            fb.created_at = created_at
        db.add(fb)
        await db.commit()
        await db.refresh(fb)
        return fb.id


async def seed_attachment(db_factory, feedback_id, *, filename="a.png", w=10, h=10) -> int:
    async with db_factory() as db:
        att = OpinionAttachment(
            feedback_id=feedback_id,
            filename=filename,
            content_type="image/png",
            width=w,
            height=h,
            size_bytes=123,
        )
        db.add(att)
        await db.commit()
        await db.refresh(att)
        return att.id


async def feedback_rows(db_factory) -> list[OpinionFeedback]:
    async with db_factory() as db:
        return list(
            (await db.execute(select(OpinionFeedback).order_by(OpinionFeedback.id))).scalars().all()
        )


async def attachment_rows(db_factory) -> list[OpinionAttachment]:
    async with db_factory() as db:
        return list(
            (await db.execute(select(OpinionAttachment).order_by(OpinionAttachment.id))).scalars().all()
        )


async def wish_rows(db_factory) -> list[Wish]:
    async with db_factory() as db:
        return list((await db.execute(select(Wish).order_by(Wish.id))).scalars().all())


# ==================================================================
#  分类与基础校验（#1 ~ #7）
# ==================================================================
async def test_01_submit_bug_records_identity_snapshot(user_client, db_factory):
    async with user_client as c:
        r = await submit(c, category="bug", content="导出按钮点击后无响应")
    assert r.status_code == 201
    body = r.json()
    assert body["category"] == "bug" and body["status"] == "open"
    assert body["attachments"] == []

    (row,) = await feedback_rows(db_factory)
    # name / uid 是登录态快照（D1），status 默认 open，未转化
    assert (row.category, row.name, row.uid) == ("bug", "alice", "00899219")
    assert row.status == "open" and row.linked_wish_id is None
    assert row.user_id == "u1"


async def test_02_submit_feature_does_not_auto_create_wish(user_client, db_factory):
    async with user_client as c:
        r = await submit(c, category="feature", content="希望支持导出为 PDF 格式")
    assert r.status_code == 201 and r.json()["category"] == "feature"
    # 转化是显式动作（D30），提交 feature 绝不自动建心愿
    assert await wish_rows(db_factory) == []
    (row,) = await feedback_rows(db_factory)
    assert row.linked_wish_id is None


async def test_03_invalid_or_missing_category_rejected(user_client, db_factory):
    async with user_client as c:
        assert (await submit(c, category="other", content="随便说点什么吧")).status_code == 422
        # 缺 category → FastAPI Form 必填校验 422
        r = await c.post("/api/opinions", data={"content": "随便说点什么吧"})
        assert r.status_code == 422
    assert await feedback_rows(db_factory) == []   # 非法请求不落库


async def test_04_content_too_short_rejected(user_client, db_factory):
    async with user_client as c:
        r = await submit(c, content="四个字啊"[:4])   # 4 字 < MIN_CONTENT_CHARS(5)
    assert r.status_code == 422 and "至少" in r.json()["detail"]
    assert await feedback_rows(db_factory) == []


async def test_05_content_too_long_rejected(user_client, db_factory):
    async with user_client as c:
        r = await submit(c, content="字" * 2001)
    assert r.status_code == 422 and "2000" in r.json()["detail"]
    assert await feedback_rows(db_factory) == []


async def test_06_rate_limit_sixth_submission_within_window(user_client, db_factory):
    async with user_client as c:
        for i in range(5):
            assert (await submit(c, content=f"第{i}条反馈内容")).status_code == 201
        sixth = await submit(c, content="第六条反馈内容")
    assert sixth.status_code == 429 and "频繁" in sixth.json()["detail"]
    assert len(await feedback_rows(db_factory)) == 5   # 第 6 次未落库


async def test_07_submit_requires_authentication(db_factory):
    """未登录 → 401：router 级 Depends(get_current_user) 生效（不覆盖该依赖）。"""
    app = FastAPI()
    app.include_router(ofb.router)   # 故意不 override get_current_user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/opinions", data={"category": "bug", "content": "未登录提交测试"})
    assert r.status_code == 401
    assert await feedback_rows(db_factory) == []


# ==================================================================
#  截图附件五道防线（#8 ~ #16）
# ==================================================================
async def test_08_single_image_persisted_with_server_filename(user_client, db_factory, attachment_dir):
    async with user_client as c:
        r = await submit(c, images=[png()])
    assert r.status_code == 201
    atts = r.json()["attachments"]
    assert len(atts) == 1 and atts[0]["url"] is None   # 用户侧响应不带 url（D35）

    (row,) = await attachment_rows(db_factory)
    # 服务端随机命名，绝不含客户端原始名（防路径穿越）
    assert re.fullmatch(r"[0-9a-f]{32}\.png", row.filename)
    assert row.filename != "shot0.png"
    assert (attachment_dir / row.filename).is_file()


async def test_09_large_image_downscaled_to_max_edge(user_client, db_factory, attachment_dir):
    async with user_client as c:
        r = await submit(c, images=[png(size=(3200, 1800))])
    assert r.status_code == 201
    (row,) = await attachment_rows(db_factory)
    assert max(row.width, row.height) == 1600          # 长边收敛到 ATTACHMENT_MAX_EDGE
    assert row.content_type == "image/png"
    saved = Image.open(attachment_dir / row.filename)
    assert saved.format == "PNG" and max(saved.size) == 1600
    assert (row.width, row.height) == saved.size       # 元数据与落盘一致


async def test_10_gif_input_reencoded_to_png(user_client, db_factory, attachment_dir):
    async with user_client as c:
        r = await submit(c, images=[png(fmt="GIF")])
    assert r.status_code == 201
    (row,) = await attachment_rows(db_factory)
    assert row.content_type == "image/png"
    assert Image.open(attachment_dir / row.filename).format == "PNG"   # 统一转 PNG（§6.7）


async def test_11_four_images_rejected_atomically(user_client, db_factory, attachment_dir):
    async with user_client as c:
        r = await submit(c, images=[png() for _ in range(4)])
    assert r.status_code == 422 and "最多" in r.json()["detail"]
    # 张数校验先于写文件：一张都不落盘、不落库
    assert await attachment_rows(db_factory) == []
    assert await feedback_rows(db_factory) == []
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


async def test_12_oversized_image_rejected_413(user_client, db_factory, attachment_dir):
    huge = os.urandom(5 * 1024 * 1024 + 100)   # >5MB；_read_capped 在解码前即拒绝
    async with user_client as c:
        r = await submit(c, images=[huge])
    assert r.status_code == 413 and "过大" in r.json()["detail"]
    assert await feedback_rows(db_factory) == []
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


async def test_13_non_image_bytes_rejected_no_residual(user_client, db_factory, attachment_dir):
    evil = b"MZ\x90\x00\x03\x00\x00\x00" + os.urandom(2048)   # 伪造 .png 的 PE 头
    async with user_client as c:
        r = await submit(c, images=[evil])
    assert r.status_code == 422 and "无法解析" in r.json()["detail"]
    assert await attachment_rows(db_factory) == []
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


async def test_14_decode_bomb_rejected_before_load(user_client, db_factory, attachment_dir):
    bomb = png(size=(30000, 1))   # 声明尺寸超 DECODE_BOMB_EDGE，压缩后仅数百字节
    assert len(bomb) < 5 * 1024
    async with user_client as c:
        r = await submit(c, images=[bomb])
    assert r.status_code == 422 and "尺寸异常" in r.json()["detail"]
    assert await attachment_rows(db_factory) == []


async def test_15_path_traversal_filename_neutralized(user_client, db_factory, attachment_dir):
    files = [("images", ("../../etc/passwd", png(), "image/png"))]
    async with user_client as c:
        r = await c.post("/api/opinions", data={"category": "bug", "content": "路径穿越测试内容"}, files=files)
    assert r.status_code == 201
    (row,) = await attachment_rows(db_factory)
    assert re.fullmatch(r"[0-9a-f]{32}\.png", row.filename)
    # 落盘文件仍在附件目录内，未在上级目录留下任何文件
    assert (attachment_dir / row.filename).is_file()
    assert list(attachment_dir.iterdir()) == [attachment_dir / row.filename]


async def test_16_second_image_invalid_writes_nothing(user_client, db_factory, attachment_dir):
    """阶段一全部解码成功才开事务：第 2 张是解码炸弹 → 一张都不落盘（原子性）。"""
    async with user_client as c:
        r = await submit(c, images=[png(), png(size=(30000, 1))])
    assert r.status_code == 422
    assert await attachment_rows(db_factory) == []
    assert await feedback_rows(db_factory) == []
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


# ==================================================================
#  后台看板：权限（#17）+ 功能（#18 ~ #20）
# ==================================================================
async def test_17_admin_endpoints_forbidden_for_normal_user(app_client_factory, db_factory):
    """普通用户访问全部管理端点 → 403（权限边界即 router 边界，D35）。"""
    fid = await seed_feedback(db_factory)
    aid = await seed_attachment(db_factory, fid)
    user = app_client_factory([ofb.admin_router], user_id="u1", username="alice", role="user")
    async with user as c:
        assert (await c.get("/api/admin/opinions")).status_code == 403
        assert (await c.get("/api/admin/opinions/stats")).status_code == 403
        assert (await c.get(f"/api/admin/opinions/{fid}/attachments")).status_code == 403
        assert (await c.get(f"/api/admin/opinions/attachments/{aid}")).status_code == 403
        assert (await c.post(f"/api/admin/opinions/{fid}/to-wish", json={"title": "x"})).status_code == 403


async def test_18_admin_list_category_filter_and_pagination(admin_client, db_factory):
    base = datetime.now(timezone.utc)
    bug_ids = []
    for i in range(5):
        bug_ids.append(
            await seed_feedback(
                db_factory, category="bug", content=f"bug-{i}",
                created_at=base - timedelta(hours=i),   # id 越小越新
            )
        )
    for i in range(3):
        await seed_feedback(db_factory, category="feature", content=f"feat-{i}",
                            created_at=base - timedelta(hours=10 + i))
    # 给第一条 bug 挂 2 个附件，验证 attachment_count 批量回填无误计
    await seed_attachment(db_factory, bug_ids[0], filename="x1.png")
    await seed_attachment(db_factory, bug_ids[0], filename="x2.png")

    async with admin_client as c:
        p1 = (await c.get("/api/admin/opinions", params={"category": "bug", "page": 1, "page_size": 2})).json()
        p2 = (await c.get("/api/admin/opinions", params={"category": "bug", "page": 2, "page_size": 2})).json()

    assert p1["total"] == 5 and p2["total"] == 5
    assert [it["id"] for it in p1["items"]] == bug_ids[:2]      # created_at DESC, id DESC
    assert [it["id"] for it in p2["items"]] == bug_ids[2:4]
    assert {it["id"] for it in p1["items"]}.isdisjoint({it["id"] for it in p2["items"]})
    assert all(it["category"] == "bug" for it in p1["items"] + p2["items"])
    count_by_id = {it["id"]: it["attachment_count"] for it in p1["items"]}
    assert count_by_id[bug_ids[0]] == 2 and count_by_id[bug_ids[1]] == 0


async def test_18b_admin_list_status_multi_and_search_escaping(admin_client, db_factory):
    await seed_feedback(db_factory, category="bug", status="open", content="进度条卡在 100% 不动")
    await seed_feedback(db_factory, category="bug", status="planned", content="另一个问题", name="100%")
    await seed_feedback(db_factory, category="bug", status="resolved", content="已修复的")

    async with admin_client as c:
        # 状态组内 OR：open + planned 命中 2 条，resolved 排除
        combo = (await c.get("/api/admin/opinions", params={"status": "open,planned"})).json()
        assert combo["total"] == 2
        # q 命中 content（"100%" 被转义，不会当成通配符匹配全表）
        by_content = (await c.get("/api/admin/opinions", params={"q": "100%"})).json()
        assert by_content["total"] == 2   # content 命中 1 + name 命中 1
        # q 命中 name 快照
        by_name = (await c.get("/api/admin/opinions", params={"q": "100%", "status": "planned"})).json()
        assert by_name["total"] == 1
        # 通配符转义：搜 "%" 不应返回全表（3 条里只有含字面 % 的命中）
        wildcard = (await c.get("/api/admin/opinions", params={"q": "%"})).json()
        assert wildcard["total"] == 2
        # 非法状态 → 422 而非静默忽略
        assert (await c.get("/api/admin/opinions", params={"status": "open,bogus"})).status_code == 422


async def test_19_admin_stats_aggregation(admin_client, db_factory):
    now = datetime.now(timezone.utc)
    # bug：1 条 8 天前未解决（计入 unresolved_7d）、1 条新的未解决、1 条已解决
    await seed_feedback(db_factory, category="bug", status="open", created_at=now - timedelta(days=8))
    await seed_feedback(db_factory, category="bug", status="open", created_at=now)
    await seed_feedback(db_factory, category="bug", status="resolved", created_at=now - timedelta(days=8))
    # feature：2 条，其中 1 条已转心愿 → conversion_rate = 0.5
    await seed_feedback(db_factory, category="feature", status="evaluating", linked_wish_id=999)
    await seed_feedback(db_factory, category="feature", status="open")

    async with admin_client as c:
        s = (await c.get("/api/admin/opinions/stats")).json()

    assert s["bug"]["total"] == 3
    assert sum(s["bug"]["by_status"].values()) == s["bug"]["total"]
    assert s["bug"]["unresolved_7d"] == 1          # 仅 8 天前且 open 的 bug
    assert s["feature"]["total"] == 2
    assert s["feature"]["linked_to_wish"] == 1
    assert s["feature"]["conversion_rate"] == 0.5


async def test_19b_admin_stats_zero_feature_no_division(admin_client, db_factory):
    await seed_feedback(db_factory, category="bug", status="open")
    async with admin_client as c:
        s = (await c.get("/api/admin/opinions/stats")).json()
    assert s["feature"]["total"] == 0
    assert s["feature"]["conversion_rate"] == 0.0   # 不除零、不 NaN


async def test_20_admin_patch_status_and_category_orthogonal(admin_client, db_factory):
    fid = await seed_feedback(db_factory, category="bug", status="open")
    async with admin_client as c:
        # 改 status
        r1 = await c.patch(f"/api/admin/opinions/{fid}", json={"status": "resolved"})
        assert r1.status_code == 200 and r1.json() == {"id": fid, "status": "resolved", "category": "bug"}
        # 改 category 不联动 status、不建心愿（正交，§5.1）
        r2 = await c.patch(f"/api/admin/opinions/{fid}", json={"category": "feature"})
        assert r2.json() == {"id": fid, "status": "resolved", "category": "feature"}
        # 空 body → 422
        assert (await c.patch(f"/api/admin/opinions/{fid}", json={})).status_code == 422
        # 非法枚举 → 422
        assert (await c.patch(f"/api/admin/opinions/{fid}", json={"status": "bogus"})).status_code == 422
        # 不存在 → 404
        assert (await c.patch("/api/admin/opinions/99999", json={"status": "open"})).status_code == 404

    (row,) = await feedback_rows(db_factory)
    assert (row.status, row.category, row.linked_wish_id) == ("resolved", "feature", None)
    assert await wish_rows(db_factory) == []


# ==================================================================
#  转心愿双通道（#21 ~ #25）+ 附件端点（#26 ~ #27）
# ==================================================================
async def test_21_admin_to_wish_links_feedback(admin_client, db_factory):
    fid = await seed_feedback(db_factory, category="feature", status="open")
    async with admin_client as c:
        r = await c.post(f"/api/admin/opinions/{fid}/to-wish",
                         json={"title": "支持导出 PDF", "description": "详情", "type": "ux"})
    assert r.status_code == 201
    body = r.json()
    assert body["feedback_id"] == fid and body["status"] == "evaluating"
    assert body["wish_id"] == body["linked_wish_id"]

    (wish,) = await wish_rows(db_factory)
    assert wish.author_id == "admin1"          # 管理员通道：作者记为操作者本人
    assert wish.title == "支持导出 PDF" and wish.type == "ux"
    (fb,) = await feedback_rows(db_factory)
    assert fb.linked_wish_id == wish.id and fb.status == "evaluating"


async def test_22_admin_to_wish_idempotent(admin_client, db_factory):
    fid = await seed_feedback(db_factory, category="feature")
    async with admin_client as c:
        first = await c.post(f"/api/admin/opinions/{fid}/to-wish", json={"title": "T"})
        second = await c.post(f"/api/admin/opinions/{fid}/to-wish", json={"title": "T"})
    assert first.status_code == 201
    assert second.status_code == 409 and "已转化" in second.json()["detail"]
    assert len(await wish_rows(db_factory)) == 1   # 心愿表只多 1 行（条件 UPDATE + rowcount）


async def test_23_user_self_service_to_wish(app_client_factory, db_factory):
    fid = await seed_feedback(db_factory, user_id="u1", category="feature")
    user = app_client_factory([wishes.router], user_id="u1", username="alice")
    async with user as c:
        r = await c.post("/api/wishes", json={"title": "我的心愿", "type": "model", "source_feedback_id": fid})
    assert r.status_code == 201
    assert r.json()["linked_feedback_id"] == fid
    (wish,) = await wish_rows(db_factory)
    assert wish.author_id == "u1"              # 自助通道：作者为提交者本人
    (fb,) = await feedback_rows(db_factory)
    assert fb.linked_wish_id == wish.id and fb.status == "evaluating"


async def test_24_self_service_four_validations(app_client_factory, db_factory):
    # 归属校验先于分类校验（403 优先）：每条校验数据播种给对应的发起用户。
    own_feature = await seed_feedback(db_factory, user_id="u1", category="feature")
    other_feature = await seed_feedback(db_factory, user_id="u2", category="feature")
    uc_bug = await seed_feedback(db_factory, user_id="uc", category="bug")
    ud_linked = await seed_feedback(db_factory, user_id="ud", category="feature", linked_wish_id=555)

    # 发布限流 3 次/60s 按 user_id 计：四类校验各用独立用户，避免撞限流
    # （限流本身由 wishes 侧 test_03 覆盖）。
    async def post_as(uid, payload):
        client = app_client_factory([wishes.router], user_id=uid, username=uid)
        async with client as c:
            return await c.post("/api/wishes", json=payload)

    # 反馈不存在 → 404
    r = await post_as("ua", {"title": "T", "source_feedback_id": 999999})
    assert r.status_code == 404
    # 不属于当前用户 → 403
    r = await post_as("ub", {"title": "T", "source_feedback_id": other_feature})
    assert r.status_code == 403
    # category == bug → 422
    r = await post_as("uc", {"title": "T", "source_feedback_id": uc_bug})
    assert r.status_code == 422
    # 已转化 → 409
    r = await post_as("ud", {"title": "T", "source_feedback_id": ud_linked})
    assert r.status_code == 409
    # 正常路径仍可成功（前面失败均未污染）
    r = await post_as("u1", {"title": "T", "source_feedback_id": own_feature})
    assert r.status_code == 201
    assert len(await wish_rows(db_factory)) == 1


async def test_25_dual_channel_race_exactly_one_wins(app_client_factory, db_factory):
    """管理员 to-wish 与用户自助并发 → 恰好一方 201、另一方 409，反馈只关联 1 个心愿。"""
    fid = await seed_feedback(db_factory, user_id="u1", category="feature")
    admin = app_client_factory([ofb.admin_router], user_id="admin1", username="root", role="admin")
    user = app_client_factory([wishes.router], user_id="u1", username="alice")

    async with admin as a, user as u:
        r_admin, r_user = await asyncio.gather(
            a.post(f"/api/admin/opinions/{fid}/to-wish", json={"title": "T", "type": "other"}),
            u.post("/api/wishes", json={"title": "T", "type": "other", "source_feedback_id": fid}),
        )

    assert sorted([r_admin.status_code, r_user.status_code]) == [201, 409]
    assert len(await wish_rows(db_factory)) == 1
    (fb,) = await feedback_rows(db_factory)
    assert fb.linked_wish_id is not None and fb.status == "evaluating"


async def test_26_attachment_file_missing_returns_404(admin_client, db_factory, attachment_dir, caplog):
    fid = await seed_feedback(db_factory)
    aid = await seed_attachment(db_factory, fid, filename="ghost.png")   # 元数据在，文件不在
    assert not (attachment_dir / "ghost.png").exists()
    with caplog.at_level(logging.WARNING, logger="app.routers.opinion_feedback"):
        async with admin_client as c:
            r = await c.get(f"/api/admin/opinions/attachments/{aid}")
    assert r.status_code == 404 and "文件不存在" in r.json()["detail"]
    assert "missing" in caplog.text          # 留痕便于运维定位卷未挂载


async def test_27_list_attachments_metadata_with_url(admin_client, db_factory):
    fid = await seed_feedback(db_factory)
    await seed_attachment(db_factory, fid, filename="m1.png")
    await seed_attachment(db_factory, fid, filename="m2.png")
    async with admin_client as c:
        r = await c.get(f"/api/admin/opinions/{fid}/attachments")
        empty = await c.get("/api/admin/opinions/99999/attachments")
    assert r.status_code == 200
    body = r.json()
    assert body["feedback_id"] == fid and len(body["items"]) == 2
    # 管理侧元数据带 url（指向仅管理员可读的字节端点）
    assert all(it["url"] == f"/api/admin/opinions/attachments/{it['id']}" for it in body["items"])
    assert empty.status_code == 404          # 反馈不存在 → 404（非空数组）
