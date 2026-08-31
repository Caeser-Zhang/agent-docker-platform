/** Normalisation + streaming reducer for opencode's message model.
 *
 * Shapes are taken from opencode 1.18.16's OpenAPI document, not guessed.
 * v1.18.16 speaks the durable V1 protocol:
 *
 *   GET /api/session/{sessionID}/message -> [{ info: Message, parts: Part[] }]
 *
 *   Message ("info") is a discriminated union on `role`:
 *     user       { id, sessionID, role:"user", time:{created}, agent, model }
 *     assistant  { id, sessionID, role:"assistant", time:{created, completed},
 *                  error, agent, model, cost, tokens }
 *
 *   Part[] holds the actual content, unioned on `type`:
 *     text       { id, messageID, type:"text", text, time }
 *     reasoning  { id, messageID, type:"reasoning", text, time }
 *     tool       { id, messageID, type:"tool", tool, callID,
 *                  state:{ status, input, output?, error?, title? } }
 *     patch      { id, messageID, type:"patch", hash, files: string[] }
 *                — live per-step workspace snapshot (git tree hash + touched
 *                file paths); the full per-file diff arrives after the round
 *                on the user message's `info.summary.diffs`.
 *     compaction { id, messageID, type:"compaction", auto, overflow?,
 *                  tail_start_id } — context was compacted (auto or manual).
 *     subtask    { id, messageID, type:"subtask", prompt, description, agent,
 *                  model?, command? } — delegation to a subagent.
 *     agent      { id, messageID, type:"agent", name } — which agent produced
 *                the message / was @-mentioned.
 *     retry      { id, messageID, type:"retry", attempt, error, time } — a
 *                failed model attempt that was retried.
 *     file       { id, messageID, type:"file", mime, filename?, url } —
 *                attachment part (user side).
 *     step-start / step-finish / snapshot  (not rendered)
 *
 * NOTE: the session todo list is NOT a part — it arrives as its own SSE event
 * `todo.updated` with payload { sessionID, todos: Todo[] }.
 *
 * The SSE stream (GET /api/event) reports the same data via the V1 events
 * `message.updated` (full-replace `info`) and `message.part.updated`
 * (full-replace `part`) — exactly how the official opencode web UI consumes
 * them. The older V2 `session.next.*` events are kept for compatibility.
 */
import type { ModelRef } from "../api";

export type ToolStatus = "pending" | "running" | "completed" | "error";

/** One file change of a completed round (user message `info.summary.diffs`). */
export interface FileDiff {
  file: string;
  patch?: string;
  additions?: number;
  deletions?: number;
  status?: string;
}

/**
 * Session todo list entry. NOT a message part — the list arrives as its own
 * SSE event `todo.updated` with payload { sessionID, todos: Todo[] } and is
 * rendered as one session-level card (like the official opencode UI).
 */
export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority?: "high" | "medium" | "low";
}

/** Defensively normalise the `todos` array of a `todo.updated` event. */
export function parseTodos(raw: unknown): TodoItem[] {
  if (!Array.isArray(raw)) return [];
  const statuses = new Set(["pending", "in_progress", "completed", "cancelled"]);
  return raw
    .filter((t: any) => t && typeof t.content === "string")
    .map((t: any) => ({
      content: t.content,
      status: statuses.has(t.status) ? t.status : "pending",
      priority: t.priority,
    }));
}

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
    }
  /** Live workspace snapshot emitted after each tool step (part type "patch"). */
  | { kind: "patch"; id: string; hash: string; files: string[] }
  /** Context-compaction marker (part type "compaction"). */
  | { kind: "compaction"; id: string; auto: boolean; overflow: boolean }
  /** Delegation of work to a subagent (part type "subtask"). */
  | { kind: "subtask"; id: string; agent: string; description: string; prompt?: string }
  /** Agent attribution marker (part type "agent"). */
  | { kind: "agentTag"; id: string; name: string }
  /** A failed model attempt that was retried (part type "retry"). */
  | { kind: "retry"; id: string; attempt: number; error?: string }
  /** File attachment part (part type "file"). */
  | { kind: "file"; id: string; filename?: string; mime: string; url: string };

