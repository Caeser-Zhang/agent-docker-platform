import { useCallback, useEffect, useRef, useState } from "react";
import { App as AntdApp, Button, Input, Pagination, Segmented, Select, Spin, Tag } from "antd";
import { HeartFilled, HeartOutlined, StarFilled, StarOutlined } from "@ant-design/icons";
import {
  deleteWish,
  getWishStats,
  listWishes,
  patchWish,
  restoreWish,
  toggleWishAction,
  type WishItem,
  type WishListResp,
  type WishScope,
  type WishSort,
  type WishStats,
  type WishStatus,
} from "../api";
import {
  WISH_SCOPE_OPTIONS,
  WISH_SORT_OPTIONS,
  WISH_STATUS_COLOR,
  WISH_STATUS_LABEL,
  WISH_STATUS_OPTIONS,
  WISH_TYPE_COLOR,
  WISH_TYPE_LABEL,
  wishStyles,
} from "../wishStyles";
import { WishFormModal } from "./WishFormModal";

/**
 * 心愿墙（设计文档 §7.3）。所有登录用户都能访问，不是管理员页面——
 * 因此 App.tsx 的渲染分支**不带** `role === "admin"` 守卫。
 *
 * 权限区分（前端只是收敛 UI，后端 §5.2 才是最终防线）：
 * - 普通用户：看 7 个字段、助力、收藏、发布心愿、编辑**自己的**心愿；
 * - 管理员：额外看作者姓名/创建日期、内联改状态、编辑任意心愿、隐藏/恢复。
 */

const PAGE_SIZE = 12;
/** 描述超过这个长度才显示「展开」（与已审核的原型一致，避免短描述也出现无意义的按钮）。 */
const CLAMP_THRESHOLD = 72;
/** 后台「查看心愿」跳入时逐页定位的上限（page_size 上限 100，覆盖 2000 条）。 */
const LOCATE_MAX_PAGE = 20;
const LOCATE_PAGE_SIZE = 100;

function errOf(e: unknown): Error & { status?: number } {
  return e as Error & { status?: number };
}

