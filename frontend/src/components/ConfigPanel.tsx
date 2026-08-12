import { useState, useEffect, useCallback } from "react";
import { api } from "../api";
import { styles } from "./chatStyles";

export function ConfigPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<"providers" | "mcp" | "skills">("providers");
  const [overview, setOverview] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      setOverview(await api.getConfigOverview());
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleReload = async () => {
    setBusy("重新加载配置到容器…");
    setError("");
    try {
      const r = await api.reloadConfigIntoContainer();
      if (!r.reloaded) setError(r.message);
      else await load();
    } catch (e: any) {
      setError(e.message);
    } finally { setBusy(""); }
  };

  return (
    <div style={styles.modal} onClick={onClose}>
      <div style={{ ...styles.modalContent, width: "90%", maxWidth: 720, maxHeight: "85vh" }} onClick={(e) => e.stopPropagation()}>
        <div style={styles.modalHeader}>
          <span>配置管理 · opencode.json</span>
          <button style={styles.modalClose} onClick={onClose}>×</button>
        </div>
        <div style={{ ...styles.modalBody, padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
          {error && <div style={styles.errorBanner}><span>{error}</span><button onClick={() => setError("")}>×</button></div>}
          <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #e0e0e0" }}>
            {(["providers", "mcp", "skills"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                style={{
                  padding: "10px 20px", border: "none", cursor: "pointer", fontSize: 13,
                  fontWeight: tab === t ? 600 : 400,
                  background: tab === t ? "#f0f4ff" : "transparent",
                  borderBottom: tab === t ? "2px solid #378ADD" : "2px solid transparent",
                }}
              >
                {t === "providers" ? "LLM Provider" : t === "mcp" ? "MCP 服务" : "Skills"} ({overview?.[t] ? (t === "skills" ? overview[t].length : Object.keys(overview[t]).length) : 0})
              </button>
            ))}
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: "16px" }}>
            {tab === "providers" && <ProviderTab overview={overview} onChange={load} />}
            {tab === "mcp" && <McpTab overview={overview} onChange={load} />}
            {tab === "skills" && <SkillTab overview={overview} onChange={load} />}
          </div>
          <div style={{ padding: "12px 16px", borderTop: "1px solid #e0e0e0", display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button style={{ ...styles.reloadBtn, opacity: busy ? 0.5 : 1 }} onClick={handleReload} disabled={!!busy}>
              {busy || "重载到容器"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

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
    await api.upsertProvider(form.id, { name: form.name, npm: form.npm, options: opts, models: {} });
    setEditing(null);
    onChange();
  };

  const handleDelete = async (id: string) => {
    if (!confirm(`删除 provider "${id}"?`)) return;
    await api.deleteProvider(id);
    onChange();
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

function McpTab({ overview, onChange }: { overview: any; onChange: () => void }) {
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
    await api.upsertMcp(form.name, cfg);
    setEditing(null);
    onChange();
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`删除 MCP "${name}"?`)) return;
    await api.deleteMcp(name);
    onChange();
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    await api.toggleMcp(name, enabled);
    onChange();
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
            </div>
            <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={data.enabled} onChange={(e) => handleToggle(name, e.target.checked)} />
              <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => startEdit(name, data)}>编辑</button>
              <button style={{ ...styles.abortBtn, fontSize: 12 }} onClick={() => handleDelete(name)}>删除</button>
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

function SkillTab({ overview, onChange }: { overview: any; onChange: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", content: "" });

  const skills = overview?.skills || [];

  const startEdit = async (name: string) => {
    if (name === "__new") {
      setForm({ name: "", content: "---\nname: \ndescription: \n---\n\n## What I do\n\n" });
    } else {
      const skill = await api.getSkill(name);
      setForm({ name, content: skill.content });
    }
    setEditing(name);
  };

  const handleSave = async () => {
    await api.upsertSkill(form.name, form.content);
    setEditing(null);
    onChange();
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`删除 Skill "${name}"?`)) return;
    await api.deleteSkill(name);
    onChange();
  };

  if (editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <h3 style={{ margin: 0 }}>{editing === "__new" ? "新增" : "编辑"} Skill</h3>
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
      {skills.map((s: any) => (
        <div key={s.dir} style={{ padding: "12px", marginBottom: 8, border: "1px solid #e0e0e0", borderRadius: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{s.name}</strong>
              <div style={{ fontSize: 12, color: "#666" }}>{s.description}</div>
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              <button style={{ ...styles.logBtn, fontSize: 12 }} onClick={() => startEdit(s.dir)}>编辑</button>
              <button style={{ ...styles.abortBtn, fontSize: 12 }} onClick={() => handleDelete(s.dir)}>删除</button>
            </div>
          </div>
        </div>
      ))}
      <button style={{ ...styles.newSessionLargeBtn, marginTop: 12 }} onClick={() => startEdit("__new")}>
        + 新增 Skill
      </button>
    </div>
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
