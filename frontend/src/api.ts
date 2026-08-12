/** API client for the platform control layer.
 *
 * Two kinds of call live here:
 *   - platform calls  (/api/auth, /api/agent)  — login and container lifecycle
 *   - tunnelled calls (/api/tunnel/oc/...)     — forwarded verbatim to
 *     `opencode serve` inside the user's container
 *
 * Everything agent-related uses opencode's own routes and payload shapes,
 * verified against opencode 1.18.16's OpenAPI document:
 *   POST /api/session                      { agent?, model?, location? }
 *   POST /api/session/{id}/prompt          { prompt: { text } }
 *   POST /api/session/{id}/model           { model: { providerID, id } }
 *   GET  /api/session/{id}/message         { data: SessionMessage[] }
 *   GET  /config                           effective merged config
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

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem("token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiCall<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
      ...options.headers,
    },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
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
    const r = await apiCall<{ data: OcSession[] }>(`${OC}/api/session`);
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
    const r = await apiCall<{ data: any[] }>(`${OC}/api/session/${sessionId}/message`);
    return r.data ?? [];
  },

  /**
   * Send a prompt. Returns as soon as opencode admits the message; the actual
   * answer arrives as `session.next.text.delta` events on the SSE stream.
   */
  async sendPrompt(sessionId: string, text: string): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/prompt`, {
      method: "POST",
      body: JSON.stringify({ prompt: { text } }),
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

  /** Agent presets opencode exposes (build / plan / general / ...). */
  async listAgents(): Promise<{ id: string; description?: string; mode?: string }[]> {
    const r = await apiCall<{ data: any[] }>(`${OC}/api/agent`);
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

  // --- SSE --------------------------------------------------------------
  createEventSource(lastEventId: number = 0): EventSource {
    const token = localStorage.getItem("token");
    const url = `${API_BASE}/tunnel/events?token=${encodeURIComponent(
      token || ""
    )}&lastEventId=${lastEventId}`;
    return new EventSource(url);
  },
};
