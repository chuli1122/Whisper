import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, Trash2, Plus, X, Lock, Maximize2, Minimize2 } from "lucide-react";
import { apiFetch } from "../utils/api";
import { getAvatar } from "../utils/db";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const TABS = [
  { key: "theirs", label: "他的日记" },
  { key: "mine", label: "我的日记" },
];

const SWIPE_WIDTH = 80;
const SNAP_THRESHOLD = 40;

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

/* ── Confirm dialog (centered, assistant-page style) ── */
function ConfirmDialog({ message, onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={onCancel}>
      <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-6" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <p className="mb-1 text-center text-[16px] font-bold" style={{ color: S.text }}>确认删除</p>
        <p className="mb-5 text-center text-[13px]" style={{ color: S.textMuted }}>{message}</p>
        <div className="flex gap-3">
          <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.text }} onClick={onCancel}>取消</button>
          <button className="flex-1 rounded-[16px] py-3 text-[15px] font-semibold text-white" style={{ background: "#ff4d6d", boxShadow: "4px 4px 10px rgba(255,77,109,0.4)" }} onClick={onConfirm}>删除</button>
        </div>
      </div>
    </div>
  );
}

/* ── SwipeRow (assistant-page style) ── */
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

/* ── Countdown ── */
function Countdown({ unlockAt }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  const diff = new Date(unlockAt).getTime() - now;
  if (diff <= 0) return null;
  const d = Math.floor(diff / 86400000);
  const h = Math.floor((diff % 86400000) / 3600000);
  const m = Math.floor((diff % 3600000) / 60000);
  const sec = Math.floor((diff % 60000) / 1000);
  let text = "";
  if (d > 0) text += `${d}天`;
  if (h > 0 || d > 0) text += `${h}小时`;
  text += `${m}分${sec}秒`;
  return (
    <div className="flex items-center gap-1.5 text-[12px]" style={{ color: S.accentDark }}>
      <Lock size={12} />
      <span>{text}</span>
    </div>
  );
}

