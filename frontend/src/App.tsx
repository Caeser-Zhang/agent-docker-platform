import { useState, useEffect } from "react";
import { Login } from "./components/Login";
import { Chat } from "./components/Chat";
import type { TokenResponse } from "./api";

export default function App() {
  const [auth, setAuth] = useState<TokenResponse | null>(null);

  useEffect(() => {
    // Restore session from localStorage
    const token = localStorage.getItem("token");
    const username = localStorage.getItem("username");
    const userId = localStorage.getItem("userId");
    if (token && username && userId) {
      setAuth({ access_token: token, token_type: "bearer", username, user_id: userId });
    }
  }, []);

  const handleLogout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("username");
    localStorage.removeItem("userId");
    setAuth(null);
  };

  if (!auth) {
    return <Login onLogin={setAuth} />;
  }

  return <Chat username={auth.username} onLogout={handleLogout} />;
}
