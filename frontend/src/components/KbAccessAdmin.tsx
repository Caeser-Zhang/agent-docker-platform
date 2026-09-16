/**
 * 知识领域管理（管理员专属）。
 *
 * 授权单位是「知识领域」而不是物理库：一个领域 = 一个 fastk API Key + 它覆盖的
 * 一到多个知识库（一库只属一个领域）。领域分两类，判定在每次请求时重读数据库，
 * 因此任何改动都即时生效、无需重建容器：
 *
 *   · 公共库（public）—— 隐式放行全平台所有用户，不写也不查名单；
 *   · 私有库（private）—— 只放行名单内的用户，名单支持三种录入方式：
 *       手动单个授权 / 批量粘贴工号 / 上传 CSV·Excel 名单。
 *
 * 批量导入走「预览 → 确认」两段式：解析结果先回显（匹配 / 已在名单 / 未匹配），
 * 管理员确认后才落库，令牌单次使用且绑定当前领域。切换 Key 类型需要输入领域名
 * 确认，避免误点导致全员开放或全员失效。详见 docs/KB_DOMAIN_DESIGN.md。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type KbDomainDetail,
  type KbDomainInfo,
  type KbImportPreview,
  type KbKeyType,
  type KbUser,
} from "../api";
import { adminStyles as adm, adminCss } from "./adminStyles";

const sectionTitle: React.CSSProperties = {
  fontSize: "12px",
  fontWeight: 700,
  color: "var(--text-2)",
  letterSpacing: "0.3px",
  margin: "18px 0 8px",
  paddingBottom: "6px",
  borderBottom: "1px solid var(--border)",
};

const rowBox: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "8px",
  padding: "6px 8px",
  border: "1px solid var(--border)",
  borderRadius: "8px",
};

function TypeBadge({ type }: { type: KbKeyType }) {
  return type === "public" ? (
    <span style={{ ...adm.badge, ...adm.badgeBlue }}>公共库 · 全员可见</span>
  ) : (
    <span style={{ ...adm.badge, ...adm.badgeYellow }}>私有库 · 名单授权</span>
  );
}

export function KbAccessAdminPage({
  username,
  onLogout,
  onExit,
}: {
  username: string;
  onLogout: () => void;
  onExit: () => void;
}) {
  const [domains, setDomains] = useState<KbDomainInfo[]>([]);
  const [users, setUsers] = useState<KbUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // 新建领域
  const [createOpen, setCreateOpen] = useState(false);
  const [cName, setCName] = useState("");
  const [cDesc, setCDesc] = useState("");
  const [cType, setCType] = useState<KbKeyType>("private");
  const [cKey, setCKey] = useState("");

  // 领域编辑器（editorOpen 打开弹窗，detail 到位后渲染内容）
  const [editorOpen, setEditorOpen] = useState(false);
  const [detail, setDetail] = useState<KbDomainDetail | null>(null);
  const [eName, setEName] = useState("");
  const [eDesc, setEDesc] = useState("");
  const [eKey, setEKey] = useState("");
  const [newDb, setNewDb] = useState("");
  const [memberIdent, setMemberIdent] = useState("");

  // 类型切换确认
  const [switchTo, setSwitchTo] = useState<KbKeyType | null>(null);
  const [switchConfirm, setSwitchConfirm] = useState("");

  // 名单导入
  const [impTab, setImpTab] = useState<"text" | "file">("text");
  const [impText, setImpText] = useState("");
  const [impFile, setImpFile] = useState<File | null>(null);
  const [impColumn, setImpColumn] = useState<number | null>(null);
  const [preview, setPreview] = useState<KbImportPreview | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
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

  const loadList = useCallback(async () => {
    setLoading(true);
    const [d, u] = await Promise.allSettled([api.adminListKbDomains(), api.adminListKbUsers()]);
    const errs: string[] = [];
    if (d.status === "fulfilled") setDomains(d.value.items ?? []);
    else errs.push(`知识领域：${d.reason?.message ?? d.reason}`);
    if (u.status === "fulfilled") setUsers(u.value.items ?? []);
    else errs.push(`用户：${u.reason?.message ?? u.reason}`);
    setError(errs.length ? errs.join("；") : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  /** 重新拉取当前打开的领域详情（每次写操作后调用，保证界面与库一致）。 */
  const reloadDetail = useCallback(
    async (id: string): Promise<KbDomainDetail | null> => {
      try {
        const d = await api.adminGetKbDomain(id);
        setDetail(d);
        return d;
      } catch (e: any) {
        flash(`读取领域详情失败：${e.message}`);
        return null;
      }
    },
    [flash]
  );

  const openEditor = useCallback(
    async (d: KbDomainInfo) => {
      setEditorOpen(true);
      setDetail(null);
      setSwitchTo(null);
      setSwitchConfirm("");
      setPreview(null);
      setImpFile(null);
      setImpText("");
      setImpColumn(null);
      setImpTab("text");
      setNewDb("");
      setMemberIdent("");
      setEKey("");
      setEName(d.name);
      setEDesc(d.description ?? "");
      if (fileRef.current) fileRef.current.value = "";
      const full = await reloadDetail(d.id);
      if (!full) {
        setEditorOpen(false);
        return;
      }
      setEName(full.name);
      setEDesc(full.description ?? "");
    },
    [reloadDetail]
  );

  const closeEditor = useCallback(() => {
    setEditorOpen(false);
    setDetail(null);
    setSwitchTo(null);
    setPreview(null);
    loadList();
  }, [loadList]);

  /** 每次写操作后：详情 + 列表一起刷新。 */
  const afterMutation = useCallback(
    async (id: string) => {
      await Promise.all([reloadDetail(id), loadList()]);
    },
    [reloadDetail, loadList]
  );

  // ------------------------------------------------------------- create

  const doCreate = useCallback(async () => {
    const name = cName.trim();
    if (!name) {
      flash("领域名称不能为空");
      return;
    }
    setBusy("create");
    try {
      const created = await api.adminCreateKbDomain({
        name,
        description: cDesc.trim(),
        key_type: cType,
        api_key: cKey.trim() || undefined,
      });
      flash(`已创建知识领域「${created.name}」`);
      setCreateOpen(false);
      setCName("");
      setCDesc("");
      setCType("private");
      setCKey("");
      await loadList();
    } catch (e: any) {
      flash(`创建失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [cName, cDesc, cType, cKey, flash, loadList]);

  const doDeleteDomain = useCallback(
    async (d: KbDomainInfo) => {
      if (
        !window.confirm(
          `删除知识领域「${d.name}」？其下 ${d.db_count} 个知识库关联与 ` +
            `${d.member_count} 条名单记录会一并删除，用户立即失去访问权限。`
        )
      ) {
        return;
      }
      setBusy(`del:${d.id}`);
      try {
        await api.adminDeleteKbDomain(d.id);
        flash(`已删除知识领域「${d.name}」`);
        if (detail?.id === d.id) {
          setEditorOpen(false);
          setDetail(null);
        }
        await loadList();
      } catch (e: any) {
        flash(`删除失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [detail, flash, loadList]
  );

  // ------------------------------------------------------------- editor

  const doSaveBasics = useCallback(async () => {
    if (!detail) return;
    const name = eName.trim();
    if (!name) {
      flash("领域名称不能为空");
      return;
    }
    setBusy("basics");
    try {
      await api.adminUpdateKbDomain(detail.id, { name, description: eDesc.trim() });
      flash("已保存领域名称与描述");
      await afterMutation(detail.id);
    } catch (e: any) {
      flash(`保存失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [detail, eName, eDesc, flash, afterMutation]);

  const doRotateKey = useCallback(async () => {
    if (!detail) return;
    const key = eKey.trim();
    if (!key) {
      flash("API Key 不能为空");
      return;
    }
    setBusy("rotate");
    try {
      await api.adminUpdateKbDomain(detail.id, { api_key: key });
      flash("已录入 / 轮换该领域的 API Key（加密存库，永不回显）");
      setEKey("");
      await afterMutation(detail.id);
    } catch (e: any) {
      flash(`轮换失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [detail, eKey, flash, afterMutation]);

  const doSwitchType = useCallback(async () => {
    if (!detail || !switchTo) return;
    if (switchConfirm.trim() !== detail.name) {
      flash("请输入与当前领域名称完全一致的名称以确认切换");
      return;
    }
    setBusy("switch");
    try {
      await api.adminUpdateKbDomain(detail.id, {
        key_type: switchTo,
        confirm_name: detail.name,
      });
      flash(`已切换为${switchTo === "public" ? "公共库（全员可访问）" : "私有库（仅名单可访问）"}，即时生效`);
      setSwitchTo(null);
      setSwitchConfirm("");
      await afterMutation(detail.id);
    } catch (e: any) {
      flash(`切换失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [detail, switchTo, switchConfirm, flash, afterMutation]);

  // ------------------------------------------------------------- databases

  const doAddDb = useCallback(async () => {
    if (!detail) return;
    const kb = newDb.trim();
    if (!kb) {
      flash("知识库名不能为空");
      return;
    }
    setBusy("adddb");
    try {
      const r = await api.adminAddKbDomainDb(detail.id, kb);
      flash(r.added ? `已把「${kb}」加入该知识领域` : `「${kb}」已在该领域中`);
      setNewDb("");
      await afterMutation(detail.id);
    } catch (e: any) {
      flash(`关联失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [detail, newDb, flash, afterMutation]);

  const doRemoveDb = useCallback(
    async (kb: string) => {
      if (!detail) return;
      setBusy(`rmdb:${kb}`);
      try {
        await api.adminRemoveKbDomainDb(detail.id, kb);
        flash(`已把「${kb}」移出该知识领域`);
        await afterMutation(detail.id);
      } catch (e: any) {
        flash(`移出失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [detail, flash, afterMutation]
  );

  // ------------------------------------------------------------- roster

  const doGrantMember = useCallback(async () => {
    if (!detail) return;
    const ident = memberIdent.trim();
    if (!ident) {
      flash("请输入工号或用户名");
      return;
    }
    setBusy("grant");
    try {
      // 纯数字按工号处理，否则按用户名；后端两者都能解析。
      const body = /^\d+$/.test(ident) ? { uid: ident } : { username: ident };
      const r = await api.adminGrantKbDomainMember(detail.id, body);
      flash(`已授权 ${r.username} 访问「${detail.name}」`);
      setMemberIdent("");
      await afterMutation(detail.id);
    } catch (e: any) {
      flash(`授权失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [detail, memberIdent, flash, afterMutation]);

  const doRevokeMember = useCallback(
    async (m: { user_id: string; username: string }) => {
      if (!detail) return;
      setBusy(`revoke:${m.user_id}`);
      try {
        await api.adminRevokeKbDomainMember(detail.id, m.user_id);
        flash(`已撤销 ${m.username} 的访问（软删除，可重新授权）`);
        await afterMutation(detail.id);
      } catch (e: any) {
        flash(`撤销失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [detail, flash, afterMutation]
  );

  // ------------------------------------------------------------- import

  const runPreview = useCallback(
    async (column: number | null) => {
      if (!detail) return;
      const source =
        impTab === "file"
          ? { file: impFile ?? undefined, column: column ?? undefined }
          : { text: impText, column: column ?? undefined };
      if (impTab === "file" && !impFile) {
        flash("请先选择 .csv 或 .xlsx 文件");
        return;
      }
      if (impTab === "text" && !impText.trim()) {
        flash("请先粘贴工号 / 用户名");
        return;
      }
      setBusy("preview");
      try {
        const p = await api.adminKbImportPreview(detail.id, source);
        setPreview(p);
        setImpColumn(p.source_meta?.column_index ?? null);
        flash(
          `解析完成：待授权 ${p.matched.length} 人 · 已在名单 ${p.already.length} 人 · 未匹配 ${p.unmatched.length} 条`
        );
      } catch (e: any) {
        setPreview(null);
        flash(`解析失败：${e.message}`);
      } finally {
        setBusy(null);
      }
    },
    [detail, impTab, impFile, impText, flash]
  );

  const doCommitImport = useCallback(async () => {
    if (!detail || !preview) return;
    setBusy("commit");
    try {
      const r = await api.adminKbImportCommit(detail.id, preview.preview_token);
      flash(`导入完成：新增授权 ${r.granted} 人 · 跳过 ${r.skipped} 人`);
      setPreview(null);
      setImpText("");
      setImpFile(null);
      setImpColumn(null);
      if (fileRef.current) fileRef.current.value = "";
      await afterMutation(detail.id);
    } catch (e: any) {
      setPreview(null);
      flash(`导入失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  }, [detail, preview, flash, afterMutation]);

  // ------------------------------------------------------------- render

  const publicCount = domains.filter((d) => d.key_type === "public").length;
  const dbTotal = domains.reduce((n, d) => n + d.db_count, 0);
  const memberTotal = domains
    .filter((d) => d.key_type === "private")
    .reduce((n, d) => n + d.member_count, 0);

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
            <div style={adm.headerTitle}>知识领域管理</div>
            <div style={adm.headerSubtitle}>
              一个 Key 覆盖多个知识库 · 公共库全员可见 · 私有库按名单授权 · 改动即时生效
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
            <div style={adm.cardLabel}>知识领域</div>
            <div style={adm.cardValue}>{domains.length}</div>
            <div style={adm.cardSub}>授权与展示的基本单位</div>
          </div>
          <div className="adm-card" style={adm.card}>
            <div style={adm.cardLabel}>公共库 / 私有库</div>
            <div style={adm.cardValueGreen}>
              {publicCount} / {domains.length - publicCount}
            </div>
            <div style={adm.cardSub}>公共库隐式放行全平台所有用户</div>
          </div>
          <div className="adm-card" style={adm.card}>
            <div style={adm.cardLabel}>已关联知识库</div>
            <div style={adm.cardValue}>{dbTotal}</div>
            <div style={adm.cardSub}>一个库只属于一个领域</div>
          </div>
          <div className="adm-card" style={adm.card}>
            <div style={adm.cardLabel}>私有库名单条目</div>
            <div style={adm.cardValue}>{memberTotal}</div>
            <div style={adm.cardSub}>仅统计未撤销（revoked_at 为空）</div>
          </div>
        </div>

        {/* Toolbar */}
        <div style={adm.toolbar}>
          <button
            className="adm-btn adm-btn-primary"
            style={adm.btnPrimary}
            disabled={busy !== null}
            onClick={() => setCreateOpen(true)}
          >
            新建知识领域
          </button>
          <button
            className="adm-btn"
            style={adm.btn}
            onClick={loadList}
            disabled={loading || busy !== null}
          >
            刷新
          </button>
          <span style={adm.toolbarHint}>
            领域名称即用户在前端看到的知识领域展示名，可随时修改
          </span>
        </div>

        {/* --- 领域表格 --------------------------------------------------- */}
        {loading ? (
          <div style={adm.empty}>加载中…</div>
        ) : domains.length === 0 ? (
          <div style={adm.empty}>
            尚无任何知识领域 —— 点「新建知识领域」录入 Key、关联知识库并配置权限
          </div>
        ) : (
          <div style={adm.tableWrap}>
            <table style={adm.table}>
              <thead>
                <tr>
                  <th style={adm.th}>知识领域</th>
                  <th style={adm.th}>类型</th>
                  <th style={{ ...adm.th, textAlign: "center" }}>知识库</th>
                  <th style={{ ...adm.th, textAlign: "center" }}>可访问用户</th>
                  <th style={{ ...adm.th, textAlign: "center" }}>API Key</th>
                  <th style={adm.th}>更新于</th>
                  <th style={{ ...adm.th, textAlign: "right" }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {domains.map((d) => (
                  <tr key={d.id} className="adm-row">
                    <td style={adm.td}>
                      <div style={{ fontWeight: 600 }}>{d.name}</div>
                      <div style={{ ...adm.muted, fontSize: "11px" }}>
                        {d.description || "（无描述）"}
                      </div>
                    </td>
                    <td style={adm.td}>
                      <TypeBadge type={d.key_type} />
                    </td>
                    <td style={{ ...adm.td, textAlign: "center" }}>{d.db_count}</td>
                    <td style={{ ...adm.td, textAlign: "center" }}>
                      {d.key_type === "public" ? (
                        <span style={{ ...adm.badge, ...adm.badgeGreen }}>全部用户</span>
                      ) : (
                        `${d.member_count} / ${users.length}`
                      )}
                    </td>
                    <td style={{ ...adm.td, textAlign: "center" }}>
                      {d.has_api_key ? (
                        <span style={{ ...adm.badge, ...adm.badgeGreen }}>已录入</span>
                      ) : (
                        <span style={{ ...adm.badge, ...adm.badgeRed }}>缺凭据</span>
                      )}
                    </td>
                    <td style={{ ...adm.td, ...adm.muted, fontSize: "11px" }}>
                      {d.updated_at ? d.updated_at.slice(0, 10) : "—"}
                    </td>
                    <td style={{ ...adm.td, textAlign: "right", whiteSpace: "nowrap" }}>
                      <button
                        className="adm-btn"
                        style={{ ...adm.btnSmall, marginRight: "6px" }}
                        disabled={busy !== null}
                        onClick={() => openEditor(d)}
                      >
                        管理
                      </button>
                      <button
                        className="adm-btn adm-btn-danger"
                        style={adm.btnSmallDanger}
                        disabled={busy !== null}
                        onClick={() => doDeleteDomain(d)}
                      >
                        {busy === `del:${d.id}` ? "…" : "删除"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* --- 新建领域弹窗 ------------------------------------------------- */}
      {createOpen && (
        <div style={adm.overlay} onClick={() => setCreateOpen(false)}>
          <div style={{ ...adm.modal, width: "min(560px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div style={adm.modalHeader}>
              <span style={adm.modalTitle}>新建知识领域</span>
              <button className="adm-btn" style={adm.btnSmall} onClick={() => setCreateOpen(false)}>
                关闭
              </button>
            </div>
            <div style={adm.modalBody}>
              <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                <label style={adm.libField}>
                  <span style={adm.libLabel}>领域名称（用户界面展示名，必填且唯一，≤50 字）</span>
                  <input
                    className="adm-input"
                    style={adm.input}
                    placeholder="如：研发知识库"
                    value={cName}
                    onChange={(e) => setCName(e.target.value)}
                  />
                </label>
                <label style={adm.libField}>
                  <span style={adm.libLabel}>领域描述（可选，≤500 字）</span>
                  <textarea
                    className="adm-textarea"
                    style={{ ...adm.textarea, minHeight: "64px" }}
                    placeholder="这个领域覆盖哪些内容"
                    value={cDesc}
                    onChange={(e) => setCDesc(e.target.value)}
                  />
                </label>
                <label style={adm.libField}>
                  <span style={adm.libLabel}>Key 类型</span>
                  <select
                    className="adm-select"
                    style={adm.select}
                    value={cType}
                    onChange={(e) => setCType(e.target.value as KbKeyType)}
                  >
                    <option value="private">私有库 —— 仅名单内用户可访问</option>
                    <option value="public">公共库 —— 全平台所有用户可访问</option>
                  </select>
                </label>
                <label style={adm.libField}>
                  <span style={adm.libLabel}>fastk API Key（可稍后在管理弹窗中录入 / 轮换）</span>
                  <input
                    className="adm-input"
                    style={adm.input}
                    type="password"
                    placeholder="Fernet 加密存库，永不回显"
                    value={cKey}
                    onChange={(e) => setCKey(e.target.value)}
                  />
                </label>
                <div style={adm.libHint}>
                  创建后是一个空领域：还需在管理弹窗中关联知识库；公共领域关联知识库后即对全员可见。
                </div>
              </div>
            </div>
            <div style={adm.modalFooter}>
              <button className="adm-btn" style={adm.btn} onClick={() => setCreateOpen(false)}>
                取消
              </button>
              <button
                className="adm-btn adm-btn-primary"
                style={adm.btnPrimary}
                disabled={busy !== null || !cName.trim()}
                onClick={doCreate}
              >
                {busy === "create" ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* --- 领域管理弹窗 ------------------------------------------------- */}
      {editorOpen && detail === null && (
        <div style={adm.overlay}>
          <div style={{ ...adm.modal, width: "min(860px, 100%)" }}>
            <div style={adm.modalBody}>
              <div style={adm.empty}>加载中…</div>
            </div>
          </div>
        </div>
      )}

      {detail && (
        <div style={adm.overlay} onClick={closeEditor}>
          <div
            style={{ ...adm.modal, width: "min(860px, 100%)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={adm.modalHeader}>
              <span style={adm.modalTitle}>
                管理知识领域 · {detail.name} <TypeBadge type={detail.key_type} />
              </span>
              <button className="adm-btn" style={adm.btnSmall} onClick={closeEditor}>
                关闭
              </button>
            </div>

            <div style={adm.modalBody} className="adm-scroll">
              {/* 基本信息 */}
              <div style={{ ...sectionTitle, marginTop: 0 }}>基本信息</div>
              <div style={{ display: "flex", gap: "10px", alignItems: "flex-end" }}>
                <label style={{ ...adm.libField, flex: 1 }}>
                  <span style={adm.libLabel}>领域名称</span>
                  <input
                    className="adm-input"
                    style={adm.input}
                    value={eName}
                    onChange={(e) => setEName(e.target.value)}
                  />
                </label>
                <button
                  className="adm-btn adm-btn-primary"
                  style={adm.btnPrimary}
                  disabled={busy !== null}
                  onClick={doSaveBasics}
                >
                  {busy === "basics" ? "保存中…" : "保存名称 / 描述"}
                </button>
              </div>
              <label style={{ ...adm.libField, marginTop: "10px" }}>
                <span style={adm.libLabel}>领域描述（可选）</span>
                <textarea
                  className="adm-textarea"
                  style={{ ...adm.textarea, minHeight: "56px" }}
                  value={eDesc}
                  onChange={(e) => setEDesc(e.target.value)}
                />
              </label>

              {/* Key 类型 */}
              <div style={sectionTitle}>Key 类型与凭据</div>
              <div style={{ ...rowBox, alignItems: "flex-start", flexDirection: "column", gap: "6px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                  <span style={{ fontSize: "12px", color: "var(--text-2)" }}>当前类型</span>
                  <TypeBadge type={detail.key_type} />
                  <button
                    className="adm-btn adm-btn-warn"
                    style={{ ...adm.btnSmall, marginLeft: "auto" }}
                    disabled={busy !== null || switchTo !== null}
                    onClick={() => {
                      setSwitchTo(detail.key_type === "public" ? "private" : "public");
                      setSwitchConfirm("");
                    }}
                  >
                    切换为{detail.key_type === "public" ? "私有库" : "公共库"}…
                  </button>
                </div>
                <div style={adm.libHint}>
                  公共库：系统默认赋予全平台所有用户访问权限，不写名单；私有库：仅名单内用户可访问。
                  切换立即生效，名单记录两个方向都保留，切回即恢复。
                </div>
              </div>

              {switchTo && (
                <div
                  style={{
                    ...rowBox,
                    flexDirection: "column",
                    alignItems: "stretch",
                    gap: "8px",
                    marginTop: "8px",
                    borderColor: "var(--amber-border)",
                    background: "var(--amber-soft)",
                  }}
                >
                  <div style={adm.warnText}>
                    {switchTo === "public"
                      ? `切换为公共库后，全平台 ${detail.total_users} 名用户立即可访问该领域下的所有知识库。`
                      : `切换为私有库后，仅名单内 ${detail.active_member_count} 名用户可访问，其余 ${Math.max(
                          detail.total_users - detail.active_member_count,
                          0
                        )} 名用户立即失去访问权限。`}
                  </div>
                  <input
                    className="adm-input"
                    style={adm.input}
                    placeholder={`请输入领域名称「${detail.name}」以确认`}
                    value={switchConfirm}
                    onChange={(e) => setSwitchConfirm(e.target.value)}
                  />
                  <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
                    <button
                      className="adm-btn"
                      style={adm.btnSmall}
                      disabled={busy !== null}
                      onClick={() => {
                        setSwitchTo(null);
                        setSwitchConfirm("");
                      }}
                    >
                      取消
                    </button>
                    <button
                      className="adm-btn adm-btn-danger"
                      style={adm.btnSmallDanger}
                      // 名称不匹配时不禁用按钮：doSwitchType 会弹 toast 说明原因。
                      // 静默 disabled 会让用户以为"点击没反应"。
                      disabled={busy !== null}
                      onClick={doSwitchType}
                    >
                      {busy === "switch" ? "切换中…" : "确认切换"}
                    </button>
                  </div>
                </div>
              )}

              <div style={{ ...rowBox, marginTop: "8px" }}>
                <input
                  className="adm-input"
                  style={{ ...adm.input, flex: 1 }}
                  type="password"
                  placeholder={detail.has_api_key ? "轮换 API Key（留空则不改动）" : "录入 API Key"}
                  value={eKey}
                  onChange={(e) => setEKey(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") doRotateKey();
                  }}
                />
                <span style={{ ...adm.muted, fontSize: "11px", whiteSpace: "nowrap" }}>
                  {detail.has_api_key ? "已录入" : "缺凭据"}
                </span>
                <button
                  className="adm-btn"
                  style={adm.btnSmall}
                  disabled={busy !== null || !eKey.trim()}
                  onClick={doRotateKey}
                >
                  {busy === "rotate" ? "提交中…" : "录入 / 轮换"}
                </button>
              </div>

              {/* 关联知识库 */}
              <div style={sectionTitle}>
                关联知识库 · {detail.databases.length}（一个 Key 可覆盖多个库，一个库只属一个领域）
              </div>
              <div style={rowBox}>
                <input
                  className="adm-input"
                  style={{ ...adm.input, flex: 1 }}
                  placeholder="fastk 物理库名，如 fastdb"
                  value={newDb}
                  onChange={(e) => setNewDb(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") doAddDb();
                  }}
                />
                <button
                  className="adm-btn adm-btn-primary"
                  style={adm.btnPrimary}
                  disabled={busy !== null || !newDb.trim()}
                  onClick={doAddDb}
                >
                  {busy === "adddb" ? "关联中…" : "关联"}
                </button>
              </div>
              {detail.databases.length === 0 ? (
                <div style={adm.empty}>尚未关联任何知识库 —— 该领域对用户不可见</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "8px" }}>
                  {detail.databases.map((db) => (
                    <div key={db.kb_name} style={rowBox}>
                      <span style={adm.mono}>{db.kb_name}</span>
                      <button
                        className="adm-btn adm-btn-danger"
                        style={{ ...adm.btnSmallDanger, marginLeft: "auto" }}
                        disabled={busy !== null}
                        onClick={() => doRemoveDb(db.kb_name)}
                      >
                        {busy === `rmdb:${db.kb_name}` ? "…" : "移出"}
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* 名单 */}
              <div style={sectionTitle}>
                访问名单 ·{" "}
                {detail.key_type === "public"
                  ? "公共库无需名单（全员放行）"
                  : `${detail.active_member_count} 人`}
              </div>

              {detail.key_type === "public" ? (
                <div style={adm.empty}>
                  该领域是公共库，全平台 {detail.total_users} 名用户都可访问。
                  如需按名单授权，请先切换为私有库（名单记录会保留）。
                </div>
              ) : (
                <>
                  {/* 1. 手动单个授权 */}
                  <div style={{ ...adm.libLabel, marginBottom: "6px" }}>① 手动单个授权</div>
                  <div style={rowBox}>
                    <select
                      className="adm-select"
                      style={{ ...adm.select, minWidth: "200px" }}
                      value=""
                      onChange={(e) => {
                        if (e.target.value) setMemberIdent(e.target.value);
                      }}
                    >
                      <option value="">— 从用户列表选择 —</option>
                      {users.map((u) => (
                        <option key={u.user_id} value={u.username}>
                          {u.username}
                          {u.uid ? `（工号 ${u.uid}）` : ""}
                        </option>
                      ))}
                    </select>
                    <input
                      className="adm-input"
                      style={{ ...adm.input, flex: 1 }}
                      placeholder="或直接输入工号 / 用户名"
                      value={memberIdent}
                      onChange={(e) => setMemberIdent(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") doGrantMember();
                      }}
                    />
                    <button
                      className="adm-btn adm-btn-primary"
                      style={adm.btnPrimary}
                      disabled={busy !== null || !memberIdent.trim()}
                      onClick={doGrantMember}
                    >
                      {busy === "grant" ? "授权中…" : "授权"}
                    </button>
                  </div>

                  {/* 2/3. 批量导入 */}
                  <div style={{ ...adm.libLabel, margin: "12px 0 6px" }}>
                    ② 批量工号导入 · ③ Excel / CSV 名单导入（先预览，确认后落库）
                  </div>
                  <div style={{ display: "flex", gap: "8px", marginBottom: "8px" }}>
                    <button
                      className="adm-btn"
                      style={impTab === "text" ? { ...adm.btnSmall, ...adm.logsTabActive } : adm.btnSmall}
                      disabled={busy !== null}
                      onClick={() => {
                        setImpTab("text");
                        setPreview(null);
                      }}
                    >
                      粘贴文本
                    </button>
                    <button
                      className="adm-btn"
                      style={impTab === "file" ? { ...adm.btnSmall, ...adm.logsTabActive } : adm.btnSmall}
                      disabled={busy !== null}
                      onClick={() => {
                        setImpTab("file");
                        setPreview(null);
                      }}
                    >
                      上传文件
                    </button>
                  </div>

                  {impTab === "text" ? (
                    <>
                      <textarea
                        className="adm-textarea"
                        style={{ ...adm.textarea, minHeight: "80px" }}
                        placeholder={"粘贴工号或用户名，用换行 / 逗号 / 空格分隔，例如：\n1001, 1002\nzhangsan lisi"}
                        value={impText}
                        onChange={(e) => {
                          setImpText(e.target.value);
                          setPreview(null);
                        }}
                      />
                      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "8px" }}>
                        <button
                          className="adm-btn adm-btn-primary"
                          style={adm.btnPrimary}
                          disabled={busy !== null || !impText.trim()}
                          onClick={() => runPreview(null)}
                        >
                          {busy === "preview" ? "解析中…" : "解析并预览"}
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <input
                        ref={fileRef}
                        type="file"
                        accept=".csv,.xlsx"
                        style={{ ...adm.libLabel, display: "block" }}
                        onChange={(e) => {
                          setImpFile(e.target.files?.[0] ?? null);
                          setPreview(null);
                          setImpColumn(null);
                        }}
                      />
                      <div style={{ ...adm.libHint, margin: "6px 0 8px" }}>
                        支持 .csv / .xlsx，≤5MB、≤5000 行；自动按表头关键词（工号 / 员工号 / uid /
                        emp / no）定位列，预览中可改选其他列。未匹配的标识符不会自动建用户。
                      </div>
                      <div style={{ display: "flex", justifyContent: "flex-end" }}>
                        <button
                          className="adm-btn adm-btn-primary"
                          style={adm.btnPrimary}
                          disabled={busy !== null || !impFile}
                          onClick={() => runPreview(null)}
                        >
                          {busy === "preview" ? "解析中…" : "解析并预览"}
                        </button>
                      </div>
                    </>
                  )}

                  {preview && (
                    <div
                      style={{
                        ...rowBox,
                        flexDirection: "column",
                        alignItems: "stretch",
                        gap: "8px",
                        marginTop: "10px",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                        <span style={{ ...adm.badge, ...adm.badgeGray }}>
                          来源 {preview.source}
                          {preview.source_meta?.filename ? ` · ${preview.source_meta.filename}` : ""}
                        </span>
                        <span style={{ ...adm.badge, ...adm.badgeGreen }}>
                          待授权 {preview.matched.length}
                        </span>
                        <span style={{ ...adm.badge, ...adm.badgeYellow }}>
                          已在名单 {preview.already.length}
                        </span>
                        <span style={{ ...adm.badge, ...adm.badgeRed }}>
                          未匹配 {preview.unmatched.length}
                        </span>
                        {preview.source_meta?.columns && preview.source_meta.columns.length > 0 && (
                          <select
                            className="adm-select"
                            style={{ ...adm.select, marginLeft: "auto", minWidth: "160px" }}
                            value={impColumn ?? preview.source_meta.column_index ?? 0}
                            disabled={busy !== null}
                            onChange={(e) => {
                              const col = Number(e.target.value);
                              setImpColumn(col);
                              runPreview(col);
                            }}
                          >
                            {preview.source_meta.columns.map((c) => (
                              <option key={c.index} value={c.index}>
                                第{c.index + 1}列 · {c.label}
                              </option>
                            ))}
                          </select>
                        )}
                      </div>

                      <div style={{ maxHeight: "160px", overflowY: "auto" }} className="adm-scroll">
                        <div style={{ ...adm.mono, lineHeight: 1.7 }}>
                          <div>
                            <strong>待授权：</strong>
                            {preview.matched.length
                              ? preview.matched
                                  .map((m) => `${m.username}${m.uid ? `(${m.uid})` : ""}`)
                                  .join("、")
                              : "（无）"}
                          </div>
                          {preview.already.length > 0 && (
                            <div style={adm.muted}>
                              <strong>已在名单（跳过）：</strong>
                              {preview.already.map((m) => m.username).join("、")}
                            </div>
                          )}
                          {preview.unmatched.length > 0 && (
                            <div style={{ color: "var(--red)" }}>
                              <strong>未匹配（不会授权，也不会建用户）：</strong>
                              {preview.unmatched.slice(0, 50).join("、")}
                              {preview.unmatched.length > 50
                                ? ` …等 ${preview.unmatched.length} 条`
                                : ""}
                            </div>
                          )}
                        </div>
                      </div>

                      <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
                        <button
                          className="adm-btn"
                          style={adm.btnSmall}
                          disabled={busy !== null}
                          onClick={() => setPreview(null)}
                        >
                          放弃
                        </button>
                        <button
                          className="adm-btn adm-btn-primary"
                          style={adm.btnPrimary}
                          disabled={busy !== null || preview.matched.length === 0}
                          onClick={doCommitImport}
                        >
                          {busy === "commit"
                            ? "导入中…"
                            : `确认授权 ${preview.matched.length} 人`}
                        </button>
                      </div>
                    </div>
                  )}

                  {/* 名单明细 */}
                  <div style={{ ...adm.libLabel, margin: "12px 0 6px" }}>
                    当前名单（{detail.members.length}）
                  </div>
                  {detail.members.length === 0 ? (
                    <div style={adm.empty}>名单为空 —— 无人可访问该私有领域</div>
                  ) : (
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: "6px",
                        maxHeight: "240px",
                        overflowY: "auto",
                      }}
                      className="adm-scroll"
                    >
                      {detail.members.map((m) => (
                        <div key={m.user_id} style={rowBox}>
                          <span style={{ fontWeight: 600, fontSize: "12.5px" }}>{m.username}</span>
                          <span style={{ ...adm.muted, fontSize: "11px" }}>
                            {m.uid ? `工号 ${m.uid}` : "无工号"}
                          </span>
                          <span style={{ ...adm.muted, fontSize: "11px", marginLeft: "auto" }}>
                            {m.created_at ? m.created_at.slice(0, 10) : ""}
                          </span>
                          <button
                            className="adm-btn adm-btn-danger"
                            style={adm.btnSmallDanger}
                            disabled={busy !== null}
                            onClick={() => doRevokeMember(m)}
                          >
                            {busy === `revoke:${m.user_id}` ? "…" : "撤销"}
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>

            <div style={adm.modalFooter}>
              <span style={{ ...adm.muted, fontSize: "11px", marginRight: "auto" }}>
                所有改动即时生效：代理与徽章通路每次请求都重读领域与名单
              </span>
              <button className="adm-btn" style={adm.btn} onClick={closeEditor}>
                完成
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="adm-toast" style={adm.toast}>
          {toast}
        </div>
      )}
    </div>
  );
}
