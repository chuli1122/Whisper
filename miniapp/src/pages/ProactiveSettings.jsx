import { useState, useEffect, useCallback } from "react";
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

function Toggle({ on, onToggle, disabled }) {
  return (
    <button
      onClick={() => !disabled && onToggle()}
      className="relative flex h-7 w-12 shrink-0 items-center rounded-full"
      style={{
        boxShadow: "var(--inset-shadow)",
        background: on ? "var(--accent)" : S.bg,
        transition: "background 0.2s",
        opacity: disabled ? 0.4 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
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

function NumberField({ label, hint, value, onChange, min, max, step = 1, unit, disabled }) {
  const [val, setVal] = useState(String(value));

  useEffect(() => setVal(String(value)), [value]);

  const commit = () => {
    let n = parseFloat(val);
    if (isNaN(n)) n = min;
    n = Math.max(min, Math.min(max, n));
    n = Math.round(n / step) * step;
    n = parseFloat(n.toFixed(step < 1 ? 1 : 0));
    setVal(String(n));
    onChange(n);
  };

  return (
    <div
      className="flex items-center justify-between"
      style={{ opacity: disabled ? 0.4 : 1, pointerEvents: disabled ? "none" : "auto" }}
    >
      <div>
        <div className="text-[13px] font-medium" style={{ color: S.text }}>{label}</div>
        {hint && <div className="text-[10px]" style={{ color: S.textMuted }}>{hint}</div>}
      </div>
      <div className="flex items-center gap-1.5">
        <input
          type="number"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()}
          step={step}
          min={min}
          max={max}
          disabled={disabled}
          className="w-16 rounded-[10px] py-2 text-center text-[13px] font-bold outline-none"
          style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
        />
        {unit && <span className="text-[12px]" style={{ color: S.textMuted }}>{unit}</span>}
      </div>
    </div>
  );
}

function RangeField({ label, hint, minValue, maxValue, onMinChange, onMaxChange, min, max, unit }) {
  const [minVal, setMinVal] = useState(String(minValue));
  const [maxVal, setMaxVal] = useState(String(maxValue));

  useEffect(() => setMinVal(String(minValue)), [minValue]);
  useEffect(() => setMaxVal(String(maxValue)), [maxValue]);

  const commit = () => {
    let a = parseInt(minVal);
    let b = parseInt(maxVal);
    if (isNaN(a)) a = min;
    if (isNaN(b)) b = min;
    a = Math.max(min, Math.min(max, a));
    b = Math.max(min, Math.min(max, b));
    if (a > b) [a, b] = [b, a];
    setMinVal(String(a));
    setMaxVal(String(b));
    onMinChange(a);
    onMaxChange(b);
  };

  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="text-[13px] font-medium" style={{ color: S.text }}>{label}</div>
        {hint && <div className="text-[10px]" style={{ color: S.textMuted }}>{hint}</div>}
      </div>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={minVal}
          onChange={(e) => setMinVal(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()}
          min={min}
          max={max}
          className="w-16 rounded-[10px] py-2 text-center text-[13px] font-bold outline-none"
          style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
        />
        <span className="text-[13px]" style={{ color: S.textMuted }}>~</span>
        <input
          type="number"
          value={maxVal}
          onChange={(e) => setMaxVal(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()}
          min={min}
          max={max}
          className="w-16 rounded-[10px] py-2 text-center text-[13px] font-bold outline-none"
          style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
        />
        {unit && <span className="text-[12px]" style={{ color: S.textMuted }}>{unit}</span>}
      </div>
    </div>
  );
}

function Card({ children }) {
  return (
    <div
      className="rounded-[20px] p-5 mb-4 space-y-4"
      style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
    >
      {children}
    </div>
  );
}

function Divider() {
  return <div className="h-px" style={{ background: "rgba(136,136,160,0.15)" }} />;
}

export default function ProactiveSettings() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [settings, setSettings] = useState({
    enabled: false,
    first_enabled: true,
    first_min: 5,
    first_max: 10,
    retry_enabled: true,
    retry_min: 15,
    retry_max: 120,
    max_retries: 8,
    voice_enabled: false,
  });
  const [toast, setToast] = useState(null);

  const showToast = useCallback((msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const data = await apiFetch("/api/settings/proactive");
        setSettings(data);
      } catch (e) {
        showToast("加载失败: " + e.message);
      } finally {
        setLoading(false);
      }
    })();
  }, [showToast]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await apiFetch("/api/settings/proactive", {
        method: "PUT",
        body: settings,
      });
      showToast("已保存");
    } catch (e) {
      showToast("保存失败: " + e.message);
    } finally {
      setSaving(false);
    }
  };

  const update = (patch) => setSettings((prev) => ({ ...prev, ...patch }));

  const disabled = !settings.enabled;

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
          onClick={() => navigate("/settings", { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>
          主动发消息
        </h1>
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

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5">
        {loading ? (
          <div className="flex justify-center py-20">
            <div
              className="h-8 w-8 animate-spin rounded-full border-2"
              style={{ borderColor: S.accent, borderTopColor: "transparent" }}
            />
          </div>
        ) : (
          <>
            {/* Card: Enable toggle + 主动追问 */}
            <Card>
              <div className="flex items-center justify-between">
                <div className="text-[15px] font-semibold" style={{ color: S.text }}>
                  开启
                </div>
                <Toggle on={settings.enabled} onToggle={() => update({ enabled: !settings.enabled })} />
              </div>
              {settings.enabled && (
                <>
                  <Divider />
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-[14px] font-semibold" style={{ color: S.text }}>主动追问</div>
                      <div className="text-[11px]" style={{ color: S.textMuted }}>她停止发消息后主动找她</div>
                    </div>
                    <Toggle on={settings.first_enabled} onToggle={() => update({ first_enabled: !settings.first_enabled })} />
                  </div>
                  {settings.first_enabled && (
                    <RangeField
                      label="间隔（分钟）"
                      hint="1-30"
                      minValue={settings.first_min}
                      maxValue={settings.first_max}
                      onMinChange={(v) => update({ first_min: v })}
                      onMaxChange={(v) => update({ first_max: v })}
                      min={1}
                      max={30}
                    />
                  )}
                </>
              )}
            </Card>

            {settings.enabled && (
              <Card>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-[14px] font-semibold" style={{ color: S.text }}>追发</div>
                    <div className="text-[11px]" style={{ color: S.textMuted }}>关闭 = 不追发，只发一次</div>
                  </div>
                  <Toggle
                    on={settings.retry_enabled}
                    onToggle={() => update({ retry_enabled: !settings.retry_enabled })}
                  />
                </div>
                {settings.retry_enabled && (
                  <>
                    <Divider />
                    <RangeField
                      label="追发间隔（分钟）"
                      hint="5-720"
                      minValue={settings.retry_min}
                      maxValue={settings.retry_max}
                      onMinChange={(v) => update({ retry_min: v })}
                      onMaxChange={(v) => update({ retry_max: v })}
                      min={5}
                      max={720}
                    />
                    <NumberField
                      label="最大追发次数"
                      hint="1-99"
                      value={settings.max_retries}
                      onChange={(v) => update({ max_retries: v })}
                      min={1}
                      max={99}
                    />
                  </>
                )}
              </Card>
            )}

          </>
        )}
      </div>

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
