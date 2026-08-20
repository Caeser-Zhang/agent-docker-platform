import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api";
import { styles } from "./chatStyles";

type Scope = "global" | "project";

export function ConfigPanel({ onClose }: { onClose: () => void }) {
  const [scope, setScope] = useState<Scope>("global");
  const [tab, setTab] = useState<"providers" | "mcp" | "skills" | "config">("providers");
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

  const scopeBadge = (s: "global" | "project") => (
    <span style={{
      marginLeft: 6, padding: "1px 6px", borderRadius: 4, fontSize: 11,
      background: s === "global" ? "#fef3c7" : "#e0f2fe",
      color: s === "global" ? "#92400e" : "#0369a1",
      fontWeight: 500,
    }}>
      {s === "global" ? "全局" : "项目"}
    </span>
  );

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
            {(["global", "project"] as const).map((s) => (
              <button
                key={s}
                onClick={() => { setScope(s); setTab(s === "project" ? "skills" : "providers"); }}
                style={{
                  padding: "10px 20px", border: "none", cursor: "pointer", fontSize: 13,
                  fontWeight: scope === s ? 600 : 400,
                  background: scope === s ? (s === "global" ? "#fef9f0" : "#f0f7ff") : "transparent",
                  borderBottom: scope === s ? `2px solid ${s === "global" ? "#f59e0b" : "#378ADD"}` : "2px solid transparent",
                }}
              >
                {s === "global" ? "全局配置" : "项目配置"}
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
            {scope === "global" && tab === "mcp" && <McpTab overview={overview} onChange={loadAll} />}
            {scope === "global" && tab === "skills" && <SkillTab skills={allSkills} onChange={loadAll} scope="global" />}
            {scope === "project" && tab === "config" && (
              <ProjectConfigTab config={projectConfig} onChange={loadAll} />
            )}
            {scope === "project" && tab === "skills" && (
              <SkillTab skills={projectSkills.map((s: any) => ({ ...s, scope: "project" }))} onChange={loadAll} scope="project" />
            )}
          </div>

          <div style={{ padding: "12px 16px", borderTop: "1px solid #e0e0e0", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 11, color: "#999" }}>
              {scope === "global" ? "全局" : "项目"}配置 · 重启容器后生效
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
    await api.upsertProvider(form.id, { name: form.name, npm: form.npm, options: opts });
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
              {scopeBadge("global")}
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
    if (editScope === "global") {
      await api.upsertSkill(form.name, form.content);
    } else {
      await api.upsertProjectSkill(form.name, form.content);
    }
    setEditing(null);
    onChange();
  };

  const handleDelete = async (name: string, s: "global" | "project") => {
    if (!confirm(`删除 ${s === "global" ? "全局" : "项目"} Skill "${name}"?`)) return;
    if (s === "global") {
      await api.deleteSkill(name);
    } else {
      await api.deleteProjectSkill(name);
    }
    onChange();
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
      {/* Zip import — only for project scope */}
      {scope === "project" && (
        <div style={{ marginBottom: 16, padding: 12, border: "2px dashed #d0d5dd", borderRadius: 8, textAlign: "center" }}>
          <p style={{ margin: "0 0 8px", fontSize: 13, color: "#666" }}>导入 Skill 压缩包 (.zip) 到项目级</p>
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip"
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