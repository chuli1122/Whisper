import { useState, useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ChevronLeft, Plus, ChevronDown, ChevronRight, GripVertical, Trash2, Check, Search, X } from "lucide-react";
import {
  DndContext, closestCenter, PointerSensor, TouchSensor,
  useSensor, useSensors, DragOverlay,
} from "@dnd-kit/core";
import {
  SortableContext, useSortable, verticalListSortingStrategy, arrayMove,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const ACTIVATION_LABELS = {
  always: { label: "常驻", color: "#6b9b6e", bg: "#e0f0e1" },
  keyword: { label: "关键词", color: "#9b6b6b", bg: "#f0e0e0" },
  mood: { label: "情绪", color: "#9b7a3b", bg: "#f0ebd8" },
  message_mode: { label: "消息模式", color: "#6b7b9b", bg: "#e0e6f0" },
};

function ActivationBadge({ activation }) {
  const meta = ACTIVATION_LABELS[activation] || ACTIVATION_LABELS.always;
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ color: meta.color, background: meta.bg }}
    >
      {meta.label}
    </span>
  );
}

// ── Swipe-to-delete row ──
const SWIPE_WIDTH = 80;
const SNAP_THRESHOLD = 40;

function SwipeRow({ children, onDelete }) {
  const rowRef = useRef(null);
  const actRef = useRef(null);
  const state = useRef({
    startX: 0, startY: 0, base: 0, current: 0,
    dragging: false, locked: false, isH: false,
  });

  const translate = (x, animate) => {
    const el = rowRef.current;
    const act = actRef.current;
    if (!el) return;
    const ease = animate ? "all 0.25s cubic-bezier(.4,0,.2,1)" : "none";
    el.style.transition = ease;
    el.style.transform = x ? `translateX(${x}px)` : "";
    if (act) {
      const p = Math.min(1, Math.abs(x) / SWIPE_WIDTH);
      act.style.transition = ease;
      act.style.opacity = `${p}`;
    }
    if (!x) el.style.willChange = "auto";
    state.current.current = x;
  };

  const close = () => translate(0, true);

  const onTouchStart = (e) => {
    // Don't interfere with drag handle
    if (e.target.closest('.drag-handle')) return;
    const t = e.touches[0];
    const s = state.current;
    s.startX = t.clientX; s.startY = t.clientY;
    s.base = s.current; s.dragging = true;
    s.locked = false; s.isH = false;
    if (rowRef.current) rowRef.current.style.transition = "none";
    if (actRef.current) actRef.current.style.transition = "none";
  };

  const onTouchMove = (e) => {
    const s = state.current;
    if (!s.dragging) return;
    const t = e.touches[0];
    const dx = t.clientX - s.startX;
    const dy = t.clientY - s.startY;
    if (!s.locked) {
      if (Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
      s.locked = true;
      s.isH = Math.abs(dx) > Math.abs(dy);
      if (s.isH && rowRef.current) rowRef.current.style.willChange = "transform";
    }
    if (!s.isH) { s.dragging = false; return; }
    e.preventDefault();
    const next = Math.max(-SWIPE_WIDTH, Math.min(0, s.base + dx));
    if (rowRef.current) rowRef.current.style.transform = `translateX(${next}px)`;
    if (actRef.current) actRef.current.style.opacity = `${Math.min(1, Math.abs(next) / SWIPE_WIDTH)}`;
    s.current = next;
  };

  const onTouchEnd = () => {
    state.current.dragging = false;
    if (state.current.current < -SNAP_THRESHOLD) translate(-SWIPE_WIDTH, true);
    else translate(0, true);
  };

  return (
    <div
      className="relative overflow-hidden rounded-[18px]"
      style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
    >
      <div
        ref={actRef}
        className="absolute right-0 top-0 bottom-0 flex items-center pr-2"
        style={{ opacity: 0 }}
      >
        <button
          className="flex h-[calc(100%-12px)] w-[68px] flex-col items-center justify-center gap-1 rounded-[14px]"
          style={{ background: "#ff4d6d" }}
          onClick={() => { close(); onDelete(); }}
        >
          <Trash2 size={16} color="white" />
          <span className="text-[11px] font-medium text-white">删除</span>
        </button>
      </div>
      <div
        ref={rowRef}
        className="relative z-10"
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        {children}
      </div>
    </div>
  );
}

// ── Sortable world book item ──

function SortableWorldBookItem({ book, onTap, onDelete }) {
  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: String(book.id) });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0 : 1,
  };

  return (
    <div ref={setNodeRef} style={style} className="mb-3">
      <SwipeRow onDelete={() => onDelete(book)}>
        <div
          className="flex items-center gap-3 rounded-[18px] p-4"
          style={{ background: S.bg, userSelect: "none" }}
        >
          {/* Drag handle */}
          <div
            {...attributes}
            {...listeners}
            className="drag-handle shrink-0 cursor-grab touch-none px-1"
            style={{ cursor: isDragging ? "grabbing" : "grab" }}
          >
            <GripVertical size={18} style={{ color: S.textMuted }} />
          </div>

          <div className="flex-1 min-w-0" onClick={() => onTap(book)}>
            <div className="flex items-center gap-2">
              <span className="truncate text-[14px] font-semibold" style={{ color: S.text }}>
                {book.name}
              </span>
              <ActivationBadge activation={book.activation} />
            </div>
            {book.folder && (
              <div className="mt-0.5 text-[11px]" style={{ color: S.textMuted }}>
                {book.folder}
              </div>
            )}
            {book.activation === "keyword" && book.keywords?.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {book.keywords.slice(0, 3).map((kw, i) => (
                  <span
                    key={i}
                    className="rounded-full px-1.5 py-0.5 text-[10px]"
                    style={{ background: "rgba(232,160,191,0.15)", color: S.accentDark }}
                  >
                    {kw}
                  </span>
                ))}
                {book.keywords.length > 3 && (
                  <span className="text-[10px]" style={{ color: S.textMuted }}>
                    +{book.keywords.length - 3}
                  </span>
                )}
              </div>
            )}
          </div>
          <ChevronRight
            size={16}
            style={{ color: S.textMuted, flexShrink: 0 }}
            onClick={() => onTap(book)}
          />
        </div>
      </SwipeRow>
    </div>
  );
}