export interface Turn {
  id: string;
  role: "user" | "assistant" | "system";
  /** Original opencode message type, kept for badges/debugging. */
  type: string;
  blocks: Block[];
  model?: ModelRef;
  agent?: string;
  /** @-mentioned agents on a user message (prompt.agents[].name). */
  agents?: string[];
  /** Attached file paths on a user message (prompt.files[]). */
  files?: string[];
  /** Per-file diffs of the round that followed this user message. */
  diffs?: FileDiff[];
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

/**
 * Converge assistant turns stranded by a container kill. When opencode is
 * SIGKILLed mid-tool-call (idle reclaim, restart), the persisted message
 * never gets `time.completed` and its tool parts stay "running"/"pending"
 * forever — and the pending approval itself was in-memory, so nobody can
 * ever answer it. Any assistant message created BEFORE the container's
 * current boot therefore cannot still be streaming; close it out so the
 * UI stops waiting on it.
 */
function reconcileDeadTurn(turn: Turn, startedAtMs?: number): Turn {
  if (!startedAtMs || turn.role !== "assistant" || !turn.streaming || !turn.created) return turn;
  if (turn.created >= startedAtMs) return turn;
  return {
    ...turn,
    streaming: false,
    blocks: turn.blocks.map((b) =>
      b.kind === "tool" && (b.status === "running" || b.status === "pending")
        ? { ...b, status: "error" as ToolStatus, error: "容器已重启，此工具调用已中断" }
        : b
    ),
  };
}

/** Convert one persisted SessionMessage into a renderable Turn. */
export function toTurn(msg: any, startedAtMs?: number): Turn | null {
  if (!msg || typeof msg !== "object") return null;
  const base = { id: msg.id, created: msg.time?.created, type: msg.type };

  switch (msg.type) {
    case "user":
      return {
        ...base,
        role: "user",
        blocks: [{ kind: "text", id: `${msg.id}:text`, text: msg.text ?? "" }],
        agents: Array.isArray(msg.agents)
          ? msg.agents.map((a: any) => a?.name ?? a).filter((x: any) => typeof x === "string")
          : undefined,
        files: Array.isArray(msg.files) ? msg.files : undefined,
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
      return reconcileDeadTurn({
        ...base,
        role: "assistant",
        blocks,
        model: msg.model,
        agent: msg.agent,
        cost: msg.cost,
        tokens: msg.tokens,
        error: msg.error ? errText(msg.error) : undefined,
        streaming: !msg.time?.completed && !msg.error,
      }, startedAtMs);
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

// Part types that carry transient UI-only state and are intentionally not
// rendered (mirrors the official opencode web UI's SKIP_PARTS set; "patch"
// IS rendered — it drives the live workspace-change card; "snapshot" is an
// internal checkpoint marker whose content duplicates the turn state).
const SKIP_PART_TYPES = new Set(["step-start", "step-finish", "snapshot"]);

/** Map one V1 Part into a renderable Block, or null if it isn't rendered. */
function partToBlock(part: any): Block | null {
  switch (part?.type) {
    case "text":
      return { kind: "text", id: part.id, text: part.text ?? "" };
    case "reasoning":
      return { kind: "reasoning", id: part.id, text: part.text ?? "" };
    case "patch":
      return {
        kind: "patch",
        id: part.id,
        hash: typeof part.hash === "string" ? part.hash : "",
        files: Array.isArray(part.files)
          ? part.files.filter((f: any) => typeof f === "string")
          : [],
      };
    case "compaction":
      return {
        kind: "compaction",
        id: part.id,
        auto: !!part.auto,
        overflow: !!part.overflow,
      };
    case "subtask":
      return {
        kind: "subtask",
        id: part.id,
        agent: typeof part.agent === "string" ? part.agent : "",
        description: typeof part.description === "string" ? part.description : "",
        prompt: typeof part.prompt === "string" ? part.prompt : undefined,
      };
    case "agent":
      return { kind: "agentTag", id: part.id, name: typeof part.name === "string" ? part.name : "" };
    case "retry":
      return {
        kind: "retry",
        id: part.id,
        attempt: typeof part.attempt === "number" ? part.attempt : 0,
        error: part.error ? errText(part.error) : undefined,
      };
    case "file":
      return {
        kind: "file",
        id: part.id,
        filename: typeof part.filename === "string" ? part.filename : undefined,
        mime: typeof part.mime === "string" ? part.mime : "",
        url: typeof part.url === "string" ? part.url : "",
      };
    case "tool": {
      const st = part.state ?? {};
      const status = (st.status as ToolStatus) ?? "pending";
      return {
        kind: "tool",
        id: part.id,
        name: part.tool ?? "",
        status,
        input: st.input,
        output: status === "completed" ? (st.output ?? "") : undefined,
        error: status === "error" ? (st.error ?? "tool failed") : undefined,
      };
    }
    default:
      return null;
  }
}

/** Build a Turn from a V1 Message (`info`) plus optional content parts. */
function turnFromInfo(info: any, parts?: any[], startedAtMs?: number): Turn {
  const role =
    info?.role === "user" ? "user" : info?.role === "assistant" ? "assistant" : "system";
  const blocks: Block[] = [];
  for (const p of parts ?? []) {
    if (!p || SKIP_PART_TYPES.has(p.type)) continue;
    const b = partToBlock(p);
    if (b) blocks.push(b);
  }
  if (role === "assistant") {
    return reconcileDeadTurn(
      {
        id: info.id,
        role: "assistant",
        type: "assistant",
        blocks,
        model: info.model,
        agent: info.agent,
        cost: info.cost,
        tokens: info.tokens,
        error: info.error ? errText(info.error) : undefined,
        streaming: !info.time?.completed && !info.error,
        created: info.time?.created,
      },
      startedAtMs
    );
  }
  // User messages carry the round's full file diffs on `info.summary.diffs`
  // (same payload as GET /session/{id}/diff?messageID=<user msg>).
  const diffs: FileDiff[] | undefined =
    role === "user" && Array.isArray(info?.summary?.diffs)
      ? info.summary.diffs.filter((d: any) => d && typeof d.file === "string")
      : undefined;
  return {
    id: info.id,
    role,
    type: role,
    blocks,
    diffs: diffs && diffs.length ? diffs : undefined,
    agent: info.agent,
    model: info.model,
    created: info.time?.created,
  };
}

/** Convert one V1 `{ info, parts }` record into a renderable Turn. */
export function toTurnFromInfoParts(info: any, parts?: any[], startedAtMs?: number): Turn | null {
  if (!info || typeof info !== "object" || !info.id) return null;
  return turnFromInfo(info, parts, startedAtMs);
}

/**
 * Convert the response of GET /session/{id}/message (a bare array of
 * `{ info, parts }` records in v1.18.16) into Turns. Legacy discriminated
 * `SessionMessage` items are still accepted for compatibility.
 *
 * `startedAtMs` (container's current boot, epoch ms) enables dead-turn
 * reconciliation — see reconcileDeadTurn.
 */
export function toTurns(messages: any[], startedAtMs?: number): Turn[] {
  const out: Turn[] = [];
  for (const m of messages ?? []) {
    if (m && typeof m === "object" && m.info) {
      const t = toTurnFromInfoParts(m.info, m.parts, startedAtMs);
      if (t) out.push(t);
      continue;
    }
    const t = toTurn(m, startedAtMs);
    if (t) out.push(t);
  }
  return out;
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
      const mentioned: string[] | undefined = Array.isArray(data?.prompt?.agents)
        ? data.prompt.agents.map((a: any) => a?.name ?? a).filter((x: any) => typeof x === "string")
        : undefined;
      const attached: string[] | undefined = Array.isArray(data?.prompt?.files)
        ? data.prompt.files
        : undefined;
      const pending = turns.findIndex((t) => t.id === "pending-user");
      if (pending !== -1 && data?.messageID) {
        const next = turns.slice();
        next[pending] = {
          ...next[pending],
          id: data.messageID,
          agents: mentioned ?? next[pending].agents,
          files: attached ?? next[pending].files,
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
              agents: mentioned,
              files: attached,
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

    // --- V1 durable events (what v1.18.16 actually emits) -----------------
    //
    // `info` is the full Message, `part` is the full Part — both are
    // full-replace, matching the official opencode web UI reducer.

    case "message.updated": {
      const info = data?.info;
      if (!info?.id) return { turns };
      const finished = info.role === "assistant" && (info.time?.completed || info.error);

      // Reconcile the optimistic user bubble with the real message id.
      const pendingIdx = turns.findIndex((t) => t.id === "pending-user");
      if (info.role === "user" && pendingIdx !== -1) {
        const next = turns.slice();
        const pending = next[pendingIdx];
        // Blocks are left empty: the authoritative text arrives via
        // `message.part.updated` right after this event.
        next[pendingIdx] = {
          ...turnFromInfo(info, []),
          agents: pending.agents,
          files: pending.files,
        };
        return { turns: next, ...(finished ? { idle: true } : {}) };
      }

      const idx = turns.findIndex((t) => t.id === info.id);
      if (idx === -1) {
        return { turns: [...turns, turnFromInfo(info, [])], ...(finished ? { idle: true } : {}) };
      }
      // Replace info but keep already-streamed blocks (parts carry content).
      const prev = turns[idx];
      const next = turns.slice();
      next[idx] = { ...turnFromInfo(info, []), blocks: prev.blocks, agents: prev.agents, files: prev.files };
      return { turns: next, ...(finished ? { idle: true } : {}) };
    }

    case "message.part.delta": {
      const messageID = data?.messageID;
      const partID = data?.partID;
      const delta = data?.delta;
      if (!messageID || !partID || typeof delta !== "string") return { turns };
      const kind = data.field === "reasoning" ? "reasoning" : "text";
      return {
        turns: upsertTurn(turns, messageID, (t) =>
          upsertBlock(t, partID, (b) => ({
            kind,
            id: partID,
            text: ((b as any)?.text ?? "") + delta,
          }))
        ),
      };
    }

    case "message.part.updated": {
      const part = data?.part;
      if (!part?.messageID || !part?.id || SKIP_PART_TYPES.has(part.type)) return { turns };
      const block = partToBlock(part);
      if (!block) return { turns };
      const mid = part.messageID;
      return {
        turns: upsertTurn(turns, mid, (t) => {
          const idx = t.blocks.findIndex((b) => b.id === part.id);
          const blocks = t.blocks.slice();
          if (idx === -1) blocks.push(block);
          else blocks[idx] = block; // full-replace this block
          return { ...t, blocks };
        }),
      };
    }

    case "message.removed": {
      const messageID = data?.messageID;
      if (!messageID) return { turns };
      return { turns: turns.filter((t) => t.id !== messageID) };
    }

    case "message.part.removed": {
      const messageID = data?.messageID;
      const partID = data?.partID;
      if (!messageID || !partID) return { turns };
      const idx = turns.findIndex((t) => t.id === messageID);
      if (idx === -1) return { turns };
      const next = turns.slice();
      const t = next[idx];
      next[idx] = { ...t, blocks: t.blocks.filter((b) => b.id !== partID) };
      return { turns: next };
    }

    default:
      return { turns };
  }
}
