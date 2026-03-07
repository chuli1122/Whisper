import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Plus, ChevronRight, Bot, Trash2 } from "lucide-react";
import { apiFetch } from "../utils/api";
import { getAvatar } from "../utils/db";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

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
      className="relative mb-3 overflow-hidden rounded-[18px]"
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

function AssistantCard({ assistant, localAvatar, onTap }) {
  const src = localAvatar || null;
  return (
    <div
      className="flex items-center gap-3 rounded-[18px] p-4"
      style={{ background: S.bg, userSelect: "none" }}
      onClick={() => onTap(assistant)}
    >
      <div
        className="shrink-0 rounded-full"
        style={{
          width: 48, height: 48,
          background: "linear-gradient(135deg, #f0c4d8, var(--accent))",
          padding: 3,
        }}
      >
        <div
          className="flex h-full w-full items-center justify-center overflow-hidden rounded-full"
          style={{ background: S.bg }}
        >
          {src ? (
            <img src={src} alt="" className="h-full w-full object-cover rounded-full" />
          ) : (
            <span className="text-[20px]" style={{ color: S.accentDark }}>
              {assistant.name?.[0] || "?"}
            </span>
          )}
        </div>
      </div>
      <div className="flex-1 min-w-0">
        <div className="truncate text-[15px] font-bold" style={{ color: S.text }}>
          {assistant.name}
        </div>
        {assistant.created_at && (
          <div className="mt-0.5 truncate text-[11px]" style={{ color: S.textMuted }}>
            {assistant.created_at.slice(0, 10)}
          </div>
        )}
      </div>
      <ChevronRight size={16} style={{ color: S.textMuted, flexShrink: 0 }} />
    </div>
  );
}

export default function Assistants() {
  const navigate = useNavigate();
  const [assistants, setAssistants] = useState([]);
  const [avatarMap, setAvatarMap] = useState({});
  const [loading, setLoading] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [toast, setToast] = useState(null);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  const load = () => {
    setLoading(true);
    apiFetch("/api/assistants")
      .then(async (d) => {
        const list = d.assistants || [];
        setAssistants(list);
        const map = {};
        await Promise.all(
          list.map(async (a) => {
            const b64 = await getAvatar(`assistant-avatar-${a.id}`).catch(() => null);
            if (b64) map[a.id] = b64;
          })
        );
        setAvatarMap(map);
      })
      .catch(() => showToast("加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const handleDelete = async (a) => {
    setDeleteTarget(null);
    try {
      await apiFetch(`/api/assistants/${a.id}`, { method: "DELETE" });
      setAssistants((prev) => prev.filter((x) => x.id !== a.id));
      showToast("已删除");
    } catch (_e) {
      showToast("删除失败");
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
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>助手配置</h1>
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => navigate("/assistants/new")}
        >
          <Plus size={20} style={{ color: S.accentDark }} />
        </button>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-5 pb-8 pt-5">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div
              className="h-8 w-8 animate-spin rounded-full border-2"
              style={{ borderColor: S.accent, borderTopColor: "transparent" }}
            />
          </div>
        ) : assistants.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-20">
            <Bot size={36} style={{ color: S.textMuted, opacity: 0.5 }} />
            <p className="text-[14px]" style={{ color: S.textMuted }}>
              还没有助手，点击 + 创建
            </p>
          </div>
        ) : (
          assistants.map((a) => (
            <SwipeRow key={a.id} onDelete={() => setDeleteTarget(a)}>
              <AssistantCard
                assistant={a}
                localAvatar={avatarMap[a.id]}
                onTap={(x) => navigate(`/assistants/${x.id}`)}
              />
            </SwipeRow>
          ))
        )}
      </div>

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
            <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>
              删除助手
            </p>
            <p className="mb-5 text-center text-[13px]" style={{ color: S.textMuted }}>
              确定要删除「{deleteTarget.name}」吗？
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
