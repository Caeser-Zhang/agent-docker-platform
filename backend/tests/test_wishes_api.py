"""心愿墙路由契约（设计文档 §8.2）。

锁死的核心性质：
  * 发布 / 助力收藏 toggle / 搜索过滤排序 —— 计数永不为负，scope 过滤不被
    JOIN 放大，``%``/``_`` 通配符转义。
  * **权限矩阵**（用户强调点，§5.2）：作者可改 title/description/type，
    ``status`` 仅管理员可改（403「仅管理员可修改心愿状态」）；非作者非管理员
    不可编辑；软删除 / 恢复是 ``Depends(require_admin)`` 的管理员专属端点；
    ``include_hidden`` 与 ``author_name`` / ``deleted_at`` 按角色收敛——
    普通用户即使显式传 ``include_hidden=true`` 也拿不到软删记录。
  * 软删除不删 wish_actions 行：恢复后计数与 my_boosted 状态保持一致（D24）。

并发助力用例（#8/#9）依赖 ``with_for_update()`` 行锁，在 SQLite 上退化为
no-op 会假绿，按 §10 标记 skip，留待 PostgreSQL 上单独跑。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401  (在 Base.metadata 上注册表)
from app.models import User, Wish, WishAction
from app.routers import wishes as ws


# ------------------------------------------------------------------
#  fixtures
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated(db_factory, monkeypatch):
    """每个用例独立库 + 清空模块级限流单例（跨测试会串味，§8.3）。"""
    monkeypatch.setattr(ws, "async_session", db_factory)
    ws._wish_create_limiter._hits.clear()
    ws._wish_action_limiter._hits.clear()


@pytest.fixture
def user_client(app_client_factory):
    return app_client_factory([ws.router], user_id="u1", username="alice")


@pytest.fixture
def admin_client(app_client_factory):
    return app_client_factory(
        [ws.router], user_id="admin1", username="root", role="admin"
    )


# ------------------------------------------------------------------
#  helpers
# ------------------------------------------------------------------
async def seed_user(db_factory, *, id="u1", username="alice", role="user"):
    """播种 users 行（author_name LEFT JOIN 用例需要；hashed_password 必填）。"""
    async with db_factory() as db:
        db.add(
            User(id=id, username=username, hashed_password="x", role=role)
        )
        await db.commit()


async def seed_wish(
    db_factory,
    *,
    author_id="u1",
    title="支持导出为 PDF",
    description="",
    type="other",
    status="evaluating",
    boost_count=0,
    favorite_count=0,
    deleted_at=None,
    created_at=None,
) -> int:
    async with db_factory() as db:
        w = Wish(
            author_id=author_id,
            title=title,
            description=description,
            type=type,
            status=status,
            boost_count=boost_count,
            favorite_count=favorite_count,
            deleted_at=deleted_at,
        )
        if created_at is not None:
            w.created_at = created_at
        db.add(w)
        await db.commit()
        await db.refresh(w)
        return w.id


async def seed_action(db_factory, wish_id, user_id, action):
    async with db_factory() as db:
        db.add(WishAction(wish_id=wish_id, user_id=user_id, action=action))
        await db.commit()


async def wish_rows(db_factory) -> list[Wish]:
    async with db_factory() as db:
        return list((await db.execute(select(Wish))).scalars().all())


async def action_rows(db_factory) -> list[WishAction]:
    async with db_factory() as db:
        return list((await db.execute(select(WishAction))).scalars().all())


def ids(resp) -> list[int]:
    return [it["id"] for it in resp.json()["items"]]


# ------------------------------------------------------------------
#  #1 发布
# ------------------------------------------------------------------
async def test_01_create_wish_defaults(user_client):
    async with user_client as c:
        r = await c.post(
            "/api/wishes",
            json={"title": "希望支持深色模式", "description": "晚上太刺眼", "type": "ux"},
        )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "evaluating"
    assert body["boost_count"] == 0 and body["favorite_count"] == 0
    assert body["is_mine"] is True
    assert body["my_boosted"] is False and body["my_favorited"] is False
    # 普通用户视角：管理员专属字段为空。
    assert body["author_name"] is None
    assert body["deleted_at"] is None
    assert body["linked_feedback_id"] is None


async def test_02_invalid_title_rejected(user_client, db_factory):
    async with user_client as c:
        r_blank = await c.post("/api/wishes", json={"title": "   "})
        r_long = await c.post("/api/wishes", json={"title": "长" * 121})
        r_type = await c.post("/api/wishes", json={"title": "ok", "type": "hack"})
    assert r_blank.status_code == 422
    assert r_long.status_code == 422
    assert r_type.status_code == 422
    assert len(await wish_rows(db_factory)) == 0


# ------------------------------------------------------------------
#  #3 发布限流（3 次/60s，第 4 次 429）
# ------------------------------------------------------------------
async def test_03_create_rate_limit_fourth_rejected(user_client, db_factory):
    async with user_client as c:
        codes = [
            (await c.post("/api/wishes", json={"title": f"心愿 {i}"})).status_code
            for i in range(4)
        ]
    assert codes == [201, 201, 201, 429]
    assert len(await wish_rows(db_factory)) == 3


# ------------------------------------------------------------------
#  #4~#6 助力 / 收藏 toggle
# ------------------------------------------------------------------
async def test_04_boost_toggle_on(user_client, db_factory):
    wid = await seed_wish(db_factory, author_id="other")
    async with user_client as c:
        r = await c.post(f"/api/wishes/{wid}/actions", json={"action": "boost"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "wish_id": wid,
        "action": "boost",
        "active": True,
        "boost_count": 1,
        "favorite_count": 0,
    }


async def test_05_boost_toggle_off_never_negative(user_client, db_factory):
    wid = await seed_wish(db_factory, author_id="other")
    async with user_client as c:
        await c.post(f"/api/wishes/{wid}/actions", json={"action": "boost"})
        r_off = await c.post(f"/api/wishes/{wid}/actions", json={"action": "boost"})
        # 计数已是 0 时再 toggle（开→关→开→关的最后一次关）也不为负。
        await c.post(f"/api/wishes/{wid}/actions", json={"action": "boost"})
        r_again = await c.post(f"/api/wishes/{wid}/actions", json={"action": "boost"})
    assert r_off.json()["active"] is False and r_off.json()["boost_count"] == 0
    assert r_again.json()["boost_count"] == 0
    row = (await wish_rows(db_factory))[0]
    assert row.boost_count == 0
    assert len(await action_rows(db_factory)) == 0


async def test_06_boost_and_favorite_independent(user_client, db_factory):
    wid = await seed_wish(db_factory, author_id="other")
    async with user_client as c:
        r_boost = await c.post(f"/api/wishes/{wid}/actions", json={"action": "boost"})
        r_fav = await c.post(f"/api/wishes/{wid}/actions", json={"action": "favorite"})
        r_fav_off = await c.post(f"/api/wishes/{wid}/actions", json={"action": "favorite"})
    assert r_boost.json()["boost_count"] == 1 and r_boost.json()["favorite_count"] == 0
    assert r_fav.json()["boost_count"] == 1 and r_fav.json()["favorite_count"] == 1
    # 取消收藏不影响助力。
    assert r_fav_off.json()["favorite_count"] == 0 and r_fav_off.json()["boost_count"] == 1
    row = (await wish_rows(db_factory))[0]
    assert (row.boost_count, row.favorite_count) == (1, 0)


async def test_06b_action_on_missing_or_invalid(user_client, db_factory):
    wid = await seed_wish(db_factory)
    async with user_client as c:
        r_404 = await c.post("/api/wishes/9999/actions", json={"action": "boost"})
        r_bad = await c.post(f"/api/wishes/{wid}/actions", json={"action": "hack"})
    assert r_404.status_code == 404
    assert r_bad.status_code == 422


# ------------------------------------------------------------------
#  #7 唯一约束（绕过 API 直接插重复行）
# ------------------------------------------------------------------
async def test_07_unique_constraint_blocks_duplicate_action(db_factory):
    wid = await seed_wish(db_factory)
    await seed_action(db_factory, wid, "u1", "boost")
    with pytest.raises(IntegrityError):
        await seed_action(db_factory, wid, "u1", "boost")


# ------------------------------------------------------------------
#  #8/#9 并发助力 —— 依赖行锁，SQLite 上假绿，按 §10 跳过
# ------------------------------------------------------------------
@pytest.mark.skip(reason="with_for_update() 在 SQLite 上是 no-op，需在 PostgreSQL 上跑（§10）")
async def test_08_concurrent_boost_same_user_single_row():
    pass


@pytest.mark.skip(reason="with_for_update() 在 SQLite 上是 no-op，需在 PostgreSQL 上跑（§10）")
async def test_09_concurrent_boost_ten_users_count_ten():
    pass


# ------------------------------------------------------------------
#  #10 搜索（命中 title or description；通配符转义）
# ------------------------------------------------------------------
async def test_10_search_escapes_wildcards(user_client, db_factory):
    await seed_wish(db_factory, title="支持导出 PDF", description="")
    await seed_wish(db_factory, title="深色模式", description="导出配置到文件")
    await seed_wish(db_factory, title="完成度 100% 的看板", description="")
    await seed_wish(db_factory, title="下划线_测试", description="")
    async with user_client as c:
        r_hit = await c.get("/api/wishes", params={"q": "导出"})
        r_pct = await c.get("/api/wishes", params={"q": "100%"})
        r_us = await c.get("/api/wishes", params={"q": "_"})
        r_none = await c.get("/api/wishes", params={"q": "不存在的关键词"})
    assert r_hit.json()["total"] == 2  # 标题命中 + 描述命中
    assert ids(r_pct) and all("100%" in it["title"] for it in r_pct.json()["items"])
    assert r_pct.json()["total"] == 1  # 搜「100%」不返回全表
    assert r_us.json()["total"] == 1  # 搜「_」不返回全表
    assert r_none.json()["total"] == 0


# ------------------------------------------------------------------
#  #11 状态多选（组内 OR）
# ------------------------------------------------------------------
async def test_11_status_multi_select_or(user_client, db_factory):
    await seed_wish(db_factory, title="a", status="evaluating")
    await seed_wish(db_factory, title="b", status="planned")
    await seed_wish(db_factory, title="c", status="developing")
    await seed_wish(db_factory, title="d", status="done")
    async with user_client as c:
        r = await c.get("/api/wishes", params={"status": "planned,developing"})
        r_bad = await c.get("/api/wishes", params={"status": "planned,hack"})
    assert sorted(ids(r)) == sorted([2, 3])
    assert r.json()["total"] == 2
    assert r_bad.status_code == 422  # 含非法值不静默忽略


# ------------------------------------------------------------------
#  #12 scope 过滤（结果正确且 total 不被 JOIN 放大）
# ------------------------------------------------------------------
async def test_12_scope_filters(user_client, db_factory):
    w1 = await seed_wish(db_factory, author_id="u1", title="我发的")
    w2 = await seed_wish(db_factory, author_id="u2", title="别人发的")
    w3 = await seed_wish(db_factory, author_id="u3", title="第三条")
    # u1 助力 w2/w3，收藏 w3。
    await seed_action(db_factory, w2, "u1", "boost")
    await seed_action(db_factory, w3, "u1", "boost")
    await seed_action(db_factory, w3, "u1", "favorite")
    async with user_client as c:
        r_mine = await c.get("/api/wishes", params={"scope": "mine"})
        r_boost = await c.get("/api/wishes", params={"scope": "boosted"})
        r_fav = await c.get("/api/wishes", params={"scope": "favorited"})
        r_all = await c.get("/api/wishes")
        r_bad = await c.get("/api/wishes", params={"scope": "hack"})
    assert ids(r_mine) == [w1] and r_mine.json()["total"] == 1
    # 两条 action 行不放大 total：boosted 恰好 2 条。
    assert sorted(ids(r_boost)) == sorted([w2, w3]) and r_boost.json()["total"] == 2
    assert ids(r_fav) == [w3] and r_fav.json()["total"] == 1
    assert r_all.json()["total"] == 3
    assert r_bad.status_code == 422
    # my_boosted / my_favorited 回填正确。
    items = {it["id"]: it for it in r_all.json()["items"]}
    assert items[w1]["my_boosted"] is False and items[w1]["is_mine"] is True
    assert items[w2]["my_boosted"] is True and items[w2]["is_mine"] is False
    assert items[w3]["my_boosted"] is True and items[w3]["my_favorited"] is True


# ------------------------------------------------------------------
#  #13 scope + status + q 组合（AND 语义）
# ------------------------------------------------------------------
async def test_13_combined_filters_are_and(user_client, db_factory):
    await seed_wish(db_factory, author_id="u1", title="导出 PDF", status="planned")
    await seed_wish(db_factory, author_id="u1", title="导出 Excel", status="evaluating")
    await seed_wish(db_factory, author_id="u2", title="导出图片", status="planned")
    async with user_client as c:
        r = await c.get(
            "/api/wishes",
            params={"scope": "mine", "status": "planned", "q": "导出"},
        )
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["title"] == "导出 PDF"


# ------------------------------------------------------------------
#  #14 六种排序（同值以 id DESC 稳定收尾）+ 分页
# ------------------------------------------------------------------
async def test_14_sorts_and_pagination(user_client, db_factory):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    wa = await seed_wish(db_factory, title="A", boost_count=5, favorite_count=1,
                         created_at=base)
    wb = await seed_wish(db_factory, title="B", boost_count=5, favorite_count=9,
                         created_at=base + timedelta(days=1))
    wc = await seed_wish(db_factory, title="C", boost_count=1, favorite_count=5,
                         created_at=base + timedelta(days=2))
    async with user_client as c:
        r_bd = await c.get("/api/wishes", params={"sort": "boost_desc"})
        r_ba = await c.get("/api/wishes", params={"sort": "boost_asc"})
        r_fd = await c.get("/api/wishes", params={"sort": "favorite_desc"})
        r_fa = await c.get("/api/wishes", params={"sort": "favorite_asc"})
        r_cd = await c.get("/api/wishes", params={"sort": "created_desc"})
        r_ca = await c.get("/api/wishes", params={"sort": "created_asc"})
        r_bad = await c.get("/api/wishes", params={"sort": "hack"})
        r_p1 = await c.get("/api/wishes", params={"sort": "boost_desc", "page": 1, "page_size": 2})
        r_p2 = await c.get("/api/wishes", params={"sort": "boost_desc", "page": 2, "page_size": 2})
    # boost 同值（A=5, B=5）→ id DESC：B 在前。
    assert ids(r_bd) == [wb, wa, wc]
    assert ids(r_ba) == [wc, wb, wa]
    assert ids(r_fd) == [wb, wc, wa]
    assert ids(r_fa) == [wa, wc, wb]
    assert ids(r_cd) == [wc, wb, wa]
    assert ids(r_ca) == [wa, wb, wc]
    assert r_bad.status_code == 422
    # 分页：第 1、2 页无交集，total 一致。
    assert ids(r_p1) == [wb, wa] and ids(r_p2) == [wc]
    assert r_p1.json()["total"] == r_p2.json()["total"] == 3
    assert r_p1.json()["page"] == 1 and r_p1.json()["page_size"] == 2


# ------------------------------------------------------------------
#  #15/#16 编辑权限矩阵（用户强调点）
# ------------------------------------------------------------------
async def test_15_author_edits_content_but_not_status(user_client, db_factory):
    wid = await seed_wish(db_factory, author_id="u1", title="旧标题")
    async with user_client as c:
        r_ok = await c.patch(
            f"/api/wishes/{wid}",
            json={"title": "新标题", "description": "新描述", "type": "model"},
        )
        r_status = await c.patch(f"/api/wishes/{wid}", json={"status": "done"})
        r_empty = await c.patch(f"/api/wishes/{wid}", json={})
        r_404 = await c.patch("/api/wishes/9999", json={"title": "x"})
    assert r_ok.status_code == 200
    assert r_ok.json()["title"] == "新标题"
    assert r_ok.json()["description"] == "新描述"
    assert r_ok.json()["type"] == "model"
    assert r_ok.json()["is_mine"] is True
    # 作者改状态 → 403（管理员才可推进心愿生命周期）。
    assert r_status.status_code == 403
    assert "仅管理员" in r_status.json()["detail"]
    assert r_empty.status_code == 422
    assert r_404.status_code == 404
    row = (await wish_rows(db_factory))[0]
    assert row.status == "evaluating"  # 403 的修改没有落库


async def test_15b_non_author_non_admin_cannot_edit(user_client, db_factory):
    wid = await seed_wish(db_factory, author_id="someone-else", title="别人的心愿")
    async with user_client as c:
        r = await c.patch(f"/api/wishes/{wid}", json={"title": "篡改"})
    assert r.status_code == 403
    assert "无权编辑" in r.json()["detail"]
    assert (await wish_rows(db_factory))[0].title == "别人的心愿"


async def test_16_admin_updates_status_and_any_wish(admin_client, db_factory):
    wid = await seed_wish(db_factory, author_id="u1", title="用户的心愿")
    async with admin_client as c:
        r_status = await c.patch(f"/api/wishes/{wid}", json={"status": "developing"})
        r_title = await c.patch(f"/api/wishes/{wid}", json={"title": "管理员代改"})
        r_bad = await c.patch(f"/api/wishes/{wid}", json={"status": "hack"})
    assert r_status.status_code == 200
    assert r_status.json()["status"] == "developing"
    assert r_status.json()["is_mine"] is False
    # 管理员视角：author_name 回填（users 无该行 → LEFT JOIN 为 null，前端显示「已注销用户」）。
    assert "author_name" in r_status.json()
    assert r_title.status_code == 200 and r_title.json()["title"] == "管理员代改"
    assert r_bad.status_code == 422
    row = (await wish_rows(db_factory))[0]
    assert (row.status, row.title) == ("developing", "管理员代改")


async def test_16b_author_name_visible_only_to_admin(app_client_factory, db_factory):
    """author_name 仅管理员返回；作者改名后跟随当前用户名（§6.4）。"""
    await seed_user(db_factory, id="u1", username="alice")
    wid = await seed_wish(db_factory, author_id="u1", title="心愿")
    admin = app_client_factory([ws.router], user_id="admin1", username="root", role="admin")
    user = app_client_factory([ws.router], user_id="u2", username="bob")
    async with admin as a, user as u:
        r_admin = await a.get("/api/wishes")
        r_user = await u.get("/api/wishes")
    item_admin = next(it for it in r_admin.json()["items"] if it["id"] == wid)
    item_user = next(it for it in r_user.json()["items"] if it["id"] == wid)
    assert item_admin["author_name"] == "alice"
    assert item_user["author_name"] is None  # 服务端是最终防线，不只是前端不渲染


# ------------------------------------------------------------------
#  #17 软删除 + 恢复（管理员专属；权限收敛）
# ------------------------------------------------------------------
async def test_17_soft_delete_restore_and_visibility(
    app_client_factory, user_client, db_factory
):
    wid = await seed_wish(db_factory, author_id="u2", title="待删心愿")
    await seed_action(db_factory, wid, "u1", "boost")
    async with db_factory() as db:  # 计数列与 action 行保持一致
        w = (await db.execute(select(Wish).where(Wish.id == wid))).scalar_one()
        w.boost_count = 1
        await db.commit()

    admin = app_client_factory([ws.router], user_id="admin1", username="root", role="admin")
    async with user_client as u, admin as a:
        # 管理员软删除 → 普通列表立即不可见。
        r_del = await a.delete(f"/api/wishes/{wid}")
        assert r_del.status_code == 200 and r_del.json()["deleted"] is True
        r_user_hidden = await u.get("/api/wishes")
        assert r_user_hidden.json()["total"] == 0
        # 普通用户显式 include_hidden=true 也看不到（角色收敛）。
        r_user_force = await u.get("/api/wishes", params={"include_hidden": "true"})
        assert r_user_force.json()["total"] == 0
        # 管理员 include_hidden=true 可见，且 deleted_at 非空。
        r_admin = await a.get("/api/wishes", params={"include_hidden": "true"})
        assert ids(r_admin) == [wid]
        assert r_admin.json()["items"][0]["deleted_at"] is not None
        # 管理员默认列表（不带 include_hidden）同样看不到软删记录。
        r_admin_default = await a.get("/api/wishes")
        assert r_admin_default.json()["total"] == 0
        # toggle 已删心愿 → 404。
        r_toggle = await u.post(f"/api/wishes/{wid}/actions", json={"action": "favorite"})
        assert r_toggle.status_code == 404
        # 编辑已删心愿 → 404（_alive() 收敛）。
        r_patch = await a.patch(f"/api/wishes/{wid}", json={"title": "x"})
        assert r_patch.status_code == 404
        # 重复删除 → 404（不静默成功）。
        r_del2 = await a.delete(f"/api/wishes/{wid}")
        assert r_del2.status_code == 404
        # 恢复 → 普通列表可见，计数与 my_boosted 状态保持（不删 action 行）。
        r_res = await a.post(f"/api/wishes/{wid}/restore")
        assert r_res.status_code == 200 and r_res.json()["deleted"] is False
        r_after = await u.get("/api/wishes")
        assert ids(r_after) == [wid]
        item = r_after.json()["items"][0]
        assert item["boost_count"] == 1 and item["my_boosted"] is True
        # 恢复未删除的心愿 → 404。
        r_res2 = await a.post(f"/api/wishes/{wid}/restore")
        assert r_res2.status_code == 404
    assert len(await action_rows(db_factory)) == 1  # action 行未随软删丢失


async def test_17b_delete_restore_forbidden_for_normal_user(user_client, db_factory):
    """软删除 / 恢复是 require_admin 端点：普通用户一律 403。"""
    wid = await seed_wish(db_factory, author_id="u1", title="自己的心愿")
    async with user_client as c:
        r_del = await c.delete(f"/api/wishes/{wid}")
        r_res = await c.post(f"/api/wishes/{wid}/restore")
    assert r_del.status_code == 403
    assert r_res.status_code == 403
    assert (await wish_rows(db_factory))[0].deleted_at is None


# ------------------------------------------------------------------
#  统计条（只计未删除；my_* 按当前用户）
# ------------------------------------------------------------------
async def test_18_stats_counts_alive_only(user_client, db_factory):
    await seed_wish(db_factory, author_id="u1", title="我的", status="evaluating")
    w2 = await seed_wish(db_factory, author_id="u2", title="别人的", status="planned")
    await seed_wish(db_factory, author_id="u2", title="已删的", status="done",
                    deleted_at=datetime.now(timezone.utc))
    await seed_action(db_factory, w2, "u1", "boost")
    await seed_action(db_factory, w2, "u1", "favorite")
    async with user_client as c:
        r = await c.get("/api/wishes/stats")
    body = r.json()
    assert r.status_code == 200
    assert body["total"] == 2  # 软删的不计
    assert body["by_status"] == {"evaluating": 1, "planned": 1, "developing": 0, "done": 0}
    assert body["mine"] == 1
    assert body["my_boosted"] == 1 and body["my_favorited"] == 1


# ------------------------------------------------------------------
#  未登录 → 401（get_current_user 未被覆盖的裸 app）
# ------------------------------------------------------------------
async def test_19_unauthenticated_requests_rejected():
    app = FastAPI()
    app.include_router(ws.router)  # 故意不 override get_current_user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/wishes")).status_code == 401
        assert (await c.post("/api/wishes", json={"title": "x"})).status_code == 401
        assert (await c.patch("/api/wishes/1", json={"title": "x"})).status_code == 401
        assert (await c.delete("/api/wishes/1")).status_code == 401
        assert (await c.post("/api/wishes/1/restore")).status_code == 401
        assert (await c.post("/api/wishes/1/actions", json={"action": "boost"})).status_code == 401
        assert (await c.get("/api/wishes/stats")).status_code == 401
