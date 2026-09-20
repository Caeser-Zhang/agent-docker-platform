import type { CSSProperties } from "react";
import type { WishScope, WishSort, WishStatus, WishType } from "./api";

/**
 * 心愿墙的标签映射与样式（设计文档 §7.3 / §7.6）。
 *
 * 标签与颜色集中在此，供 WishWall 卡片、发布/编辑弹窗、以及意见反馈弹窗的
 * 「转心愿引导态」复用——避免同一枚举在三处各写一份中文文案。
 * 颜色一律走 theme.css 的 CSS 变量，明暗主题自动适配。
 */

// --- 枚举文案 -------------------------------------------------------------

export const WISH_TYPE_LABEL: Record<WishType, string> = {
  model: "模型",
  memory: "上下文记忆",
  ux: "用户体验",
  other: "其他",
};

/** antd Tag 四色映射（§7.3 卡片字段 3）。 */
export const WISH_TYPE_COLOR: Record<WishType, string> = {
  model: "purple",
  memory: "cyan",
  ux: "gold",
  other: "default",
};

export const WISH_STATUS_LABEL: Record<WishStatus, string> = {
  evaluating: "评估中",
  planned: "已规划",
  developing: "开发中",
  done: "已实现",
};

/** 评估中=default / 已规划=processing / 开发中=warning / 已实现=success（§7.3 字段 4）。 */
export const WISH_STATUS_COLOR: Record<WishStatus, string> = {
  evaluating: "default",
  planned: "processing",
  developing: "warning",
  done: "success",
};

export const WISH_TYPE_OPTIONS = (Object.keys(WISH_TYPE_LABEL) as WishType[]).map(
  (v) => ({ value: v, label: WISH_TYPE_LABEL[v] })
);

export const WISH_STATUS_OPTIONS = (Object.keys(WISH_STATUS_LABEL) as WishStatus[]).map(
  (v) => ({ value: v, label: WISH_STATUS_LABEL[v] })
);

/** scope Segmented 四项（D20，单选）。 */
export const WISH_SCOPE_OPTIONS: { value: WishScope; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "mine", label: "我创建的" },
  { value: "favorited", label: "我收藏的" },
  { value: "boosted", label: "我助力的" },
];

/** 6 种排序，默认助力数从多到少（D21）。 */
export const WISH_SORT_OPTIONS: { value: WishSort; label: string }[] = [
  { value: "boost_desc", label: "助力数从多到少" },
  { value: "boost_asc", label: "助力数从少到多" },
  { value: "favorite_desc", label: "收藏数从多到少" },
  { value: "favorite_asc", label: "收藏数从少到多" },
  { value: "created_desc", label: "最新发布" },
  { value: "created_asc", label: "最早发布" },
];

// --- 样式 -----------------------------------------------------------------

const border = "var(--border)";
const surface = "var(--surface)";
const subtle = "var(--surface-2)";
const text = "var(--text)";
const textSec = "var(--text-2)";
const textMut = "var(--text-3)";
const accent = "var(--primary)";
const accentSoft = "var(--primary-soft)";