/* ── Assistant picker (centered) ── */
function AssistantPicker({ assistants, avatarMap, currentId, onSelect, onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.25)" }} onClick={onClose}>
      <div className="mx-6 w-full max-w-[300px] rounded-[22px] p-5" style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <span className="text-[15px] font-bold" style={{ color: S.text }}>选择助手</span>
          <button onClick={onClose}><X size={18} style={{ color: S.textMuted }} /></button>
        </div>
        <div className="max-h-[50vh] overflow-y-auto">
          {assistants.map((a) => {
            const src = avatarMap[a.id] || a.avatar_url;
            return (
              <button key={a.id} className="mb-2 flex w-full items-center gap-3 rounded-[14px] p-3 text-left"
                style={{ background: S.bg, boxShadow: a.id === currentId ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }}
                onClick={() => { onSelect(a.id); onClose(); }}>
                <div className="shrink-0 rounded-full" style={{ width: 36, height: 36, background: "linear-gradient(135deg, #f0c4d8, var(--accent))", padding: 2 }}>
                  <div className="flex h-full w-full items-center justify-center overflow-hidden rounded-full" style={{ background: S.bg }}>
                    {src ? <img src={src} alt="" className="h-full w-full object-cover rounded-full" /> : <span className="text-[14px]" style={{ color: S.accentDark }}>{a.name?.[0] || "?"}</span>}
                  </div>
                </div>
                <span className="text-[13px] font-medium truncate" style={{ color: S.text }}>{a.name}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ── Diary detail page (notebook style) ── */
function DiaryDetail({ diary, onBack, onMarkRead }) {
  useEffect(() => {
    if (diary && !diary.is_read && diary.author === "assistant") {
      const unlocked = !diary.unlock_at || new Date(diary.unlock_at) <= new Date();
      if (unlocked) onMarkRead(diary.id);
    }
  }, [diary]);

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      <div className="flex shrink-0 items-center justify-between px-5 pb-3" style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}>
        <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }} onClick={onBack}>
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[15px] font-bold truncate max-w-[60%]" style={{ color: S.text }}>{diary.title || "日记"}</h1>
        <div className="w-10" />
      </div>
      <div className="flex-1 overflow-y-auto px-5 pb-8">
        <div className="rounded-[18px] p-5" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}>
          <div className="mb-3 text-[10px]" style={{ color: S.textMuted }}>{fmtTime(diary.created_at)}</div>
          <div className="text-[13px] leading-[28px]" style={{ color: S.text }}>
            {diary.content.split("\n").map((line, i) => (
              <div key={i} style={{ borderBottom: "1px solid rgba(0,0,0,0.06)", minHeight: 28 }}>{line || "\u00A0"}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── New diary form ── */
function NewDiaryForm({ assistantId, onSave, onCancel }) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [timed, setTimed] = useState(false);
  const [unlockDate, setUnlockDate] = useState("");
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const handleSave = async () => {
    if (!content.trim() || saving) return;
    setSaving(true);
    try {
      const body = { assistant_id: assistantId, author: "user", title: title.trim(), content: content.trim() };
      if (timed && unlockDate) body.unlock_at = new Date(unlockDate).toISOString();
      await onSave(body);
    } finally { setSaving(false); }
  };

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      <div className="flex shrink-0 items-center justify-between px-5 pb-3" style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}>
        <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }} onClick={onCancel}>
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[15px] font-bold" style={{ color: S.text }}>写日记</h1>
        <button className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: content.trim() ? "var(--card-shadow-sm)" : "var(--inset-shadow)" }}
          onClick={handleSave} disabled={!content.trim() || saving}>
          <span className="text-[12px] font-bold" style={{ color: content.trim() ? S.accentDark : S.textMuted }}>存</span>
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-5 pb-8">
        {!expanded && (
          <input className="mb-3 w-full rounded-[12px] px-3 py-2.5 text-[13px] font-medium outline-none"
            style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text }}
            placeholder="标题（可选）" value={title} onChange={(e) => setTitle(e.target.value)} />
        )}
        <div className="relative mb-3">
          <textarea
            className="w-full rounded-[14px] p-3 text-[13px] leading-[28px] resize-none outline-none"
            style={{
              background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text,
              minHeight: expanded ? "calc(100vh - 120px)" : 200,
              backgroundImage: "repeating-linear-gradient(transparent, transparent 27px, rgba(0,0,0,0.05) 27px, rgba(0,0,0,0.05) 28px)",
              backgroundPositionY: 9,
            }}
            placeholder="写点什么..." value={content} onChange={(e) => setContent(e.target.value)} autoFocus
          />
          <button
            className="absolute top-2 right-2 flex h-7 w-7 items-center justify-center rounded-full"
            style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? <Minimize2 size={13} style={{ color: S.accentDark }} /> : <Maximize2 size={13} style={{ color: S.accentDark }} />}
          </button>
        </div>
        {!expanded && (
          <>
            {/* Timed unlock toggle */}
            <div className="mb-2 flex items-center justify-between rounded-[14px] px-4 py-3" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}>
              <div>
                <span className="text-[13px] font-medium" style={{ color: S.text }}>定时解锁</span>
                <p className="text-[10px]" style={{ color: S.textMuted }}>对方需要等到指定时间才能查看</p>
              </div>
              <button className="relative h-7 w-12 rounded-full transition-colors" style={{ background: timed ? S.accent : "rgba(0,0,0,0.1)" }} onClick={() => {
                if (!timed && !unlockDate) {
                  const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0);
                  const pad = (n) => String(n).padStart(2, "0");
                  setUnlockDate(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`);
                }
                setTimed(!timed);
              }}>
                <div className="absolute top-0.5 h-6 w-6 rounded-full transition-transform" style={{ background: "white", boxShadow: "0 1px 3px rgba(0,0,0,0.2)", transform: timed ? "translateX(22px)" : "translateX(2px)" }} />
              </button>
            </div>
            {timed && (
              <div className="rounded-[14px] px-4 py-3" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}>
                <p className="mb-2 text-[12px] font-medium" style={{ color: S.text }}>选择解锁时间</p>
                <input type="datetime-local" className="w-full rounded-[10px] px-3 py-2 text-[13px] outline-none"
                  style={{ background: S.bg, boxShadow: "var(--inset-shadow)", color: S.text }}
                  value={unlockDate} onChange={(e) => setUnlockDate(e.target.value)} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ── Main page ── */
export default function DiaryPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState("theirs");
  const [assistants, setAssistants] = useState([]);
  const [avatarMap, setAvatarMap] = useState({});
  const [assistantId, _setAssistantId] = useState(() => {
    const saved = localStorage.getItem("diary_assistant_id");
    return saved ? Number(saved) : null;
  });
  const setAssistantId = (id) => {
    _setAssistantId(id);
    if (id != null) localStorage.setItem("diary_assistant_id", String(id));
  };
  const [pickerOpen, setPickerOpen] = useState(false);
  const [diaries, setDiaries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [confirm, setConfirm] = useState(null);
  const [detail, setDetail] = useState(null);
  const [newForm, setNewForm] = useState(false);

  // Load assistants + avatars
  useEffect(() => {
    apiFetch("/api/assistants").then(async (d) => {
      const list = d.assistants || [];
      setAssistants(list);
      if (list.length > 0) {
        const valid = assistantId && list.some((a) => a.id === assistantId);
        if (!valid) setAssistantId(list[0].id);
      }
      const map = {};
      await Promise.all(list.map(async (a) => {
        const b64 = await getAvatar(`assistant-avatar-${a.id}`).catch(() => null);
        if (b64) map[a.id] = b64;
      }));
      setAvatarMap(map);
    }).catch(() => {});
  }, []);

  const currentAssistant = assistants.find((a) => a.id === assistantId);
  const currentAvatar = (assistantId && avatarMap[assistantId]) || currentAssistant?.avatar_url;

  // Load diaries
  useEffect(() => { if (assistantId != null) loadDiaries(); }, [assistantId, tab]);

  const loadDiaries = async () => {
    setLoading(true);
    try {
      const author = tab === "theirs" ? "assistant" : "user";
      const d = await apiFetch(`/api/diary?assistant_id=${assistantId}&author=${author}&limit=100`);
      setDiaries(d.diary || []);
    } catch (e) { console.error(e); }
    setLoading(false);
  };

  const deleteDiary = (id) => {
    setConfirm({ message: "确定要删除这篇日记吗？", action: async () => { await apiFetch(`/api/diary/${id}`, { method: "DELETE" }); setDiaries((p) => p.filter((d) => d.id !== id)); } });
  };

  const markRead = async (id) => {
    try { await apiFetch(`/api/diary/${id}/read`, { method: "POST" }); setDiaries((p) => p.map((d) => d.id === id ? { ...d, is_read: true } : d)); } catch (e) { console.error(e); }
  };

  const handleCreate = async (body) => { await apiFetch("/api/diary", { method: "POST", body }); setNewForm(false); loadDiaries(); };

  const openDiary = (diary) => {
    const locked = diary.unlock_at && new Date(diary.unlock_at) > new Date();
    if (locked) return;
    setDetail(diary);
  };

  if (detail) return <DiaryDetail diary={detail} onBack={() => { setDetail(null); loadDiaries(); }} onMarkRead={markRead} />;
  if (newForm) return <NewDiaryForm assistantId={assistantId} onSave={handleCreate} onCancel={() => setNewForm(false)} />;

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between px-5 pb-3" style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}>
        <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }} onClick={() => navigate("/", { replace: true })}>
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>日记</h1>
        {/* Assistant avatar */}
        <button className="flex h-10 w-10 items-center justify-center rounded-full" style={{ border: "2px solid #e8a0bf", padding: 2 }} onClick={() => setPickerOpen(true)}>
          <div className="flex h-full w-full items-center justify-center overflow-hidden rounded-full" style={{ background: S.bg }}>
            {currentAvatar
              ? <img src={currentAvatar} alt="" className="h-full w-full object-cover rounded-full" />
              : <span className="text-[14px]" style={{ color: S.accentDark }}>{currentAssistant?.name?.[0] || "?"}</span>
            }
          </div>
        </button>
      </div>

      {/* Tabs */}
      <div className="shrink-0 px-5 pb-3">
        <div className="flex rounded-[14px] p-1" style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}>
          {TABS.map((t) => (
            <button key={t.key} className="flex-1 rounded-[12px] py-2 text-[12px] font-medium transition-all"
              style={tab === t.key ? { background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark } : { color: S.textMuted }}
              onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-5 pb-24 pt-5">
        {loading ? (
          <div className="flex justify-center py-16">
            <div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : diaries.length === 0 ? (
          <p className="py-16 text-center text-[14px]" style={{ color: S.textMuted }}>
            {tab === "theirs" ? "还没有收到日记" : "还没有写过日记"}
          </p>
        ) : (
          diaries.map((diary) => {
            const locked = diary.unlock_at && new Date(diary.unlock_at) > new Date();
            return (
              <SwipeRow key={diary.id} onDelete={() => deleteDiary(diary.id)}>
                <div className="p-4 rounded-[18px]" style={{ background: S.bg, userSelect: "none" }} onClick={() => openDiary(diary)}>
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-2 min-w-0">
                      {!diary.is_read && diary.author === "assistant" && !locked && (
                        <div className="h-2 w-2 shrink-0 rounded-full" style={{ background: "#ef4444" }} />
                      )}
                      <span className="text-[13px] font-medium truncate" style={{ color: S.text }}>{diary.title || "无题"}</span>
                    </div>
                    <span className="text-[10px] shrink-0 ml-2" style={{ color: S.textMuted }}>{fmtTime(diary.created_at)}</span>
                  </div>
                  {locked ? (
                    <Countdown unlockAt={diary.unlock_at} />
                  ) : (
                    <p className="text-[12px] leading-relaxed break-words" style={{ color: S.textMuted, maxHeight: 40, overflow: "hidden" }}>
                      {diary.content.length > 60 ? diary.content.slice(0, 60) + "..." : diary.content}
                    </p>
                  )}
                </div>
              </SwipeRow>
            );
          })
        )}
      </div>

      {/* New diary FAB */}
      {tab === "mine" && (
        <div className="fixed bottom-6 left-0 right-0 flex justify-center" style={{ zIndex: 30 }}>
          <button className="flex h-12 w-12 items-center justify-center rounded-full"
            style={{ background: "linear-gradient(135deg, #f0c4d8, var(--accent))", boxShadow: "0 4px 14px rgba(232,160,191,0.4)" }}
            onClick={() => setNewForm(true)}>
            <Plus size={22} color="white" />
          </button>
        </div>
      )}

      {/* Modals */}
      {pickerOpen && <AssistantPicker assistants={assistants} avatarMap={avatarMap} currentId={assistantId} onSelect={setAssistantId} onClose={() => setPickerOpen(false)} />}
      {confirm && (
        <ConfirmDialog message={confirm.message}
          onConfirm={async () => { try { await confirm.action(); } catch (e) { console.error(e); } setConfirm(null); }}
          onCancel={() => setConfirm(null)} />
      )}
    </div>
  );
}
