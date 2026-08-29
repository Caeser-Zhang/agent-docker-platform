/** API client for the platform control layer.
 *
 * Two kinds of call live here:
 *   - platform calls  (/api/auth, /api/agent)  — login and container lifecycle
 *   - tunnelled calls (/api/tunnel/oc/...)     — forwarded verbatim to
 *     `opencode serve` inside the user's container
 *
 * Everything agent-related uses opencode's own routes and payload shapes,
 * verified against opencode 1.18.16's live OpenAPI document (`/doc`):
 *   GET,POST /api/session               { data: SessionV2Info[] | SessionV2Info }
 *   GET  /api/session/{id}              { data: SessionV2Info }
 *   POST /api/session/{id}/prompt       { prompt: { text } } → { data: SessionInputAdmitted }
 *   POST /api/session/{id}/model        { model: ModelRef }   → 204
 *   POST /api/session/{id}/agent        { agent: string }     → 204
 *   POST /api/session/{id}/interrupt    → 204
 *   GET  /session/{id}/message           [{ info, parts }]  (legacy surface, bare array)
 *   GET  /api/model                     { location, data: ModelV2Info[] }
 *   GET  /api/agent                     { location, data: AgentV2Info[] } (native only — listAgents uses legacy /agent for plugin agents)
 *   GET  /api/permission/request        { location, data: PermissionV2Request[] }
 *   POST /api/session/{id}/permission/{rid}/reply  { reply: once|always|reject } → 204
 *   GET  /api/question/request          { location, data: QuestionV2Request[] }
 *   POST /api/session/{id}/question/{rid}/reply    { answers: string[][] }       → 204
 *   POST /api/session/{id}/question/{rid}/reject   → 204
 *
 * Session delete/rename has no v2 route — it lives on the legacy surface:
 *   DELETE /session/{id}   → bare true
 *   PATCH  /session/{id}   { title } → bare legacy Session
 */

import type { FileDiff } from "./oc/messages";

const API_BASE = "/api";
/** Prefix that reverse-proxies straight into the container's opencode server. */
const OC = "/tunnel/oc";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  username: string;
  /** "user" | "admin" — gates the Docker management panel. */
  role?: string;
}

export interface AgentStatus {
  running: boolean;
  healthy: boolean;
  status: string;
  /**
   * Epoch seconds when the CURRENT start flow began (P1-4): set server-side
   * on entering "creating" and kept across phase advances, so any browser —
   * even one that attached mid-start — can show an accurate total wait.
   */
  phase_since?: number | null;
  container_name: string | null;
  workspace: string | null;
  message: string;
  error?: string | null;
}

export interface AgentRuntime {
  runtime: string;
  image: string;
  port: number;
  workdir: string;
  network: string;
  config: {
    source: string;
    mounted: boolean;
    providers: string[];
    stripped: string[];
    host_alias: string;
  };
}

/** opencode ModelRef — the exact shape its API requires. */
export interface ModelRef {
  providerID: string;
  id: string;
  variant?: string;
}

export interface ProviderOption {
  id: string;
  name: string;
  baseURL?: string | null;
  models: { id: string; name: string }[];
}

export interface ProvidersResponse {
  providers: ProviderOption[];
  default: string | null;
  smallModel?: string | null;
  error?: string;
  source?: AgentRuntime["config"];
}

/** One user-owned LLM provider stored in the platform DB (masked view). */
export interface UserLlmProvider {
  id: string;
  provider_id: string;
  name: string | null;
  npm: string;
  baseURL: string | null;
  hasApiKey: boolean;
  models: Record<string, any>;
  created_at: string;
  updated_at: string;
}

/** The user's active LLM selection (empty when none is set). */
export interface ActiveLlm {
  provider_id: string | null;
  model: string | null;
}

/** Payload for creating a user LLM provider. */
export interface UserLlmProviderInput {
  provider_id: string;
  name?: string | null;
  npm?: string;
  base_url?: string | null;
  api_key?: string | null;
  models?: Record<string, any> | null;
}