export const wishStyles: Record<string, CSSProperties> = {
  page: {
    height: "100vh",
    overflowY: "auto",
    background: "var(--bg)",
    color: text,
    padding: "20px 24px 40px",
    boxSizing: "border-box",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    marginBottom: "16px",
    flexWrap: "wrap",
  },
  title: { fontSize: "18px", fontWeight: 600, margin: 0 },
  subtitle: { fontSize: "12px", color: textMut },
  headerSpacer: { flex: 1 },

  // 顶部统计条（D27）
  statsRow: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
    gap: "10px",
    marginBottom: "14px",
  },
  statCard: {
    background: surface,
    border: `1px solid ${border}`,
    borderRadius: "var(--radius)",
    padding: "10px 12px",
  },
  statLabel: { fontSize: "12px", color: textMut, marginBottom: "4px" },
  statValue: { fontSize: "20px", fontWeight: 600, lineHeight: "24px" },

  // 工具栏
  toolbar: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
    marginBottom: "14px",
  },

  // 卡片网格：auto-fill + minmax(320px, 1fr)，窄屏自然退化为单列（§7.6）
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
    gap: "12px",
  },
  card: {
    background: surface,
    border: `1px solid ${border}`,
    borderRadius: "var(--radius-lg)",
    padding: "14px",
    display: "flex",
    flexDirection: "column",
    gap: "8px",
    transition: "border-color var(--t), box-shadow var(--t)",
  },
  cardDeleted: {
    opacity: 0.6,
    borderStyle: "dashed",
  },
  cardTitleRow: {
    display: "flex",
    alignItems: "flex-start",
    gap: "8px",
  },
  cardTitle: {
    fontSize: "15px",
    fontWeight: 600,
    lineHeight: "22px",
    flex: 1,
    wordBreak: "break-word",
  },
  tagRow: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" },
  // 描述：默认 clamp 3 行，展开态取消 clamp（D15）
  desc: {
    fontSize: "13px",
    color: textSec,
    lineHeight: "20px",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  descClamp: {
    display: "-webkit-box",
    WebkitLineClamp: 3,
    WebkitBoxOrient: "vertical",
    overflow: "hidden",
  },
  expandLink: {
    background: "none",
    border: "none",
    padding: 0,
    cursor: "pointer",
    fontSize: "12px",
    color: accent,
  },
  metaRow: {
    fontSize: "12px",
    color: textMut,
    display: "flex",
    alignItems: "center",
    gap: "10px",
    flexWrap: "wrap",
  },
  actionRow: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    marginTop: "auto",
    paddingTop: "8px",
    borderTop: `1px solid ${border}`,
    flexWrap: "wrap",
  },
  actionSpacer: { flex: 1 },
  actionBtn: {
    display: "inline-flex",
    alignItems: "center",
    gap: "4px",
    border: `1px solid ${border}`,
    background: "transparent",
    color: textSec,
    borderRadius: "999px",
    fontSize: "12px",
    height: "28px",
    padding: "0 12px",
    cursor: "pointer",
    transition: "all var(--t)",
  },
  actionBtnActive: {
    borderColor: "var(--primary-border)",
    background: accentSoft,
    color: accent,
    fontWeight: 600,
  },
  linkBtn: {
    background: "none",
    border: "none",
    padding: "0 4px",
    cursor: "pointer",
    fontSize: "12px",
    color: textSec,
  },
  dangerBtn: {
    background: "none",
    border: "none",
    padding: "0 4px",
    cursor: "pointer",
    fontSize: "12px",
    color: "var(--red)",
  },
  empty: {
    padding: "48px 16px",
    textAlign: "center",
    color: textMut,
    fontSize: "13px",
    background: surface,
    border: `1px solid ${border}`,
    borderRadius: "var(--radius-lg)",
  },
  pagerRow: {
    display: "flex",
    justifyContent: "center",
    marginTop: "18px",
  },
  // 后台「查看心愿」跳入后的 2 秒定位高亮（§7.3）
  highlight: {
    borderColor: accent,
    boxShadow: `0 0 0 3px ${accentSoft}`,
  },
  formHint: { fontSize: "12px", color: textMut, marginTop: "4px" },
  formError: { fontSize: "12px", color: "var(--red)", marginTop: "4px" },
  fieldLabel: {
    fontSize: "12px",
    color: textSec,
    fontWeight: 600,
    marginBottom: "6px",
    display: "block",
  },
  field: { marginBottom: "14px" },
  guideNote: {
    fontSize: "12px",
    color: textMut,
    lineHeight: "20px",
    background: subtle,
    border: `1px solid ${border}`,
    borderRadius: "var(--radius-sm)",
    padding: "8px 10px",
    marginBottom: "14px",
  },
};
