import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Check, Pencil, ChevronDown, Link } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

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

const KLASS_OPTIONS = [
  { value: "identity", label: "identity" },
  { value: "relationship", label: "relationship" },
  { value: "bond", label: "bond" },
  { value: "conflict", label: "conflict" },
  { value: "fact", label: "fact" },
  { value: "preference", label: "preference" },
  { value: "health", label: "health" },
  { value: "task", label: "task" },
  { value: "other", label: "other" },
];

/* ── Helpers ── */

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const h = d.getHours().toString().padStart(2, "0");
  const m = d.getMinutes().toString().padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${h}:${m}`;
}

/* ── Checkbox ── */

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

function ConfirmDialog({ title, message, confirmLabel = "确定", confirmColor = "#c9628a", onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={onCancel}>
      <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-6" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>{title}</p>
        <p className="mb-5 text-center text-[13px] whitespace-pre-line" style={{ color: S.textMuted }}>{message}</p>
        <div className="flex gap-3">
          <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={onCancel}>取消</button>
          <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold text-white" style={{ background: confirmColor, boxShadow: `4px 4px 10px ${confirmColor}66` }} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

/* ── Edit modal ── */

function EditModal({ item, onSave, onCancel }) {
  const [text, setText] = useState(item.content);
  const [klass, setKlass] = useState(item.klass || "other");
  const [tagsText, setTagsText] = useState((item.tags?.topic || []).join(", "));
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={onCancel}>
      <div className="mx-5 w-full max-w-[340px] rounded-[18px] p-4" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <textarea
          className="w-full rounded-[12px] p-3 text-[12px] leading-relaxed resize-none outline-none overflow-y-auto thin-scrollbar"
          style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text, minHeight: 100, maxHeight: 280 }}
          value={text}
          onChange={(e) => setText(e.target.value)}
          autoFocus
        />
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
                {KLASS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
              <ChevronDown size={10} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2" style={{ color: S.textMuted }} />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="shrink-0 text-[11px] font-medium" style={{ color: S.textMuted }}>标签</span>
            <input
              className="flex-1 rounded-[10px] px-3 py-1.5 text-[11px] outline-none"
              style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text }}
              placeholder="逗号分隔"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
            />
          </div>
        </div>
        <div className="mt-3 flex gap-3">
          <button className="flex-1 rounded-[12px] py-2 text-[12px] font-medium" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.textMuted }} onClick={onCancel}>取消</button>
          <button
            className="flex-1 rounded-[12px] py-2 text-[12px] font-medium"
            style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }}
            onClick={() => {
              const topics = tagsText.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
              onSave(text.trim(), klass, { topic: topics });
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

/* ── Related memory expandable ── */

function RelatedMemory({ relatedId, relatedContent, relatedKlass, relatedTags, similarity, onOverwrite }) {
  const [expanded, setExpanded] = useState(false);
  if (!relatedId || !relatedContent) return null;
  const pct = similarity ? Math.round(similarity * 100) : null;
  const rc = KLASS_COLORS[relatedKlass] || KLASS_COLORS.other;
  const rTopics = relatedTags?.topic || [];

  return (
    <div className="mt-1.5">
      <button
        className="flex items-center gap-1 text-[10px]"
        style={{ color: "#c47a30" }}
        onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
      >
        <Link size={10} />
        <span>相似记忆 #{relatedId}{pct ? ` (${pct}%)` : ""}</span>
        <ChevronDown size={10} style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.2s" }} />
      </button>
      {expanded && (
        <div className="mt-1 rounded-[10px] p-2.5" style={{ boxShadow: "var(--inset-shadow)", background: S.bg }}>
          <div className="flex flex-wrap items-center gap-1 mb-1">
            {relatedKlass && <span className="inline-block rounded-full px-2 py-0.5 text-[10px] font-medium" style={{ background: rc.bg, color: rc.color }}>{relatedKlass}</span>}
            {rTopics.map((t, i) => (
              <span key={i} className="inline-block rounded-full px-1.5 py-0.5 text-[9px]" style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}>{t}</span>
            ))}
          </div>
          <p className="text-[11px] leading-relaxed whitespace-pre-wrap" style={{ color: S.text }}>{relatedContent}</p>
          <div className="mt-2 flex justify-end">
            <button
              className="rounded-[8px] px-3 py-1 text-[10px] font-medium"
              style={{ background: "rgba(196,122,48,0.12)", color: "#c47a30" }}
              onClick={(e) => { e.stopPropagation(); onOverwrite(); }}
            >
              覆盖原有
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Card ── */

function PendingCard({ item, selected, onToggle, onEdit, onOverwrite }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const textRef = useRef(null);

  useLayoutEffect(() => {
    const el = textRef.current;
    if (el && !expanded) setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [item.content, expanded]);

  const c = KLASS_COLORS[item.klass] || KLASS_COLORS.other;
  const topics = item.tags?.topic || [];

  return (
    <div
      className="mb-3 rounded-[18px] p-3 flex items-start gap-2.5"
      style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
      onClick={onToggle}
    >
      <div className="mt-1">
        <SelectCircle selected={selected} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-1">
          <div className="flex-1 min-w-0">
            {/* Badge: klass + id + tags */}
            <div className="flex flex-wrap items-center gap-1 mb-1">
              <span className="inline-block rounded-full px-2 py-0.5 text-[10px] font-medium" style={{ background: c.bg, color: c.color }}>{item.klass}</span>
              <span className="inline-block text-[10px]" style={{ color: S.textMuted }}>#{item.id}</span>
              {topics.map((t, i) => (
                <span key={i} className="inline-block rounded-full px-1.5 py-0.5 text-[9px]" style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}>{t}</span>
              ))}
            </div>
            {/* Content */}
            <div
              ref={textRef}
              className="text-[12px] leading-relaxed break-words cursor-pointer"
              style={expanded ? { color: S.text } : { color: S.text, display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            >
              {item.content}
            </div>
            {/* Expand toggle */}
            {(overflows || expanded) && (
              <div className="mt-0.5 flex justify-end">
                <button
                  className="rounded-full px-2 py-0.5 text-[10px]"
                  style={{ color: S.accentDark, background: "rgba(232,160,191,0.1)" }}
                  onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
                >
                  {expanded ? "收起" : "查看更多"}
                </button>
              </div>
            )}
            {/* Related memory */}
            <RelatedMemory
              relatedId={item.related_memory_id}
              relatedContent={item.related_memory_content}
              relatedKlass={item.related_memory_klass}
              relatedTags={item.related_memory_tags}
              similarity={item.similarity}
              onOverwrite={onOverwrite}
            />
          </div>
          {/* Edit button */}
          <button
            className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
            style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
            onClick={(e) => { e.stopPropagation(); onEdit(); }}
          >
            <Pencil size={11} style={{ color: S.accentDark }} />
          </button>
        </div>
        <div className="mt-1">
          <span className="text-[10px]" style={{ color: S.textMuted }}>{fmtTime(item.created_at)}</span>
        </div>
      </div>
    </div>
  );
}

/* ── Summary group divider ── */

function SummaryDivider({ summaryId }) {
  return (
    <div className="flex items-center gap-2 my-3 px-1">
      <div className="flex-1 h-px" style={{ background: "rgba(136,136,160,0.2)" }} />
      <span className="text-[10px] font-medium shrink-0" style={{ color: S.textMuted }}>摘要 #{summaryId}</span>
      <div className="flex-1 h-px" style={{ background: "rgba(136,136,160,0.2)" }} />
    </div>
  );
}

/* ── Main page ── */

export default function PendingMemories() {
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [editing, setEditing] = useState(null); // item being edited
  const [dialog, setDialog] = useState(null); // { type, ... }
  const [busy, setBusy] = useState(false);

  const load = () => {
    setLoading(true);
    apiFetch("/api/pending-memories")
      .then((data) => {
        setItems(data.items || []);
        // Remove any selected ids that no longer exist
        setSelectedIds((prev) => {
          const validIds = new Set((data.items || []).map((i) => i.id));
          const next = new Set([...prev].filter((id) => validIds.has(id)));
          return next;
        });
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const toggleSelect = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    if (selectedIds.size === items.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(items.map((i) => i.id)));
    }
  };

  /* ── Actions ── */

  const handleConfirm = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await apiFetch("/api/pending-memories/confirm", {
        method: "POST",
        body: { ids: [...selectedIds] },
      });
      setSelectedIds(new Set());
      load();
    } catch {}
    setBusy(false);
    setDialog(null);
  };

  const handleDismiss = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await apiFetch("/api/pending-memories/dismiss", {
        method: "POST",
        body: { ids: [...selectedIds] },
      });
      setSelectedIds(new Set());
      load();
    } catch {}
    setBusy(false);
    setDialog(null);
  };

  const handleOverwrite = async (pendingId, targetMemoryId) => {
    if (busy) return;
    setBusy(true);
    try {
      await apiFetch("/api/pending-memories/update-existing", {
        method: "POST",
        body: { pending_id: pendingId, target_memory_id: targetMemoryId },
      });
      load();
    } catch {}
    setBusy(false);
    setDialog(null);
  };

  const handleEdit = async (content, klass, tags) => {
    if (!editing) return;
    try {
      await apiFetch(`/api/pending-memories/${editing.id}`, {
        method: "PATCH",
        body: { content, klass, tags },
      });
      load();
    } catch {}
    setEditing(null);
  };

  /* ── Group by summary_id ── */

  const groups = [];
  let currentSummaryId = Symbol(); // sentinel: never equals any real value
  for (const item of items) {
    if (item.summary_id !== currentSummaryId) {
      currentSummaryId = item.summary_id;
      groups.push({ summaryId: currentSummaryId, items: [] });
    }
    groups[groups.length - 1].items.push(item);
  }

  const allSelected = items.length > 0 && selectedIds.size === items.length;

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div
        className="flex shrink-0 items-center justify-between px-5 pb-3"
        style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}
      >
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => navigate(-1)}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>摘要提取记忆</h1>
        <div className="w-10" /> {/* spacer */}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-5 pb-6 pt-3">
        {loading ? (
          <div className="flex justify-center py-16">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" style={{ borderColor: `${S.accentDark} transparent ${S.accentDark} ${S.accentDark}` }} />
          </div>
        ) : items.length === 0 ? (
          <p className="py-16 text-center text-[13px]" style={{ color: S.textMuted }}>没有待审的提取记忆</p>
        ) : (
          groups.map((group, gi) => (
            <div key={gi}>
              {group.summaryId && <SummaryDivider summaryId={group.summaryId} />}
              {group.items.map((item) => (
                <PendingCard
                  key={item.id}
                  item={item}
                  selected={selectedIds.has(item.id)}
                  onToggle={() => toggleSelect(item.id)}
                  onEdit={() => setEditing(item)}
                  onOverwrite={() => {
                    setDialog({
                      type: "overwrite",
                      title: "覆盖确认",
                      message: `确定用这条提取记忆覆盖已有记忆 #${item.related_memory_id} 吗？`,
                      onConfirm: () => handleOverwrite(item.id, item.related_memory_id),
                    });
                  }}
                />
              ))}
            </div>
          ))
        )}
      </div>

      {/* Bottom bar */}
      <div
        className="flex shrink-0 gap-2 px-5 pb-5 pt-3"
        style={{ background: S.bg, paddingBottom: "max(1.25rem, env(safe-area-inset-bottom))" }}
      >
          {/* Select All 1/5 */}
          <button
            className="rounded-[14px] py-3 text-[13px] font-semibold"
            style={{ flex: 1, background: S.bg, boxShadow: allSelected ? "var(--inset-shadow)" : "var(--card-shadow-sm)", color: allSelected ? S.accentDark : S.text }}
            onClick={selectAll}
          >
            {allSelected ? "取消" : "全选"}
          </button>
          {/* Delete 2/5 */}
          <button
            className="rounded-[14px] py-3 text-[13px] font-semibold"
            style={{ flex: 2, background: S.bg, boxShadow: selectedIds.size > 0 ? "var(--card-shadow-sm)" : "var(--inset-shadow)", color: selectedIds.size > 0 ? "#ef4444" : S.textMuted }}
            disabled={selectedIds.size === 0}
            onClick={() => {
              setDialog({
                type: "dismiss",
                title: "删除确认",
                message: `确定删除选中的 ${selectedIds.size} 条记忆？\n删除后可在记忆回收站恢复`,
                confirmLabel: "删除",
                confirmColor: "#ef4444",
                onConfirm: handleDismiss,
              });
            }}
          >
            删除{selectedIds.size > 0 ? ` (${selectedIds.size})` : ""}
          </button>
          {/* Confirm Save 2/5 */}
          <button
            className="rounded-[14px] py-3 text-[13px] font-semibold text-white"
            style={{
              flex: 2,
              background: selectedIds.size > 0 ? "linear-gradient(135deg, var(--accent), var(--accent-dark))" : "#ccc",
              boxShadow: selectedIds.size > 0 ? "4px 4px 10px rgba(201,98,138,0.35)" : "none",
            }}
            disabled={selectedIds.size === 0}
            onClick={() => {
              setDialog({
                type: "confirm",
                title: "保存确认",
                message: `确定将选中的 ${selectedIds.size} 条记忆保存到记忆库？`,
                confirmLabel: "保存",
                onConfirm: handleConfirm,
              });
            }}
          >
            保存{selectedIds.size > 0 ? ` (${selectedIds.size})` : ""}
          </button>
      </div>

      {/* Edit modal */}
      {editing && (
        <EditModal
          item={editing}
          onSave={handleEdit}
          onCancel={() => setEditing(null)}
        />
      )}

      {/* Confirmation dialog */}
      {dialog && (
        <ConfirmDialog
          title={dialog.title}
          message={dialog.message}
          confirmLabel={dialog.confirmLabel || "确定"}
          confirmColor={dialog.confirmColor || "#c9628a"}
          onConfirm={dialog.onConfirm}
          onCancel={() => setDialog(null)}
        />
      )}
    </div>
  );
}
