/**
 * Styles for the admin Docker management panel — light theme matching Chat.
 *
 * Design tokens (same palette as chatStyles.ts):
 *   page #f7f8fa · surface #ffffff · subtle #f1f3f5 · border #e6e8ee
 *   text #16181d · secondary #5b6472 · muted #9aa2b1
 *   accent #2563eb · green #16a34a · danger #dc2626
 *
 * Hover / focus / transition states can't be expressed as inline styles, so
 * `adminCss` is injected once by AdminPanel as a <style> tag; interactive
 * elements carry both an inline base style and an `adm-*` class.
 */

/** Class-based hover/focus/motion rules — see comment above. */
export const adminCss = `
  .adm-btn { transition: background-color .15s ease, border-color .15s ease, color .15s ease, box-shadow .15s ease, transform .15s ease; }
  .adm-btn:hover:not(:disabled) { background: #f1f3f5; border-color: #d5dae3; color: #16181d; }
  .adm-btn:active:not(:disabled) { transform: translateY(1px); }
  .adm-btn-primary:hover:not(:disabled) { background: #1d4ed8; border-color: #2563eb; box-shadow: 0 4px 14px rgba(37,99,235,.25); }
  .adm-btn-warn:hover:not(:disabled) { background: #fffbeb; border-color: #d97706; color: #b45309; }
  .adm-btn-danger:hover:not(:disabled) { background: #fef2f2; border-color: #b91c1c; color: #dc2626; }
  .adm-btn:focus-visible, .adm-btn-primary:focus-visible, .adm-select:focus-visible, .adm-input:focus-visible, .adm-search:focus-visible {
    outline: 2px solid rgba(37,99,235,.55); outline-offset: 1px;
  }
  .adm-select:hover, .adm-search:hover, .adm-input:hover { border-color: #d5dae3; }
  .adm-select, .adm-search, .adm-input { transition: border-color .15s ease, box-shadow .15s ease; }

  .adm-card { transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease; }
  .adm-card:hover { border-color: #d5dae3; transform: translateY(-1px); box-shadow: 0 8px 24px rgba(15,23,42,.06); }

  .adm-row { transition: background-color .12s ease; }
  .adm-row:hover { background: rgba(37,99,235,.04); }
  .adm-row-selected, .adm-row-selected:hover { background: rgba(37,99,235,.08); }

  .adm-th-sort { cursor: pointer; user-select: none; }
  .adm-th-sort:hover { color: #16181d; }

  .adm-check { accent-color: #2563eb; width: 15px; height: 15px; cursor: pointer; flex-shrink: 0; }
  .adm-check:focus-visible { outline: 2px solid rgba(37,99,235,.55); outline-offset: 2px; border-radius: 3px; }

  .adm-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
  .adm-scroll::-webkit-scrollbar-thumb { background: #d5dae3; border-radius: 4px; }
  .adm-scroll::-webkit-scrollbar-thumb:hover { background: #9aa2b1; }
  .adm-scroll::-webkit-scrollbar-track { background: transparent; }

  .adm-toast { animation: adm-toast-in .22s ease-out; }
  @keyframes adm-toast-in { from { opacity: 0; transform: translate(-50%, 8px); } to { opacity: 1; transform: translate(-50%, 0); } }

  @media (prefers-reduced-motion: reduce) {
    .adm-btn, .adm-card, .adm-row, .adm-select, .adm-search, .adm-input { transition: none; }
    .adm-toast { animation: none; }
  }
`;

