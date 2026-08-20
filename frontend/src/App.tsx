import { useState, useEffect } from "react";
import { Login } from "./components/Login";
import { Chat } from "./components/Chat";
import { AdminPanel } from "./components/AdminPanel";
import type { TokenResponse } from "./api";

export default function App() {
  const [auth, setAuth] = useState<TokenResponse | null>(null);
  const [page, setPage] = useState<"chat" | "admin">("chat");

  useEffect(() => {
    // Restore session from localStorage
    const token = localStorage.getItem("token");
    const username = localStorage.getItem("username");
    const userId = localStorage.getItem("userId");
    const role = localStorage.getItem("role");
    if (token && username && userId) {
      setAuth({ access_token: token, token_type: "bearer", username, user_id: userId, role: role || "user" });
    }
  }, []);

  const handleLogin = (t: TokenResponse) => {
    setAuth(t);
    setPage("chat");
  };

  const handleLogout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("username");
    localStorage.removeItem("userId");
    localStorage.removeItem("role");
    setAuth(null);
    setPage("chat");
  };

  if (!auth) {
    return <Login onLogin={handleLogin} />;
  }

  if (page === "admin" && auth.role === "admin") {
    return <AdminPanel username={auth.username} onLogout={handleLogout} onExit={() => setPage("chat")} />;
  }

  return (
    <Chat
      username={auth.username}
      role={auth.role || "user"}
      onOpenAdmin={auth.role === "admin" ? () => setPage("admin") : undefined}
      onLogout={handleLogout}
    />
  );
}
