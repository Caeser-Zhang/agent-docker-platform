import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  api,
  type AgentRuntime,
  type AgentStatus,
  type ModelRef,
  type OcAgent,
  type OcCommand,
  type OcPermissionReply,
  type OcPermissionRequest,
  type OcQuestionRequest,
  type OcSession,
  type ProvidersResponse,
} from "../api";
import { parseTodos, reduceEvent, toTurns, type Block, type FileDiff, type TodoItem, type Turn } from "../oc/messages";
import { styles } from "./chatStyles";
import { ConfigPanel } from "./ConfigPanel";

/** "provider/model" <-> ModelRef, the format opencode uses in config.model. */
function parseModel(value: string | null | undefined): ModelRef | undefined {
  if (!value) return undefined;
  const i = value.indexOf("/");
  if (i <= 0) return undefined;
  return { providerID: value.slice(0, i), id: value.slice(i + 1) };
}
const modelKey = (m?: ModelRef) => (m ? `${m.providerID}/${m.id}` : "");

/** All "@path" references in the text (start of line or after whitespace). */
const collectAtTokens = (text: string): string[] => {
  const out = new Set<string>();
  for (const m of text.matchAll(/(?:^|\s)@([^\s@]+)/g)) out.add(m[1]);
  return [...out];
};

/**
 * Best-effort mime for a workspace file referenced via @mention. opencode only
 * handles image/* attachments natively; everything else must be sent as
 * text/plain so it goes through the Read tool instead of a media part that
 * OpenAI-Chat providers reject. (SVG stays text/plain — it is textual.)
 */
const IMAGE_MIME_BY_EXT: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
};
const mimeForPath = (p: string): string => {
  const ext = p.includes(".") ? p.split(".").pop()!.toLowerCase() : "";
  return IMAGE_MIME_BY_EXT[ext] ?? "text/plain";
};

/**
 * Slash menu entry kinds: real opencode commands, the client-side "/agents"
 * pseudo command, and the agent entries it expands into.
 */
type SlashOption =
  | { kind: "command"; name: string; description?: string; source?: string }
  | { kind: "agentsCmd"; name: "agents"; description: string }
  | { kind: "agent"; name: string; description?: string };

/**
 * @-menu entries: workspace files plus — when the orchestrator is selected —
 * the subagents it manages (sent as prompt.agents mentions).
 */
type AtOption =
  | { kind: "file"; path: string }
  | { kind: "agent"; name: string; description?: string };

