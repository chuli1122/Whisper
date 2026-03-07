import { useState, useEffect, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, Save, X, Plus, Maximize2, Minimize2, FileText } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

function NmInput({ label, value, onChange, placeholder }) {
  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        {label}
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-[14px] px-4 py-3 text-[14px] outline-none"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
      />
    </div>
  );
}

function FolderInput({ value, onChange, folders }) {
  const [show, setShow] = useState(false);
  const filtered = folders.filter(
    (f) => f !== value && f.toLowerCase().includes(value.toLowerCase())
  );

  return (
    <div className="relative mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        文件夹
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => { onChange(e.target.value); setShow(true); }}
        onFocus={() => setShow(true)}
        onBlur={() => setTimeout(() => setShow(false), 150)}
        placeholder="可选，如：人物设定"
        className="w-full rounded-[14px] px-4 py-3 text-[14px] outline-none"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
      />
      {show && filtered.length > 0 && (
        <div
          className="absolute left-0 right-0 top-full z-30 mt-1 max-h-[160px] overflow-y-auto rounded-[14px]"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {filtered.map((f) => (
            <button
              key={f}
              className="flex w-full items-center px-4 py-3 text-[14px]"
              style={{ color: S.text }}
              onMouseDown={() => { onChange(f); setShow(false); }}
            >
              {f}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ActivationSelector({ value, onChange }) {
  const options = [
    { value: "always", label: "常驻" },
    { value: "keyword", label: "关键词" },
    { value: "mood", label: "情绪" },
    { value: "message_mode", label: "消息模式" },
  ];

  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        触发方式
      </label>
      <div
        className="flex rounded-[14px] p-1"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg }}
      >
        {options.map((opt) => (
          <button
            key={opt.value}
            className="flex-1 rounded-[10px] py-2 text-[13px] font-semibold transition-all"
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
    </div>
  );
}

function MessageModeChips({ selected, onChange }) {
  const modes = [
    { value: "long", label: "长消息" },
    { value: "short", label: "短消息" },
  ];

  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        消息模式
      </label>
      <div
        className="flex flex-wrap gap-2 rounded-[14px] p-3"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, minHeight: 48 }}
      >
        {modes.map((m) => {
          const isSel = selected === m.value;
          return (
            <button
              key={m.value}
              className="rounded-full px-3 py-1.5 text-[13px] font-medium transition-all"
              style={{
                background: isSel ? S.accentDark : S.bg,
                color: isSel ? "white" : S.textMuted,
                boxShadow: isSel ? "none" : "var(--card-shadow-sm)",
              }}
              onClick={() => onChange(m.value)}
            >
              {m.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

const MOOD_PRESETS = [
  { value: "happy", label: "开心" },
  { value: "sad", label: "难过" },
  { value: "angry", label: "生气" },
  { value: "anxious", label: "焦虑" },
  { value: "tired", label: "疲意" },
  { value: "emo", label: "低落" },
  { value: "flirty", label: "心动" },
  { value: "proud", label: "得意" },
  { value: "calm", label: "平静" },
];

function MoodChips({ selected, onChange }) {
  const toggle = (value) => {
    if (selected.includes(value)) {
      onChange(selected.filter((m) => m !== value));
    } else {
      onChange([...selected, value]);
    }
  };

  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        情绪
      </label>
      <div
        className="flex flex-wrap gap-2 rounded-[14px] p-3"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, minHeight: 48 }}
      >
        {MOOD_PRESETS.map((mood) => {
          const isSel = selected.includes(mood.value);
          return (
            <button
              key={mood.value}
              className="rounded-full px-3 py-1.5 text-[13px] font-medium transition-all"
              style={{
                background: isSel ? S.accentDark : S.bg,
                color: isSel ? "white" : S.textMuted,
                boxShadow: isSel ? "none" : "var(--card-shadow-sm)",
              }}
              onClick={() => toggle(mood.value)}
            >
              {mood.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function KeywordTags({ keywords, onChange }) {
  const [input, setInput] = useState("");

  const addKeyword = () => {
    const kw = input.trim();
    if (!kw || keywords.includes(kw)) { setInput(""); return; }
    onChange([...keywords, kw]);
    setInput("");
  };

  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        关键词
      </label>
      <div
        className="flex flex-wrap gap-2 rounded-[14px] p-3"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, minHeight: 48 }}
      >
        {keywords.map((kw) => (
          <span
            key={kw}
            className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-medium"
            style={{ background: "rgba(232,160,191,0.2)", color: S.accentDark }}
          >
            {kw}
            <button onClick={() => onChange(keywords.filter((k) => k !== kw))}>
              <X size={10} />
            </button>
          </span>
        ))}
        <div className="flex items-center gap-1">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addKeyword(); }
            }}
            placeholder="添加关键词..."
            className="w-24 bg-transparent text-[12px] outline-none"
            style={{ color: S.text }}
          />
          {input.trim() && (
            <button
              className="flex h-5 w-5 items-center justify-center rounded-full"
              style={{ background: S.accentDark }}
              onClick={addKeyword}
            >
              <Plus size={10} color="white" />
            </button>
          )}
        </div>
      </div>
      <p className="mt-1 text-[10px]" style={{ color: S.textMuted }}>
        按 Enter 或逗号确认
      </p>
    </div>
  );
}

function FullscreenEditor({ value, onChange, onClose }) {
  return (
    <div className="fixed inset-0 z-[200] flex flex-col" style={{ background: S.bg }}>
      <div
        className="flex shrink-0 items-center justify-between px-5 py-4"
        style={{ paddingTop: "max(1rem, env(safe-area-inset-top))" }}
      >
        <span className="text-[15px] font-bold" style={{ color: S.text }}>编辑内容</span>
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={onClose}
        >
          <Minimize2 size={18} style={{ color: S.accentDark }} />
        </button>
      </div>
      <textarea
        autoFocus
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 px-5 pb-10 text-[14px] resize-none outline-none"
        style={{ background: S.bg, color: S.text }}
        placeholder="在这里填写世界书内容..."
      />
    </div>
  );
}

export default function WorldBookEdit() {
  const navigate = useNavigate();
  const { id } = useParams();
  const isNew = !id;

  const [name, setName] = useState("");
  const [folder, setFolder] = useState("");
  const [activation, setActivation] = useState("always");
  const [keywords, setKeywords] = useState([]);
  const [messageMode, setMessageMode] = useState("long");
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(!isNew);
  const [toast, setToast] = useState(null);
  const [allFolders, setAllFolders] = useState([]);
  const [fullscreen, setFullscreen] = useState(false);

  const contentFileRef = useRef(null);

  const handleContentFile = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => setContent(ev.target.result || "");
    reader.readAsText(file);
    e.target.value = "";
  };

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => {
    apiFetch("/api/world-books")
      .then((d) => {
        const books = d.world_books || [];
        // Extract unique non-empty folders for autocomplete
        setAllFolders([...new Set(books.map((b) => b.folder).filter(Boolean))]);

        if (!isNew) {
          const book = books.find((b) => String(b.id) === String(id));
          if (book) {
            setName(book.name);
            setFolder(book.folder || "");
            setActivation(book.activation);
            setKeywords(book.keywords || []);
            setMessageMode(book.message_mode || "long");
            setContent(book.content);
          }
        }
      })
      .catch(() => showToast("加载失败"))
      .finally(() => setLoading(false));
  }, [id, isNew]);

  const handleSave = async () => {
    if (!name.trim()) { showToast("请输入名称"); return; }
    setSaving(true);
    try {
      const body = {
        name: name.trim(),
        folder: folder.trim() || null,
        activation,
        keywords: activation === "keyword" || activation === "mood" ? keywords : [],
        message_mode: activation === "message_mode" ? messageMode : null,
        content,
      };
      if (isNew) {
        await apiFetch("/api/world-books", { method: "POST", body });
        showToast("已创建");
      } else {
        await apiFetch(`/api/world-books/${id}`, { method: "PUT", body });
        showToast("已保存");
      }
    } catch (e) {
      showToast("保存失败: " + e.message);
    } finally {
      setSaving(false);
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
          onClick={() => navigate("/world-books", { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>
          {isNew ? "新建世界书" : "编辑世界书"}
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

      {/* Form */}
      <div className="flex-1 overflow-y-auto px-5 pb-10 pt-5">
        {loading ? (
          <div className="flex h-40 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : (<>
        <div
          className="rounded-[20px] p-5 mb-4"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <NmInput label="名称" value={name} onChange={setName} placeholder="世界书名称" />
          <FolderInput value={folder} onChange={setFolder} folders={allFolders} />
          <ActivationSelector value={activation} onChange={setActivation} />
          {activation === "keyword" && (
            <KeywordTags keywords={keywords} onChange={setKeywords} />
          )}
          {activation === "mood" && (
            <MoodChips selected={keywords} onChange={setKeywords} />
          )}
          {activation === "message_mode" && (
            <MessageModeChips selected={messageMode} onChange={setMessageMode} />
          )}
        </div>

        <div
          className="rounded-[20px] p-5"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          <div className="mb-1.5 flex items-center justify-between">
            <label className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
              内容
            </label>
            <div className="flex items-center gap-2">
              <button
                className="flex h-7 w-7 items-center justify-center rounded-full"
                style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg }}
                onClick={() => contentFileRef.current?.click()}
                title="从文件导入"
              >
                <FileText size={13} style={{ color: S.textMuted }} />
              </button>
              <input
                ref={contentFileRef}
                type="file"
                accept=".txt,.md,.text"
                className="hidden"
                onChange={handleContentFile}
              />
              <button
                className="flex h-7 w-7 items-center justify-center rounded-full"
                style={{ boxShadow: "var(--card-shadow-sm)", background: S.bg }}
                onClick={() => setFullscreen(true)}
                title="全屏编辑"
              >
                <Maximize2 size={13} style={{ color: S.accentDark }} />
              </button>
            </div>
          </div>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="在这里填写世界书内容..."
            rows={10}
            className="w-full rounded-[14px] px-4 py-3 text-[14px] resize-none outline-none"
            style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
          />
        </div>

        </>)}
      </div>

      {/* Fullscreen content editor */}
      {fullscreen && (
        <FullscreenEditor
          value={content}
          onChange={setContent}
          onClose={() => setFullscreen(false)}
        />
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
