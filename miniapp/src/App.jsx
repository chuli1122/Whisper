import { useState, useEffect } from "react";
import { HashRouter, Routes, Route, useLocation } from "react-router-dom";
import { loginWithPassword } from "./utils/api";
import Home from "./pages/Home";
import WorldBooks from "./pages/WorldBooks";
import WorldBookEdit from "./pages/WorldBookEdit";
import Assistants from "./pages/Assistants";
import AssistantEdit from "./pages/AssistantEdit";
import Settings from "./pages/Settings";
import ApiSettings from "./pages/ApiSettings";
import ProactiveSettings from "./pages/ProactiveSettings";
import VoiceSettings from "./pages/VoiceSettings";
import CotViewer from "./pages/CotViewer";
import Messages from "./pages/Messages";
import Memories from "./pages/Memories";
import Diary from "./pages/Diary";
import PendingMemories from "./pages/PendingMemories";
import Profile from "./pages/Profile";

/* ── Viewport height sync ── */

function syncHeight() {
  const tg = window.Telegram?.WebApp;
  const h = Math.max(tg?.viewportHeight || 0, window.innerHeight);
  document.documentElement.style.setProperty("--tg-viewport-height", h + "px");
}

/* ── Login screen ── */

function LoginScreen({ onSuccess }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!password.trim()) return;
    setLoading(true);
    setError("");
    try {
      await loginWithPassword(password);
      onSuccess();
    } catch (err) {
      setError(err.message || "密码错误");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        background: "var(--bg)",
        padding: "2rem",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 320,
          borderRadius: 24,
          padding: "2rem 1.5rem",
          background: "var(--bg)",
          boxShadow: "var(--card-shadow)",
        }}
      >
        <div style={{ textAlign: "center", marginBottom: "1.5rem" }}>
          <div style={{ fontSize: 36, marginBottom: 8 }}>🐰</div>
          <h1
            style={{
              fontSize: "1.1rem",
              fontWeight: 700,
              color: "var(--text)",
              margin: 0,
            }}
          >
            WHISPER
          </h1>
          <p
            style={{
              fontSize: "0.8rem",
              color: "var(--text-muted)",
              marginTop: 4,
            }}
          >
            请输入密码
          </p>
        </div>

        <form onSubmit={handleSubmit}>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="密码"
            autoFocus
            style={{
              width: "100%",
              padding: "12px 16px",
              borderRadius: 14,
              border: "none",
              outline: "none",
              fontSize: 14,
              color: "var(--text)",
              background: "var(--bg)",
              boxShadow: "var(--inset-shadow)",
              boxSizing: "border-box",
            }}
          />
          {error && (
            <p
              style={{
                color: "#ef4444",
                fontSize: "0.75rem",
                textAlign: "center",
                marginTop: 8,
                marginBottom: 0,
              }}
            >
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={loading || !password.trim()}
            style={{
              width: "100%",
              marginTop: 16,
              padding: "12px 0",
              borderRadius: 14,
              border: "none",
              fontSize: 14,
              fontWeight: 600,
              color: "#fff",
              background:
                loading || !password.trim()
                  ? "var(--text-muted)"
                  : "linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%)",
              cursor:
                loading || !password.trim() ? "not-allowed" : "pointer",
              boxShadow: "var(--card-shadow-sm)",
            }}
          >
            {loading ? "验证中..." : "确认"}
          </button>
        </form>
      </div>
    </div>
  );
}

/* ── Route-aware shell: expand on non-COT pages ── */

function AppRoutes() {
  const location = useLocation();

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    if (tg && !location.pathname.startsWith("/cot") && !location.pathname.startsWith("/pending-memories")) {
      tg.expand();
    }
    syncHeight();
  }, [location.pathname]);

  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/profile" element={<Profile />} />
      <Route path="/world-books" element={<WorldBooks />} />
      <Route path="/world-books/new" element={<WorldBookEdit />} />
      <Route path="/world-books/:id" element={<WorldBookEdit />} />
      <Route path="/assistants" element={<Assistants />} />
      <Route path="/assistants/new" element={<AssistantEdit />} />
      <Route path="/assistants/:id" element={<AssistantEdit />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/settings/api" element={<ApiSettings />} />
      <Route path="/settings/proactive" element={<ProactiveSettings />} />
      <Route path="/settings/voice" element={<VoiceSettings />} />
      <Route path="/cot" element={<CotViewer />} />
      <Route path="/memories" element={<Memories />} />
      <Route path="/pending-memories" element={<PendingMemories />} />
      <Route path="/diary" element={<Diary />} />
      <Route path="/messages" element={<Messages />} />
    </Routes>
  );
}

/* ── App root ── */

export default function App() {
  // null = checking, "ok" = authenticated, "login" = need password
  const [authState, setAuthState] = useState(null);

  useEffect(() => {
    const tg = window.Telegram?.WebApp;

    // 1. Already have a token in localStorage → pass through
    if (localStorage.getItem("whisper_token")) {
      setAuthState("ok");
    }
    // 2. Telegram initData present → will auto-auth on first API call
    else if (tg && tg.initData && tg.initData.length > 0) {
      setAuthState("ok");
    }
    // 3. No token, no Telegram → need password
    else {
      setAuthState("login");
    }

    if (tg) {
      tg.ready();
      if (!window.location.hash.startsWith("#/cot")) {
        tg.expand();
      }
    }

    syncHeight();

    if (tg) tg.onEvent("viewportChanged", syncHeight);
    window.addEventListener("resize", syncHeight);

    return () => {
      if (tg) tg.offEvent("viewportChanged", syncHeight);
      window.removeEventListener("resize", syncHeight);
    };
  }, []);

  if (authState === null) return null;

  if (authState === "login") {
    return <LoginScreen onSuccess={() => setAuthState("ok")} />;
  }

  return (
    <HashRouter>
      <AppRoutes />
    </HashRouter>
  );
}
