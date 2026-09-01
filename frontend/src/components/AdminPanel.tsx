/**
 * AdminPanel — platform-wide Docker container management (role=admin only).
 *
 * Mounted by App when the authenticated user has role="admin" and switched
 * to from Chat's header button. Everything here talks to /api/admin/*,
 * which the backend guards with require_admin.
 *
 * Panels:
 *   - overview cards (users, containers, platform limits)
 *   - container table merged from Docker daemon + agent_containers records
 *     (status badges, live CPU/memory sampling, last activity, restarts)
 *   - search (username / uid / container name) + Docker-status & health
 *     filters + CPU / memory sorting (click: desc → asc, cycle)
 *   - multi-select with batch restart / stop / destroy (typed confirm)
 *   - actions: view logs, restart, graceful stop, destroy (typed confirm)
 *   - optional 5s auto-refresh
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type AdminContainer, type AdminOverview } from "../api";
import { adminStyles as s, adminCss } from "./adminStyles";

interface LogsModalState {
  userId: string;
  username: string | null;
  containerName: string;
  tail: number;
  logs: string;
  loading: boolean;
}

interface DestroyModalState {
  userId: string;
  username: string | null;
  containerName: string;
  confirmText: string;
  busy: boolean;
}

/** Batch destroy needs its own typed confirmation across N containers. */
interface BatchDestroyModalState {
  targets: AdminContainer[];
  confirmText: string;
  busy: boolean;
}

type SortKey = "cpu" | "mem";
interface SortState {
  key: SortKey;
  dir: "desc" | "asc";
}

type BatchOp = "restart" | "stop" | "destroy";
interface BatchBusyState {
  op: BatchOp;
  done: number;
  total: number;
}

const TAIL_OPTIONS = [100, 200, 500, 1000, 2000];
const BATCH_OP_LABEL: Record<BatchOp, string> = {
  restart: "重启",
  stop: "停止",
  destroy: "销毁",
};
/** How many containers a batch operates on concurrently. */
const BATCH_CONCURRENCY = 3;

/** "3 分钟前" style relative time, falling back to a locale timestamp. */
function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diff = Date.now() - t;
  if (diff < 0) return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

function Badge({ text, variant }: { text: string; variant: "green" | "yellow" | "red" | "gray" | "blue" }) {
  const v =
    variant === "green" ? s.badgeGreen
    : variant === "yellow" ? s.badgeYellow
    : variant === "red" ? s.badgeRed
    : variant === "blue" ? s.badgeBlue
    : s.badgeGray;
  return <span style={{ ...s.badge, ...v }}>{text}</span>;
}

function dockerVariant(status: string): "green" | "yellow" | "gray" {
  if (status === "running") return "green";
  if (["restarting", "paused", "created", "removing"].includes(status)) return "yellow";
  return "gray"; // exited / dead / absent
}

function dbVariant(status: string): "green" | "yellow" | "red" | "gray" | "blue" {
  if (status === "running") return "green";
  if (["starting", "creating"].includes(status)) return "yellow";
  if (["failed", "error"].includes(status)) return "red";
  if (status === "unmanaged") return "blue";
  return "gray";
}

function healthVariant(h: string | null): "green" | "red" | "gray" {
  if (h === "healthy") return "green";
  if (h === "unhealthy" || h === "starting") return "red";
  return "gray";
}

// --- Inline SVG icons (no emoji per design system) -------------------------
const iconProps = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

function ShieldIcon() {
  return (
    <svg {...iconProps} width={18} height={18}>
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg {...iconProps} width={14} height={14}>
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  );
}

