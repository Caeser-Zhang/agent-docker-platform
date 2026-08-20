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
 *   GET  /api/session/{id}/message      { data: SessionMessage[], cursor }
 *   GET  /api/model                     { location, data: ModelV2Info[] }
 *   GET  /api/agent                     { location, data: AgentV2Info[] }
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

const API_BASE = "/api";
/** Prefix that reverse-proxies straight into the container's opencode server. */
const OC = "/tunnel/oc";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  username: string;
}

export interface AgentStatus {
  running: boolean;
  healthy: boolean;
  status: string;
  container_name: string | null;
  workspace: string | null;
  message: string;
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
  steps?: number;
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

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem("token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiCall<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...getAuthHeaders(),
      ...options.headers,
    },
  });
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

  async listContainers(): Promise<{ containers: any[] }> {
    return apiCall("/agent/containers");
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

  async getMessages(sessionId: string): Promise<any[]> {
    const r = await apiCall<OcPage<any>>(`${OC}/api/session/${sessionId}/message`);
    return r.data ?? [];
  },

  /**
   * Send a prompt. Returns as soon as opencode admits the message; the actual
   * answer arrives as `session.next.text.delta` events on the SSE stream.
   * Payload shape (verified against the running opencode server):
   *   { prompt: { text: "...", parts?: [ {type:"text"|"file", ...} ] } }
   * `prompt.text` is required; extra `parts` attach files by container path.
   */
  async sendPrompt(sessionId: string, text: string, parts?: any[]): Promise<any> {
    const prompt: any = { text: text || (parts && parts.length ? "(attachments)" : "") };
    if (parts && parts.length > 0) prompt.parts = parts;
    return apiCall(`${OC}/api/session/${sessionId}/prompt`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    });
  },

  async interruptSession(sessionId: string): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/interrupt`, { method: "POST" });
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

  /** Agent presets opencode exposes (build / plan / general / ...). */
  async listAgents(): Promise<OcAgent[]> {
    const r = await apiCall<OcEnvelope<OcAgent>>(`${OC}/api/agent`);
    return (r.data ?? []).filter((a) => !a.hidden);
  },

  /** Raw escape hatch onto any opencode route not wrapped above. */
  async oc<T>(path: string, options: RequestInit = {}): Promise<T> {
    return apiCall<T>(`${OC}${path.startsWith("/") ? path : `/${path}`}`, options);
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
  createEventSource(lastEventId: number = 0): EventSource {
    const token = localStorage.getItem("token");
    const url = `${API_BASE}/tunnel/events?token=${encodeURIComponent(
      token || ""
    )}&lastEventId=${lastEventId}`;
    return new EventSource(url);
  },
};
