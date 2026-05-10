import { useState, useEffect, useLayoutEffect, useRef, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ChevronLeft, Trash2, ChevronDown, Pencil, Search, X, Check, BookOpen, RefreshCw, History, Minimize2 } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const TABS = [
  { key: "memories", label: "记忆" },
  { key: "summaries", label: "摘要" },
  { key: "messages", label: "消息记录" },
];

const KLASS_COLORS = {
  identity:     { color: "#7b5ea7", bg: "#ede4f7" },
  relationship: { color: "#c26a8a", bg: "#f7e0ea" },
  bond:         { color: "#d48aab", bg: "#fce8f0" },
  conflict:     { color: "#b5454a", bg: "#f5dede" },
  fact:         { color: "#4a8ab5", bg: "#deedf5" },
  preference:   { color: "#9b7a3b", bg: "#f0ebd8" },
  health:       { color: "#4a9b6e", bg: "#ddf0e5" },
  task:         { color: "#6b7b9b", bg: "#e0e6f0" },
  ephemeral:    { color: "#9b9b9b", bg: "#ececec" },
  other:        { color: "#8a7a6a", bg: "#efe8df" },
};

// Strip [YYYY.MM.DD HH:MM] prefix for char count (matches save_memory logic)
const stripTimePrefix = (s) => s.replace(/^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] /, "");

const KLASS_OPTIONS = [
  { value: "", label: "全部分类" },
  { value: "identity", label: "identity" },
  { value: "relationship", label: "relationship" },
  { value: "bond", label: "bond" },
  { value: "conflict", label: "conflict" },
  { value: "fact", label: "fact" },
  { value: "preference", label: "preference" },
  { value: "health", label: "health" },
  { value: "task", label: "task" },
  { value: "ephemeral", label: "ephemeral" },
  { value: "other", label: "other" },
];

const MOOD_OPTIONS = [
  { value: "", label: "全部心情" },
  { value: "sad", label: "sad" },
  { value: "angry", label: "angry" },
  { value: "anxious", label: "anxious" },
  { value: "tired", label: "tired" },
  { value: "emo", label: "emo" },
  { value: "happy", label: "happy" },
  { value: "flirty", label: "flirty" },
  { value: "proud", label: "proud" },
  { value: "calm", label: "calm" },
];

const ROLE_OPTIONS = [
  { value: "", label: "全部类型" },
  { value: "user", label: "用户消息" },
  { value: "assistant", label: "助手消息" },
  { value: "system", label: "系统消息" },
  { value: "no_message", label: "判定消息" },
  { value: "thinking", label: "草稿消息" },
  { value: "native_thinking", label: "思考过程" },
  { value: "cafe", label: "群聊消息" },
  { value: "tool", label: "工具消息" },
];

const SEARCH_MODE_OPTIONS = [
  { value: "text", label: "按内容" },
  { value: "summary_id", label: "按摘要ID" },
];

/* ── Helpers ── */

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const now = new Date();
  const h = d.getHours().toString().padStart(2, "0");
  const m = d.getMinutes().toString().padStart(2, "0");
  const time = `${h}:${m}`;
  if (now - d < 86400000 && d.toDateString() === now.toDateString()) return time;
  return `${d.getMonth() + 1}/${d.getDate()} ${time}`;
}

function Highlight({ text, keyword }) {
  if (!keyword || !text) return <>{text}</>;
  const lower = text.toLowerCase();
  const kw = keyword.toLowerCase();
  const parts = [];
  let last = 0;
  let idx = lower.indexOf(kw, last);
  while (idx !== -1) {
    if (idx > last) parts.push(<span key={last}>{text.slice(last, idx)}</span>);
    parts.push(
      <span key={`h${idx}`} style={{ color: S.accentDark, fontWeight: 600 }}>
        {text.slice(idx, idx + kw.length)}
      </span>
    );
    last = idx + kw.length;
    idx = lower.indexOf(kw, last);
  }
  if (last < text.length) parts.push(<span key={last}>{text.slice(last)}</span>);
  return <>{parts}</>;
}

/* ── Selection checkbox ── */

function SelectCircle({ selected }) {
  return (
    <div
      className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full"
      style={selected
        ? { background: S.accentDark, boxShadow: "0 2px 6px rgba(201,98,138,0.3)" }
        : { background: S.bg, boxShadow: "var(--inset-shadow)" }
      }
    >
      {selected && <Check size={12} color="white" strokeWidth={3} />}
    </div>
  );
}

/* ── Confirm dialog ── */

