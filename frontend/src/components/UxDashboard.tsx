/**
 * UxDashboard — 用户体验指标看板（任务二），仅管理员可见。
 *
 * 挂在 AdminPanel 的「用户体验」Tab 下，数据全部来自 /api/admin/ux/*。
 * 四层指标（产品经理视角）：
 *   L1 结果层   —— 回合成功率 / 任务成功率 / 错误率
 *   L2 效率层   —— 回合耗时（均值 + p50/p90/p99）与成本
 *   L3 过程层   —— 工具调用准确率、Token 消耗效率
 *   L4 满意度层 —— 点赞 / 点踩（任务一）与点踩原因分布
 * 下方附回合明细、点赞点踩（含上下文快照）与用户反馈看板（§7.4）供下钻排障，
 * 以及历史指标回补入口。
 *
 * 图表配色取自主题 CSS 变量（切换主题时重新解析），保证暗/亮色下都可读。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  fetchOpinionAttachmentBlob,
  getOpinionStats,
  listOpinionAttachments,
  listOpinions,
  opinionToWish,
  patchOpinion,
  type FeedbackCategory,
  type OpinionAttachmentItem,
  type OpinionCategoryStats,
  type OpinionItem,
  type OpinionStatsResp,
  type OpinionStatus,
  type UxFeedback,
  type UxGranularity,
  type UxLlm,
  type UxOverview,
  type UxRounds,
  type UxToolCalls,
  type UxTools,
  type UxTrends,
  type UxUserActivity,
  type WishType,
} from "../api";
import { REASON_OPTIONS } from "../oc/feedback";
import { useTheme } from "../theme";
import { WISH_TYPE_OPTIONS } from "../wishStyles";
import { adminStyles as s } from "./adminStyles";

const PAGE = 20;
/** 时间维度粒度切换器：粒度自带默认统计范围。 */
const GRAN_OPTIONS: { v: UxGranularity; label: string; range: string }[] = [
  { v: "day", label: "日", range: "近 30 天" },
  { v: "week", label: "周", range: "近 12 周" },
  { v: "month", label: "月", range: "近 12 月" },
  { v: "year", label: "年", range: "近 5 年" },
];
/** 分桶键 → 图表 X 轴短标签（按粒度裁剪）。 */
const bucketLabel = (key: string, g: UxGranularity): string => {
  if (g === "year") return key; // 2026
  if (g === "month") return key.slice(2); // 26-09
  if (g === "week") return key.slice(5); // W38
  return key.slice(5); // 09-15
};
const REASON_LABEL: Record<string, string> = Object.fromEntries(
  REASON_OPTIONS.map((o) => [o.code, o.label])
);

// --- 用户反馈（意见反馈看板，§7.4）-----------------------------------------
/** 分类 Tab：默认 Bug —— 积压最需要被先看到（D34）。 */
type OpinionTab = FeedbackCategory | "all";
const OPINION_TABS: { v: OpinionTab; label: string }[] = [
  { v: "bug", label: "Bug" },
  { v: "feature", label: "功能特性" },
  { v: "all", label: "全部" },
];
const OPINION_STATUS_LABEL: Record<OpinionStatus, string> = {
  open: "未解决",
  resolved: "已解决",
  evaluating: "评估中",
  planned: "已规划",
  developing: "开发中",
};
/** 徽标与下拉的固定顺序（by_status 是无序字典）。 */
const OPINION_STATUS_ORDER: OpinionStatus[] = [
  "open",
  "resolved",
  "evaluating",
  "planned",
  "developing",
];
/** 内容列 clamp 阈值：短内容不给「展开」。 */
const CONTENT_CLAMP = 60;

/** `yyyy-MM-dd HH:mm`（反馈时间列，比 fmtTime 更紧凑）。 */
const fmtMinute = (iso: string | null): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes()
  )}`;
};

const fmtKB = (bytes: number): string =>
  bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)}MB`
    : `${Math.max(1, Math.round(bytes / 1024))}KB`;

/** 截图预览态：url 为 null 表示该张取字节失败（404 文件丢失）→ 渲染占位灰块。 */
interface PreviewItem {
  meta: OpinionAttachmentItem;
  url: string | null;
}

/** 已提交的过滤条件（草稿输入需点「应用」才生效，避免逐字触发请求）。 */
interface Filters {
  granularity: UxGranularity;
  days?: number;
  user_id?: string;
  model_provider?: string;
}

// --- 格式化 ---------------------------------------------------------------
const pct1 = (v: number | null | undefined): string =>
  v == null ? "—" : `${(v * 100).toFixed(1)}%`;
/** 图表用：0-1 → 百分数（null 保持 null，recharts 自动断线）。 */
const toPct = (v: number | null | undefined): number | null =>
  v == null ? null : Math.round(v * 1000) / 10;
const fmtMs = (v: number | null | undefined): string => {
  if (v == null) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
};
const fmtInt = (v: number | null | undefined): string =>
  v == null ? "—" : Math.round(v).toLocaleString("zh-CN");
const fmtCost = (v: number | null | undefined): string => `$${(v ?? 0).toFixed(4)}`;
const fmtTime = (iso: string | null): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
};
const clip = (text: string | null | undefined, n: number): string => {
  const t = (text ?? "").trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
};

/** 用户展示：name（工号）；缺失时回退到 user_id。 */
const userLabel = (
  name: string | null | undefined,
  uid: string | null | undefined,
  id: string | null | undefined
): string => {
  if (name && uid) return `${name}（${uid}）`;
  if (name) return name;
  if (uid) return `（${uid}）`;
  return id ?? "—";
};

/** 解析主题 CSS 变量为图表可用的实色（recharts 的 stroke/fill 不吃 var()）。 */
function useChartColors() {
  const { theme } = useTheme();
  return useMemo(() => {
    const cs = getComputedStyle(document.documentElement);
    const v = (name: string, fb: string) => cs.getPropertyValue(name).trim() || fb;
    return {
      green: v("--green", "#22c55e"),
      red: v("--red", "#ef4444"),
      amber: v("--amber", "#f59e0b"),
      indigo: v("--indigo", "#6366f1"),
      text: v("--text", "#e2e8f0"),
      text2: v("--text-2", "#94a3b8"),
      text3: v("--text-3", "#64748b"),
      border: v("--border", "#334155"),
      surface: v("--surface", "#1e293b"),
    };
  }, [theme]);
}

const grid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
  gap: "12px",
  marginBottom: "12px",
};

const panel: CSSProperties = {
  border: "1px solid var(--border)",
  background: "var(--surface)",
  borderRadius: "10px",
  padding: "12px 14px",
};

const panelTitle: CSSProperties = {
  fontSize: "12.5px",
  fontWeight: 600,
  color: "var(--text-2)",
  marginBottom: "8px",
  letterSpacing: "0.3px",
};

const sectionTitle: CSSProperties = {
  fontSize: "12px",
  fontWeight: 700,
  color: "var(--text-3)",
  margin: "18px 0 8px",
  letterSpacing: "0.6px",
};

function Card({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "green" | "red" | "amber";
}) {
  const valueStyle =
    tone === "green" ? s.cardValueGreen : tone === "red" ? s.cardValueRed : s.cardValue;
  return (
    <div className="adm-card" style={s.card}>
      <div style={s.cardLabel}>{label}</div>
      <div style={{ ...valueStyle, ...(tone === "amber" ? { color: "var(--amber)" } : {}) }}>
        {value}
      </div>
      {sub && <div style={s.cardSub}>{sub}</div>}
    </div>
  );
}

