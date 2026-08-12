/** Normalisation + streaming reducer for opencode's message model.
 *
 * Shapes below are taken from opencode 1.18.16's OpenAPI document, not guessed:
 *
 *   GET /api/session/{sessionID}/message -> { data: SessionMessage[], cursor }
 *
 *   SessionMessage is a discriminated union on `type`:
 *     user            { id, time, text, files[], agents[] }
 *     assistant       { id, time, agent, model, content[], cost, tokens, error }
 *     system          { id, time, text }
 *     synthetic       { id, time, sessionID, text }
 *     shell           { id, time, callID, command, output }
 *     compaction      { id, time, reason, summary, recent }
 *     agent-switched  { id, time, agent }
 *     model-switched  { id, time, model }
 *
 *   assistant.content[] is a union on `type`:
 *     text       { type, id, text }
 *     reasoning  { type, id, text, time }
 *     tool       { type, id, name, state: { status, input, content[], error }, time }
 *
 * The SSE stream (GET /api/event) reports the same information incrementally
 * via `session.next.*` events, so both paths converge on the `Turn` type here.
 */
import type { ModelRef } from "../api";

export type ToolStatus = "pending" | "running" | "completed" | "error";

export type Block =
  | { kind: "text"; id: string; text: string }
  | { kind: "reasoning"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      status: ToolStatus;
      input?: unknown;
      output?: string;
      error?: string;
    };

export interface Turn {
  id: string;
  role: "user" | "assistant" | "system";
  /** Original opencode message type, kept for badges/debugging. */
  type: string;
  blocks: Block[];
  model?: ModelRef;
  agent?: string;
  cost?: number;
  tokens?: { input?: number; output?: number; reasoning?: number };
  error?: string;
  streaming?: boolean;
  created?: number;
}

/** opencode returns tool output as LLMToolContent[]; flatten to displayable text. */
export function flattenToolContent(content: unknown, fallback?: unknown): string {
  if (Array.isArray(content)) {
    const parts = content
      .map((c: any) => {
        if (typeof c === "string") return c;
        if (c && typeof c === "object") {
          if (typeof c.text === "string") return c.text;
          if (c.type === "image") return "[image]";
        }
        return "";
      })
      .filter(Boolean);
    if (parts.length) return parts.join("\n");
  }
  if (typeof fallback === "string") return fallback;
  if (fallback != null) {
    try {
      return JSON.stringify(fallback, null, 2);
    } catch {
      return String(fallback);
    }
  }
  return "";
}

function errText(err: unknown): string {
  if (!err) return "";
  if (typeof err === "string") return err;
  const e = err as any;
  return e.message || e.data?.message || e.type || JSON.stringify(e);
}

/** Convert one persisted SessionMessage into a renderable Turn. */
export function toTurn(msg: any): Turn | null {
  if (!msg || typeof msg !== "object") return null;
  const base = { id: msg.id, created: msg.time?.created, type: msg.type };

  switch (msg.type) {
    case "user":
      return {
        ...base,
        role: "user",
        blocks: [{ kind: "text", id: `${msg.id}:text`, text: msg.text ?? "" }],
      };

    case "assistant": {
      const blocks: Block[] = [];
      for (const c of msg.content ?? []) {
        if (c.type === "text") {
          blocks.push({ kind: "text", id: c.id, text: c.text ?? "" });
        } else if (c.type === "reasoning") {
          blocks.push({ kind: "reasoning", id: c.id, text: c.text ?? "" });
        } else if (c.type === "tool") {
          const st = c.state ?? {};
          blocks.push({
            kind: "tool",
            id: c.id,
            name: c.name,
            status: (st.status as ToolStatus) ?? "pending",
            input: st.input,
            output:
              st.status === "completed"
                ? flattenToolContent(st.content, st.result)
                : undefined,
            error: st.status === "error" ? errText(st.error) : undefined,
          });
        }
      }
      return {
        ...base,
        role: "assistant",
        blocks,
        model: msg.model,
        agent: msg.agent,
        cost: msg.cost,
        tokens: msg.tokens,
        error: msg.error ? errText(msg.error) : undefined,
        streaming: !msg.time?.completed && !msg.error,
      };
    }

    case "shell":
      return {
        ...base,
        role: "assistant",
        blocks: [
          {
            kind: "tool",
            id: msg.callID ?? msg.id,
            name: "shell",
            status: msg.time?.completed ? "completed" : "running",
            input: { command: msg.command },
            output: msg.output,
          },
        ],
      };

    case "system":
    case "synthetic":
      return {
        ...base,
        role: "system",
        blocks: [{ kind: "text", id: `${msg.id}:text`, text: msg.text ?? "" }],
      };

    case "compaction":
      return {
        ...base,
        role: "system",
        blocks: [
          {
            kind: "text",
            id: `${msg.id}:text`,
            text: `上下文已压缩(${msg.reason})：${msg.summary ?? ""}`,
          },
        ],
      };

    case "agent-switched":
      return {
        ...base,
        role: "system",
        blocks: [{ kind: "text", id: `${msg.id}:t`, text: `已切换 agent → ${msg.agent}` }],
      };

    case "model-switched":
      return {
        ...base,
        role: "system",
        blocks: [
          {
            kind: "text",
            id: `${msg.id}:t`,
            text: `已切换模型 → ${msg.model?.providerID}/${msg.model?.id}`,
          },
        ],
      };

    default:
      return null;
  }
}

export function toTurns(messages: any[]): Turn[] {
  return (messages ?? []).map(toTurn).filter((t): t is Turn => t !== null);
}

