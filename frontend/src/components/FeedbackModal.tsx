/** 点踩原因收集弹窗（任务一）。
 *
 * 多选原因码（白名单）+「其他」自由文本（选了 other 或任意时候都可填）。
 * 提交后不可取消；关闭即视为放弃本次点踩（不提交、不锁定）。
 */
import { useState, type CSSProperties } from "react";
import { REASON_OPTIONS } from "../oc/feedback";

const overlay: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.45)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 2000,
};

const card: CSSProperties = {
  width: "min(440px, 92vw)",
  background: "var(--surface)",
  color: "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: "12px",
  padding: "18px",
  boxShadow: "0 12px 40px rgba(0,0,0,0.35)",
};

const title: CSSProperties = { fontSize: "15px", fontWeight: 600, marginBottom: "4px" };
const sub: CSSProperties = { fontSize: "12px", color: "var(--text-3)", marginBottom: "14px" };

const chipRow: CSSProperties = { display: "flex", flexWrap: "wrap", gap: "8px", marginBottom: "14px" };

const chip = (active: boolean): CSSProperties => ({
  border: "1px solid var(--border)",
  background: active ? "var(--primary-soft)" : "transparent",
  color: active ? "var(--primary)" : "var(--text-2)",
  cursor: "pointer",
  fontSize: "12px",
  padding: "5px 10px",
  borderRadius: "16px",
});

const textarea: CSSProperties = {
  width: "100%",
  minHeight: "70px",
  resize: "vertical",
  boxSizing: "border-box",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  background: "var(--surface-2)",
  color: "var(--text)",
  fontSize: "13px",
  padding: "8px",
  marginBottom: "14px",
};

const footer: CSSProperties = { display: "flex", justifyContent: "flex-end", gap: "8px" };

const btnBase: CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: "8px",
  cursor: "pointer",
  fontSize: "13px",
  padding: "6px 14px",
};

export function FeedbackModal({
  busy,
  onCancel,
  onSubmit,
}: {
  busy?: boolean;
  onCancel: () => void;
  /** 提交选中的原因码与「其他」文本。 */
  onSubmit: (codes: string[], text: string | null) => void;
}) {
  const [codes, setCodes] = useState<string[]>([]);
  const [text, setText] = useState("");

  const toggle = (code: string) =>
    setCodes((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]));

  const canSubmit = !busy && (codes.length > 0 || text.trim().length > 0);

  const submit = () => {
    if (!canSubmit) return;
    onSubmit(codes, text.trim() ? text.trim().slice(0, 500) : null);
  };

  return (
    <div style={overlay} onClick={busy ? undefined : onCancel}>
      <div style={card} onClick={(e) => e.stopPropagation()}>
        <div style={title}>这条回复哪里有问题？</div>
        <div style={sub}>可多选。你的反馈将用于改进体验，提交后不可修改。</div>

        <div style={chipRow}>
          {REASON_OPTIONS.map((o) => (
            <button key={o.code} style={chip(codes.includes(o.code))} onClick={() => toggle(o.code)}>
              {o.label}
            </button>
          ))}
        </div>

        <textarea
          style={textarea}
          placeholder="补充说明（可选，最多 500 字）"
          value={text}
          maxLength={500}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
        />

        <div style={footer}>
          <button
            style={{ ...btnBase, background: "transparent", color: "var(--text-2)" }}
            onClick={onCancel}
            disabled={busy}
          >
            取消
          </button>
          <button
            style={{
              ...btnBase,
              background: canSubmit ? "var(--primary)" : "var(--surface-2)",
              color: canSubmit ? "#fff" : "var(--text-3)",
              cursor: canSubmit ? "pointer" : "default",
            }}
            onClick={submit}
            disabled={!canSubmit}
          >
            {busy ? "提交中…" : "提交反馈"}
          </button>
        </div>
      </div>
    </div>
  );
}
