import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, ChevronRight, Server, Mic2, MessageSquare, Database, Timer, Hash, BookOpen, Repeat } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

function RowLink({ icon, label, hint, onClick }) {
  return (
    <button
      className="flex w-full items-center gap-4 px-4 py-4"
      onClick={onClick}
    >
      <div
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
        style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
      >
        {icon}
      </div>
      <div className="flex-1 text-left">
        <div className="text-[15px] font-semibold" style={{ color: S.text }}>{label}</div>
        {hint && <div className="text-[11px]" style={{ color: S.textMuted }}>{hint}</div>}
      </div>
      <ChevronRight size={16} style={{ color: S.textMuted }} />
    </button>
  );
}

function Divider() {
  return <div className="mx-4 h-px" style={{ background: "rgba(136,136,160,0.15)" }} />;
}

function NumberInput({ value, onChange, min, max }) {
  const [val, setVal] = useState(String(value));

  useEffect(() => setVal(String(value)), [value]);

  const commit = () => {
    let n = parseInt(val);
    if (isNaN(n)) n = min;
    n = Math.max(min, Math.min(max, n));
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
      className="w-20 rounded-[10px] py-2 text-center text-[14px] font-bold outline-none"
      style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
      min={min}
      max={max}
    />
  );
}

export default function Settings() {
  const navigate = useNavigate();

  // localStorage settings
  const [tgBufferSec, setTgBufferSec] = useState(() =>
    parseInt(localStorage.getItem("telegram_buffer_seconds") || "15")
  );
  const [shortMsgMax, setShortMsgMax] = useState(() =>
    parseInt(localStorage.getItem("short_msg_max") || "8")
  );
  const [maxToolRounds, setMaxToolRounds] = useState(() =>
    parseInt(localStorage.getItem("max_tool_rounds") || "15")
  );
  const miscMounted = useRef(false);

  // API settings
  const [retainBudget, setRetainBudget] = useState(8000);
  const [triggerThreshold, setTriggerThreshold] = useState(16000);
  const [sbLongterm, setSbLongterm] = useState(800);
  const [sbDaily, setSbDaily] = useState(800);
  const [sbRecent, setSbRecent] = useState(2000);
  const [budgetLoaded, setBudgetLoaded] = useState(false);
  const [budgetSaving, setBudgetSaving] = useState(false);

  const [toast, setToast] = useState(null);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => {
    apiFetch("/api/settings/context-budget")
      .then((d) => {
        setRetainBudget(d.retain_budget);
        setTriggerThreshold(d.trigger_threshold);
        setSbLongterm(d.summary_budget_longterm);
        setSbDaily(d.summary_budget_daily);
        setSbRecent(d.summary_budget_recent);
        setBudgetLoaded(true);
      })
      .catch(() => setBudgetLoaded(true));
  }, []);

  useEffect(() => {
    localStorage.setItem("telegram_buffer_seconds", String(tgBufferSec));
    // sync to backend so Telegram bot picks it up
    apiFetch("/api/settings/buffer-seconds", {
      method: "PUT",
      body: { seconds: tgBufferSec },
    }).catch(() => {});
  }, [tgBufferSec]);

  // Load short msg max & tool rounds from backend
  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings/short-msg-max")
        .then((d) => { const v = d.max_count || 8; setShortMsgMax(v); localStorage.setItem("short_msg_max", String(v)); }),
      apiFetch("/api/settings/max-tool-rounds")
        .then((d) => { const v = d.max_rounds || 15; setMaxToolRounds(v); localStorage.setItem("max_tool_rounds", String(v)); }),
    ]).finally(() => { miscMounted.current = true; });
  }, []);

  useEffect(() => {
    if (!miscMounted.current) return;
    localStorage.setItem("short_msg_max", String(shortMsgMax));
    apiFetch("/api/settings/short-msg-max", {
      method: "PUT",
      body: { max_count: shortMsgMax },
    }).catch(() => {});
  }, [shortMsgMax]);

  useEffect(() => {
    if (!miscMounted.current) return;
    localStorage.setItem("max_tool_rounds", String(maxToolRounds));
    apiFetch("/api/settings/max-tool-rounds", {
      method: "PUT",
      body: { max_rounds: maxToolRounds },
    }).catch(() => {});
  }, [maxToolRounds]);

  const saveBudget = async () => {
    setBudgetSaving(true);
    try {
      await apiFetch("/api/settings/context-budget", {
        method: "PUT",
        body: {
          retain_budget: retainBudget,
          trigger_threshold: triggerThreshold,
          summary_budget_longterm: sbLongterm,
          summary_budget_daily: sbDaily,
          summary_budget_recent: sbRecent,
        },
      });
      showToast("预算已保存");
    } catch (_e) {
      showToast("保存失败");
    } finally {
      setBudgetSaving(false);
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
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>设置</h1>
        <div className="w-10" />
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5 space-y-4">

        {/* Navigation links */}
        <div
          className="rounded-[20px] overflow-hidden"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <RowLink
            icon={<Server size={18} style={{ color: S.text }} />}
            label="API 设置"
            hint="模型 · 预设"
            onClick={() => navigate("/settings/api")}
          />
          <Divider />
          <RowLink
            icon={<Mic2 size={18} style={{ color: S.text }} />}
            label="语音设置"
            hint="TTS · STT"
            onClick={() => navigate("/settings/voice")}
          />
        </div>

        {/* Proactive message entry */}
        <div
          className="rounded-[20px] overflow-hidden"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <RowLink
            icon={<MessageSquare size={18} style={{ color: S.text }} />}
            label="主动发消息"
            hint="定时触发 AI 发送消息"
            onClick={() => navigate("/settings/proactive")}
          />
        </div>

        {/* Short-mode: buffer time + max count */}
        <div
          className="rounded-[20px] overflow-hidden"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="flex items-center gap-4 px-4 py-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Timer size={18} style={{ color: S.text }} />
            </div>
            <div className="flex-1">
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>缓冲时间</div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>短消息模式收集等待 (秒)</div>
            </div>
            <div className="flex items-center gap-1.5">
              <NumberInput value={tgBufferSec} onChange={setTgBufferSec} min={1} max={120} />
              <span className="text-[12px]" style={{ color: S.textMuted }}>秒</span>
            </div>
          </div>
          <Divider />
          <div className="flex items-center gap-4 px-4 py-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Hash size={18} style={{ color: S.text }} />
            </div>
            <div className="flex-1">
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>短消息条数</div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>短消息模式最大回复条数</div>
            </div>
            <div className="flex items-center gap-1.5">
              <NumberInput value={shortMsgMax} onChange={setShortMsgMax} min={2} max={20} />
              <span className="text-[12px]" style={{ color: S.textMuted }}>条</span>
            </div>
          </div>
          <Divider />
          <div className="flex items-center gap-4 px-4 py-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Repeat size={18} style={{ color: S.text }} />
            </div>
            <div className="flex-1">
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>工具轮次</div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>单次对话最大工具调用轮数</div>
            </div>
            <div className="flex items-center gap-1.5">
              <NumberInput value={maxToolRounds} onChange={setMaxToolRounds} min={3} max={50} />
              <span className="text-[12px]" style={{ color: S.textMuted }}>轮</span>
            </div>
          </div>
        </div>

        {/* Context + Summary budget */}
        <div
          className="rounded-[20px] p-4"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {/* 上下文预算 title */}
          <div className="flex items-center gap-3 mb-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
              style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
            >
              <Database size={18} style={{ color: S.text }} />
            </div>
            <div>
              <div className="text-[15px] font-semibold" style={{ color: S.text }}>上下文预算</div>
              <div className="text-[11px]" style={{ color: S.textMuted }}>控制对话历史长度</div>
            </div>
          </div>
          {budgetLoaded ? (
            <>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <div className="text-[13px] font-medium" style={{ color: S.text }}>保留预算 (tokens)</div>
                  <div className="text-[10px]" style={{ color: S.textMuted }}>摘要后保留的最大 token 数</div>
                </div>
                <NumberInput value={retainBudget} onChange={setRetainBudget} min={1000} max={100000} />
              </div>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <div className="text-[13px] font-medium" style={{ color: S.text }}>触发阈值 (tokens)</div>
                  <div className="text-[10px]" style={{ color: S.textMuted }}>超过此值时触发摘要</div>
                </div>
                <NumberInput value={triggerThreshold} onChange={setTriggerThreshold} min={1000} max={200000} />
              </div>

              {/* 摘要预算 title */}
              <div className="h-px mb-4" style={{ background: "rgba(136,136,160,0.15)" }} />
              <div className="flex items-center gap-3 mb-4">
                <div
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
                  style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
                >
                  <BookOpen size={18} style={{ color: S.text }} />
                </div>
                <div>
                  <div className="text-[15px] font-semibold" style={{ color: S.text }}>摘要预算</div>
                  <div className="text-[11px]" style={{ color: S.textMuted }}>三层摘要注入上限</div>
                </div>
              </div>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <div className="text-[13px] font-medium" style={{ color: S.text }}>长期记忆 (tokens)</div>
                  <div className="text-[10px]" style={{ color: S.textMuted }}>关系脉络、重大事件</div>
                </div>
                <NumberInput value={sbLongterm} onChange={setSbLongterm} min={200} max={5000} />
              </div>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <div className="text-[13px] font-medium" style={{ color: S.text }}>近期日常 (tokens)</div>
                  <div className="text-[10px]" style={{ color: S.textMuted }}>当天的合并回顾</div>
                </div>
                <NumberInput value={sbDaily} onChange={setSbDaily} min={200} max={5000} />
              </div>
              <div className="flex items-center justify-between mb-4">
                <div>
                  <div className="text-[13px] font-medium" style={{ color: S.text }}>最近摘要 (tokens)</div>
                  <div className="text-[10px]" style={{ color: S.textMuted }}>保留最近几条原始摘要</div>
                </div>
                <NumberInput value={sbRecent} onChange={setSbRecent} min={500} max={20000} />
              </div>

              <button
                className="w-full rounded-[14px] py-3 text-[14px] font-bold text-white"
                style={{
                  background: budgetSaving
                    ? "rgba(201,98,138,0.5)"
                    : "linear-gradient(135deg, var(--accent), var(--accent-dark))",
                  boxShadow: "3px 3px 8px rgba(201,98,138,0.3)",
                }}
                onClick={saveBudget}
                disabled={budgetSaving}
              >
                {budgetSaving ? "保存中..." : "保存预算"}
              </button>
            </>
          ) : (
            <div className="flex justify-center py-4">
              <div
                className="h-6 w-6 animate-spin rounded-full border-2"
                style={{ borderColor: S.accent, borderTopColor: "transparent" }}
              />
            </div>
          )}
        </div>
      </div>

      {toast && (
        <div className="pointer-events-none fixed inset-x-0 top-1/2 z-[200] flex justify-center">
          <div className="rounded-2xl px-6 py-3 text-[14px] font-medium text-white" style={{ background: "rgba(0,0,0,0.75)" }}>
            {toast}
          </div>
        </div>
      )}
    </div>
  );
}