function ConfirmDialog({ title = "确认删除", message, confirmLabel = "删除", confirmColor = "#ff4d6d", onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={onCancel}>
      <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-6" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>{title}</p>
        <p className="mb-5 text-center text-[13px]" style={{ color: S.textMuted }}>{message}</p>
        <div className="flex gap-3">
          <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={onCancel}>取消</button>
          <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold text-white" style={{ background: confirmColor, boxShadow: `4px 4px 10px ${confirmColor}66` }} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

/* ── Pending badge with expandable detail ── */

function PendingBadge({ label, rawIds, dailyGroups }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => { document.removeEventListener("mousedown", handler); document.removeEventListener("touchstart", handler); };
  }, [open]);

  return (
    <span className="relative inline-flex items-center" ref={ref}>
      <span
        className="inline-block rounded-full px-1.5 py-0.5 text-[9px] font-medium cursor-pointer"
        style={{ background: "rgba(232,160,100,0.15)", color: "#c47a30" }}
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
      >
        {label}
      </span>
      {open && (
        <div
          className="absolute left-0 top-full mt-1 z-30 rounded-[10px] p-2.5 min-w-[140px] max-w-[220px]"
          style={{ background: S.bg, boxShadow: "0 4px 16px rgba(0,0,0,0.15)" }}
          onClick={(e) => e.stopPropagation()}
        >
          {rawIds.length > 0 && (
            <div className="mb-1.5">
              <p className="text-[10px] font-semibold mb-0.5" style={{ color: S.text }}>原始摘要</p>
              <p className="text-[9px] leading-relaxed" style={{ color: S.textMuted }}>
                {rawIds.map((id) => `#${id}`).join(", ")}
              </p>
            </div>
          )}
          {dailyGroups.map((g, i) => (
            <div key={i} className={i > 0 || rawIds.length > 0 ? "mt-1.5" : ""}>
              <p className="text-[10px] font-semibold mb-0.5" style={{ color: S.text }}>
                来自 daily v{g.version}
              </p>
              <p className="text-[9px] leading-relaxed" style={{ color: S.textMuted }}>
                {g.ids.map((id) => `#${id}`).join(", ")}
              </p>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}

/* ── Layer history overlay ── */

function LayerHistoryOverlay({ items, onApply, onClose, onDelete }) {
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [confirmId, setConfirmId] = useState(null);

  const fmtDate = (ts) => {
    if (!ts) return "";
    const d = new Date(ts);
    return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  const handleApply = async () => {
    if (!selected) return;
    const item = items.find((h) => h.id === selected);
    if (item) await onApply(item);
    onClose();
  };

  if (detail) {
    return (
      <div className="fixed inset-0 z-[200] flex flex-col" style={{ background: S.bg }}>
        <div className="flex shrink-0 items-center justify-between px-5 py-4" style={{ paddingTop: "max(1rem, env(safe-area-inset-top))" }}>
          <span className="text-[15px] font-bold" style={{ color: S.text }}>v{detail.version}</span>
          <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }} onClick={() => setDetail(null)}>
            <Minimize2 size={18} style={{ color: S.accentDark }} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 pb-10">
          {detail.merged_summary_ids?.length > 0 && (
            <p className="mb-2 text-[11px]" style={{ color: S.textMuted }}>
              合并摘要: {detail.merged_summary_ids.map((id) => `#${id}`).join(", ")}
            </p>
          )}
          <div className="rounded-[14px] p-4" style={{ boxShadow: "var(--inset-shadow)", background: S.bg }}>
            <p className="whitespace-pre-wrap text-[13px] leading-relaxed" style={{ color: S.text }}>{detail.content}</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-[200] flex flex-col" style={{ background: S.bg }}>
      <div className="flex shrink-0 items-center justify-between px-5 py-4" style={{ paddingTop: "max(1rem, env(safe-area-inset-top))" }}>
        <span className="text-[15px] font-bold" style={{ color: S.text }}>历史版本</span>
        <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }} onClick={onClose}>
          <Minimize2 size={18} style={{ color: S.accentDark }} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5">
        {items.length === 0 && (
          <p className="py-8 text-center text-[13px]" style={{ color: S.textMuted }}>暂无历史版本</p>
        )}

        {items.map((item) => (
          <div key={item.id} className="mb-2 flex items-center gap-3 rounded-[14px] p-3" style={{ boxShadow: selected === item.id ? "var(--inset-shadow)" : "var(--card-shadow-sm)", background: S.bg }}>
            {item.is_current ? (
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full" style={{ background: S.accentDark }}><Check size={12} color="white" /></span>
            ) : (
              <button onClick={() => setSelected(item.id)} className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full" style={{ background: selected === item.id ? S.accentDark : S.bg, boxShadow: selected === item.id ? "none" : "var(--icon-inset)" }}>
                {selected === item.id && <Check size={12} color="white" />}
              </button>
            )}
            <div className="flex-1 min-w-0" onClick={() => setDetail(item)}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="text-[13px] font-semibold" style={{ color: S.text }}>v{item.version}</span>
                  {item.is_current && <span className="rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(155,120,200,0.12)", color: "#8b6abf" }}>当前</span>}
                </div>
                <span className="text-[10px]" style={{ color: S.textMuted }}>{fmtDate(item.created_at)}</span>
              </div>
              <p className="mt-0.5 truncate text-[11px]" style={{ color: S.textMuted }}>{item.content}</p>
              {item.merged_summary_ids?.length > 0 && (
                <p className="mt-0.5 text-[10px]" style={{ color: S.accent }}>
                  合并摘要: {item.merged_summary_ids.map((id) => `#${id}`).join(", ")}
                </p>
              )}
            </div>
            {!item.is_current && (
              <button onClick={() => setConfirmId(item.id)} className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full" style={{ color: "#ccc" }}>
                <Trash2 size={11} />
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="shrink-0 p-5">
        <button
          className="w-full rounded-[14px] py-3.5 text-[15px] font-bold text-white"
          style={{
            background: selected ? "linear-gradient(135deg, var(--accent), var(--accent-dark))" : "#ccc",
            boxShadow: selected ? "4px 4px 10px rgba(201,98,138,0.35)" : "none",
          }}
          onClick={handleApply}
          disabled={!selected}
        >
          确定
        </button>
      </div>

      {confirmId && (
        <ConfirmDialog
          message="确定要删除这条历史版本吗？"
          onCancel={() => setConfirmId(null)}
          onConfirm={() => { onDelete(confirmId); setConfirmId(null); }}
        />
      )}
    </div>
  );
}

/* ── Versions modal (memory / summary) ── */

function VersionsModal({ type, id, sessionId, onClose, onRollback }) {
  const [versions, setVersions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null);
  const [confirmVer, setConfirmVer] = useState(null);

  useEffect(() => {
    const url = type === "memory" ? `/api/memories/${id}/versions` : `/api/summaries/${id}/versions`;
    apiFetch(url).then((d) => setVersions(Array.isArray(d) ? d : d.versions || [])).catch(() => {}).finally(() => setLoading(false));
  }, [type, id]);

  const fmtDate = (ts) => {
    if (!ts) return "";
    const d = new Date(ts);
    return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  const doRollback = async (versionId) => {
    try {
      const url = type === "memory" ? `/api/memories/${id}/rollback/${versionId}` : `/api/summaries/${id}/rollback/${versionId}`;
      await apiFetch(url, { method: "POST" });
      onRollback?.();
      onClose();
    } catch (_e) { /* ignore */ }
  };

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.35)" }} onClick={onClose}>
      <div className="relative w-[90vw] max-w-md rounded-[18px] flex flex-col" style={{ background: S.bg, maxHeight: "70vh", boxShadow: "0 8px 32px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex shrink-0 items-center justify-between px-5 py-3 border-b" style={{ borderColor: "rgba(0,0,0,0.06)" }}>
          <span className="text-[14px] font-bold" style={{ color: S.text }}>{detail ? "版本详情" : "版本历史"}</span>
          <button className="flex h-8 w-8 items-center justify-center rounded-full" style={{ background: "rgba(0,0,0,0.05)" }} onClick={detail ? () => setDetail(null) : onClose}>
            <X size={16} style={{ color: S.textMuted }} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {detail ? (
            <>
              <div className="mb-2 flex items-center gap-2 text-[11px]" style={{ color: S.textMuted }}>
                <span>{fmtDate(detail.created_at)}</span>
                {detail.changed_by && <span className="rounded-full px-1.5 py-0.5 text-[9px]" style={{ background: "rgba(136,136,160,0.1)" }}>{detail.changed_by}</span>}
              </div>
              <div className="rounded-[14px] p-3" style={{ boxShadow: "var(--inset-shadow)", background: S.bg }}>
                <p className="whitespace-pre-wrap text-[12px] leading-relaxed" style={{ color: S.text }}>{detail.content}</p>
              </div>
            </>
          ) : (
            <>
              {loading && <Spinner />}
              {!loading && versions.length === 0 && (
                <p className="py-6 text-center text-[13px]" style={{ color: S.textMuted }}>暂无版本历史</p>
              )}
              {versions.map((ver) => (
                <div key={ver.id} className="mb-2 rounded-[14px] p-3" style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg }}>
                  <div className="flex items-center justify-between" onClick={() => setDetail(ver)} style={{ cursor: "pointer" }}>
                    <div className="flex items-center gap-1.5">
                      <span className="text-[11px]" style={{ color: S.textMuted }}>{fmtDate(ver.created_at)}</span>
                      {ver.changed_by && <span className="rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(155,120,200,0.12)", color: "#8b6abf" }}>{ver.changed_by}</span>}
                    </div>
                    <button
                      className="rounded-[8px] px-2 py-1 text-[10px] font-medium"
                      style={{ background: "rgba(232,160,191,0.15)", color: S.accentDark }}
                      onClick={(e) => { e.stopPropagation(); setConfirmVer(ver.id); }}
                    >
                      回滚
                    </button>
                  </div>
                  <p className="mt-1 truncate text-[11px]" style={{ color: S.textMuted }} onClick={() => setDetail(ver)}>{ver.content}</p>
                </div>
              ))}
            </>
          )}
        </div>
      </div>

      {confirmVer && (
        <ConfirmDialog
          title="确认回滚"
          message="确定要回滚到这个版本吗？"
          confirmLabel="回滚"
          confirmColor={S.accentDark}
          onCancel={() => setConfirmVer(null)}
          onConfirm={() => { doRollback(confirmVer); setConfirmVer(null); }}
        />
      )}
    </div>
  );
}

/* ── Edit modal ── */

function EditModal({ initialText, onSave, onCancel, memoryData }) {
  const [text, setText] = useState(initialText);
  const [klass, setKlass] = useState(memoryData?.klass || "other");
  const [tagsText, setTagsText] = useState((memoryData?.tags?.topic || []).join(", "));
  const [disclosure, setDisclosure] = useState(memoryData?.disclosure || "");
  const isMemory = !!memoryData;
  const KLASS_EDIT = KLASS_OPTIONS.filter((o) => o.value);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={onCancel}>
      <div className="mx-5 w-full max-w-[340px] rounded-[18px] p-4" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <textarea
          className="w-full rounded-[12px] p-3 text-[12px] leading-relaxed resize-none outline-none overflow-y-auto thin-scrollbar"
          style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text, minHeight: isMemory ? 100 : 140, maxHeight: 280 }}
          value={text}
          onChange={(e) => setText(e.target.value)}
          autoFocus
        />
        {isMemory && (
          <div className="mt-1 text-right">
            <span className="text-[10px]" style={{ color: stripTimePrefix(text).length > 120 ? "#b5454a" : S.textMuted }}>{stripTimePrefix(text).length}字{stripTimePrefix(text).length > 120 ? " 建议精简到120字以内" : ""}</span>
          </div>
        )}
        {isMemory && (
          <div className="mt-2 flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <span className="shrink-0 text-[11px] font-medium" style={{ color: S.textMuted }}>分类</span>
              <div className="relative flex-1">
                <select
                  className="w-full appearance-none rounded-[10px] px-3 py-1.5 text-[11px] font-medium outline-none"
                  style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text, WebkitAppearance: "none" }}
                  value={klass}
                  onChange={(e) => setKlass(e.target.value)}
                >
                  {KLASS_EDIT.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                <ChevronDown size={10} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2" style={{ color: S.textMuted }} />
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="shrink-0 text-[11px] font-medium" style={{ color: S.textMuted }}>标签</span>
              <input
                className="flex-1 rounded-[10px] px-3 py-1.5 text-[11px] outline-none"
                style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text }}
                placeholder="逗号分隔，如：旅行, 美食"
                value={tagsText}
                onChange={(e) => setTagsText(e.target.value)}
              />
            </div>
            <div className="flex items-start gap-2">
              <span className="text-[11px] font-medium mt-1.5" style={{ color: S.textMuted, width: 22, lineHeight: '14px' }}>触发条件</span>
              <textarea
                className="flex-1 rounded-[10px] px-3 py-1.5 text-[11px] outline-none resize-none"
                style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text, minHeight: 40, maxHeight: 100 }}
                placeholder="什么情况下触发这条记忆"
                value={disclosure}
                onChange={(e) => setDisclosure(e.target.value)}
              />
            </div>
          </div>
        )}
        <div className="mt-3 flex gap-3">
          <button className="flex-1 rounded-[12px] py-2 text-[12px] font-medium" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.textMuted }} onClick={onCancel}>取消</button>
          <button
            className="flex-1 rounded-[12px] py-2 text-[12px] font-medium"
            style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }}
            onClick={() => {
              if (isMemory) {
                const topics = tagsText.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
                onSave(text, klass, { topic: topics }, disclosure);
              } else {
                onSave(text);
              }
            }}
            disabled={!text.trim()}
          >
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Search mode toggle (inside search bar) ── */