// ---------------------------------------------------------------------------
//  Streaming reducer
// ---------------------------------------------------------------------------

function upsertTurn(turns: Turn[], id: string, patch: (t: Turn) => Turn): Turn[] {
  const idx = turns.findIndex((t) => t.id === id);
  if (idx === -1) {
    const fresh: Turn = { id, role: "assistant", type: "assistant", blocks: [], streaming: true };
    return [...turns, patch(fresh)];
  }
  const next = turns.slice();
  next[idx] = patch(next[idx]);
  return next;
}

function upsertBlock(turn: Turn, id: string, patch: (b: Block | undefined) => Block): Turn {
  const idx = turn.blocks.findIndex((b) => b.id === id);
  const blocks = turn.blocks.slice();
  if (idx === -1) blocks.push(patch(undefined));
  else blocks[idx] = patch(blocks[idx]);
  return { ...turn, blocks };
}

export interface ReduceResult {
  turns: Turn[];
  /** Set when the event means "generation finished" for the active session. */
  idle?: boolean;
  /** Session-level error surfaced to the banner. */
  error?: string;
  /** Model switched inside the container (e.g. by opencode itself). */
  model?: ModelRef;
}

/**
 * Apply one opencode SSE event to the turn list.
 *
 * `data.sessionID` filtering is the caller's job — this only handles events
 * already known to belong to the visible session.
 */
export function reduceEvent(turns: Turn[], type: string, data: any): ReduceResult {
  const mid: string = data?.assistantMessageID;

  switch (type) {
    case "session.next.prompted":
    case "session.next.prompt.admitted": {
      // Reconcile the optimistic user bubble with the real message id.
      const text = data?.prompt?.text ?? data?.prompt?.parts?.[0]?.text;
      const pending = turns.findIndex((t) => t.id === "pending-user");
      if (pending !== -1 && data?.messageID) {
        const next = turns.slice();
        next[pending] = {
          ...next[pending],
          id: data.messageID,
          blocks: text
            ? [{ kind: "text", id: `${data.messageID}:text`, text }]
            : next[pending].blocks,
        };
        return { turns: next };
      }
      if (data?.messageID && !turns.some((t) => t.id === data.messageID) && text) {
        return {
          turns: [
            ...turns,
            {
              id: data.messageID,
              role: "user",
              type: "user",
              blocks: [{ kind: "text", id: `${data.messageID}:text`, text }],
            },
          ],
        };
      }
      return { turns };
    }

    case "session.next.step.started":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) => ({
          ...t,
          streaming: true,
          agent: data.agent ?? t.agent,
          model: data.model ?? t.model,
        })),
      };

    case "session.next.text.started":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.textID, (b) =>
            b ?? { kind: "text", id: data.textID, text: "" }
          )
        ),
      };

    case "session.next.text.delta":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.textID, (b) => ({
            kind: "text",
            id: data.textID,
            text: ((b as any)?.text ?? "") + (data.delta ?? ""),
          }))
        ),
      };

    case "session.next.text.ended":
      if (!mid) return { turns };
      // `data.text` is authoritative — replaces whatever the deltas accumulated.
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.textID, () => ({
            kind: "text",
            id: data.textID,
            text: data.text ?? "",
          }))
        ),
      };

    case "session.next.reasoning.delta":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.reasoningID, (b) => ({
            kind: "reasoning",
            id: data.reasoningID,
            text: ((b as any)?.text ?? "") + (data.delta ?? ""),
          }))
        ),
      };

    case "session.next.tool.called":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.callID, () => ({
            kind: "tool",
            id: data.callID,
            name: data.tool,
            status: "running",
            input: data.input,
          }))
        ),
      };

    case "session.next.tool.progress":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.callID, (b) => ({
            kind: "tool",
            id: data.callID,
            name: (b as any)?.name ?? "tool",
            status: "running",
            input: (b as any)?.input,
            output: flattenToolContent(data.content) || (b as any)?.output,
          }))
        ),
      };

    case "session.next.tool.success":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.callID, (b) => ({
            kind: "tool",
            id: data.callID,
            name: (b as any)?.name ?? "tool",
            status: "completed",
            input: (b as any)?.input,
            output: flattenToolContent(data.content, data.result),
          }))
        ),
      };

    case "session.next.tool.failed":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) =>
          upsertBlock(t, data.callID, (b) => ({
            kind: "tool",
            id: data.callID,
            name: (b as any)?.name ?? "tool",
            status: "error",
            input: (b as any)?.input,
            error: errText(data.error) || "tool failed",
          }))
        ),
      };

    case "session.next.step.ended":
      if (!mid) return { turns };
      return {
        turns: upsertTurn(turns, mid, (t) => ({
          ...t,
          cost: data.cost ?? t.cost,
          tokens: data.tokens ?? t.tokens,
        })),
      };

    case "session.next.step.failed": {
      const message = errText(data.error) || "模型调用失败";
      if (!mid) return { turns, idle: true, error: message };
      return {
        turns: upsertTurn(turns, mid, (t) => ({ ...t, streaming: false, error: message })),
        idle: true,
        error: message,
      };
    }

    case "session.next.model.switched":
      return { turns, model: data.model };

    case "session.idle":
      return { turns: turns.map((t) => (t.streaming ? { ...t, streaming: false } : t)), idle: true };

    case "session.error":
      return { turns, idle: true, error: errText(data.error) || "会话错误" };

    default:
      return { turns };
  }
}
