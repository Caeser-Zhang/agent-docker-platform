/**
 * UxDashboard — 用户体验指标看板（任务二），仅管理员可见。
 *
 * 挂在 AdminPanel 的「用户体验」Tab 下，数据全部来自 /api/admin/ux/*。
 * 四层指标（产品经理视角）：
 *   L1 结果层   —— 回合成功率 / 任务成功率 / 错误率
 *   L2 效率层   —— 回合耗时（均值 + p50/p90/p99）与成本
 *   L3 过程层   —— 工具调用准确率、Token 消耗效率
 *   L4 满意度层 —— 点赞 / 点踩（任务一）与点踩原因分布
 * 下方附回合明细与反馈明细（含上下文快照）供下钻排障，以及历史指标回补入口。
 *
 * 图表配色取自主题 CSS 变量（切换主题时重新解析），保证暗/亮色下都可读。
 */
import { Fragment, useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
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
  type UxFeedback,
  type UxOverview,
  type UxRounds,
  type UxTools,
  type UxTrends,
} from "../api";
import { REASON_OPTIONS } from "../oc/feedback";
import { useTheme } from "../theme";
import { adminStyles as s } from "./adminStyles";

const PAGE = 20;
const DAYS_OPTIONS = [1, 7, 14, 30, 90, 365];
const REASON_LABEL: Record<string, string> = Object.fromEntries(
  REASON_OPTIONS.map((o) => [o.code, o.label])
);

/** 已提交的过滤条件（草稿输入需点「应用」才生效，避免逐字触发请求）。 */
interface Filters {
  days: number;
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

export function UxDashboard() {
  const c = useChartColors();

  const [filters, setFilters] = useState<Filters>({ days: 30 });
  const [draftUser, setDraftUser] = useState("");
  const [draftProvider, setDraftProvider] = useState("");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [overview, setOverview] = useState<UxOverview | null>(null);
  const [trends, setTrends] = useState<UxTrends | null>(null);
  const [tools, setTools] = useState<UxTools | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  // --- 明细（回合 / 反馈）---------------------------------------------------
  const [detailTab, setDetailTab] = useState<"rounds" | "feedback">("rounds");
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

  const loadSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ov, tr, tl] = await Promise.all([
        api.uxOverview(filters),
        api.uxTrends(filters),
        api.uxTools(filters),
      ]);
      setOverview(ov);
      setTrends(tr);
      setTools(tl);
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
    setDetailLoading(true);
    try {
      if (detailTab === "rounds") {
        const r = await api.uxRounds({
          days: filters.days,
          user_id: filters.user_id,
          only_failed: onlyFailed,
          limit: PAGE,
          offset: page * PAGE,
        });
        setRounds(r);
      } else {
        const f = await api.uxFeedback({
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
  }, [detailTab, filters.days, filters.user_id, onlyFailed, verdict, page]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  // 过滤条件变化时回到第一页。
  useEffect(() => {
    setPage(0);
  }, [detailTab, onlyFailed, verdict, filters]);

  const applyDraft = () =>
    setFilters((f) => ({
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

  // --- 图表数据 ------------------------------------------------------------
  const trendRows = useMemo(
    () =>
      (trends?.series ?? []).map((p) => ({
        date: p.date.slice(5),
        rounds: p.rounds,
        success: toPct(p.success_rate),
        tool: toPct(p.tool_accuracy),
        satisfaction: toPct(p.satisfaction_rate),
        avgSec: p.duration_avg_ms == null ? null : Math.round(p.duration_avg_ms) / 1000,
        p90Sec: p.duration_p90_ms == null ? null : Math.round(p.duration_p90_ms) / 1000,
      })),
    [trends]
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

  const detailTotal = detailTab === "rounds" ? (rounds?.total ?? 0) : (feedback?.total ?? 0);
  const pageCount = Math.max(1, Math.ceil(detailTotal / PAGE));

  const l1 = overview?.l1_outcome;
  const l2 = overview?.l2_efficiency;
  const l3 = overview?.l3_process;
  const l4 = overview?.l4_satisfaction;

  return (
    <>
      {/* --- 过滤工具栏 --------------------------------------------------- */}
      <div style={s.toolbar}>
        <select
          className="adm-select"
          style={s.select}
          value={filters.days}
          aria-label="统计时间窗"
          onChange={(e) => setFilters((f) => ({ ...f, days: Number(e.target.value) }))}
        >
          {DAYS_OPTIONS.map((d) => (
            <option key={d} value={d}>
              最近 {d} 天
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
          {filters.user_id && ` · user=${filters.user_id}`}
          {filters.model_provider && ` · provider=${filters.model_provider}`}
        </span>
      </div>

      {error && <div style={s.errorBanner}>加载失败：{error}</div>}

      {!overview && !error && <div style={s.empty}>{loading ? "加载中…" : "暂无数据"}</div>}

      {overview && (
        <>
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
              反馈明细
            </button>
          </div>

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
            <span style={s.toolbarHint}>
              共 {fmtInt(detailTotal)} 条 · 第 {page + 1}/{pageCount} 页
            </span>
            <button
              className="adm-btn"
              style={{ ...s.btnSmall, ...(page === 0 ? s.btnDisabled : {}) }}
              disabled={page === 0 || detailLoading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              上一页
            </button>
            <button
              className="adm-btn"
              style={{ ...s.btnSmall, ...(page + 1 >= pageCount ? s.btnDisabled : {}) }}
              disabled={page + 1 >= pageCount || detailLoading}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </button>
          </div>

          {detailTab === "rounds" ? (
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
                      <td style={{ ...s.td, ...s.mono }}>{r.user_id}</td>
                      <td style={{ ...s.td, ...s.mono }}>
                        {clip(r.session_id, 10)} #{r.round_seq}
                        {r.is_task && <span style={{ color: "var(--indigo)" }}> ·任务</span>}
                      </td>
                      <td style={s.td}>
                        {r.errored ? (
                          <span title={r.error_text ?? ""} style={{ color: "var(--red)" }}>
                            错误
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
                      <td style={s.td}>{fmtInt(r.total_tokens)}</td>
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