// ── Folder group with its own DndContext ──

function FolderGroup({ folder, books, onDelete, onTap, onReorder }) {
  const [open, setOpen] = useState(true);
  const [activeId, setActiveId] = useState(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 5 } }),
  );

  const handleDragEnd = ({ active, over }) => {
    setActiveId(null);
    if (!over || active.id === over.id) return;
    const oldIdx = books.findIndex((b) => String(b.id) === String(active.id));
    const newIdx = books.findIndex((b) => String(b.id) === String(over.id));
    if (oldIdx < 0 || newIdx < 0) return;
    const reordered = arrayMove(books, oldIdx, newIdx);
    onReorder(folder, reordered);
  };

  const activeBook = activeId ? books.find((b) => String(b.id) === String(activeId)) : null;

  return (
    <div className="mb-4">
      <button
        className="mb-2 flex w-full items-center gap-2 px-1"
        onClick={() => setOpen(!open)}
      >
        {open ? (
          <ChevronDown size={14} style={{ color: S.textMuted }} />
        ) : (
          <ChevronRight size={14} style={{ color: S.textMuted }} />
        )}
        <span className="text-[12px] font-bold tracking-wide" style={{ color: S.textMuted }}>
          {folder || "未分类"}
        </span>
        <span
          className="ml-1 rounded-full px-1.5 py-0.5 text-[10px]"
          style={{ background: "rgba(136,136,160,0.12)", color: S.textMuted }}
        >
          {books.length}
        </span>
      </button>
      {open && (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragStart={({ active }) => setActiveId(active.id)}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={books.map((b) => String(b.id))}
            strategy={verticalListSortingStrategy}
          >
            {books.map((b) => (
              <SortableWorldBookItem
                key={b.id}
                book={b}
                onTap={onTap}
                onDelete={onDelete}
              />
            ))}
          </SortableContext>
          <DragOverlay>
            {activeBook && (
              <div
                className="flex items-center gap-3 rounded-[18px] p-4"
                style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
              >
                <div className="shrink-0 px-1">
                  <GripVertical size={18} style={{ color: S.textMuted }} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-[14px] font-semibold" style={{ color: S.text }}>
                      {activeBook.name}
                    </span>
                    <ActivationBadge activation={activeBook.activation} />
                  </div>
                  {activeBook.folder && (
                    <div className="mt-0.5 text-[11px]" style={{ color: S.textMuted }}>
                      {activeBook.folder}
                    </div>
                  )}
                </div>
                <ChevronRight size={16} style={{ color: S.textMuted, flexShrink: 0 }} />
              </div>
            )}
          </DragOverlay>
        </DndContext>
      )}
    </div>
  );
}

