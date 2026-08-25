/**
 * Styles for the admin Docker management panel — dark theme matching Chat.
 *
 * Design system (ui-ux-pro-max, "Dark tech + status green"):
 *   bg #0F172A · card #1B2336 · muted-fg #94A3B8 · borders rgba(148,163,184,.14)
 *   interactive indigo #6366F1 · running/healthy green #22C55E · danger #EF4444
 *
 * Hover / focus / transition states can't be expressed as inline styles, so
 * `adminCss` is injected once by AdminPanel as a <style> tag; interactive
 * elements carry both an inline base style and an `adm-*` class.
 */

/** Class-based hover/focus/motion rules — see comment above. */
export const adminCss = `
  .adm-btn { transition: background-color .15s ease, border-color .15s ease, color .15s ease, box-shadow .15s ease, transform .15s ease; }
  .adm-btn:hover:not(:disabled) { background: #26314b; border-color: #3d4a68; color: #f1f5f9; }
  .adm-btn:active:not(:disabled) { transform: translateY(1px); }
  .adm-btn-primary:hover:not(:disabled) { background: #4f46e5; border-color: #6366f1; box-shadow: 0 4px 14px rgba(99,102,241,.35); }
  .adm-btn-warn:hover:not(:disabled) { background: #3a2f0e; border-color: #a16207; color: #fde047; }
  .adm-btn-danger:hover:not(:disabled) { background: #45161b; border-color: #b91c1c; color: #fca5a5; }
  .adm-btn:focus-visible, .adm-btn-primary:focus-visible, .adm-select:focus-visible, .adm-input:focus-visible, .adm-search:focus-visible {
    outline: 2px solid rgba(129,140,248,.75); outline-offset: 1px;
  }
  .adm-select:hover, .adm-search:hover, .adm-input:hover { border-color: #3d4a68; }
  .adm-select, .adm-search, .adm-input { transition: border-color .15s ease, box-shadow .15s ease; }

  .adm-card { transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease; }
  .adm-card:hover { border-color: rgba(148,163,184,.3); transform: translateY(-1px); box-shadow: 0 8px 24px rgba(2,6,23,.45); }

  .adm-row { transition: background-color .12s ease; }
  .adm-row:hover { background: rgba(99,102,241,.05); }
  .adm-row-selected, .adm-row-selected:hover { background: rgba(99,102,241,.1); }

  .adm-th-sort { cursor: pointer; user-select: none; }
  .adm-th-sort:hover { color: #e2e8f0; }

  .adm-check { accent-color: #6366f1; width: 15px; height: 15px; cursor: pointer; flex-shrink: 0; }
  .adm-check:focus-visible { outline: 2px solid rgba(129,140,248,.75); outline-offset: 2px; border-radius: 3px; }

  .adm-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
  .adm-scroll::-webkit-scrollbar-thumb { background: #33415a; border-radius: 4px; }
  .adm-scroll::-webkit-scrollbar-thumb:hover { background: #475569; }
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
    background: "#0f172a",
    color: "#e2e8f0",
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
    borderBottom: "1px solid rgba(148,163,184,.14)",
    background: "rgba(27,35,54,.92)",
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
    background: "rgba(99,102,241,.15)",
    border: "1px solid rgba(99,102,241,.35)",
    color: "#a5b4fc",
    flexShrink: 0,
  },
  headerTitle: { fontSize: "17px", fontWeight: 700, color: "#f8fafc", letterSpacing: "0.2px" },
  headerSubtitle: { fontSize: "12px", color: "#64748b", marginTop: "1px" },
  headerRight: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
  },
  whoami: {
    fontSize: "13px",
    color: "#94a3b8",
    padding: "5px 12px",
    borderRadius: "999px",
    border: "1px solid rgba(148,163,184,.18)",
    background: "rgba(15,23,42,.6)",
  },

  // --- Buttons -----------------------------------------------------------
  btn: {
    padding: "6px 14px",
    border: "1px solid rgba(148,163,184,.2)",
    background: "#1b2336",
    color: "#cbd5e1",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    lineHeight: "20px",
  },
  btnPrimary: {
    padding: "6px 14px",
    border: "1px solid #6366f1",
    background: "#6366f1",
    color: "#fff",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 600,
    lineHeight: "20px",
  },
  btnDanger: {
    padding: "6px 14px",
    border: "1px solid #b91c1c",
    background: "#2a1215",
    color: "#f87171",
    borderRadius: "7px",
    cursor: "pointer",
    fontSize: "13px",
    lineHeight: "20px",
  },
  btnSmall: {
    padding: "4px 10px",
    border: "1px solid rgba(148,163,184,.2)",
    background: "#1b2336",
    color: "#cbd5e1",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnSmallDanger: {
    padding: "4px 10px",
    border: "1px solid rgba(185,28,28,.6)",
    background: "#2a1215",
    color: "#f87171",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  btnSmallWarn: {
    padding: "4px 10px",
    border: "1px solid rgba(161,98,7,.6)",
    background: "#27200d",
    color: "#fbbf24",
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
    background: "#1b2336",
    border: "1px solid rgba(148,163,184,.14)",
    borderRadius: "12px",
    padding: "14px 16px",
  },
  cardLabel: { fontSize: "12px", color: "#64748b", marginBottom: "6px", letterSpacing: "0.3px" },
  cardValue: { fontSize: "24px", fontWeight: 700, color: "#f1f5f9" },
  cardValueGreen: { fontSize: "24px", fontWeight: 700, color: "#22c55e" },
  cardValueRed: { fontSize: "24px", fontWeight: 700, color: "#ef4444" },
  cardSub: { fontSize: "11px", color: "#64748b", marginTop: "4px" },

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
    border: "1px solid rgba(148,163,184,.2)",
    background: "#0f172a",
    minWidth: "260px",
    flex: "0 1 300px",
  },
  searchIcon: { color: "#64748b", display: "flex", flexShrink: 0 },
  search: {
    flex: 1,
    border: "none",
    background: "transparent",
    color: "#e2e8f0",
    fontSize: "13px",
    outline: "none",
    minWidth: 0,
  },
  select: {
    padding: "6px 10px",
    border: "1px solid rgba(148,163,184,.2)",
    borderRadius: "8px",
    background: "#0f172a",
    color: "#cbd5e1",
    fontSize: "12.5px",
    cursor: "pointer",
    height: "34px",
  },
  toolbarHint: { fontSize: "12px", color: "#64748b", marginLeft: "auto" },

  // --- Batch action bar ----------------------------------------------------
  batchBar: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
    flexShrink: 0,
    padding: "10px 14px",
    borderRadius: "10px",
    background: "rgba(99,102,241,.1)",
    border: "1px solid rgba(99,102,241,.4)",
  },
  batchCount: { fontSize: "13px", fontWeight: 600, color: "#c7d2fe" },
  batchProgress: { fontSize: "12px", color: "#a5b4fc" },
  batchSpacer: { marginLeft: "auto", display: "flex", gap: "8px", alignItems: "center" },

  // --- Table -------------------------------------------------------------
  tableWrap: {
    background: "#1b2336",
    border: "1px solid rgba(148,163,184,.14)",
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
    color: "#94a3b8",
    fontWeight: 600,
    fontSize: "12px",
    letterSpacing: "0.3px",
    borderBottom: "1px solid rgba(148,163,184,.18)",
    position: "sticky" as const,
    top: 0,
    background: "#1b2336",
    whiteSpace: "nowrap",
    zIndex: 1,
  },
  thCheck: {
    width: "36px",
    padding: "10px 8px 10px 14px",
    textAlign: "left",
    borderBottom: "1px solid rgba(148,163,184,.18)",
    position: "sticky" as const,
    top: 0,
    background: "#1b2336",
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
  sortArrow: { display: "inline-flex", color: "#818cf8" },
  sortArrowIdle: { display: "inline-flex", color: "#475569", opacity: 0.7 },
  td: {
    padding: "10px 12px",
    borderBottom: "1px solid rgba(148,163,184,.08)",
    verticalAlign: "top",
    whiteSpace: "nowrap",
  },
  tdCheck: {
    padding: "10px 8px 10px 14px",
    borderBottom: "1px solid rgba(148,163,184,.08)",
    verticalAlign: "middle",
  },
  tdWrap: {
    padding: "10px 12px",
    borderBottom: "1px solid rgba(148,163,184,.08)",
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
    background: "linear-gradient(135deg, #6366f1, #8b5cf6)",
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
    color: "#a5b4fc",
  },
  muted: { color: "#64748b" },
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
  badgeGreen: { background: "rgba(34,197,94,.12)", color: "#4ade80", border: "1px solid rgba(34,197,94,.35)" },
  badgeYellow: { background: "rgba(251,191,36,.1)", color: "#fbbf24", border: "1px solid rgba(251,191,36,.35)" },
  badgeRed: { background: "rgba(239,68,68,.12)", color: "#f87171", border: "1px solid rgba(239,68,68,.4)" },
  badgeGray: { background: "rgba(148,163,184,.08)", color: "#94a3b8", border: "1px solid rgba(148,163,184,.25)" },
  badgeBlue: { background: "rgba(96,165,250,.1)", color: "#93c5fd", border: "1px solid rgba(96,165,250,.35)" },

  // --- Modal (logs & destroy confirm) -------------------------------------
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(2,6,23,.7)",
    backdropFilter: "blur(4px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
    padding: "24px",
  },
  modal: {
    background: "#1b2336",
    border: "1px solid rgba(148,163,184,.2)",
    borderRadius: "14px",
    width: "min(900px, 100%)",
    maxHeight: "85vh",
    display: "flex",
    flexDirection: "column",
    boxShadow: "0 24px 64px rgba(2,6,23,.6)",
  },
  modalHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "14px 18px",
    borderBottom: "1px solid rgba(148,163,184,.14)",
    gap: "10px",
  },
  modalTitle: { fontSize: "14px", fontWeight: 600, color: "#f1f5f9" },
  modalBody: { padding: "16px 18px", overflowY: "auto", fontSize: "13px" },
  logBox: {
    background: "#0b1120",
    border: "1px solid rgba(148,163,184,.14)",
    borderRadius: "8px",
    padding: "12px",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: "12px",
    lineHeight: 1.55,
    color: "#c9d3e0",
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
    borderTop: "1px solid rgba(148,163,184,.14)",
  },
  input: {
    padding: "8px 12px",
    border: "1px solid rgba(148,163,184,.2)",
    borderRadius: "8px",
    background: "#0f172a",
    color: "#e2e8f0",
    fontSize: "13px",
    outline: "none",
    width: "100%",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  },
  warnText: {
    color: "#fbbf24",
    fontSize: "13px",
    lineHeight: 1.6,
    marginBottom: "12px",
  },
  /** Scrollable list of containers inside the batch-destroy modal. */
  destroyList: {
    border: "1px solid rgba(239,68,68,.25)",
    background: "rgba(239,68,68,.05)",
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
    background: "#1b2336",
    border: "1px solid rgba(148,163,184,.25)",
    color: "#e2e8f0",
    padding: "10px 18px",
    borderRadius: "10px",
    fontSize: "13px",
    boxShadow: "0 12px 36px rgba(2,6,23,.55)",
    zIndex: 1100,
    maxWidth: "80vw",
  },
  errorBanner: {
    background: "rgba(239,68,68,.1)",
    border: "1px solid rgba(239,68,68,.4)",
    color: "#f87171",
    borderRadius: "10px",
    padding: "10px 14px",
    fontSize: "13px",
  },
  empty: {
    textAlign: "center",
    color: "#64748b",
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
    color: "#94a3b8",
    cursor: "pointer",
    userSelect: "none",
  },
};