function SearchModeButton({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => { document.removeEventListener("mousedown", handler); document.removeEventListener("touchstart", handler); };
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        className="flex items-center justify-center rounded-full"
        style={{ width: 18, height: 18, background: value !== "text" ? "rgba(74,138,181,0.15)" : "rgba(136,136,160,0.1)" }}
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
      >
        <ChevronDown size={10} style={{ color: value !== "text" ? "#4a8ab5" : S.textMuted, transform: open ? "rotate(180deg)" : "none", transition: "transform 0.2s" }} />
      </button>
      {open && (
        <div
          className="absolute right-0 top-full z-50 mt-1 min-w-[80px] rounded-[10px] py-1"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {SEARCH_MODE_OPTIONS.map((o) => (
            <button
              key={o.value}
              className="flex w-full items-center justify-between px-2.5 py-1.5 text-[10px]"
              style={{ color: S.text }}
              onClick={(e) => { e.stopPropagation(); onChange(o.value); setOpen(false); }}
            >
              <span>{o.label}</span>
              {value === o.value && <Check size={9} style={{ color: S.accentDark, marginLeft: 4 }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Custom dropdown (ModelDropdown style) ── */

function FilterDropdown({ value, rawValue, onChange, options, width, active }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => { document.removeEventListener("mousedown", handler); document.removeEventListener("touchstart", handler); };
  }, [open]);

  const displayLabel = options.find((o) => o.value === String(rawValue))?.label || value || "";

  return (
    <div className="relative" style={{ width }} ref={ref}>
      <button
        className="flex w-full items-center justify-between rounded-[12px] px-2.5 py-2 text-[11px] font-medium text-left"
        style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg, color: active ? S.accentDark : S.text }}
        onClick={() => setOpen(!open)}
      >
        <span className="truncate flex-1">{displayLabel}</span>
        <ChevronDown size={10} style={{ color: S.textMuted, flexShrink: 0, marginLeft: 2, transform: open ? "rotate(180deg)" : "none", transition: "transform 0.2s" }} />
      </button>
      {open && (
        <div
          className="absolute left-0 right-0 top-full z-40 mt-1 max-h-[200px] overflow-y-auto rounded-[12px]"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {options.map((o) => (
            <button
              key={o.value}
              className="flex w-full items-center justify-between px-3 py-2 text-[11px]"
              style={{ color: S.text }}
              onClick={() => { onChange(o.value); setOpen(false); }}
            >
              <span className="truncate">{o.label}</span>
              {String(rawValue) === o.value && <Check size={10} style={{ color: S.accentDark, flexShrink: 0 }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Expandable card (memory / summary) ── */

function ExpandableCard({ children, time, badge, keyword, onEdit, onVersions, selectMode, selected, onToggle, onLongPress, cardRef, isHighlighted, charLimit }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const textRef = useRef(null);
  const text = typeof children === "string" ? children : "";
  const lpRef = useRef(null);
  const lpTriggered = useRef(false);
  const touchStartPos = useRef(null);

  useLayoutEffect(() => {
    const el = textRef.current;
    if (el && !expanded) setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [text, expanded]);

  const handleTouchStart = (e) => {
    if (selectMode) return;
    const t = e.touches[0];
    touchStartPos.current = { x: t.clientX, y: t.clientY };
    lpTriggered.current = false;
    lpRef.current = setTimeout(() => { lpTriggered.current = true; onLongPress?.(); }, 600);
  };
  const handleTouchMove = (e) => {
    if (!touchStartPos.current || !lpRef.current) return;
    const t = e.touches[0];
    const dx = t.clientX - touchStartPos.current.x;
    const dy = t.clientY - touchStartPos.current.y;
    if (dx * dx + dy * dy > 100) { clearTimeout(lpRef.current); lpRef.current = null; }
  };
  const handleTouchEnd = () => { clearTimeout(lpRef.current); };
  const handleClick = () => {
    if (lpTriggered.current) return;
    if (selectMode) { onToggle?.(); return; }
  };

  return (
    <div
      ref={cardRef}
      className="mb-3 rounded-[18px] p-3 flex items-start gap-2.5 relative"
      style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
      onTouchCancel={handleTouchEnd}
      onClick={handleClick}
    >
      {isHighlighted && (
        <div className="absolute pointer-events-none z-10" style={{ top: -10, right: 42, animation: "paw-sway 2.2s ease-in-out forwards" }}>
          <img src="/miniapp/assets/decorations/两个小猫爪.png" alt="" style={{ width: 36, height: 36, imageRendering: "pixelated" }} />
        </div>
      )}
      {selectMode && <div className="mt-1"><SelectCircle selected={selected} /></div>}
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-1">
          <div className="flex-1 min-w-0">
            {badge}
            <div
              ref={textRef}
              className="text-[12px] leading-relaxed break-words"
              style={expanded ? { color: S.text } : { color: S.text, display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}
            >
              <Highlight text={text} keyword={keyword} />
            </div>
          </div>
          {!selectMode && (onEdit || onVersions) && (
            <div className="mt-0.5 flex shrink-0 flex-col items-center gap-2">
              {onEdit && (
                <button
                  className="flex h-6 w-6 items-center justify-center rounded-full"
                  style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
                  onClick={(e) => { e.stopPropagation(); onEdit(); }}
                >
                  <Pencil size={11} style={{ color: S.accentDark }} />
                </button>
              )}
              {onVersions && (
                <button
                  className="flex h-6 w-6 items-center justify-center rounded-full"
                  style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
                  onClick={(e) => { e.stopPropagation(); onVersions(); }}
                >
                  <History size={11} style={{ color: S.textMuted }} />
                </button>
              )}
            </div>
          )}
        </div>
        <div className="mt-1 flex items-center justify-between">
          <span className="text-[10px]" style={{ color: S.textMuted }}>{time || ""}</span>
          <div className="flex items-center gap-2">
            {!selectMode && (overflows || expanded) && (
              <button
                className="rounded-full px-2 py-0.5 text-[10px]"
                style={{ color: S.accentDark, background: "rgba(232,160,191,0.1)" }}
                onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
              >
                {expanded ? "收起" : "查看更多"}
              </button>
            )}
            <span className="text-[10px]" style={{ color: charLimit && stripTimePrefix(text).length > charLimit ? "#b5454a" : S.textMuted }}>{stripTimePrefix(text).length}字</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Trash card ── */

function TrashCard({ id, content, deletedAt, createdAt, klass, moodTag, keyword, onRestore, onPermanentDelete }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const textRef = useRef(null);

  useLayoutEffect(() => {
    const el = textRef.current;
    if (el && !expanded) setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [content, expanded]);

  const klassColor = klass ? (KLASS_COLORS[klass] || KLASS_COLORS.other) : null;

  return (
    <div className="mb-2 rounded-[14px] p-3" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", opacity: 0.75 }}>
      <div className="flex flex-wrap items-center gap-1 mb-1">
        <span className="text-[10px]" style={{ color: S.textMuted }}>#{id}</span>
        {klassColor && <span className="inline-block rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: klassColor.bg, color: klassColor.color }}>{klass}</span>}
        {moodTag && <span className="inline-block rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(232,160,191,0.15)", color: S.accentDark }}>{moodTag}</span>}
      </div>
      <div
        ref={textRef}
        className="text-[12px] leading-relaxed break-words"
        style={expanded ? { color: S.text } : { color: S.text, display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}
      >
        <Highlight text={content} keyword={keyword} />
      </div>
      {(overflows || expanded) && (
        <div className="mt-1 flex justify-center">
          <button
            className="rounded-full px-2 py-0.5 text-[10px]"
            style={{ color: S.accentDark, background: "rgba(232,160,191,0.1)" }}
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
          >
            {expanded ? "收起" : "查看更多"}
          </button>
        </div>
      )}
      <div className="mt-2 flex items-center justify-between">
        <div className="flex flex-col gap-0.5">
          {createdAt && <span className="text-[10px]" style={{ color: S.textMuted }}>创建于 {fmtTime(createdAt)}</span>}
          <span className="text-[10px]" style={{ color: S.textMuted }}>{deletedAt ? (() => { const dl = Math.max(0, 30 - Math.floor((Date.now() - new Date(deletedAt).getTime()) / 86400000)); return `删除于 ${fmtTime(deletedAt)} · ${dl}天后自动清理`; })() : ""}</span>
        </div>
        <div className="flex gap-2">
          <button className="rounded-lg px-2 py-1 text-[10px] font-medium" style={{ background: "rgba(59,130,246,0.1)", color: "#3b82f6" }} onClick={onRestore}>恢复</button>
          <button className="rounded-lg px-2 py-1 text-[10px] font-medium" style={{ background: "rgba(239,68,68,0.1)", color: "#ef4444" }} onClick={onPermanentDelete}>彻底删除</button>
        </div>
      </div>
    </div>
  );
}

/* ── Message card (expandable) ── */

function MessageCard({ msg, keyword, roleLabel, roleColor, selectMode, selected, onToggle, onLongPress, onImageClick }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const textRef = useRef(null);
  const lpRef = useRef(null);
  const lpTriggered = useRef(false);
  const touchStartPos = useRef(null);

  useLayoutEffect(() => {
    const el = textRef.current;
    if (el && !expanded) setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [msg.content, expanded]);

  const handleTouchStart = (e) => {
    if (selectMode) return;
    const t = e.touches[0];
    touchStartPos.current = { x: t.clientX, y: t.clientY };
    lpTriggered.current = false;
    lpRef.current = setTimeout(() => { lpTriggered.current = true; onLongPress?.(); }, 600);
  };
  const handleTouchMove = (e) => {
    if (!touchStartPos.current || !lpRef.current) return;
    const t = e.touches[0];
    const dx = t.clientX - touchStartPos.current.x;
    const dy = t.clientY - touchStartPos.current.y;
    if (dx * dx + dy * dy > 100) { clearTimeout(lpRef.current); lpRef.current = null; }
  };
  const handleTouchEnd = () => { clearTimeout(lpRef.current); };
  const handleClick = () => {
    if (lpTriggered.current) return;
    if (selectMode) { onToggle?.(); return; }
  };

  return (
    <div
      className="mb-3 rounded-[18px] p-3 flex items-start gap-2.5"
      style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
      onTouchCancel={handleTouchEnd}
      onClick={handleClick}
    >
      {selectMode && <div className="mt-1"><SelectCircle selected={selected} /></div>}
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] font-semibold" style={{ color: roleColor(msg.role) }}>{roleLabel(msg.role)}</span>
            {msg.meta_info?.tool_name && (
              <span className="rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(212,136,58,0.1)", color: "#d4883a" }}>{msg.meta_info.tool_name}</span>
            )}
            {msg.meta_info?.source && (
              <span className="rounded-full px-1.5 py-0.5 text-[9px]" style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}>{msg.meta_info.source}</span>
            )}
            {msg.summary_group_id && (
              <span className="rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(74,138,181,0.1)", color: "#4a8ab5" }}>
                已摘要：{msg.summary_group_id}
              </span>
            )}
          </div>
          <span className="text-[10px]" style={{ color: S.textMuted }}>{fmtTime(msg.created_at)}</span>
        </div>
        {msg.image_url && (
          <div className="mb-1.5">
            <img
              src={msg.image_url}
              alt=""
              className="rounded-lg"
              style={{ maxWidth: 160, maxHeight: 160, objectFit: "cover", boxShadow: "var(--inset-shadow)", cursor: "pointer" }}
              onClick={(e) => { e.stopPropagation(); onImageClick?.(msg.image_url); }}
              onError={(e) => {
                e.target.style.display = "none";
                const span = document.createElement("span");
                span.textContent = "图片已过期";
                span.style.cssText = "font-size:11px;color:var(--text-muted);";
                e.target.parentNode.appendChild(span);
              }}
            />
          </div>
        )}
        <div
          ref={textRef}
          className="text-[12px] leading-relaxed break-words"
          style={expanded ? { color: S.text } : { color: S.text, display: "-webkit-box", WebkitLineClamp: 4, WebkitBoxOrient: "vertical", overflow: "hidden" }}
        >
          {(() => {
            const raw = msg.image_url ? msg.content.replace(/\[图片[：:][^\]]*\]|\[图片\]/g, "").trim() : msg.content;
            if (raw && raw.trim()) return <Highlight text={raw} keyword={keyword} />;
            // tool_calls assistant message — show tool name + args summary
            const tc = msg.meta_info?.tool_calls;
            if (tc && Array.isArray(tc)) {
              return tc.map((c, i) => {
                const fn = c.function?.name || c.name || "unknown";
                const args = c.function?.arguments || c.arguments;
                let argStr = "";
                try { argStr = typeof args === "string" ? args : JSON.stringify(args); } catch {}
                if (argStr.length > 120) argStr = argStr.slice(0, 120) + "...";
                return <div key={i} style={{ color: S.textMuted }}><span style={{ color: "#d4883a" }}>{fn}</span>({argStr})</div>;
              });
            }
            // tool_call (single) result message
            const tc1 = msg.meta_info?.tool_call;
            if (tc1) {
              const fn = msg.meta_info?.tool_name || tc1.name || "tool";
              const args = tc1.arguments;
              let argStr = "";
              try { argStr = typeof args === "string" ? args : JSON.stringify(args); } catch {}
              if (argStr.length > 120) argStr = argStr.slice(0, 120) + "...";
              return <div style={{ color: S.textMuted }}><span style={{ color: "#d4883a" }}>{fn}</span>({argStr})</div>;
            }
            return <span style={{ color: S.textMuted, fontStyle: "italic" }}>(空)</span>;
          })()}
        </div>
        {!selectMode && (overflows || expanded) && (
          <div className="mt-1 flex justify-center">
            <button
              className="rounded-full px-2 py-0.5 text-[10px]"
              style={{ color: S.accentDark, background: "rgba(232,160,191,0.1)" }}
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            >
              {expanded ? "收起" : "查看更多"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Main page ── */

export default function Memories() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [highlightId, setHighlightId] = useState(null);
  const [highlightToast, setHighlightToast] = useState(null);
  const highlightRef = useRef(null);
  const [tab, setTab] = useState("memories");
  const [layersMode, setLayersMode] = useState(false);
  const [trashMode, setTrashMode] = useState(false);
  const [lightboxUrl, setLightboxUrl] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [assistantName, setAssistantName] = useState(null);
  const [searchText, setSearchText] = useState("");
  const [confirm, setConfirm] = useState(null);
  const [editing, setEditing] = useState(null); // { type, id, text }
  const [versionsModal, setVersionsModal] = useState(null); // { type: "memory"|"summary", id }

  // Filters
  const [filterKlass, setFilterKlass] = useState("");
  const [filterMood, setFilterMood] = useState("");
  const [filterRole, setFilterRole] = useState("");
  const [searchMode, setSearchMode] = useState("text"); // "text" | "summary_id"

  // Multi-select
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState(new Set());

  const [memories, setMemories] = useState([]);
  const [summaries, setSummaries] = useState([]);
  const [messages, setMessages] = useState([]);
  const [trashMemories, setTrashMemories] = useState([]);
  const [trashSummaries, setTrashSummaries] = useState([]);
  const [layers, setLayers] = useState({ longterm: null, daily: null });
  const [layersLoading, setLayersLoading] = useState(false);
  const [editingLayer, setEditingLayer] = useState(null); // { type: "longterm"|"daily", content: "..." }
  const [layerHistory, setLayerHistory] = useState(null); // { type, items }
  const [archiving, setArchiving] = useState(false);
  const [archiveResult, setArchiveResult] = useState(null);
  const [mergeInfo, setMergeInfo] = useState(null);
  const [mergeDialog, setMergeDialog] = useState(false);
  const [merging, setMerging] = useState(null); // "daily" | "longterm" | null
  const [mergeResult, setMergeResult] = useState(null);
  const countdownRef = useRef(null); // remaining_seconds for live tick
  const countdownTimerRef = useRef(null);

  const [loading, setLoading] = useState(true);
  const [hasMoreMem, setHasMoreMem] = useState(false);
  const [hasMoreSum, setHasMoreSum] = useState(false);
  const [hasMoreMsg, setHasMoreMsg] = useState(false);
  const [totalMem, setTotalMem] = useState(0);
  const [totalSum, setTotalSum] = useState(0);
  const [totalMsg, setTotalMsg] = useState(0);

  const scrollRef = useRef(null);
  // Exit select mode + reset scroll on tab/layers change
  useEffect(() => { setSelectMode(false); setSelectedIds(new Set()); scrollRef.current?.scrollTo(0, 0); }, [tab, layersMode, trashMode]);

  const loadLayers = () => {
    setLayersLoading(true);
    apiFetch("/api/settings/summary-layers")
      .then((d) => setLayers({ longterm: d.longterm, daily: d.daily }))
      .catch((e) => console.error(e))
      .finally(() => setLayersLoading(false));
  };

  // Load merge info and start live countdown
  const loadMergeInfo = () => {
    apiFetch("/api/settings/merge-info").then((info) => {
      setMergeInfo(info);
      countdownRef.current = info.remaining_seconds;
    }).catch(() => {});
  };

  // Load summary layers when entering layers view
  useEffect(() => {
    if (tab !== "messages" || !layersMode) return;
    loadLayers();
    loadMergeInfo();
    // Live countdown tick every 60s
    countdownTimerRef.current = setInterval(() => {
      countdownRef.current = Math.max(0, (countdownRef.current || 0) - 60);
      const s = countdownRef.current;
      const d = Math.floor(s / 86400);
      const h = Math.floor((s % 86400) / 3600);
      const m = Math.floor((s % 3600) / 60);
      setMergeInfo((prev) => prev ? { ...prev, days_left: d, hours_left: h, minutes_left: m, remaining_seconds: s } : prev);
    }, 60000);
    return () => { if (countdownTimerRef.current) clearInterval(countdownTimerRef.current); };
  }, [tab, layersMode]);

  const openLayerHistory = async (type) => {
    try {
      const data = await apiFetch(`/api/settings/summary-layers/${type}/history`);
      const layer = layers[type];
      const histItems = data.history || [];
      // Prepend current version so it's always visible
      if (layer) {
        histItems.unshift({
          id: "__current__",
          version: layer.version || 1,
          content: layer.content || "",
          merged_summary_ids: null,
          created_at: layer.updated_at || null,
          is_current: true,
        });
      }
      setLayerHistory({ type, items: histItems });
    } catch (_e) { /* ignore */ }
  };

  const doArchive = async () => {
    setArchiving(true); setArchiveResult(null);
    try {
      const res = await apiFetch("/api/settings/summary-layers/archive", { method: "POST" });
      setArchiveResult(res.flushed ? `归档 ${res.flushed} 条` : "无需归档");
      if (res.flushed) setTimeout(loadLayers, 1500);
      setTimeout(() => setArchiveResult(null), 3000);
    } catch (_e) {
      setArchiveResult("归档失败");
      setTimeout(() => setArchiveResult(null), 3000);
    } finally { setArchiving(false); }
  };

  const doMergeDaily = async () => {
    setMergeDialog(false); setMerging("daily"); setMergeResult(null);
    try {
      await apiFetch("/api/settings/summary-layers/merge-daily", { method: "POST" });
      setMergeResult("正在合并...");
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        try {
          const st = await apiFetch("/api/settings/summary-layers/flush-status");
          if (!st.pending_merge?.length) {
            setMergeResult("合并完成"); loadLayers();
            setTimeout(() => setMergeResult(null), 3000);
            return;
          }
        } catch (_) { /* keep polling */ }
      }
      setMergeResult("合并超时");
      setTimeout(() => setMergeResult(null), 3000);
    } catch (_e) {
      setMergeResult("合并失败");
      setTimeout(() => setMergeResult(null), 3000);
    } finally { setMerging(null); }
  };

  const doMergeToLongterm = async () => {
    setMergeDialog(false); setMerging("longterm"); setMergeResult(null);
    try {
      const info = await apiFetch("/api/settings/summary-layers/merge-to-longterm", { method: "POST" });
      setMergeResult("正在合并至长期...");
      // Update countdown immediately
      setMergeInfo(info);
      countdownRef.current = info.remaining_seconds;
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        try {
          const st = await apiFetch("/api/settings/summary-layers/flush-status");
          if (!st.pending_merge?.length) {
            setMergeResult("合并完成"); loadLayers();
            setTimeout(() => setMergeResult(null), 3000);
            return;
          }
        } catch (_) { /* keep polling */ }
      }
      setMergeResult("合并超时");
      setTimeout(() => setMergeResult(null), 3000);
    } catch (_e) {
      setMergeResult("合并失败");
      setTimeout(() => setMergeResult(null), 3000);
    } finally { setMerging(null); }
  };

  // Check if daily layer has pending merge (for enabling "合并近期" button)
  const dailyHasPending = useMemo(() => {
    const d = layers.daily;
    return d?.needs_merge || (d?.pending_ids?.length > 0);
  }, [layers.daily]);

  // Check if daily layer has any content (for enabling "合并至长期" button)
  const dailyHasContent = useMemo(() => {
    const d = layers.daily;
    return !!(d?.content?.trim());
  }, [layers.daily]);

  // Load sessions
  useEffect(() => {
    apiFetch("/api/sessions?limit=50").then((d) => {
      const list = d.sessions || [];
      setSessions(list);
      if (list.length > 0 && !sessionId) setSessionId(list[0].id);
    }).catch(() => {});
  }, []);

  // Load assistant name
  useEffect(() => {
    if (!sessionId) return;
    apiFetch(`/api/sessions/${sessionId}/info`).then((d) => setAssistantName(d.assistant_name || null)).catch(() => setAssistantName(null));
  }, [sessionId]);

  // Load data
  const PAGE_SIZE = 50;
  const searchRef = useRef("");
  const debounceRef = useRef(null);
  const filterRef = useRef({ klass: "", mood: "", role: "" });
  filterRef.current = { klass: filterKlass, mood: filterMood, role: filterRole };
  const searchModeRef = useRef("text");
  searchModeRef.current = searchMode;

  useEffect(() => { if (sessionId) loadData(); }, [sessionId, trashMode]);
  useEffect(() => { if (sessionId && !trashMode) loadData(); }, [tab]);
  useEffect(() => { if (sessionId && !trashMode) loadData(); }, [filterKlass, filterMood, filterRole]);

  const loadDataRef = useRef(null);
  const pollRef = useRef(null);
  const selectModeRef = useRef(false);
  useEffect(() => { selectModeRef.current = selectMode; }, [selectMode]);

  // Start polling on mount, stop on unmount.
  // Polling is kept at 5s as a fallback in case the WebSocket below drops a
  // notification — the WS push is the primary low-latency path.
  useEffect(() => {
    pollRef.current = setInterval(() => {
      if (!document.hidden && loadDataRef.current && !selectModeRef.current) {
        loadDataRef.current(undefined, true);
      }
    }, 5000);
    const onVisible = () => {
      if (!document.hidden && loadDataRef.current) loadDataRef.current(undefined, true);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(pollRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // WebSocket push: subscribe to /ws/cot and re-fetch when backend emits
  // 'messages_updated' for this session. Bursts (tool-call rounds write
  // several messages back-to-back) are collapsed via a 300ms debounce.
  //
  // Heartbeat: the upstream proxy (cloudflare/nginx) closes idle WS after
  // ~60s. cot_broadcaster.publish silently drops events when there are no
  // clients, so a dropped connection means lost events. We send a ping
  // frame every 30s so the server sees traffic and keeps the socket alive.
  useEffect(() => {
    if (!sessionId) return;
    const token = localStorage.getItem("whisper_token");
    if (!token) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/ws/cot?token=${encodeURIComponent(token)}`;
    let ws;
    let reconnectTimer;
    let debounceTimer;
    let pingTimer;
    let closed = false;
    const connect = () => {
      if (closed) return;
      ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        clearInterval(pingTimer);
        pingTimer = setInterval(() => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ type: "ping" })); } catch {}
          }
        }, 30000);
        // On (re)connect, re-fetch once — we may have missed events while
        // the socket was down.
        if (loadDataRef.current && !selectModeRef.current) {
          loadDataRef.current(undefined, true);
        }
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type !== "messages_updated") return;
          if (msg.session_id != null && msg.session_id !== sessionId) return;
          clearTimeout(debounceTimer);
          debounceTimer = setTimeout(() => {
            if (!document.hidden && loadDataRef.current && !selectModeRef.current) {
              loadDataRef.current(undefined, true);
            }
          }, 300);
        } catch {}
      };
      ws.onclose = () => {
        clearInterval(pingTimer);
        if (closed) return;
        reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => {};
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(reconnectTimer);
      clearTimeout(debounceTimer);
      clearInterval(pingTimer);
      if (ws && ws.readyState <= 1) ws.close();
    };
  }, [sessionId]);

  // Highlight a memory card from URL param (?highlight=123)
  const pendingHighlight = useRef(null);
  useEffect(() => {
    const hId = searchParams.get("highlight");
    if (hId) {
      pendingHighlight.current = parseInt(hId, 10);
      setSearchParams({}, { replace: true });
    }
    const id = pendingHighlight.current;
    if (!id || memories.length === 0) return;
    if (memories.some(m => m.id === id)) {
      pendingHighlight.current = null;
      setHighlightId(id);
      setTimeout(() => highlightRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }), 100);
      setTimeout(() => setHighlightId(null), 2500);
    }
  }, [memories, searchParams]);

  // Load enough data when pendingHighlight is set but memory not found
  const highlightLoading = useRef(false);
  useEffect(() => {
    const id = pendingHighlight.current;
    if (!id || memories.length === 0 || memories.some(m => m.id === id) || highlightLoading.current) return;
    highlightLoading.current = true;
    apiFetch(`/api/memories/${id}/position`).then(({ position }) => {
      // Load from offset 0 up to position+10, in batches of PAGE_SIZE
      const needed = position + 10;
      if (needed <= memories.length) return;
      // Load the gap between what we have and what we need
      const extra = _buildParams();
      const offset = memories.length;
      const limit = Math.min(needed - offset, 100);
      return apiFetch(`/api/memories?limit=${limit}&offset=${offset}${extra}`).then(d => {
        const more = d.memories || [];
        setMemories(prev => [...prev, ...more]);
        setTotalMem(d.total || 0);
        setHasMoreMem((d.total || 0) > memories.length + more.length);
      });
    }).catch(() => {
      pendingHighlight.current = null;
      setHighlightToast("定位失败，该条目不存在");
      setTimeout(() => setHighlightToast(null), 2000);
    }).finally(() => { highlightLoading.current = false; });
  }, [memories]);

  // Debounced server-side search
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      const kw = searchText.trim();
      if (kw !== searchRef.current) {
        searchRef.current = kw;
        if (sessionId) loadData(kw);
      }
    }, 400);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [searchText]);

  const _buildParams = (kwOverride) => {
    const parts = [];
    const kw = (kwOverride ?? (searchRef.current || "")).trim();
    const mode = searchModeRef.current;
    if (tab === "messages" && mode === "summary_id" && kw) {
      // 按摘要ID搜索 — 传 summary_group_id 参数，不传 search
      const sid = parseInt(kw, 10);
      if (!isNaN(sid)) parts.push(`summary_group_id=${sid}`);
    } else {
      if (kw) parts.push(`search=${encodeURIComponent(kw)}`);
    }
    const f = filterRef.current;
    if (tab === "memories" && f.klass) parts.push(`klass=${encodeURIComponent(f.klass)}`);
    if (tab === "summaries" && f.mood) parts.push(`mood_tag=${encodeURIComponent(f.mood)}`);
    if (tab === "messages" && mode !== "summary_id" && f.role) {
      if (f.role === "no_message") {
        parts.push("only_no_message=true");
      } else if (f.role === "thinking") {
        parts.push("only_thinking=true");
      } else if (f.role === "native_thinking") {
        parts.push("only_native_thinking=true");
      } else if (f.role === "cafe") {
        parts.push("only_cafe=true");
      } else if (f.role === "tool") {
        parts.push("only_tool=true");
      } else {
        parts.push(`role=${encodeURIComponent(f.role)}`);
      }
    }
    return parts.length ? `&${parts.join("&")}` : "";
  };

  const loadData = async (kwOverride, silent = false) => {
    if (!sessionId) return;
    if (!silent) setLoading(true);
    const extra = _buildParams(kwOverride);
    try {
      if (trashMode) {
        const [memT, sumT] = await Promise.all([
          apiFetch("/api/memories/trash?limit=100"),
          apiFetch(`/api/sessions/${sessionId}/summaries/trash`),
        ]);
        setTrashMemories(memT.memories || []);
        setTrashSummaries(sumT.summaries || []);
      } else if (tab === "memories") {
        const d = await apiFetch(`/api/memories?limit=${PAGE_SIZE}&offset=0${extra}`);
        if (silent) {
          // Polling: only prepend truly new items, don't replace loaded list
          setMemories((prev) => {
            const existingIds = new Set(prev.map((m) => m.id));
            const fresh = (d.memories || []).filter((m) => !existingIds.has(m.id));
            return fresh.length ? [...fresh, ...prev] : prev;
          });
        } else {
          setMemories(d.memories || []);
        }
        setTotalMem(d.total || 0);
        setHasMoreMem((d.total || 0) > (d.memories || []).length);
      } else if (tab === "summaries") {
        const d = await apiFetch(`/api/sessions/${sessionId}/summaries?limit=${PAGE_SIZE}&offset=0${extra}`);
        if (silent) {
          setSummaries((prev) => {
            const existingIds = new Set(prev.map((s) => s.id));
            const fresh = (d.summaries || []).filter((s) => !existingIds.has(s.id));
            return fresh.length ? [...fresh, ...prev] : prev;
          });
        } else {
          setSummaries(d.summaries || []);
        }
        setTotalSum(d.total || 0);
        setHasMoreSum((d.total || 0) > (d.summaries || []).length);
      } else {
        const d = await apiFetch(`/api/sessions/${sessionId}/messages?limit=50${extra}`);
        const fetched = (d.messages || []).reverse();
        if (silent) {
          // Polling: only prepend new messages, preserve loaded history
          setMessages((prev) => {
            if (!prev.length) return fetched;
            const newestId = prev[0].id;
            const fresh = fetched.filter((m) => m.id > newestId);
            return fresh.length ? [...fresh, ...prev] : prev;
          });
        } else {
          setMessages(fetched);
        }
        setTotalMsg(d.total || 0);
        setHasMoreMsg(d.has_more || false);
      }
    } catch (e) { console.error(e); }
    if (!silent) setLoading(false);
  };

  // Keep ref updated so polling always calls the latest loadData
  loadDataRef.current = loadData;

  const loadMoreMem = async () => {
    if (!hasMoreMem || loading) return;
    setLoading(true);
    const extra = _buildParams();
    try {
      const d = await apiFetch(`/api/memories?limit=${PAGE_SIZE}&offset=${memories.length}${extra}`);
      const more = d.memories || [];
      setMemories((prev) => [...prev, ...more]);
      setHasMoreMem((d.total || 0) > memories.length + more.length);
    } catch (e) { console.error(e); }
    setLoading(false);
  };

  const loadMoreSum = async () => {
    if (!sessionId || !hasMoreSum || loading) return;
    setLoading(true);
    const extra = _buildParams();
    try {
      const d = await apiFetch(`/api/sessions/${sessionId}/summaries?limit=${PAGE_SIZE}&offset=${summaries.length}${extra}`);
      const more = d.summaries || [];
      setSummaries((prev) => [...prev, ...more]);
      setHasMoreSum((d.total || 0) > summaries.length + more.length);
    } catch (e) { console.error(e); }
    setLoading(false);
  };

  const loadMoreMsg = async () => {
    if (!sessionId || !hasMoreMsg || loading) return;
    const oldest = messages[messages.length - 1];
    if (!oldest) return;
    setLoading(true);
    const extra = _buildParams();
    try {
      const d = await apiFetch(`/api/sessions/${sessionId}/messages?limit=50&before_id=${oldest.id}${extra}`);
      setMessages((prev) => [...prev, ...(d.messages || []).reverse()]);
      setHasMoreMsg(d.has_more || false);
    } catch (e) { console.error(e); }
    setLoading(false);
  };

  const kw = searchText.trim();
  // Trash tabs still use local filtering (no backend search for trash)
  const filteredTrashMem = useMemo(() => kw ? trashMemories.filter((m) => m.content.toLowerCase().includes(kw.toLowerCase())) : trashMemories, [trashMemories, kw]);
  const filteredTrashSum = useMemo(() => kw ? trashSummaries.filter((s) => (s.summary_content || "").toLowerCase().includes(kw.toLowerCase())) : trashSummaries, [trashSummaries, kw]);

  // Actions
  const confirmAction = (message, action) => setConfirm({ message, action });

  const restoreMemory = async (id) => { await apiFetch(`/api/memories/${id}/restore`, { method: "POST" }); setTrashMemories((p) => p.filter((m) => m.id !== id)); };
  const permanentDeleteMemory = (id) => confirmAction("彻底删除后不可恢复，确定吗？", async () => { await apiFetch(`/api/memories/${id}/permanent`, { method: "DELETE" }); setTrashMemories((p) => p.filter((m) => m.id !== id)); });
  const restoreSummary = async (id) => { await apiFetch(`/api/sessions/${sessionId}/summaries/${id}/restore`, { method: "POST" }); setTrashSummaries((p) => p.filter((s) => s.id !== id)); };
  const permanentDeleteSummary = (id) => confirmAction("彻底删除后不可恢复，确定吗？", async () => { await apiFetch(`/api/sessions/${sessionId}/summaries/${id}/permanent`, { method: "DELETE" }); setTrashSummaries((p) => p.filter((s) => s.id !== id)); });

  // Multi-select helpers
  const toggleSelect = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const enterSelectMode = (id) => {
    setSelectMode(true);
    setSelectedIds(new Set([id]));
  };

  const selectAll = () => {
    let ids = [];
    if (tab === "memories") ids = memories.map((m) => m.id);
    else if (tab === "summaries") ids = summaries.map((s) => s.id);
    else ids = messages.map((m) => m.id);
    setSelectedIds(new Set(ids));
  };

  const cancelSelect = () => {
    setSelectMode(false);
    setSelectedIds(new Set());
  };

  const batchDelete = () => {
    if (selectedIds.size === 0) return;
    const count = selectedIds.size;
    const msg = tab === "messages"
      ? `确定要永久删除选中的 ${count} 条消息吗？此操作不可恢复。`
      : `确定要删除选中的 ${count} 条${tab === "memories" ? "记忆" : "摘要"}吗？删除后将移入回收站。`;
    confirmAction(msg, async () => {
      const ids = [...selectedIds];
      if (tab === "memories") {
        await apiFetch("/api/memories/batch", { method: "DELETE", body: { ids } });
        setMemories((p) => p.filter((m) => !selectedIds.has(m.id)));
      } else if (tab === "summaries") {
        await apiFetch(`/api/sessions/${sessionId}/summaries/batch`, { method: "DELETE", body: { ids } });
        setSummaries((p) => p.filter((s) => !selectedIds.has(s.id)));
      } else {
        await apiFetch(`/api/sessions/${sessionId}/messages/batch`, { method: "DELETE", body: { ids } });
        setMessages((p) => p.filter((m) => !selectedIds.has(m.id)));
      }
      cancelSelect();
    });
  };

  const saveEdit = async (text, klass, tags, disclosure) => {
    if (!editing) return;
    try {
      if (editing.type === "memory") {
        const body = { content: text };
        if (klass !== undefined) body.klass = klass;
        if (tags !== undefined) body.tags = tags;
        if (disclosure !== undefined) body.disclosure = disclosure;
        await apiFetch(`/api/memories/${editing.id}`, { method: "PUT", body });
        setMemories((p) => p.map((m) => m.id === editing.id ? { ...m, content: text, ...(klass !== undefined ? { klass } : {}), ...(tags !== undefined ? { tags } : {}), ...(disclosure !== undefined ? { disclosure } : {}) } : m));
      } else {
        await apiFetch(`/api/sessions/${sessionId}/summaries/${editing.id}`, { method: "PATCH", body: { summary_content: text } });
        setSummaries((p) => p.map((s) => s.id === editing.id ? { ...s, summary_content: text } : s));
      }
    } catch (e) { console.error(e); }
    setEditing(null);
  };

  const saveLayerEdit = (text) => {
    if (!editingLayer) return;
    const layerType = editingLayer.type;
    setEditingLayer(null);
    setConfirm({
      title: "确认修改",
      message: "修改后将直接覆盖，不会自动恢复。",
      confirmLabel: "保存",
      confirmColor: S.accentDark,
      action: async () => {
        await apiFetch(`/api/settings/summary-layers/${layerType}`, { method: "PUT", body: { content: text } });
        loadLayers();
      },
    });
  };

  const roleLabel = (role) => { if (role === "user") return "我"; if (role === "assistant") return assistantName || "助手"; if (role === "tool") return "工具"; return "系统"; };
  const roleColor = (role) => { if (role === "user") return S.accentDark; if (role === "assistant") return "#8d68c4"; if (role === "tool") return "#d4883a"; return S.textMuted; };

  // Filter options for current tab
  const filterOptions = tab === "memories" ? KLASS_OPTIONS : tab === "summaries" ? MOOD_OPTIONS : ROLE_OPTIONS;
  const filterValue = tab === "memories" ? filterKlass : tab === "summaries" ? filterMood : filterRole;
  const setFilterValue = (v) => {
    if (tab === "memories") setFilterKlass(v);
    else if (tab === "summaries") setFilterMood(v);
    else setFilterRole(v);
  };

  /* ── Render ── */

  const renderMemories = () => {
    if (loading && memories.length === 0) return <Spinner />;
    if (memories.length === 0) return <Empty text={kw ? "无匹配记忆" : "暂无记忆"} />;
    return (
      <>
        {!selectMode && <p className="mb-2 text-[11px] flex justify-between" style={{ color: S.textMuted }}><span>长按卡片可多选删除</span><span>共 {totalMem} 条</span></p>}
        {memories.map((mem) => (
          <ExpandableCard key={mem.id}
            cardRef={highlightId === mem.id ? highlightRef : undefined}
            isHighlighted={highlightId === mem.id}
            time={mem.updated_at ? `${fmtTime(mem.created_at)} · 更新于 ${fmtTime(mem.updated_at)}` : fmtTime(mem.created_at)}
            keyword={kw}
            onEdit={() => setEditing({ type: "memory", id: mem.id, text: mem.content, klass: mem.klass, tags: mem.tags, disclosure: mem.disclosure })}
            onVersions={() => setVersionsModal({ type: "memory", id: mem.id })}
            selectMode={selectMode}
            selected={selectedIds.has(mem.id)}
            onToggle={() => toggleSelect(mem.id)}
            onLongPress={() => enterSelectMode(mem.id)}
            charLimit={120}
            badge={(() => { const c = KLASS_COLORS[mem.klass] || KLASS_COLORS.other; const topics = mem.tags?.topic || []; return (<><div className="flex flex-wrap items-center gap-1 mb-1"><span className="inline-block rounded-full px-2 py-0.5 text-[10px] font-medium" style={{ background: c.bg, color: c.color }}>{mem.klass}</span><span className="inline-block text-[10px]" style={{ color: S.textMuted }}>#{mem.id}</span>{topics.map((t, i) => (<span key={i} className="inline-block rounded-full px-1.5 py-0.5 text-[9px]" style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}>{t}</span>))}</div>{mem.disclosure && <p className="text-[10px] mb-1" style={{ color: S.textMuted }}>触发: {mem.disclosure}</p>}</>); })()}
          >{mem.content}</ExpandableCard>
        ))}
        {hasMoreMem && (
          <button className="mx-auto mt-2 block rounded-[10px] px-4 py-2 text-[12px]" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }} onClick={loadMoreMem} disabled={loading}>
            {loading ? "加载中..." : "加载更多"}
          </button>
        )}
      </>
    );
  };

  const renderSummaries = () => {
    if (loading && summaries.length === 0) return <Spinner />;
    if (summaries.length === 0) return <Empty text={kw ? "无匹配摘要" : "暂无摘要"} />;
    return (
      <>
        {!selectMode && <p className="mb-2 text-[11px] flex justify-between" style={{ color: S.textMuted }}><span>长按卡片可多选删除</span><span>共 {totalSum} 条</span></p>}
        {summaries.map((s) => (
          <ExpandableCard key={s.id} time={fmtTime(s.created_at)} keyword={kw}
            onEdit={() => setEditing({ type: "summary", id: s.id, text: s.summary_content })}
            onVersions={() => setVersionsModal({ type: "summary", id: s.id })}
            selectMode={selectMode}
            selected={selectedIds.has(s.id)}
            onToggle={() => toggleSelect(s.id)}
            onLongPress={() => enterSelectMode(s.id)}
            badge={<div className="flex flex-wrap items-center gap-1 mb-1">{s.mood_tag && <span className="inline-block rounded-full px-2 py-0.5 text-[10px] font-medium" style={{ background: "rgba(232,160,191,0.15)", color: S.accentDark }}>{s.mood_tag}</span>}{s.merged_into && <span className="inline-block rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(155,120,200,0.12)", color: "#8b6abf" }}>已归档至{s.merged_into === "daily" ? "近期" : "长期"}</span>}<span className="inline-block text-[10px]" style={{ color: S.textMuted }}>#{s.id}</span></div>}
          >{s.summary_content || "(空)"}</ExpandableCard>
        ))}
        {hasMoreSum && (
          <button className="mx-auto mt-2 block rounded-[10px] px-4 py-2 text-[12px]" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }} onClick={loadMoreSum} disabled={loading}>
            {loading ? "加载中..." : "加载更多"}
          </button>
        )}
      </>
    );
  };

  const renderMessages = () => {
    if (loading && messages.length === 0) return <Spinner />;
    if (messages.length === 0) return <Empty text={kw ? "无匹配消息" : "暂无消息"} />;
    return (
      <>
        {!selectMode && <p className="mb-2 text-[11px] flex justify-between" style={{ color: S.textMuted }}><span>长按卡片可多选删除</span><span>共 {totalMsg} 条</span></p>}
        {messages.map((msg) => (
          <MessageCard key={msg.id} msg={msg} keyword={kw} roleLabel={roleLabel} roleColor={roleColor}
            selectMode={selectMode}
            selected={selectedIds.has(msg.id)}
            onToggle={() => toggleSelect(msg.id)}
            onLongPress={() => enterSelectMode(msg.id)}
            onImageClick={setLightboxUrl}
          />
        ))}
        {hasMoreMsg && (
          <button className="mx-auto mt-2 block rounded-[10px] px-4 py-2 text-[12px]" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }} onClick={loadMoreMsg} disabled={loading}>
            {loading ? "加载中..." : "加载更多"}
          </button>
        )}
      </>
    );
  };

  const renderTrash = () => {
    if (loading) return <Spinner />;
    const items = tab === "memories" ? filteredTrashMem : filteredTrashSum;
    if (items.length === 0) return <Empty text={kw ? "无匹配项" : "回收站为空"} />;
    return items.map((item) => (
      <TrashCard key={item.id} id={item.id} content={item.content || item.summary_content || "(空)"} deletedAt={item.deleted_at} createdAt={item.created_at} klass={item.klass} moodTag={item.mood_tag} keyword={kw}
        onRestore={() => (tab === "memories" ? restoreMemory(item.id) : restoreSummary(item.id))}
        onPermanentDelete={() => (tab === "memories" ? permanentDeleteMemory(item.id) : permanentDeleteSummary(item.id))}
      />
    ));
  };

  const renderLayers = () => {
    if (layersLoading) return <Spinner />;
    return (
      <div className="space-y-4">
        {[
          { type: "daily", label: "近期记忆", hint: "7天内的合并回顾" },
          { type: "longterm", label: "长期记忆", hint: "关系脉络、重大事件" },
        ].map(({ type, label, hint }) => {
          const layer = layers[type];
          const hasContent = layer?.content?.trim();
          return (
            <div key={type} className="rounded-[18px] p-4" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}>
              <div className="flex items-start justify-between mb-3">
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[15px] font-semibold" style={{ color: S.text }}>{label}</span>
                    {(() => {
                      const rawN = layer?.pending_ids?.length || 0;
                      const dailyN = (layer?.pending_daily || []).reduce((sum, g) => sum + g.ids.length, 0);
                      if (!rawN && !dailyN) return null;
                      const parts = [];
                      if (rawN) parts.push(`${rawN}条原始待合并`);
                      if (dailyN) parts.push(`${dailyN}条来自daily`);
                      return (
                        <PendingBadge
                          label={parts.join(" · ")}
                          rawIds={layer?.pending_ids || []}
                          dailyGroups={layer?.pending_daily || []}
                        />
                      );
                    })()}
                  </div>
                  <div className="text-[11px]" style={{ color: S.textMuted }}>{hint}</div>
                </div>
                <div className="flex items-center gap-2 mt-0.5">
                  <button
                    className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
                    style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
                    onClick={() => openLayerHistory(type)}
                  >
                    <History size={11} style={{ color: S.textMuted }} />
                  </button>
                  <button
                    className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
                    style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
                    onClick={() => setEditingLayer({ type, content: layer?.content || "" })}
                  >
                    <Pencil size={11} style={{ color: S.accentDark }} />
                  </button>
                </div>
              </div>
              <div
                className="text-[12px] leading-relaxed whitespace-pre-wrap break-words"
                style={{ color: hasContent ? S.text : S.textMuted }}
              >
                {hasContent ? layer.content : "暂无内容"}
              </div>
              <div className="mt-2 flex items-center justify-between">
                <span className="text-[10px]" style={{ color: S.textMuted }}>
                  {layer?.updated_at ? `更新于 ${fmtTime(layer.updated_at)}` : ""}
                </span>
                <span className="text-[10px]" style={{ color: S.textMuted }}>
                  v{layer?.version || 1} · {layer?.content?.length || 0} 字
                </span>
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  const content = trashMode ? renderTrash() : tab === "memories" ? renderMemories() : tab === "summaries" ? renderSummaries() : layersMode ? renderLayers() : renderMessages();

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between px-5 pb-3" style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}>
        <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }} onClick={() => navigate("/", { replace: true })}>
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>
          {trashMode ? "回收站" : "记忆管理"}
        </h1>
        {tab === "messages" ? (
          <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: layersMode ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }} onClick={() => setLayersMode(!layersMode)}>
            <BookOpen size={16} style={{ color: layersMode ? S.accentDark : S.textMuted }} />
          </button>
        ) : (
          <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: trashMode ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }} onClick={() => setTrashMode(!trashMode)}>
            <Trash2 size={16} style={{ color: trashMode ? "#ef4444" : S.accentDark }} />
          </button>
        )}
      </div>

      {/* Select mode toolbar */}
      {selectMode && (
        <div className="shrink-0 px-5 pb-2">
          <div className="flex items-center justify-between rounded-[14px] px-4 py-2.5" style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}>
            <span className="text-[12px] font-semibold" style={{ color: S.accentDark }}>已选 {selectedIds.size} 项</span>
            <div className="flex items-center gap-2">
              <button
                className="rounded-[10px] px-3 py-1.5 text-[12px] font-semibold"
                style={{ background: "rgba(59,130,246,0.15)", color: "#3b82f6" }}
                onClick={selectAll}
              >全选</button>
              <button
                className="rounded-[10px] px-3 py-1.5 text-[12px] font-semibold"
                style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.textMuted }}
                onClick={cancelSelect}
              >取消</button>
              <button
                className="rounded-[10px] px-3 py-1.5 text-[12px] font-semibold text-white"
                style={{ background: selectedIds.size > 0 ? "#ff4d6d" : "rgba(255,77,109,0.3)" }}
                onClick={batchDelete}
                disabled={selectedIds.size === 0}
              >删除</button>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      {!selectMode && (
        <div className="shrink-0 px-5 pb-2">
          <div className="flex rounded-[14px] p-1" style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}>
            {TABS.map((t) => {
              const disabled = trashMode && t.key === "messages";
              const active = tab === t.key;
              const label = t.key === "messages" && layersMode ? "长期记忆" : t.label;
              return (
                <button key={t.key} className="flex-1 rounded-[12px] py-2 text-[12px] font-medium transition-all"
                  style={disabled ? { color: S.textMuted, opacity: 0.35, cursor: "default" } : active ? { background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark } : { color: S.textMuted }}
                  disabled={disabled} onClick={() => !disabled && setTab(t.key)}
                >{label}</button>
              );
            })}
          </div>
        </div>
      )}

      {/* Session dropdown + Filter + Search */}
      {!selectMode && !(tab === "messages" && layersMode) && (
        <div className="shrink-0 px-5 pb-3">
          <div className="flex items-center gap-1.5">
            {/* Session dropdown */}
            <FilterDropdown
              value={sessionId ? `#${sessionId}` : ""}
              rawValue={sessionId ?? ""}
              onChange={(v) => setSessionId(Number(v))}
              options={sessions.map((s) => ({ value: String(s.id), label: `#${s.id} ${s.title || ""}` }))}
              width="22%"
            />

            {/* Filter dropdown */}
            <FilterDropdown
              value={filterOptions.find((o) => o.value === filterValue)?.label || filterOptions[0].label}
              rawValue={filterValue}
              onChange={setFilterValue}
              options={filterOptions.map((o) => ({ value: o.value, label: o.label }))}
              width="22%"
              active={!!filterValue}
            />

            {/* Search */}
            <div className="relative flex-1">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 z-10" style={{ color: S.textMuted }} />
              <input
                className="w-full rounded-[12px] py-2 pl-8 text-[11px] outline-none"
                style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text, paddingRight: tab === "messages" ? (searchMode === "summary_id" ? 90 : 50) : 28 }}
                placeholder={searchMode === "summary_id" ? "输入摘要ID..." : "搜索..."}
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                inputMode={searchMode === "summary_id" ? "numeric" : "text"}
              />
              {/* Right side inside input: clear button + optional tag + dropdown trigger */}
              <div className="absolute right-1.5 top-1/2 -translate-y-1/2 flex items-center gap-1">
                {searchText && (
                  <button onClick={() => setSearchText("")}>
                    <X size={12} style={{ color: S.textMuted }} />
                  </button>
                )}
                {tab === "messages" && searchMode === "summary_id" && (
                  <span className="rounded-full px-1.5 py-0.5 text-[9px] font-medium" style={{ background: "rgba(74,138,181,0.15)", color: "#4a8ab5" }}>
                    摘要ID
                  </span>
                )}
                {tab === "messages" && (
                  <SearchModeButton value={searchMode} onChange={(v) => { setSearchMode(v); setSearchText(""); }} />
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Content */}
      <div ref={scrollRef} className={`flex-1 overflow-y-auto thin-scrollbar px-5${tab === "messages" && layersMode ? " pt-5 pb-4" : " pb-8"}`}>{content}</div>

      {/* Layers bottom bar */}
      {tab === "messages" && layersMode && (
        <div
          className="shrink-0 px-5 pb-5 pt-3 space-y-2"
          style={{ background: S.bg, paddingBottom: "max(1.25rem, env(safe-area-inset-bottom))" }}
        >
          {mergeInfo && (
            <div className="text-[11px] text-center" style={{ color: S.textMuted }}>
              {mergeInfo.days_left > 0
                ? `${mergeInfo.days_left}天${mergeInfo.hours_left}小时${mergeInfo.minutes_left}分钟后自动合并近期记忆 → 长期记忆`
                : mergeInfo.hours_left > 0
                  ? `${mergeInfo.hours_left}小时${mergeInfo.minutes_left}分钟后自动合并近期记忆 → 长期记忆`
                  : mergeInfo.minutes_left > 0
                    ? `${mergeInfo.minutes_left}分钟后自动合并近期记忆 → 长期记忆`
                    : "即将自动合并近期记忆 → 长期记忆"}
            </div>
          )}
          <div className="flex gap-2">
            <button
              className="flex flex-1 items-center justify-center gap-1.5 rounded-[14px] py-3 text-[13px] font-semibold text-white"
              style={{ background: "linear-gradient(135deg, var(--accent), var(--accent-dark))", boxShadow: "4px 4px 10px rgba(201,98,138,0.35)" }}
              onClick={doArchive}
              disabled={archiving || !!merging}
            >
              <RefreshCw size={13} className={archiving ? "animate-spin" : ""} />
              {archiveResult || (archiving ? "归档中..." : "归档")}
            </button>
            <button
              className="flex flex-1 items-center justify-center gap-1.5 rounded-[14px] py-3 text-[13px] font-semibold text-white"
              style={{ background: merging ? "#999" : "linear-gradient(135deg, var(--accent), var(--accent-dark))", boxShadow: merging ? "none" : "4px 4px 10px rgba(201,98,138,0.35)" }}
              onClick={() => !merging && setMergeDialog(true)}
              disabled={!!merging}
            >
              {mergeResult || (merging ? "合并中..." : "合并")}
            </button>
          </div>
        </div>
      )}

      {/* Modals */}
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          confirmLabel={confirm.confirmLabel}
          confirmColor={confirm.confirmColor}
          onConfirm={async () => { try { await confirm.action(); } catch (e) { console.error(e); } setConfirm(null); }}
          onCancel={() => setConfirm(null)}
        />
      )}
      {editing && <EditModal initialText={editing.text} onSave={saveEdit} onCancel={() => setEditing(null)} memoryData={editing.type === "memory" ? { klass: editing.klass, tags: editing.tags, disclosure: editing.disclosure } : null} />}
      {editingLayer && <EditModal initialText={editingLayer.content} onSave={saveLayerEdit} onCancel={() => setEditingLayer(null)} />}
      {layerHistory && (
        <LayerHistoryOverlay
          items={layerHistory.items}
          onApply={async (item) => {
            try {
              const res = await apiFetch(`/api/settings/summary-layers/${layerHistory.type}/rollback`, { method: "POST", body: { history_id: item.id } });
              setLayers((p) => ({ ...p, [layerHistory.type]: { ...p[layerHistory.type], content: res.content, version: res.version } }));
              loadLayers();
            } catch (_e) { /* ignore */ }
          }}
          onDelete={async (historyId) => {
            try {
              await apiFetch(`/api/settings/summary-layers/history/${historyId}`, { method: "DELETE" });
              setLayerHistory((p) => p ? { ...p, items: p.items.filter((h) => h.id !== historyId) } : null);
            } catch (_e) { /* ignore */ }
          }}
          onClose={() => setLayerHistory(null)}
        />
      )}
      {versionsModal && (
        <VersionsModal
          type={versionsModal.type}
          id={versionsModal.id}
          sessionId={sessionId}
          onClose={() => setVersionsModal(null)}
          onRollback={() => loadData()}
        />
      )}
      {mergeDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={() => setMergeDialog(false)}>
          <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-6" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
            <p className="mb-4 text-center text-[16px] font-bold" style={{ color: S.text }}>合并记忆</p>
            <div className="flex flex-col gap-3">
              <button
                className="w-full rounded-[16px] py-3 text-[15px] font-semibold text-white"
                style={{
                  background: dailyHasPending ? S.accentDark : "#bbb",
                  boxShadow: dailyHasPending ? "4px 4px 10px rgba(201,98,138,0.4)" : "none",
                  opacity: dailyHasPending ? 1 : 0.5,
                }}
                disabled={!dailyHasPending}
                onClick={doMergeDaily}
              >
                合并近期
              </button>
              <button
                className="w-full rounded-[16px] py-3 text-[15px] font-semibold text-white"
                style={{
                  background: dailyHasContent ? "linear-gradient(135deg, var(--accent), var(--accent-dark))" : "#bbb",
                  boxShadow: dailyHasContent ? "4px 4px 10px rgba(201,98,138,0.35)" : "none",
                  opacity: dailyHasContent ? 1 : 0.5,
                }}
                disabled={!dailyHasContent}
                onClick={doMergeToLongterm}
              >
                合并至长期
              </button>
              <button
                className="w-full rounded-[16px] py-3 text-[15px] font-semibold"
                style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }}
                onClick={() => setMergeDialog(false)}
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}
      {highlightToast && (
        <div className="fixed left-1/2 bottom-24 -translate-x-1/2 rounded-full px-4 py-2 text-[12px] font-medium shadow-lg z-50"
          style={{ background: "rgba(0,0,0,0.75)", color: "#fff" }}>
          {highlightToast}
        </div>
      )}
      {lightboxUrl && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.85)" }}
          onClick={() => setLightboxUrl(null)}
        >
          <img
            src={lightboxUrl}
            alt=""
            style={{ maxWidth: "90vw", maxHeight: "85vh", objectFit: "contain", borderRadius: 8 }}
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
}

function Spinner() {
  return <div className="flex justify-center py-16"><div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} /></div>;
}
function Empty({ text }) {
  return <p className="py-16 text-center text-[14px]" style={{ color: S.textMuted }}>{text}</p>;
}
