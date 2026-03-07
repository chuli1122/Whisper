import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Save, Eye, EyeOff } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

function NmInput({ label, value, onChange, placeholder, password }) {
  const [show, setShow] = useState(false);
  return (
    <div className="mb-4 last:mb-0">
      {label && (
        <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
          {label}
        </label>
      )}
      <div className="relative">
        <input
          type={password && !show ? "password" : "text"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full rounded-[14px] px-4 py-3 text-[14px] outline-none pr-12"
          style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
        />
        {password && (
          <button type="button" className="absolute right-3 top-1/2 -translate-y-1/2" onClick={() => setShow(!show)}>
            {show ? <Eye size={16} style={{ color: S.textMuted }} /> : <EyeOff size={16} style={{ color: S.textMuted }} />}
          </button>
        )}
      </div>
    </div>
  );
}

function Card({ children }) {
  return (
    <div className="mb-4 rounded-[20px] p-5" style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}>
      {children}
    </div>
  );
}

function Toggle({ on, onToggle }) {
  return (
    <button
      onClick={onToggle}
      className="relative flex h-7 w-12 shrink-0 items-center rounded-full"
      style={{
        boxShadow: "var(--inset-shadow)",
        background: on ? "var(--accent)" : S.bg,
        transition: "background 0.2s",
      }}
    >
      <span
        className="absolute h-5 w-5 rounded-full"
        style={{
          left: on ? "calc(100% - 22px)" : "2px",
          background: "white",
          boxShadow: "2px 2px 5px rgba(174,176,182,0.5)",
          transition: "left 0.2s ease",
        }}
      />
    </button>
  );
}

export default function VoiceSettings() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState(null);
  const [voiceEnabled, setVoiceEnabled] = useState(false);
  const [settings, setSettings] = useState({
    tts_api_key: "",
    tts_group_id: "",
    tts_voice_id: "",
    tts_model: "speech-02-hd",
    stt_fallback_key: "",
  });

  const showToast = useCallback((msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  }, []);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings/voice").then((d) => setSettings(d)),
      apiFetch("/api/settings/proactive").then((d) => setVoiceEnabled(d.voice_enabled)),
    ])
      .catch((e) => showToast("加载失败: " + e.message))
      .finally(() => setLoading(false));
  }, [showToast]);

  const toggleVoice = async () => {
    const next = !voiceEnabled;
    setVoiceEnabled(next);
    try {
      await apiFetch("/api/settings/proactive", { method: "PUT", body: { voice_enabled: next } });
    } catch (_) {}
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await apiFetch("/api/settings/voice", {
        method: "PUT",
        body: settings,
      });
      setSettings(res);
      showToast("已保存");
    } catch (e) {
      showToast("保存失败: " + e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex h-full flex-col" style={{ background: "var(--bg-deeper)" }}>
      {/* Toast */}
      {toast && (
        <div className="fixed left-1/2 top-6 z-50 -translate-x-1/2 rounded-2xl px-5 py-2.5 text-[13px] font-semibold"
          style={{ background: S.bg, color: S.text, boxShadow: "var(--card-shadow)" }}>
          {toast}
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between px-5 pb-2 pt-4">
        <button className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => navigate("/settings")}>
          <ChevronLeft size={20} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>语音设置</h1>
        <button className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: saving ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }}
          onClick={handleSave} disabled={saving}>
          <Save size={18} style={{ color: S.accentDark }} />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5">
        {loading ? (
          <div className="flex justify-center py-20">
            <div className="h-8 w-8 animate-spin rounded-full border-2"
              style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : (
          <>
            {/* Voice master switch */}
            <Card>
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[14px] font-semibold" style={{ color: S.text }}>语音消息</div>
                  <div className="text-[11px]" style={{ color: S.textMuted }}>开启后 AI 回复可附带语音</div>
                </div>
                <Toggle on={voiceEnabled} onToggle={toggleVoice} />
              </div>
            </Card>

            {/* TTS Settings */}
            <Card>
              <div className="mb-4 text-[14px] font-semibold" style={{ color: S.text }}>
                TTS 设置 (文字转语音)
              </div>
              <NmInput label="MINIMAX API KEY" value={settings.tts_api_key}
                onChange={(v) => setSettings({ ...settings, tts_api_key: v })} password placeholder="输入 API Key" />
              <NmInput label="GROUP ID" value={settings.tts_group_id}
                onChange={(v) => setSettings({ ...settings, tts_group_id: v })} placeholder="MiniMax Group ID" />
              <NmInput label="VOICE ID (克隆音色)" value={settings.tts_voice_id}
                onChange={(v) => setSettings({ ...settings, tts_voice_id: v })} placeholder="voice_id" />
              <NmInput label="MODEL" value={settings.tts_model}
                onChange={(v) => setSettings({ ...settings, tts_model: v })} placeholder="speech-02-hd" />
            </Card>

            {/* STT Settings */}
            <Card>
              <div className="mb-2 text-[14px] font-semibold" style={{ color: S.text }}>
                STT 设置 (语音转文字)
              </div>
              <div className="mb-4 text-[11px]" style={{ color: S.textMuted }}>
                主要：Groq API（快速，免费额度足够日常使用）<br />
                备用：本地 Whisper Turbo（终端在线 + Groq 失败时降级）
              </div>
              <NmInput label="GROQ API KEY" value={settings.stt_fallback_key}
                onChange={(v) => setSettings({ ...settings, stt_fallback_key: v })} password placeholder="gsk_..." />
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
