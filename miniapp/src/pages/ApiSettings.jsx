import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  ChevronLeft, ChevronDown, Plus, Trash2, Pencil,
  Eye, EyeOff, RefreshCw, Loader2, Save, Copy, Check,
} from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const ANTHROPIC_MODELS = [
  "claude-opus-4-6",
  "claude-opus-4-5-20251101",
  "claude-sonnet-4-6",
  "claude-sonnet-4-5-20250929",
  "claude-haiku-4-5-20251001",
];

function domainFromUrl(url) {
  try { return new URL(url).hostname; } catch (_e) { return url; }
}

// ── NM Input ──
function NmInput({ label, value, onChange, placeholder, password, disabled }) {
  const [show, setShow] = useState(false);
  return (
    <div className="mb-4">
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
          disabled={disabled}
          className="w-full rounded-[14px] px-4 py-3 text-[14px] outline-none pr-12"
          style={{
            boxShadow: "var(--inset-shadow)",
            background: S.bg,
            color: disabled ? S.textMuted : S.text,
            opacity: disabled ? 0.5 : 1,
          }}
        />
        {password && (
          <button
            type="button"
            className="absolute right-3 top-1/2 -translate-y-1/2"
            onClick={() => setShow(!show)}
          >
            {show ? <Eye size={16} style={{ color: S.textMuted }} /> : <EyeOff size={16} style={{ color: S.textMuted }} />}
          </button>
        )}
      </div>
    </div>
  );
}