function MemeFilterDropdown({ value, onChange, options, width }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => { document.removeEventListener("mousedown", handler); document.removeEventListener("touchstart", handler); };
  }, [open]);

  const displayLabel = options.find((o) => o.value === value)?.label || options[0]?.label || "";

  return (
    <div className="relative" style={{ width }} ref={ref}>
      <button
        className="flex w-full items-center justify-between rounded-[12px] px-2.5 py-2 text-[11px] font-medium text-left"
        style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg, color: value ? S.accentDark : S.text }}
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
              {value === o.value && <Check size={10} style={{ color: S.accentDark, flexShrink: 0 }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function WorldBooks() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState(searchParams.get("tab") === "meme" ? "meme" : "worldbook");
  const [books, setBooks] = useState([]);
  const [memes, setMemes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteType, setDeleteType] = useState("worldbook");
  const [toast, setToast] = useState(null);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [confirm, setConfirm] = useState(null);
  const [memeFilterCat, setMemeFilterCat] = useState(searchParams.get("cat") || "");
  const [memeFilterType, setMemeFilterType] = useState(searchParams.get("type") || "");
  const [memeSearch, setMemeSearch] = useState(searchParams.get("q") || "");
  const lpRef = useRef(null);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  const enterSelectMode = (id) => { setSelectMode(true); setSelectedIds(new Set([id])); };
  const cancelSelect = () => { setSelectMode(false); setSelectedIds(new Set()); };
  const toggleSelect = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const batchDeleteMemes = () => {
    if (selectedIds.size === 0) return;
    setConfirm({
      message: `确定要删除选中的 ${selectedIds.size} 条热梗吗？`,
      action: async () => {
        for (const id of selectedIds) {
          await apiFetch(`/api/memes/${id}`, { method: "DELETE" });
        }
        setMemes((p) => p.filter((m) => !selectedIds.has(m.id)));
        cancelSelect();
        showToast("已删除");
      },
    });
  };

  useEffect(() => {
    setLoading(true);
    Promise.all([
      apiFetch("/api/world-books").then((d) => setBooks(d.world_books || [])),
      apiFetch("/api/memes").then((d) => setMemes(d.memes || [])),
    ]).catch(() => showToast("加载失败")).finally(() => setLoading(false));
  }, []);

  const handleDelete = async (item) => {
    setDeleteTarget(null);
    try {
      if (deleteType === "meme") {
        await apiFetch(`/api/memes/${item.id}`, { method: "DELETE" });
        setMemes((prev) => prev.filter((m) => m.id !== item.id));
      } else {
        await apiFetch(`/api/world-books/${item.id}`, { method: "DELETE" });
        setBooks((prev) => prev.filter((b) => b.id !== item.id));
      }
      showToast("已删除");
    } catch (_e) {
      showToast("删除失败");
    }
  };

  const handleReorder = (folder, reorderedBooks) => {
    const folderKey = folder || "";
    setBooks((prev) => {
      const others = prev.filter((b) => (b.folder || "") !== folderKey);
      return [...others, ...reorderedBooks];
    });
    apiFetch("/api/world-books/reorder", {
      method: "PUT",
      body: { ordered_ids: reorderedBooks.map((b) => b.id) },
    }).catch(() => showToast("排序保存失败"));
  };

  // Group by folder
  const grouped = {};
  for (const b of books) {
    const key = b.folder || "";
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(b);
  }
  const folderKeys = Object.keys(grouped).sort((a, b) =>
    a === "" ? 1 : b === "" ? -1 : a.localeCompare(b)
  );

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div
        className="flex shrink-0 items-center justify-between px-5 pb-2"
        style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}
      >
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full nm-active"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => { if (selectMode) { cancelSelect(); return; } navigate("/", { replace: true }); }}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>
          {selectMode ? `已选 ${selectedIds.size} 项` : tab === "worldbook" ? "世界书" : "热梗"}
        </h1>
        {selectMode ? (
          <button
            className="flex h-10 w-10 items-center justify-center rounded-full"
            style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
            onClick={batchDeleteMemes}
          >
            <Trash2 size={18} style={{ color: selectedIds.size > 0 ? "#ff4d6d" : S.textMuted }} />
          </button>
        ) : (
          <button
            className="flex h-10 w-10 items-center justify-center rounded-full"
            style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
            onClick={() => {
              if (tab === "worldbook") { navigate("/world-books/new"); return; }
              const p = new URLSearchParams();
              if (memeFilterCat) p.set("cat", memeFilterCat);
              if (memeFilterType) p.set("type", memeFilterType);
              if (memeSearch) p.set("q", memeSearch);
              navigate(`/memes/new?${p.toString()}`);
            }}
          >
            <Plus size={20} style={{ color: S.accentDark }} />
          </button>
        )}
      </div>

      {/* Tab bar */}
      <div className="shrink-0 px-5 pb-3">
        <div className="flex rounded-[14px] p-1" style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}>
          {[
            { key: "worldbook", label: "世界书", count: books.length },
            { key: "meme", label: "热梗", count: memes.length },
          ].map((t) => (
            <button
              key={t.key}
              className="flex-1 rounded-[12px] py-2 text-[12px] font-medium transition-all"
              style={tab === t.key
                ? { background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }
                : { color: S.textMuted }}
              onClick={() => { setTab(t.key); setSearchParams(t.key === "meme" ? { tab: "meme" } : {}); cancelSelect(); }}
            >
              {t.label}{t.count > 0 ? `(${t.count})` : ""}
            </button>
          ))}
        </div>
      </div>

      {/* Meme filters */}
      {tab === "meme" && !selectMode && (
        <div className="shrink-0 px-5 pb-3">
          <div className="flex items-center gap-1.5">
            <MemeFilterDropdown
              value={memeFilterCat}
              onChange={setMemeFilterCat}
              options={[
                { value: "", label: "全部分类" },
                ...[...new Set(memes.map((m) => m.category).filter(Boolean))].map((c) => ({ value: c, label: c })),
              ]}
              width="30%"
            />
            <MemeFilterDropdown
              value={memeFilterType}
              onChange={setMemeFilterType}
              options={[
                { value: "", label: "全部类型" },
                ...[...new Set(memes.map((m) => m.type).filter(Boolean))].map((t) => ({ value: t, label: t })),
              ]}
              width="26%"
            />
            <div className="relative flex-1">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 z-10" style={{ color: S.textMuted }} />
              <input
                className="w-full rounded-[12px] py-2 pl-8 pr-7 text-[11px] outline-none"
                style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text }}
                placeholder="搜索..."
                value={memeSearch}
                onChange={(e) => setMemeSearch(e.target.value)}
              />
              {memeSearch && (
                <button className="absolute right-2 top-1/2 -translate-y-1/2" onClick={() => setMemeSearch("")}>
                  <X size={12} style={{ color: S.textMuted }} />
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto thin-scrollbar px-5 pt-3 pb-8">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-t-transparent" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : tab === "worldbook" ? (
          books.length === 0 ? (
            <div className="flex flex-col items-center gap-3 py-20">
              <GripVertical size={36} style={{ color: S.textMuted, opacity: 0.5 }} />
              <p className="text-[14px]" style={{ color: S.textMuted }}>
                还没有世界书，点击 + 创建
              </p>
            </div>
          ) : (
            folderKeys.map((folder) => (
              <FolderGroup
                key={folder}
                folder={folder}
                books={grouped[folder]}
                onDelete={(b) => { setDeleteType("worldbook"); setDeleteTarget(b); }}
                onTap={(b) => navigate(`/world-books/${b.id}`)}
                onReorder={handleReorder}
              />
            ))
          )
        ) : (
          memes.length === 0 ? (
            <div className="flex flex-col items-center gap-3 py-20">
              <GripVertical size={36} style={{ color: S.textMuted, opacity: 0.5 }} />
              <p className="text-[14px]" style={{ color: S.textMuted }}>
                还没有热梗，点击 + 添加
              </p>
            </div>
          ) : (
            memes.filter((m) => {
              if (memeFilterCat && m.category !== memeFilterCat) return false;
              if (memeFilterType && m.type !== memeFilterType) return false;
              if (memeSearch) {
                const q = memeSearch.toLowerCase();
                const inTerm = m.term?.toLowerCase().includes(q);
                const inKw = m.keywords?.some((k) => k.toLowerCase().includes(q));
                const inContent = m.content && typeof m.content === "object" &&
                  Object.values(m.content).some((v) =>
                    Array.isArray(v) ? v.some((x) => String(x).toLowerCase().includes(q)) : String(v).toLowerCase().includes(q)
                  );
                if (!inTerm && !inKw && !inContent) return false;
              }
              return true;
            }).map((m) => (
              <div
                key={m.id}
                className="mb-3 rounded-[18px] p-3 flex items-start gap-2.5"
                style={{ background: S.bg, boxShadow: selectMode && selectedIds.has(m.id) ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }}
                onClick={() => {
                  if (selectMode) { toggleSelect(m.id); return; }
                  const p = new URLSearchParams();
                  if (memeFilterCat) p.set("cat", memeFilterCat);
                  if (memeFilterType) p.set("type", memeFilterType);
                  if (memeSearch) p.set("q", memeSearch);
                  navigate(`/memes/${m.id}?${p.toString()}`);
                }}
                onTouchStart={() => {
                  if (selectMode) return;
                  lpRef.current = setTimeout(() => enterSelectMode(m.id), 600);
                }}
                onTouchMove={() => { clearTimeout(lpRef.current); }}
                onTouchEnd={() => { clearTimeout(lpRef.current); }}
              >
                {selectMode && (
                  <div className="mt-1 flex h-5 w-5 shrink-0 items-center justify-center rounded-full"
                    style={selectedIds.has(m.id)
                      ? { background: S.accentDark, boxShadow: "0 2px 6px rgba(201,98,138,0.3)" }
                      : { background: S.bg, boxShadow: "var(--inset-shadow)" }}
                  >
                    {selectedIds.has(m.id) && <Check size={12} color="white" strokeWidth={3} />}
                  </div>
                )}
                <div className="flex-1 min-w-0">
                <div className="flex flex-wrap items-center gap-1 mb-1.5">
                  {m.category && (
                    <span className="inline-block rounded-full px-2 py-0.5 text-[10px] font-medium"
                      style={{ background: "#e0e6f0", color: "#6b7b9b" }}>{m.category}</span>
                  )}
                  {m.type && (
                    <span className="inline-block rounded-full px-2 py-0.5 text-[10px] font-medium"
                      style={{ background: "#f0ebd8", color: "#9b7a3b" }}>{m.type}</span>
                  )}
                  {m.keywords && m.keywords.map((kw, i) => (
                    <span key={i} className="inline-block rounded-full px-1.5 py-0.5 text-[9px]"
                      style={{ background: "rgba(232,160,191,0.15)", color: S.accentDark }}>{kw}</span>
                  ))}
                </div>
                <p className="text-[13px] font-semibold mb-1" style={{ color: S.text }}>{m.term}</p>
                {m.content && typeof m.content === "object" && Object.entries(m.content).map(([k, v]) => (
                  <div key={k} className="text-[12px] leading-relaxed mb-0.5" style={{ color: S.textMuted }}>
                    <span style={{ color: S.accentDark }}>{k}: </span>
                    {Array.isArray(v) ? v.join("；") : v}
                  </div>
                ))}
                </div>
              </div>
            ))
          )
        )}
      </div>

      {/* Delete confirm overlay */}
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
            <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>
              {deleteType === "meme" ? "删除热梗" : "删除世界书"}
            </p>
            <p className="mb-5 text-center text-[13px]" style={{ color: S.textMuted }}>
              确定要删除「{deleteTarget.name || deleteTarget.term}」吗？
            </p>
            <div className="flex gap-3">
              <button
                className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold"
                style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }}
                onClick={() => setDeleteTarget(null)}
              >
                取消
              </button>
              <button
                className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold text-white"
                style={{ background: "#ff4d6d", boxShadow: "4px 4px 10px rgba(255,77,109,0.4)" }}
                onClick={() => handleDelete(deleteTarget)}
              >
                删除
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Batch delete confirm */}
      {confirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={() => setConfirm(null)}>
          <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-6" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
            <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>确认删除</p>
            <p className="mb-5 text-center text-[13px]" style={{ color: S.textMuted }}>{confirm.message}</p>
            <div className="flex gap-3">
              <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={() => setConfirm(null)}>取消</button>
              <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold text-white" style={{ background: "#ff4d6d", boxShadow: "4px 4px 10px rgba(255,77,109,0.4)" }} onClick={() => { confirm.action(); setConfirm(null); }}>删除</button>
            </div>
          </div>
        </div>
      )}

      {/* Toast */}
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
