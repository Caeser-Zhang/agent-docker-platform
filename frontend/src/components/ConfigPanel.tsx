import { useState, useEffect, useCallback, useRef } from "react";
import { api, type ActiveLlm, type UserLlmProvider, type UserLlmProviderInput } from "../api";
import { styles } from "./chatStyles";

type Scope = "global" | "project" | "user";

export function ConfigPanel({ onClose }: { onClose: () => void }) {
  const [scope, setScope] = useState<Scope>("global");
  const [tab, setTab] = useState<"providers" | "mcp" | "skills" | "config" | "userLlm">("providers");
  const [overview, setOverview] = useState<any>(null);
  const [projectConfig, setProjectConfig] = useState<{ content: string; valid: boolean; config: Record<string, any> } | null>(null);
  const [projectSkills, setProjectSkills] = useState<{ name: string; description: string; dir: string; scope: string }[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const loadAll = useCallback(async () => {
    try {
      const [ov, pc] = await Promise.all([
        api.getConfigOverview(),
        api.getProjectConfig().catch(() => null),
      ]);
      setOverview(ov);
      setProjectConfig(pc ? { content: pc.content, valid: pc.valid, config: pc.config } : null);
      if (pc?.exists) {
        const ps = await api.listProjectSkills().catch(() => ({ skills: [] }));
        setProjectSkills(ps.skills || []);
      } else {
        setProjectSkills([]);
      }
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  const handleReload = async () => {
    setBusy("重新加载配置到容器…");
    setError("");
    try {
      const r = await api.reloadConfigIntoContainer();
      if (!r.reloaded) setError(r.message);
      else await loadAll();
    } catch (e: any) {
      setError(e.message);
    } finally { setBusy(""); }
  };

  const globalSkills = overview?.skills || [];
  const allSkills = [
    ...globalSkills.map((s: any) => ({ ...s, scope: "global" as const })),
    ...projectSkills.map((s: any) => ({ ...s, scope: "project" as const })),
  ];

  return (
    <div style={styles.modal} onClick={onClose}>
      <div style={{ ...styles.modalContent, width: "90%", maxWidth: 720, maxHeight: "85vh" }} onClick={(e) => e.stopPropagation()}>
        <div style={styles.modalHeader}>
          <span>配置管理</span>
          <button style={styles.modalClose} onClick={onClose}>×</button>
        </div>
        <div style={{ ...styles.modalBody, padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
          {error && <div style={styles.errorBanner}><span>{error}</span><button onClick={() => setError("")}>×</button></div>}

          {/* Scope selector */}
          <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #e0e0e0", padding: "0 16px" }}>
            {(["global", "project", "user"] as const).map((s) => (
              <button
                key={s}
                onClick={() => {
                  setScope(s);
                  setTab(s === "project" ? "skills" : s === "user" ? "userLlm" : "providers");
                }}
                style={{
                  padding: "10px 20px", border: "none", cursor: "pointer", fontSize: 13,
                  fontWeight: scope === s ? 600 : 400,
                  background: scope === s
                    ? s === "global" ? "#fef9f0" : s === "project" ? "#f0f7ff" : "#f5f3ff"
                    : "transparent",
                  borderBottom: scope === s
                    ? `2px solid ${s === "global" ? "#f59e0b" : s === "project" ? "#378ADD" : "#8b5cf6"}`
                    : "2px solid transparent",
                }}
              >
                {s === "global" ? "全局配置" : s === "project" ? "项目配置" : "用户 LLM"}
                {s === "project" && projectConfig && " ✓"}
              </button>
            ))}
          </div>

          {/* Tab bar */}
          {scope === "global" && (
            <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #e0e0e0" }}>
              {(["providers", "mcp", "skills"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  style={{
                    padding: "8px 16px", border: "none", cursor: "pointer", fontSize: 12,
                    fontWeight: tab === t ? 600 : 400,
                    background: tab === t ? "#f0f4ff" : "transparent",
                    borderBottom: tab === t ? "2px solid #378ADD" : "2px solid transparent",
                  }}
                >
                  {t === "providers" ? "LLM Provider" : t === "mcp" ? "MCP 服务" : "Skills"}
                  {t === "skills" ? ` (${allSkills.length})` : ` (${overview?.[t] ? Object.keys(overview[t]).length : 0})`}
                </button>
              ))}
            </div>
          )}

          {scope === "project" && (
            <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #e0e0e0" }}>
              {(["config", "skills"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t as any)}
                  style={{
                    padding: "8px 16px", border: "none", cursor: "pointer", fontSize: 12,
                    fontWeight: tab === (t as any) ? 600 : 400,
                    background: tab === (t as any) ? "#e0f2fe" : "transparent",
                    borderBottom: tab === (t as any) ? "2px solid #0369a1" : "2px solid transparent",
                  }}
                >
                  {t === "config" ? "opencode.json" : "Skills"}
                  {(t === "skills") && ` (${projectSkills.length})`}
                </button>
              ))}
            </div>
          )}

          <div style={{ flex: 1, overflow: "auto", padding: "16px" }}>
            {scope === "global" && tab === "providers" && <ProviderTab overview={overview} onChange={loadAll} />}
            {scope === "global" && tab === "mcp" && <McpTab overview={overview} onChange={loadAll} onReload={handleReload} />}
            {scope === "global" && tab === "skills" && <SkillTab skills={allSkills} onChange={loadAll} scope="global" />}
            {scope === "project" && tab === "config" && (
              <ProjectConfigTab config={projectConfig} onChange={loadAll} />
            )}
            {scope === "project" && tab === "skills" && (
              <SkillTab skills={projectSkills.map((s: any) => ({ ...s, scope: "project" }))} onChange={loadAll} scope="project" />
            )}
            {scope === "user" && tab === "userLlm" && <UserLlmTab onReload={handleReload} />}
          </div>

          <div style={{ padding: "12px 16px", borderTop: "1px solid #e0e0e0", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 11, color: "#999" }}>
              {scope === "global" ? "全局" : scope === "project" ? "项目" : "用户"}配置 · 重启容器后生效
            </span>
            <button style={{ ...styles.reloadBtn, opacity: busy ? 0.5 : 1 }} onClick={handleReload} disabled={!!busy}>
              {busy || "重载到容器"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------
//  Project config JSON editor
// ------------------------------------------------------------------

function ProjectConfigTab({ config, onChange }: { config: { content: string; valid: boolean; config: Record<string, any> } | null; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [saveMsg, setSaveMsg] = useState("");

  useEffect(() => {
    if (config) setText(config.content);
    else setText('{\n  "$schema": "https://opencode.ai/config.json"\n}\n');
  }, [config]);

  const handleSave = async () => {
    try {
      const r = await api.saveProjectConfig(text);
      setSaveMsg(r.message);
      setEditing(false);
      onChange();
    } catch (e: any) {
      setSaveMsg("保存失败: " + e.message);
    }
  };

  const handleCancel = () => {
    setText(config?.content || "");
    setEditing(false);
    setSaveMsg("");
  };

  if (!config) {
    return (
      <div style={{ textAlign: "center", padding: 40, color: "#999" }}>
        <p>Agent 容器尚未创建，无法加载项目级配置</p>
        <p style={{ fontSize: 12 }}>请先启动 Agent 再管理项目配置</p>
      </div>
    );
  }

  if (!editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <strong>/workspace/opencode.json</strong>
            {scopeBadge("project")}
          </div>
          <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => { setEditing(true); setText(config.content); }}>
            编辑
          </button>
        </div>
        {saveMsg && <div style={{ padding: "8px 12px", borderRadius: 6, background: "#f0fdf4", color: "#166534", fontSize: 12 }}>{saveMsg}</div>}
        <pre style={{
          margin: 0, padding: 12, background: "#f8f9fa", borderRadius: 8, fontSize: 12,
          overflow: "auto", maxHeight: 400, border: "1px solid #e0e0e0",
          whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>
          {config.content}
        </pre>
        {config.config && Object.keys(config.config).length > 0 && (
          <div style={{ fontSize: 12, color: "#666" }}>
            当前配置项: {Object.keys(config.config).join(", ")}
          </div>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <h3 style={{ margin: 0 }}>编辑项目 opencode.json</h3>
      <textarea
        style={{ ...styles.textInput, height: 350, fontFamily: "monospace", fontSize: 12 }}
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
      />
      <div style={{ display: "flex", gap: 8 }}>
        <button style={styles.sendBtn} onClick={handleSave}>保存</button>
        <button style={styles.abortBtn} onClick={handleCancel}>取消</button>
      </div>
      {saveMsg && <div style={{ padding: "8px 12px", borderRadius: 6, background: "#f0fdf4", color: "#166534", fontSize: 12 }}>{saveMsg}</div>}
    </div>
  );
}

// ------------------------------------------------------------------
//  Provider tab (global only)
// ------------------------------------------------------------------

function ProviderTab({ overview, onChange }: { overview: any; onChange: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState({ id: "", name: "", baseURL: "", apiKey: "", npm: "@ai-sdk/openai-compatible" });

  const providers = overview?.providers || {};

  const startEdit = (id: string, data?: any) => {
    setEditing(id);
    setForm({
      id,
      name: data?.name || id,
      baseURL: data?.options?.baseURL || "",
      apiKey: "",
      npm: data?.npm || "@ai-sdk/openai-compatible",
    });
  };

  const handleSave = async () => {
    const opts: Record<string, string> = { baseURL: form.baseURL };
    if (form.apiKey) opts.apiKey = form.apiKey;
    try {
      await api.upsertProvider(form.id, { name: form.name, npm: form.npm, options: opts });
      setEditing(null);
      onChange();
    } catch (e: any) {
      alert("保存失败: " + e.message);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm(`删除 provider "${id}"?`)) return;
    try {
      await api.deleteProvider(id);
      onChange();
    } catch (e: any) {
      alert("删除失败: " + e.message);
    }
  };

  if (editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <h3 style={{ margin: 0 }}>{editing === "__new" ? "新增" : "编辑"} Provider</h3>
        <Field label="Provider ID" value={form.id} onChange={(v) => setForm({ ...form, id: v })} disabled={editing !== "__new"} />
        <Field label="名称" value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
        <Field label="Base URL" value={form.baseURL} onChange={(v) => setForm({ ...form, baseURL: v })} />
        <Field label="API Key" value={form.apiKey} onChange={(v) => setForm({ ...form, apiKey: v })} placeholder="留空不修改" />
        <Field label="NPM 包" value={form.npm} onChange={(v) => setForm({ ...form, npm: v })} />
        <div style={{ display: "flex", gap: 8 }}>
          <button style={styles.sendBtn} onClick={handleSave}>保存</button>
          <button style={styles.abortBtn} onClick={() => setEditing(null)}>取消</button>
        </div>
      </div>
    );
  }

  return (
    <div>
      {Object.entries(providers).map(([id, data]: [string, any]) => (
        <div key={id} style={{ padding: "12px", marginBottom: 8, border: "1px solid #e0e0e0", borderRadius: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{data.name || id}</strong>
              <span style={{ marginLeft: 8, fontSize: 12, color: "#888" }}>{id}</span>
              {scopeBadge("global")}
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => startEdit(id, data)}>编辑</button>
              <button style={{ ...styles.abortBtn, fontSize: 12 }} onClick={() => handleDelete(id)}>删除</button>
            </div>
          </div>
          <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>
            {data.options?.baseURL || "(no baseURL)"} · API Key: {data.options?.hasApiKey ? "✓" : "✗"}
          </div>
        </div>
      ))}
      <button style={{ ...styles.newSessionLargeBtn, marginTop: 12 }} onClick={() => startEdit("__new", {})}>
        + 新增 Provider
      </button>
    </div>
  );
}

// ------------------------------------------------------------------
//  MCP tab (global only)
// ------------------------------------------------------------------

function McpTab({ overview, onChange, onReload }: { overview: any; onChange: () => void; onReload: () => Promise<void> }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", type: "remote" as "remote" | "local", url: "", command: "", enabled: true });

  const mcp = overview?.mcp || {};

  const startEdit = (name: string, data?: any) => {
    setEditing(name);
    setForm({
      name,
      type: data?.type || "remote",
      url: data?.url || "",
      command: Array.isArray(data?.command) ? data.command.join(" ") : "",
      enabled: data?.enabled ?? true,
    });
  };

  const handleSave = async () => {
    const cfg: any = { type: form.type, enabled: form.enabled };
    if (form.type === "remote") cfg.url = form.url;
    else cfg.command = form.command.split(/\s+/).filter(Boolean);
    try {
      await api.upsertMcp(form.name, cfg);
      setEditing(null);
      onChange();
    } catch (e: any) {
      alert("保存失败: " + e.message);
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`删除 MCP "${name}"?`)) return;
    try {
      await api.deleteMcp(name);
      onChange();
    } catch (e: any) {
      alert("删除失败: " + e.message);
    }
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    try {
      await api.toggleMcp(name, enabled);
      onChange();
      await onReload();
    } catch (e: any) {
      alert("切换失败: " + e.message);
    }
  };

  if (editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <h3 style={{ margin: 0 }}>{editing === "__new" ? "新增" : "编辑"} MCP Server</h3>
        <Field label="名称" value={form.name} onChange={(v) => setForm({ ...form, name: v })} disabled={editing !== "__new"} />
        <div>
          <label style={{ fontSize: 12, color: "#666" }}>类型</label>
          <select style={{ ...styles.select, width: "100%" }} value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value as any })}>
            <option value="remote">Remote (URL)</option>
            <option value="local">Local (Command)</option>
          </select>
        </div>
        {form.type === "remote" ? (
          <Field label="URL" value={form.url} onChange={(v) => setForm({ ...form, url: v })} placeholder="https://mcp.example.com/mcp" />
        ) : (
          <Field label="Command" value={form.command} onChange={(v) => setForm({ ...form, command: v })} placeholder="npx -y @mcp/server" />
        )}
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
          <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          启用
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={styles.sendBtn} onClick={handleSave}>保存</button>
          <button style={styles.abortBtn} onClick={() => setEditing(null)}>取消</button>
        </div>
        {form.type === "local" && (
          <p style={{ fontSize: 12, color: "#999" }}>注意: Local 类型的 MCP 不会被注入容器（无法访问宿主机可执行文件）</p>
        )}
      </div>
    );
  }

  return (
    <div>
      {Object.entries(mcp).map(([name, data]: [string, any]) => (
        <div key={name} style={{ padding: "12px", marginBottom: 8, border: "1px solid #e0e0e0", borderRadius: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{name}</strong>
              <span style={{ marginLeft: 8, fontSize: 12, padding: "2px 6px", borderRadius: 4, background: data.type === "remote" ? "#e6f1fb" : "#e1f5ee" }}>
                {data.type}
              </span>
              {data.builtin ? (
                <span style={{ marginLeft: 6, padding: "1px 6px", borderRadius: 4, fontSize: 11, background: "#ede9fe", color: "#6d28d9", fontWeight: 500 }}>
                  内置
                </span>
              ) : (
                <span style={{ marginLeft: 6, padding: "1px 6px", borderRadius: 4, fontSize: 11, background: "#e0f2fe", color: "#0369a1", fontWeight: 500 }}>
                  用户添加
                </span>
              )}
              {scopeBadge("global")}
            </div>
            <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={!!data.enabled} onChange={(e) => handleToggle(name, e.target.checked)} />
              {!data.builtin && (
                <>
                  <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => startEdit(name, data)}>编辑</button>
                  <button style={{ ...styles.abortBtn, fontSize: 12 }} onClick={() => handleDelete(name)}>删除</button>
                </>
              )}
            </div>
          </div>
          <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>
            {data.type === "remote" ? data.url : data.command?.join(" ")}
          </div>
        </div>
      ))}
      <button style={{ ...styles.newSessionLargeBtn, marginTop: 12 }} onClick={() => startEdit("__new", {})}>
        + 新增 MCP Server
      </button>
    </div>
  );
}

// ------------------------------------------------------------------
//  User LLM tab (user scope): DB-backed providers + active selection
// ------------------------------------------------------------------

function UserLlmTab({ onReload }: { onReload: () => Promise<void> }) {
  const [providers, setProviders] = useState<UserLlmProvider[]>([]);
  const [active, setActive] = useState<ActiveLlm>({ provider_id: null, model: null });
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState({ provider_id: "", name: "", base_url: "", api_key: "", npm: "@ai-sdk/openai-compatible", models: "" });
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const load = useCallback(async () => {
    const [p, a] = await Promise.all([api.listUserLlmProviders(), api.getActiveLlm()]);
    setProviders(p.providers);
    setActive(a);
  }, []);

  useEffect(() => {
    load().catch((e: any) => setMsg({ text: e.message, ok: false }));
  }, [load]);

  const activeProvider = providers.find((p) => p.provider_id === active.provider_id);
  const activeModels = activeProvider ? Object.keys(activeProvider.models || {}) : [];

  const flash = (text: string, ok: boolean) => setMsg({ text, ok });

  const handleSetActiveProvider = async (providerId: string) => {
    setBusy("激活中…");
    try {
      await api.setActiveLlm(providerId, null);
      await load();
      await onReload();
      flash(`已激活 "${providerId}"（使用默认模型）`, true);
    } catch (e: any) {
      flash(e.message, false);
    } finally { setBusy(""); }
  };

  const handleSetActiveModel = async (model: string | null) => {
    if (!active.provider_id) return;
    setBusy("设置模型…");
    try {
      await api.setActiveLlm(active.provider_id, model);
      await load();
      await onReload();
      flash(model ? `已选择模型 "${model}"` : "已切换为默认模型", true);
    } catch (e: any) {
      flash(e.message, false);
    } finally { setBusy(""); }
  };

  const handleClearActive = async () => {
    setBusy("清除中…");
    try {
      await api.setActiveLlm(null);
      await load();
      await onReload();
      flash("已清除激活 LLM", true);
    } catch (e: any) {
      flash(e.message, false);
    } finally { setBusy(""); }
  };

  const startEdit = (id: string, data?: UserLlmProvider) => {
    setEditing(id);
    setForm({
      provider_id: data?.provider_id || "",
      name: data?.name || "",
      base_url: data?.baseURL || "",
      api_key: "",
      npm: data?.npm || "@ai-sdk/openai-compatible",
      models: data?.models ? JSON.stringify(data.models, null, 2) : "",
    });
  };

  const handleSave = async () => {
    let models: Record<string, any> | null = null;
    if (form.models.trim()) {
      try {
        models = JSON.parse(form.models);
      } catch {
        flash("models 不是合法 JSON", false);
        return;
      }
    }
    const payload: UserLlmProviderInput = {
      provider_id: form.provider_id,
      name: form.name || null,
      npm: form.npm,
      base_url: form.base_url || null,
      api_key: form.api_key || null,
      models,
    };
    try {
      if (editing === "__new") {
        await api.createUserLlmProvider(payload);
      } else if (editing) {
        await api.updateUserLlmProvider(editing, payload);
      }
      setEditing(null);
      await load();
      flash("已保存", true);
    } catch (e: any) {
      flash(e.message, false);
    }
  };

  const handleDelete = async (id: string, providerId: string) => {
    if (!confirm(`删除用户 LLM Provider "${providerId}"?`)) return;
    try {
      await api.deleteUserLlmProvider(id);
      await load();
      flash(`已删除 "${providerId}"`, true);
    } catch (e: any) {
      flash(e.message, false);
    }
  };

  if (editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <h3 style={{ margin: 0 }}>{editing === "__new" ? "新增" : "编辑"}用户 LLM Provider</h3>
        <Field label="Provider ID" value={form.provider_id} onChange={(v) => setForm({ ...form, provider_id: v })} disabled={editing !== "__new"} placeholder="如 my-openai" />
        <Field label="名称" value={form.name} onChange={(v) => setForm({ ...form, name: v })} placeholder="可选" />
        <Field label="Base URL" value={form.base_url} onChange={(v) => setForm({ ...form, base_url: v })} placeholder="https://api.openai.com/v1" />
        <Field label="API Key" value={form.api_key} onChange={(v) => setForm({ ...form, api_key: v })} placeholder="留空不修改" />
        <Field label="NPM 包" value={form.npm} onChange={(v) => setForm({ ...form, npm: v })} />
        <div>
          <label style={{ fontSize: 12, color: "#666" }}>Models (JSON, 可选)</label>
          <textarea
            style={{ ...styles.textInput, height: 120, fontFamily: "monospace", fontSize: 12 }}
            value={form.models}
            onChange={(e) => setForm({ ...form, models: e.target.value })}
            placeholder='{"gpt-4o": { "name": "GPT-4o" }}'
          />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={styles.sendBtn} onClick={handleSave}>保存</button>
          <button style={styles.abortBtn} onClick={() => setEditing(null)}>取消</button>
        </div>
      </div>
    );
  }

  return (
    <div>
      {msg && (
        <div style={{ padding: "8px 12px", borderRadius: 6, fontSize: 12, marginBottom: 12, background: msg.ok ? "#f0fdf4" : "#fef2f2", color: msg.ok ? "#166534" : "#991b1b" }}>
          {msg.text}
        </div>
      )}

      {/* Active selection */}
      <div style={{ padding: 12, marginBottom: 16, border: "1px solid #e9d5ff", borderRadius: 8, background: "#faf5ff" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <strong style={{ fontSize: 13 }}>激活 LLM</strong>
          {active.provider_id && (
            <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={handleClearActive} disabled={!!busy}>清除激活</button>
          )}
        </div>
        {active.provider_id ? (
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 13 }}>
              当前: <strong style={{ color: "#7c3aed" }}>{active.provider_id}</strong>
              {active.model ? ` · ${active.model}` : " · 默认模型"}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <select
                style={{ ...styles.select, flex: 1 }}
                value={active.model ?? ""}
                onChange={(e) => handleSetActiveModel(e.target.value === "" ? null : e.target.value)}
                disabled={!!busy || activeModels.length === 0}
              >
                <option value="">默认（自动）</option>
                {activeModels.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
              <span style={{ fontSize: 11, color: "#999" }}>{busy || "选择模型"}</span>
            </div>
          </div>
        ) : (
          <p style={{ margin: "8px 0 0", fontSize: 12, color: "#999" }}>尚未选择激活 LLM，点击下方 Provider 的「设为激活」按钮。</p>
        )}
      </div>

      {/* Provider list */}
      {providers.length === 0 && !busy && (
        <p style={{ textAlign: "center", color: "#999", padding: 20 }}>暂无用户 LLM Provider</p>
      )}

      {providers.map((p) => {
        const isActive = active.provider_id === p.provider_id;
        const modelIds = Object.keys(p.models || {});
        return (
          <div key={p.id} style={{ padding: 12, marginBottom: 8, border: isActive ? "2px solid #8b5cf6" : "1px solid #e0e0e0", borderRadius: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <strong>{p.name || p.provider_id}</strong>
                <span style={{ marginLeft: 8, fontSize: 12, color: "#888" }}>{p.provider_id}</span>
                {isActive && <span style={{ marginLeft: 6, padding: "1px 6px", borderRadius: 4, fontSize: 11, background: "#ede9fe", color: "#6d28d9", fontWeight: 500 }}>激活</span>}
              </div>
              <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                {!isActive && (
                  <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => handleSetActiveProvider(p.provider_id)} disabled={!!busy}>设为激活</button>
                )}
                <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => startEdit(p.id, p)}>编辑</button>
                <button style={{ ...styles.abortBtn, fontSize: 12 }} onClick={() => handleDelete(p.id, p.provider_id)}>删除</button>
              </div>
            </div>
            <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>
              {p.baseURL || "(no baseURL)"} · API Key: {p.hasApiKey ? "✓" : "✗"}
            </div>
            {modelIds.length > 0 && (
              <div style={{ fontSize: 11, color: "#999", marginTop: 2 }}>Models: {modelIds.join(", ")}</div>
            )}
          </div>
        );
      })}

      <button style={{ ...styles.newSessionLargeBtn, marginTop: 12 }} onClick={() => startEdit("__new")} disabled={!!busy}>
        + 新增用户 LLM Provider
      </button>
    </div>
  );
}

// ------------------------------------------------------------------
//  Skill tab (global + project, or project-only depending on scope)
// ------------------------------------------------------------------

function SkillTab({ skills, onChange, scope }: { skills: any[]; onChange: () => void; scope: "global" | "project" }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [editScope, setEditScope] = useState<"global" | "project">("global");
  const [form, setForm] = useState({ name: "", content: "" });
  const [importMsg, setImportMsg] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const startEdit = async (name: string, s: "global" | "project") => {
    setEditScope(s);
    if (name === "__new") {
      setForm({ name: "", content: "---\nname: \ndescription: \n---\n\n## What I do\n\n" });
    } else {
      try {
        const skill = s === "global" ? await api.getSkill(name) : await api.getProjectSkill(name);
        setForm({ name, content: skill.content });
      } catch (e: any) {
        setForm({ name, content: "---\nname: " + name + "\ndescription: \n---\n\n" });
      }
    }
    setEditing(name);
  };

  const handleSave = async () => {
    try {
      if (editScope === "global") {
        await api.upsertSkill(form.name, form.content);
      } else {
        await api.upsertProjectSkill(form.name, form.content);
      }
      setEditing(null);
      onChange();
    } catch (e: any) {
      alert("保存失败: " + e.message);
    }
  };

  const handleDelete = async (name: string, s: "global" | "project") => {
    if (!confirm(`删除 ${s === "global" ? "全局" : "项目"} Skill "${name}"?`)) return;
    try {
      if (s === "global") {
        await api.deleteSkill(name);
      } else {
        await api.deleteProjectSkill(name);
      }
      onChange();
    } catch (e: any) {
      alert("删除失败: " + e.message);
    }
  };

  const handleZipImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportMsg("正在导入…");
    try {
      const r = await api.importSkillsZip(file);
      setImportMsg(r.message);
      onChange();
    } catch (err: any) {
      setImportMsg("导入失败: " + err.message);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  if (editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <h3 style={{ margin: 0 }}>
          {editing === "__new" ? "新增" : "编辑"} Skill
          {scopeBadge(editScope)}
        </h3>
        {editing === "__new" && (
          <Field label="名称" value={form.name} onChange={(v) => setForm({ ...form, name: v })} placeholder="my-skill (lowercase, hyphens)" />
        )}
        <div>
          <label style={{ fontSize: 12, color: "#666" }}>SKILL.md 内容</label>
          <textarea
            style={{ ...styles.textInput, height: 300, fontFamily: "monospace", fontSize: 12 }}
            value={form.content}
            onChange={(e) => setForm({ ...form, content: e.target.value })}
          />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={styles.sendBtn} onClick={handleSave}>保存</button>
          <button style={styles.abortBtn} onClick={() => setEditing(null)}>取消</button>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Archive import — only for project scope */}
      {scope === "project" && (
        <div style={{ marginBottom: 16, padding: 12, border: "2px dashed #d0d5dd", borderRadius: 8, textAlign: "center" }}>
          <p style={{ margin: "0 0 8px", fontSize: 13, color: "#666" }}>导入 Skill 压缩包 (.zip/.rar/.7z/.tar.gz 等) 到项目级</p>
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip,.rar,.7z,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz"
            onChange={handleZipImport}
            style={{ fontSize: 12 }}
          />
          {importMsg && (
            <div style={{ marginTop: 8, padding: "4px 8px", borderRadius: 4, fontSize: 12, background: importMsg.includes("失败") ? "#fef2f2" : "#f0fdf4", color: importMsg.includes("失败") ? "#991b1b" : "#166534" }}>
              {importMsg}
            </div>
          )}
        </div>
      )}

      {skills.length === 0 && (
        <p style={{ textAlign: "center", color: "#999", padding: 20 }}>暂无 Skill</p>
      )}

      {skills.map((s: any) => (
        <div key={`${s.scope}-${s.dir}`} style={{ padding: "12px", marginBottom: 8, border: "1px solid #e0e0e0", borderRadius: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{s.name}</strong>
              {scopeBadge(s.scope)}
              <div style={{ fontSize: 12, color: "#666", marginTop: 2 }}>{s.description}</div>
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => startEdit(s.dir, s.scope)}>编辑</button>
              <button style={{ ...styles.abortBtn, fontSize: 12 }} onClick={() => handleDelete(s.dir, s.scope)}>删除</button>
            </div>
          </div>
        </div>
      ))}

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button style={{ ...styles.newSessionLargeBtn, flex: 1 }} onClick={() => { setEditScope("global"); startEdit("__new", "global"); }}>
          + 新增全局 Skill
        </button>
        <button style={{ ...styles.newSessionLargeBtn, flex: 1, background: "#e0f2fe", color: "#0369a1", borderColor: "#bae6fd" }} onClick={() => { setEditScope("project"); startEdit("__new", "project"); }}>
          + 新增项目 Skill
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------
//  Shared helpers
// ------------------------------------------------------------------

function scopeBadge(s: "global" | "project") {
  return (
    <span style={{
      marginLeft: 6, padding: "1px 6px", borderRadius: 4, fontSize: 11,
      background: s === "global" ? "#fef3c7" : "#e0f2fe",
      color: s === "global" ? "#92400e" : "#0369a1",
      fontWeight: 500,
    }}>
      {s === "global" ? "全局" : "项目"}
    </span>
  );
}

function Field({ label, value, onChange, disabled, placeholder }: { label: string; value: string; onChange: (v: string) => void; disabled?: boolean; placeholder?: string }) {
  return (
    <div>
      <label style={{ fontSize: 12, color: "#666" }}>{label}</label>
      <input
        style={{ ...styles.textInput, height: "auto", padding: "8px 12px", width: "100%", opacity: disabled ? 0.5 : 1 }}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={placeholder}
      />
    </div>
  );
}