export function UxDashboard({
  onOpenWishes,
}: {
  /** 「查看心愿」→ 跳到心愿墙并定位高亮该条（App.tsx 提供，缺省时按钮仍渲染但无跳转）。 */
  onOpenWishes?: (wishId?: number | null) => void;
}) {
  const c = useChartColors();

  const [filters, setFilters] = useState<Filters>({ granularity: "day" });
  const [draftUser, setDraftUser] = useState("");
  const [draftProvider, setDraftProvider] = useState("");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [overview, setOverview] = useState<UxOverview | null>(null);
  const [trends, setTrends] = useState<UxTrends | null>(null);
  const [activity, setActivity] = useState<UxUserActivity | null>(null);
  const [tools, setTools] = useState<UxTools | null>(null);
  const [toolCalls, setToolCalls] = useState<UxToolCalls | null>(null);
  const [llm, setLlm] = useState<UxLlm | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  // --- 明细（回合 / 点赞点踩 / 用户反馈）------------------------------------
  const [detailTab, setDetailTab] = useState<"rounds" | "feedback" | "opinions">("rounds");
  const [onlyFailed, setOnlyFailed] = useState(false);
  const [verdict, setVerdict] = useState<"" | "up" | "down">("");
  const [page, setPage] = useState(0);
  const [detailLoading, setDetailLoading] = useState(false);
  const [rounds, setRounds] = useState<UxRounds | null>(null);
  const [feedback, setFeedback] = useState<UxFeedback | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // --- 历史回补 ------------------------------------------------------------
  const [bfUser, setBfUser] = useState("");
  const [bfSession, setBfSession] = useState("");
  const [bfBusy, setBfBusy] = useState(false);
  const [bfMsg, setBfMsg] = useState<string | null>(null);

  // --- 用户反馈（意见反馈看板，§7.4）--------------------------------------
  const [opTab, setOpTab] = useState<OpinionTab>("bug"); // 默认 Bug：积压最需要被先看到（D34）
  const [opStatuses, setOpStatuses] = useState<OpinionStatus[]>([]); // 空 = 不限
  const [opSearchDraft, setOpSearchDraft] = useState("");
  const [opSearch, setOpSearch] = useState(""); // 已提交的关键字（回车 / 点「搜索」才生效）
  const [opItems, setOpItems] = useState<OpinionItem[]>([]);
  const [opTotal, setOpTotal] = useState(0);
  const [opPage, setOpPage] = useState(1); // 后端 page 从 1 起（与回合明细的 offset 不同）
  const [opLoading, setOpLoading] = useState(false);
  const [opStats, setOpStats] = useState<OpinionStatsResp | null>(null);
  const [opExpanded, setOpExpanded] = useState<number[]>([]); // 展开看全文的反馈 id
  const [preview, setPreview] = useState<{ id: number; loading: boolean; items: PreviewItem[] } | null>(
    null
  );
  const [convertTarget, setConvertTarget] = useState<OpinionItem | null>(null);
  const [convertForm, setConvertForm] = useState<{
    title: string;
    description: string;
    type: WishType;
  }>({ title: "", description: "", type: "other" });
  const [convertBusy, setConvertBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);
  /** 已创建的 objectURL：关闭 / 卸载时统一 revoke，避免内存泄漏。 */
  const previewUrls = useRef<string[]>([]);

  const loadSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ov, tr, ua, tl, tc, lm] = await Promise.all([
        api.uxOverview(filters),
        api.uxTrends(filters),
        api.uxUserActivity(filters),
        api.uxTools(filters),
        api.uxToolCalls({ ...filters, limit: 50 }),
        api.uxLlm({ ...filters }),
      ]);
      setOverview(ov);
      setTrends(tr);
      setActivity(ua);
      setTools(tl);
      setToolCalls(tc);
      setLlm(lm);
      setUpdatedAt(new Date());
    } catch (e: any) {
      setError(e?.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    loadSummary();
  }, [loadSummary]);

  const loadDetail = useCallback(async () => {
    if (detailTab === "opinions") return; // 用户反馈有独立的加载链路（§7.4）
    setDetailLoading(true);
    try {
      if (detailTab === "rounds") {
        const r = await api.uxRounds({
          granularity: filters.granularity,
          days: filters.days,
          user_id: filters.user_id,
          only_failed: onlyFailed,
          limit: PAGE,
          offset: page * PAGE,
        });
        setRounds(r);
      } else {
        const f = await api.uxFeedback({
          granularity: filters.granularity,
          days: filters.days,
          verdict: verdict || undefined,
          limit: PAGE,
          offset: page * PAGE,
        });
        setFeedback(f);
      }
    } catch (e: any) {
      setError(e?.message || "明细加载失败");
    } finally {
      setDetailLoading(false);
    }
  }, [detailTab, filters.granularity, filters.days, filters.user_id, onlyFailed, verdict, page]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  // 过滤条件变化时回到第一页。
  useEffect(() => {
    setPage(0);
  }, [detailTab, onlyFailed, verdict, filters]);

  const applyDraft = () =>
    setFilters((f) => ({
      granularity: f.granularity,
      days: f.days,
      user_id: draftUser.trim() || undefined,
      model_provider: draftProvider.trim() || undefined,
    }));

  const runBackfill = async () => {
    const uid = bfUser.trim();
    const sid = bfSession.trim();
    if (!uid || !sid || bfBusy) return;
    setBfBusy(true);
    setBfMsg(null);
    try {
      const r = await api.uxBackfill(uid, sid);
      setBfMsg(`回补完成：读取 ${r.records} 个回合，新增 ${r.inserted} 条`);
      await Promise.all([loadSummary(), loadDetail()]);
    } catch (e: any) {
      setBfMsg(`回补失败：${e?.message ?? e}`);
    } finally {
      setBfBusy(false);
    }
  };

  // --- 用户反馈：加载 / 改字段 / 转心愿 / 看截图 ---------------------------
  const flash = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  const loadOpinions = useCallback(async () => {
    setOpLoading(true);
    try {
      const r = await listOpinions({
        page: opPage,
        page_size: PAGE,
        category: opTab === "all" ? undefined : opTab,
        status: opStatuses,
        q: opSearch.trim() || undefined,
      });
      setOpItems(r.items);
      setOpTotal(r.total);
      setOpExpanded([]);
    } catch (e: any) {
      flash(`反馈列表加载失败：${e?.message ?? e}`);
    } finally {
      setOpLoading(false);
    }
  }, [opPage, opTab, opStatuses, opSearch, flash]);

  /** 统计卡片：与列表分离请求，翻页时不重拉（§7.4）。 */
  const loadOpStats = useCallback(async () => {
    try {
      setOpStats(await getOpinionStats());
    } catch (e: any) {
      flash(`反馈统计加载失败：${e?.message ?? e}`);
    }
  }, [flash]);

  const reloadOpinions = useCallback(
    () => Promise.allSettled([loadOpinions(), loadOpStats()]),
    [loadOpinions, loadOpStats]
  );

  useEffect(() => {
    if (detailTab !== "opinions") return;
    loadOpinions();
  }, [detailTab, loadOpinions]);

  useEffect(() => {
    if (detailTab !== "opinions") return;
    loadOpStats();
  }, [detailTab, loadOpStats]);

  // 筛选条件变化 → 回到第一页（若已在第一页，loadOpinions 身份变化会直接触发重拉）。
  const resetOpPage = () => setOpPage((p) => (p === 1 ? p : 1));

  const toggleOpExpand = (id: number) =>
    setOpExpanded((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const applyLocal = (id: number, patch: Partial<OpinionItem>) =>
    setOpItems((list) => list.map((it) => (it.id === id ? { ...it, ...patch } : it)));

  /** 两个 select 即改即存：先乐观更新，失败回滚；成功后重拉列表 + 统计。 */
  const patchOp = async (row: OpinionItem, patch: Partial<OpinionItem>) => {
    const snapshot = opItems;
    applyLocal(row.id, patch);
    try {
      const r = await patchOpinion(row.id, {
        status: patch.status,
        category: patch.category,
      });
      applyLocal(row.id, { status: r.status, category: r.category });
      await reloadOpinions();
    } catch (e: any) {
      setOpItems(snapshot);
      flash(`修改失败：${e?.message ?? e}`);
    }
  };

  const openConvert = (row: OpinionItem) => {
    const firstLine = (row.content.split("\n")[0] ?? "").trim();
    setConvertForm({
      title: firstLine.slice(0, 30) || "（未命名心愿）",
      description: row.content,
      type: "other",
    });
    setConvertTarget(row);
  };

  const submitConvert = async () => {
    if (!convertTarget || convertBusy) return;
    const title = convertForm.title.trim();
    if (!title) {
      flash("请填写心愿标题");
      return;
    }
    setConvertBusy(true);
    try {
      const r = await opinionToWish(convertTarget.id, {
        title,
        description: convertForm.description.trim(),
        type: convertForm.type,
      });
      applyLocal(convertTarget.id, { status: r.status, linked_wish_id: r.linked_wish_id });
      setConvertTarget(null);
      flash("已转为心愿");
      await reloadOpinions();
    } catch (e: any) {
      if ((e as { status?: number })?.status === 409) {
        // 已被（他人或另一标签页）转化过：刷新即可看到「查看心愿」。
        setConvertTarget(null);
        flash("该反馈已转化为心愿");
        await reloadOpinions();
      } else {
        flash(`转化失败：${e?.message ?? e}`);
      }
    } finally {
      setConvertBusy(false);
    }
  };

  const closePreview = useCallback(() => {
    setPreview(null);
    previewUrls.current.forEach((u) => URL.revokeObjectURL(u));
    previewUrls.current = [];
  }, []);

  /** <img> 带不了 Authorization → 先取元数据，再逐张换 objectURL（§7.4④）。 */
  const openPreview = async (row: OpinionItem) => {
    setPreview({ id: row.id, loading: true, items: [] });
    try {
      const { items } = await listOpinionAttachments(row.id);
      setPreview({ id: row.id, loading: true, items: items.map((meta) => ({ meta, url: null })) });
      const urls = await Promise.all(
        items.map((m) =>
          fetchOpinionAttachmentBlob(m.id)
            .then((b) => URL.createObjectURL(b))
            .catch(() => null) // 单张 404 不阻断其余张
        )
      );
      previewUrls.current = urls.filter((u): u is string => u !== null);
      setPreview({
        id: row.id,
        loading: false,
        items: items.map((meta, i) => ({ meta, url: urls[i] ?? null })),
      });
    } catch (e: any) {
      closePreview();
      flash(`截图加载失败：${e?.message ?? e}`);
    }
  };

  // Esc 关闭预览（仅浮层打开时挂监听）。
  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closePreview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview, closePreview]);

  // 卸载兜底：revoke 残留 objectURL + 清 toast 定时器。
  useEffect(
    () => () => {
      previewUrls.current.forEach((u) => URL.revokeObjectURL(u));
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    },
    []
  );

  // --- 图表数据 ------------------------------------------------------------
  const trendRows = useMemo(
    () =>
      (trends?.series ?? []).map((p) => ({
        date: bucketLabel(p.date, filters.granularity),
        rounds: p.rounds,
        success: toPct(p.success_rate),
        tool: toPct(p.tool_accuracy),
        satisfaction: toPct(p.satisfaction_rate),
        avgSec: p.duration_avg_ms == null ? null : Math.round(p.duration_avg_ms) / 1000,
        p90Sec: p.duration_p90_ms == null ? null : Math.round(p.duration_p90_ms) / 1000,
      })),
    [trends, filters.granularity]
  );

  // 用户视角时序：活跃用户 / 请求数(会话回合) / 会话数 / 新增用户。
  const activityRows = useMemo(
    () =>
      (activity?.series ?? []).map((p) => ({
        bucket: bucketLabel(p.bucket, filters.granularity),
        activeUsers: p.active_users,
        requests: p.requests,
        sessions: p.sessions,
        newUsers: p.new_users,
      })),
    [activity, filters.granularity]
  );

  const toolRows = useMemo(
    () =>
      (tools?.tools ?? []).slice(0, 12).map((t) => ({
        name: t.tool_name,
        calls: t.calls,
        errors: t.errors,
        accuracy: toPct(t.accuracy),
      })),
    [tools]
  );

  const reasonRows = useMemo(() => {
    const dist = overview?.l4_satisfaction.down_reasons ?? {};
    return Object.entries(dist)
      .map(([code, n]) => ({ name: REASON_LABEL[code] ?? code, code, value: n }))
      .sort((a, b) => b.value - a.value);
  }, [overview]);

  const tooltipStyle = {
    background: c.surface,
    border: `1px solid ${c.border}`,
    borderRadius: "8px",
    fontSize: "12px",
    color: c.text,
  };
  const axisProps = { stroke: c.text3, fontSize: 11, tickLine: false } as const;

  const detailTotal =
    detailTab === "opinions"
      ? opTotal
      : detailTab === "rounds"
        ? (rounds?.total ?? 0)
        : (feedback?.total ?? 0);
  const pageCount = Math.max(1, Math.ceil(detailTotal / PAGE));
  /** 用户反馈的 page 从 1 起，其余明细用 0 起的 offset（对外统一显示 1 起）。 */
  const curPage = detailTab === "opinions" ? opPage : page + 1;
  const busy = detailTab === "opinions" ? opLoading : detailLoading;
  const goPrev = () =>
    detailTab === "opinions"
      ? setOpPage((p) => Math.max(1, p - 1))
      : setPage((p) => Math.max(0, p - 1));
  const goNext = () =>
    detailTab === "opinions" ? setOpPage((p) => p + 1) : setPage((p) => p + 1);

  const l0 = overview?.l0_user;
  const l1 = overview?.l1_outcome;
  const l2 = overview?.l2_efficiency;
  const l3 = overview?.l3_process;
  const l4 = overview?.l4_satisfaction;
  const granRange = GRAN_OPTIONS.find((g) => g.v === filters.granularity)?.range ?? "";

  return (
    <>
      {/* --- 过滤工具栏 --------------------------------------------------- */}
      <div style={s.toolbar}>
        <select
          className="adm-select"
          style={s.select}
          value={filters.granularity}
          aria-label="统计时间维度"
          onChange={(e) =>
            setFilters((f) => ({ ...f, granularity: e.target.value as UxGranularity }))
          }
        >
          {GRAN_OPTIONS.map((g) => (
            <option key={g.v} value={g.v}>
              按{g.label} · {g.range}
            </option>
          ))}
        </select>
        <input
          className="adm-input"
          style={s.input}
          value={draftUser}
          placeholder="按用户工号过滤"
          aria-label="按用户工号过滤"
          onChange={(e) => setDraftUser(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && applyDraft()}
        />
        <input
          className="adm-input"
          style={{ ...s.input, width: "150px" }}
          value={draftProvider}
          placeholder="按模型厂商过滤"
          aria-label="按模型厂商过滤"
          onChange={(e) => setDraftProvider(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && applyDraft()}
        />
        <button className="adm-btn" style={s.btn} onClick={applyDraft}>
          应用
        </button>
        <button className="adm-btn" style={s.btn} onClick={() => loadSummary()}>
          {loading ? "刷新中…" : "刷新"}
        </button>
        <span style={s.toolbarHint}>
          {updatedAt && `更新于 ${updatedAt.toLocaleTimeString("zh-CN", { hour12: false })}`}
          {granRange && ` · ${granRange}`}
          {filters.user_id && ` · user=${filters.user_id}`}
          {filters.model_provider && ` · provider=${filters.model_provider}`}
        </span>
      </div>

      {error && <div style={s.errorBanner}>加载失败：{error}</div>}

      {!overview && !error && <div style={s.empty}>{loading ? "加载中…" : "暂无数据"}</div>}

      {overview && (
        <>
          {/* --- L0 用户视角 ---------------------------------------------- */}
          <div style={sectionTitle}>L0 用户视角 · 有多少人在用、用了多少（{granRange}）</div>
          <div style={s.cardsRow}>
            <Card
              label="活跃用户"
              value={fmtInt(l0?.active_users)}
              sub="窗口内有会话回合或请求的去重用户"
              tone="green"
            />
            <Card
              label="用户请求数"
              value={fmtInt(l0?.requests)}
              sub="会话回合总数（agent_round_metrics）"
            />
            <Card label="会话数" value={fmtInt(l0?.sessions)} sub="去重 session_id" />
            <Card label="新增用户" value={fmtInt(l0?.new_users)} sub="窗口内注册的账号数" />
          </div>

          {/* 用户活跃趋势 */}
          <div style={{ ...panel, marginBottom: "12px" }}>
            <div style={panelTitle}>
              用户活跃趋势 · 活跃用户 / 请求数 / 会话数 / 新增用户（按
              {GRAN_OPTIONS.find((g) => g.v === filters.granularity)?.label ?? "日"}）
            </div>
            {activityRows.length === 0 ? (
              <div style={s.empty}>暂无用户活跃数据</div>
            ) : (
              <div style={{ height: "240px" }}>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={activityRows} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
                    <CartesianGrid stroke={c.border} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="bucket" {...axisProps} />
                    <YAxis yAxisId="u" allowDecimals={false} {...axisProps} />
                    <YAxis yAxisId="req" orientation="right" allowDecimals={false} {...axisProps} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Legend wrapperStyle={{ fontSize: "11px", color: c.text2 }} />
                    <Bar yAxisId="req" dataKey="requests" name="请求数" fill={c.border} radius={[3, 3, 0, 0]} />
                    <Bar yAxisId="u" dataKey="sessions" name="会话数" fill={c.indigo} radius={[3, 3, 0, 0]} />
                    <Bar yAxisId="u" dataKey="newUsers" name="新增用户" fill={c.amber} radius={[3, 3, 0, 0]} />
                    <Line yAxisId="u" type="monotone" dataKey="activeUsers" name="活跃用户" stroke={c.green} strokeWidth={2} dot={false} connectNulls />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          {/* --- L1 结果层 ------------------------------------------------ */}
          <div style={sectionTitle}>L1 结果层 · 用户拿到了结果吗</div>
          <div style={s.cardsRow}>
            <Card label="回合总数" value={fmtInt(l1?.rounds_total)} sub={`时间窗 ${overview.window_days} 天`} />
            <Card
              label="回合成功率"
              value={pct1(l1?.round_success_rate)}
              sub="无 error 且正常收尾的回合占比"
              tone={(l1?.round_success_rate ?? 1) >= 0.9 ? "green" : "amber"}
            />
            <Card
              label="任务成功率"
              value={pct1(l1?.task_success_rate)}
              sub={`任务型回合 ${fmtInt(l1?.task_rounds)} 个（todo 全部完成）`}
              tone={(l1?.task_success_rate ?? 1) >= 0.8 ? "green" : "amber"}
            />
            <Card
              label="错误率"
              value={pct1(l1?.error_rate)}
              sub="出现 session.error / message.error 的回合"
              tone={(l1?.error_rate ?? 0) > 0.1 ? "red" : undefined}
            />
          </div>
          {l1 && Object.keys(l1.error_breakdown ?? {}).length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "12px" }}>
              {Object.entries(l1.error_breakdown)
                .sort((a, b) => b[1] - a[1])
                .map(([name, n]) => (
                  <span
                    key={name}
                    style={{
                      ...s.mono,
                      fontSize: "11px",
                      padding: "2px 8px",
                      borderRadius: "10px",
                      border: "1px solid var(--border)",
                      color: "var(--text-2)",
                    }}
                  >
                    {name}: {n}
                  </span>
                ))}
            </div>
          )}

          {/* --- L2 效率与性能 -------------------------------------------- */}
          <div style={sectionTitle}>L2 效率与性能 · 拿到结果花了多少代价</div>
          <div style={s.cardsRow}>
            <Card label="平均耗时" value={fmtMs(l2?.duration_avg_ms)} sub={`p50 ${fmtMs(l2?.duration_p50_ms)}`} />
            <Card
              label="p90 耗时"
              value={fmtMs(l2?.duration_p90_ms)}
              sub={`p99 ${fmtMs(l2?.duration_p99_ms)}`}
              tone={(l2?.duration_p90_ms ?? 0) > 180000 ? "amber" : undefined}
            />
            <Card label="总成本" value={fmtCost(l2?.total_cost)} sub={`单回合均值 ${fmtCost(l2?.avg_cost)}`} />
            <Card
              label="Token 效率"
              value={fmtInt(l3?.tokens_per_success)}
              sub="每个成功回合消耗的 token（越低越好）"
            />
          </div>

          {/* --- 趋势图 --------------------------------------------------- */}
          <div style={grid}>
            <div style={panel}>
              <div style={panelTitle}>质量趋势 · 成功率 / 工具准确率 / 满意度（%）</div>
              <div style={{ height: "230px" }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={trendRows} margin={{ top: 4, right: 12, bottom: 0, left: -18 }}>
                    <CartesianGrid stroke={c.border} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" {...axisProps} />
                    <YAxis domain={[0, 100]} unit="%" {...axisProps} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Legend wrapperStyle={{ fontSize: "11px", color: c.text2 }} />
                    <Line type="monotone" dataKey="success" name="回合成功率" stroke={c.green} strokeWidth={2} dot={false} connectNulls />
                    <Line type="monotone" dataKey="tool" name="工具准确率" stroke={c.indigo} strokeWidth={2} dot={false} connectNulls />
                    <Line type="monotone" dataKey="satisfaction" name="满意度" stroke={c.amber} strokeWidth={2} dot={false} connectNulls />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div style={panel}>
              <div style={panelTitle}>效率趋势 · 回合量 / 平均耗时 / p90 耗时（秒）</div>
              <div style={{ height: "230px" }}>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={trendRows} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
                    <CartesianGrid stroke={c.border} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" {...axisProps} />
                    <YAxis yAxisId="sec" unit="s" {...axisProps} />
                    <YAxis yAxisId="cnt" orientation="right" allowDecimals={false} {...axisProps} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Legend wrapperStyle={{ fontSize: "11px", color: c.text2 }} />
                    <Bar yAxisId="cnt" dataKey="rounds" name="回合数" fill={c.border} radius={[3, 3, 0, 0]} />
                    <Line yAxisId="sec" type="monotone" dataKey="avgSec" name="平均耗时" stroke={c.indigo} strokeWidth={2} dot={false} connectNulls />
                    <Line yAxisId="sec" type="monotone" dataKey="p90Sec" name="p90 耗时" stroke={c.red} strokeWidth={2} dot={false} connectNulls />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>

          {/* --- L3 过程与轨迹 -------------------------------------------- */}
          <div style={sectionTitle}>L3 过程与轨迹 · Agent 走对了路吗</div>
          <div style={s.cardsRow}>
            <Card label="工具调用" value={fmtInt(l3?.tool_calls)} sub={`失败 ${fmtInt(l3?.tool_errors)} 次`} />
            <Card
              label="工具调用准确率"
              value={pct1(l3?.tool_accuracy)}
              sub="未返回错误的工具调用占比"
              tone={(l3?.tool_accuracy ?? 1) >= 0.95 ? "green" : "amber"}
            />
            <Card label="Token 总消耗" value={fmtInt(l3?.total_tokens)} sub={`单回合均值 ${fmtInt(l3?.avg_tokens_per_round)}`} />
            <Card label="反馈样本" value={fmtInt(l4?.total)} sub={`👍 ${fmtInt(l4?.thumbs_up)} · 👎 ${fmtInt(l4?.thumbs_down)}`} />
          </div>

          {/* 工具调用记录明细（L3 过程与轨迹下钻） */}
          <div style={{ ...panel, marginBottom: "12px" }}>
            <div style={panelTitle}>
              工具调用记录 · 最近 {(toolCalls?.tool_calls ?? []).length} 条（共 {fmtInt(toolCalls?.total)} 条）
            </div>
            {(toolCalls?.tool_calls ?? []).length === 0 ? (
              <div style={s.empty}>暂无工具调用记录</div>
            ) : (
              <div style={{ ...s.tableWrap, border: "none", borderRadius: 0, maxHeight: "360px" }}>
                <table style={s.table}>
                  <thead>
                    <tr>
                      <th style={s.th}>时间</th>
                      <th style={s.th}>用户</th>
                      <th style={s.th}>工具</th>
                      <th style={s.th}>状态</th>
                      <th style={s.th}>耗时</th>
                      <th style={s.th}>会话 / 回合</th>
                      <th style={s.th}>错误</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(toolCalls?.tool_calls ?? []).map((t) => (
                      <tr key={t.id}>
                        <td style={s.td}>{fmtTime(t.created_at)}</td>
                        <td style={s.td} title={t.user_id}>
                          {userLabel(t.user_name, t.user_uid, t.user_id)}
                        </td>
                        <td style={{ ...s.td, ...s.mono }}>{t.tool_name}</td>
                        <td style={s.td}>
                          {t.is_error ? (
                            <span style={{ color: "var(--red)" }}>{t.status ?? "错误"}</span>
                          ) : (
                            <span style={{ color: "var(--green)" }}>{t.status ?? "成功"}</span>
                          )}
                        </td>
                        <td style={s.td}>{fmtMs(t.duration_ms)}</td>
                        <td style={{ ...s.td, ...s.mono }}>
                          {clip(t.session_id, 10)} #{t.round_seq}
                        </td>
                        <td style={s.td}>
                          {t.error_text ? (
                            <span style={{ color: "var(--red)" }} title={t.error_text}>
                              {clip(t.error_text, 40)}
                            </span>
                          ) : (
                            <span style={s.muted}>—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div style={grid}>
            <div style={panel}>
              <div style={panelTitle}>工具调用量与失败量（Top 12）</div>
              {toolRows.length === 0 ? (
                <div style={s.empty}>暂无工具调用记录</div>
              ) : (
                <div style={{ height: "260px" }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={toolRows} layout="vertical" margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
                      <CartesianGrid stroke={c.border} strokeDasharray="3 3" horizontal={false} />
                      <XAxis type="number" allowDecimals={false} {...axisProps} />
                      <YAxis type="category" dataKey="name" width={110} {...axisProps} />
                      <Tooltip contentStyle={tooltipStyle} />
                      <Legend wrapperStyle={{ fontSize: "11px", color: c.text2 }} />
                      <Bar dataKey="calls" name="调用" fill={c.indigo} radius={[0, 3, 3, 0]} />
                      <Bar dataKey="errors" name="失败" fill={c.red} radius={[0, 3, 3, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>

            {/* --- L4 主观满意度 ------------------------------------------ */}
            <div style={panel}>
              <div style={panelTitle}>
                L4 主观满意度 · {pct1(l4?.satisfaction_rate)}（👎 原因分布）
              </div>
              {reasonRows.length === 0 ? (
                <div style={s.empty}>
                  {l4 && l4.total === 0 ? "暂无用户反馈" : "暂无点踩原因"}
                </div>
              ) : (
                <div style={{ height: "260px" }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={reasonRows}
                        dataKey="value"
                        nameKey="name"
                        innerRadius="45%"
                        outerRadius="72%"
                        paddingAngle={2}
                        stroke={c.surface}
                        label={{ fontSize: 11, fill: c.text2 }}
                      >
                        {reasonRows.map((r, i) => (
                          <Cell
                            key={r.code}
                            fill={[c.red, c.amber, c.indigo, c.green, c.text3, "#a855f7", "#0ea5e9"][i % 7]}
                          />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={tooltipStyle} />
                      <Legend wrapperStyle={{ fontSize: "11px", color: c.text2 }} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </div>

          {/* --- LLM 代理上游（B1/B2）------------------------------------ */}
          <div style={sectionTitle}>LLM 代理上游 · 状态码 / 首字节 / 耗时</div>
          <div style={s.cardsRow}>
            <Card label="上游调用数" value={fmtInt(llm?.totals.calls)} sub={`时间窗 ${overview.window_days} 天`} />
            <Card
              label="上游错误率"
              value={pct1(llm?.totals.error_rate)}
              sub={`失败 ${fmtInt(llm?.totals.errors)} 次（4xx/5xx/连接失败）`}
              tone={(llm?.totals.error_rate ?? 0) > 0.05 ? "red" : undefined}
            />
          </div>
          <div style={s.tableWrap}>
            <table style={s.table}>
              <thead>
                <tr>
                  <th style={s.th}>Provider</th>
                  <th style={s.th}>调用</th>
                  <th style={s.th}>错误率</th>
                  <th style={s.th}>状态码分布</th>
                  <th style={s.th}>TTFT p50 / p90</th>
                  <th style={s.th}>耗时 p50 / p90 / p99</th>
                  <th style={s.th}>SSE</th>
                </tr>
              </thead>
              <tbody>
                {(llm?.providers ?? []).map((p) => (
                  <tr key={p.provider_id}>
                    <td style={{ ...s.td, ...s.mono }}>{p.provider_id}</td>
                    <td style={s.td}>{fmtInt(p.calls)}</td>
                    <td style={s.td}>
                      <span style={{ color: (p.error_rate ?? 0) > 0.05 ? "var(--red)" : "var(--text)" }}>
                        {pct1(p.error_rate)}
                      </span>
                    </td>
                    <td style={{ ...s.td, ...s.mono, fontSize: "11px" }}>
                      {Object.entries(p.status_counts)
                        .sort((a, b) => b[1] - a[1])
                        .map(([code, n]) => (
                          <span
                            key={code}
                            style={{
                              marginRight: "6px",
                              color: code !== "none" && Number(code) >= 400 ? "var(--red)" : "var(--text-3)",
                            }}
                          >
                            {code}:{n}
                          </span>
                        ))}
                    </td>
                    <td style={s.td}>
                      {fmtMs(p.ttft_p50_ms)} / {fmtMs(p.ttft_p90_ms)}
                    </td>
                    <td style={s.td}>
                      {fmtMs(p.duration_p50_ms)} / {fmtMs(p.duration_p90_ms)} / {fmtMs(p.duration_p99_ms)}
                    </td>
                    <td style={s.td}>{fmtInt(p.sse_calls)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(llm?.providers ?? []).length === 0 && (
              <div style={s.empty}>{loading ? "加载中…" : "暂无 LLM 代理调用记录"}</div>
            )}
          </div>

          {/* --- 明细下钻 ------------------------------------------------- */}
          <div style={sectionTitle}>明细下钻</div>
          <div style={s.logsTabBar}>
            <button
              className={`adm-tab${detailTab === "rounds" ? " adm-tab-active" : ""}`}
              style={{ ...s.logsTab, ...(detailTab === "rounds" ? s.logsTabActive : {}) }}
              onClick={() => setDetailTab("rounds")}
            >
              回合明细
            </button>
            <button
              className={`adm-tab${detailTab === "feedback" ? " adm-tab-active" : ""}`}
              style={{ ...s.logsTab, ...(detailTab === "feedback" ? s.logsTabActive : {}) }}
              onClick={() => setDetailTab("feedback")}
            >
              点赞点踩
            </button>
            <button
              className={`adm-tab${detailTab === "opinions" ? " adm-tab-active" : ""}`}
              style={{ ...s.logsTab, ...(detailTab === "opinions" ? s.logsTabActive : {}) }}
              onClick={() => setDetailTab("opinions")}
            >
              用户反馈
            </button>
          </div>

          {detailTab === "opinions" ? (
            <div style={{ ...s.toolbar, marginTop: "10px" }}>
              <div style={s.searchWrap}>
                <input
                  className="adm-input"
                  style={s.search}
                  value={opSearchDraft}
                  placeholder="搜索内容 / 姓名 / 工号"
                  aria-label="搜索用户反馈"
                  onChange={(e) => setOpSearchDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key !== "Enter") return;
                    setOpSearch(opSearchDraft);
                    resetOpPage();
                  }}
                />
              </div>
              <button
                className="adm-btn"
                style={s.btnSmall}
                onClick={() => {
                  setOpSearch(opSearchDraft);
                  resetOpPage();
                }}
              >
                搜索
              </button>
              {opSearch && (
                <button
                  className="adm-btn"
                  style={s.btnSmall}
                  onClick={() => {
                    setOpSearchDraft("");
                    setOpSearch("");
                    resetOpPage();
                  }}
                >
                  清空
                </button>
              )}
              <button
                className="adm-btn"
                style={{ ...s.btnSmall, ...(opLoading ? s.btnDisabled : {}) }}
                disabled={opLoading}
                onClick={reloadOpinions}
              >
                刷新
              </button>
            </div>
          ) : (
            <div style={{ ...s.toolbar, marginTop: "10px" }}>
              {detailTab === "rounds" ? (
                <label style={s.checkboxLabel}>
                  <input
                    className="adm-check"
                    type="checkbox"
                    checked={onlyFailed}
                    onChange={(e) => setOnlyFailed(e.target.checked)}
                  />
                  只看失败回合
                </label>
              ) : (
                <select
                  className="adm-select"
                  style={s.select}
                  value={verdict}
                  aria-label="按反馈类型筛选"
                  onChange={(e) => setVerdict(e.target.value as "" | "up" | "down")}
                >
                  <option value="">全部反馈</option>
                  <option value="up">只看点赞</option>
                  <option value="down">只看点踩</option>
                </select>
              )}
            </div>
          )}

          <div style={{ ...s.toolbar, marginTop: "8px" }}>
            <span style={s.toolbarHint}>
              共 {fmtInt(detailTotal)} 条 · 第 {curPage}/{pageCount} 页
            </span>
            <button
              className="adm-btn"
              style={{ ...s.btnSmall, ...(curPage <= 1 ? s.btnDisabled : {}) }}
              disabled={curPage <= 1 || busy}
              onClick={goPrev}
            >
              上一页
            </button>
            <button
              className="adm-btn"
              style={{ ...s.btnSmall, ...(curPage >= pageCount ? s.btnDisabled : {}) }}
              disabled={curPage >= pageCount || busy}
              onClick={goNext}
            >
              下一页
            </button>
          </div>

          {detailTab === "opinions" ? (
            <>
              {/* 三分类 Tab：徽标数直接取统计接口，不单独请求（§7.4①） */}
              <div style={s.opinionTabRow}>
                {OPINION_TABS.map((t) => {
                  const n =
                    t.v === "bug"
                      ? (opStats?.bug.total ?? 0)
                      : t.v === "feature"
                        ? (opStats?.feature.total ?? 0)
                        : (opStats?.bug.total ?? 0) + (opStats?.feature.total ?? 0);
                  return (
                    <button
                      key={t.v}
                      className="opinion-tab"
                      data-active={opTab === t.v}
                      style={s.opinionTabButton}
                      onClick={() => {
                        if (opTab === t.v) return;
                        setOpTab(t.v);
                        resetOpPage();
                      }}
                    >
                      {t.label}
                      <span style={{ marginLeft: "6px", opacity: 0.72 }}>{n}</span>
                    </button>
                  );
                })}
              </div>

              {/* 统计卡片 + 状态徽标（点徽标即加入筛选，§7.4②） */}
              <div style={{ marginTop: "10px" }}>
                <OpinionStatCards
                  tab={opTab}
                  stats={opStats}
                  statuses={opStatuses}
                  onToggleStatus={(k) => {
                    setOpStatuses((cur) =>
                      cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]
                    );
                    resetOpPage();
                  }}
                  onClearStatuses={() => {
                    setOpStatuses([]);
                    resetOpPage();
                  }}
                />
              </div>

              <div style={{ ...s.tableWrap, marginTop: "10px" }}>
                <table style={s.table}>
                  <thead>
                    <tr>
                      <th style={s.th}>反馈时间</th>
                      <th style={s.th}>分类</th>
                      <th style={s.th}>姓名</th>
                      <th style={s.th}>工号</th>
                      <th style={s.th}>内容</th>
                      <th style={s.th}>截图</th>
                      <th style={s.th}>解决状态</th>
                      <th style={s.th}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {opItems.map((w) => {
                      const expanded = opExpanded.includes(w.id);
                      const long = w.content.trim().length > CONTENT_CLAMP;
                      return (
                        <tr key={w.id}>
                          <td style={{ ...s.td, whiteSpace: "nowrap" }}>{fmtMinute(w.created_at)}</td>
                          <td style={s.td}>
                            <select
                              className="opinion-select"
                              style={s.statusSelect}
                              value={w.category}
                              aria-label={`反馈 #${w.id} 的分类`}
                              onChange={(e) =>
                                patchOp(w, { category: e.target.value as FeedbackCategory })
                              }
                            >
                              <option value="bug">Bug</option>
                              <option value="feature">功能特性</option>
                            </select>
                          </td>
                          <td style={s.td}>{w.name || <span style={s.muted}>—</span>}</td>
                          <td style={{ ...s.td, ...s.mono }}>
                            {w.uid || <span style={s.muted}>—</span>}
                          </td>
                          <td style={s.td}>
                            <div
                              style={
                                expanded
                                  ? { ...s.opinionContentCell, WebkitLineClamp: "none" }
                                  : s.opinionContentCell
                              }
                              onClick={long ? () => toggleOpExpand(w.id) : undefined}
                              title={long && !expanded ? "点击展开全文" : undefined}
                            >
                              {w.content}
                            </div>
                            {long && (
                              <button
                                className="opinion-link"
                                style={{ marginTop: "2px", fontSize: "11.5px" }}
                                onClick={() => toggleOpExpand(w.id)}
                              >
                                {expanded ? "收起" : "展开"}
                              </button>
                            )}
                          </td>
                          <td style={s.td}>
                            {w.attachment_count > 0 ? (
                              <button className="opinion-link" onClick={() => openPreview(w)}>
                                查看截图({w.attachment_count})
                              </button>
                            ) : (
                              <span style={s.muted}>—</span>
                            )}
                          </td>
                          <td style={s.td}>
                            <select
                              className="opinion-select"
                              style={s.statusSelect}
                              value={w.status}
                              aria-label={`反馈 #${w.id} 的解决状态`}
                              onChange={(e) =>
                                patchOp(w, { status: e.target.value as OpinionStatus })
                              }
                            >
                              {OPINION_STATUS_ORDER.map((k) => (
                                <option key={k} value={k}>
                                  {OPINION_STATUS_LABEL[k]}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td style={s.td}>
                            {w.linked_wish_id != null ? (
                              <button
                                className="opinion-link"
                                onClick={() => onOpenWishes?.(w.linked_wish_id)}
                              >
                                查看心愿
                              </button>
                            ) : w.category === "feature" ? (
                              <button className="opinion-link" onClick={() => openConvert(w)}>
                                转为心愿
                              </button>
                            ) : (
                              <span style={s.muted}>—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {opItems.length === 0 && (
                  <div style={s.empty}>{opLoading ? "加载中…" : "暂无符合条件的反馈"}</div>
                )}
              </div>
            </>
          ) : detailTab === "rounds" ? (
            <div style={s.tableWrap}>
              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>时间</th>
                    <th style={s.th}>用户</th>
                    <th style={s.th}>会话 / 回合</th>
                    <th style={s.th}>结果</th>
                    <th style={s.th}>耗时</th>
                    <th style={s.th}>工具</th>
                    <th style={s.th}>Token</th>
                    <th style={s.th}>成本</th>
                    <th style={s.th}>模型 / Agent</th>
                    <th style={s.th}>来源</th>
                  </tr>
                </thead>
                <tbody>
                  {(rounds?.rounds ?? []).map((r) => (
                    <tr key={r.id}>
                      <td style={s.td}>{fmtTime(r.created_at)}</td>
                      <td style={s.td} title={r.user_id}>
                        {userLabel(r.user_name, r.user_uid, r.user_id)}
                      </td>
                      <td style={{ ...s.td, ...s.mono }}>
                        {clip(r.session_id, 10)} #{r.round_seq}
                        {r.is_task && <span style={{ color: "var(--indigo)" }}> ·任务</span>}
                      </td>
                      <td style={s.td}>
                        {r.errored ? (
                          <span title={r.error_text ?? ""} style={{ color: "var(--red)" }}>
                            {r.error_name ?? "错误"}
                            {r.error_status_code != null && (
                              <span style={s.muted}> ·{r.error_status_code}</span>
                            )}
                          </span>
                        ) : r.succeeded ? (
                          <span style={{ color: "var(--green)" }}>成功</span>
                        ) : (
                          <span style={s.muted}>未完成</span>
                        )}
                        {r.is_task && r.task_success != null && (
                          <span style={s.muted}>{r.task_success ? " ✓" : " ✗"}</span>
                        )}
                      </td>
                      <td style={s.td}>{fmtMs(r.duration_ms)}</td>
                      <td style={s.td}>
                        {r.tool_calls}
                        {r.tool_errors > 0 && <span style={{ color: "var(--red)" }}> /{r.tool_errors}错</span>}
                      </td>
                      <td
                        style={s.td}
                        title={
                          `in ${fmtInt(r.input_tokens)} · out ${fmtInt(r.output_tokens)}` +
                          ` · reason ${fmtInt(r.reasoning_tokens)}` +
                          ` · cache r/w ${fmtInt(r.cache_read_tokens)}/${fmtInt(r.cache_write_tokens)}`
                        }
                      >
                        {fmtInt(r.total_tokens)}
                      </td>
                      <td style={s.td}>{fmtCost(r.cost)}</td>
                      <td style={{ ...s.td, ...s.mono }}>
                        {r.model_provider ? `${r.model_provider}/${r.model_id ?? ""}` : "—"}
                        {r.agent && <span style={s.muted}> ·{r.agent}</span>}
                      </td>
                      <td style={s.td}>
                        <span style={s.muted}>{r.source === "backfill" ? "回补" : "实时"}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(rounds?.rounds ?? []).length === 0 && (
                <div style={s.empty}>{detailLoading ? "加载中…" : "暂无回合记录"}</div>
              )}
            </div>
          ) : (
            <div style={s.tableWrap}>
              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>时间</th>
                    <th style={s.th}>用户</th>
                    <th style={s.th}>评价</th>
                    <th style={s.th}>原因</th>
                    <th style={s.th}>补充说明</th>
                    <th style={s.th}>模型 / Agent</th>
                    <th style={s.th}>会话 / 消息</th>
                    <th style={s.th}></th>
                  </tr>
                </thead>
                <tbody>
                  {(feedback?.feedback ?? []).map((f) => (
                    <Fragment key={f.id}>
                      <tr>
                        <td style={s.td}>{fmtTime(f.created_at)}</td>
                        <td style={{ ...s.td, ...s.mono }}>{f.user_id}</td>
                        <td style={s.td}>
                          <span style={{ color: f.verdict === "up" ? "var(--green)" : "var(--red)" }}>
                            {f.verdict === "up" ? "👍 点赞" : "👎 点踩"}
                          </span>
                          {f.turn_errored && <span style={{ color: "var(--amber)" }}> ·回合报错</span>}
                        </td>
                        <td style={s.td}>
                          {f.reason_codes.length === 0 ? (
                            <span style={s.muted}>—</span>
                          ) : (
                            f.reason_codes.map((code) => REASON_LABEL[code] ?? code).join("、")
                          )}
                        </td>
                        <td style={s.td}>{clip(f.reason_text, 40) || <span style={s.muted}>—</span>}</td>
                        <td style={{ ...s.td, ...s.mono }}>
                          {f.model_provider ? `${f.model_provider}/${f.model_id ?? ""}` : "—"}
                          {f.agent && <span style={s.muted}> ·{f.agent}</span>}
                        </td>
                        <td style={{ ...s.td, ...s.mono }}>{clip(f.session_id, 10)}</td>
                        <td style={s.td}>
                          <button
                            className="adm-btn"
                            style={s.btnSmall}
                            onClick={() => setExpandedId(expandedId === f.id ? null : f.id)}
                          >
                            {expandedId === f.id ? "收起" : "上下文"}
                          </button>
                        </td>
                      </tr>
                      {expandedId === f.id && (
                        <tr>
                          <td colSpan={8} style={{ ...s.td, background: "var(--surface-2)" }}>
                            <FeedbackContext row={f} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
              {(feedback?.feedback ?? []).length === 0 && (
                <div style={s.empty}>{detailLoading ? "加载中…" : "暂无反馈记录"}</div>
              )}
            </div>
          )}

          {/* --- 历史回补 ------------------------------------------------- */}
          <div style={sectionTitle}>历史指标回补</div>
          <div style={{ ...panel, display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
            <span style={{ fontSize: "12px", color: "var(--text-3)" }}>
              对已跑过但未采集到的会话，从容器重放消息补齐指标（幂等，容器须运行中）：
            </span>
            <input
              className="adm-input"
              style={{ ...s.input, width: "150px" }}
              value={bfUser}
              placeholder="用户工号"
              aria-label="回补目标用户工号"
              onChange={(e) => setBfUser(e.target.value)}
            />
            <input
              className="adm-input"
              style={{ ...s.input, width: "260px" }}
              value={bfSession}
              placeholder="会话 ID"
              aria-label="回补目标会话 ID"
              onChange={(e) => setBfSession(e.target.value)}
            />
            <button
              className="adm-btn"
              style={{ ...s.btn, ...(!bfUser.trim() || !bfSession.trim() || bfBusy ? s.btnDisabled : {}) }}
              disabled={!bfUser.trim() || !bfSession.trim() || bfBusy}
              onClick={runBackfill}
            >
              {bfBusy ? "回补中…" : "开始回补"}
            </button>
            {bfMsg && <span style={{ fontSize: "12px", color: "var(--text-2)" }}>{bfMsg}</span>}
          </div>
        </>
      )}

      {toast && <div className="adm-toast" style={s.toast}>{toast}</div>}

      {preview && <AttachmentPreviewOverlay state={preview} onClose={closePreview} />}

      {convertTarget && (
        <OpinionConvertModal
          form={convertForm}
          busy={convertBusy}
          onChange={setConvertForm}
          onCancel={() => setConvertTarget(null)}
          onSubmit={submitConvert}
        />
      )}
    </>
  );
}

/** 反馈明细展开行：本轮上下文快照（用户提问 → assistant 输出 → 工具轨迹）。 */
function FeedbackContext({ row }: { row: UxFeedback["feedback"][number] }) {
  const ctx = row.context ?? {};
  const tools: any[] = Array.isArray(ctx.tools) ? ctx.tools : [];
  return (
    <div style={{ fontSize: "12px", lineHeight: 1.7, color: "var(--text-2)" }}>
      <div>
        <b style={{ color: "var(--text)" }}>用户提问</b>
        <span style={{ ...s.mono, ...s.muted, marginLeft: "6px" }}>{clip(ctx.user_message_id, 24)}</span>
      </div>
      <div style={{ whiteSpace: "pre-wrap" }}>{clip(ctx.user_text, 800) || "（无文本）"}</div>
      {ctx.assistant_reasoning && (
        <>
          <div style={{ marginTop: "8px" }}>
            <b style={{ color: "var(--text)" }}>思考过程</b>
          </div>
          <div style={{ whiteSpace: "pre-wrap", color: "var(--text-3)" }}>
            {clip(ctx.assistant_reasoning, 600)}
          </div>
        </>
      )}
      <div style={{ marginTop: "8px" }}>
        <b style={{ color: "var(--text)" }}>Agent 回复</b>
        <span style={{ ...s.mono, ...s.muted, marginLeft: "6px" }}>{clip(ctx.assistant_message_id, 24)}</span>
      </div>
      <div style={{ whiteSpace: "pre-wrap" }}>{clip(ctx.assistant_text, 1200) || "（无文本）"}</div>
      {tools.length > 0 && (
        <>
          <div style={{ marginTop: "8px" }}>
            <b style={{ color: "var(--text)" }}>工具轨迹</b>（{tools.length}）
          </div>
          {tools.map((t, i) => (
            <div key={i} style={{ ...s.mono, color: t.status === "error" ? "var(--red)" : "var(--text-3)" }}>
              {i + 1}. {t.name} · {t.status}
              {t.error ? ` · ${clip(String(t.error), 160)}` : ""}
            </div>
          ))}
        </>
      )}
      {ctx.error && (
        <div style={{ marginTop: "8px", color: "var(--red)" }}>回合错误：{clip(String(ctx.error), 300)}</div>
      )}
      {row.context_truncated && (
        <div style={{ marginTop: "6px", color: "var(--amber)" }}>（上下文过长，落库时已截断）</div>
      )}
    </div>
  );
}

// --- 用户反馈辅助组件（§7.4）------------------------------------------------

/** 统计卡片 + 状态徽标：口径随分类 Tab 切换；徽标点击即加入状态筛选。 */
function OpinionStatCards({
  tab,
  stats,
  statuses,
  onToggleStatus,
  onClearStatuses,
}: {
  tab: OpinionTab;
  stats: OpinionStatsResp | null;
  statuses: OpinionStatus[];
  onToggleStatus: (k: OpinionStatus) => void;
  onClearStatuses: () => void;
}) {
  const bug = stats?.bug;
  const feat = stats?.feature;
  const sum = (f: (st: OpinionCategoryStats) => number): number =>
    (bug ? f(bug) : 0) + (feat ? f(feat) : 0);

  const cards: { label: string; value: string; alert?: boolean }[] =
    tab === "bug"
      ? [
          { label: "Bug 总数", value: fmtInt(bug?.total ?? 0) },
          { label: "未解决", value: fmtInt(bug?.by_status.open ?? 0) },
          {
            label: "超 7 天未解决",
            value: fmtInt(bug?.unresolved_7d ?? 0),
            alert: (bug?.unresolved_7d ?? 0) > 0,
          },
          { label: "带截图", value: fmtInt(bug?.with_attachment ?? 0) },
        ]
      : tab === "feature"
        ? [
            { label: "功能特性总数", value: fmtInt(feat?.total ?? 0) },
            { label: "待评估", value: fmtInt(feat?.by_status.evaluating ?? 0) },
            { label: "已转心愿", value: fmtInt(feat?.linked_to_wish ?? 0) },
            { label: "转化率", value: pct1(feat?.conversion_rate ?? null) },
          ]
        : [
            { label: "反馈总数", value: fmtInt(sum((x) => x.total)) },
            { label: "未解决", value: fmtInt(sum((x) => x.by_status.open ?? 0)) },
            {
              label: "超 7 天未解决",
              value: fmtInt(sum((x) => x.unresolved_7d)),
              alert: sum((x) => x.unresolved_7d) > 0,
            },
            { label: "已转心愿", value: fmtInt(feat?.linked_to_wish ?? 0) },
          ];

  const badgeCount = (k: OpinionStatus): number =>
    tab === "all"
      ? sum((x) => x.by_status[k] ?? 0)
      : ((tab === "bug" ? bug : feat)?.by_status[k] ?? 0);

  return (
    <>
      <div style={s.statCardRow}>
        {cards.map((cd) => (
          <div
            key={cd.label}
            className="stat-card"
            data-alert={cd.alert ? "true" : "false"}
            style={s.statCard}
          >
            <div style={s.cardLabel}>{cd.label}</div>
            <div
              style={{
                ...s.cardValue,
                fontSize: "22px",
                ...(cd.alert ? { color: "var(--red)" } : {}),
              }}
            >
              {cd.value}
            </div>
          </div>
        ))}
      </div>
      <div style={{ ...s.opinionTabRow, marginTop: "8px" }}>
        <span style={{ fontSize: "12px", color: "var(--text-3)" }}>按状态筛选</span>
        {OPINION_STATUS_ORDER.map((k) => (
          <button
            key={k}
            className="opinion-badge"
            data-active={statuses.includes(k)}
            aria-pressed={statuses.includes(k)}
            onClick={() => onToggleStatus(k)}
          >
            {OPINION_STATUS_LABEL[k]}
            <span style={{ opacity: 0.72 }}>{badgeCount(k)}</span>
          </button>
        ))}
        {statuses.length > 0 && (
          <button className="opinion-link" style={{ fontSize: "11.5px" }} onClick={onClearStatuses}>
            清除
          </button>
        )}
      </div>
    </>
  );
}

/** 截图预览浮层：objectURL 由父组件持有并在关闭/卸载时 revoke（§7.4④）。 */
function AttachmentPreviewOverlay({
  state,
  onClose,
}: {
  state: { id: number; loading: boolean; items: PreviewItem[] };
  onClose: () => void;
}) {
  return (
    <div
      style={s.attachmentOverlay}
      role="dialog"
      aria-modal="true"
      aria-label={`反馈 #${state.id} 的截图`}
      onClick={onClose}
    >
      <div
        style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "18px" }}
        onClick={(e) => e.stopPropagation()}
      >
        {state.loading && state.items.length === 0 && (
          <div style={{ fontSize: "13px", color: "var(--text-2)" }}>截图加载中…</div>
        )}
        {state.items.map((it) => (
          <figure key={it.meta.id} style={s.attachmentFigure}>
            {it.url ? (
              <img
                src={it.url}
                alt={`反馈 #${state.id} 截图 ${it.meta.id}`}
                style={{
                  maxWidth: "90vw",
                  maxHeight: "76vh",
                  objectFit: "contain",
                  borderRadius: "8px",
                  border: "1px solid var(--border)",
                  background: "var(--surface)",
                }}
              />
            ) : (
              <div
                style={{
                  width: "280px",
                  height: "160px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  borderRadius: "8px",
                  border: "1px dashed var(--border-strong)",
                  background: "var(--surface-2)",
                  color: "var(--text-3)",
                  fontSize: "12.5px",
                }}
              >
                附件文件不存在
              </div>
            )}
            <figcaption style={{ fontSize: "11.5px", color: "var(--text-3)" }}>
              {it.meta.width}×{it.meta.height} · {fmtKB(it.meta.size_bytes)}
              {state.loading && !it.url ? " · 加载中…" : ""}
            </figcaption>
          </figure>
        ))}
        <button className="adm-btn" style={s.btnSmall} onClick={onClose}>
          关闭（Esc）
        </button>
      </div>
    </div>
  );
}

/** 转为心愿弹窗：预填内容首行为标题，类型默认「其他」（§7.4⑤）。 */
function OpinionConvertModal({
  form,
  busy,
  onChange,
  onCancel,
  onSubmit,
}: {
  form: { title: string; description: string; type: WishType };
  busy: boolean;
  onChange: (f: { title: string; description: string; type: WishType }) => void;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  const canSubmit = form.title.trim().length > 0 && !busy;
  return (
    <div style={s.overlay} role="dialog" aria-modal="true" aria-label="转为心愿">
      <div style={{ ...s.modal, width: "min(560px, 100%)" }}>
        <div style={s.modalHeader}>
          <span style={s.modalTitle}>转为心愿</span>
          <button
            className="adm-btn"
            style={s.btnSmall}
            onClick={onCancel}
            disabled={busy}
            aria-label="关闭"
          >
            ✕
          </button>
        </div>
        <div style={s.modalBody}>
          <div style={{ ...s.libField, marginBottom: "12px" }}>
            <label style={s.libLabel} htmlFor="op-wish-title">
              心愿标题 *
            </label>
            <input
              id="op-wish-title"
              className="adm-input"
              style={{ ...s.input, fontFamily: "var(--sans)" }}
              value={form.title}
              maxLength={80}
              onChange={(e) => onChange({ ...form, title: e.target.value })}
            />
            <span style={s.libHint}>已按反馈内容首行预填，可自由修改。</span>
          </div>
          <div style={{ ...s.libField, marginBottom: "12px" }}>
            <label style={s.libLabel} htmlFor="op-wish-type">
              心愿类型
            </label>
            <select
              id="op-wish-type"
              className="adm-select"
              style={{ ...s.select, width: "100%" }}
              value={form.type}
              onChange={(e) => onChange({ ...form, type: e.target.value as WishType })}
            >
              {WISH_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div style={s.libField}>
            <label style={s.libLabel} htmlFor="op-wish-desc">
              心愿描述
            </label>
            <textarea
              id="op-wish-desc"
              className="adm-textarea"
              style={{ ...s.textarea, minHeight: "120px" }}
              value={form.description}
              onChange={(e) => onChange({ ...form, description: e.target.value })}
            />
            <span style={s.libHint}>转化后反馈状态自动变为「评估中」，并在此列表提供「查看心愿」。</span>
          </div>
        </div>
        <div style={s.modalFooter}>
          <button className="adm-btn" style={s.btn} onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            className="adm-btn"
            style={{ ...s.btnPrimary, ...(canSubmit ? {} : s.btnDisabled) }}
            disabled={!canSubmit}
            onClick={onSubmit}
          >
            {busy ? "转化中…" : "确认转化"}
          </button>
        </div>
      </div>
    </div>
  );
}
