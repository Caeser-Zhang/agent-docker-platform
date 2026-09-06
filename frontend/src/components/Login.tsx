import { useState } from "react";
import { api, type TokenResponse } from "../api";
import { ThemeToggle } from "../theme";

/** 登录屏样式（对齐 frontend/design/hifi-redesign.html，全部走 theme.css 令牌，随双主题切换） */
const loginCss = `
.lg-screen{
  position:relative;display:flex;align-items:center;justify-content:center;height:100%;
  background:
    radial-gradient(900px 520px at 18% 8%,rgba(124,58,237,.16),transparent 60%),
    radial-gradient(760px 480px at 85% 90%,rgba(34,211,238,.09),transparent 60%),
    var(--bg);
}
.lg-screen::before{
  content:'';position:absolute;inset:0;pointer-events:none;
  background-image:
    linear-gradient(rgba(124,58,237,.05) 1px,transparent 1px),
    linear-gradient(90deg,rgba(124,58,237,.05) 1px,transparent 1px);
  background-size:44px 44px;
  -webkit-mask-image:radial-gradient(640px 480px at 50% 42%,#000 20%,transparent 78%);
  mask-image:radial-gradient(640px 480px at 50% 42%,#000 20%,transparent 78%);
}
.lg-card{
  position:relative;z-index:1;width:430px;max-width:calc(100vw - 32px);
  background:rgba(22,19,39,.88);border:1px solid var(--border-strong);border-radius:18px;
  box-shadow:var(--shadow-3);padding:34px 36px 28px;backdrop-filter:blur(10px);
}
.lg-brand{display:flex;align-items:center;gap:13px;margin-bottom:6px}
.lg-logo{
  width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,var(--primary),#4C1D95);color:#fff;box-shadow:0 6px 20px rgba(124,58,237,.45);
}
.lg-brand h1{font-size:19px;font-weight:700;letter-spacing:-.3px;color:var(--text);margin:0}
.lg-brand-sub{font-size:12px;color:var(--text-3);font-family:var(--mono);letter-spacing:.02em}
.lg-desc{font-size:13px;color:var(--text-2);margin:12px 0 20px}
.lg-seg{display:flex;gap:4px;padding:4px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:20px}
.lg-seg button{
  flex:1;height:32px;border:none;border-radius:7px;font-size:13px;font-weight:500;color:var(--text-3);
  background:transparent;cursor:pointer;font-family:inherit;
  transition:background var(--t),color var(--t),box-shadow var(--t);
}
.lg-seg button:hover{color:var(--text)}
.lg-seg button.active{background:var(--primary);color:#fff;box-shadow:0 2px 10px rgba(124,58,237,.4)}
.lg-form .lg-fgroup{margin-bottom:14px}
.lg-lbl{display:block;font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:7px}
/* 边框画在 wrapper 上（input 画边框时，Chrome autofill 机制会干预其边框/背景，
   wrapper + :focus-within 是表单组件库的标准做法） */
.lg-field-wrap{
  height:36px;border-radius:var(--radius-sm);
  background:var(--surface-2);border:1px solid var(--border);
  transition:border-color var(--t),box-shadow var(--t);
}
.lg-field-wrap:focus-within{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft)}
.lg-field{
  height:100%;width:100%;padding:0 11px;border:none;outline:none;background:transparent;
  color:var(--text);font-family:inherit;font-size:13.5px;
}
.lg-field::placeholder{color:var(--text-3)}
/* autofill 兼容：覆盖 Chrome 密码管理器的蓝底预览，背景/文字跟随主题令牌 */
.lg-field:-webkit-autofill,.lg-field:autofill{
  -webkit-box-shadow:0 0 0 1000px var(--surface-2) inset;
  box-shadow:0 0 0 1000px var(--surface-2) inset;
  -webkit-text-fill-color:var(--text);
  transition:background-color 99999s ease-out;
}
.lg-error{
  display:flex;align-items:center;gap:8px;padding:9px 12px;margin-bottom:14px;border-radius:var(--radius-sm);
  background:var(--red-soft);border:1px solid var(--red-border);color:var(--red);font-size:12.5px;
}
.lg-btn{
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  width:100%;height:40px;font-size:14px;margin-top:4px;
  border:none;border-radius:var(--radius-sm);font-weight:500;font-family:inherit;cursor:pointer;
  background:var(--primary);color:#fff;
  transition:background var(--t),box-shadow var(--t);
}
.lg-btn:hover:not(:disabled){background:var(--primary-hover);box-shadow:0 4px 18px rgba(124,58,237,.4)}
.lg-btn:active:not(:disabled){transform:translateY(1px)}
.lg-btn:disabled{opacity:.45;cursor:not-allowed;pointer-events:none}
.lg-arch{margin-top:24px;padding-top:18px;border-top:1px dashed var(--border-strong)}
.lg-arch-title{
  font-size:11px;font-weight:600;letter-spacing:.1em;color:var(--text-3);text-transform:uppercase;
  margin-bottom:12px;display:flex;align-items:center;gap:6px;
}
.lg-steps{display:flex;align-items:center;gap:6px}
.lg-step{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;font-size:11.5px;color:var(--text-2);text-align:center}
.lg-step .lg-ic{
  width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;
  background:var(--surface-2);border:1px solid var(--border);
}
.lg-step .lg-sub{font-size:10.5px;color:var(--text-3);font-family:var(--mono);margin-top:-2px}
.lg-arrow{color:var(--text-3);display:flex;align-items:center}
.lg-step.s1 .lg-ic{color:#38BDF8;border-color:rgba(56,189,248,.35);background:rgba(56,189,248,.1)}
.lg-step.s2 .lg-ic{color:#A78BFA;border-color:rgba(167,139,250,.35);background:rgba(167,139,250,.1)}
.lg-step.s3 .lg-ic{color:var(--accent);border-color:var(--accent-border);background:var(--accent-soft)}
.lg-hint{margin-top:16px;text-align:center;font-size:12px;color:var(--text-3)}
.lg-kbd{
  display:inline-flex;align-items:center;height:20px;padding:0 6px;border-radius:5px;margin:0 2px;
  font-family:var(--mono);font-size:11px;color:var(--text-3);
  background:var(--surface-3);border:1px solid var(--border);border-bottom-width:2px;
}
/* 亮色局部覆盖 */
html[data-theme="light"] .lg-card{background:rgba(255,255,255,.92)}
html[data-theme="light"] .lg-screen::before{
  background-image:
    linear-gradient(rgba(76,29,149,.08) 1px,transparent 1px),
    linear-gradient(90deg,rgba(76,29,149,.08) 1px,transparent 1px);
}
`;

