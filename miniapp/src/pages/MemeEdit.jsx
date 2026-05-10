import { useState, useEffect } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ChevronLeft, Save, X, Plus } from "lucide-react";
import { apiFetch } from "../utils/api";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const CATEGORIES = [
  "句式与口头禅",
  "吐槽与评论",
  "发疯与情绪宣泄",
  "谐音与黑话",
  "特定人物/事件梗",
  "社交互动与关系",
  "其他流行语",
];

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

function CategorySelector({ value, onChange }) {
  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
        分类
      </label>
      <div
        className="flex flex-wrap gap-2 rounded-[14px] p-3"
        style={{ boxShadow: "var(--inset-shadow)", background: S.bg, minHeight: 48 }}
      >
        {CATEGORIES.map((c) => {
          const isSel = value === c;
          return (
            <button
              key={c}
              className="rounded-full px-3 py-1.5 text-[13px] font-medium transition-all"
              style={{
                background: isSel ? S.accentDark : S.bg,
                color: isSel ? "white" : S.textMuted,
                boxShadow: isSel ? "none" : "var(--card-shadow-sm)",
              }}
              onClick={() => onChange(isSel ? "" : c)}
            >
              {c}
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

export default function MemeEdit() {
  const navigate = useNavigate();
  const { id } = useParams();
  const [searchParams] = useSearchParams();
  const isNew = !id || id === "new";

  const backUrl = (() => {
    const p = new URLSearchParams({ tab: "meme" });
    const cat = searchParams.get("cat");
    const type = searchParams.get("type");
    const q = searchParams.get("q");
    if (cat) p.set("cat", cat);
    if (type) p.set("type", type);
    if (q) p.set("q", q);
    return `/world-books?${p.toString()}`;
  })();

  const CONTENT_FIELDS = [
    { key: "usage", label: "用法" },
    { key: "meaning", label: "含义" },
    { key: "source", label: "来源" },
    { key: "origin", label: "起源" },
    { key: "features", label: "特征" },
    { key: "core_meaning", label: "核心含义" },
    { key: "examples", label: "例句", isArray: true },
  ];

  const [term, setTerm] = useState("");
  const [category, setCategory] = useState("");
  const [type, setType] = useState("");
  const [content, setContent] = useState({});
  const [keywords, setKeywords] = useState([]);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(!isNew);
  const [toast, setToast] = useState(null);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => {
    if (!isNew) {
      apiFetch(`/api/memes/${id}`)
        .then((d) => {
          setTerm(d.term || "");
          setCategory(d.category || "");
          setType(d.type || "");
          setContent(d.content || {});
          setKeywords(d.keywords || []);
        })
        .catch(() => showToast("加载失败"))
        .finally(() => setLoading(false));
    }
  }, [id, isNew]);

  const setField = (key, val) => {
    setContent((prev) => {
      const next = { ...prev };
      if (!val || (Array.isArray(val) && val.length === 0) || val === "") {
        delete next[key];
      } else {
        next[key] = val;
      }
      return next;
    });
  };

  const handleSave = async () => {
    if (!term.trim()) { showToast("请输入梗名"); return; }
    setSaving(true);
    try {
      const body = {
        term: term.trim(),
        category: category || null,
        type: type.trim() || null,
        content: Object.keys(content).length ? content : null,
        keywords: keywords.length ? keywords : null,
      };
      if (isNew) {
        await apiFetch("/api/memes", { method: "POST", body });
        showToast("已创建");
      } else {
        await apiFetch(`/api/memes/${id}`, { method: "PUT", body });
        showToast("已保存");
      }
      navigate(backUrl, { replace: true });
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
          onClick={() => navigate(backUrl, { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="text-[17px] font-bold" style={{ color: S.text }}>
          {isNew ? "添加热梗" : "编辑热梗"}
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
          <NmInput label="梗名" value={term} onChange={setTerm} placeholder="例: 问就是XX？" />
          <NmInput label="类型" value={type} onChange={setType} placeholder="例: 万能回答句式" />
          <CategorySelector value={category} onChange={setCategory} />
          <KeywordTags keywords={keywords} onChange={setKeywords} />
        </div>

        <div
          className="rounded-[20px] p-5"
          style={{ background: S.bg, boxShadow: "var(--card-shadow)" }}
        >
          {CONTENT_FIELDS.map((f) => {
            const val = content[f.key];
            const hasValue = f.isArray ? (val && val.length > 0) : !!val;
            return (
              <div key={f.key} className="mb-4">
                <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide" style={{ color: S.textMuted }}>
                  {f.label}
                </label>
                {f.isArray ? (
                  <div>
                    {(val || []).map((item, i) => (
                      <div key={i} className="mb-2 flex items-start gap-2">
                        <input
                          type="text"
                          value={item}
                          onChange={(e) => {
                            const arr = [...(val || [])];
                            arr[i] = e.target.value;
                            setField(f.key, arr);
                          }}
                          className="flex-1 rounded-[14px] px-4 py-2.5 text-[13px] outline-none"
                          style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
                        />
                        <button
                          className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
                          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
                          onClick={() => {
                            const arr = (val || []).filter((_, j) => j !== i);
                            setField(f.key, arr);
                          }}
                        >
                          <X size={10} style={{ color: S.textMuted }} />
                        </button>
                      </div>
                    ))}
                    <button
                      className="rounded-[10px] px-3 py-1.5 text-[12px] font-medium"
                      style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark }}
                      onClick={() => setField(f.key, [...(val || []), ""])}
                    >
                      + 添加{f.label}
                    </button>
                  </div>
                ) : (
                  <textarea
                    value={val || ""}
                    onChange={(e) => setField(f.key, e.target.value)}
                    placeholder={`${f.label}...`}
                    rows={2}
                    className="w-full rounded-[14px] px-4 py-3 text-[13px] resize-none outline-none"
                    style={{ boxShadow: "var(--inset-shadow)", background: S.bg, color: S.text }}
                  />
                )}
              </div>
            );
          })}
        </div>
        </>)}
      </div>

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
