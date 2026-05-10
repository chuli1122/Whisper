import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Save } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const GROUPS = [
  {
    label: "长消息模式",
    hint: "TG 长叙事文风（主动消息也用）",
    versions: { "4.7": "long_mode", "4.6": "long_mode_legacy" },
    suffix: { key: "long_mode_suffix", hint: "仅普通聊天拼在后面；主动消息不用" },
  },
  {
    label: "短消息模式",
    hint: "QQ/微信 口语化（主动消息也用）",
    versions: { "4.7": "short_mode", "4.6": "short_mode_legacy" },
    suffix: { key: "short_mode_suffix", hint: "仅普通聊天拼在后面；主动消息不用。{short_max} 是变量占位" },
  },
  {
    label: "通用提醒",
    hint: "scratchpad · NO_MESSAGE 规则",
    fields: [{ key: "important_notice" }],
  },
  {
    label: "主动消息场景说明",
    hint: "助手A主动开口时贴的说明",
    fields: [{ key: "proactive_extra" }],
  },
  {
    label: "主动消息触发 · 首次",
    hint: "用户离开后第一次触发的系统提醒",
    fields: [{ key: "trigger_first" }],
  },
  {
    label: "主动消息触发 · 续发",
    hint: "已经主动过后再触发的系统提醒",
    fields: [{ key: "trigger_followup" }],
  },
  {
    label: "群聊艾特通知",
    hint: "助手A在🐰群被艾特时收到的 trigger",
    fields: [{ key: "cafe_trigger_header" }],
  },
  {
    label: "QQ 群艾特通知",
    hint: "助手A在 QQ 群被用户艾特时收到的 trigger",
    fields: [{ key: "qq_group_trigger_header" }],
  },
];

function TextArea({ value, onChange }) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={6}
      className="w-full rounded-[12px] p-3 text-[13px] leading-relaxed outline-none resize-y"
      style={{
        background: S.bg,
        color: S.text,
        boxShadow: "var(--inset-shadow)",
        minHeight: 100,
        fontFamily: "inherit",
        boxSizing: "border-box",
      }}
    />
  );
}

function VersionToggle({ versions, active, onChange }) {
  const keys = Object.keys(versions);
  return (
    <div
      className="flex items-center rounded-full p-0.5"
      style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}
    >
      {keys.map((v) => {
        const isActive = v === active;
        return (
          <button
            key={v}
            onClick={() => onChange(v)}
            className="rounded-full px-3 py-1 text-[11px] font-semibold transition-all"
            style={{
              color: isActive ? "#fff" : S.textMuted,
              background: isActive
                ? "linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%)"
                : "transparent",
              boxShadow: isActive ? "var(--card-shadow-sm)" : "none",
              minWidth: 36,
            }}
          >
            {v}
          </button>
        );
      })}
    </div>
  );
}

export default function PromptEditor() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [defaults, setDefaults] = useState(null);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState(null);
  const [activeVersions, setActiveVersions] = useState({});

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => {
    apiFetch("/api/settings/prompts")
      .then(setData)
      .catch(() => showToast("加载失败"));
    apiFetch("/api/settings/prompts/defaults")
      .then(setDefaults)
      .catch(() => {});
  }, []);

  const update = (key) => (val) => {
    setData((prev) => ({ ...prev, [key]: val }));
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    try {
      const fresh = await apiFetch("/api/settings/prompts", {
        method: "PUT",
        body: data,
      });
      setData(fresh);
      showToast("已保存");
    } catch (e) {
      showToast("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const resetField = (key) => () => {
    if (!defaults) return;
    setData((prev) => ({ ...prev, [key]: defaults[key] }));
  };

  const setVersion = (label) => (v) => {
    setActiveVersions((prev) => ({ ...prev, [label]: v }));
  };

  if (!data) {
    return (
      <div
        className="flex h-full items-center justify-center"
        style={{ background: S.bg, color: S.textMuted }}
      >
        加载中...
      </div>
    );
  }

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
          onClick={() => navigate("/settings")}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>提示词</h1>
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{
            background: S.bg,
            boxShadow: saving ? "var(--inset-shadow)" : "var(--card-shadow-sm)",
          }}
          onClick={save}
          disabled={saving}
        >
          <Save size={18} style={{ color: S.accentDark }} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5 space-y-4">
        {GROUPS.map((group) => {
          const hasVersions = !!group.versions;
          const activeVer = activeVersions[group.label] || (hasVersions ? Object.keys(group.versions)[0] : null);
          const mainKey = hasVersions ? group.versions[activeVer] : group.fields[0].key;
          const otherFields = hasVersions ? [] : group.fields.slice(1);
          return (
            <div
              key={group.label}
              className="rounded-[20px] p-4 space-y-3"
              style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1">
                  <div className="text-[15px] font-semibold" style={{ color: S.text }}>{group.label}</div>
                  {group.hint && <div className="text-[11px]" style={{ color: S.textMuted }}>{group.hint}</div>}
                </div>
                {hasVersions && (
                  <VersionToggle
                    versions={group.versions}
                    active={activeVer}
                    onChange={setVersion(group.label)}
                  />
                )}
              </div>
              {/* main field */}
              <div className="space-y-1">
                <TextArea value={data[mainKey] ?? ""} onChange={update(mainKey)} />
                {defaults && defaults[mainKey] !== data[mainKey] && (
                  <button
                    onClick={resetField(mainKey)}
                    className="text-[11px] underline"
                    style={{ color: S.textMuted }}
                  >
                    恢复默认
                  </button>
                )}
              </div>
              {/* suffix (only for long/short mode groups) */}
              {group.suffix && (
                <div className="space-y-1">
                  {group.suffix.hint && (
                    <div className="text-[11px]" style={{ color: S.textMuted }}>{group.suffix.hint}</div>
                  )}
                  <TextArea value={data[group.suffix.key] ?? ""} onChange={update(group.suffix.key)} />
                  {defaults && defaults[group.suffix.key] !== data[group.suffix.key] && (
                    <button
                      onClick={resetField(group.suffix.key)}
                      className="text-[11px] underline"
                      style={{ color: S.textMuted }}
                    >
                      恢复默认
                    </button>
                  )}
                </div>
              )}
              {/* extra fields (for groups without versions/suffix, not currently used) */}
              {otherFields.map((f) => (
                <div key={f.key} className="space-y-1">
                  {f.hint && <div className="text-[11px]" style={{ color: S.textMuted }}>{f.hint}</div>}
                  <TextArea value={data[f.key] ?? ""} onChange={update(f.key)} />
                  {defaults && defaults[f.key] !== data[f.key] && (
                    <button
                      onClick={resetField(f.key)}
                      className="text-[11px] underline"
                      style={{ color: S.textMuted }}
                    >
                      恢复默认
                    </button>
                  )}
                </div>
              ))}
            </div>
          );
        })}
      </div>

      {toast && (
        <div
          className="fixed bottom-8 left-1/2 -translate-x-1/2 rounded-full px-5 py-2.5 text-[13px] font-semibold shadow-lg"
          style={{ background: S.bg, color: S.text, boxShadow: "var(--card-shadow)" }}
        >
          {toast}
        </div>
      )}
    </div>
  );
}