export const adminStyles: Record<string, React.CSSProperties> = {
  container: {
    height: "100vh",
    display: "flex",
    flexDirection: "column",
    background: "#f7f8fa",
    color: "#16181d",
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
    overflow: "hidden",
  },

  // --- Header ------------------------------------------------------------
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 20px",
    borderBottom: "1px solid #e6e8ee",
    background: "#ffffff",
    flexShrink: 0,
  },
  headerLeft: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
  },
  headerIcon: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: "34px",
    height: "34px",
    borderRadius: "9px",
    background: "rgba(37,99,235,.08)",
    border: "1px solid rgba(37,99,235,.3)",
    color: "#2563eb",
    flexShrink: 0,
  },
  headerTitle: { fontSize: "17px", fontWeight: 700, color: "#16181d", letterSpacing: "0.2px" },
  headerSubtitle: { fontSize: "12px", color: "#9aa2b1", marginTop: "1px" },
  headerRight: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
  },
  whoami: {
    fontSize: "13px",
    color: "#5b6472",
    padding: "5px 12px",
    borderRadius: "999px",
    border: "1px solid #e6e8ee",
    background: "#f1f3f5",
  },

  // --- Buttons -----------------------------------------------------------
  btn: {
    padding: "6px 14px",
    border: "1px solid #d5dae3",
    background: "#ffffff",
    color: "#5b6472",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    lineHeight: "20px",
  },
  btnPrimary: {
    padding: "6px 14px",
    border: "1px solid #2563eb",
    background: "#2563eb",
    color: "#fff",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 600,
    lineHeight: "20px",
  },
  btnDanger: {
    padding: "6px 14px",
    border: "1px solid #fca5a5",
    background: "#fef2f2",
    color: "#dc2626",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    lineHeight: "20px",
  },
  btnSmall: {
    padding: "4px 10px",
    border: "1px solid #d5dae3",
    background: "#ffffff",
    color: "#5b6472",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnSmallDanger: {
    padding: "4px 10px",
    border: "1px solid rgba(220,38,38,.35)",
    background: "#fef2f2",
    color: "#dc2626",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnSmallWarn: {
    padding: "4px 10px",
    border: "1px solid rgba(217,119,6,.4)",
    background: "#fffbeb",
    color: "#b45309",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnDisabled: {
    opacity: 0.4,
    cursor: "not-allowed",
    transform: "none",
  },

  // --- Body / overview cards ---------------------------------------------
  body: {
    flex: 1,
    overflowY: "auto",
    padding: "18px 20px",
    display: "flex",
    flexDirection: "column",
    gap: "14px",
  },
  cardsRow: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
    gap: "12px",
    flexShrink: 0,
  },
  card: {
    background: "#ffffff",
    border: "1px solid #e6e8ee",
    borderRadius: "12px",
    padding: "14px 16px",
  },
  cardLabel: { fontSize: "12px", color: "#9aa2b1", marginBottom: "6px", letterSpacing: "0.3px" },
  cardValue: { fontSize: "24px", fontWeight: 700, color: "#16181d" },
  cardValueGreen: { fontSize: "24px", fontWeight: 700, color: "#16a34a" },
  cardValueRed: { fontSize: "24px", fontWeight: 700, color: "#dc2626" },
  cardSub: { fontSize: "11px", color: "#9aa2b1", marginTop: "4px" },

  // --- Toolbar (search / filters / switches) ------------------------------
  toolbar: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
    flexShrink: 0,
  },
  searchWrap: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "0 10px",
    height: "34px",
    borderRadius: "8px",
    border: "1px solid #d5dae3",
    background: "#ffffff",
    minWidth: "260px",
    flex: "0 1 300px",
  },
  searchIcon: { color: "#9aa2b1", display: "flex", flexShrink: 0 },
  search: {
    flex: 1,
    border: "none",
    background: "transparent",
    color: "#16181d",
    fontSize: "13px",
    outline: "none",
    minWidth: 0,
  },
  select: {
    padding: "6px 10px",
    border: "1px solid #d5dae3",
    borderRadius: "8px",
    background: "#ffffff",
    color: "#5b6472",
    fontSize: "12.5px",
    cursor: "pointer",
    height: "34px",
  },
  toolbarHint: { fontSize: "12px", color: "#9aa2b1", marginLeft: "auto" },

  // --- Batch action bar ----------------------------------------------------
  batchBar: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
    flexShrink: 0,
    padding: "10px 14px",
    borderRadius: "10px",
    background: "rgba(37,99,235,.08)",
    border: "1px solid rgba(37,99,235,.35)",
  },
  batchCount: { fontSize: "13px", fontWeight: 600, color: "#1d4ed8" },
  batchProgress: { fontSize: "12px", color: "#2563eb" },
  batchSpacer: { marginLeft: "auto", display: "flex", gap: "8px", alignItems: "center" },

  // --- Table -------------------------------------------------------------
  tableWrap: {
    background: "#ffffff",
    border: "1px solid #e6e8ee",
    borderRadius: "12px",
    overflow: "auto",
    flexShrink: 0,
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: "13px",
    minWidth: "1180px",
  },
  th: {
    textAlign: "left",
    padding: "10px 12px",
    color: "#5b6472",
    fontWeight: 600,
    fontSize: "12px",
    letterSpacing: "0.3px",
    borderBottom: "1px solid #e6e8ee",
    position: "sticky" as const,
    top: 0,
    background: "#ffffff",
    whiteSpace: "nowrap",
    zIndex: 1,
  },
  thCheck: {
    width: "36px",
    padding: "10px 8px 10px 14px",
    textAlign: "left",
    borderBottom: "1px solid #e6e8ee",
    position: "sticky" as const,
    top: 0,
    background: "#ffffff",
    zIndex: 1,
  },
  sortLabel: {
    display: "inline-flex",
    alignItems: "center",
    gap: "4px",
    background: "none",
    border: "none",
    padding: 0,
    font: "inherit",
    color: "inherit",
    letterSpacing: "inherit",
    cursor: "pointer",
  },
  sortArrow: { display: "inline-flex", color: "#2563eb" },
  sortArrowIdle: { display: "inline-flex", color: "#9aa2b1", opacity: 0.7 },
  td: {
    padding: "10px 12px",
    borderBottom: "1px solid #f1f3f5",
    verticalAlign: "top",
    whiteSpace: "nowrap",
  },
  tdCheck: {
    padding: "10px 8px 10px 14px",
    borderBottom: "1px solid #f1f3f5",
    verticalAlign: "middle",
  },
  tdWrap: {
    padding: "10px 12px",
    borderBottom: "1px solid #f1f3f5",
    verticalAlign: "top",
    whiteSpace: "normal",
    wordBreak: "break-all",
    maxWidth: "260px",
  },
  userCell: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
  },
  avatar: {
    width: "24px",
    height: "24px",
    borderRadius: "50%",
    background: "linear-gradient(135deg, #2563eb, #8b5cf6)",
    color: "#fff",
    fontSize: "11px",
    fontWeight: 700,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
  },
  uidText: {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: "12px",
    color: "#2563eb",
  },
  muted: { color: "#9aa2b1" },
  mono: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", fontSize: "12px" },

  // --- Status badges -------------------------------------------------------
  badge: {
    display: "inline-block",
    padding: "2px 9px",
    borderRadius: "999px",
    fontSize: "11px",
    fontWeight: 600,
    lineHeight: "16px",
  },
  badgeGreen: { background: "rgba(22,163,74,.1)", color: "#16a34a", border: "1px solid rgba(22,163,74,.3)" },
  badgeYellow: { background: "rgba(245,158,11,.1)", color: "#b45309", border: "1px solid rgba(245,158,11,.35)" },
  badgeRed: { background: "rgba(220,38,38,.08)", color: "#dc2626", border: "1px solid rgba(220,38,38,.3)" },
  badgeGray: { background: "rgba(154,162,177,.1)", color: "#5b6472", border: "1px solid rgba(154,162,177,.3)" },
  badgeBlue: { background: "rgba(37,99,235,.08)", color: "#2563eb", border: "1px solid rgba(37,99,235,.3)" },

  // --- Modal (logs & destroy confirm) -------------------------------------
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(15,23,42,.4)",
    backdropFilter: "blur(4px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
    padding: "24px",
  },
  modal: {
    background: "#ffffff",
    border: "1px solid #e6e8ee",
    borderRadius: "14px",
    width: "min(900px, 100%)",
    maxHeight: "85vh",
    display: "flex",
    flexDirection: "column",
    boxShadow: "0 24px 64px rgba(15,23,42,.18)",
  },
  modalHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "14px 18px",
    borderBottom: "1px solid #e6e8ee",
    gap: "10px",
  },
  modalTitle: { fontSize: "14px", fontWeight: 600, color: "#16181d" },
  modalBody: { padding: "16px 18px", overflowY: "auto", fontSize: "13px" },
  logBox: {
    background: "#f1f3f5",
    border: "1px solid #e6e8ee",
    borderRadius: "8px",
    padding: "12px",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: "12px",
    lineHeight: 1.55,
    color: "#16181d",
    whiteSpace: "pre-wrap",
    wordBreak: "break-all",
    minHeight: "200px",
    maxHeight: "58vh",
    overflowY: "auto",
    margin: 0,
  },
  modalFooter: {
    display: "flex",
    alignItems: "center",
    justifyContent: "flex-end",
    gap: "10px",
    padding: "12px 18px",
    borderTop: "1px solid #e6e8ee",
  },
  input: {
    padding: "8px 12px",
    border: "1px solid #d5dae3",
    borderRadius: "8px",
    background: "#f7f8fa",
    color: "#16181d",
    fontSize: "13px",
    outline: "none",
    width: "100%",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  },
  warnText: {
    color: "#b45309",
    fontSize: "13px",
    lineHeight: 1.6,
    marginBottom: "12px",
  },
  /** Scrollable list of containers inside the batch-destroy modal. */
  destroyList: {
    border: "1px solid rgba(220,38,38,.25)",
    background: "rgba(220,38,38,.05)",
    borderRadius: "8px",
    padding: "8px 12px",
    marginBottom: "12px",
    maxHeight: "160px",
    overflowY: "auto",
    fontSize: "12px",
    lineHeight: 1.9,
  },

  // --- Toast & misc --------------------------------------------------------
  toast: {
    position: "fixed",
    bottom: "28px",
    left: "50%",
    transform: "translateX(-50%)",
    background: "#16181d",
    border: "1px solid #16181d",
    color: "#ffffff",
    padding: "10px 18px",
    borderRadius: "10px",
    fontSize: "13px",
    boxShadow: "0 12px 36px rgba(15,23,42,.25)",
    zIndex: 1100,
    maxWidth: "80vw",
  },
  errorBanner: {
    background: "#fef2f2",
    border: "1px solid rgba(220,38,38,.3)",
    color: "#b91c1c",
    borderRadius: "10px",
    padding: "10px 14px",
    fontSize: "13px",
  },
  empty: {
    textAlign: "center",
    color: "#9aa2b1",
    padding: "40px 0",
    fontSize: "13px",
  },
  actionsCell: {
    display: "flex",
    gap: "6px",
    flexWrap: "wrap",
  },
  checkboxLabel: {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    fontSize: "12px",
    color: "#5b6472",
    cursor: "pointer",
    userSelect: "none",
  },
};
