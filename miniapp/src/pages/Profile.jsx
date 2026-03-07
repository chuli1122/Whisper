import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Camera, FileText, Maximize2, Minimize2, Save } from "lucide-react";
import { apiFetch } from "../utils/api";
import { saveAvatar, getAvatar } from "../utils/db";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

function FullscreenEditor({ value, onChange, onClose }) {
  return (
    <div className="fixed inset-0 z-[200] flex flex-col" style={{ background: S.bg }}>
      <div
        className="flex shrink-0 items-center justify-between px-5 py-4"
        style={{ paddingTop: "max(1rem, env(safe-area-inset-top))" }}
      >
        <span className="text-[15px] font-bold" style={{ color: S.text }}>基本信息</span>
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={onClose}
        >
          <Minimize2 size={18} style={{ color: S.accentDark }} />
        </button>
      </div>
      <textarea
        autoFocus
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 px-5 pb-10 text-[14px] resize-none outline-none"
        style={{ background: S.bg, color: S.text }}
        placeholder="介绍一下自己..."
      />
    </div>
  );
}

export default function Profile() {
  const navigate = useNavigate();
  const [nickname, setNickname] = useState("");
  const [signature, setSignature] = useState("");
  const [basicInfo, setBasicInfo] = useState("");
  const [avatarUrl, setAvatarUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState(null);
  const [fullscreen, setFullscreen] = useState(false);

  const fileRef = useRef(null);
  const infoFileRef = useRef(null);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => {
    apiFetch("/api/user/profile")
      .then((d) => {
        setNickname(d.nickname || "");
        setSignature(d.background_url || "");
        setBasicInfo(d.basic_info || "");
      })
      .catch(() => {})
      .finally(() => setLoading(false));
    getAvatar("user-avatar").then((b64) => { if (b64) setAvatarUrl(b64); }).catch(function() {});
  }, []);

  const handleAvatarUpload = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      const base64 = ev.target.result;
      try {
        await saveAvatar("user-avatar", base64);
        setAvatarUrl(base64);
      } catch (_e) {
        showToast("头像保存失败");
      }
    };
    reader.readAsDataURL(file);
    e.target.value = "";
  };

  const handleInfoFile = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => setBasicInfo(ev.target.result || "");
    reader.readAsText(file);
    e.target.value = "";
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const body = {
        nickname: nickname.trim() || null,
        background_url: signature || null,
        basic_info: basicInfo || null,
        avatar_url: avatarUrl ? "user-avatar" : null,
      };
      await apiFetch("/api/user/profile", { method: "PUT", body });
      // Sync cache so Home page doesn't flash on return
      try {
        const cached = JSON.parse(localStorage.getItem("whisper_profile") || "{}");
        localStorage.setItem("whisper_profile", JSON.stringify({ ...cached, ...body }));
      } catch (_e) {}
      showToast("已保存");
    } catch (_e) {
      showToast("保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div
        className="flex shrink-0 items-center justify-between px-5 pb-4"
        style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}
      >
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => navigate("/", { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>个人资料</h1>
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{
            background: S.bg,
            boxShadow: saving ? "var(--inset-shadow)" : "var(--card-shadow-sm)",
          }}
          onClick={handleSave}
          disabled={saving}
        >
          <Save size={18} style={{ color: S.accentDark }} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5 space-y-4">
        {loading ? (
          <div className="flex h-40 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : (<>
        {/* Avatar + Nickname + Signature */}
        <div
          className="rounded-[20px] p-5"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="flex items-start gap-4">
            {/* Avatar */}
            <button
              className="relative shrink-0"
              style={{ width: 68, height: 68 }}
              onClick={() => fileRef.current?.click()}
            >
              <div
                className="flex h-full w-full items-center justify-center overflow-hidden rounded-full"
                style={{
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
                    <span style={{ fontSize: 24 }}>🐰</span>
                  )}
                </div>
              </div>
              <div
                className="absolute bottom-0 right-0 flex h-5 w-5 items-center justify-center rounded-full"
                style={{ background: S.accentDark }}
              >
                <Camera size={9} color="white" />
              </div>
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={handleAvatarUpload}
            />

            {/* Nickname + Signature */}
            <div className="flex-1 min-w-0 space-y-3">
              <div>
                <label
                  className="mb-1 block text-[11px] font-semibold uppercase tracking-wide"
                  style={{ color: S.textMuted }}
                >
                  昵称
                </label>
                <input
                  type="text"
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  placeholder="你的名字"
                  className="w-full rounded-[12px] px-3 py-2.5 text-[15px] font-bold outline-none"
                  style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
                />
              </div>
              <div>
                <label
                  className="mb-1 block text-[11px] font-semibold uppercase tracking-wide"
                  style={{ color: S.textMuted }}
                >
                  签名
                </label>
                <input
                  type="text"
                  value={signature}
                  onChange={(e) => setSignature(e.target.value)}
                  placeholder="今晚的月亮很圆"
                  className="w-full rounded-[12px] px-3 py-2 text-[13px] italic outline-none"
                  style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
                />
              </div>
            </div>
          </div>
        </div>

        {/* Basic info */}
        <div
          className="rounded-[20px] p-5"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="mb-1.5 flex items-center justify-between">
            <label
              className="text-[11px] font-semibold uppercase tracking-wide"
              style={{ color: S.textMuted }}
            >
              基本信息
            </label>
            <div className="flex items-center gap-2">
              <button
                className="flex h-7 w-7 items-center justify-center rounded-full"
                style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg }}
                onClick={() => infoFileRef.current?.click()}
                title="从文件导入"
              >
                <FileText size={13} style={{ color: S.textMuted }} />
              </button>
              <input
                ref={infoFileRef}
                type="file"
                accept=".txt,.md,.text"
                className="hidden"
                onChange={handleInfoFile}
              />
              <button
                className="flex h-7 w-7 items-center justify-center rounded-full"
                style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg }}
                onClick={() => setFullscreen(true)}
                title="全屏编辑"
              >
                <Maximize2 size={13} style={{ color: S.accentDark }} />
              </button>
            </div>
          </div>
          <textarea
            value={basicInfo}
            onChange={(e) => setBasicInfo(e.target.value)}
            placeholder="介绍一下自己..."
            rows={8}
            className="w-full rounded-[14px] px-4 py-3 text-[14px] resize-none outline-none"
            style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
          />
        </div>

        </>)}
      </div>

      {fullscreen && (
        <FullscreenEditor
          value={basicInfo}
          onChange={setBasicInfo}
          onClose={() => setFullscreen(false)}
        />
      )}

      {toast && (
        <div className="pointer-events-none fixed inset-x-0 top-1/2 z-[200] flex justify-center">
          <div
            className="rounded-2xl px-6 py-3 text-[14px] font-medium text-white"
            style={{ background: "rgba(0,0,0,0.75)" }}
          >
            {toast}
          </div>
        </div>
      )}
    </div>
  );
}
