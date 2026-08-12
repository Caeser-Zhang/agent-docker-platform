import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AgentRuntime,
  type AgentStatus,
  type ModelRef,
  type OcSession,
  type ProvidersResponse,
} from "../api";
import { reduceEvent, toTurns, type Block, type Turn } from "../oc/messages";
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

export function Chat({ username, onLogout }: { username: string; onLogout: () => void }) {
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null);
  const [runtime, setRuntime] = useState<AgentRuntime | null>(null);
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [model, setModel] = useState<ModelRef | undefined>(undefined);

  const [sessions, setSessions] = useState<OcSession[]>([]);
  const [currentSession, setCurrentSession] = useState<OcSession | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState("");
  const [logs, setLogs] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [showConfig, setShowConfig] = useState(false);

  const esRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  // The SSE callback is registered once; it reads the live session id from here.
  const sessionIdRef = useRef<string | null>(null);

  const isAgentRunning = Boolean(agentStatus?.running && agentStatus?.healthy);

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

  const loadContainerState = useCallback(async () => {
    // Everything below is served by opencode inside the container.
    const [rt, prov, sess] = await Promise.allSettled([
      api.getAgentRuntime(),
      api.getProviders(),
      api.listSessions(),
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
  }, []);

  // ------------------------------------------------------------------
  //  SSE — opencode's own event stream, relayed by the platform
  // ------------------------------------------------------------------
  const connectSSE = useCallback(() => {
    esRef.current?.close();
    const es = api.createEventSource(lastEventIdRef.current);
    esRef.current = es;

    es.onmessage = (event) => {
      let evt: any;
      try {
        evt = JSON.parse(event.data);
      } catch {
        return;
      }
      if (typeof evt.id === "number") lastEventIdRef.current = evt.id;

      const type: string = evt.type || "";
      const data = evt.data || {};

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
      if (type === "agent.disconnected") {
        setError("Agent 未运行，事件流已断开");
        return;
      }

      // Everything else is scoped to one session.
      const sid = data.sessionID;
      if (sid && sid !== sessionIdRef.current) return;

      setTurns((prev) => {
        const r = reduceEvent(prev, type, data);
        if (r.idle) setIsGenerating(false);
        if (r.error) setError(r.error);
        if (r.model) setModel(r.model);
        return r.turns;
      });
    };

    es.onerror = () => {
      // EventSource reconnects on its own; lastEventId replays the gap.
      console.warn("SSE dropped, browser will retry");
    };
  }, []);

  useEffect(() => {
    refreshStatus();
    return () => esRef.current?.close();
  }, [refreshStatus]);

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
    setBusy("启动容器中…");
    try {
      const result = await api.startAgent();
      setAgentStatus(result);
      if (result.running) {
        attachedRef.current = true;
        connectSSE();
        await loadContainerState();
      } else {
        setError(result.message || "启动 Agent 失败");
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  };

  const handleStopAgent = async () => {
    setBusy("停止容器中…");
    try {
      await api.stopAgent();
      esRef.current?.close();
      attachedRef.current = false;
      lastEventIdRef.current = 0;
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
    setTurns([]);
    setIsGenerating(false);
    if (session.model) setModel(session.model);
    try {
      setTurns(toTurns(await api.getMessages(session.id)));
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  const handleNewSession = async () => {
    setError("");
    try {
      const s = await api.createSession(model);
      setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
      await openSession(s);
    } catch (e: any) {
      setError(e.message);
    }
  };

  // ------------------------------------------------------------------
  //  Prompting
  // ------------------------------------------------------------------
  const handleSend = async () => {
    const text = input.trim();
    if (!text || !currentSession || isGenerating) return;
    setError("");
    setInput("");
    setIsGenerating(true);
    // Optimistic bubble; reconciled to the real id by session.next.prompted.
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
      await api.sendPrompt(currentSession.id, text);
    } catch (e: any) {
      setError(e.message);
      setIsGenerating(false);
    }
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

  const handleViewLogs = async () => {
    try {
      setLogs((await api.getAgentLogs()).logs || "(empty)");
    } catch (e: any) {
      setError(e.message);
    }
  };

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

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
                  : agentStatus?.status === "stopped"
                  ? "#f59e0b"
                  : "#52525b",
              }}
            />
            <span style={styles.statusText}>
              {busy
                ? busy
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
              <button style={styles.startBtn} onClick={handleStartAgent} disabled={!!busy}>
                启动 Agent
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
                <span style={styles.sessionTitle}>{s.title || s.id.slice(0, 12)}</span>
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
            <span style={styles.modelLabel}>
              来源：容器内 opencode /config
              {providers?.source?.mounted ? "（宿主 opencode.json）" : ""}
            </span>
            <button style={styles.reloadBtn} onClick={handleReloadConfig} disabled={!!busy}>
              重载配置
            </button>
          </div>
        )}

        {error && (
          <div style={styles.errorBanner}>
            <span>{error}</span>
            <button style={styles.errorClose} onClick={() => setError("")}>×</button>
          </div>
        )}

        {!isAgentRunning ? (
          <Welcome onStart={handleStartAgent} disabled={!!busy} />
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
              {turns.map((t) => (
                <TurnView key={t.id} turn={t} username={username} />
              ))}
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
              <div ref={bottomRef} />
            </div>

            <div style={styles.inputArea}>
              <textarea
                style={styles.textInput}
                placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
                rows={1}
              />
              {isGenerating ? (
                <button style={styles.abortBtn} onClick={handleInterrupt}>停止</button>
              ) : (
                <button style={styles.sendBtn} onClick={handleSend} disabled={!input.trim()}>
                  发送
                </button>
              )}
            </div>
          </>
        )}
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
//  Sub-components
// ---------------------------------------------------------------------------

function TurnView({ turn, username }: { turn: Turn; username: string }) {
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
          <span>{isUser ? "You" : "opencode (容器内)"}</span>
          {turn.model && (
            <span style={styles.modelTag}>
              {turn.model.providerID}/{turn.model.id}
            </span>
          )}
          {turn.streaming && <span style={styles.streaming}>streaming…</span>}
        </div>

        {turn.blocks.map((b) => (
          <BlockView key={b.id} block={b} streaming={!!turn.streaming} />
        ))}

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

function Welcome({ onStart, disabled }: { onStart: () => void; disabled: boolean }) {
  const features = [
    ["🔒", "强隔离", "每用户独立容器：文件系统 / 进程 / 网络 / 资源命名空间隔离"],
    ["🛡️", "安全加固", "非 root + cap-drop ALL + no-new-privileges + 只读根文件系统"],
    ["🧩", "零业务耦合", "平台不实现任何 agent 逻辑，全部能力来自容器内 opencode serve"],
    ["🔄", "崩溃自愈", "双层健康检查 + restart policy + /workspace 与 /data 卷持久化"],
  ];
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
      <button style={styles.welcomeBtn} onClick={onStart} disabled={disabled}>
        启动 Agent 容器
      </button>
    </div>
  );
}
