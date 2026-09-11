/** 点赞/点踩反馈条（任务一）—— 挂在每条已完成的 assistant 回复下方。
 *
 * 交互约定（见设计）：
 *   - 未投票：显示 👍 / 👎 两个按钮。
 *   - 点 👍：直接提交并锁定，显示"感谢反馈"。
 *   - 点 👎：弹出 FeedbackModal 收集原因，提交后锁定。
 *   - 一旦投票不可取消；提交中禁用按钮（乐观锁定）。
 */
import type { CSSProperties } from "react";

const bar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "6px",
  marginTop: "6px",
};

const btn = (active: boolean, busy: boolean): CSSProperties => ({
  border: "1px solid var(--border)",
  background: active ? "var(--primary-soft)" : "transparent",
  color: active ? "var(--primary)" : "var(--text-3)",
  cursor: busy ? "default" : "pointer",
  fontSize: "12px",
  lineHeight: 1,
  padding: "3px 8px",
  borderRadius: "8px",
  opacity: busy ? 0.6 : 1,
});

const thanks: CSSProperties = {
  fontSize: "11px",
  color: "var(--text-3)",
  marginLeft: "2px",
};

export function FeedbackBar({
  verdict,
  busy,
  onUp,
  onDown,
}: {
  /** 已锁定的投票（来自后端回填或本次提交）；undefined 表示未投票。 */
  verdict?: "up" | "down";
  /** 提交进行中 —— 禁用交互（乐观锁定）。 */
  busy?: boolean;
  onUp: () => void;
  onDown: () => void;
}) {
  const locked = !!verdict || busy;
  return (
    <div style={bar}>
      <button
        style={btn(verdict === "up", !!busy)}
        title={verdict ? "已评价，不可修改" : "有帮助"}
        disabled={locked}
        onClick={onUp}
      >
        👍{verdict === "up" ? " 已赞" : ""}
      </button>
      <button
        style={btn(verdict === "down", !!busy)}
        title={verdict ? "已评价，不可修改" : "有问题"}
        disabled={locked}
        onClick={onDown}
      >
        👎{verdict === "down" ? " 已踩" : ""}
      </button>
      {verdict && <span style={thanks}>感谢反馈</span>}
      {busy && <span style={thanks}>提交中…</span>}
    </div>
  );
}