// ── Segment control ──
function SegmentControl({ value, onChange, options }) {
  return (
    <div
      className="flex rounded-[14px] p-1"
      style={{ boxShadow: "var(--inset-shadow)", background: S.bg }}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          className="flex-1 rounded-[10px] py-2 text-[12px] font-semibold transition-all"
          style={{
            background: value === opt.value ? S.bg : "transparent",
            boxShadow: value === opt.value ? "var(--card-shadow-sm)" : "none",
            color: value === opt.value ? S.accentDark : S.textMuted,
          }}
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

// ── Model selector (dropdown with list) ──
function ModelDropdown({ value, onChange, options, placeholder }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        className="flex w-full items-center justify-between rounded-[14px] px-4 py-3 text-[14px] text-left"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: value ? S.text : S.textMuted }}
        onClick={() => setOpen(!open)}
      >
        <span className="truncate">{value || placeholder || "请先拉取模型列表"}</span>
        <ChevronDown size={16} style={{ color: S.textMuted, flexShrink: 0, transform: open ? "rotate(180deg)" : "none" }} />
      </button>
      {open && (
        <div
          className="absolute left-0 right-0 top-full z-40 mt-1 max-h-[200px] overflow-y-auto rounded-[14px]"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {options.map((m) => (
            <button
              key={m}
              className="flex w-full items-center justify-between px-4 py-3 text-[13px]"
              style={{ color: S.text }}
              onClick={() => { onChange(m); setOpen(false); }}
            >
              <span className="truncate">{m}</span>
              {m === value && <Check size={12} style={{ color: S.accentDark, flexShrink: 0 }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Anthropic model input (text + preset dropdown) ──
function AnthropicModelInput({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <div className="relative">
        <input
          type="text"
          placeholder="输入或选择模型名称"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-[14px] px-4 py-3 pr-10 text-[14px] outline-none"
          style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
        />
        <button
          type="button"
          className="absolute right-3 top-1/2 -translate-y-1/2"
          onClick={() => setOpen(!open)}
        >
          <ChevronDown size={16} style={{ color: S.textMuted }} />
        </button>
      </div>
      {open && (
        <div
          className="absolute left-0 right-0 top-full z-40 mt-1 max-h-[200px] overflow-y-auto rounded-[14px]"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {ANTHROPIC_MODELS.map((m) => (
            <button
              key={m}
              className="flex w-full px-4 py-3 text-left text-[13px]"
              style={{ color: S.text }}
              onClick={() => { onChange(m); setOpen(false); }}
            >
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Number field ──
function NumberField({ value, onChange, min, max, step }) {
  const [val, setVal] = useState(String(value));
  useEffect(() => setVal(String(value)), [value]);
  const commit = () => {
    let n = parseFloat(val);
    if (isNaN(n)) n = min;
    n = Math.max(min, Math.min(max, n));
    n = Math.round(n / step) * step;
    n = parseFloat(n.toFixed(4));
    setVal(String(n));
    onChange(n);
  };
  return (
    <input
      type="number"
      value={val}
      onChange={(e) => setVal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
      step={step}
      min={min}
      max={max}
      className="w-20 rounded-[10px] py-2 text-center text-[13px] font-bold outline-none"
      style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
    />
  );
}

// ── Toggle ──
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

// ── Card Section ──
function Card({ children, className = "" }) {
  return (
    <div
      className={`rounded-[20px] p-5 mb-4 space-y-4 ${className}`}
      style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
    >
      {children}
    </div>
  );
}

// ── Main ──
export default function ApiSettings() {
  const navigate = useNavigate();

  const [providers, setProviders] = useState([]);
  const [presets, setPresets] = useState([]);
  const [loading, setLoading] = useState(true);

  // Form
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [authType, setAuthType] = useState("api_key");
  const [modelName, setModelName] = useState("");
  const [modelOptions, setModelOptions] = useState([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [temperature, setTemperature] = useState(1.0);
  const [tempEnabled, setTempEnabled] = useState(false);
  const [topP, setTopP] = useState(1.0);
  const [topPEnabled, setTopPEnabled] = useState(false);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [editingPreset, setEditingPreset] = useState(null);

  // Thinking budget (per preset)
  const [thinkingBudget, setThinkingBudget] = useState(0);

  // UI
  const [toast, setToast] = useState(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [nameModalOpen, setNameModalOpen] = useState(false);
  const [presetName, setPresetName] = useState("");
  const [saveAsNew, setSaveAsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [renameTarget, setRenameTarget] = useState(null);
  const [renameValue, setRenameValue] = useState("");
  const longPressTimer = useRef(null);
  const longPressTriggered = useRef(false);
  const pressStartPos = useRef(null);

  const formRef = useRef({});
  formRef.current = { baseUrl, apiKey, authType, modelName, temperature, tempEnabled, topP, topPEnabled, maxTokens, thinkingBudget, editingPreset, providers };

  const showToast = useCallback((msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  }, []);

  const fetchAll = useCallback(async () => {
    try {
      const [provRes, presRes] = await Promise.all([
        apiFetch("/api/providers"),
        apiFetch("/api/presets"),
      ]);
      setProviders(provRes.providers || []);
      setPresets(presRes.presets || []);
    } catch (e) {
      showToast("加载失败: " + e.message);
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const loadPreset = (preset) => {
    setEditingPreset(preset);
    setModelName(preset.model_name);
    setTempEnabled(preset.temperature != null);
    setTemperature(preset.temperature ?? 1.0);
    setTopPEnabled(preset.top_p != null);
    setTopP(preset.top_p ?? 1.0);
    setMaxTokens(preset.max_tokens);
    setThinkingBudget(preset.thinking_budget || 0);
    const provider = providers.find((p) => p.id === preset.api_provider_id);
    if (provider) {
      setBaseUrl(provider.base_url);
      setApiKey(provider.api_key);
      setAuthType(provider.auth_type || "api_key");
    }
  };

  const resetForm = () => {
    setEditingPreset(null);
    setBaseUrl("");
    setApiKey("");
    setAuthType("api_key");
    setModelName("");
    setTemperature(1.0);
    setTempEnabled(false);
    setTopP(1.0);
    setTopPEnabled(false);
    setMaxTokens(4096);
    setThinkingBudget(0);
    setModelOptions([]);
  };

  const handleFetchModels = async () => {
    if (!baseUrl.trim() || !apiKey.trim()) { showToast("请先填写 API 地址和 Key"); return; }

    setFetchingModels(true);
    try {
      const res = await fetch(`${baseUrl.trim().replace(/\/+$/, "")}/models`, {
        headers: { Authorization: `Bearer ${apiKey.trim()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const models = (data.data || []).map((m) => m.id).sort();
      setModelOptions(models);
      showToast(models.length ? `获取到 ${models.length} 个模型` : "未找到模型");
    } catch (e) {
      showToast("拉取失败: " + e.message);
    } finally {
      setFetchingModels(false);
    }
  };

  const handleTestConnection = async () => {
    if (!baseUrl.trim() || !apiKey.trim() || !modelName.trim()) {
      showToast("请先填写 API 地址、Key 和模型名称"); return;
    }
    setTestingConnection(true);
    try {
      const url = baseUrl.trim().replace(/\/+$/, "");
      const hdrs = {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey.trim()}`,
      };
      if (authType === "oauth_token") {
        hdrs["anthropic-beta"] = "claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14";
        hdrs["anthropic-dangerous-direct-browser-access"] = "true";
        hdrs["x-app"] = "cli";
      }
      const res = await fetch(`${url}/chat/completions`, {
        method: "POST",
        headers: hdrs,
        body: JSON.stringify({ model: modelName.trim(), max_tokens: 10, messages: [{ role: "user", content: "Hi" }] }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error?.message || `HTTP ${res.status}`);
      }
      showToast("✓ 连接成功");
    } catch (e) {
      showToast("连接失败: " + e.message);
    } finally {
      setTestingConnection(false);
    }
  };

  const resolveProvider = async (currentProviders) => {
    const f = formRef.current;
    const url = f.baseUrl.trim();
    const key = f.apiKey.trim();
    const at = f.authType;
    const existing = currentProviders.find((p) => p.base_url === url);
    if (existing) {
      if (existing.api_key !== key || existing.auth_type !== at) {
        await apiFetch(`/api/providers/${existing.id}`, { method: "PUT", body: { api_key: key, auth_type: at } });
      }
      return existing.id;
    }
    const res = await apiFetch("/api/providers", {
      method: "POST",
      body: { name: domainFromUrl(url), base_url: url, api_key: key, auth_type: at },
    });
    return res.id;
  };

  const doSave = async (name, asNew) => {
    const f = formRef.current;
    if (!f.baseUrl.trim() || !f.apiKey.trim() || !f.modelName.trim()) {
      showToast("请填写完整信息"); return;
    }
    setSaving(true);
    try {
      const providerId = await resolveProvider(f.providers);
      const body = {
        name,
        model_name: f.modelName.trim(),
        temperature: f.tempEnabled ? f.temperature : null,
        top_p: f.topPEnabled ? f.topP : null,
        max_tokens: Number(f.maxTokens) || 4096,
        thinking_budget: Number(f.thinkingBudget) || 0,
        api_provider_id: providerId,
      };
      if (f.editingPreset && !asNew) {
        await apiFetch(`/api/presets/${f.editingPreset.id}`, { method: "PUT", body });
        showToast("预设已更新");
      } else {
        await apiFetch("/api/presets", { method: "POST", body });
        showToast("预设已保存");
      }
      await fetchAll();
    } catch (e) {
      showToast("保存失败: " + e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleNameConfirm = () => {
    const name = presetName.trim();
    if (!name) return;
    setNameModalOpen(false);
    doSave(name, saveAsNew);
  };

  const handleSaveEdit = () => {
    if (editingPreset) doSave(editingPreset.name, false);
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      await apiFetch(`/api/presets/${deleteTarget.id}`, { method: "DELETE" });
      showToast("已删除");
      if (editingPreset?.id === deleteTarget.id) resetForm();
      fetchAll();
    } catch (e) {
      showToast("删除失败: " + e.message);
    }
    setDeleteTarget(null);
  };

  const confirmRename = async () => {
    const name = renameValue.trim();
    if (!name || !renameTarget) return;
    try {
      await apiFetch(`/api/presets/${renameTarget.id}`, { method: "PUT", body: { name } });
      showToast("已重命名");
      fetchAll();
    } catch (_e) { showToast("重命名失败"); }
    setRenameTarget(null);
  };

  const handlePressStart = (e, preset) => {
    longPressTriggered.current = false;
    const t = e.touches ? e.touches[0] : e;
    pressStartPos.current = { x: t.clientX, y: t.clientY };
    longPressTimer.current = setTimeout(() => {
      longPressTriggered.current = true;
      setDeleteTarget(preset);
    }, 600);
  };

  const handlePressMove = (e) => {
    if (!pressStartPos.current || !longPressTimer.current) return;
    const t = e.touches ? e.touches[0] : e;
    const dx = t.clientX - pressStartPos.current.x;
    const dy = t.clientY - pressStartPos.current.y;
    if (dx * dx + dy * dy > 100) { clearTimeout(longPressTimer.current); longPressTimer.current = null; }
  };

  const handlePressEnd = () => clearTimeout(longPressTimer.current);

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
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>API 设置</h1>
        <div className="w-10" />
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5">
        {loading ? (
          <div className="flex justify-center py-20">
            <div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : (
          <>
            {/* Connection */}
            <Card>
              <NmInput label="API 地址" value={baseUrl} onChange={setBaseUrl} placeholder="https://api.openai.com/v1" disabled={authType === "oauth_token"} />
              <NmInput label="API Key" value={apiKey} onChange={setApiKey} placeholder={authType === "oauth_token" ? "sk-ant-oat01-..." : "sk-..."} password />
              <div>
                <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>认证方式</label>
                <SegmentControl
                  value={authType}
                  onChange={(v) => { setAuthType(v); if (v === "oauth_token") setBaseUrl("https://api.anthropic.com/v1"); }}
                  options={[
                    { value: "api_key", label: "标准 API Key" },
                    { value: "oauth_token", label: "Setup Token" },
                  ]}
                />
              </div>
            </Card>

            {/* Model */}
            <Card>
              <div>
                <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>模型名称</label>
                {authType === "oauth_token" ? (
                  <AnthropicModelInput value={modelName} onChange={setModelName} />
                ) : (
                  <ModelDropdown value={modelName} onChange={setModelName} options={modelOptions} />
                )}
              </div>
              <div className="flex gap-3">
                <button
                  className="flex flex-1 items-center justify-center gap-2 rounded-[14px] py-3 text-[13px] font-semibold"
                  style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }}
                  onClick={handleTestConnection}
                  disabled={testingConnection}
                >
                  {testingConnection ? <Loader2 size={14} className="animate-spin" /> : null}
                  测试连接
                </button>
                <button
                  className="flex flex-1 items-center justify-center gap-2 rounded-[14px] py-3 text-[13px] font-semibold"
                  style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text, opacity: authType === "oauth_token" ? 0.4 : 1 }}
                  onClick={handleFetchModels}
                  disabled={fetchingModels || authType === "oauth_token"}
                >
                  {fetchingModels ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
                  拉取模型
                </button>
              </div>
            </Card>

            {/* Parameters */}
            <Card>
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[14px] font-semibold" style={{ color: S.text }}>温度 (0-2)</div>
                  <div className="text-[11px]" style={{ color: S.textMuted }}>越低越稳定，越高越有创意</div>
                </div>
                <Toggle on={tempEnabled} onToggle={() => setTempEnabled(!tempEnabled)} />
              </div>
              {tempEnabled && (
                <div className="flex items-center justify-between">
                  <span className="text-[12px]" style={{ color: S.textMuted }}>值</span>
                  <NumberField value={temperature} onChange={setTemperature} min={0} max={2} step={0.1} />
                </div>
              )}
              <div className="h-px" style={{ background: "rgba(136,136,160,0.15)" }} />
              <div className="flex items-center justify-between">
                <span className="text-[14px] font-semibold" style={{ color: S.text }}>Top P (0-1)</span>
                <Toggle on={topPEnabled} onToggle={() => setTopPEnabled(!topPEnabled)} />
              </div>
              {topPEnabled && (
                <div className="flex items-center justify-between">
                  <span className="text-[12px]" style={{ color: S.textMuted }}>值</span>
                  <NumberField value={topP} onChange={setTopP} min={0} max={1} step={0.05} />
                </div>
              )}
              <div className="h-px" style={{ background: "rgba(136,136,160,0.15)" }} />
              <div className="flex items-center justify-between">
                <span className="text-[14px] font-semibold" style={{ color: S.text }}>Max Tokens</span>
                <NumberField value={maxTokens} onChange={setMaxTokens} min={128} max={200000} step={1} />
              </div>
              <div className="h-px" style={{ background: "rgba(136,136,160,0.15)" }} />
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[14px] font-semibold" style={{ color: S.text }}>思考预算</div>
                  <div className="text-[11px]" style={{ color: S.textMuted }}>Extended Thinking tokens</div>
                </div>
                <select
                  className="w-20 rounded-[10px] py-2 text-center text-[13px] font-bold outline-none appearance-none"
                  style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text, WebkitAppearance: "none", textAlignLast: "center" }}
                  value={thinkingBudget}
                  onChange={(e) => setThinkingBudget(Number(e.target.value))}
                >
                  <option value={0}>关闭</option>
                  <option value={1024}>1024</option>
                  <option value={2048}>2048</option>
                  <option value={4096}>4096</option>
                  <option value={8192}>8192</option>
                  <option value={16384}>16384</option>
                </select>
              </div>
            </Card>

            {/* Save */}
            <div className="mb-4 space-y-3">
              {editingPreset ? (
                <div className="flex gap-3">
                  <button
                    className="flex flex-1 items-center justify-center gap-2 rounded-[18px] py-3.5 text-[14px] font-bold text-white"
                    style={{ background: "linear-gradient(135deg, var(--accent), var(--accent-dark))", boxShadow: "4px 4px 10px rgba(201,98,138,0.35)" }}
                    onClick={handleSaveEdit}
                    disabled={saving}
                  >
                    <Save size={14} />
                    {saving ? "保存中..." : "保存修改"}
                  </button>
                  <button
                    className="flex flex-1 items-center justify-center gap-2 rounded-[18px] py-3.5 text-[14px] font-bold"
                    style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }}
                    onClick={() => { setSaveAsNew(true); setPresetName(""); setNameModalOpen(true); }}
                    disabled={saving}
                  >
                    <Copy size={14} />
                    另存为
                  </button>
                </div>
              ) : (
                <button
                  className="flex w-full items-center justify-center gap-2 rounded-[18px] py-3.5 text-[14px] font-bold text-white"
                  style={{ background: "linear-gradient(135deg, var(--accent), var(--accent-dark))", boxShadow: "4px 4px 10px rgba(201,98,138,0.35)" }}
                  onClick={() => { setSaveAsNew(true); setPresetName(""); setNameModalOpen(true); }}
                  disabled={saving}
                >
                  <Plus size={14} />
                  保存为预设
                </button>
              )}
              {editingPreset && (
                <button
                  className="w-full py-1 text-center text-[12px]"
                  style={{ color: S.textMuted }}
                  onClick={resetForm}
                >
                  取消编辑，清空表单
                </button>
              )}
            </div>

            {/* Preset list */}
            <div>
              <div className="mb-3 text-[12px] font-bold uppercase tracking-wide" style={{ color: S.textMuted }}>已保存的预设</div>
              {presets.length === 0 ? (
                <div className="rounded-[20px] py-8 text-center text-[13px]" style={{ background: S.bg, boxShadow: "var(--card-shadow)", color: S.textMuted }}>
                  暂无预设
                </div>
              ) : (
                <div className="space-y-3">
                  {presets.map((p) => (
                    <div
                      key={p.id}
                      className="rounded-[18px] p-4"
                      style={{
                        background: S.bg,
                        boxShadow: editingPreset?.id === p.id ? "var(--inset-shadow)" : "var(--card-shadow-sm)",
                      }}
                      onClick={() => { if (!longPressTriggered.current) loadPreset(p); }}
                      onTouchStart={(e) => handlePressStart(e, p)}
                      onTouchMove={handlePressMove}
                      onTouchEnd={handlePressEnd}
                      onMouseDown={(e) => handlePressStart(e, p)}
                      onMouseMove={handlePressMove}
                      onMouseUp={handlePressEnd}
                    >
                      <div className="flex items-center justify-between">
                        <div className="text-[14px] font-bold truncate" style={{ color: editingPreset?.id === p.id ? S.accentDark : S.text }}>
                          {p.name}
                        </div>
                        <Pencil size={12} style={{ color: S.textMuted, flexShrink: 0, marginLeft: 8 }} />
                      </div>
                      <div className="mt-0.5 truncate text-[11px]" style={{ color: S.textMuted }}>{p.model_name}</div>
                      <div className="mt-1 text-[10px] font-mono" style={{ color: S.textMuted }}>
                        T: {p.temperature ?? "默认"} | Top-P: {p.top_p ?? "默认"} | Max: {p.max_tokens}{p.thinking_budget ? ` | Think: ${p.thinking_budget}` : ""}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* Name modal */}
      {nameModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.25)" }}
          onClick={() => setNameModalOpen(false)}
        >
          <div
            className="mx-6 w-full max-w-[300px] rounded-[22px] p-6"
            style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <p className="mb-4 text-center text-[16px] font-bold" style={{ color: S.text }}>预设名称</p>
            <input
              type="text"
              value={presetName}
              onChange={(e) => setPresetName(e.target.value)}
              placeholder="如：日常聊天、写作模式"
              className="w-full rounded-[14px] px-4 py-3 text-[15px] outline-none mb-4"
              style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && handleNameConfirm()}
            />
            <div className="flex gap-3">
              <button className="flex-1 rounded-[16px] py-3 text-[14px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={() => setNameModalOpen(false)}>取消</button>
              <button className="flex-1 rounded-[16px] py-3 text-[14px] font-bold text-white" style={{ background: presetName.trim() ? "linear-gradient(135deg, var(--accent), var(--accent-dark))" : "rgba(201,98,138,0.3)" }} onClick={handleNameConfirm} disabled={!presetName.trim()}>保存</button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirm */}
      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.25)" }}
          onClick={() => setDeleteTarget(null)}
        >
          <div
            className="mx-6 w-full max-w-[300px] rounded-[22px] p-6"
            style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>删除预设</p>
            <p className="mb-5 text-center text-[13px]" style={{ color: S.textMuted }}>确定要删除「{deleteTarget.name}」吗？</p>
            <div className="flex gap-3">
              <button className="flex-1 rounded-[16px] py-3 text-[14px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={() => setDeleteTarget(null)}>取消</button>
              <button className="flex-1 rounded-[16px] py-3 text-[14px] font-bold text-white" style={{ background: "#ff4d6d", boxShadow: "4px 4px 10px rgba(255,77,109,0.4)" }} onClick={confirmDelete}>删除</button>
            </div>
          </div>
        </div>
      )}

      {/* Rename modal */}
      {renameTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={() => setRenameTarget(null)}>
          <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-6" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
            <p className="mb-4 text-center text-[16px] font-bold" style={{ color: S.text }}>修改名称</p>
            <input autoFocus type="text" value={renameValue} onChange={(e) => setRenameValue(e.target.value)} className="w-full rounded-[14px] px-4 py-3 text-[15px] outline-none mb-4" style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }} onKeyDown={(e) => e.key === "Enter" && confirmRename()} />
            <div className="flex gap-3">
              <button className="flex-1 rounded-[16px] py-3 text-[14px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={() => setRenameTarget(null)}>取消</button>
              <button className="flex-1 rounded-[16px] py-3 text-[14px] font-bold text-white" style={{ background: "linear-gradient(135deg, var(--accent), var(--accent-dark))" }} onClick={confirmRename}>保存</button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="pointer-events-none fixed inset-x-0 top-1/2 z-[200] flex justify-center">
          <div className="rounded-2xl px-6 py-3 text-[14px] font-medium text-white" style={{ background: "rgba(0,0,0,0.75)" }}>{toast}</div>
        </div>
      )}
    </div>
  );
}