/** Subset of opencode's SessionV2Info that the UI renders. */
export interface OcSession {
  id: string;
  title: string;
  agent?: string;
  model?: ModelRef;
  cost?: number;
  tokens?: { input: number; output: number; reasoning: number };
  time: { created: number; updated: number };
  location?: { directory: string };
}

/** opencode SSE envelope (GET /api/event), re-emitted by the platform pump. */
export interface OcEvent {
  /** Platform-assigned monotonic id used for replay after reconnect. */
  id: number;
  event_id?: string;
  type: string;
  data: Record<string, any>;
  durable?: { aggregateID: string; seq: number; version: number };
  location?: { directory: string };
}

/** opencode location info, part of the v2 list envelope. */
export interface OcLocation {
  directory: string;
  workspaceID?: string;
  project?: { id: string; directory: string };
}

/** Pagination envelope: GET /api/session, GET /api/session/{id}/message. */
export interface OcPage<T> {
  data: T[];
  cursor: { previous: string | null; next: string | null };
}

/** Location envelope: GET /api/agent, /api/model, /api/permission/request, /api/question/request. */
export interface OcEnvelope<T> {
  location: OcLocation;
  data: T[];
}

/** opencode AgentV2Info (GET /api/agent) — fields the UI renders. */
export interface OcAgent {
  id: string;
  model?: ModelRef;
  system?: string;
  description?: string;
  mode?: "subagent" | "primary" | "all";
  hidden?: boolean;
  /** Present on the legacy /agent surface: true for opencode's own agents. */
  native?: boolean;
  steps?: number;
}

/** opencode command entry (GET /command) — global list incl. plugin commands. */
export interface OcCommand {
  name: string;
  description?: string;
  source?: string;
  template?: string;
}

/** opencode ModelV2Info (GET /api/model) — fields the UI renders. */
export interface OcModel {
  id: string;
  providerID: string;
  family?: string;
  name: string;
  status?: "alpha" | "beta" | "deprecated" | "active";
  enabled?: boolean;
  limit?: { context: number; input?: number; output: number };
}

/** opencode PermissionV2Reply. */
export type OcPermissionReply = "once" | "always" | "reject";

/** opencode PermissionV2Request (GET /api/permission/request). */
export interface OcPermissionRequest {
  /** "per_*" */
  id: string;
  sessionID: string;
  action: string;
  resources: string[];
  save?: string[];
  metadata?: Record<string, unknown>;
  source?: { type: "tool"; messageID: string; callID: string };
}

export interface OcQuestionOption {
  label: string;
  description?: string;
}

/** One question inside a QuestionV2Request. */
export interface OcQuestion {
  question: string;
  header?: string;
  options: OcQuestionOption[];
  multiple?: boolean;
  custom?: boolean;
}

/** opencode QuestionV2Request (GET /api/question/request). */
export interface OcQuestionRequest {
  /** "que_*" */
  id: string;
  sessionID: string;
  questions: OcQuestion[];
  tool?: { messageID: string; callID: string };
}

/** One-shot docker stats sample (admin container list). */
export interface AdminContainerStats {
  cpu_percent: number;
  mem_usage_mb: number;
  mem_limit_mb: number;
  mem_percent: number;
  pids?: number;
}

/** A user container as seen by the admin panel. */
export interface AdminContainer {
  user_id: string;
  username: string | null;
  /** Employee number (工号) from the users table. */
  uid: string | null;
  container_name: string;
  /** Status from the agent_containers DB record ("unmanaged" = no record). */
  db_status: string;
  /** Live status from the Docker daemon ("absent" = no container). */
  docker_status: string;
  /** Docker healthcheck status, e.g. "healthy" / "unhealthy" / null. */
  health: string | null;
  image: string;
  started_at: string | null;
  last_activity: string | null;
  restart_count: number;
  last_error: string | null;
  stats?: AdminContainerStats | null;
}

