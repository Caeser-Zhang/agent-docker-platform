/**
 * Styles for the admin Docker management panel — light theme matching Chat.
 *
 * Design tokens (same palette as chatStyles.ts):
 *   page var(--bg) · surface var(--surface) · subtle var(--surface-2) · border var(--border)
 *   text var(--text) · secondary var(--text-2) · muted var(--text-3)
 *   accent var(--scope-project) · green var(--green) · danger var(--red)
 *
 * Hover / focus / transition states can't be expressed as inline styles, so
 * `adminCss` is injected once by AdminPanel as a <style> tag; interactive
 * elements carry both an inline base style and an `adm-*` class.
 */

/** Class-based hover/focus/motion rules — see comment above. */
export const adminCss = `
  .adm-btn { transition: background-color .15s ease, border-color .15s ease, color .15s ease, box-shadow .15s ease, transform .15s ease; }
  .adm-btn:hover:not(:disabled) { background: var(--surface-3); border-color: var(--border-strong); color: var(--text); }
  .adm-btn:active:not(:disabled) { transform: translateY(1px); }
  .adm-btn-primary:hover:not(:disabled) { background: var(--scope-project); border-color: var(--scope-project); box-shadow: 0 4px 14px var(--scope-project-soft); }
  .adm-btn-warn:hover:not(:disabled) { background: var(--amber-soft); border-color: var(--amber); color: var(--amber); }
  .adm-btn-danger:hover:not(:disabled) { background: var(--red-soft); border-color: var(--red); color: var(--red); }
  .adm-btn:focus-visible, .adm-btn-primary:focus-visible, .adm-select:focus-visible, .adm-input:focus-visible, .adm-search:focus-visible {
    outline: 2px solid var(--scope-project); outline-offset: 1px;
  }
  .adm-select:hover, .adm-search:hover, .adm-input:hover { border-color: var(--border-strong); }
  .adm-select, .adm-search, .adm-input { transition: border-color .15s ease, box-shadow .15s ease; }

  .adm-card { transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease; }
  .adm-card:hover { border-color: var(--border-strong); transform: translateY(-1px); box-shadow: var(--shadow-1); }

  .adm-row { transition: background-color .12s ease; }
  .adm-row:hover { background: var(--surface-3); }
  .adm-row-selected, .adm-row-selected:hover { background: var(--scope-project-soft); }

  .adm-th-sort { cursor: pointer; user-select: none; }
  .adm-th-sort:hover { color: var(--text); }

  .adm-check { accent-color: var(--scope-project); width: 15px; height: 15px; cursor: pointer; flex-shrink: 0; }
  .adm-check:focus-visible { outline: 2px solid var(--scope-project); outline-offset: 2px; border-radius: 3px; }

  .adm-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
  .adm-scroll::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
  .adm-scroll::-webkit-scrollbar-thumb:hover { background: var(--text-3); }
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
    background: "var(--bg)",
    color: "var(--text)",
    fontFamily:
      "var(--sans)",
    overflow: "hidden",
  },

  // --- Header ------------------------------------------------------------
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 20px",
    borderBottom: "1px solid var(--border)",
    background: "var(--surface)",
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
    background: "var(--scope-project-soft)",
    border: "1px solid var(--scope-project-border)",
    color: "var(--scope-project)",
    flexShrink: 0,
  },
  headerTitle: { fontSize: "17px", fontWeight: 700, color: "var(--text)", letterSpacing: "0.2px" },
  headerSubtitle: { fontSize: "12px", color: "var(--text-3)", marginTop: "1px" },
  headerRight: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
  },
  whoami: {
    fontSize: "13px",
    color: "var(--text-2)",
    padding: "5px 12px",
    borderRadius: "999px",
    border: "1px solid var(--border)",
    background: "var(--surface-2)",
  },

  // --- Buttons -----------------------------------------------------------
  btn: {
    padding: "6px 14px",
    border: "1px solid var(--border-strong)",
    background: "var(--surface)",
    color: "var(--text-2)",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    lineHeight: "20px",
  },
  btnPrimary: {
    padding: "6px 14px",
    border: "1px solid var(--scope-project)",
    background: "var(--scope-project)",
    color: "var(--bg)",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 600,
    lineHeight: "20px",
  },
  btnDanger: {
    padding: "6px 14px",
    border: "1px solid var(--red-border)",
    background: "var(--red-soft)",
    color: "var(--red)",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    lineHeight: "20px",
  },
  btnSmall: {
    padding: "4px 10px",
    border: "1px solid var(--border-strong)",
    background: "var(--surface)",
    color: "var(--text-2)",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnSmallDanger: {
    padding: "4px 10px",
    border: "1px solid var(--red-border)",
    background: "var(--red-soft)",
    color: "var(--red)",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnSmallWarn: {
    padding: "4px 10px",
    border: "1px solid var(--amber-border)",
    background: "var(--amber-soft)",
    color: "var(--amber)",
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
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: "12px",
    padding: "14px 16px",
  },
  cardLabel: { fontSize: "12px", color: "var(--text-3)", marginBottom: "6px", letterSpacing: "0.3px" },
  cardValue: { fontSize: "24px", fontWeight: 700, color: "var(--text)" },
  cardValueGreen: { fontSize: "24px", fontWeight: 700, color: "var(--green)" },
  cardValueRed: { fontSize: "24px", fontWeight: 700, color: "var(--red)" },
  cardSub: { fontSize: "11px", color: "var(--text-3)", marginTop: "4px" },

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
    border: "1px solid var(--border-strong)",
    background: "var(--surface)",
    minWidth: "260px",
    flex: "0 1 300px",
  },
  searchIcon: { color: "var(--text-3)", display: "flex", flexShrink: 0 },
  search: {
    flex: 1,
    border: "none",
    background: "transparent",
    color: "var(--text)",
    fontSize: "13px",
    outline: "none",
    minWidth: 0,
  },
  select: {
    padding: "6px 10px",
    border: "1px solid var(--border-strong)",
    borderRadius: "8px",
    background: "var(--surface)",
    color: "var(--text-2)",
    fontSize: "12.5px",
    cursor: "pointer",
    height: "34px",
  },
  toolbarHint: { fontSize: "12px", color: "var(--text-3)", marginLeft: "auto" },

  // --- Batch action bar ----------------------------------------------------
  batchBar: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
    flexShrink: 0,
    padding: "10px 14px",
    borderRadius: "10px",
    background: "var(--scope-project-soft)",
    border: "1px solid var(--scope-project-border)",
  },
  batchCount: { fontSize: "13px", fontWeight: 600, color: "var(--scope-project)" },
  batchProgress: { fontSize: "12px", color: "var(--scope-project)" },
  batchSpacer: { marginLeft: "auto", display: "flex", gap: "8px", alignItems: "center" },

  // --- Table -------------------------------------------------------------
  tableWrap: {
    background: "var(--surface)",
    border: "1px solid var(--border)",
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
    color: "var(--text-2)",
    fontWeight: 600,
    fontSize: "12px",
    letterSpacing: "0.3px",
    borderBottom: "1px solid var(--border)",
    position: "sticky" as const,
    top: 0,
    background: "var(--surface)",
    whiteSpace: "nowrap",
    zIndex: 1,
  },
  thCheck: {
    width: "36px",
    padding: "10px 8px 10px 14px",
    textAlign: "left",
    borderBottom: "1px solid var(--border)",
    position: "sticky" as const,
    top: 0,
    background: "var(--surface)",
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
  sortArrow: { display: "inline-flex", color: "var(--scope-project)" },
  sortArrowIdle: { display: "inline-flex", color: "var(--text-3)", opacity: 0.7 },
  td: {
    padding: "10px 12px",
    borderBottom: "1px solid var(--border)",
    verticalAlign: "top",
    whiteSpace: "nowrap",
  },
  tdCheck: {
    padding: "10px 8px 10px 14px",
    borderBottom: "1px solid var(--border)",
    verticalAlign: "middle",
  },
  tdWrap: {
    padding: "10px 12px",
    borderBottom: "1px solid var(--border)",
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
    background: "linear-gradient(135deg, var(--scope-project), var(--scope-user))",
    color: "var(--bg)",
    fontSize: "11px",
    fontWeight: 700,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
  },
  uidText: {
    fontFamily: "var(--mono)",
    fontSize: "12px",
    color: "var(--scope-project)",
  },
  muted: { color: "var(--text-3)" },
  mono: { fontFamily: "var(--mono)", fontSize: "12px" },

  // --- Status badges -------------------------------------------------------
  badge: {
    display: "inline-block",
    padding: "2px 9px",
    borderRadius: "999px",
    fontSize: "11px",
    fontWeight: 600,
    lineHeight: "16px",
  },
  badgeGreen: { background: "var(--green-soft)", color: "var(--green)", border: "1px solid var(--green-border)" },
  badgeYellow: { background: "var(--amber-soft)", color: "var(--amber)", border: "1px solid var(--amber-border)" },
  badgeRed: { background: "var(--red-soft)", color: "var(--red)", border: "1px solid var(--red-border)" },
  badgeGray: { background: "var(--surface-3)", color: "var(--text-2)", border: "1px solid var(--border-strong)" },
  badgeBlue: { background: "var(--scope-project-soft)", color: "var(--scope-project)", border: "1px solid var(--scope-project-border)" },

  // --- Modal (logs & destroy confirm) -------------------------------------
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(5,3,15,.55)",
    backdropFilter: "blur(4px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
    padding: "24px",
  },
  modal: {
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: "14px",
    width: "min(900px, 100%)",
    maxHeight: "85vh",
    display: "flex",
    flexDirection: "column",
    boxShadow: "var(--shadow-3)",
  },
  modalHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "14px 18px",
    borderBottom: "1px solid var(--border)",
    gap: "10px",
  },
  modalTitle: { fontSize: "14px", fontWeight: 600, color: "var(--text)" },
  modalBody: { padding: "16px 18px", overflowY: "auto", fontSize: "13px" },
  logBox: {
    background: "var(--surface-2)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    padding: "12px",
    fontFamily: "var(--mono)",
    fontSize: "12px",
    lineHeight: 1.55,
    color: "var(--text)",
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
    borderTop: "1px solid var(--border)",
  },
  input: {
    padding: "8px 12px",
    border: "1px solid var(--border-strong)",
    borderRadius: "8px",
    background: "var(--bg)",
    color: "var(--text)",
    fontSize: "13px",
    outline: "none",
    width: "100%",
    fontFamily: "var(--mono)",
  },
  warnText: {
    color: "var(--amber)",
    fontSize: "13px",
    lineHeight: 1.6,
    marginBottom: "12px",
  },
  /** Scrollable list of containers inside the batch-destroy modal. */
  destroyList: {
    border: "1px solid var(--red-border)",
    background: "var(--red-soft)",
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
    background: "var(--text)",
    border: "1px solid var(--text)",
    color: "var(--bg)",
    padding: "10px 18px",
    borderRadius: "10px",
    fontSize: "13px",
    boxShadow: "var(--shadow-2)",
    zIndex: 1100,
    maxWidth: "80vw",
  },
  errorBanner: {
    background: "var(--red-soft)",
    border: "1px solid var(--red-border)",
    color: "var(--red)",
    borderRadius: "10px",
    padding: "10px 14px",
    fontSize: "13px",
  },
  empty: {
    textAlign: "center",
    color: "var(--text-3)",
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
    color: "var(--text-2)",
    cursor: "pointer",
    userSelect: "none",
  },
};
