/**
 * 知识库权限矩阵（管理员专属）。
 *
 * 后端把「用户 × 知识库」的白名单存在 kb_grants 这张 junction 表里，撤销是软删除
 * （盖 revoked_at 时间戳而非删行），所以重新授权走的是「复活」而不是新插入。本页
 * 只做三件事，全部对着 /api/admin/kb-* 这组只读+写接口：
 *
 *   1. 矩阵查看 —— 行是用户、列是已录入凭据的知识库，格子显示是否已授权；
 *   2. 授权 / 撤销 —— 点格子或在选定用户的两张列表里点按钮即可，立即生效
 *      （代理与徽章通路每次请求都重读 kb_grants，无需重建容器）；
 *   3. 分别查询选定用户的「已授权」与「未授权」列表 —— 走 kb-user-access，
 *      未授权项若尚未录入凭据会被禁用（上游没有可注入的 Key，授权必然失败）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type KbGrantInfo,
  type KbKeyInfo,
  type KbUser,
  type KbUserAccess,
} from "../api";
import { adminStyles as adm, adminCss } from "./adminStyles";

export function KbAccessAdminPage({
  username,
  onLogout,
  onExit,
}: {
  username: string;
  onLogout: () => void;
  onExit: () => void;
}) {
  const [users, setUsers] = useState<KbUser[]>([]);
  const [keys, setKeys] = useState<KbKeyInfo[]>([]);
  const [grants, setGrants] = useState<KbGrantInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [access, setAccess] = useState<KbUserAccess | null>(null);
  // 凭据录入表单（kb_keys）：物理库名 + API Key。
  const [newKbName, setNewKbName] = useState("");
  const [newApiKey, setNewApiKey] = useState("");
  const toastTimer = useRef<number | null>(null);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  useEffect(
    () => () => {
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    },
    []
  );

  const loadMatrix = useCallback(async () => {
    setLoading(true);
    const [u, k, g] = await Promise.allSettled([
      api.adminListKbUsers(),
      api.adminListKbKeys(),
      api.adminListKbGrants(),
    ]);
    const errs: string[] = [];
    if (u.status === "fulfilled") setUsers(u.value.items ?? []);
    else errs.push(`用户：${u.reason?.message ?? u.reason}`);
    if (k.status === "fulfilled") setKeys(k.value.items ?? []);
    else errs.push(`知识库：${k.reason?.message ?? k.reason}`);
    if (g.status === "fulfilled") setGrants(g.value.items ?? []);
    else errs.push(`授权：${g.reason?.message ?? g.reason}`);
    setError(errs.length ? errs.join("；") : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    loadMatrix();
  }, [loadMatrix]);

  // 选定用户后拉取「已授权 / 未授权」两张列表。
  const loadAccess = useCallback(async (userId: string) => {
    if (!userId) {
      setAccess(null);
      return;
    }
    try {
      setAccess(await api.adminKbUserAccess(userId));
    } catch (e: any) {
      flash(`读取用户授权失败：${e.message}`);
      setAccess(null);
    }
  }, [flash]);

  useEffect(() => {
    loadAccess(selected);
  }, [selected, loadAccess]);

  // 默认选中第一个用户，省得管理员再点一次。
  useEffect(() => {
    if (!selected && users.length) setSelected(users[0].user_id);
  }, [users, selected]);

  /** `${user_id}|${kb_name}` 集合，矩阵格子 O(1) 判定是否已授权。 */
  const grantSet = useMemo(
    () => new Set(grants.map((g) => `${g.user_id}|${g.kb_name}`)),
    [grants]
  );

  const refreshAfterMutation = useCallback(async () => {
    await Promise.all([loadMatrix(), loadAccess(selected)]);
  }, [loadMatrix, loadAccess, selected]);

  const doGrant = useCallback(
    async (user: KbUser, kbName: string) => {
      setBusy(`grant:${user.user_id}|${kbName}`);
      try {
        await api.adminGrantKb(user.username, kbName);
        flash(`已授权 ${user.username} 访问「${kbName}」`);
        await refreshAfterMutation();
      } catch (e: any) {
        flash(`授权失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [flash, refreshAfterMutation]
  );

  const doRevoke = useCallback(
    async (user: KbUser, kbName: string) => {
      setBusy(`revoke:${user.user_id}|${kbName}`);
      try {
        await api.adminRevokeKb(user.user_id, kbName);
        flash(`已撤销 ${user.username} 对「${kbName}」的访问（软删除，可重新授权）`);
        await refreshAfterMutation();
      } catch (e: any) {
        flash(`撤销失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [flash, refreshAfterMutation]
  );

  /** 录入 / 轮换某个物理库的 API Key（幂等 upsert，加密存库、永不回显）。 */
  const doPutKey = useCallback(async () => {
    const name = newKbName.trim();
    const key = newApiKey.trim();
    if (!name || !key) {
      flash("库名和 API Key 都不能为空");
      return;
    }
    setBusy(`putkey:${name}`);
    try {
      await api.adminPutKbKey(name, key);
      flash(`已录入 / 轮换「${name}」的凭据`);
      setNewKbName("");
      setNewApiKey("");
      await refreshAfterMutation();
    } catch (e: any) {
      flash(`录入失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [newKbName, newApiKey, flash, refreshAfterMutation]);

  /** 删除某个库的凭据（后端会连带撤销该库的所有授权）。 */
  const doDeleteKey = useCallback(
    async (name: string) => {
      if (!window.confirm(`删除「${name}」的凭据？该库的所有用户授权也会被一并撤销。`)) {
        return;
      }
      setBusy(`delkey:${name}`);
      try {
        await api.adminDeleteKbKey(name);
        flash(`已删除「${name}」的凭据`);
        await refreshAfterMutation();
      } catch (e: any) {
        flash(`删除失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [flash, refreshAfterMutation]
  );

  const selectedUser = users.find((u) => u.user_id === selected) ?? null;
  const totalGranted = grants.length;

  return (
    <div style={adm.container}>
      <style>{adminCss}</style>

      {/* --- Header ------------------------------------------------------- */}
      <div style={adm.header}>
        <div style={adm.headerLeft}>
          <span style={adm.headerIcon}>
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M3 9h18M3 15h18M9 3v18M15 3v18" />
            </svg>
          </span>
          <div>
            <div style={adm.headerTitle}>知识库权限矩阵</div>
            <div style={adm.headerSubtitle}>
              用户 × 知识库白名单 · 撤销为软删除 · 授权即时生效 · 管理员专属
            </div>
          </div>
        </div>
        <div style={adm.headerRight}>
          <span style={adm.whoami}>
            {username} · <span style={{ color: "var(--amber)" }}>admin</span>
          </span>
          <button className="adm-btn adm-btn-primary" style={adm.btnPrimary} onClick={onExit}>
            返回聊天
          </button>
          <button className="adm-btn" style={adm.btn} onClick={onLogout}>
            退出
          </button>
        </div>
      </div>

      {/* --- Body --------------------------------------------------------- */}
      <div style={adm.body} className="adm-scroll">
        {error && <div style={adm.errorBanner}>加载失败：{error}</div>}

        <div style={adm.cardsRow}>
          <div className="adm-card" style={adm.card}>
            <div style={adm.cardLabel}>用户</div>
            <div style={adm.cardValue}>{users.length}</div>
            <div style={adm.cardSub}>矩阵的行</div>
          </div>
          <div className="adm-card" style={adm.card}>
            <div style={adm.cardLabel}>知识库</div>
            <div style={adm.cardValue}>{keys.length}</div>
            <div style={adm.cardSub}>
              已录入凭据 {keys.filter((k) => k.has_api_key).length} · 矩阵的列
            </div>
          </div>
          <div className="adm-card" style={adm.card}>
            <div style={adm.cardLabel}>生效授权</div>
            <div style={adm.cardValueGreen}>{totalGranted}</div>
            <div style={adm.cardSub}>仅统计未撤销（revoked_at 为空）的白名单行</div>
          </div>
        </div>

        {/* --- 知识库凭据管理（录入 / 轮换 / 删除 kb_keys）------------------ */}
        <div className="adm-card" style={adm.card}>
          <div style={adm.cardLabel}>知识库凭据管理 · 已录入 {keys.filter((k) => k.has_api_key).length} / {keys.length}</div>
          <div style={{ ...adm.muted, fontSize: "12px", margin: "6px 0 12px" }}>
            录入 fastk 服务器的<strong>物理库名</strong>及其 API Key（Fernet 加密存库、永不回显）。
            下方权限矩阵的「列」即来自这里；未录入凭据的库无法授权。
          </div>

          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "8px",
              alignItems: "center",
              marginBottom: "12px",
            }}
          >
            <input
              className="adm-select"
              style={{ ...adm.select, minWidth: "180px" }}
              placeholder="物理库名，如 fastdb"
              value={newKbName}
              onChange={(e) => setNewKbName(e.target.value)}
            />
            <input
              className="adm-select"
              style={{ ...adm.select, minWidth: "280px" }}
              type="password"
              placeholder="API Key"
              value={newApiKey}
              onChange={(e) => setNewApiKey(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") doPutKey();
              }}
            />
            <button
              className="adm-btn adm-btn-primary"
              style={adm.btnPrimary}
              disabled={busy !== null || !newKbName.trim() || !newApiKey.trim()}
              onClick={doPutKey}
            >
              {busy?.startsWith("putkey:") ? "录入中…" : "录入 / 轮换"}
            </button>
          </div>

          {keys.length === 0 ? (
            <div style={adm.empty}>尚未录入任何知识库凭据 —— 在上方填写库名与 Key 后点「录入 / 轮换」</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
              {keys.map((k) => (
                <div
                  key={k.kb_name}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    padding: "6px 8px",
                    border: "1px solid var(--border)",
                    borderRadius: "8px",
                  }}
                >
                  <span style={adm.mono}>{k.kb_name}</span>
                  {k.has_api_key ? (
                    <span style={{ ...adm.badge, ...adm.badgeGreen }}>已录入</span>
                  ) : (
                    <span style={{ ...adm.badge, ...adm.badgeYellow }}>缺凭据</span>
                  )}
                  <span style={{ ...adm.muted, fontSize: "11px", marginLeft: "auto" }}>
                    {k.updated_at ? `更新于 ${k.updated_at.slice(0, 10)}` : ""}
                  </span>
                  <button
                    className="adm-btn adm-btn-danger"
                    style={adm.btnSmallDanger}
                    disabled={busy !== null}
                    onClick={() => doDeleteKey(k.kb_name)}
                  >
                    {busy === `delkey:${k.kb_name}` ? "…" : "删除"}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Toolbar */}
        <div style={adm.toolbar}>
          <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span style={adm.cardLabel}>选定用户</span>
            <select
              className="adm-select"
              style={adm.select}
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">— 请选择 —</option>
              {users.map((u) => (
                <option key={u.user_id} value={u.user_id}>
                  {u.username}
                  {u.uid ? `（工号 ${u.uid}）` : ""}
                  {u.role === "admin" ? " · admin" : ""}
                </option>
              ))}
            </select>
          </label>
          <button
            className="adm-btn"
            style={adm.btn}
            onClick={loadMatrix}
            disabled={loading || busy !== null}
          >
            刷新
          </button>
          <span style={adm.toolbarHint}>
            点击矩阵格子即可授权 / 撤销；撤销是软删除，重新授权会复活原行
          </span>
        </div>

        {/* --- 选定用户的已授权 / 未授权列表 ------------------------------ */}
        {selectedUser && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
            <div className="adm-card" style={adm.card}>
              <div style={adm.cardLabel}>
                已授权知识库 · {access?.granted.length ?? 0}
              </div>
              {!access && <div style={adm.empty}>加载中…</div>}
              {access && access.granted.length === 0 && (
                <div style={adm.empty}>该用户暂无任何知识库授权</div>
              )}
              {access && access.granted.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  {access.granted.map((g) => (
                    <div
                      key={g.kb_name}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                        padding: "6px 8px",
                        border: "1px solid var(--border)",
                        borderRadius: "8px",
                      }}
                    >
                      <span style={{ ...adm.badge, ...adm.badgeGreen }}>已授权</span>
                      <span style={adm.mono}>{g.kb_name}</span>
                      <span style={{ ...adm.muted, fontSize: "11px", marginLeft: "auto" }}>
                        {g.created_at ? g.created_at.slice(0, 10) : ""}
                      </span>
                      <button
                        className="adm-btn adm-btn-danger"
                        style={adm.btnSmallDanger}
                        disabled={busy !== null}
                        onClick={() => doRevoke(selectedUser, g.kb_name)}
                      >
                        {busy === `revoke:${selectedUser.user_id}|${g.kb_name}` ? "…" : "撤销"}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="adm-card" style={adm.card}>
              <div style={adm.cardLabel}>
                未授权知识库 · {access?.available.length ?? 0}
              </div>
              {!access && <div style={adm.empty}>加载中…</div>}
              {access && access.available.length === 0 && (
                <div style={adm.empty}>所有已录入的知识库都已授权给该用户</div>
              )}
              {access && access.available.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  {access.available.map((a) => (
                    <div
                      key={a.kb_name}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                        padding: "6px 8px",
                        border: "1px solid var(--border)",
                        borderRadius: "8px",
                      }}
                    >
                      <span style={{ ...adm.badge, ...adm.badgeGray }}>未授权</span>
                      <span style={adm.mono}>{a.kb_name}</span>
                      {!a.has_api_key && (
                        <span style={{ ...adm.badge, ...adm.badgeYellow }}>缺凭据</span>
                      )}
                      <button
                        className="adm-btn adm-btn-primary"
                        style={{ ...adm.btnSmall, marginLeft: "auto" }}
                        disabled={busy !== null || !a.has_api_key}
                        title={
                          a.has_api_key
                            ? "授权该用户访问此知识库"
                            : "该知识库尚未录入凭据，请先在凭据管理中 PUT 后再授权"
                        }
                        onClick={() => doGrant(selectedUser, a.kb_name)}
                      >
                        {busy === `grant:${selectedUser.user_id}|${a.kb_name}` ? "…" : "授权"}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* --- 权限矩阵 --------------------------------------------------- */}
        <div style={adm.cardLabel}>权限矩阵（点击格子授权 / 撤销）</div>
        {loading && <div style={adm.empty}>加载中…</div>}
        {!loading && keys.length === 0 && (
          <div style={adm.empty}>
            尚未录入任何知识库凭据 —— 请先在上方「知识库凭据管理」录入库名与 Key
          </div>
        )}
        {!loading && keys.length > 0 && (
          <div style={{ overflowX: "auto", border: "1px solid var(--border)", borderRadius: "12px" }}>
            <table style={{ ...adm.table, minWidth: 0 }}>
              <thead>
                <tr>
                  <th style={adm.th}>用户</th>
                  {keys.map((k) => (
                    <th key={k.kb_name} style={{ ...adm.th, textAlign: "center" }}>
                      <span style={adm.mono}>{k.kb_name}</span>
                      {!k.has_api_key && (
                        <span style={{ ...adm.badge, ...adm.badgeYellow, marginLeft: "4px" }}>
                          缺凭据
                        </span>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.user_id}>
                    <td style={adm.td}>
                      <div style={{ fontWeight: 600 }}>{u.username}</div>
                      <div style={{ ...adm.muted, fontSize: "11px" }}>
                        {u.uid ? `工号 ${u.uid}` : "无工号"}
                        {u.role === "admin" ? " · admin" : ""}
                      </div>
                    </td>
                    {keys.map((k) => {
                      const cellKey = `${u.user_id}|${k.kb_name}`;
                      const granted = grantSet.has(cellKey);
                      const isBusy = busy === `grant:${cellKey}` || busy === `revoke:${cellKey}`;
                      const disabled = busy !== null || (!granted && !k.has_api_key);
                      return (
                        <td
                          key={k.kb_name}
                          style={{ ...adm.td, textAlign: "center", padding: "6px" }}
                        >
                          <button
                            className="adm-btn"
                            style={{
                              ...adm.btnSmall,
                              minWidth: "64px",
                              cursor: disabled ? "not-allowed" : "pointer",
                              opacity: disabled ? 0.5 : 1,
                            }}
                            disabled={disabled}
                            title={
                              granted
                                ? `点击撤销 ${u.username} 对「${k.kb_name}」的授权（软删除）`
                                : k.has_api_key
                                  ? `点击授权 ${u.username} 访问「${k.kb_name}」`
                                  : "该知识库尚未录入凭据，无法授权"
                            }
                            onClick={() =>
                              granted ? doRevoke(u, k.kb_name) : doGrant(u, k.kb_name)
                            }
                          >
                            {isBusy ? (
                              "…"
                            ) : granted ? (
                              <span style={{ ...adm.badge, ...adm.badgeGreen }}>✓ 已授权</span>
                            ) : (
                              <span style={{ ...adm.badge, ...adm.badgeGray }}>— 未授权</span>
                            )}
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {toast && (
        <div className="adm-toast" style={adm.toast}>
          {toast}
        </div>
      )}
    </div>
  );
}
