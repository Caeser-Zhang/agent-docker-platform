import { useState } from "react";
import { api, type TokenResponse } from "../api";

export function Login({ onLogin }: { onLogin: (t: TokenResponse) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
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
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={styles.header}>
          <div style={styles.logo}>🤖</div>
          <h1 style={styles.title}>Agent Docker Platform</h1>
          <p style={styles.subtitle}>浏览器层 → 平台控制层 → 容器执行层</p>
        </div>

        <div style={styles.tabs}>
          <button
            style={mode === "login" ? styles.tabActive : styles.tab}
            onClick={() => setMode("login")}
          >
            登录
          </button>
          <button
            style={mode === "register" ? styles.tabActive : styles.tab}
            onClick={() => setMode("register")}
          >
            注册
          </button>
        </div>

        <form onSubmit={handleSubmit} style={styles.form}>
          <input
            style={styles.input}
            type="text"
            placeholder="用户名"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={loading}
          />
          <input
            style={styles.input}
            type="password"
            placeholder="密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={loading}
          />
          {error && <div style={styles.error}>{error}</div>}
          <button style={styles.button} type="submit" disabled={loading}>
            {loading ? "请稍候..." : mode === "login" ? "登录" : "注册"}
          </button>
        </form>

        <div style={styles.archInfo}>
          <div style={styles.archItem}><span style={styles.archIcon}>1️⃣</span> 浏览器层 (React SPA)</div>
          <div style={styles.archArrow}>↓ HTTPS / SSE</div>
          <div style={styles.archItem}><span style={styles.archIcon}>2️⃣</span> 平台控制层 (FastAPI)</div>
          <div style={styles.archArrow}>↓ Docker API</div>
          <div style={styles.archItem}><span style={styles.archIcon}>3️⃣</span> 容器执行层 (per-user Docker)</div>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    height: "100vh",
    background: "#f7f8fa",
  },
  card: {
    background: "#ffffff",
    border: "1px solid #e6e8ee",
    borderRadius: "16px",
    padding: "40px",
    width: "420px",
    maxWidth: "90vw",
    boxShadow: "0 8px 24px rgba(15,23,42,0.06)",
  },
  header: {
    textAlign: "center",
    marginBottom: "32px",
  },
  logo: {
    fontSize: "48px",
    marginBottom: "12px",
  },
  title: {
    fontSize: "22px",
    fontWeight: 600,
    color: "#16181d",
    marginBottom: "4px",
  },
  subtitle: {
    fontSize: "13px",
    color: "#9aa2b1",
  },
  tabs: {
    display: "flex",
    gap: "4px",
    marginBottom: "20px",
    background: "#f1f3f5",
    borderRadius: "8px",
    padding: "4px",
  },
  tab: {
    flex: 1,
    padding: "8px",
    border: "none",
    background: "transparent",
    color: "#5b6472",
    cursor: "pointer",
    borderRadius: "6px",
    fontSize: "14px",
  },
  tabActive: {
    flex: 1,
    padding: "8px",
    border: "none",
    background: "#ffffff",
    color: "#16181d",
    cursor: "pointer",
    borderRadius: "6px",
    fontSize: "14px",
    fontWeight: 600,
    boxShadow: "0 1px 3px rgba(15,23,42,0.08)",
  },
  form: {
    display: "flex",
    flexDirection: "column",
    gap: "12px",
  },
  input: {
    padding: "12px 16px",
    border: "1px solid #d5dae3",
    borderRadius: "8px",
    background: "#f7f8fa",
    color: "#16181d",
    fontSize: "14px",
    outline: "none",
  },
  button: {
    padding: "12px",
    border: "none",
    borderRadius: "8px",
    background: "#2563eb",
    color: "#fff",
    fontSize: "14px",
    fontWeight: 600,
    cursor: "pointer",
    marginTop: "8px",
  },
  error: {
    color: "#dc2626",
    fontSize: "13px",
    textAlign: "center",
  },
  archInfo: {
    marginTop: "32px",
    padding: "16px",
    background: "#f1f3f5",
    border: "1px solid #e6e8ee",
    borderRadius: "8px",
    fontSize: "12px",
  },
  archItem: {
    color: "#5b6472",
    padding: "4px 0",
  },
  archIcon: {
    marginRight: "8px",
  },
  archArrow: {
    color: "#9aa2b1",
    textAlign: "center",
    fontSize: "11px",
    padding: "2px 0",
  },
};