export function WishWall({
  role,
  focusWishId,
  onExit,
}: {
  role: string;
  /** 从后台「查看心愿」跳入时携带的目标 id（§7.4⑤）。 */
  focusWishId?: number | null;
  onExit: () => void;
}) {
  const { message, modal } = AntdApp.useApp();
  const isAdmin = role === "admin";

  // --- 过滤条件 -----------------------------------------------------------
  const [searchInput, setSearchInput] = useState("");
  const [q, setQ] = useState("");
  const [statuses, setStatuses] = useState<WishStatus[]>([]);
  const [scope, setScope] = useState<WishScope>("all");
  const [sort, setSort] = useState<WishSort>("boost_desc");

  // --- 列表数据 -----------------------------------------------------------
  const [items, setItems] = useState<WishItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<WishStats | null>(null);

  // --- 视图态 -------------------------------------------------------------
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [formOpen, setFormOpen] = useState(false);
  const [formMode, setFormMode] = useState<"create" | "edit">("create");
  const [formWish, setFormWish] = useState<WishItem | null>(null);

  /** 定位流程会自己负责首屏渲染，用它跳过 effect 的自动加载，避免两次请求互相覆盖。 */
  const skipLoadRef = useRef(false);
  const bootRef = useRef(false);

  const loadList = useCallback(
    async (p: number) => {
      setLoading(true);
      try {
        const res = await listWishes({
          q: q || undefined,
          status: statuses.length ? statuses : undefined,
          scope,
          sort,
          page: p,
          page_size: PAGE_SIZE,
          // 软删记录只有管理员能看到；服务端同样按角色收敛，前端多传也无害。
          include_hidden: isAdmin ? true : undefined,
        });
        setItems(res.items);
        setTotal(res.total);
        setPage(res.page);
      } catch (e) {
        message.error(errOf(e).message);
      } finally {
        setLoading(false);
      }
    },
    [q, statuses, scope, sort, isAdmin, message]
  );

  const loadStats = useCallback(async () => {
    // 统计条失败不打断列表：数字缺失比整页报错好。
    try {
      setStats(await getWishStats());
    } catch {
      setStats(null);
    }
  }, []);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  useEffect(() => {
    if (!bootRef.current) {
      bootRef.current = true;
      if (focusWishId) {
        skipLoadRef.current = true;
        return;
      }
    }
    if (skipLoadRef.current) {
      skipLoadRef.current = false;
      return;
    }
    loadList(1);
  }, [loadList, focusWishId]);

  // 搜索 300ms 防抖；回车 / 点击放大镜走 onSearch 立即触发。
  useEffect(() => {
    const t = setTimeout(() => setQ(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  // --- 后台「查看心愿」跳入：清过滤 + created_desc 定位 + 2 秒高亮 ----------
  useEffect(() => {
    if (!focusWishId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        let res: WishListResp | null = null;
        let found: WishItem | null = null;
        for (let p = 1; p <= LOCATE_MAX_PAGE; p += 1) {
          res = await listWishes({
            sort: "created_desc",
            page: p,
            page_size: LOCATE_PAGE_SIZE,
            include_hidden: isAdmin ? true : undefined,
          });
          if (cancelled) return;
          found = res.items.find((w) => w.id === focusWishId) ?? null;
          if (found || res.items.length < LOCATE_PAGE_SIZE) break;
        }
        if (cancelled) return;
        if (!found || !res) {
          message.warning("该心愿已不可见");
          skipLoadRef.current = false;
          loadList(1);
          return;
        }
        // 一次性重置过滤条件：同一批 setState 只会触发一次 loadList effect，
        // 由 skipLoadRef 吞掉，保留这里定位到的整页数据。
        skipLoadRef.current = true;
        setSearchInput("");
        setQ("");
        setStatuses([]);
        setScope("all");
        setSort("created_desc");
        setItems(res.items);
        setTotal(res.total);
        setPage(res.page);
        setHighlightId(focusWishId);
        requestAnimationFrame(() => {
          document
            .getElementById(`wish-${focusWishId}`)
            ?.scrollIntoView({ behavior: "smooth", block: "center" });
        });
        setTimeout(() => setHighlightId(null), 2000);
      } catch (e) {
        if (!cancelled) message.error(errOf(e).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // 只在挂载时执行一次：focusWishId 是进入页面时的一次性意图，不是响应式条件。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- 助力 / 收藏：乐观更新 + 失败回滚（§7.3） ---------------------------
  const onAction = async (w: WishItem, action: "boost" | "favorite") => {
    const lockKey = `${w.id}:${action}`;
    if (pending[lockKey]) return;
    const flag = action === "boost" ? "my_boosted" : "my_favorited";
    const count = action === "boost" ? "boost_count" : "favorite_count";
    const was = w[flag];
    setPending((p) => ({ ...p, [lockKey]: true }));
    setItems((list) =>
      list.map((it) =>
        it.id === w.id
          ? { ...it, [flag]: !was, [count]: Math.max(0, it[count] + (was ? -1 : 1)) }
          : it
      )
    );
    try {
      const res = await toggleWishAction(w.id, action);
      // 以服务端计数为准校正本地（并发点击时不会出现漂移）。
      setItems((list) =>
        list.map((it) =>
          it.id === w.id
            ? {
                ...it,
                boost_count: res.boost_count,
                favorite_count: res.favorite_count,
                [flag]: res.active,
              }
            : it
        )
      );
    } catch (e) {
      setItems((list) =>
        list.map((it) =>
          it.id === w.id
            ? { ...it, [flag]: was, [count]: Math.max(0, it[count] + (was ? 1 : -1)) }
            : it
        )
      );
      message.error(errOf(e).message || "操作失败，已恢复");
    } finally {
      setPending((p) => {
        const next = { ...p };
        delete next[lockKey];
        return next;
      });
    }
  };

  // --- 管理员专属：改状态 / 隐藏 / 恢复 -----------------------------------
  const onStatusChange = async (w: WishItem, next: WishStatus) => {
    if (next === w.status) return;
    const prev = w.status;
    setItems((list) => list.map((it) => (it.id === w.id ? { ...it, status: next } : it)));
    try {
      await patchWish(w.id, { status: next });
      message.success("状态已更新");
      loadStats();
    } catch (e) {
      const err = errOf(e);
      setItems((list) => list.map((it) => (it.id === w.id ? { ...it, status: prev } : it)));
      if (err.status === 403) message.warning(err.message || "无权修改心愿状态");
      else message.error(err.message || "修改失败，已恢复");
    }
  };

  const onHide = (w: WishItem) => {
    modal.confirm({
      title: "隐藏这条心愿？",
      content: `「${w.title}」将从普通用户的视野中移除，你可以随时恢复。`,
      okText: "隐藏",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await deleteWish(w.id);
          message.success("已隐藏");
          loadList(page);
          loadStats();
        } catch (e) {
          message.error(errOf(e).message || "隐藏失败");
        }
      },
    });
  };

  const onRestore = async (w: WishItem) => {
    try {
      await restoreWish(w.id);
      message.success("已恢复");
      loadList(page);
      loadStats();
    } catch (e) {
      message.error(errOf(e).message || "恢复失败");
    }
  };

  // --- 发布 / 编辑 --------------------------------------------------------
  const openCreate = () => {
    setFormMode("create");
    setFormWish(null);
    setFormOpen(true);
  };
  const openEdit = (w: WishItem) => {
    setFormMode("edit");
    setFormWish(w);
    setFormOpen(true);
  };

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div style={wishStyles.page}>
      <div style={wishStyles.header}>
        <div>
          <h1 style={wishStyles.title}>
            🌟 心愿墙
            {isAdmin && (
              <Tag color="blue" style={{ marginLeft: 8, verticalAlign: "middle" }}>
                管理员视图
              </Tag>
            )}
          </h1>
          <div style={wishStyles.subtitle}>
            {isAdmin
              ? "你可以改状态、编辑任意心愿、隐藏与恢复；普通用户只能助力、收藏和编辑自己的心愿"
              : "每一个愿望都值得被看见 · 助力让好想法更快落地"}
          </div>
        </div>
        <div style={wishStyles.headerSpacer} />
        <Button onClick={onExit}>← 返回对话</Button>
        <Button type="primary" onClick={openCreate}>
          发布心愿
        </Button>
      </div>

      {stats && (
        <div style={wishStyles.statsRow}>
          <div style={wishStyles.statCard}>
            <div style={wishStyles.statLabel}>全部心愿</div>
            <div style={wishStyles.statValue}>{stats.total}</div>
          </div>
          <div style={wishStyles.statCard}>
            <div style={wishStyles.statLabel}>评估中</div>
            <div style={wishStyles.statValue}>{stats.by_status?.evaluating ?? 0}</div>
          </div>
          <div style={wishStyles.statCard}>
            <div style={wishStyles.statLabel}>进行中</div>
            <div style={wishStyles.statValue}>
              {(stats.by_status?.planned ?? 0) + (stats.by_status?.developing ?? 0)}
            </div>
          </div>
          <div style={wishStyles.statCard}>
            <div style={wishStyles.statLabel}>已实现</div>
            <div style={wishStyles.statValue}>{stats.by_status?.done ?? 0}</div>
          </div>
          <div style={wishStyles.statCard}>
            <div style={wishStyles.statLabel}>我创建的</div>
            <div style={wishStyles.statValue}>{stats.mine}</div>
          </div>
          <div style={wishStyles.statCard}>
            <div style={wishStyles.statLabel}>我助力的</div>
            <div style={wishStyles.statValue}>{stats.my_boosted}</div>
          </div>
        </div>
      )}

      <div style={wishStyles.toolbar}>
        <Input.Search
          allowClear
          value={searchInput}
          placeholder="搜索心愿名称或描述"
          style={{ width: 260 }}
          onChange={(e) => setSearchInput(e.target.value)}
          onSearch={(v) => setQ(v.trim())}
        />
        <Select<WishStatus[]>
          mode="multiple"
          allowClear
          maxTagCount="responsive"
          value={statuses}
          options={WISH_STATUS_OPTIONS}
          placeholder="状态：全部"
          style={{ minWidth: 180 }}
          onChange={(v) => setStatuses(v)}
        />
        <Segmented
          value={scope}
          options={WISH_SCOPE_OPTIONS}
          onChange={(v) => setScope(v as WishScope)}
        />
        <Select<WishSort>
          value={sort}
          options={WISH_SORT_OPTIONS}
          style={{ width: 170 }}
          onChange={(v) => setSort(v)}
        />
      </div>

      <Spin spinning={loading}>
        {items.length === 0 && !loading ? (
          <div style={wishStyles.empty}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>没有符合条件的心愿</div>
            <div style={{ marginBottom: 12 }}>试试调整筛选条件，或成为第一个发布心愿的人</div>
            <Button type="primary" size="small" onClick={openCreate}>
              发布心愿
            </Button>
          </div>
        ) : (
          <div style={wishStyles.grid}>
            {items.map((w) => (
              <WishCard
                key={w.id}
                wish={w}
                isAdmin={isAdmin}
                expanded={!!expanded[w.id]}
                highlight={highlightId === w.id}
                pendingAction={pending[`${w.id}:boost`] ? "boost" : pending[`${w.id}:favorite`] ? "favorite" : null}
                onToggleExpand={() =>
                  setExpanded((m) => ({ ...m, [w.id]: !m[w.id] }))
                }
                onAction={onAction}
                onEdit={openEdit}
                onStatusChange={onStatusChange}
                onHide={onHide}
                onRestore={onRestore}
              />
            ))}
          </div>
        )}
      </Spin>

      {total > PAGE_SIZE && (
        <div style={wishStyles.pagerRow}>
          <Pagination
            current={page}
            pageSize={PAGE_SIZE}
            total={total}
            showSizeChanger={false}
            onChange={(p) => {
              if (p >= 1 && p <= pages) loadList(p);
            }}
          />
        </div>
      )}

      <WishFormModal
        open={formOpen}
        mode={formMode}
        wish={formWish}
        isAdmin={isAdmin}
        onCancel={() => setFormOpen(false)}
        onDone={() => {
          setFormOpen(false);
          loadList(formMode === "create" ? 1 : page);
          loadStats();
        }}
      />
    </div>
  );
}

/**
 * 单张心愿卡片。用户视角严格 7 个字段（§7.3 / D26）；
 * 管理员与作者的额外操作按 props 收敛，普通用户拿不到任何管理入口。
 */
function WishCard({
  wish: w,
  isAdmin,
  expanded,
  highlight,
  pendingAction,
  onToggleExpand,
  onAction,
  onEdit,
  onStatusChange,
  onHide,
  onRestore,
}: {
  wish: WishItem;
  isAdmin: boolean;
  expanded: boolean;
  highlight: boolean;
  pendingAction: "boost" | "favorite" | null;
  onToggleExpand: () => void;
  onAction: (w: WishItem, action: "boost" | "favorite") => void;
  onEdit: (w: WishItem) => void;
  onStatusChange: (w: WishItem, next: WishStatus) => void;
  onHide: (w: WishItem) => void;
  onRestore: (w: WishItem) => void;
}) {
  const hidden = !!w.deleted_at;
  const long = w.description.length > CLAMP_THRESHOLD;
  const style = {
    ...wishStyles.card,
    ...(hidden ? wishStyles.cardDeleted : null),
    ...(highlight ? wishStyles.highlight : null),
  };

  return (
    <article id={`wish-${w.id}`} style={style}>
      <div style={wishStyles.cardTitleRow}>
        <div style={wishStyles.cardTitle}>{w.title}</div>
        {hidden && <Tag color="red">已隐藏</Tag>}
      </div>

      <div style={wishStyles.tagRow}>
        <Tag color={WISH_TYPE_COLOR[w.type]}>{WISH_TYPE_LABEL[w.type]}</Tag>
        <Tag color={WISH_STATUS_COLOR[w.status]}>{WISH_STATUS_LABEL[w.status]}</Tag>
      </div>

      {w.description && (
        <>
          <div
            style={{
              ...wishStyles.desc,
              ...(long && !expanded ? wishStyles.descClamp : null),
            }}
          >
            {w.description}
          </div>
          {long && (
            <button type="button" style={wishStyles.expandLink} onClick={onToggleExpand}>
              {expanded ? "收起" : "展开"}
            </button>
          )}
        </>
      )}

      {/* 作者姓名 + 创建日期：管理员专属字段（普通用户的响应里就是 null）。 */}
      {isAdmin && (
        <div style={wishStyles.metaRow}>
          <span>👤 {w.author_name || "未知"}</span>
          <span>{w.created_at.slice(0, 10)}</span>
        </div>
      )}

      <div style={wishStyles.actionRow}>
        <button
          type="button"
          style={{
            ...wishStyles.actionBtn,
            ...(w.my_boosted ? wishStyles.actionBtnActive : null),
          }}
          disabled={!!pendingAction || hidden}
          aria-pressed={w.my_boosted}
          title={w.my_boosted ? "取消助力" : "为它助力"}
          onClick={() => onAction(w, "boost")}
        >
          {w.my_boosted ? <HeartFilled /> : <HeartOutlined />}
          {w.boost_count}
        </button>
        <button
          type="button"
          style={{
            ...wishStyles.actionBtn,
            ...(w.my_favorited ? wishStyles.actionBtnActive : null),
          }}
          disabled={!!pendingAction || hidden}
          aria-pressed={w.my_favorited}
          title={w.my_favorited ? "取消收藏" : "收藏"}
          onClick={() => onAction(w, "favorite")}
        >
          {w.my_favorited ? <StarFilled /> : <StarOutlined />}
          {w.favorite_count}
        </button>

        <div style={wishStyles.actionSpacer} />

        {isAdmin ? (
          <>
            {/* 内联改状态：只有管理员渲染这个 Select（D9）。 */}
            <Select<WishStatus>
              size="small"
              value={w.status}
              options={WISH_STATUS_OPTIONS}
              style={{ width: 100 }}
              onChange={(v) => onStatusChange(w, v)}
            />
            <button type="button" style={wishStyles.linkBtn} onClick={() => onEdit(w)}>
              编辑
            </button>
            {hidden ? (
              <button type="button" style={wishStyles.linkBtn} onClick={() => onRestore(w)}>
                恢复
              </button>
            ) : (
              <button type="button" style={wishStyles.dangerBtn} onClick={() => onHide(w)}>
                隐藏
              </button>
            )}
          </>
        ) : (
          // 作者本人才能编辑；后端对非作者非管理员的 PATCH 一律 403。
          w.is_mine && (
            <button type="button" style={wishStyles.linkBtn} onClick={() => onEdit(w)}>
              编辑
            </button>
          )
        )}
      </div>
    </article>
  );
}
