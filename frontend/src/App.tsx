import { useState, useEffect } from "react";
import type { ReactNode } from "react";
import { App as AntdApp, ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useTheme } from "./theme";
import { Login } from "./components/Login";
import { Chat } from "./components/Chat";
import { AdminPanel } from "./components/AdminPanel";
import { LibraryAdminPage } from "./components/PptxLibrary";
import { KbAccessAdminPage } from "./components/KbAccessAdmin";
import type { TokenResponse } from "./api";

function AppRoutes() {
  const [auth, setAuth] = useState<TokenResponse | null>(null);
  const [page, setPage] = useState<"chat" | "admin" | "library" | "kbaccess">("chat");

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

  if (page === "library" && auth.role === "admin") {
    return <LibraryAdminPage username={auth.username} onLogout={handleLogout} onExit={() => setPage("chat")} />;
  }

  if (page === "kbaccess" && auth.role === "admin") {
    return <KbAccessAdminPage username={auth.username} onLogout={handleLogout} onExit={() => setPage("chat")} />;
  }

  return (
    <Chat
      username={auth.username}
      role={auth.role || "user"}
      onOpenAdmin={auth.role === "admin" ? () => setPage("admin") : undefined}
      onOpenLibrary={auth.role === "admin" ? () => setPage("library") : undefined}
      onOpenKbAccess={auth.role === "admin" ? () => setPage("kbaccess") : undefined}
      onLogout={handleLogout}
    />
  );
}

/**
 * antd 主题与 theme.css 令牌对齐：
 * 主色 / 圆角 / 字体沿用现有设计变量，明暗随 ThemeProvider 的 data-theme 切换。
 * 未引入 antd/dist/reset.css —— theme.css 已自带全局 reset，
 * 且 antd reset 使用裸元素选择器（p / h1~h6）特异度高于 `*`，会改变对话区既有排版。
 */
export default function App() {
  const { theme } = useTheme();
  const dark = theme !== "light";
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: "#7c3aed",
          colorInfo: "#7c3aed",
          colorLink: dark ? "#a78bfa" : "#6d28d9",
          colorBgBase: dark ? "#161327" : "#ffffff",
          colorText: dark ? "#f1effb" : "#211b3d",
          colorTextSecondary: dark ? "#b7b2d6" : "#57516f",
          colorTextTertiary: dark ? "#8f89b4" : "#837d9e",
          colorBorder: dark ? "#2a2444" : "#e2def0",
          colorBorderSecondary: dark ? "#2a2444" : "#e2def0",
          colorSplit: dark ? "#2a2444" : "#e2def0",
          borderRadius: 10,
          fontFamily: '"Space Grotesk", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
          fontSize: 14,
        },
        components: {
          Modal: { contentBg: dark ? "#161327" : "#ffffff", headerBg: dark ? "#161327" : "#ffffff", titleColor: dark ? "#f1effb" : "#211b3d" },
          Tag: { borderRadiusSM: 999 },
        },
      }}
    >
      {/* AntdApp 为 Chat.tsx 的 AntdApp.useApp()（modal.confirm 等）提供上下文；
          缺少它时 useApp() 返回空壳对象，调用 modal.confirm 会抛
          "confirm is not a function"。component={false} 不渲染包裹 div，
          避免 .ant-app 类影响 theme.css 的既有排版。 */}
      <AntdApp component={false}>
        <AppRoutes />
      </AntdApp>
    </ConfigProvider>
  );
}
