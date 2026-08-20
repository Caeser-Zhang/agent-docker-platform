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
 *   - actions: view logs, restart, graceful stop, destroy (typed confirm)
 *   - optional 5s auto-refresh
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AdminContainer, type AdminOverview } from "../api";
import { adminStyles as s } from "./adminStyles";

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

const TAIL_OPTIONS = [100, 200, 500, 1000, 2000];

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

  const toastTimer = useRef<number | null>(null);
  const logBoxRef = useRef<HTMLPreElement>(null);

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
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

  const byStatus = overview
    ? Object.entries(overview.containers.by_status)
        .map(([k, v]) => `${k}: ${v}`)
        .join(" · ")
    : "";

  return (
    <div style={s.container}>
      {/* --- Header ------------------------------------------------------- */}
      <div style={s.header}>
        <div style={s.headerLeft}>
          <span style={s.headerIcon}>🛡️</span>
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
          <button style={s.btnPrimary} onClick={onExit}>
            返回聊天
          </button>
          <button style={s.btn} onClick={onLogout}>
            退出
          </button>
        </div>
      </div>

      {/* --- Body --------------------------------------------------------- */}
      <div style={s.body}>
        {error && <div style={s.errorBanner}>加载失败：{error}</div>}

        {overview && (
          <div style={s.cardsRow}>
            <div style={s.card}>
              <div style={s.cardLabel}>用户总数</div>
              <div style={s.cardValue}>{overview.users.total}</div>
              <div style={s.cardSub}>管理员 {overview.users.admins} 名</div>
            </div>
            <div style={s.card}>
              <div style={s.cardLabel}>容器记录</div>
              <div style={s.cardValue}>{overview.containers.records}</div>
              <div style={s.cardSub}>{byStatus || "无记录"}</div>
            </div>
            <div style={s.card}>
              <div style={s.cardLabel}>Docker 运行中</div>
              <div style={overview.containers.docker_running > 0 ? s.cardValueGreen : s.cardValue}>
                {overview.containers.docker_running}
              </div>
              <div style={s.cardSub}>Docker 共 {overview.containers.docker_total} 个</div>
            </div>
            <div style={s.card}>
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

        {/* Toolbar */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "16px",
            flexWrap: "wrap",
            flexShrink: 0,
          }}
        >
          <label style={s.checkboxLabel}>
            <input
              type="checkbox"
              checked={statsEnabled}
              onChange={(e) => setStatsEnabled(e.target.checked)}
            />
            采集 CPU / 内存
          </label>
          <label style={s.checkboxLabel}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            自动刷新（5s）
          </label>
          <button style={s.btn} onClick={() => refresh()}>
            {loading ? "刷新中…" : "立即刷新"}
          </button>
          <span style={{ fontSize: "12px", color: "#52525b" }}>
            销毁会同时删除容器与数据卷，操作需输入容器名确认
          </span>
        </div>

        {/* Container table */}
        <div style={s.tableWrap}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}>用户</th>
                <th style={s.th}>容器 / 镜像</th>
                <th style={s.th}>Docker 状态</th>
                <th style={s.th}>记录状态</th>
                <th style={s.th}>健康</th>
                <th style={s.th}>CPU</th>
                <th style={s.th}>内存</th>
                <th style={s.th}>启动时间</th>
                <th style={s.th}>最近活动</th>
                <th style={s.th}>重启</th>
                <th style={s.th}>操作</th>
              </tr>
            </thead>
            <tbody>
              {containers.map((c) => {
                const absent = c.docker_status === "absent";
                const isBusy = busy === c.user_id;
                const disabled = isBusy || busy !== null;
                return (
                  <tr key={c.user_id}>
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
                      <div style={{ ...s.mono, ...s.muted, fontSize: "11px" }}>{c.image}</div>
                      {c.last_error && (
                        <div
                          style={{ color: "#f87171", fontSize: "11px", marginTop: "4px" }}
                          title={c.last_error}
                        >
                          ⚠ {c.last_error.length > 80 ? c.last_error.slice(0, 80) + "…" : c.last_error}
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
                          style={{ ...s.btnSmall, ...(absent || disabled ? s.btnDisabled : {}) }}
                          disabled={absent || disabled}
                          onClick={() => openLogs(c)}
                        >
                          日志
                        </button>
                        <button
                          style={{ ...s.btnSmallWarn, ...(absent || disabled ? s.btnDisabled : {}) }}
                          disabled={absent || disabled}
                          onClick={() => runOp(c.user_id, () => api.adminRestartContainer(c.user_id))}
                        >
                          {isBusy ? "…" : "重启"}
                        </button>
                        <button
                          style={{ ...s.btnSmall, ...(absent || disabled ? s.btnDisabled : {}) }}
                          disabled={absent || disabled}
                          onClick={() => runOp(c.user_id, () => api.adminStopContainer(c.user_id))}
                        >
                          停止
                        </button>
                        <button
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
          {containers.length === 0 && !loading && <div style={s.empty}>暂无容器记录</div>}
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
              <button style={s.btnSmall} onClick={() => setLogsModal(null)}>
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
              <div style={{ fontSize: "13px", color: "#a1a1aa", marginBottom: "8px" }}>
                请输入容器名 <span style={s.mono}>{destroyModal.containerName}</span> 以确认：
              </div>
              <input
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

      {/* --- Toast ---------------------------------------------------------- */}
      {toast && <div style={s.toast}>{toast}</div>}
    </div>
  );
}
