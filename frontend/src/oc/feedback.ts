/** 用户体验反馈 —— 本轮上下文快照构建（任务一）。
 *
 * 采集范围严格遵循设计：从**上一条 user 提问**到**本条 assistant 的全部输出**。
 * 快照在前端组装（前端持有完整 turns），后端二次截断后落库。
 */
import type { Block, Turn } from "./messages";

/** 单个工具调用的快照（去除大字段冗余，保留可诊断信息）。 */
export interface ToolSnapshot {
  name: string;
  status: string;
  input?: unknown;
  output?: string;
  error?: string;
}

export interface TurnContext {
  user_message_id: string | null;
  user_text: string;
  assistant_message_id: string;
  assistant_text: string;
  assistant_reasoning: string;
  tools: ToolSnapshot[];
  model?: { providerID?: string; id?: string } | null;
  agent?: string | null;
  cost?: number | null;
  tokens?: unknown;
  turn_errored: boolean;
  error?: string | null;
}

function blockText(blocks: Block[], kind: "text" | "reasoning"): string {
  return blocks
    .filter((b) => b.kind === kind)
    .map((b) => (b as any).text ?? "")
    .filter(Boolean)
    .join("\n");
}

function toolSnapshots(blocks: Block[]): ToolSnapshot[] {
  return blocks
    .filter((b) => b.kind === "tool")
    .map((b) => {
      const t = b as any;
      return {
        name: t.name,
        status: t.status,
        input: t.input,
        output: typeof t.output === "string" ? t.output.slice(0, 4000) : undefined,
        error: t.error,
      };
    });
}

/**
 * 给定 turns 列表与目标 assistant 回合 id，向前找最近的 user 回合，
 * 组装完整上下文快照。找不到 assistant 回合返回 null。
 */
export function buildTurnContext(turns: Turn[], assistantId: string): TurnContext | null {
  const idx = turns.findIndex((t) => t.id === assistantId && t.role === "assistant");
  if (idx === -1) return null;
  const asst = turns[idx];

  // 向前找最近的 user 回合（本轮提问）。
  let user: Turn | null = null;
  for (let i = idx - 1; i >= 0; i--) {
    if (turns[i].role === "user") {
      user = turns[i];
      break;
    }
  }

  return {
    user_message_id: user?.id ?? null,
    user_text: user ? blockText(user.blocks, "text") : "",
    assistant_message_id: asst.id,
    assistant_text: blockText(asst.blocks, "text"),
    assistant_reasoning: blockText(asst.blocks, "reasoning"),
    tools: toolSnapshots(asst.blocks),
    model: asst.model ?? null,
    agent: asst.agent ?? null,
    cost: asst.cost ?? null,
    tokens: asst.tokens ?? null,
    turn_errored: !!asst.error,
    error: asst.error ?? null,
  };
}

/** 点踩原因码 → 中文标签（与后端白名单一致）。 */
export const REASON_OPTIONS: { code: string; label: string }[] = [
  { code: "misunderstood", label: "没理解我的意图" },
  { code: "wrong_answer", label: "答案错误" },
  { code: "tool_failure", label: "工具调用失败" },
  { code: "too_verbose", label: "太啰嗦" },
  { code: "ignored_constraints", label: "忽略了约束/要求" },
  { code: "interrupted", label: "中途被打断" },
  { code: "other", label: "其他" },
];