function WarningIcon() {
  return (
    <svg {...iconProps} width={12} height={12}>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/** Sort indicator: active key shows the current direction, idle shows both. */
function SortIcon({ active, dir }: { active: boolean; dir: "desc" | "asc" }) {
  return (
    <span style={active ? s.sortArrow : s.sortArrowIdle} aria-hidden={true}>
      {active ? (
        dir === "desc" ? (
          <svg {...iconProps} width={12} height={12}>
            <path d="m6 9 6 6 6-6" />
          </svg>
        ) : (
          <svg {...iconProps} width={12} height={12}>
            <path d="m18 15-6-6-6 6" />
          </svg>
        )
      ) : (
        <svg {...iconProps} width={12} height={12}>
          <path d="m7 15 5 5 5-5" />
          <path d="m7 9 5-5 5 5" />
        </svg>
      )}
    </span>
  );
}

export function AdminPanel({
  username,
  onLogout,
  onExit,
}: {
  username: string;
  onLogout: () => void;
  onExit: () => void;
}) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [containers, setContainers] = useState<AdminContainer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // user_id being operated on
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [statsEnabled, setStatsEnabled] = useState(true);
  const [toast, setToast] = useState<string | null>(null);
  const [logsModal, setLogsModal] = useState<LogsModalState | null>(null);
  const [destroyModal, setDestroyModal] = useState<DestroyModalState | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  // --- Search / filter / sort state ---------------------------------------
  const [search, setSearch] = useState("");
  const [dockerFilter, setDockerFilter] = useState("all");
  const [healthFilter, setHealthFilter] = useState("all");
  const [sort, setSort] = useState<SortState | null>(null);

  // --- Multi-select & batch state ------------------------------------------
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchBusy, setBatchBusy] = useState<BatchBusyState | null>(null);
  const [batchDestroyModal, setBatchDestroyModal] = useState<BatchDestroyModalState | null>(null);

  const toastTimer = useRef<number | null>(null);
  const logBoxRef = useRef<HTMLPreElement>(null);
  /** Header checkbox needs .indeterminate, which has no JSX prop. */
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  const showToast = useCallback((msg: string, ms = 3200) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), ms);
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [ov, list] = await Promise.all([
        api.getAdminOverview(),
        api.getAdminContainers(statsEnabled),
      ]);
      setOverview(ov);
      setContainers(list.containers);
      setUpdatedAt(new Date());
      setError(null);
      // Drop selections whose container disappeared.
      const ids = new Set(list.containers.map((c) => c.user_id));
      setSelected((prev) => {
        const kept = new Set([...prev].filter((id) => ids.has(id)));
        return kept.size === prev.size ? prev : kept;
      });
    } catch (e: any) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [statsEnabled]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, [autoRefresh, refresh]);

  // Auto-scroll the log box to the bottom whenever new logs land.
  useEffect(() => {
    if (logsModal && logBoxRef.current) {
      logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight;
    }
  }, [logsModal?.logs]);

  // Reflect partial selection into the header checkbox.
  const visibleSelectedCount = useMemo(
    () => (containers.length ? [...selected].filter((id) => containers.some((c) => c.user_id === id)).length : 0),
    [selected, containers]
  );

  // --- Derived: filter options from live data -------------------------------
  const dockerStatusOptions = useMemo(() => {
    const fixed = ["running", "exited", "absent", "restarting", "paused", "created", "removing", "dead"];
    const found = new Set(containers.map((c) => c.docker_status));
    return fixed.filter((v) => found.has(v)).concat([...found].filter((v) => !fixed.includes(v)).sort());
  }, [containers]);

  const healthOptions = useMemo(() => {
    const found = new Set(containers.map((c) => c.health));
    const fixed = ["healthy", "unhealthy", "starting"];
    const opts = fixed.filter((v) => found.has(v));
    if (found.has(null)) opts.push("__none__");
    return opts;
  }, [containers]);

  // --- Derived: filtered + sorted rows --------------------------------------
  const visibleContainers = useMemo(() => {
    const q = search.trim().toLowerCase();
    let rows = containers;
    if (q) {
      rows = rows.filter(
        (c) =>
          (c.username || "").toLowerCase().includes(q) ||
          (c.uid || "").toLowerCase().includes(q) ||
          c.container_name.toLowerCase().includes(q)
      );
    }
    if (dockerFilter !== "all") rows = rows.filter((c) => c.docker_status === dockerFilter);
    if (healthFilter !== "all") {
      rows = rows.filter((c) => (healthFilter === "__none__" ? !c.health : c.health === healthFilter));
    }
    if (sort) {
      const val = (c: AdminContainer) =>
        sort.key === "cpu"
          ? c.stats?.cpu_percent ?? -Infinity
          : c.stats?.mem_usage_mb ?? -Infinity;
      // Rows without a sample always sink to the bottom, regardless of dir.
      rows = [...rows].sort((a, b) => {
        const va = val(a);
        const vb = val(b);
        if (va === -Infinity && vb === -Infinity) return 0;
        if (va === -Infinity) return 1;
        if (vb === -Infinity) return -1;
        return sort.dir === "desc" ? vb - va : va - vb;
      });
    }
    return rows;
  }, [containers, search, dockerFilter, healthFilter, sort]);

  /** Clicking CPU/内存: first click desc, second asc, then cycle. */
  const toggleSort = (key: SortKey) => {
    setSort((prev) =>
      !prev || prev.key !== key ? { key, dir: "desc" } : { key, dir: prev.dir === "desc" ? "asc" : "desc" }
    );
  };

  // --- Selection helpers -----------------------------------------------------
  const toggleRow = (userId: string, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(userId);
      else next.delete(userId);
      return next;
    });
  };

  const allVisibleSelected =
    visibleContainers.length > 0 && visibleContainers.every((c) => selected.has(c.user_id));

  const toggleSelectAll = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        visibleContainers.forEach((c) => next.delete(c.user_id));
      } else {
        visibleContainers.forEach((c) => next.add(c.user_id));
      }
      return next;
    });
  };

  // Keep the native checkbox's indeterminate flag in sync.
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = visibleSelectedCount > 0 && !allVisibleSelected;
    }
  }, [visibleSelectedCount, allVisibleSelected, visibleContainers.length]);

  // --- Single-row operations ---------------------------------------------------
  const runOp = async (
    userId: string,
    fn: () => Promise<{ ok: boolean; message: string }>
  ) => {
    setBusy(userId);
    try {
      const r = await fn();
      showToast(r.message || "操作完成");
      await refresh();
    } catch (e: any) {
      showToast(`操作失败: ${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const openLogs = async (c: AdminContainer) => {
    setLogsModal({
      userId: c.user_id,
      username: c.username,
      containerName: c.container_name,
      tail: 200,
      logs: "",
      loading: true,
    });
    try {
      const r = await api.getAdminContainerLogs(c.user_id, 200);
      setLogsModal((m) => (m && m.userId === c.user_id ? { ...m, logs: r.logs, loading: false } : m));
    } catch (e: any) {
      setLogsModal((m) =>
        m && m.userId === c.user_id ? { ...m, logs: `加载日志失败: ${e.message}`, loading: false } : m
      );
    }
  };

  const reloadLogs = async (tail: number) => {
    if (!logsModal) return;
    setLogsModal({ ...logsModal, tail, loading: true });
    try {
      const r = await api.getAdminContainerLogs(logsModal.userId, tail);
      setLogsModal((m) => (m ? { ...m, logs: r.logs, loading: false } : m));
    } catch (e: any) {
      setLogsModal((m) => (m ? { ...m, logs: `加载日志失败: ${e.message}`, loading: false } : m));
    }
  };

  const confirmDestroy = async () => {
    if (!destroyModal) return;
    setDestroyModal({ ...destroyModal, busy: true });
    try {
      const r = await api.adminDestroyContainer(destroyModal.userId);
      showToast(r.message || "容器已销毁");
      setDestroyModal(null);
      await refresh();
    } catch (e: any) {
      showToast(`销毁失败: ${e.message}`);
      setDestroyModal((m) => (m ? { ...m, busy: false } : m));
    }
  };

  // --- Batch operations ---------------------------------------------------------
  /** Run one op over all selected containers with bounded concurrency. */
  const runBatch = async (op: BatchOp) => {
    const ids = [...selected];
    if (ids.length === 0 || batchBusy) return;
    const call =
      op === "restart" ? api.adminRestartContainer
      : op === "stop" ? api.adminStopContainer
      : api.adminDestroyContainer;

    setBatchBusy({ op, done: 0, total: ids.length });
    let ok = 0;
    const errors: string[] = [];
    let cursor = 0;

    const worker = async () => {
      while (cursor < ids.length) {
        const id = ids[cursor++];
        try {
          await call(id);
          ok++;
        } catch (e: any) {
          const c = containers.find((x) => x.user_id === id);
          errors.push(`${c?.container_name || id}: ${e.message}`);
        }
        setBatchBusy((b) => (b ? { ...b, done: b.done + 1 } : b));
      }
    };
    await Promise.all(Array.from({ length: Math.min(BATCH_CONCURRENCY, ids.length) }, worker));

    setBatchBusy(null);
    setSelected(new Set());
    setBatchDestroyModal(null);
    const label = BATCH_OP_LABEL[op];
    showToast(
      errors.length === 0
        ? `批量${label}完成：${ok} 个容器`
        : `批量${label}完成：成功 ${ok} / 失败 ${errors.length}（${errors[0]}${errors.length > 1 ? " 等" : ""}）`,
      errors.length === 0 ? 3200 : 6000
    );
    await refresh();
  };

  const byStatus = overview
    ? Object.entries(overview.containers.by_status)
        .map(([k, v]) => `${k}: ${v}`)
        .join(" · ")
    : "";

  const anyBusy = busy !== null || batchBusy !== null;
  const filtered = visibleContainers.length !== containers.length;

  return (
    <div style={s.container}>
      <style>{adminCss}</style>

      {/* --- Header ------------------------------------------------------- */}
      <div style={s.header}>
        <div style={s.headerLeft}>
          <span style={s.headerIcon}>
            <ShieldIcon />
          </span>
          <div>
            <div style={s.headerTitle}>Docker 容器管理</div>
            <div style={s.headerSubtitle}>
              平台级容器状态与操作 · 管理员专属
              {updatedAt && ` · 更新于 ${updatedAt.toLocaleTimeString("zh-CN", { hour12: false })}`}
            </div>
          </div>
        </div>
        <div style={s.headerRight}>
          <span style={s.whoami}>
            {username} · <span style={{ color: "#fbbf24" }}>admin</span>
          </span>
          <button className="adm-btn adm-btn-primary" style={s.btnPrimary} onClick={onExit}>
            返回聊天
          </button>
          <button className="adm-btn" style={s.btn} onClick={onLogout}>
            退出
          </button>
        </div>
      </div>

      {/* --- Body --------------------------------------------------------- */}
      <div style={{ ...s.body }} className="adm-scroll">
        {error && <div style={s.errorBanner}>加载失败：{error}</div>}

        {overview && (
          <div style={s.cardsRow}>
            <div className="adm-card" style={s.card}>
              <div style={s.cardLabel}>用户总数</div>
              <div style={s.cardValue}>{overview.users.total}</div>
              <div style={s.cardSub}>管理员 {overview.users.admins} 名</div>
            </div>
            <div className="adm-card" style={s.card}>
              <div style={s.cardLabel}>容器记录</div>
              <div style={s.cardValue}>{overview.containers.records}</div>
              <div style={s.cardSub}>{byStatus || "无记录"}</div>
            </div>
            <div className="adm-card" style={s.card}>
              <div style={s.cardLabel}>Docker 运行中</div>
              <div style={overview.containers.docker_running > 0 ? s.cardValueGreen : s.cardValue}>
                {overview.containers.docker_running}
              </div>
              <div style={s.cardSub}>Docker 共 {overview.containers.docker_total} 个</div>
            </div>
            <div className="adm-card" style={s.card}>
              <div style={s.cardLabel}>单容器限额</div>
              <div style={s.cardValue}>
                <span style={{ fontSize: "16px" }}>
                  {overview.platform.cpu_limit} CPU / {overview.platform.memory_limit}
                </span>
              </div>
              <div style={s.cardSub}>{overview.platform.image}</div>
            </div>
          </div>
        )}

        {/* Toolbar: search + filters + switches */}
        <div style={s.toolbar}>
          <div className="adm-search" style={s.searchWrap}>
            <span style={s.searchIcon}>
              <SearchIcon />
            </span>
            <input
              className="adm-search"
              style={s.search}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索 用户名 / 工号 / 容器名"
              aria-label="搜索用户名、工号或容器名"
            />
          </div>
          <select
            className="adm-select"
            style={s.select}
            value={dockerFilter}
            onChange={(e) => setDockerFilter(e.target.value)}
            aria-label="按 Docker 状态筛选"
          >
            <option value="all">Docker 状态：全部</option>
            {dockerStatusOptions.map((v) => (
              <option key={v} value={v}>
                {v === "absent" ? "absent（无容器）" : v}
              </option>
            ))}
          </select>
          <select
            className="adm-select"
            style={s.select}
            value={healthFilter}
            onChange={(e) => setHealthFilter(e.target.value)}
            aria-label="按健康状态筛选"
          >
            <option value="all">健康：全部</option>
            {healthOptions.map((v) => (
              <option key={v} value={v}>
                {v === "__none__" ? "无健康检查" : v}
              </option>
            ))}
          </select>
          <label style={s.checkboxLabel}>
            <input
              className="adm-check"
              type="checkbox"
              checked={statsEnabled}
              onChange={(e) => setStatsEnabled(e.target.checked)}
            />
            采集 CPU / 内存
          </label>
          <label style={s.checkboxLabel}>
            <input
              className="adm-check"
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            自动刷新（5s）
          </label>
          <button className="adm-btn" style={s.btn} onClick={() => refresh()}>
            {loading ? "刷新中…" : "立即刷新"}
          </button>
          {filtered && (
            <span style={{ fontSize: "12px", color: "#818cf8" }}>
              显示 {visibleContainers.length} / {containers.length}
            </span>
          )}
        </div>

        {/* Batch action bar — appears once anything is selected */}
        {visibleSelectedCount > 0 && (
          <div style={s.batchBar}>
            <span style={s.batchCount}>已选 {visibleSelectedCount} 项</span>
            {batchBusy && (
              <span style={s.batchProgress}>
                批量{BATCH_OP_LABEL[batchBusy.op]}中… {batchBusy.done}/{batchBusy.total}
              </span>
            )}
            <button
              className="adm-btn adm-btn-warn"
              style={{ ...s.btnSmallWarn, ...{ padding: "6px 14px" }, ...(batchBusy ? s.btnDisabled : {}) }}
              disabled={!!batchBusy}
              onClick={() => runBatch("restart")}
            >
              批量重启
            </button>
            <button
              className="adm-btn"
              style={{ ...s.btnSmall, ...{ padding: "6px 14px" }, ...(batchBusy ? s.btnDisabled : {}) }}
              disabled={!!batchBusy}
              onClick={() => runBatch("stop")}
            >
              批量停止
            </button>
            <button
              className="adm-btn adm-btn-danger"
              style={{ ...s.btnSmallDanger, ...{ padding: "6px 14px" }, ...(batchBusy ? s.btnDisabled : {}) }}
              disabled={!!batchBusy}
              onClick={() =>
                setBatchDestroyModal({
                  targets: containers.filter((c) => selected.has(c.user_id)),
                  confirmText: "",
                  busy: false,
                })
              }
            >
              批量销毁
            </button>
            <div style={s.batchSpacer}>
              <button
                className="adm-btn"
                style={{ ...s.btnSmall, ...(batchBusy ? s.btnDisabled : {}) }}
                disabled={!!batchBusy}
                onClick={toggleSelectAll}
              >
                {allVisibleSelected ? "取消全选" : "全选本页"}
              </button>
              <button
                className="adm-btn"
                style={{ ...s.btnSmall, ...(batchBusy ? s.btnDisabled : {}) }}
                disabled={!!batchBusy}
                onClick={() => setSelected(new Set())}
              >
                清除选择
              </button>
            </div>
          </div>
        )}

        {/* Container table */}
        <div className="adm-scroll" style={s.tableWrap}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.thCheck}>
                  <input
                    ref={selectAllRef}
                    className="adm-check"
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleSelectAll}
                    aria-label="全选当前列表"
                  />
                </th>
                <th style={s.th}>工号</th>
                <th style={s.th}>用户</th>
                <th style={s.th}>容器 / 镜像</th>
                <th style={s.th}>Docker 状态</th>
                <th style={s.th}>记录状态</th>
                <th style={s.th}>健康</th>
                <th style={s.th}>
                  <button
                    className="adm-th-sort"
                    style={s.sortLabel}
                    onClick={() => toggleSort("cpu")}
                    aria-label="按 CPU 排序"
                    title="点击排序：先降序，再升序"
                  >
                    CPU <SortIcon active={sort?.key === "cpu"} dir={sort?.dir ?? "desc"} />
                  </button>
                </th>
                <th style={s.th}>
                  <button
                    className="adm-th-sort"
                    style={s.sortLabel}
                    onClick={() => toggleSort("mem")}
                    aria-label="按内存排序"
                    title="点击排序：先降序，再升序"
                  >
                    内存 <SortIcon active={sort?.key === "mem"} dir={sort?.dir ?? "desc"} />
                  </button>
                </th>
                <th style={s.th}>启动时间</th>
                <th style={s.th}>最近活动</th>
                <th style={s.th}>重启</th>
                <th style={s.th}>操作</th>
              </tr>
            </thead>
            <tbody>
              {visibleContainers.map((c) => {
                const absent = c.docker_status === "absent";
                const isBusy = busy === c.user_id;
                const disabled = isBusy || anyBusy;
                const checked = selected.has(c.user_id);
                return (
                  <tr
                    key={c.user_id}
                    className={checked ? "adm-row adm-row-selected" : "adm-row"}
                  >
                    <td style={s.tdCheck}>
                      <input
                        className="adm-check"
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => toggleRow(c.user_id, e.target.checked)}
                        aria-label={`选择 ${c.container_name}`}
                      />
                    </td>
                    <td style={s.td}>
                      {c.uid ? (
                        <span style={s.uidText}>{c.uid}</span>
                      ) : (
                        <span style={s.muted}>—</span>
                      )}
                    </td>
                    <td style={s.td}>
                      <div style={s.userCell}>
                        <span style={s.avatar}>
                          {(c.username || "?")[0]?.toUpperCase()}
                        </span>
                        <span>{c.username || <span style={s.muted}>已删除用户</span>}</span>
                      </div>
                    </td>
                    <td style={s.tdWrap}>
                      <div style={s.mono}>{c.container_name}</div>
                      <div style={{ ...s.mono, ...s.muted, fontSize: "11px", display: "flex", gap: "6px", alignItems: "center" }}>
                        <span>{c.image}</span>
                        {c.image_id && (
                          <span title={`镜像 ID ${c.image_id}`} style={{ opacity: 0.75 }}>
                            @{c.image_id.replace(/^sha256:/, "").slice(0, 12)}
                          </span>
                        )}
                        {c.image_stale && (
                          <span
                            className="adm-badge adm-badge-warn"
                            title="容器运行的镜像与当前镜像不一致（镜像已重建）— 点击「更新镜像」以应用"
                            style={{ fontSize: "10px", padding: "1px 6px", borderRadius: "999px", background: "#78350f", color: "#fbbf24" }}
                          >
                            镜像过期
                          </span>
                        )}
                      </div>
                      {c.last_error && (
                        <div
                          style={{ color: "#f87171", fontSize: "11px", marginTop: "4px", display: "flex", gap: "4px", alignItems: "center" }}
                          title={c.last_error}
                        >
                          <WarningIcon />
                          {c.last_error.length > 80 ? c.last_error.slice(0, 80) + "…" : c.last_error}
                        </div>
                      )}
                    </td>
                    <td style={s.td}>
                      <Badge text={c.docker_status} variant={dockerVariant(c.docker_status)} />
                    </td>
                    <td style={s.td}>
                      <Badge text={c.db_status} variant={dbVariant(c.db_status)} />
                    </td>
                    <td style={s.td}>
                      {c.health ? (
                        <Badge text={c.health} variant={healthVariant(c.health)} />
                      ) : (
                        <span style={s.muted}>—</span>
                      )}
                    </td>
                    <td style={s.td}>
                      {c.stats ? `${c.stats.cpu_percent.toFixed(1)}%` : <span style={s.muted}>—</span>}
                    </td>
                    <td style={s.td}>
                      {c.stats ? (
                        <span title={`内存占用 ${c.stats.mem_percent.toFixed(1)}%`}>
                          {c.stats.mem_usage_mb} / {c.stats.mem_limit_mb} MB
                        </span>
                      ) : (
                        <span style={s.muted}>—</span>
                      )}
                    </td>
                    <td style={s.td}>{fmtTime(c.started_at)}</td>
                    <td style={s.td}>{fmtTime(c.last_activity)}</td>
                    <td style={s.td}>{c.restart_count}</td>
                    <td style={s.td}>
                      <div style={s.actionsCell}>
                        <button
                          className="adm-btn"
                          style={{ ...s.btnSmall, ...(absent || disabled ? s.btnDisabled : {}) }}
                          disabled={absent || disabled}
                          onClick={() => openLogs(c)}
                        >
                          日志
                        </button>
                        <button
                          className="adm-btn adm-btn-warn"
                          style={{ ...s.btnSmallWarn, ...(absent || disabled ? s.btnDisabled : {}) }}
                          disabled={absent || disabled}
                          onClick={() => runOp(c.user_id, () => api.adminRestartContainer(c.user_id))}
                        >
                          {isBusy ? "…" : "重启"}
                        </button>
                        <button
                          className="adm-btn"
                          style={{
                            ...s.btnSmall,
                            ...(absent || disabled ? s.btnDisabled : {}),
                            ...(c.image_stale && !disabled ? { borderColor: "#fbbf24", color: "#fbbf24" } : {}),
                          }}
                          disabled={absent || disabled}
                          title="删除容器并用当前镜像重建（工作区/数据卷保留，容器会短暂重启）"
                          onClick={() => {
                            if (window.confirm(`用当前镜像重建 ${c.container_name}？工作区和数据会保留，容器将短暂中断。`)) {
                              runOp(c.user_id, () => api.adminRecreateContainer(c.user_id));
                            }
                          }}
                        >
                          更新镜像
                        </button>
                        <button
                          className="adm-btn"
                          style={{ ...s.btnSmall, ...(absent || disabled ? s.btnDisabled : {}) }}
                          disabled={absent || disabled}
                          onClick={() => runOp(c.user_id, () => api.adminStopContainer(c.user_id))}
                        >
                          停止
                        </button>
                        <button
                          className="adm-btn adm-btn-danger"
                          style={{ ...s.btnSmallDanger, ...(disabled ? s.btnDisabled : {}) }}
                          disabled={disabled}
                          onClick={() =>
                            setDestroyModal({
                              userId: c.user_id,
                              username: c.username,
                              containerName: c.container_name,
                              confirmText: "",
                              busy: false,
                            })
                          }
                        >
                          销毁
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {visibleContainers.length === 0 && !loading && (
            <div style={s.empty}>{filtered ? "没有匹配的容器" : "暂无容器记录"}</div>
          )}
          {loading && containers.length === 0 && <div style={s.empty}>加载中…</div>}
        </div>
      </div>

      {/* --- Logs modal ----------------------------------------------------- */}
      {logsModal && (
        <div style={s.overlay} onClick={() => setLogsModal(null)}>
          <div style={s.modal} onClick={(e) => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <div style={s.modalTitle}>
                容器日志 — {logsModal.username || logsModal.userId}
                <span style={{ ...s.mono, ...s.muted, marginLeft: "8px" }}>
                  {logsModal.containerName}
                </span>
              </div>
              <button className="adm-btn" style={s.btnSmall} onClick={() => setLogsModal(null)}>
                关闭
              </button>
            </div>
            <div style={s.modalBody}>
              <pre ref={logBoxRef} style={s.logBox}>
                {logsModal.loading ? "加载中…" : logsModal.logs || "（无日志）"}
              </pre>
            </div>
            <div style={s.modalFooter}>
              <label style={s.checkboxLabel}>
                行数
                <select
                  className="adm-select"
                  style={s.select}
                  value={logsModal.tail}
                  onChange={(e) => reloadLogs(Number(e.target.value))}
                >
                  {TAIL_OPTIONS.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="adm-btn"
                style={{ ...s.btnSmall, ...(logsModal.loading ? s.btnDisabled : {}) }}
                disabled={logsModal.loading}
                onClick={() => reloadLogs(logsModal.tail)}
              >
                刷新日志
              </button>
            </div>
          </div>
        </div>
      )}

      {/* --- Destroy confirmation modal -------------------------------------- */}
      {destroyModal && (
        <div style={s.overlay} onClick={() => !destroyModal.busy && setDestroyModal(null)}>
          <div style={s.modal} onClick={(e) => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <div style={s.modalTitle}>销毁容器</div>
              <button
                className="adm-btn"
                style={{ ...s.btnSmall, ...(destroyModal.busy ? s.btnDisabled : {}) }}
                disabled={destroyModal.busy}
                onClick={() => setDestroyModal(null)}
              >
                取消
              </button>
            </div>
            <div style={s.modalBody}>
              <div style={s.warnText}>
                即将销毁用户 <b>{destroyModal.username || destroyModal.userId}</b> 的容器
                <span style={s.mono}> {destroyModal.containerName} </span>
                及其全部数据卷。该操作<b>不可恢复</b>，用户的工作区文件与 opencode
                会话历史将被永久删除。用户下次进入聊天时容器会重新创建。
              </div>
              <div style={{ fontSize: "13px", color: "#94a3b8", marginBottom: "8px" }}>
                请输入容器名 <span style={s.mono}>{destroyModal.containerName}</span> 以确认：
              </div>
              <input
                className="adm-input"
                style={s.input}
                value={destroyModal.confirmText}
                onChange={(e) => setDestroyModal({ ...destroyModal, confirmText: e.target.value })}
                placeholder={destroyModal.containerName}
                disabled={destroyModal.busy}
                autoFocus
              />
            </div>
            <div style={s.modalFooter}>
              <button
                className="adm-btn adm-btn-danger"
                style={{ ...s.btnDanger, ...(destroyModal.busy ? s.btnDisabled : {}) }}
                disabled={destroyModal.confirmText !== destroyModal.containerName || destroyModal.busy}
                onClick={confirmDestroy}
              >
                {destroyModal.busy ? "销毁中…" : "确认销毁"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* --- Batch destroy confirmation modal --------------------------------- */}
      {batchDestroyModal && (
        <div style={s.overlay} onClick={() => !batchDestroyModal.busy && !batchBusy && setBatchDestroyModal(null)}>
          <div style={{ ...s.modal, width: "min(640px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <div style={s.modalTitle}>批量销毁 {batchDestroyModal.targets.length} 个容器</div>
              <button
                className="adm-btn"
                style={{ ...s.btnSmall, ...(batchBusy ? s.btnDisabled : {}) }}
                disabled={!!batchBusy}
                onClick={() => setBatchDestroyModal(null)}
              >
                取消
              </button>
            </div>
            <div style={s.modalBody}>
              <div style={s.warnText}>
                即将销毁以下 <b>{batchDestroyModal.targets.length}</b> 个容器及其<b>全部数据卷</b>。
                该操作<b>不可恢复</b>，相关用户的工作区文件与 opencode 会话历史将被永久删除。
              </div>
              <div className="adm-scroll" style={s.destroyList}>
                {batchDestroyModal.targets.map((t) => (
                  <div key={t.user_id}>
                    <span style={s.mono}>{t.container_name}</span>
                    <span style={{ ...s.muted, marginLeft: "8px" }}>
                      {t.uid ? `工号 ${t.uid} · ` : ""}
                      {t.username || "已删除用户"}
                    </span>
                  </div>
                ))}
              </div>
              <div style={{ fontSize: "13px", color: "#94a3b8", marginBottom: "8px" }}>
                请输入 <b style={{ color: "#f87171" }}>销毁</b> 二字以确认：
              </div>
              <input
                className="adm-input"
                style={s.input}
                value={batchDestroyModal.confirmText}
                onChange={(e) =>
                  setBatchDestroyModal({ ...batchDestroyModal, confirmText: e.target.value })
                }
                placeholder="销毁"
                disabled={batchDestroyModal.busy || !!batchBusy}
                autoFocus
              />
            </div>
            <div style={s.modalFooter}>
              <button
                className="adm-btn adm-btn-danger"
                style={{ ...s.btnDanger, ...((batchDestroyModal.busy || batchBusy) ? s.btnDisabled : {}) }}
                disabled={batchDestroyModal.confirmText !== "销毁" || batchDestroyModal.busy || !!batchBusy}
                onClick={() => {
                  setBatchDestroyModal((m) => (m ? { ...m, busy: true } : m));
                  runBatch("destroy");
                }}
              >
                {batchBusy ? `销毁中… ${batchBusy.done}/${batchBusy.total}` : "确认批量销毁"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* --- Toast ---------------------------------------------------------- */}
      {toast && (
        <div className="adm-toast" style={s.toast}>
          {toast}
        </div>
      )}
    </div>
  );
}