export function Chat({
  username,
  role,
  onOpenAdmin,
  onLogout,
}: {
  username: string;
  role?: string;
  onOpenAdmin?: () => void;
  onLogout: () => void;
}) {
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null);
  const [runtime, setRuntime] = useState<AgentRuntime | null>(null);
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [model, setModel] = useState<ModelRef | undefined>(undefined);

  const [sessions, setSessions] = useState<OcSession[]>([]);
  const [currentSession, setCurrentSession] = useState<OcSession | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [promptAgent, setPromptAgent] = useState<string | undefined>(undefined);
  const [promptModel, setPromptModel] = useState<ModelRef | undefined>(undefined);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState("");
  const [logs, setLogs] = useState("");
  const [busy, setBusy] = useState<string>("");
  // True while a background container start is in flight (page warmup or the
  // start button). POST /agent/start returns immediately; this drives the
  // status polling + phase UI until the phase settles on running/failed.
  const [starting, setStarting] = useState(false);
  // P1-4: SSE connection health — the browser's EventSource reconnects on its
  // own; while it is down the banner warns that replies may lag.
  const [sseDown, setSseDown] = useState(false);
  const [showConfig, setShowConfig] = useState(false);

  // P1-1: revert/unrevert of the last round's file changes, the agent's live
  // task list (todo.updated SSE events), and a second busy label for actions
  // that must not run concurrently (fork / summarize).
  const [revertedId, setRevertedId] = useState<string | null>(null);
  const [revertBusy, setRevertBusy] = useState(false);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [busyLabel, setBusyLabel] = useState("");

  // opencode agent presets + pending approval requests.
  const [agents, setAgents] = useState<OcAgent[]>([]);
  const [agentId, setAgentId] = useState("build");
  const [permissions, setPermissions] = useState<OcPermissionRequest[]>([]);
  const [questions, setQuestions] = useState<OcQuestionRequest[]>([]);

  // Chat attach: skill picker + file uploads.
  const [allSkills, setAllSkills] = useState<{ name: string; description: string; dir: string; scope: string }[]>([]);
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [attachments, setAttachments] = useState<{ filename: string; path: string; mime: string; isImage: boolean; size: number; dataUrl?: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // @-mention autocomplete: activated while typing "@query" in the textarea.
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const [atQuery, setAtQuery] = useState<string | null>(null);
  const [atOptions, setAtOptions] = useState<AtOption[]>([]);
  const [atIndex, setAtIndex] = useState(0);
  const atTimerRef = useRef<number | null>(null);

  // Slash command menu: "/" at the very start of the input opens the command
  // picker; typing "/agents" fully swaps it to an agent picker (client-side
  // pseudo command — the opencode server has no /agents command).
  const [commands, setCommands] = useState<OcCommand[]>([]);
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const commandsLoadedRef = useRef(false);

  // Workspace file browser panel.
  const [showFiles, setShowFiles] = useState(false);
  const [wsFiles, setWsFiles] = useState<{ path: string; type: "file" | "dir"; size: number }[]>([]);
  const [preview, setPreview] = useState<{ path: string; type: string; mime: string; content?: string; base64?: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // Workspace panel uploads: files land in the container workspace (tmp/) and
  // the tree is refreshed afterwards. `wsNotice` is a short-lived success hint.
  const [wsUploading, setWsUploading] = useState(false);
  const [wsNotice, setWsNotice] = useState("");
  const wsNoticeTimerRef = useRef<number | null>(null);

  const esRef = useRef<EventSource | null>(null);
  // P1-5: one-time SSE tickets mean the browser can't auto-reconnect; we
  // drive reconnection with this timer (fresh ticket each attempt).
  const sseReconnectRef = useRef<number | null>(null);
  // Retry indirection: SSE error handlers reach the current connectSSE
  // through this ref, so the callbacks don't form a self-referential
  // useCallback cycle.
  const connectSSERef = useRef<() => void>(() => {});
  const lastEventIdRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  // The SSE callback is registered once; it reads the live session id from here.
  const sessionIdRef = useRef<string | null>(null);
  // Container's current boot time (epoch ms), mirrored from agentStatus into a
  // ref so message loaders (refreshSessionMessages / selectSession) can do
  // dead-turn reconciliation without depending on the agentStatus state.
  const agentStartedAtRef = useRef<number | undefined>(undefined);
  // Timestamp of the last received `message.*` SSE event, and a debounce timer
  // for message refetches. The platform's SSE pump can drop events while the
  // agent is streaming (opencode severs the upstream connection every ~1s),
  // so the UI re-pulls GET /session/{id}/message as a fallback: when a
  // message completes, and on a slow poll while isGenerating.
  const lastMsgEvtRef = useRef(0);
  const refreshDebounceRef = useRef<number | null>(null);
  // P1-4: one-shot session restore across page refreshes (localStorage).
  const restoredRef = useRef(false);
  // P1-4: browser-side fallback for "how long has the start been running"
  // (the backend's phase_since is preferred whenever it is present).
  const startupAtRef = useRef<number | null>(null);

  // Readiness = container running AND the controller marked it "running"
  // (which only happens after its own health probe + session warmup).
  // Deliberately NOT gated on `healthy`: the image's HEALTHCHECK has a 45s
  // start-period, so `healthy` stays false for a while even when opencode
  // is already serving — gating on it would leave the UI stuck on
  // "starting" right after a successful boot.
  const isAgentRunning = Boolean(agentStatus?.running && agentStatus?.status === "running");

  // Live phase label while a background start is in flight.
  const startupPhaseLabel =
    agentStatus?.status === "creating"
      ? "正在创建容器…"
      : agentStatus?.status === "starting"
      ? "正在启动 opencode 服务…"
      : agentStatus?.status === "warming"
      ? "正在预热模型会话…"
      : "启动中…";

  // Mirror the container's boot time into the ref used by message loaders.
  useEffect(() => {
    agentStartedAtRef.current = agentStatus?.started_at ? agentStatus.started_at * 1000 : undefined;
  }, [agentStatus?.started_at]);

  // ------------------------------------------------------------------
  //  Control plane
  // ------------------------------------------------------------------
  const refreshStatus = useCallback(async () => {
    try {
      setAgentStatus(await api.getAgentStatus());
    } catch (e) {
      console.error("status check failed", e);
    }
  }, []);

  /** Pull pending permission/question requests (GET /api/{permission,question}/request). */
  const refreshPending = useCallback(async () => {
    const [ps, qs] = await Promise.allSettled([
      api.listPermissionRequests(),
      api.listQuestionRequests(),
    ]);
    if (ps.status === "fulfilled") setPermissions(ps.value);
    if (qs.status === "fulfilled") setQuestions(qs.value);
  }, []);

  // Skill 列表：优先 opencode 原生 GET /skill（v1 面，opencode 自己注册的
  // 权威来源，含 description 与容器内 location），失败时回退平台侧
  // /workspace/skills/all。allSkills 状态 shape 保持 {name, description, dir, scope}。
  const loadSkills = useCallback(async () => {
    try {
      const native = await api.listNativeSkills();
      setAllSkills(
        native.map((s) => ({
          name: s.name,
          description: s.description ?? "",
          dir: s.location,
          // 全局 skill 位于容器内 XDG 配置目录；其余（workspace 下）为项目级。
          scope: s.location.includes("/data/config/opencode") ? "global" : "project",
        }))
      );
    } catch {
      try {
        const r = await api.listAllSkills();
        setAllSkills(r.skills ?? []);
      } catch {
        /* 保留现有列表 */
      }
    }
  }, []);

  const loadContainerState = useCallback(async () => {
    // Everything below is served by opencode inside the container.
    loadSkills();
    const [rt, prov, sess, ags] = await Promise.allSettled([
      api.getAgentRuntime(),
      api.getProviders(),
      api.listSessions(),
      api.listAgents(),
    ]);
    if (rt.status === "fulfilled") setRuntime(rt.value);
    if (prov.status === "fulfilled") {
      setProviders(prov.value);
      setModel((cur) => cur ?? parseModel(prov.value.default));
      if (prov.value.error) setError(prov.value.error);
    }
    if (sess.status === "fulfilled") {
      setSessions(
        sess.value.slice().sort((a, b) => (b.time?.updated ?? 0) - (a.time?.updated ?? 0))
      );
    }
    if (ags.status === "fulfilled") {
      const list = ags.value;
      setAgents(list);
      setAgentId((cur) =>
        list.some((a) => a.id === cur)
          ? cur
          : list.find((a) => !a.hidden && a.mode !== "subagent")?.id ?? cur
      );
    }
    refreshPending();
  }, [refreshPending, loadSkills]);

  // ------------------------------------------------------------------
  //  SSE — opencode's own event stream, relayed by the platform
  // ------------------------------------------------------------------
  /** Replace the open session's turns with the server truth. */
  const refreshSessionMessages = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    try {
      const msgs = await api.getMessages(sid);
      if (sessionIdRef.current !== sid || msgs.length === 0) return;
      // Dead-turn reconciliation: close tool calls stranded by a container
      // kill so isGenerating can't wedge on a message that will never finish.
      const next = toTurns(msgs, agentStartedAtRef.current);
      setTurns(next);
      // Server truth beats the streaming flag: if no assistant turn is still
      // streaming, the round is over (covers SSE events lost in transit).
      if (next.some((t) => t.role === "assistant") && !next.some((t) => t.streaming)) {
        setIsGenerating(false);
      }
    } catch {
      /* transient tunnel error — the next poll retries */
    }
  }, []);

  /** Debounced refresh (SSE reports a completed message). */
  const scheduleRefresh = useCallback(() => {
    if (refreshDebounceRef.current !== null) window.clearTimeout(refreshDebounceRef.current);
    refreshDebounceRef.current = window.setTimeout(() => {
      refreshDebounceRef.current = null;
      refreshSessionMessages();
    }, 400);
  }, [refreshSessionMessages]);

  const connectSSE = useCallback(() => {
    esRef.current?.close();
    if (sseReconnectRef.current !== null) {
      window.clearTimeout(sseReconnectRef.current);
      sseReconnectRef.current = null;
    }
    // P1-5: minting a one-time ticket is async; whoever resolves last wins
    // (each resolution closes whatever ES is currently attached).
    api
      .createEventSource(lastEventIdRef.current)
      .then((es) => {
        esRef.current?.close();
        esRef.current = es;
        attachSSEHandlers(es);
      })
      .catch(() => {
        // Ticket mint failed (backend down / session expired) — back off
        // and redial; a 401 already forces a relogin via apiCall.
        setSseDown(true);
        if (sseReconnectRef.current !== null) return;
        sseReconnectRef.current = window.setTimeout(() => {
          sseReconnectRef.current = null;
          connectSSERef.current();
        }, 2000);
      });
  }, [refreshPending, scheduleRefresh]);

  /** Register the event reducers on a freshly attached EventSource. */
  const attachSSEHandlers = useCallback((es: EventSource) => {
    es.onmessage = (event) => {
      let evt: any;
      try {
        evt = JSON.parse(event.data);
      } catch {
        return;
      }
      if (typeof evt.id === "number") lastEventIdRef.current = evt.id;

      const type: string = evt.type || "";
      // `/event` is opencode's streaming SSE surface (the one used by its
      // official web app). It carries payloads in `properties`; the v2
      // `/api/event` surface uses `data`. Accept both at the boundary so the
      // reducer has one normalized shape.
      const data = evt.properties || evt.data || {};

      // Session list events apply regardless of which session is open.
      if (type === "session.created" || type === "session.updated") {
        const info: OcSession | undefined = data.info;
        if (info) {
          setSessions((prev) => {
            const rest = prev.filter((s) => s.id !== info.id);
            return [info, ...rest].sort(
              (a, b) => (b.time?.updated ?? 0) - (a.time?.updated ?? 0)
            );
          });
          setCurrentSession((cur) => (cur?.id === info.id ? info : cur));
        }
        return;
      }
      if (type === "session.deleted") {
        const sid = data.info?.id || data.sessionID || data.id;
        if (typeof sid === "string") {
          setSessions((prev) => prev.filter((s) => s.id !== sid));
          setCurrentSession((cur) => {
            if (cur?.id !== sid) return cur;
            sessionIdRef.current = null;
            setTurns([]);
            setIsGenerating(false);
            return null;
          });
        }
        return;
      }
      // Approval requests arrive as permission.v2.* / question.v2.* (and
      // legacy permission.* / question.* aliases).
      if (
        type.startsWith("permission.") ||
        type.startsWith("question.")
      ) {
        refreshPending();
        return;
      }
      if (type === "agent.disconnected") {
        setError("Agent 未运行，事件流已断开");
        return;
      }

      // Everything else is scoped to one session.
      const sid = data.sessionID;
      if (sid && sid !== sessionIdRef.current) return;

      // P1-1: the agent's task list updates — session-scoped, but not a
      // message turn, so it bypasses the turn reducer entirely.
      if (type === "todo.updated") {
        setTodos(parseTodos(data.todos));
        return;
      }

      // Track live message traffic so the generation poll can stand down
      // while SSE is actually delivering (it only kicks in as a fallback
      // when the pump has dropped events).
      if (type.startsWith("message.")) lastMsgEvtRef.current = Date.now();
      if (
        type === "message.updated" &&
        data.info?.role === "assistant" &&
        (data.info.time?.completed || data.info.error)
      ) {
        // Final (or failed) assistant message — pull the authoritative
        // record: its text parts may have been lost to SSE drops.
        scheduleRefresh();
      }

      setTurns((prev) => {
        const r = reduceEvent(prev, type, data);
        if (r.idle) setIsGenerating(false);
        if (r.error) setError(r.error);
        if (r.model) setModel(r.model);
        return r.turns;
      });
    };

    es.onopen = () => setSseDown(false);
    es.onerror = () => {
      // P1-5: the ticket in the URL is one-time, so the browser's native
      // auto-reconnect can't replay it. Redial manually (fresh ticket via
      // connectSSERef → connectSSE) after a short backoff; lastEventId
      // replays the gap.
      es.close();
      esRef.current = null;
      setSseDown(true);
      console.warn("SSE dropped, redialing with a fresh ticket");
      if (sseReconnectRef.current !== null) return;
      sseReconnectRef.current = window.setTimeout(() => {
        sseReconnectRef.current = null;
        connectSSERef.current();
      }, 2000);
    };
  }, [refreshPending, scheduleRefresh]);

  // Keep the retry indirection pointed at the current connectSSE. Declared
  // before the mount effect so the first connectSSE() call can't fire early.
  useEffect(() => {
    connectSSERef.current = connectSSE;
  }, [connectSSE]);

  // On mount: fetch the status once, then auto-warm the container if it
  // isn't up — the user should never have to click "start" and wait. The
  // start endpoint is async (returns immediately); progress arrives via
  // the polling effect below.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const st = await api.getAgentStatus();
        if (cancelled) return;
        setAgentStatus(st);
        if (st.status === "running") return;
        if (["creating", "starting", "warming"].includes(st.status)) {
          // Another tab / request already started it — just poll.
          setStarting(true);
          return;
        }
        // absent / stopped / failed → kick off the background start now.
        setStarting(true);
        const r = await api.startAgent();
        if (cancelled) return;
        setAgentStatus(r);
        if (r.status === "running") {
          setStarting(false); // fast path: container was already up
        } else if (r.status === "failed") {
          setStarting(false);
          setError(r.error || r.message || "自动启动 Agent 失败");
        }
      } catch (e) {
        console.error("status check failed", e);
      }
    })();
    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
      if (sseReconnectRef.current !== null) {
        window.clearTimeout(sseReconnectRef.current);
        sseReconnectRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // While a background start is in flight, poll GET /agent/status (~1s)
  // until the phase settles on running / failed / stopped.
  useEffect(() => {
    if (!starting) return;
    let stop = false;
    (async () => {
      while (!stop) {
        await new Promise((r) => setTimeout(r, 1000));
        if (stop) break;
        try {
          const st = await api.getAgentStatus();
          if (stop) break;
          setAgentStatus(st);
          if (["running", "failed", "stopped"].includes(st.status)) {
            setStarting(false);
            if (st.status === "failed") {
              setError("Agent 启动失败：" + (st.error || st.message || "健康检查超时"));
            }
            break;
          }
        } catch {
          // transient network error — keep polling
        }
      }
    })();
    return () => {
      stop = true;
    };
  }, [starting]);

  // P1-4: browser-side start timestamp — used only when the backend hasn't
  // reported phase_since yet (e.g. the request hasn't landed).
  useEffect(() => {
    if (starting) {
      if (startupAtRef.current === null) startupAtRef.current = Date.now();
    } else {
      startupAtRef.current = null;
    }
  }, [starting]);

  // P1-4: total wait so far while a start is in flight. phase_since is set
  // server-side when the flow began, so a browser that attached mid-start
  // still shows an accurate count; the local timer is the fallback.
  const startupElapsedSec = (() => {
    if (!starting) return null;
    const ps = agentStatus?.phase_since;
    if (typeof ps === "number" && ps > 0) return Math.max(0, Math.round(Date.now() / 1e3 - ps));
    const started = startupAtRef.current;
    return started ? Math.max(0, Math.round((Date.now() - started) / 1e3)) : null;
  })();
  const startupPhaseIdx =
    agentStatus?.status === "creating"
      ? 1
      : agentStatus?.status === "starting"
      ? 2
      : agentStatus?.status === "warming"
      ? 3
      : 0;

  // If the container was already up when the page loaded, attach to it.
  const attachedRef = useRef(false);
  useEffect(() => {
    if (!isAgentRunning || attachedRef.current) return;
    attachedRef.current = true;
    connectSSE();
    loadContainerState();
  }, [isAgentRunning, connectSSE, loadContainerState]);

  const handleStartAgent = async () => {
    setError("");
    setStarting(true);
    try {
      const result = await api.startAgent();
      setAgentStatus(result);
      if (result.status === "running") {
        // Fast path: container was already up.
        setStarting(false);
        attachedRef.current = true;
        connectSSE();
        await loadContainerState();
      } else if (result.status === "failed") {
        setStarting(false);
        setError(result.message || "启动 Agent 失败");
      }
      // otherwise: background start in flight — the polling effect takes
      // over and flips `starting` off when the phase settles.
    } catch (e: any) {
      setStarting(false);
      setError(e.message);
    }
  };

  const handleStopAgent = async () => {
    setBusy("停止容器中…");
    try {
      await api.stopAgent();
      esRef.current?.close();
      esRef.current = null;
      if (sseReconnectRef.current !== null) {
        window.clearTimeout(sseReconnectRef.current);
        sseReconnectRef.current = null;
      }
      attachedRef.current = false;
      lastEventIdRef.current = 0;
      // P1-4: clear connection-health + session-restore markers so a later
      // restart reattaches cleanly and can restore the session once more.
      setSseDown(false);
      restoredRef.current = false;
      setSessions([]);
      setCurrentSession(null);
      sessionIdRef.current = null;
      setTurns([]);
      setProviders(null);
      await refreshStatus();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  };

  const handleReloadConfig = async () => {
    setBusy("重新注入 opencode.json…");
    setError("");
    try {
      await api.reloadConfig();
      // The container restarted: its SSE connection and provider list are new.
      lastEventIdRef.current = 0;
      connectSSE();
      await loadContainerState();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  };

  // ------------------------------------------------------------------
  //  Sessions
  // ------------------------------------------------------------------
  const openSession = useCallback(async (session: OcSession) => {
    setCurrentSession(session);
    sessionIdRef.current = session.id;
    // P1-4: remember the open session so a page refresh can restore it.
    window.localStorage.setItem("oc.lastSession", session.id);
    setTurns([]);
    setIsGenerating(false);
    // P1-1: revert state + task list belong to the session, not the page.
    setRevertedId(null);
    setTodos([]);
    if (session.model) setModel(session.model);
    if (session.agent) setAgentId(session.agent);
    refreshPending();
    try {
      setTurns(toTurns(await api.getMessages(session.id), agentStartedAtRef.current));
    } catch (e: any) {
      setError(e.message);
    }
  }, [refreshPending]);

  // P1-4: after a page refresh, reopen the session the user had open (once
  // the list has loaded). Manual navigation writes sessionIdRef before this
  // runs, so an explicit choice is never overridden.
  useEffect(() => {
    if (restoredRef.current || !isAgentRunning || sessions.length === 0) return;
    restoredRef.current = true;
    if (sessionIdRef.current) return;
    const saved = window.localStorage.getItem("oc.lastSession");
    if (!saved) return;
    const found = sessions.find((s) => s.id === saved);
    if (found) openSession(found);
  }, [sessions, isAgentRunning, openSession]);

  // ------------------------------------------------------------------
  //  P1-1: revert / unrevert / fork / summarize — all opencode-native
  //  session routes, forwarded verbatim through the tunnel.
  // ------------------------------------------------------------------
  const handleRevert = useCallback(
    async (messageId: string) => {
      const sid = sessionIdRef.current;
      if (!sid || revertBusy) return;
      if (!window.confirm("回退该回合产生的所有文件改动？")) return;
      setRevertBusy(true);
      try {
        await api.revertSession(sid, messageId);
        setRevertedId(messageId);
        refreshSessionMessages();
      } catch (e: any) {
        setError(`回退失败：${e?.message ?? e}`);
      } finally {
        setRevertBusy(false);
      }
    },
    [revertBusy, refreshSessionMessages]
  );

  const handleUnrevert = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (!sid || revertBusy) return;
    setRevertBusy(true);
    try {
      await api.unrevertSession(sid);
      setRevertedId(null);
      refreshSessionMessages();
    } catch (e: any) {
      setError(`恢复失败：${e?.message ?? e}`);
    } finally {
      setRevertBusy(false);
    }
  }, [revertBusy, refreshSessionMessages]);

  const handleFork = useCallback(
    async (messageId?: string) => {
      const sid = sessionIdRef.current;
      if (!sid || busyLabel) return;
      setBusyLabel("分叉会话中…");
      try {
        const forked = await api.forkSession(sid, messageId);
        setSessions((prev) => [forked, ...prev.filter((s) => s.id !== forked.id)]);
        await openSession(forked);
      } catch (e: any) {
        setError(`分叉失败：${e?.message ?? e}`);
      } finally {
        setBusyLabel("");
      }
    },
    [busyLabel, openSession]
  );

  // 重新生成（方案一：直接替换）。对目标回合前的最后一条 user 消息调用
  // revert —— opencode 只打软标记，下次 prompt 时才会硬删旧分支 —— 然后
  // 从该 user turn 的 blocks 重建原始输入（文本 + file parts）并重发，等效
  // "替换原回答"。SSE 的 message.removed / session.next.prompted 事件以及
  // 既有的防抖刷新会驱动 UI 收敛到新回复。
  const handleRegenerate = useCallback(
    async (assistantId: string) => {
      const sid = sessionIdRef.current;
      if (!sid || isGenerating) return;
      const aIdx = turns.findIndex((t) => t.id === assistantId);
      if (aIdx < 0) return;
      let userTurn: Turn | undefined;
      for (let i = aIdx - 1; i >= 0; i--) {
        if (turns[i].role === "user") {
          userTurn = turns[i];
          break;
        }
      }
      if (!userTurn) return;
      const text = userTurn.blocks
        .filter((b) => b.kind === "text")
        .map((b) => b.text)
        .join("\n");
      const files = userTurn.blocks
        .filter((b) => b.kind === "file")
        .map((b) => ({
          mime: b.mime,
          url: b.url,
          ...(b.filename ? { filename: b.filename } : {}),
        }));
      setIsGenerating(true);
      setRevertedId(null);
      try {
        await api.revertSession(sid, userTurn.id);
        await api.sendPrompt(sid, text, {
          files: files.length ? files : undefined,
          agents: userTurn.agents?.length ? userTurn.agents : undefined,
          agent: userTurn.agent,
          model: userTurn.model,
        });
      } catch (e: any) {
        setError(`重新生成失败：${e?.message ?? e}`);
        setIsGenerating(false);
        refreshSessionMessages();
      }
    },
    [isGenerating, turns, refreshSessionMessages]
  );

  const handleSummarize = useCallback(async () => {
    const sid = sessionIdRef.current;
    const m = model ?? currentSession?.model;
    if (!sid || !m || busyLabel) return;
    setBusyLabel("生成摘要中…");
    try {
      await api.summarizeSession(sid, m);
      refreshSessionMessages();
    } catch (e: any) {
      setError(`生成摘要失败：${e?.message ?? e}`);
    } finally {
      setBusyLabel("");
    }
  }, [busyLabel, model, currentSession, refreshSessionMessages]);

  // Fallback poll while a prompt is in flight. opencode severs its event
  // stream ~every second during agent activity, so the platform pump can
  // silently drop the assistant's parts; when that happens no SSE event
  // ever clears isGenerating and the reply never renders. While SSE is
  // delivering message events this poll stands down; otherwise it re-pulls
  // the whole message list every 2.5s until the round completes.
  useEffect(() => {
    if (!isGenerating) return;
    const timer = window.setInterval(() => {
      if (Date.now() - lastMsgEvtRef.current < 4000) return; // SSE is live
      refreshSessionMessages();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [isGenerating, refreshSessionMessages]);

  const handleNewSession = async () => {
    setError("");
    try {
      const s = await api.createSession(model, agentId);
      setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
      await openSession(s);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleRenameSession = async (s: OcSession, e: React.MouseEvent) => {
    e.stopPropagation();
    const title = window.prompt("新的会话标题", s.title || "")?.trim();
    if (!title) return;
    try {
      // opencode's v2 surface has no update route — legacy PATCH returns the
      // bare legacy Session; session.updated on SSE also refreshes the list.
      await api.renameSession(s.id, title);
      setSessions((prev) => prev.map((x) => (x.id === s.id ? { ...x, title } : x)));
      setCurrentSession((cur) => (cur?.id === s.id ? { ...cur, title } : cur));
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDeleteSession = async (s: OcSession, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm(`删除会话「${s.title || s.id.slice(0, 12)}」？此操作不可恢复。`)) return;
    try {
      await api.deleteSession(s.id);
      setSessions((prev) => prev.filter((x) => x.id !== s.id));
      if (sessionIdRef.current === s.id) {
        setCurrentSession(null);
        sessionIdRef.current = null;
        setTurns([]);
        setIsGenerating(false);
      }
    } catch (err: any) {
      setError(err.message);
    }
  };

  // ------------------------------------------------------------------
  //  Prompting
  // ------------------------------------------------------------------
  const handleSend = async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || !currentSession || isGenerating) return;
    setError("");
    setAtQuery(null);
    setAtOptions([]);
    setSlashQuery(null);

    // Slash command dispatch: "/name args..." goes to the dedicated command
    // endpoint instead of the prompt route. Unknown "/" text falls through
    // as a normal prompt (same as opencode's own TUI behaviour).
    const slashMatch = text.match(/^\/([a-zA-Z0-9_:-]+)(?:\s+([\s\S]*))?$/);
    if (slashMatch) {
      const name = slashMatch[1];
      const args = (slashMatch[2] ?? "").trim();
      if (name.toLowerCase() === "agents") {
        // Client-side pseudo command: switching happens in the menu itself.
        setInput("");
        return;
      }
      let cmds = commands;
      if (cmds.length === 0) {
        try {
          cmds = await api.listCommands();
          setCommands(cmds);
        } catch {
          cmds = [];
        }
      }
      if (cmds.some((c) => c.name === name)) {
        setInput("");
        setAttachments([]);
        setIsGenerating(true);
        // Optimistic bubble; reconciled by session.next.prompted, and
        // isGenerating is cleared by the session.idle SSE event.
        setTurns((prev) => [
          ...prev,
          {
            id: "pending-user",
            role: "user",
            type: "user",
            blocks: [{ kind: "text", id: "pending-user:text", text }],
          },
        ]);
        try {
          await api.runCommand(currentSession.id, name, args);
        } catch (e: any) {
          setError(e.message);
          setIsGenerating(false);
        }
        return;
      }
    }

    // Attachments + "@path" file references → parts of POST /session/{id}/prompt_async.
    // FilePartInput requires an explicit `mime`, which decides how opencode
    // routes the attachment:
    //   - image/* with a data: URL → inline base64 media part, the model sees
    //     the pixels directly (no upload into the container involved);
    //   - "text/plain" with a file:// URL → inlined through the Read tool;
    //   - OpenAI-Chat providers reject every other media type (e.g.
    //     text/markdown), so text-ish files are always sent as text/plain.
    // "@agent" tokens matching a registered agent go into agent parts instead.
    // Unknown tokens stay as plain text — same as opencode's own @mention
    // behaviour, where the mention is turned into a file/agent attachment
    // before the prompt is sent.
    const files: { mime: string; url: string; filename?: string }[] = [];
    const agentNames: string[] = [];
    const seen = new Set<string>();
    for (const a of attachments) {
      const key = a.dataUrl ?? a.path;
      if (seen.has(key)) continue;
      seen.add(key);
      files.push({
        mime: a.isImage ? a.mime : "text/plain",
        url: a.dataUrl ?? `file:///workspace/${a.path}`,
        filename: a.filename,
      });
    }

    const atTokens = collectAtTokens(text);
    if (atTokens.length > 0) {
      // Only subagents (the ones the @-menu offers) become agent mentions.
      const agentSet = new Set(mentionableAgents.map((a) => a.id));
      let knownPaths = new Set(wsFiles.filter((f) => f.type === "file").map((f) => f.path));
      if (knownPaths.size === 0) {
        // The file browser was never opened; fetch the tree once to resolve.
        try {
          const r = await api.listWorkspaceFiles();
          knownPaths = new Set((r.files ?? []).filter((f) => f.type === "file").map((f) => f.path));
          setWsFiles(r.files ?? []);
        } catch {
          /* leave tokens as plain text */
        }
      }
      for (const tok of atTokens) {
        if (seen.has(tok)) continue;
        if (agentSet.has(tok)) {
          seen.add(tok);
          agentNames.push(tok);
          continue;
        }
        if (!knownPaths.has(tok)) continue;
        seen.add(tok);
        files.push({ mime: mimeForPath(tok), url: `file:///workspace/${tok}` });
      }
    }

    // Skills: opencode has no dedicated prompt field for them, so request them
    // explicitly in the text (the skill tool picks them up by name).
    const finalText = selectedSkills.length > 0
      ? `请使用 skill: ${selectedSkills.join(", ")}\n\n${text}`
      : text;

    setInput("");
    setAttachments([]);
    setIsGenerating(true);
    // Optimistic bubble; reconciled to the real id by session.next.prompted.
    setTurns((prev) => [
      ...prev,
      {
        id: "pending-user",
        role: "user",
        type: "user",
        agents: agentNames.length ? agentNames : undefined,
        files: files.length
          ? files.map((f) => (f.url.startsWith("data:") ? f.filename ?? "image" : f.url))
          : undefined,
        blocks: [
          { kind: "text", id: "pending-user:text", text: selectedSkills.length ? `🧩 ${selectedSkills.join(", ")}\n${text}` : text },
          ...(attachments.map((a, i) => ({
            kind: "text" as const,
            id: `pending-user:file-${i}`,
            text: a.isImage ? `🖼️ ${a.filename}` : `📎 ${a.filename}`,
          }))),
        ],
      },
    ]);
    try {
      await api.sendPrompt(currentSession.id, finalText, {
        files: files.length ? files : undefined,
        agents: agentNames.length ? agentNames : undefined,
        agent: promptAgent,
        model: promptModel,
      });
      setPromptAgent(undefined);
      setPromptModel(undefined);
    } catch (e: any) {
      setError(e.message);
      setIsGenerating(false);
    }
  };

  /** Read a browser File as a base64 data URL (for inline image parts). */
  const readFileAsDataUrl = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(new Error(`读取 ${file.name} 失败`));
      reader.readAsDataURL(file);
    });

  const handleFilesPicked = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      for (const f of Array.from(files)) {
        if (f.type.startsWith("image/")) {
          // Images never touch the container: the browser encodes them as a
          // base64 data URL that goes straight into the prompt's file part,
          // so the model sees the pixels directly.
          const dataUrl = await readFileAsDataUrl(f);
          setAttachments((prev) => [
            ...prev,
            { filename: f.name, path: "", mime: f.type, isImage: true, size: f.size, dataUrl },
          ]);
          continue;
        }
        const r = await api.uploadChatFile(f);
        setAttachments((prev) => [
          ...prev.filter((a) => a.path !== r.path),
          { filename: r.filename, path: r.path, mime: r.mime, isImage: r.isImage, size: r.size },
        ]);
        // Auto-reference the upload so the model (and the user) can see it.
        setInput((prev) => {
          const ref = `@${r.path}`;
          if (prev.includes(ref)) return prev;
          return prev ? (prev.endsWith(" ") ? prev + ref : prev + " " + ref) : ref;
        });
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // ------------------------------------------------------------------
  //  @-mention file autocomplete + slash command menu
  // ------------------------------------------------------------------
  /** Extract the active "@query" before the caret, or null when not in a mention. */
  const detectAtMention = (text: string, caret: number): string | null => {
    const before = text.slice(0, caret);
    const at = before.lastIndexOf("@");
    if (at === -1) return null;
    const query = before.slice(at + 1);
    if (/\s/.test(query)) return null; // a space ends the mention
    return query;
  };

  /** The active "/cmd" token when the input starts with "/" (before caret). */
  const detectSlashCommand = (text: string, caret: number): string | null => {
    const before = text.slice(0, caret);
    if (!before.startsWith("/")) return null; // commands only at message start
    const query = before.slice(1);
    if (/\s/.test(query)) return null; // a space ends command selection
    return query;
  };

  /** Called on every input change: opens/closes the menus, debounced search. */
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const text = e.target.value;
    setInput(text);
    const caret = e.target.selectionStart ?? text.length;
    // The slash menu (message start) and the @-menu are mutually exclusive.
    const sq = detectSlashCommand(text, caret);
    setSlashQuery(sq);
    setSlashIndex(0);
    if (sq !== null) {
      setAtQuery(null);
      setAtOptions([]);
      // Lazy-load the command list on first slash usage; plugins can register
      // more, so a failed fetch retries on the next slash input.
      if (commands.length === 0 && !commandsLoadedRef.current) {
        commandsLoadedRef.current = true;
        api
          .listCommands()
          .then(setCommands)
          .catch(() => {
            setCommands([]);
            commandsLoadedRef.current = false;
          });
      }
      return;
    }
    const q = detectAtMention(text, caret);
    setAtQuery(q);
    if (q === null) {
      setAtOptions([]);
      return;
    }
    if (atTimerRef.current) window.clearTimeout(atTimerRef.current);
    atTimerRef.current = window.setTimeout(async () => {
      // Agent mentions first when the orchestrator is active.
      const agentOpts: AtOption[] = showAgentMentions
        ? mentionableAgents
            .filter((a) => q === "" || a.id.toLowerCase().includes(q.toLowerCase()))
            .map((a) => ({ kind: "agent" as const, name: a.id, description: a.description }))
        : [];
      try {
        // Empty query right after "@": show workspace files as a starter list.
        let fileOpts: AtOption[] = [];
        if (q === "") {
          fileOpts = wsFiles
            .filter((f) => f.type === "file")
            .slice(0, 15)
            .map((f) => ({ kind: "file" as const, path: f.path }));
        } else {
          const found = await api.findFiles(q, 15);
          fileOpts = (Array.isArray(found) ? found : []).map((p) => ({
            kind: "file" as const,
            path: p,
          }));
        }
        setAtOptions([...agentOpts, ...fileOpts]);
      } catch {
        setAtOptions(agentOpts);
      }
      setAtIndex(0);
    }, 200);
  };

  /** Replace the active "@query" with "@name " and keep the caret after it. */
  const insertAtToken = (token: string) => {
    const ta = inputRef.current;
    const caret = ta?.selectionStart ?? input.length;
    const before = input.slice(0, caret);
    const at = before.lastIndexOf("@");
    if (at === -1) return;
    const next = input.slice(0, at) + "@" + token + " " + input.slice(caret);
    setInput(next);
    setAtQuery(null);
    setAtOptions([]);
    // Move caret past the inserted reference.
    requestAnimationFrame(() => {
      const pos = at + token.length + 2;
      ta?.focus();
      ta?.setSelectionRange(pos, pos);
    });
  };

  /** Apply the highlighted @-menu entry: agent mention or file reference. */
  const applyAtSelection = (opt: AtOption) => {
    insertAtToken(opt.kind === "agent" ? opt.name : opt.path);
  };

  /** Keyboard routing while the @-menu is open. Returns true if handled. */
  const handleAtKeyDown = (e: React.KeyboardEvent): boolean => {
    if (atQuery === null || atOptions.length === 0) return false;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setAtIndex((i) => (i + 1) % atOptions.length);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setAtIndex((i) => (i - 1 + atOptions.length) % atOptions.length);
      return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applyAtSelection(atOptions[atIndex]);
      return true;
    }
    if (e.key === "Escape") {
      setAtQuery(null);
      setAtOptions([]);
      return true;
    }
    return false;
  };

  // ------------------------------------------------------------------
  //  Workspace file browser
  // ------------------------------------------------------------------
  const loadWsFiles = useCallback(async () => {
    try {
      const r = await api.listWorkspaceFiles();
      setWsFiles(r.files ?? []);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    if (showFiles && isAgentRunning) {
      setPreview(null);
      loadWsFiles();
    }
  }, [showFiles, isAgentRunning, loadWsFiles]);

  /** Show a short-lived hint under the workspace panel header. */
  const showWsNotice = useCallback((msg: string) => {
    setWsNotice(msg);
    if (wsNoticeTimerRef.current) window.clearTimeout(wsNoticeTimerRef.current);
    wsNoticeTimerRef.current = window.setTimeout(() => setWsNotice(""), 4000);
  }, []);

  /**
   * Upload picked files straight into the container workspace (they land in
   * tmp/, the same endpoint chat attachments use) and refresh the file tree.
   * The files are then addressable via @-mention — no prompt is sent.
   */
  const handleWsFilesPicked = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setWsUploading(true);
    setError("");
    try {
      const uploaded: string[] = [];
      for (const f of Array.from(files)) {
        const r = await api.uploadChatFile(f);
        uploaded.push(r.path);
      }
      await loadWsFiles();
      showWsNotice(
        `已上传 ${uploaded.length} 个文件至 ${uploaded[0].split("/")[0] || "工作区"}，可在对话中 @ 引用`
      );
    } catch (e: any) {
      setError(e.message);
    } finally {
      setWsUploading(false);
    }
  };

  const openPreview = async (path: string) => {
    setPreviewLoading(true);
    setPreview({ path, type: "loading", mime: "" });
    try {
      const r = await api.readWorkspaceFile(path);
      setPreview({ path, ...r });
    } catch (e: any) {
      setPreview({ path, type: "error", mime: "", content: e.message });
    } finally {
      setPreviewLoading(false);
    }
  };

  /** Append "@path" to the input (from the file panel), then refocus it. */
  const insertAtReference = (path: string) => {
    setInput((prev) => {
      const ref = `@${path}`;
      if (prev.includes(ref)) return prev;
      return prev ? (prev.endsWith(" ") ? prev + ref : prev + " " + ref) : ref;
    });
    inputRef.current?.focus();
  };

  const toggleSkill = (name: string) => {
    setSelectedSkills((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]
    );
  };

  const handleInterrupt = async () => {
    if (!currentSession) return;
    try {
      await api.interruptSession(currentSession.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setIsGenerating(false);
    }
  };

  // ------------------------------------------------------------------
  //  Model switching — writes straight into opencode's session state
  // ------------------------------------------------------------------
  const modelOptions = useMemo(() => {
    const out: { key: string; label: string; ref: ModelRef; baseURL?: string | null }[] = [];
    for (const p of providers?.providers ?? []) {
      for (const m of p.models) {
        out.push({
          key: `${p.id}/${m.id}`,
          label: `${p.name} · ${m.name}`,
          ref: { providerID: p.id, id: m.id },
          baseURL: p.baseURL,
        });
      }
    }
    return out;
  }, [providers]);

  const activeBaseURL = useMemo(
    () => modelOptions.find((o) => o.key === modelKey(model))?.baseURL,
    [modelOptions, model]
  );

  const handleModelChange = async (key: string) => {
    const opt = modelOptions.find((o) => o.key === key);
    if (!opt) return;
    setModel(opt.ref);
    if (!currentSession) return; // applies to the next new session
    try {
      await api.setSessionModel(currentSession.id, opt.ref);
    } catch (e: any) {
      setError(e.message);
    }
  };

  // ------------------------------------------------------------------
  //  Agent switching / permission & question approvals
  // ------------------------------------------------------------------
  const handleAgentChange = async (id: string) => {
    setAgentId(id);
    if (!currentSession) return; // applies to the next new session
    try {
      await api.setSessionAgent(currentSession.id, id);
      setCurrentSession((cur) => (cur ? { ...cur, agent: id } : cur));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handlePermissionReply = async (requestId: string, reply: OcPermissionReply) => {
    if (!currentSession) return;
    try {
      await api.replyPermission(currentSession.id, requestId, reply);
      setPermissions((prev) => prev.filter((p) => p.id !== requestId));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleQuestionReply = async (
    sessionId: string,
    requestId: string,
    answers: string[][]
  ) => {
    try {
      // Reply against the request's own session — a question raised inside a
      // subagent session carries that session's id, not the open one's.
      await api.replyQuestion(sessionId, requestId, answers);
      setQuestions((prev) => prev.filter((q) => q.id !== requestId));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleQuestionReject = async (sessionId: string, requestId: string) => {
    try {
      await api.rejectQuestion(sessionId, requestId);
      setQuestions((prev) => prev.filter((q) => q.id !== requestId));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const activePermissions = permissions.filter((p) => p.sessionID === currentSession?.id);
  const activeQuestions = questions.filter((q) => q.sessionID === currentSession?.id);
  // Approval cards are pushed via SSE, but a dropped event (the pump's
  // reconnect gap, a throttled background tab) must not strand the user
  // without a way to answer. Poll lightly while a run is active or while
  // approvals are pending.
  useEffect(() => {
    if (!isGenerating && activeQuestions.length === 0 && activePermissions.length === 0)
      return;
    const t = window.setInterval(refreshPending, 5000);
    return () => window.clearInterval(t);
  }, [isGenerating, activeQuestions.length, activePermissions.length, refreshPending]);
  // Subagents are invoked by the primary agent; only primary agents are
  // selectable as a session's agent.
  const primaryAgents = useMemo(
    () => agents.filter((a) => !a.hidden && a.mode !== "subagent"),
    [agents]
  );
  // Subagents the orchestrator manages — surfaced in the @-menu so the user
  // can @-mention them (sent as prompt.agents).
  const mentionableAgents = useMemo(
    () => agents.filter((a) => !a.hidden && a.mode === "subagent"),
    [agents]
  );
  // Subagents are injected via prompt.agents regardless of which primary
  // agent leads the session, so surface them in the @-menu whenever the
  // runtime exposes any (e.g. oh-my-opencode-slim's orchestrator crew).
  // Gating on a specific agent id (orchestrator) left the menu empty with
  // the default session agent.
  const showAgentMentions = mentionableAgents.length > 0;

  // Slash menu options, filtered locally from the cached command list. Once
  // the query fully reads "agents", the list swaps to the agent picker.
  const slashOptions = useMemo<SlashOption[]>(() => {
    if (slashQuery === null) return [];
    const q = slashQuery.toLowerCase();
    if (q === "agents") {
      return primaryAgents.map((a) => ({
        kind: "agent" as const,
        name: a.id,
        description: a.description,
      }));
    }
    const out: SlashOption[] = [];
    if (q === "" || "agents".startsWith(q)) {
      out.push({ kind: "agentsCmd", name: "agents", description: "查看并切换当前会话的 Agent" });
    }
    for (const c of commands) {
      // opencode registers skills on GET /command with source "skill"; they
      // are knowledge packs, not user-invocable commands, so keep the menu
      // to real commands (and MCP-provided ones).
      if (c.source === "skill") continue;
      if (c.name.toLowerCase().includes(q)) {
        out.push({ kind: "command", name: c.name, description: c.description, source: c.source });
      }
    }
    return out;
  }, [slashQuery, commands, primaryAgents]);

  /** Apply the highlighted menu entry: pick agent / expand /agents / insert "/name ". */
  const applySlashSelection = (index?: number) => {
    if (slashQuery === null) return;
    const opt = slashOptions[index ?? slashIndex];
    if (!opt) return;
    if (opt.kind === "agent") {
      // Agent picker: switch the session and clear the input.
      setInput("");
      setSlashQuery(null);
      handleAgentChange(opt.name);
      return;
    }
    if (opt.kind === "agentsCmd") {
      // Rewrite the input to "/agents" so the agent picker takes over.
      setInput("/agents");
      setSlashQuery("agents");
      setSlashIndex(0);
      requestAnimationFrame(() => {
        const ta = inputRef.current;
        ta?.focus();
        ta?.setSelectionRange(7, 7);
      });
      return;
    }
    // Insert "/name " and let the user append arguments; Enter then dispatches.
    setInput(`/${opt.name} `);
    setSlashQuery(null);
    requestAnimationFrame(() => {
      const ta = inputRef.current;
      ta?.focus();
      const pos = opt.name.length + 2;
      ta?.setSelectionRange(pos, pos);
    });
  };

  /** Keyboard routing while the slash menu is open. Returns true if handled. */
  const handleSlashKeyDown = (e: React.KeyboardEvent): boolean => {
    if (slashQuery === null || slashOptions.length === 0) return false;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSlashIndex((i) => (i + 1) % slashOptions.length);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSlashIndex((i) => (i - 1 + slashOptions.length) % slashOptions.length);
      return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applySlashSelection();
      return true;
    }
    if (e.key === "Escape") {
      setSlashQuery(null);
      return true;
    }
    return false;
  };

  const handleViewLogs = async () => {
    try {
      setLogs((await api.getAgentLogs()).logs || "(empty)");
    } catch (e: any) {
      setError(e.message);
    }
  };

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, activePermissions.length, activeQuestions.length]);

  // ------------------------------------------------------------------
  //  Render
  // ------------------------------------------------------------------
  return (
    <div style={styles.container}>
      <div style={styles.sidebar}>
        <div style={styles.sidebarHeader}>
          <div style={styles.brand}>
            <span style={styles.brandIcon}>🤖</span>
            <span style={styles.brandText}>Agent Platform</span>
          </div>
          <div style={styles.userInfo}>
            <span style={styles.userAvatar}>{username[0]?.toUpperCase()}</span>
            <span style={styles.userName}>{username}</span>
            {role === "admin" && onOpenAdmin && (
              <button style={styles.logoutBtn} onClick={onOpenAdmin} title="Docker 容器管理（仅管理员）">
                管理
              </button>
            )}
            <button style={styles.logoutBtn} onClick={onLogout}>退出</button>
          </div>
        </div>

        <div style={styles.statusPanel}>
          <div style={styles.statusRow}>
            <span
              style={{
                ...styles.statusDot,
                background: isAgentRunning
                  ? "#22c55e"
                  : starting
                  ? "#3b82f6"
                  : agentStatus?.status === "stopped"
                  ? "#f59e0b"
                  : "#52525b",
              }}
            />
            <span style={styles.statusText}>
              {busy
                ? busy
                : starting
                ? startupPhaseLabel
                : isAgentRunning
                ? "opencode serve 运行中"
                : agentStatus?.status === "stopped"
                ? "容器已停止"
                : "容器未启动"}
            </span>
          </div>
          {agentStatus?.container_name && (
            <div style={styles.containerName}>📦 {agentStatus.container_name}</div>
          )}
          <div style={styles.statusDetail}>
            状态: {agentStatus?.status || "absent"} | 健康: {agentStatus?.healthy ? "✓" : "✗"}
          </div>

          {runtime && (
            <div style={styles.runtimeBox}>
              <div>
                <span style={styles.runtimeKey}>运行时 </span>
                <span style={styles.runtimeVal}>{runtime.runtime}</span>
              </div>
              <div>
                <span style={styles.runtimeKey}>镜像 </span>
                <span style={styles.runtimeVal}>{runtime.image}</span>
              </div>
              <div>
                <span style={styles.runtimeKey}>工作区 </span>
                <span style={styles.runtimeVal}>{runtime.workdir}</span>
              </div>
              <div>
                <span style={styles.runtimeKey}>配置 </span>
                <span style={styles.runtimeVal}>
                  {runtime.config?.mounted ? "已挂载宿主 opencode.json" : "使用容器默认配置"}
                </span>
              </div>
              {!!runtime.config?.stripped?.length && (
                <div>
                  <span style={styles.runtimeKey}>已剥离 </span>
                  <span style={styles.runtimeVal}>{runtime.config.stripped.join(", ")}</span>
                </div>
              )}
            </div>
          )}

          <div style={styles.statusButtons}>
            {!isAgentRunning ? (
              <button style={styles.startBtn} onClick={handleStartAgent} disabled={!!busy || starting}>
                {starting ? "启动中…" : "启动 Agent"}
              </button>
            ) : (
              <button style={styles.stopBtn} onClick={handleStopAgent} disabled={!!busy}>
                停止 Agent
              </button>
            )}
            <button style={styles.logBtn} onClick={handleViewLogs}>日志</button>
            <button style={styles.logBtn} onClick={() => setShowConfig(true)}>配置管理</button>
          </div>
        </div>

        <div style={styles.sessionsSection}>
          <div style={styles.sessionsHeader}>
            <span>会话列表</span>
            <button
              style={styles.newSessionBtn}
              onClick={handleNewSession}
              disabled={!isAgentRunning}
            >
              +
            </button>
          </div>
          <div style={styles.sessionsList}>
            {sessions.length === 0 && (
              <div style={styles.emptySessions}>
                {isAgentRunning ? "点击 + 创建新会话" : "请先启动 Agent"}
              </div>
            )}
            {sessions.map((s) => (
              <div
                key={s.id}
                style={currentSession?.id === s.id ? styles.sessionItemActive : styles.sessionItem}
                onClick={() => openSession(s)}
              >
                <span style={styles.sessionIcon}>💬</span>
                <span style={styles.sessionItemActiveTitle}>
                  {s.title || s.id.slice(0, 12)}
                </span>
                <div style={styles.sessionActions}>
                  <button
                    style={styles.sessionActionBtn}
                    title="重命名会话"
                    onClick={(e) => handleRenameSession(s, e)}
                  >
                    ✏️
                  </button>
                  <button
                    style={styles.sessionActionBtn}
                    title="删除会话"
                    onClick={(e) => handleDeleteSession(s, e)}
                  >
                    🗑️
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div style={styles.archPanel}>
          <div style={styles.archTitle}>四层架构</div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("browser")} /> 浏览器层 (React SPA)
          </div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("platform")} /> 平台控制层 (FastAPI 反向代理)
          </div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("container")} /> 容器执行层 (opencode serve)
          </div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("shared")} /> 共享服务层 (Postgres / Redis)
          </div>
        </div>
      </div>

      <div style={styles.mainArea}>
        {isAgentRunning && (
          <div style={styles.modelBar}>
            <span style={styles.modelLabel}>模型</span>
            <select
              style={styles.select}
              value={modelKey(model)}
              onChange={(e) => handleModelChange(e.target.value)}
            >
              <option value="">
                {modelOptions.length ? "选择模型…" : "opencode 未配置任何 provider"}
              </option>
              {modelOptions.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
            {activeBaseURL && <span style={styles.baseUrlTag}>{activeBaseURL}</span>}
            {primaryAgents.length > 0 && (
              <>
                <span style={styles.modelLabel}>Agent</span>
                <select
                  style={styles.select}
                  value={agentId}
                  onChange={(e) => handleAgentChange(e.target.value)}
                  title="切换当前会话的 Agent 模式（新会话同样生效）"
                >
                  {primaryAgents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.id}
                      {a.description ? ` · ${a.description}` : ""}
                    </option>
                  ))}
                </select>
              </>
            )}
            <span style={styles.modelLabel}>
              来源：容器内 opencode /config
              {providers?.source?.mounted ? "（宿主 opencode.json）" : ""}
            </span>
            <button
              style={styles.reloadBtn}
              onClick={handleSummarize}
              disabled={!!busy || !!busyLabel || !model}
              title="让模型总结当前会话（生成摘要消息）"
            >
              {busyLabel === "生成摘要中…" ? busyLabel : "✨ 摘要"}
            </button>
            <button style={styles.reloadBtn} onClick={handleReloadConfig} disabled={!!busy}>
              重载配置
            </button>
            <button style={styles.reloadBtn} onClick={() => setShowFiles((v) => !v)}>
              {showFiles ? "隐藏文件" : "📁 文件"}
            </button>
          </div>
        )}

        {error && (
          <div style={styles.errorBanner}>
            <span>{error}</span>
            <button style={styles.errorClose} onClick={() => setError("")}>×</button>
          </div>
        )}

        {/* P1-4: the stream self-heals, but the user deserves to know that
            replies may lag while it reconnects. */}
        {isAgentRunning && sseDown && (
          <div style={styles.sseDownBanner}>
            ⚠ 实时事件流已断开，正在自动重连…（新消息可能延迟显示）
          </div>
        )}

        <div style={styles.chatBody}>
        <div style={styles.chatColumn}>
        {!isAgentRunning ? (
          <Welcome
            onStart={handleStartAgent}
            disabled={!!busy}
            phase={starting ? agentStatus?.status || "creating" : undefined}
            phaseIdx={startupPhaseIdx}
            elapsedSec={startupElapsedSec}
          />
        ) : !currentSession ? (
          <div style={styles.noSession}>
            <div style={styles.noSessionIcon}>💬</div>
            <p style={styles.noSessionText}>选择一个会话，或创建新会话开始对话</p>
            <button style={styles.newSessionLargeBtn} onClick={handleNewSession}>
              创建新会话
            </button>
          </div>
        ) : (
          <>
            <div style={styles.messagesArea}>
              {/* P1-1: the agent's live task list (todo.updated events). */}
              {todos.length > 0 && <TodoList todos={todos} />}
              {/* 重新生成按钮只挂在最后一条 assistant 回复上（其后没有其它回合）。 */}
              {turns.map((t) => {
                const last = turns[turns.length - 1];
                const regenTargetId =
                  last && last.role === "assistant" && !last.streaming ? last.id : null;
                return (
                  <TurnView
                    key={t.id}
                    turn={t}
                    username={username}
                    reverted={revertedId === t.id}
                    revertBusy={revertBusy}
                    canRevert={!!currentSession && !isGenerating}
                    onRevert={handleRevert}
                    onUnrevert={handleUnrevert}
                    onFork={t.role === "user" && !isGenerating ? handleFork : undefined}
                    onRegenerate={
                      t.id === regenTargetId && !!currentSession && !isGenerating
                        ? handleRegenerate
                        : undefined
                    }
                  />
                );
              })}
              {isGenerating && !turns.some((t) => t.streaming) && (
                <div style={styles.msgAssistant}>
                  <div style={styles.msgAvatarAssistant}>🤖</div>
                  <div style={styles.msgContent}>
                    <div style={styles.msgRole}>
                      opencode <span style={styles.streaming}>思考中…</span>
                    </div>
                  </div>
                </div>
              )}

              {activePermissions.map((p) => (
                <div key={p.id} style={styles.permCard}>
                  <div style={styles.permTitle}>🔑 工具权限请求 · {p.action}</div>
                  {p.resources.map((r, i) => (
                    <div key={i} style={styles.permRes}>{r}</div>
                  ))}
                  {/* P1-5: show the tool's raw input arguments so the user can
                      make an informed allow/reject decision. */}
                  {p.metadata && Object.keys(p.metadata).length > 0 && (
                    <pre style={styles.permMeta}>
                      {JSON.stringify(p.metadata, null, 2)}
                    </pre>
                  )}
                  <div style={styles.permActions}>
                    <button
                      style={styles.permAllowBtn}
                      onClick={() => handlePermissionReply(p.id, "once")}
                    >
                      允许一次
                    </button>
                    <button
                      style={styles.permAlwaysBtn}
                      onClick={() => handlePermissionReply(p.id, "always")}
                    >
                      总是允许
                    </button>
                    <button
                      style={styles.permRejectBtn}
                      onClick={() => handlePermissionReply(p.id, "reject")}
                    >
                      拒绝
                    </button>
                  </div>
                </div>
              ))}

              {activeQuestions.map((q) => (
                <QuestionCard
                  key={q.id}
                  request={q}
                  onSubmit={handleQuestionReply}
                  onReject={handleQuestionReject}
                />
              ))}

              <div ref={bottomRef} />
            </div>

            <div style={styles.inputArea}>
              <div style={styles.attachBar}>
                {primaryAgents.length > 0 && (
                  <>
                    <select
                      aria-label="本条消息 Agent"
                      style={{ ...styles.select, maxWidth: "180px" }}
                      value={promptAgent ?? ""}
                      onChange={(e) => setPromptAgent(e.target.value || undefined)}
                    >
                      <option value="">本条 Agent（会话默认）</option>
                      {primaryAgents.map((agent) => (
                        <option key={agent.id} value={agent.id}>
                          {agent.id}
                        </option>
                      ))}
                    </select>
                    {promptAgent && (
                      <button
                        style={styles.chipRemove}
                        onClick={() => setPromptAgent(undefined)}
                        title="清除本条消息 Agent 选择"
                        aria-label="清除本条消息 Agent 选择"
                      >
                        ×
                      </button>
                    )}
                  </>
                )}
                <select
                  aria-label="本条消息模型"
                  style={{ ...styles.select, maxWidth: "220px" }}
                  value={modelKey(promptModel)}
                  onChange={(e) =>
                    setPromptModel(modelOptions.find((option) => option.key === e.target.value)?.ref)
                  }
                >
                  <option value="">本条模型（会话默认）</option>
                  {modelOptions.map((option) => (
                    <option key={option.key} value={option.key}>
                      {option.label}
                    </option>
                  ))}
                </select>
                {promptModel && (
                  <button
                    style={styles.chipRemove}
                    onClick={() => setPromptModel(undefined)}
                    title="清除本条消息模型选择"
                    aria-label="清除本条消息模型选择"
                  >
                    ×
                  </button>
                )}
              </div>

              {/* Attachment chips + selected skills, shown above the textarea */}
              {(attachments.length > 0 || selectedSkills.length > 0) && (
                <div style={styles.attachBar}>
                  {selectedSkills.map((s) => (
                    <span key={s} style={styles.skillChip} title="本条消息显式指定的 skill">
                      🧩 {s}
                      <button
                        style={styles.chipRemove}
                        onClick={() => toggleSkill(s)}
                        title="移除"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  {attachments.map((a) => (
                    <span
                      key={a.dataUrl ?? a.path}
                      style={styles.attachChip}
                      title={`${a.path || a.filename} (${Math.ceil(a.size / 1024)}KB)${a.dataUrl ? " · base64 直传" : ""}`}
                    >
                      {a.isImage ? "🖼️" : "📎"} {a.filename}
                      <button
                        style={styles.chipRemove}
                        onClick={() =>
                          setAttachments((prev) =>
                            prev.filter((x) => (x.dataUrl ?? x.path) !== (a.dataUrl ?? a.path))
                          )
                        }
                        title="移除"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}

              <div style={styles.inputRow}>
                {/* Skill picker dropdown */}
                <div style={styles.skillPickerWrap}>
                  <button
                    style={styles.skillBtn}
                    onClick={() => setSkillMenuOpen((v) => !v)}
                    title="为这条消息显式指定 skill"
                    disabled={allSkills.length === 0}
                  >
                    🧩 {selectedSkills.length > 0 ? `×${selectedSkills.length}` : "Skill"}
                  </button>
                  {skillMenuOpen && (
                    <>
                      <div style={styles.skillBackdrop} onClick={() => setSkillMenuOpen(false)} />
                      <div style={styles.skillMenu}>
                        <div style={styles.skillMenuHeader}>选择要显式使用的 skill</div>
                        {allSkills.length === 0 && (
                          <div style={styles.skillEmpty}>暂无可用 skill（可在配置面板添加）</div>
                        )}
                        {allSkills.map((s) => (
                          <label key={`${s.scope}-${s.name}`} style={styles.skillItem}>
                            <input
                              type="checkbox"
                              checked={selectedSkills.includes(s.name)}
                              onChange={() => toggleSkill(s.name)}
                            />
                            <span style={styles.skillItemName}>{s.name}</span>
                            <span style={s.scope === "project" ? styles.scopeProject : styles.scopeGlobal}>
                              {s.scope === "project" ? "项目" : "全局"}
                            </span>
                          </label>
                        ))}
                        {allSkills.length > 0 && (
                          <button style={styles.skillMenuClose} onClick={() => setSkillMenuOpen(false)}>
                            完成{selectedSkills.length > 0 ? `（已选 ${selectedSkills.length}）` : ""}
                          </button>
                        )}
                      </div>
                    </>
                  )}
                </div>

                {/* File upload button */}
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  hidden
                  onChange={(e) => handleFilesPicked(e.target.files)}
                />
                <button
                  style={styles.skillBtn}
                  onClick={() => fileInputRef.current?.click()}
                  title="附加图片（base64 直传）或上传文件到工作空间"
                  disabled={uploading}
                >
                  {uploading ? "⏳" : "📎"}
                </button>

                <div style={styles.atMenuWrap}>
                  <textarea
                    ref={inputRef}
                    style={styles.textInput}
                    placeholder={
                      uploading
                        ? "上传文件中…"
                        : "输入消息…（/ 执行命令，@ 引用文件，Enter 发送，Shift+Enter 换行）"
                    }
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={(e) => {
                      if (handleSlashKeyDown(e)) return;
                      if (handleAtKeyDown(e)) return;
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleSend();
                      }
                    }}
                    rows={1}
                  />
                  {slashQuery !== null && slashOptions.length > 0 && (
                    <div style={styles.atMenu}>
                      <div style={styles.atMenuHeader}>
                        {slashQuery.toLowerCase() === "agents"
                          ? "选择 Agent · ↑↓ 选择 · Enter 切换 · Esc 关闭"
                          : "命令 · ↑↓ 选择 · Enter/Tab 选中 · Esc 关闭"}
                      </div>
                      {slashOptions.map((o, i) => (
                        <div
                          key={`${o.kind}:${o.name}`}
                          style={{ ...styles.atItem, ...(i === slashIndex ? styles.atItemActive : {}) }}
                          onClick={() => applySlashSelection(i)}
                          onMouseEnter={() => setSlashIndex(i)}
                        >
                          <span style={styles.atItemIcon}>{o.kind === "agent" ? "🤖" : "⌘"}</span>
                          <span style={styles.cmdName}>
                            {o.kind === "agent" ? o.name : `/${o.name}`}
                          </span>
                          {o.description && <span style={styles.cmdDesc}>{o.description}</span>}
                          {o.kind === "command" && o.source && (
                            <span style={styles.cmdSource}>{o.source}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {atQuery !== null && atOptions.length > 0 && (
                    <div style={styles.atMenu}>
                      <div style={styles.atMenuHeader}>
                        {showAgentMentions
                          ? "提及 Agent / 引用文件 · ↑↓ 选择 · Enter/Tab 插入 · Esc 关闭"
                          : "引用工作区文件 · ↑↓ 选择 · Enter/Tab 插入 · Esc 关闭"}
                      </div>
                      {atOptions.map((o, i) => (
                        <div
                          key={o.kind === "agent" ? `a:${o.name}` : `f:${o.path}`}
                          style={{ ...styles.atItem, ...(i === atIndex ? styles.atItemActive : {}) }}
                          onClick={() => applyAtSelection(o)}
                          onMouseEnter={() => setAtIndex(i)}
                        >
                          {o.kind === "agent" ? (
                            <>
                              <span style={styles.atItemIcon}>🤖</span>
                              <span style={styles.atItemAgentName}>{o.name}</span>
                              {o.description && (
                                <span style={styles.atItemAgentDesc}>{o.description}</span>
                              )}
                            </>
                          ) : (
                            <>
                              <span style={styles.atItemIcon}>📄</span>
                              <span style={styles.atItemPath}>{o.path}</span>
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                {isGenerating ? (
                  <button style={styles.abortBtn} onClick={handleInterrupt}>停止</button>
                ) : (
                  <button
                    style={styles.sendBtn}
                    onClick={handleSend}
                    disabled={!input.trim() && attachments.length === 0}
                  >
                    发送
                  </button>
                )}
              </div>
            </div>
          </>
        )}
        </div>

        {showFiles && isAgentRunning && (
          <FilesPanel
            files={wsFiles}
            preview={preview}
            previewLoading={previewLoading}
            onOpen={openPreview}
            onRefresh={loadWsFiles}
            onInsert={insertAtReference}
            onBack={() => setPreview(null)}
            onClose={() => setShowFiles(false)}
            onUpload={handleWsFilesPicked}
            uploading={wsUploading}
            notice={wsNotice}
          />
        )}
        </div>
      </div>

      {logs && (
        <div style={styles.modal} onClick={() => setLogs("")}>
          <div style={styles.modalContent} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalHeader}>
              <span>容器日志 · opencode serve</span>
              <button style={styles.modalClose} onClick={() => setLogs("")}>×</button>
            </div>
            <pre style={styles.modalBody}>{logs}</pre>
          </div>
        </div>
      )}

      {showConfig && <ConfigPanel onClose={() => setShowConfig(false)} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
//  Workspace file browser panel
// ---------------------------------------------------------------------------

type WsFile = { path: string; type: "file" | "dir"; size: number };
type PreviewState = { path: string; type: string; mime: string; content?: string; base64?: string };

type TreeNode = WsFile & { name: string; children: TreeNode[] };

/** Flat find(1) output → nested tree, dirs first then files, each sorted. */
function buildTree(files: WsFile[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", type: "dir", size: 0, children: [] };
  const dirs = new Map<string, TreeNode>([["", root]]);
  const ensureDir = (path: string): TreeNode => {
    const hit = dirs.get(path);
    if (hit) return hit;
    const parts = path.split("/");
    const name = parts.pop()!;
    const parent = ensureDir(parts.join("/"));
    const node: TreeNode = { name, path, type: "dir", size: 0, children: [] };
    parent.children.push(node);
    dirs.set(path, node);
    return node;
  };
  for (const f of files) {
    if (f.type === "dir") {
      ensureDir(f.path);
      continue;
    }
    const parts = f.path.split("/");
    const name = parts.pop()!;
    ensureDir(parts.join("/")).children.push({
      name, path: f.path, type: "file", size: f.size, children: [],
    });
  }
  const sortRec = (n: TreeNode) => {
    n.children.sort((a, b) =>
      a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1
    );
    n.children.forEach(sortRec);
  };
  sortRec(root);
  return root.children;
}

const fmtSize = (n: number) =>
  n < 1024 ? `${n}B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)}K` : `${(n / 1024 / 1024).toFixed(1)}M`;

function TreeRow({
  depth, icon, label, onClick, children,
}: {
  depth: number; icon: string; label: string;
  onClick?: () => void; children?: React.ReactNode;
}) {
  const [hover, setHover] = useState(false);
  return (
    <div
      style={{
        ...styles.treeRow,
        paddingLeft: `${8 + depth * 14}px`,
        ...(hover ? styles.treeRowHover : {}),
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={onClick}
    >
      <span style={styles.atItemIcon}>{icon}</span>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
      {children}
    </div>
  );
}

function TreeView({
  nodes, depth, collapsed, onToggle, onOpenFile, onInsert, activePath,
}: {
  nodes: TreeNode[];
  depth: number;
  collapsed: Set<string>;
  onToggle: (path: string) => void;
  onOpenFile: (path: string) => void;
  onInsert: (path: string) => void;
  activePath?: string;
}) {
  return (
    <>
      {nodes.map((n) =>
        n.type === "dir" ? (
          <div key={`d-${n.path}`}>
            <TreeRow depth={depth} icon={collapsed.has(n.path) ? "📁" : "📂"} label={n.name} onClick={() => onToggle(n.path)} />
            {!collapsed.has(n.path) && (
              <TreeView
                nodes={n.children}
                depth={depth + 1}
                collapsed={collapsed}
                onToggle={onToggle}
                onOpenFile={onOpenFile}
                onInsert={onInsert}
                activePath={activePath}
              />
            )}
          </div>
        ) : (
          <TreeRow
            key={`f-${n.path}`}
            depth={depth}
            icon="📄"
            label={n.name}
            onClick={() => onOpenFile(n.path)}
          >
            {activePath === n.path && <span style={{ fontSize: "10px", color: "#2563eb" }}>预览中</span>}
            <span style={styles.treeFileSize}>{fmtSize(n.size)}</span>
            <button
              style={styles.previewBack}
              title="插入 @ 引用到输入框"
              onClick={(e) => {
                e.stopPropagation();
                onInsert(n.path);
              }}
            >
              @
            </button>
          </TreeRow>
        )
      )}
    </>
  );
}

/** Minimal markdown rendering for the preview pane (headings/lists/code/bold). */
function MiniMarkdown({ src }: { src: string }) {
  const out: React.ReactNode[] = [];
  let key = 0;
  let inCode = false;
  let codeBuf: string[] = [];

  const inline = (s: string): React.ReactNode => {
    const parts: React.ReactNode[] = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(s))) {
      if (m.index > last) parts.push(s.slice(last, m.index));
      if (m[0].startsWith("**")) {
        parts.push(<strong key={key++} style={styles.mdStrong}>{m[0].slice(2, -2)}</strong>);
      } else {
        parts.push(<code key={key++} style={styles.mdCode}>{m[0].slice(1, -1)}</code>);
      }
      last = m.index + m[0].length;
    }
    if (last < s.length) parts.push(s.slice(last));
    return parts;
  };

  for (const line of src.split("\n")) {
    if (line.trim().startsWith("```")) {
      if (inCode && codeBuf.length) {
        out.push(<pre key={key++} style={styles.mdCodeBlock}>{codeBuf.join("\n")}</pre>);
        codeBuf = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      codeBuf.push(line);
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      out.push(
        <div key={key++} style={h[1].length <= 1 ? styles.mdTitle : styles.mdH2}>
          {inline(h[2])}
        </div>
      );
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      out.push(
        <div key={key++} style={styles.mdLi}>{inline(line.replace(/^\s*[-*+]\s+/, ""))}</div>
      );
      continue;
    }
    if (!line.trim()) {
      out.push(<div key={key++} style={{ height: "8px" }} />);
      continue;
    }
    out.push(<p key={key++} style={styles.mdP}>{inline(line)}</p>);
  }
  if (inCode && codeBuf.length) {
    out.push(<pre key={key++} style={styles.mdCodeBlock}>{codeBuf.join("\n")}</pre>);
  }
  return <>{out}</>;
}

function FilesPanel({
  files, preview, previewLoading, onOpen, onRefresh, onInsert, onBack, onClose,
  onUpload, uploading, notice,
}: {
  files: WsFile[];
  preview: PreviewState | null;
  previewLoading: boolean;
  onOpen: (path: string) => void;
  onRefresh: () => void;
  onInsert: (path: string) => void;
  onBack: () => void;
  onClose: () => void;
  onUpload: (files: FileList | null) => void;
  uploading: boolean;
  notice?: string;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const tree = useMemo(() => buildTree(files), [files]);
  const fileCount = useMemo(() => files.filter((f) => f.type === "file").length, [files]);

  const baseName = (p: string) => p.split("/").pop() || p;

  return (
    <div style={styles.filesPanel}>
      {/* Hidden picker feeding the workspace upload endpoint. */}
      <input
        ref={uploadInputRef}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          onUpload(e.target.files);
          e.target.value = ""; // allow re-picking the same file
        }}
      />
      <div style={styles.filesPanelHeader}>
        {preview ? (
          <>
            <button style={styles.previewBack} onClick={onBack} title="返回文件列表">← 返回</button>
            <span
              style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              title={preview.path}
            >
              {baseName(preview.path)}
            </span>
            <button style={styles.previewBack} title="插入 @ 引用到输入框" onClick={() => onInsert(preview.path)}>
              @ 引用
            </button>
          </>
        ) : (
          <>
            <span style={{ flex: 1 }}>📁 工作区（{fileCount} 个文件）</span>
            <button
              style={{ ...styles.wsUploadBtn, ...(uploading ? { opacity: 0.6 } : {}) }}
              onClick={() => uploadInputRef.current?.click()}
              title="上传文件到后端工作空间（存入 tmp/）"
              disabled={uploading}
            >
              {uploading ? "⏳ 上传中…" : "⬆ 上传"}
            </button>
            <button style={styles.previewBack} onClick={onRefresh} title="刷新">刷新</button>
            <button style={styles.previewBack} onClick={onClose} title="关闭面板">×</button>
          </>
        )}
      </div>

      {!preview && notice && <div style={styles.wsNotice}>✓ {notice}</div>}

      {preview ? (
        preview.type === "loading" ? (
          <div style={styles.previewBody}>加载中…</div>
        ) : preview.type === "error" ? (
          <div style={styles.previewBody}>
            <div style={styles.turnError}>⚠ {preview.content}</div>
          </div>
        ) : preview.type === "text" && preview.mime === "text/html" ? (
          // Static sandbox: no scripts, no same-origin access.
          <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
            <iframe
              style={{ ...styles.previewFrame, flex: 1 }}
              srcDoc={preview.content}
              sandbox=""
              title={preview.path}
            />
          </div>
        ) : (
          <div style={styles.previewBody}>
            {previewLoading && <div>加载中…</div>}
            {preview.type === "text" && preview.mime === "text/markdown" && (
              <MiniMarkdown src={preview.content || ""} />
            )}
            {preview.type === "text" && preview.mime !== "text/markdown" && (
              <pre style={styles.previewPre}>{preview.content}</pre>
            )}
            {preview.type === "image" && (
              <img
                style={styles.previewImg}
                src={`data:${preview.mime};base64,${preview.base64}`}
                alt={preview.path}
              />
            )}
            {preview.type === "binary" && (
              <div style={styles.previewBinary}>
                二进制文件（{preview.mime}），无法在此预览。
                <br />
                可通过 @ 引用让 Agent 处理。
              </div>
            )}
          </div>
        )
      ) : (
        <div style={styles.filesPanelBody}>
          {files.length === 0 ? (
            <div style={{ padding: "16px 12px", fontSize: "12px", color: "#5b6472" }}>
              工作区为空（或读取失败，请刷新重试）
            </div>
          ) : (
            <TreeView
              nodes={tree}
              depth={0}
              collapsed={collapsed}
              onToggle={toggle}
              onOpenFile={onOpen}
              onInsert={onInsert}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
//  Sub-components
// ---------------------------------------------------------------------------

/** "/workspace/foo" → "foo" (display only — paths are container-absolute). */
const stripWorkspace = (p: string): string =>
  p.startsWith("/workspace/") ? p.slice(11) : p;

/** Diff line → syntax highlight style (adds/dels/hunk/headers). */
const diffLineStyle = (line: string): CSSProperties | undefined =>
  line.startsWith("+") && !line.startsWith("+++")
    ? styles.diffLineAdd
    : line.startsWith("-") && !line.startsWith("---")
    ? styles.diffLineDel
    : line.startsWith("@@")
    ? styles.diffLineHunk
    : line.startsWith("Index:") || line.startsWith("===")
    ? styles.diffLineMeta
    : undefined;

function TurnView({
  turn,
  username,
  reverted,
  revertBusy,
  canRevert,
  onRevert,
  onUnrevert,
  onFork,
  onRegenerate,
}: {
  turn: Turn;
  username: string;
  reverted: boolean;
  revertBusy: boolean;
  canRevert: boolean;
  onRevert: (messageId: string) => void;
  onUnrevert: () => void;
  onFork?: (messageId?: string) => void;
  /** 传入即在该 assistant 回复的角色行显示"重新生成"按钮。 */
  onRegenerate?: (assistantId: string) => void;
}) {
  if (turn.role === "system") {
    const text = turn.blocks.map((b) => ("text" in b ? b.text : "")).join(" ");
    return (
      <div style={styles.msgSystem}>
        <span style={styles.systemNote}>{text}</span>
      </div>
    );
  }

  const isUser = turn.role === "user";
  return (
    <div style={isUser ? styles.msgUser : styles.msgAssistant}>
      <div style={isUser ? styles.msgAvatarUser : styles.msgAvatarAssistant}>
        {isUser ? username[0]?.toUpperCase() : "🤖"}
      </div>
      <div style={styles.msgContent}>
        <div style={styles.msgRole}>
          <span>{isUser ? "You" : turn.agent ? `opencode · ${turn.agent}` : "opencode (容器内)"}</span>
          {turn.model && (
            <span style={styles.modelTag}>
              {turn.model.providerID}/{turn.model.id}
            </span>
          )}
          {turn.streaming && <span style={styles.streaming}>streaming…</span>}
          {isUser && onFork && (
            <button
              style={styles.forkBtn}
              title="从此处分叉：复制到新会话（保留到本回合为止的历史）"
              onClick={() => onFork(turn.id)}
            >
              ⑂ 分叉
            </button>
          )}
          {!isUser && onRegenerate && (
            <button
              style={styles.regenBtn}
              title="重新生成：回退本回合并按原输入重新请求（替换当前回复）"
              onClick={() => onRegenerate(turn.id)}
            >
              ↻ 重新生成
            </button>
          )}
        </div>

        {/* @-mentioned agents on this user message */}
        {isUser && turn.agents && turn.agents.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "6px", justifyContent: "flex-end" }}>
            {turn.agents.map((a) => (
              <span key={a} style={styles.agentChip}>🤖 @{a}</span>
            ))}
          </div>
        )}

        {turn.blocks.map((b) => (
          <BlockView key={b.id} block={b} streaming={!!turn.streaming} />
        ))}

        {/* P1-1: per-round file diffs with revert / unrevert */}
        {isUser && turn.diffs && turn.diffs.length > 0 && (
          <DiffCard
            diffs={turn.diffs}
            reverted={reverted}
            busy={revertBusy}
            canAct={canRevert}
            onRevert={onRevert ? () => onRevert(turn.id) : undefined}
            onUnrevert={onUnrevert}
          />
        )}

        {turn.error && <div style={styles.turnError}>⚠ {turn.error}</div>}

        {(turn.cost != null || turn.tokens) && (
          <div style={styles.turnMeta}>
            {turn.tokens &&
              `tokens ↑${turn.tokens.input ?? 0} ↓${turn.tokens.output ?? 0}`}
            {turn.cost != null && `  ·  $${turn.cost.toFixed(4)}`}
          </div>
        )}
      </div>
    </div>
  );
}

/** P1-1: per-round diff card — expandable file rows with syntax-highlighted
 *  patches, plus revert / unrevert actions for the whole round. */
function DiffCard({
  diffs,
  reverted,
  busy,
  canAct,
  onRevert,
  onUnrevert,
}: {
  diffs: FileDiff[];
  reverted: boolean;
  busy: boolean;
  canAct: boolean;
  onRevert?: () => void;
  onUnrevert?: () => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const totalAdd = diffs.reduce((s, d) => s + (d.additions ?? 0), 0);
  const totalDel = diffs.reduce((s, d) => s + (d.deletions ?? 0), 0);
  return (
    <div style={styles.diffCard}>
      <div style={styles.diffCardTitle}>
        <span>📝 本回合文件变更 · {diffs.length} 个文件</span>
        <span>
          <span style={styles.diffStatAdd}>+{totalAdd}</span>{" "}
          <span style={styles.diffStatDel}>−{totalDel}</span>
        </span>
      </div>
      {diffs.map((d) => {
        const open = expanded === d.file;
        return (
          <div key={d.file}>
            <div
              style={styles.diffFileRow}
              onClick={() => setExpanded(open ? null : d.file)}
            >
              <span style={styles.diffFileStatus(d.status ?? "changed")}>
                {d.status ?? "changed"}
              </span>
              <span style={styles.diffFileName} title={d.file}>
                {stripWorkspace(d.file)}
              </span>
              <span style={{ fontSize: "11px", flexShrink: 0 }}>
                <span style={styles.diffStatAdd}>+{d.additions ?? 0}</span>{" "}
                <span style={styles.diffStatDel}>−{d.deletions ?? 0}</span>
              </span>
              <span style={styles.diffChevron}>
                {d.patch ? (open ? "▾" : "▸") : ""}
              </span>
            </div>
            {open && d.patch && (
              <pre style={styles.diffPatchBody}>
                {d.patch.split("\n").map((line, i) => (
                  <div
                    key={i}
                    style={{ ...styles.diffLine, ...(diffLineStyle(line) ?? {}) }}
                  >
                    {line || " "}
                  </div>
                ))}
              </pre>
            )}
          </div>
        );
      })}
      {(onRevert || (reverted && onUnrevert)) && (
        <div style={styles.diffActions}>
          {reverted ? (
            <>
              <span style={styles.revertedNote}>✓ 已回退此回合的文件改动</span>
              <button
                style={styles.unrevertBtn}
                onClick={onUnrevert}
                disabled={busy || !canAct}
              >
                恢复改动
              </button>
            </>
          ) : (
            <button
              style={styles.revertBtn}
              onClick={onRevert}
              disabled={busy || !canAct}
            >
              ⏪ 回退此回合的文件改动
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** P1-1: live todo list rendered from `todo.updated` SSE events. */
function TodoList({ todos }: { todos: TodoItem[] }) {
  const done = todos.filter((t) => t.status === "completed").length;
  const labels: Record<string, string> = {
    pending: "待办",
    in_progress: "进行中",
    completed: "完成",
    cancelled: "已取消",
  };
  return (
    <div style={styles.todoCard}>
      <div style={styles.todoTitle}>
        <span>📋 任务清单</span>
        <span>
          {done}/{todos.length}
        </span>
      </div>
      {todos.map((t, i) => (
        <div key={i} style={styles.todoItem}>
          <span style={styles.todoStatus(t.status)}>{labels[t.status] || t.status}</span>
          <span
            style={{
              ...styles.todoContent,
              ...(t.status === "completed" || t.status === "cancelled"
                ? styles.todoDone
                : {}),
            }}
          >
            {t.content}
          </span>
        </div>
      ))}
    </div>
  );
}

function BlockView({ block, streaming }: { block: Block; streaming: boolean }) {
  const [open, setOpen] = useState(false);

  if (block.kind === "text") {
    if (!block.text && !streaming) return null;
    return (
      <div style={styles.msgText}>
        {block.text}
        {streaming && <span style={styles.cursor}>▊</span>}
      </div>
    );
  }

  if (block.kind === "reasoning") {
    if (!block.text) return null;
    return <div style={styles.reasoningBox}>💭 {block.text}</div>;
  }

  // Protocol-capability blocks (opencode part types) rendered inline instead
  // of falling through to the generic tool accordion below.

  if (block.kind === "patch") {
    if (!block.files.length) return null;
    const hash = block.hash ? block.hash.slice(0, 8) : "";
    return (
      <div style={styles.patchBox} title={block.files.join("\n")}>
        <span>📝</span>
        <span style={styles.patchFiles}>
          {block.files.length === 1
            ? stripWorkspace(block.files[0])
            : `工作区变更 · ${block.files.length} 个文件`}
        </span>
        {hash && <span style={styles.patchHash}>@{hash}</span>}
      </div>
    );
  }

  if (block.kind === "compaction") {
    return (
      <div style={styles.compactionBox}>
        ⚡ 上下文已压缩{block.auto ? "（自动）" : ""}——更早的消息被摘要替代
        {block.overflow ? "（上下文溢出触发）" : ""}
      </div>
    );
  }

  if (block.kind === "subtask") {
    return (
      <div style={styles.subtaskBox} title={block.prompt}>
        <span style={styles.subtaskAgent}>🧩 {block.agent}</span>
        <span>{block.description || "子任务委派"}</span>
      </div>
    );
  }

  if (block.kind === "agentTag") {
    return block.name ? <div style={styles.agentTagChip}>🤖 {block.name}</div> : null;
  }

  if (block.kind === "retry") {
    return (
      <div style={styles.retryBox} title={block.error}>
        ⚠ 第 {block.attempt} 次尝试失败，已自动重试
        {block.error ? `：${block.error}` : ""}
      </div>
    );
  }

  if (block.kind === "file") {
    const name =
      block.filename || stripWorkspace(block.url.replace(/^file:\/\//, ""));
    return name ? <div style={styles.fileChip}>📎 {name}</div> : null;
  }

  const detail =
    block.status === "error"
      ? block.error
      : block.output ||
        (block.input ? JSON.stringify(block.input, null, 2) : "");

  return (
    <div style={styles.toolBox}>
      <div style={styles.toolHeader} onClick={() => setOpen((v) => !v)}>
        <span style={styles.toolDot(block.status)} />
        <span style={styles.toolName}>{block.name}</span>
        <span style={styles.toolStatus}>
          {block.status} {detail ? (open ? "▾" : "▸") : ""}
        </span>
      </div>
      {open && detail && <pre style={styles.toolBody}>{detail}</pre>}
    </div>
  );
}

/** Pending question approval card. Local state holds selected labels per question. */
function QuestionCard({
  request,
  onSubmit,
  onReject,
}: {
  request: OcQuestionRequest;
  onSubmit: (sessionId: string, requestId: string, answers: string[][]) => void;
  onReject: (sessionId: string, requestId: string) => void;
}) {
  // selections[i] = labels selected for questions[i]; custom input is appended
  // to the reply because opencode treats custom answers as free-form labels.
  const [selections, setSelections] = useState<string[][]>(() =>
    request.questions.map(() => [])
  );
  const [customTexts, setCustomTexts] = useState<string[]>(() =>
    request.questions.map(() => "")
  );

  const toggle = (qi: number, label: string, multiple: boolean) => {
    setSelections((prev) =>
      prev.map((sel, i) => {
        if (i !== qi) return sel;
        if (sel.includes(label)) return sel.filter((l) => l !== label);
        return multiple ? [...sel, label] : [label];
      })
    );
  };

  const submit = () => {
    const answers = selections.map((sel, i) => {
      const custom = customTexts[i].trim();
      return custom ? [...sel, custom] : sel;
    });
    onSubmit(request.sessionID, request.id, answers);
  };

  const allAnswered = selections.every(
    (sel, i) => sel.length > 0 || customTexts[i].trim().length > 0
  );

  return (
    <div style={styles.quesCard}>
      <div style={styles.quesTitle}>❓ Agent 需要你的输入</div>
      {request.questions.map((q, qi) => (
        <div key={qi} style={{ marginBottom: "8px" }}>
          {q.header && <div style={styles.quesTitle}>{q.header}</div>}
          <div style={styles.quesQuestion}>{q.question}</div>
          <div style={styles.quesOptions}>
            {q.options.map((o) => {
              const selected = selections[qi]?.includes(o.label);
              return (
                <label key={o.label} style={styles.quesOption}>
                  <input
                    type={q.multiple ? "checkbox" : "radio"}
                    checked={selected}
                    onChange={() => toggle(qi, o.label, !!q.multiple)}
                  />
                  <span style={styles.quesOptionLabel}>{o.label}</span>
                  {o.description && (
                    <span style={styles.quesOptionDesc}>— {o.description}</span>
                  )}
                </label>
              );
            })}
            {q.custom && (
              <input
                type="text"
                style={styles.quesCustomInput}
                placeholder="或输入自定义回答…"
                value={customTexts[qi]}
                onChange={(e) =>
                  setCustomTexts((prev) =>
                    prev.map((t, i) => (i === qi ? e.target.value : t))
                  )
                }
              />
            )}
          </div>
        </div>
      ))}
      <div style={styles.quesActions}>
        <button style={styles.quesSubmitBtn} onClick={submit} disabled={!allAnswered}>
          提交
        </button>
        <button style={styles.quesCancelBtn} onClick={() => onReject(request.sessionID, request.id)}>
          取消
        </button>
      </div>
    </div>
  );
}

function Welcome({
  onStart,
  disabled,
  phase,
  phaseIdx,
  elapsedSec,
}: {
  onStart: () => void;
  disabled: boolean;
  phase?: string;
  phaseIdx?: number;
  elapsedSec?: number | null;
}) {
  const features = [
    ["🔒", "强隔离", "每用户独立容器：文件系统 / 进程 / 网络 / 资源命名空间隔离"],
    ["🛡️", "安全加固", "非 root + cap-drop ALL + no-new-privileges + 只读根文件系统"],
    ["🧩", "零业务耦合", "平台不实现任何 agent 逻辑，全部能力来自容器内 opencode serve"],
    ["🔄", "崩溃自愈", "双层健康检查 + restart policy + /workspace 与 /data 卷持久化"],
  ];
  const phaseText =
    phase === "creating"
      ? "正在创建容器…"
      : phase === "starting"
      ? "正在启动 opencode 服务…"
      : phase === "warming"
      ? "正在预热模型会话…"
      : "启动中…";
  // P1-4: concrete startup progress, e.g. "阶段 2/3 · 已等待 6s"
  const hint =
    phase && (phaseIdx || elapsedSec != null)
      ? `${phaseIdx ? `阶段 ${phaseIdx}/3` : ""}${
          phaseIdx && elapsedSec != null ? " · " : ""
        }${elapsedSec != null ? `已等待 ${elapsedSec}s` : ""}`
      : null;
  return (
    <div style={styles.welcome}>
      <div style={styles.welcomeIcon}>🐳</div>
      <h2 style={styles.welcomeTitle}>Agent Docker Platform</h2>
      <p style={styles.welcomeText}>
        浏览器 → 平台控制层 → 容器执行层（opencode serve）→ 共享服务层
      </p>
      <div style={styles.welcomeFeatures}>
        {features.map(([icon, title, desc]) => (
          <div key={title} style={styles.welcomeFeature}>
            <span style={styles.welcomeFeatureIcon}>{icon}</span>
            <div>
              <div style={styles.welcomeFeatureTitle}>{title}</div>
              <div style={styles.welcomeFeatureDesc}>{desc}</div>
            </div>
          </div>
        ))}
      </div>
      {phase ? (
        <div style={styles.welcomeStarting}>
          <span style={styles.welcomeSpinner}>⏳</span>
          <span>
            {phaseText}
            {hint && <span style={styles.welcomeProgress}>（{hint}）</span>}
          </span>
        </div>
      ) : (
        <button style={styles.welcomeBtn} onClick={onStart} disabled={disabled}>
          启动 Agent 容器
        </button>
      )}
    </div>
  );
}