/** Lucide 风格 24×24 stroke-2 图标（同原型 sprite） */
function Ic({ d, size = 16 }: { d: React.ReactNode; size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      style={{ width: size, height: size, flex: "none", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" as const }}
    >
      {d}
    </svg>
  );
}

export function Login({ onLogin }: { onLogin: (t: TokenResponse) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Empty fields must tell the user why nothing happened — a silent
    // return looks exactly like a broken button.
    if (!username || !password) {
      setError("请输入用户名和密码");
      return;
    }
    if (mode === "register" && password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setError("");
    setLoading(true);
    try {
      const result = mode === "login"
        ? await api.login(username, password)
        : await api.register(username, password);
      localStorage.setItem("token", result.access_token);
      localStorage.setItem("userId", result.user_id);
      localStorage.setItem("username", result.username);
      localStorage.setItem("role", result.role || "user");
      onLogin(result);
    } catch (err: any) {
      setError(err.message || "Authentication failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="lg-screen">
      <style>{loginCss}</style>
      <div className="lg-card">
        <ThemeToggle style={{ position: "absolute", top: 18, right: 18 }} />

        <div className="lg-brand">
          <div className="lg-logo">
            <Ic
              size={24}
              d={
                <>
                  <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
                  <circle cx="12" cy="12" r="3" />
                </>
              }
            />
          </div>
          <div>
            <h1>Agent Docker Platform</h1>
            <div className="lg-brand-sub">per-user sandboxed coding agent</div>
          </div>
        </div>
        <p className="lg-desc">
          每用户独立容器沙箱中的编码智能体。登录后自动为你的会话分配隔离的 Docker 运行环境，代码执行互不影响。
        </p>

        <div className="lg-seg" role="tablist" aria-label="登录或注册">
          <button className={mode === "login" ? "active" : ""} role="tab" aria-selected={mode === "login"} onClick={() => setMode("login")}>
            登录
          </button>
          <button className={mode === "register" ? "active" : ""} role="tab" aria-selected={mode === "register"} onClick={() => setMode("register")}>
            注册
          </button>
        </div>

        <form className="lg-form" onSubmit={handleSubmit}>
          {error && (
            <div className="lg-error">
              <Ic d={<><circle cx="12" cy="12" r="10" /><path d="M12 8v4" /><path d="M12 16h.01" /></>} />
              <span>{error}</span>
            </div>
          )}
          <div className="lg-fgroup">
            <label className="lg-lbl" htmlFor="lg-username">用户名</label>
            <div className="lg-field-wrap">
              <input className="lg-field" id="lg-username" type="text" placeholder="用户名" autoComplete="username"
                value={username} onChange={(e) => setUsername(e.target.value)} disabled={loading} />
            </div>
          </div>
          <div className="lg-fgroup">
            <label className="lg-lbl" htmlFor="lg-password">密码</label>
            <div className="lg-field-wrap">
              <input className="lg-field" id="lg-password" type="password" placeholder="密码" autoComplete="current-password"
                value={password} onChange={(e) => setPassword(e.target.value)} disabled={loading} />
            </div>
          </div>
          {mode === "register" && (
            <div className="lg-fgroup">
              <label className="lg-lbl" htmlFor="lg-confirm">确认密码</label>
              <div className="lg-field-wrap">
                <input className="lg-field" id="lg-confirm" type="password" placeholder="再次输入密码" autoComplete="new-password"
                  value={confirm} onChange={(e) => setConfirm(e.target.value)} disabled={loading} />
              </div>
            </div>
          )}
          <button className="lg-btn" type="submit" disabled={loading}>
            <Ic d={<><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></>} />
            <span>{loading ? "请稍候…" : mode === "login" ? "进入工作台" : "创建账户并进入"}</span>
          </button>
        </form>

        <div className="lg-arch">
          <div className="lg-arch-title">
            <Ic d={<><path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z" /><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65" /><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65" /></>} />
            三段式架构
          </div>
          <div className="lg-steps">
            <div className="lg-step s1">
              <span className="lg-ic">
                <Ic size={17} d={<><rect width="20" height="14" x="2" y="3" rx="2" /><line x1="8" x2="16" y1="21" y2="21" /><line x1="12" x2="12" y1="17" y2="21" /></>} />
              </span>
              <span>浏览器层</span>
              <span className="lg-sub">React SPA</span>
            </div>
            <span className="lg-arrow"><Ic size={13} d={<path d="m9 18 6-6-6-6" />} /></span>
            <div className="lg-step s2">
              <span className="lg-ic">
                <Ic size={17} d={<><path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z" /><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65" /><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65" /></>} />
              </span>
              <span>平台控制层</span>
              <span className="lg-sub">FastAPI</span>
            </div>
            <span className="lg-arrow"><Ic size={13} d={<path d="m9 18 6-6-6-6" />} /></span>
            <div className="lg-step s3">
              <span className="lg-ic">
                <Ic size={17} d={<><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" /><path d="m3.3 7 8.7 5 8.7-5" /><path d="M12 22V12" /></>} />
              </span>
              <span>容器执行层</span>
              <span className="lg-sub">Docker</span>
            </div>
          </div>
        </div>

        <p className="lg-hint">
          按 <span className="lg-kbd">Enter</span> 快速登录 · 每用户独立容器沙箱
        </p>
      </div>
    </div>
  );
}
