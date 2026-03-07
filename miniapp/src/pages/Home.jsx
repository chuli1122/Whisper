import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { BookOpen, Brain, Bot, Settings, BookMarked, Theater, Heart, ChevronRight } from "lucide-react";
import { apiFetch } from "../utils/api";
import { getAvatar } from "../utils/db";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

function GridCard({ icon, label, desc, disabled, onClick, badge }) {
  const [pressed, setPressed] = useState(false);

  const start = () => !disabled && setPressed(true);
  const end = () => { setPressed(false); if (!disabled) onClick?.(); };

  return (
    <button
      className="flex flex-col items-start rounded-[18px] gap-2 text-left"
      style={{
        padding: "16px 14px",
        background: S.bg,
        boxShadow: pressed
          ? "inset 3px 3px 8px rgba(174,176,182,0.5), inset -3px -3px 8px #ffffff"
          : "5px 5px 12px rgba(174,176,182,0.5), -5px -5px 12px #ffffff",
        opacity: disabled ? 0.4 : 1,
        transform: pressed ? "scale(0.97)" : "scale(1)",
        transition: "transform 0.1s, box-shadow 0.1s",
        cursor: disabled ? "default" : "pointer",
        WebkitTapHighlightColor: "transparent",
      }}
      disabled={disabled}
      onTouchStart={start}
      onTouchEnd={end}
      onMouseDown={start}
      onMouseUp={end}
      onMouseLeave={() => setPressed(false)}
    >
      <div className="relative">
        <div
          className="flex h-10 w-10 items-center justify-center rounded-[12px]"
          style={{ background: "linear-gradient(135deg, #f0c4d8, var(--accent))" }}
        >
          {icon}
        </div>
        {badge && (
          <div className="absolute -top-1 -right-1 h-2.5 w-2.5 rounded-full" style={{ background: "#ef4444", border: "2px solid var(--bg)" }} />
        )}
      </div>
      <div className="pl-1">
        <div className="text-[13px] font-medium" style={{ color: S.text }}>{label}</div>
        <div className="mt-0.5 text-[10px] leading-snug" style={{ color: S.textMuted }}>{desc}</div>
      </div>
    </button>
  );
}

function SectionLabel({ children }) {
  return (
    <div
      className="mb-2 text-[11px] font-bold uppercase tracking-[3px]"
      style={{ color: S.textMuted }}
    >
      {children}
    </div>
  );
}

export default function Home() {
  const navigate = useNavigate();
  const [profile, setProfile] = useState(() => {
    try { return JSON.parse(localStorage.getItem("whisper_profile") || "null"); }
    catch (_e) { return null; }
  });
  const [avatarUrl, setAvatarUrl] = useState(null);
  const [diaryUnread, setDiaryUnread] = useState(false);

  useEffect(() => {
    apiFetch("/api/user/profile").then((d) => {
      setProfile(d);
      localStorage.setItem("whisper_profile", JSON.stringify(d));
    }).catch(() => {});
    getAvatar("user-avatar").then((b64) => { if (b64) setAvatarUrl(b64); }).catch(function() {});
    apiFetch("/api/diary/unread-count").then((d) => { if (d.count > 0) setDiaryUnread(true); }).catch(() => {});
  }, []);
  const nickname = profile?.nickname || "阿怀";
  const signature = profile?.background_url || "今晚的月亮很圆";

  return (
    <div
      className="flex flex-col overflow-y-auto"
      style={{ background: S.bg, height: "100%", paddingBottom: 16 }}
    >
      <div
        className="flex flex-col px-5"
        style={{ paddingTop: "max(12px, env(safe-area-inset-top))" }}
      >
        {/* Title */}
        <h1
          className="mb-4 text-center text-[17px] font-medium"
          style={{ color: S.textMuted, letterSpacing: "4px" }}
        >
          W H I S P E R
        </h1>

        {/* Profile card — clickable, no avatar upload */}
        <button
          className="mb-5 flex w-full items-center gap-4 rounded-[20px] p-5 text-left"
          style={{ background: S.bg, boxShadow: "6px 6px 14px rgba(174,176,182,0.5), -6px -6px 14px #ffffff" }}
          onClick={() => navigate("/profile")}
        >
          {/* Avatar — display only */}
          <div
            className="shrink-0 rounded-full"
            style={{
              width: 52, height: 52,
              background: "linear-gradient(135deg, #f0c4d8, var(--accent))",
              padding: 3,
            }}
          >
            <div
              className="flex h-full w-full items-center justify-center overflow-hidden rounded-full"
              style={{ background: S.bg }}
            >
              {avatarUrl ? (
                <img src={avatarUrl} alt="avatar" className="h-full w-full object-cover rounded-full" />
              ) : (
                <span style={{ fontSize: 22 }}>🐰</span>
              )}
            </div>
          </div>

          <div className="flex-1 min-w-0">
            <div className="text-[17px] font-bold truncate" style={{ color: S.text }}>
              @{nickname}
            </div>
            <div className="mt-0.5 text-[11px] italic" style={{ color: S.textMuted }}>
              "{signature}"
            </div>
          </div>
          <ChevronRight size={16} style={{ color: S.textMuted, flexShrink: 0 }} />
        </button>

        {/* 管理 section */}
        <SectionLabel>管理</SectionLabel>
        <div className="mb-4 grid grid-cols-2 gap-3.5">
          <GridCard
            icon={<BookOpen size={18} color="white" />}
            label="世界书"
            desc="规则集·文风·指南"
            onClick={() => navigate("/world-books")}
          />
          <GridCard
            icon={<Brain size={18} color="white" />}
            label="记忆管理"
            desc="记忆卡片·摘要·消息记录"
            onClick={() => navigate("/memories")}
          />
          <GridCard
            icon={<Bot size={18} color="white" />}
            label="助手配置"
            desc="人设·Core Blocks·挂载"
            onClick={() => navigate("/assistants")}
          />
          <GridCard
            icon={<Settings size={18} color="white" />}
            label="设置"
            desc="API·模型预设·参数"
            onClick={() => navigate("/settings")}
          />
        </div>

        {/* 空间 section */}
        <SectionLabel>空间</SectionLabel>
        <div className="mb-4 grid grid-cols-2 gap-3.5">
          <GridCard
            icon={<BookMarked size={18} color="white" />}
            label="日记"
            desc="写给彼此的"
            badge={diaryUnread}
            onClick={() => navigate("/diary")}
          />
          <GridCard
            icon={<Theater size={18} color="white" />}
            label="小剧场"
            desc="角色卡·故事线"
            disabled
          />
        </div>

        {/* Heart widget — click to open COT */}
        <button
          className="flex w-full items-center justify-center rounded-[18px] py-4"
          style={{ background: S.bg, boxShadow: "5px 5px 12px rgba(174,176,182,0.5), -5px -5px 12px #ffffff" }}
          onClick={() => navigate("/cot")}
        >
          <Heart
            size={32}
            fill={S.accent}
            style={{
              color: S.accent,
              animation: "heartbeat 2s ease-in-out infinite",
            }}
          />
        </button>
      </div>

      <style>{`
        @keyframes heartbeat {
          0%, 100% { transform: scale(1); }
          15% { transform: scale(1.15); }
          30% { transform: scale(1); }
          45% { transform: scale(1.1); }
          60% { transform: scale(1); }
        }
      `}</style>
    </div>
  );
}