export interface AdminContainerLogs {
  user_id: string;
  tail: number;
  logs: string;
}

export interface AdminOverview {
  users: { total: number; admins: number };
  containers: {
    records: number;
    by_status: Record<string, number>;
    docker_total: number;
    docker_running: number;
  };
  platform: {
    image: string;
    network: string;
    port: number;
    cpu_limit: number;
    memory_limit: string;
  };
}

async function apiCall<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem("token");
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  // Session-expired interception: a 401 on an authenticated call means the
  // JWT is invalid/expired — every subsequent call would fail identically,
  // so drop the stale session and return to the login page. Unauthenticated
  // calls (login/register) carry no token, so their 401s still surface to
  // the caller as normal error messages.
  if (resp.status === 401 && token) {
    for (const key of ["token", "username", "userId", "role"]) {
      localStorage.removeItem(key);
    }
    window.location.reload();
    throw new Error("登录已过期，请重新登录");
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    // Platform errors use {detail}; opencode errors use {message}.
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export const api = {
  // --- Auth -------------------------------------------------------------
  async register(username: string, password: string): Promise<TokenResponse> {
    return apiCall("/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },

  async login(username: string, password: string): Promise<TokenResponse> {
    return apiCall("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },

  // --- Agent container lifecycle (platform control plane) ---------------
  async getAgentStatus(): Promise<AgentStatus> {
    return apiCall("/agent/status");
  },

  async getAgentRuntime(): Promise<AgentRuntime> {
    return apiCall("/agent/runtime");
  },

  async startAgent(): Promise<AgentStatus> {
    return apiCall("/agent/start", { method: "POST", body: JSON.stringify({}) });
  },

  async stopAgent(): Promise<AgentStatus> {
    return apiCall("/agent/stop", { method: "POST" });
  },

  async getAgentLogs(): Promise<{ logs: string }> {
    return apiCall("/agent/logs");
  },

  // --- Admin — platform-wide Docker management (role=admin only) --------
  async getAdminOverview(): Promise<AdminOverview> {
    return apiCall("/admin/overview");
  },

  async getAdminContainers(stats = true): Promise<{ containers: AdminContainer[] }> {
    return apiCall(`/admin/containers?stats=${stats}`);
  },

  async getAdminContainerLogs(userId: string, tail = 200): Promise<AdminContainerLogs> {
    return apiCall(`/admin/containers/${userId}/logs?tail=${tail}`);
  },

  async adminRestartContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/restart`, { method: "POST" });
  },

  async adminStopContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/stop`, { method: "POST" });
  },

  async adminDestroyContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/destroy`, { method: "POST" });
  },

  // --- LLM configuration (read from opencode's own /config) -------------
  async getProviders(): Promise<ProvidersResponse> {
    return apiCall("/tunnel/providers");
  },

  /** Re-inject the host opencode.json and restart the container. */
  async reloadConfig(): Promise<{ reloaded: boolean }> {
    return apiCall("/tunnel/config/reload", { method: "POST" });
  },

  // --- Sessions (opencode /api/session) ---------------------------------
  async listSessions(): Promise<OcSession[]> {
    const r = await apiCall<OcPage<OcSession>>(`${OC}/api/session`);
    return r.data ?? [];
  },

  /**
   * opencode derives the project from `location.directory`; /workspace is the
   * per-user volume mounted into the container.
   */
  async createSession(model?: ModelRef, agent = "coder"): Promise<OcSession> {
    const body: Record<string, any> = {
      agent,
      location: { directory: "/workspace" },
    };
    if (model) body.model = model;
    const r = await apiCall<{ data: OcSession }>(`${OC}/api/session`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    return r.data;
  },

  async getSession(sessionId: string): Promise<OcSession> {
    const r = await apiCall<{ data: OcSession }>(`${OC}/api/session/${sessionId}`);
    return r.data;
  },

  async getMessages(sessionId: string): Promise<Array<{ info: any; parts: any[] }>> {
    // v1.18.16: the v2 route (/api/session/{id}/message) returns an empty
    // {data:[],cursor} envelope for these sessions — the real messages live
    // on the legacy surface, as a bare array of { info, parts } records.
    const r = await apiCall<any[]>(`${OC}/session/${sessionId}/message`);
    return Array.isArray(r) ? r : [];
  },

  /**
   * Send a prompt via POST /session/{id}/prompt_async (async variant).
   * Payload shape (verified against the running opencode server's OpenAPI doc):
   *   { parts: TextPartInput | FilePartInput | AgentPartInput[] }
   * - text:  { type: "text", text }
   * - file:  { type: "file", mime, url, filename? }  — `mime` is REQUIRED and
   *   decides how opencode handles the attachment: only "text/plain" (inlined
   *   via the Read tool) and image/* (base64 media part) are accepted by
   *   OpenAI-Chat providers; anything else (e.g. text/markdown for .md) is
   *   rejected with "does not support media type", so text-ish files must be
   *   sent as text/plain and images with their real mime. `url` accepts both
   *   file:// paths (container workspace) and data: URLs — the browser-side
   *   base64 encoding the chat attachments use for images, so nothing has to
   *   be uploaded into the container first.
   * - agent: { type: "agent", name }  (@-mentions of subagents)
   *
   * Unlike the synchronous POST /session/{id}/message (which blocks until the
   * whole agent run finishes and is therefore prone to proxy/browser timeouts
   * on long tasks), prompt_async forks the run and returns 204 immediately —
   * this is exactly what the official opencode Web UI does. Message content
   * is delivered afterwards via the SSE `session.next.prompted` event, which
   * our reconcile logic in oc/messages.ts already handles.
   */
  async sendPrompt(
    sessionId: string,
    text: string,
    opts?: {
      files?: { mime: string; url: string; filename?: string }[];
      agents?: string[];
      agent?: string;
      model?: ModelRef;
    }
  ): Promise<any> {
    const parts: any[] = [];
    for (const f of opts?.files ?? []) {
      parts.push({ type: "file", mime: f.mime, url: f.url, ...(f.filename ? { filename: f.filename } : {}) });
    }
    for (const name of opts?.agents ?? []) {
      parts.push({ type: "agent", name });
    }
    parts.push({ type: "text", text: text || (opts?.files?.length ? "(attachments)" : "") });
    return apiCall(`${OC}/session/${sessionId}/prompt_async`, {
      method: "POST",
      body: JSON.stringify({
        parts,
        ...(opts?.agent ? { agent: opts.agent } : {}),
        ...(opts?.model
          ? { model: { providerID: opts.model.providerID, modelID: opts.model.id } }
          : {}),
      }),
    });
  },

  async interruptSession(sessionId: string): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/interrupt`, { method: "POST" });
  },

  /**
   * File diffs of one completed round (P1-1). Legacy surface, bare array —
   * same per-file shape the user message's `info.summary.diffs` carries.
   */
  async getSessionDiff(sessionId: string, messageID: string): Promise<FileDiff[]> {
    const r = await apiCall<any[]>(`${OC}/session/${sessionId}/diff?messageID=${encodeURIComponent(messageID)}`);
    return Array.isArray(r) ? r : [];
  },

  /** Revert the file changes a round produced (P1-1, legacy surface). */
  async revertSession(sessionId: string, messageID: string): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/revert`, {
      method: "POST",
      body: JSON.stringify({ messageID }),
    });
  },

  /** Restore the changes of the last revert (P1-1, legacy surface). */
  async unrevertSession(sessionId: string): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/unrevert`, { method: "POST" });
  },

  /** Fork a session at (or before) a message into a new session (P1-1). */
  async forkSession(sessionId: string, messageID?: string): Promise<OcSession> {
    return apiCall(`${OC}/session/${sessionId}/fork`, {
      method: "POST",
      body: JSON.stringify(messageID ? { messageID } : {}),
    });
  },

  /** Ask the model to summarize the session (P1-1, compaction summary). */
  async summarizeSession(sessionId: string, model: ModelRef): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/summarize`, {
      method: "POST",
      body: JSON.stringify({ providerID: model.providerID, modelID: model.id }),
    });
  },

  /** Switch the model for an existing session (opencode ModelRef payload). */
  async setSessionModel(sessionId: string, model: ModelRef): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/model`, {
      method: "POST",
      body: JSON.stringify({ model }),
    });
  },

  async setSessionAgent(sessionId: string, agent: string): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/agent`, {
      method: "POST",
      body: JSON.stringify({ agent }),
    });
  },

  /**
   * Rename a session. v2 has no update route, so this uses the legacy
   * PATCH /session/{id}, which returns the bare legacy Session object.
   */
  async renameSession(sessionId: string, title: string): Promise<OcSession> {
    return apiCall<OcSession>(`${OC}/session/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },

  /** Delete a session (legacy DELETE /session/{id}, returns bare true). */
  async deleteSession(sessionId: string): Promise<boolean> {
    return apiCall<boolean>(`${OC}/session/${sessionId}`, { method: "DELETE" });
  },

  /** All models across providers (GET /api/model). */
  async listModels(): Promise<OcModel[]> {
    const r = await apiCall<OcEnvelope<OcModel>>(`${OC}/api/model`);
    return r.data ?? [];
  },

  /** Pending permission requests (GET /api/permission/request). */
  async listPermissionRequests(): Promise<OcPermissionRequest[]> {
    const r = await apiCall<OcEnvelope<OcPermissionRequest>>(`${OC}/api/permission/request`);
    return r.data ?? [];
  },

  /** Answer a permission request — 204 on success. */
  async replyPermission(
    sessionId: string,
    requestId: string,
    reply: OcPermissionReply,
    message?: string
  ): Promise<void> {
    return apiCall(`${OC}/api/session/${sessionId}/permission/${requestId}/reply`, {
      method: "POST",
      body: JSON.stringify(message ? { reply, message } : { reply }),
    });
  },

  /** Pending question requests (GET /api/question/request). */
  async listQuestionRequests(): Promise<OcQuestionRequest[]> {
    const r = await apiCall<OcEnvelope<OcQuestionRequest>>(`${OC}/api/question/request`);
    return r.data ?? [];
  },

  /**
   * Answer questions. `answers` aligns with `questions` in order; each entry
   * is an array of selected option labels. 204 on success.
   */
  async replyQuestion(
    sessionId: string,
    requestId: string,
    answers: string[][]
  ): Promise<void> {
    return apiCall(`${OC}/api/session/${sessionId}/question/${requestId}/reply`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    });
  },

  /** Dismiss a question request — 204 on success. */
  async rejectQuestion(sessionId: string, requestId: string): Promise<void> {
    return apiCall(`${OC}/api/session/${sessionId}/question/${requestId}/reject`, {
      method: "POST",
    });
  },

  /**
   * Agent presets opencode exposes (build / plan / general / ... plus plugin
   * agents). NOTE: v2 GET /api/agent only lists native agents on opencode
   * 1.18.x — agents registered by plugins (oh-my-opencode-slim's
   * orchestrator / designer / oracle / ...) are absent from it. The legacy
   * GET /agent returns the full registry (native + plugin); it uses "name"
   * instead of "id" and returns a bare array.
   */
  async listAgents(): Promise<OcAgent[]> {
    const r = await apiCall<any[]>(`${OC}/agent`);
    return (Array.isArray(r) ? r : [])
      .filter((a) => !a.hidden)
      .map((a) => ({ ...a, id: a.name }));
  },

  /**
   * Global slash commands (GET /command). Note: this route lives outside the
   * /api prefix, like /find/file. Includes commands registered by plugins;
   * returns a bare array (no envelope).
   */
  async listCommands(): Promise<OcCommand[]> {
    const r = await apiCall<OcCommand[]>(`${OC}/command`);
    return Array.isArray(r) ? r : [];
  },

  /**
   * Run a slash command in a session. Payload verified against the running
   * opencode server: both `command` and `arguments` are required strings
   * (pass "" when the command takes no arguments). Returns { info: Message }.
   */
  async runCommand(sessionId: string, command: string, args: string): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/command`, {
      method: "POST",
      body: JSON.stringify({ command, arguments: args }),
    });
  },

  /** Raw escape hatch onto any opencode route not wrapped above. */
  async oc<T>(path: string, options: RequestInit = {}): Promise<T> {
    return apiCall<T>(`${OC}${path.startsWith("/") ? path : `/${path}`}`, options);
  },

  // --- User-scoped LLM providers + active selection (/api/user-config) ---
  async listUserLlmProviders(): Promise<{ providers: UserLlmProvider[] }> {
    return apiCall("/user-config/llm");
  },

  async createUserLlmProvider(payload: UserLlmProviderInput): Promise<UserLlmProvider> {
    return apiCall("/user-config/llm", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async updateUserLlmProvider(id: string, payload: Partial<UserLlmProviderInput>): Promise<UserLlmProvider> {
    return apiCall(`/user-config/llm/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async deleteUserLlmProvider(id: string): Promise<void> {
    return apiCall(`/user-config/llm/${id}`, { method: "DELETE" });
  },

  async getActiveLlm(): Promise<ActiveLlm> {
    return apiCall("/user-config/active-llm");
  },

  async setActiveLlm(providerId: string | null, model?: string | null): Promise<ActiveLlm> {
    return apiCall("/user-config/active-llm", {
      method: "PUT",
      body: JSON.stringify({ provider_id: providerId, model: model ?? null }),
    });
  },

  // --- Config management (host opencode.json) ---------------------------
  async getConfigOverview(): Promise<{
    providers: Record<string, any>;
    mcp: Record<string, any>;
    skills: { name: string; description: string; dir: string }[];
  }> {
    return apiCall("/config");
  },

  async listProvidersConfig(): Promise<{ providers: Record<string, any> }> {
    return apiCall("/config/providers");
  },

  async upsertProvider(providerId: string, config: any): Promise<any> {
    return apiCall(`/config/providers/${providerId}`, {
      method: "POST",
      body: JSON.stringify(config),
    });
  },

  async deleteProvider(providerId: string): Promise<any> {
    return apiCall(`/config/providers/${providerId}`, { method: "DELETE" });
  },

  async listMcp(): Promise<{ mcp: Record<string, any> }> {
    return apiCall("/config/mcp");
  },

  async upsertMcp(name: string, config: any): Promise<any> {
    return apiCall(`/config/mcp/${name}`, {
      method: "POST",
      body: JSON.stringify(config),
    });
  },

  async toggleMcp(name: string, enabled: boolean): Promise<any> {
    return apiCall(`/config/mcp/${name}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
  },

  async deleteMcp(name: string): Promise<any> {
    return apiCall(`/config/mcp/${name}`, { method: "DELETE" });
  },

  async listSkills(): Promise<{ skills: { name: string; description: string; dir: string }[] }> {
    return apiCall("/config/skills");
  },

  async getSkill(name: string): Promise<{ name: string; description: string; content: string; dir: string }> {
    return apiCall(`/config/skills/${name}`);
  },

  async upsertSkill(name: string, content: string): Promise<any> {
    return apiCall(`/config/skills/${name}`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },

  async deleteSkill(name: string): Promise<any> {
    return apiCall(`/config/skills/${name}`, { method: "DELETE" });
  },

  async reloadConfigIntoContainer(): Promise<{ reloaded: boolean; message: string }> {
    return apiCall("/config/reload", { method: "POST" });
  },

  // --- Workspace (project-scope) config & skills -----------------------
  async getProjectConfig(): Promise<{
    scope: string;
    exists: boolean;
    created: boolean;
    valid: boolean;
    content: string;
    config: Record<string, any>;
  }> {
    return apiCall("/workspace/config");
  },

  async saveProjectConfig(content: string): Promise<{ status: string; message: string }> {
    return apiCall("/workspace/config", {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
  },

  async listProjectSkills(): Promise<{ skills: { name: string; description: string; dir: string; scope: string }[] }> {
    return apiCall("/workspace/skills");
  },

  async getProjectSkill(name: string): Promise<{ name: string; description: string; content: string; dir: string; scope: string }> {
    return apiCall(`/workspace/skills/${name}`);
  },

  async upsertProjectSkill(name: string, content: string): Promise<any> {
    return apiCall(`/workspace/skills/${name}`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },

  async deleteProjectSkill(name: string): Promise<any> {
    return apiCall(`/workspace/skills/${name}`, { method: "DELETE" });
  },

  async importSkillsZip(file: File): Promise<{ status: string; imported: { name: string; description: string; dir: string; scope: string; fileCount: number }[]; message: string }> {
    const form = new FormData();
    form.append("file", file);
    return apiCall("/workspace/skills/import", {
      method: "POST",
      body: form,
    });
  },

  // --- Chat attach: skill picker + file upload --------------------------
  async listAllSkills(): Promise<{ skills: { name: string; description: string; dir: string; scope: string }[] }> {
    return apiCall("/workspace/skills/all");
  },

  /**
   * opencode 原生 skill 列表（v1 面 GET /skill，返回裸数组）。每项含
   * name / description? / location（容器内绝对路径）/ content，是
   * opencode 自己注册的权威来源；平台侧 /workspace/skills/all 仅作回退。
   */
  async listNativeSkills(): Promise<
    { name: string; description?: string; location: string; content: string }[]
  > {
    const r = await apiCall<any[]>(`${OC}/skill`);
    return Array.isArray(r) ? r : [];
  },

  async uploadChatFile(file: File): Promise<{
    status: string;
    path: string;
    absPath: string;
    filename: string;
    size: number;
    mime: string;
    isImage: boolean;
  }> {
    const form = new FormData();
    form.append("file", file);
    return apiCall("/workspace/files/upload", {
      method: "POST",
      body: form,
    });
  },

  // --- Workspace file browser -------------------------------------------
  async listWorkspaceFiles(): Promise<{ files: { path: string; type: "file" | "dir"; size: number }[] }> {
    return apiCall("/workspace/files");
  },

  async readWorkspaceFile(path: string): Promise<{
    type: "text" | "image" | "binary";
    mime: string;
    content?: string;
    base64?: string;
    size?: number;
  }> {
    return apiCall(`/workspace/file-content?path=${encodeURIComponent(path)}`);
  },

  /**
   * Fuzzy file search backed by opencode's own /find/file endpoint (the same
   * index the @-mention picker uses internally). Returns workspace-relative
   * paths. Note: this route lives outside the /api prefix.
   */
  async findFiles(query: string, limit = 20): Promise<string[]> {
    return apiCall(
      `${OC}/find/file?query=${encodeURIComponent(query)}&limit=${limit}&type=file`
    );
  },

  // --- SSE --------------------------------------------------------------
  // P1-5: the long-lived JWT never goes in the SSE URL anymore (it leaks via
  // logs/history). Swap it for a one-time, 60s ticket first. Because the
  // ticket is single-use, the browser's native EventSource auto-reconnect
  // can't replay it — Chat.tsx drives reconnection manually with fresh
  // tickets instead.
  async createEventSource(lastEventId: number = 0): Promise<EventSource> {
    const { ticket } = await apiCall<{ ticket: string }>("/tunnel/ticket", {
      method: "POST",
    });
    const url = `${API_BASE}/tunnel/events?ticket=${encodeURIComponent(
      ticket
    )}&lastEventId=${lastEventId}`;
    return new EventSource(url);
  },
};
