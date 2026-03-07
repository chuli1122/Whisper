import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Trash2, RefreshCw, ChevronDown } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

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

function MsgItem({ msg, roleLabel, roleColor, fmtTime, onDelete }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const textRef = useRef(null);

  useLayoutEffect(() => {
    const el = textRef.current;
    if (el && !expanded) setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [msg.content, expanded]);

  return (
    <SwipeRow onDelete={onDelete}>
      <div className="rounded-[18px] p-3" style={{ background: S.bg }}>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold" style={{ color: roleColor(msg.role) }}>{roleLabel(msg.role)}</span>
          <div className="flex items-center gap-1.5">
            {msg.meta_info?.source && (
              <span
                className="rounded-full px-1.5 py-0.5 text-[9px]"
                style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}
              >{msg.meta_info.source}</span>
            )}
            {msg.meta_info?.no_message && (
              <span
                className="rounded-full px-1.5 py-0.5 text-[10px] font-semibold"
                style={{ background: "rgba(180,130,50,0.15)", color: "#a07828" }}
              >消息判定</span>
            )}
            {msg.summarized && (
              <span
                className="rounded-full px-1.5 py-0.5 text-[10px] font-semibold"
                style={{ background: "rgba(58,138,95,0.15)", color: "#2d7a4a" }}
              >已摘要</span>
            )}
            <span className="text-[10px]" style={{ color: S.textMuted }}>{fmtTime(msg.created_at)}</span>
          </div>
        </div>
        <div
          ref={textRef}
          className="text-[12px] leading-relaxed break-words cursor-pointer"
          style={expanded ? { color: S.text } : { color: S.text, display: "-webkit-box", WebkitLineClamp: 4, WebkitBoxOrient: "vertical", overflow: "hidden" }}
          onClick={() => setExpanded(!expanded)}
        >
          {msg.content}
        </div>
        {(overflows || expanded) && (
          <div className="mt-1 flex justify-center">
            <button
              className="rounded-full px-2 py-0.5 text-[10px]"
              style={{ color: "#d48aab", background: "rgba(232,160,191,0.1)" }}
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            >
              {expanded ? "收起" : "查看更多"}
            </button>
          </div>
        )}
      </div>
    </SwipeRow>
  );
}

const FILTER_OPTIONS = [
  { value: "all", label: "全部" },
  { value: "user", label: "我" },
  { value: "assistant", label: "AI" },
  { value: "no_message", label: "消息判定" },
];

export default function Messages() {
  const navigate = useNavigate();
  const [messages, setMessages] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [filter, setFilter] = useState("all");

  const _buildUrl = (sessId, extra = "") => {
    let url = `/api/sessions/${sessId}/messages?limit=50`;
    if (filter === "user" || filter === "assistant") url += `&role=${filter}`;
    if (filter === "no_message") url += "&include_no_message=true";
    return url + extra;
  };

  const load = async () => {
    setLoading(true);
    try {
      // Find the latest session
      const sessData = await apiFetch("/api/sessions?limit=1");
      const sess = sessData.sessions?.[0];
      if (!sess) { setLoading(false); return; }
      setSessionId(sess.id);

      const msgData = await apiFetch(_buildUrl(sess.id));
      setMessages((msgData.messages || []).reverse());
      setHasMore(msgData.has_more || false);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, [filter]);

  const deleteMsg = async (msgId) => {
    if (!sessionId) return;
    try {
      await apiFetch(`/api/sessions/${sessionId}/messages/${msgId}`, { method: "DELETE" });
      setMessages((prev) => prev.filter((m) => m.id !== msgId));
    } catch (e) {
      console.error("Delete failed", e);
    }
  };

  const loadMore = async () => {
    if (!sessionId || !hasMore || loading) return;
    const oldest = messages[messages.length - 1];
    if (!oldest) return;
    setLoading(true);
    try {
      const msgData = await apiFetch(_buildUrl(sessionId, `&before_id=${oldest.id}`));
      const older = (msgData.messages || []).reverse();
      setMessages((prev) => [...prev, ...older]);
      setHasMore(msgData.has_more || false);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  const fmtTime = (ts) => {
    if (!ts) return "";
    const d = new Date(ts);
    const now = new Date();
    if (d.toDateString() === now.toDateString())
      return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  const roleLabel = (role) => {
    if (role === "user") return "我";
    if (role === "assistant") return "AI";
    return "系统";
  };

  const roleColor = (role) => {
    if (role === "user") return S.accentDark;
    if (role === "assistant") return "#3a9b5c";
    return S.textMuted;
  };

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
          onClick={() => navigate("/", { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>消息记录</h1>
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: loading ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }}
          onClick={load}
          disabled={loading}
        >
          <RefreshCw size={16} style={{ color: S.accentDark }} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Filter + Hint */}
      <div className="shrink-0 px-5 pb-2 flex items-center justify-between">
        <div className="flex gap-1.5">
          {FILTER_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              className="rounded-full px-2.5 py-1 text-[11px] font-medium transition-all"
              style={filter === opt.value
                ? { background: S.accentDark, color: "#fff" }
                : { background: "rgba(0,0,0,0.04)", color: S.textMuted }
              }
              onClick={() => setFilter(opt.value)}
            >{opt.label}</button>
          ))}
        </div>
        <p className="text-[10px]" style={{ color: S.textMuted }}>左滑删除</p>
      </div>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto px-5 pb-8">
        {loading && messages.length === 0 ? (
          <div className="flex justify-center py-16">
            <div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : messages.length === 0 ? (
          <p className="py-16 text-center text-[14px]" style={{ color: S.textMuted }}>暂无消息</p>
        ) : (
          <>
            {(filter === "no_message" ? messages.filter((m) => m.meta_info?.no_message) : messages).map((msg) => (
              <MsgItem key={msg.id} msg={msg} roleLabel={roleLabel} roleColor={roleColor} fmtTime={fmtTime} onDelete={() => deleteMsg(msg.id)} />
            ))}
            {hasMore && (
              <button
                className="mx-auto mt-2 block rounded-[10px] px-4 py-2 text-[12px]"
                style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }}
                onClick={loadMore}
                disabled={loading}
              >
                {loading ? "加载中..." : "加载更多"}
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
