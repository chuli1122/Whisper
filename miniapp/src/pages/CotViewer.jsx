import { useState, useEffect, useRef, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronDown, ChevronUp, ChevronLeft, RefreshCw, Cpu } from "lucide-react";
import { apiFetch } from "../utils/api";
import { getAvatar } from "../utils/db";

const S = {
  bg: "var(--bg)",
  accent: "var(--accent)",
  accentDark: "var(--accent-dark)",
  text: "var(--text)",
  textMuted: "var(--text-muted)",
};

const BLOCK_COLORS = {
  thinking: { bg: "rgba(168,130,200,0.12)", color: "#8860c8", label: "思考" },
  thinking_fake: { bg: "rgba(144,136,160,0.12)", color: "#9088a0", label: "草稿" },
  tool_use: { bg: "rgba(232,160,60,0.12)", color: "#b8820a", label: "工具调用" },
  tool_result: { bg: "rgba(80,160,200,0.12)", color: "#1a7ab0", label: "工具结果" },
  injected_memories: { bg: "rgba(80,180,120,0.12)", color: "#3a8a5f", label: "注入记忆" },
  text: { bg: "rgba(232,160,191,0.08)", color: "#c9628a", label: "回复" },
  info: { bg: "rgba(136,136,160,0.08)", color: "#8888a0", label: "info" },
  error: { bg: "rgba(229,62,62,0.10)", color: "#e53e3e", label: "错误" },
  request_payload: { bg: "rgba(100,120,160,0.10)", color: "#5a7098", label: "请求" },
};

const MOODS = [
  { key: "happy", label: "开心" },
  { key: "sad", label: "难过" },
  { key: "angry", label: "生气" },
  { key: "anxious", label: "焦虑" },
  { key: "tired", label: "疲意" },
  { key: "emo", label: "低落" },
  { key: "flirty", label: "心动" },
  { key: "proud", label: "得意" },
  { key: "calm", label: "平静" },
];

const COLLAPSE_THRESHOLD = 300;

/* ── Block chip ── */

function BlockChip({ block_type }) {
  const meta = BLOCK_COLORS[block_type] || { bg: "rgba(136,136,160,0.1)", color: S.textMuted, label: block_type };
  return (
    <span
      className="shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ background: meta.bg, color: meta.color }}
    >
      {meta.label}
    </span>
  );
}

/* ── Expandable block content ── */

function BlockContent({ content, onInteract }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > COLLAPSE_THRESHOLD;

  return (
    <>
      <p className="whitespace-pre-wrap break-words text-[12px] leading-relaxed" style={{ color: S.text }}>
        {isLong && !expanded ? content.slice(0, COLLAPSE_THRESHOLD) + "..." : content}
      </p>
      {isLong && (
        <div className="mt-1.5 flex justify-center">
          <button
            className="rounded-full px-3 py-0.5 text-[11px]"
            style={{ color: S.accentDark, background: "rgba(232,160,191,0.12)" }}
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); if (!expanded && onInteract) onInteract(); }}
          >
            {expanded ? "收起" : "查看更多"}
          </button>
        </div>
      )}
    </>
  );
}

/* ── Request payload block ── */

function prettyStringify(obj, indent = 2) {
  const NL = "\u2408";
  const clone = JSON.parse(JSON.stringify(obj, (_k, v) =>
    typeof v === "string" ? v.replace(/\n/g, NL) : v
  ));
  let json = JSON.stringify(clone, null, indent);
  const lines = json.split("\n");
  const out = [];
  for (const line of lines) {
    if (!line.includes(NL)) { out.push(line); continue; }
    const indentMatch = line.match(/^(\s*)/);
    const pad = indentMatch ? indentMatch[1] : "";
    const parts = line.split(NL);
    out.push(parts[0]);
    for (let i = 1; i < parts.length; i++) out.push(pad + parts[i]);
  }
  return out.join("\n");
}

function RequestPayloadBlock({ block, collapsed, requestId, roundIndex }) {
  const meta = BLOCK_COLORS.request_payload;
  const [payloadOpen, setPayloadOpen] = useState(false);
  const [remotePayload, setRemotePayload] = useState(null);
  const [loadingPayload, setLoadingPayload] = useState(false);
  const stop = (e) => e.stopPropagation();
  let snapshot = null;
  try { snapshot = JSON.parse(block.content); } catch { /* keep null */ }

  // Backend strips the heavy payload from list_cot. Lazy-fetch when user opens it.
  const needsRemote = !!snapshot && (snapshot.payload == null);
  useEffect(() => {
    if (payloadOpen && needsRemote && remotePayload == null && !loadingPayload && requestId && roundIndex != null) {
      setLoadingPayload(true);
      apiFetch(`/api/cot/${requestId}/request-payload/${roundIndex}`)
        .then((data) => setRemotePayload(data?.payload ?? null))
        .catch(() => {})
        .finally(() => setLoadingPayload(false));
    }
  }, [payloadOpen, needsRemote, remotePayload, loadingPayload, requestId, roundIndex]);

  if (!snapshot) {
    return (
      <div className="mb-2 rounded-[12px] p-3" style={{ background: meta.bg }}>
        <div className="flex items-center gap-2">
          <BlockChip block_type="request_payload" />
          <span className="text-[10px] font-mono opacity-70" style={{ color: meta.color }}>parse failed</span>
        </div>
        {!collapsed && (
          <pre className="mt-2 whitespace-pre-wrap break-all text-[10px] font-mono" style={{ color: S.text }}>
            {block.content}
          </pre>
        )}
      </div>
    );
  }

  const { provider, payload, token_stats: ts = {}, cache_bp_positions: bp = {} } = snapshot;
  const msgByRole = ts.messages_by_role || {};
  const msgCountByRole = ts.messages_count_by_role || {};
  const sysBp = Array.isArray(bp.system) ? bp.system : [];
  const msgBp = Array.isArray(bp.messages) ? bp.messages : [];

  return (
    <div className="mb-2 rounded-[12px] p-3" style={{ background: meta.bg }}>
      <div className={collapsed ? "flex items-center gap-2" : "mb-2 flex items-center gap-2"}>
        <BlockChip block_type="request_payload" />
        <span className="text-[10px] font-mono" style={{ color: meta.color }}>
          {provider} · ≈{ts.grand_total ?? 0} tk
        </span>
        {collapsed && (
          <span className="flex-1 min-w-0 truncate text-[10px] font-mono opacity-70" style={{ color: meta.color }}>
            sys={ts.system ?? 0} tools={ts.tools ?? 0} msgs={ts.messages_total ?? 0}
          </span>
        )}
      </div>
      {!collapsed && (
        <>
          <div className="mb-2 rounded-[8px] p-2" style={{ background: "rgba(255,255,255,0.45)" }}>
            <div className="mb-1 text-[10px] font-semibold" style={{ color: meta.color }}>Token 分布（估算）</div>
            <div className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-[10px] font-mono" style={{ color: S.text }}>
              <span>system</span><span className="text-right">{ts.system ?? 0}</span>
              <span>tools</span><span className="text-right">{ts.tools ?? 0}</span>
              <span>messages</span><span className="text-right">{ts.messages_total ?? 0}</span>
              {Object.entries(msgByRole).flatMap(([role, n]) => [
                <span key={`${role}-k`} className="pl-3 opacity-70">· {role}{msgCountByRole[role] ? ` ×${msgCountByRole[role]}` : ""}</span>,
                <span key={`${role}-v`} className="text-right opacity-70">{n}</span>,
              ])}
              <span className="font-semibold">total</span><span className="text-right font-semibold">{ts.grand_total ?? 0}</span>
            </div>
          </div>
          {(sysBp.length > 0 || msgBp.length > 0) && (
            <div className="mb-2 rounded-[8px] p-2" style={{ background: "rgba(255,255,255,0.45)" }}>
              <div className="mb-1 text-[10px] font-semibold" style={{ color: meta.color }}>缓存断点</div>
              <div className="text-[10px] font-mono leading-relaxed" style={{ color: S.text }}>
                {sysBp.length > 0 && (
                  <div>system: {sysBp.map((i) => <span key={i} className="mr-1 rounded px-1" style={{ background: "rgba(100,120,160,0.18)" }}>[{i}]</span>)}</div>
                )}
                {msgBp.length > 0 && (
                  <div>messages: {msgBp.map((x, j) => {
                    const [idx, role] = Array.isArray(x) ? x : [x, "?"];
                    return <span key={j} className="mr-1 rounded px-1" style={{ background: "rgba(100,120,160,0.18)" }}>[{idx}:{role}]</span>;
                  })}</div>
                )}
              </div>
            </div>
          )}
          <button
            type="button"
            className="w-full text-left text-[10px] font-semibold"
            style={{ color: meta.color }}
            onClick={(e) => { stop(e); setPayloadOpen((v) => !v); }}
          >
            {payloadOpen ? "▼" : "▶"} 完整 payload
          </button>
          {payloadOpen && (
            <pre
              className="mt-1 max-h-[400px] overflow-auto whitespace-pre-wrap break-all rounded-[8px] p-2 text-[10px] font-mono"
              style={{ background: "rgba(0,0,0,0.06)", color: S.text }}
              onClick={stop}
            >
              {loadingPayload
                ? "加载中..."
                : prettyStringify(payload ?? remotePayload)}
            </pre>
          )}
        </>
      )}
    </div>
  );
}

/* ── Thinking block with translate ── */

function ThinkingBlock({ block, cacheKey, translateCache, collapsed, onInteract, assistantId, isFake }) {
  const cached = translateCache.current.get(cacheKey);
  const [translated, setTranslated] = useState(cached || null);
  const [showTranslated, setShowTranslated] = useState(!!cached);
  const [translating, setTranslating] = useState(false);
  const meta = isFake ? BLOCK_COLORS.thinking_fake : BLOCK_COLORS.thinking;

  const handleTranslate = async (e) => {
    e.stopPropagation();
    if (onInteract) onInteract();
    if (translated) { setShowTranslated(!showTranslated); return; }
    setTranslating(true);
    try {
      const data = await apiFetch("/api/cot/translate", { method: "POST", body: { text: block.content, assistant_id: assistantId } });
      setTranslated(data.translated);
      setShowTranslated(true);
      translateCache.current.set(cacheKey, data.translated);
    } catch (err) {
      console.error("Translation failed:", err);
      alert("翻译失败，请稍后重试");
    }
    finally { setTranslating(false); }
  };

  return (
    <div className="mb-2 rounded-[12px] p-3" style={{ background: meta.bg }}>
      <div className={collapsed ? "flex items-center gap-2" : "mb-1 flex items-center gap-2"}>
        <BlockChip block_type={isFake ? "thinking_fake" : "thinking"} />
        {collapsed && block.content && (
          <span className="flex-1 min-w-0 truncate text-[10px] font-mono opacity-85" style={{ color: meta.color }}>
            {block.content.replace(/\n/g, " ").slice(0, 50)}
          </span>
        )}
        {!collapsed && (
          <>
            <span className="flex-1" />
            <button
              className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
              style={{ color: "#1a7ab0", background: "rgba(80,160,200,0.15)" }}
              onClick={handleTranslate}
              disabled={translating}
            >
              {translating ? "翻译中..." : showTranslated ? "原文" : "翻译"}
            </button>
          </>
        )}
      </div>
      {!collapsed && <BlockContent content={showTranslated && translated ? translated : block.content} onInteract={onInteract} />}
    </div>
  );
}

/* ── Avatar helper ── */

function AvatarIcon({ avatarUrl }) {
  return (
    <div
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full overflow-hidden"
      style={{ boxShadow: "var(--icon-inset)", background: S.bg }}
    >
      {avatarUrl ? (
        <img src={avatarUrl} alt="" className="h-full w-full object-cover" />
      ) : (
        <span style={{ fontSize: 16 }}>🐰</span>
      )}
    </div>
  );
}

/* ── COT Card ── */

function fmtTokens(n) {
  if (!n) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function fmtElapsed(ms) {
  if (!ms) return null;
  const sec = ms / 1000;
  return sec >= 100 ? `${Math.round(sec)}s` : `${sec.toFixed(1)}s`;
}

// API pricing per million tokens. Anthropic models quote in USD, GLM in CNY.
const MODEL_PRICING = {
  opus:   { input: 5, cache_create: 10, cache_read: 0.50, output: 25, currency: "USD" },
  sonnet: { input: 3, cache_create: 3.75, cache_read: 0.30, output: 15, currency: "USD" },
  haiku:  { input: 0.80, cache_create: 1.00, cache_read: 0.08, output: 4, currency: "USD" },
  glm_short: { input: 6, cache_create: 0, cache_read: 1.3, output: 24, currency: "CNY" },
  glm_long:  { input: 8, cache_create: 0, cache_read: 2,   output: 28, currency: "CNY" },
};

function getPricing(modelName, totalInput = 0) {
  if (!modelName) return MODEL_PRICING.opus;
  const m = modelName.toLowerCase();
  if (m.includes("opus")) return MODEL_PRICING.opus;
  if (m.includes("haiku")) return MODEL_PRICING.haiku;
  if (m.includes("sonnet") || m.includes("claude")) return MODEL_PRICING.sonnet;
  if (m.includes("glm")) return totalInput >= 32000 ? MODEL_PRICING.glm_long : MODEL_PRICING.glm_short;
  return null;
}

function calcRoundCost(pricing, usage) {
  if (!pricing || !usage) return 0;
  return (
    (usage.input || 0) * pricing.input / 1e6 +
    (usage.cache_create || 0) * pricing.cache_create / 1e6 +
    (usage.cache_read || 0) * pricing.cache_read / 1e6 +
    (usage.output || 0) * pricing.output / 1e6
  );
}

function fmtCost(cost, currency = "USD") {
  if (cost <= 0) return null;
  const sym = currency === "CNY" ? "¥" : "$";
  if (cost < 0.01) return `${sym}${cost.toFixed(4)}`;
  if (cost < 0.1) return `${sym}${cost.toFixed(3)}`;
  return `${sym}${cost.toFixed(2)}`;
}

function TokenBadges({ prompt, completion, elapsedMs, hasToolCalls, cacheHit, totalInput }) {
  const hasAnyValue = prompt || completion || elapsedMs;
  if (!hasAnyValue && !hasToolCalls) return null;

  const promptBg = cacheHit ? "rgba(220,120,160,0.12)" : "rgba(80,160,120,0.12)";
  const promptColor = cacheHit ? "#d478a0" : "#3a8a5f";

  return (
    <>
      <span
        className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold whitespace-nowrap"
        style={{ background: promptBg, color: promptColor }}
      >
        ↑{fmtTokens(prompt)}{totalInput > 0 && totalInput !== prompt ? `/${fmtTokens(totalInput)}` : ""}
      </span>
      <span
        className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold whitespace-nowrap"
        style={{ background: "rgba(160,100,220,0.12)", color: "#8a5abf" }}
      >
        ↓{fmtTokens(completion)}
      </span>
      {elapsedMs ? (
        <span
          className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold whitespace-nowrap"
          style={{ background: "rgba(200,140,60,0.12)", color: "#b8820a" }}
        >
          {fmtElapsed(elapsedMs)}
        </span>
      ) : null}
    </>
  );
}

function pairToolBlocks(blocks) {
  // Assign stable _idx so we can track identity without relying on .id
  const typeOrder = { request_payload: -2, injected_memories: -1, info: -0.5, thinking: 0, thinking_fake: 0.5, text: 1, tool_use: 2, tool_result: 3 };
  const sorted = [...blocks].map((b, i) => ({ ...b, _idx: i })).sort((a, b) => {
    const ra = a.round_index ?? 0;
    const rb = b.round_index ?? 0;
    if (ra !== rb) return ra - rb;
    return (typeOrder[a.block_type] ?? 99) - (typeOrder[b.block_type] ?? 99);
  });
  // Pair each tool_use with its tool_result (by tool_name within same round)
  const result = [];
  const usedResultIdxs = new Set();
  for (const b of sorted) {
    if (b.block_type === "tool_result") continue;
    result.push(b);
    if (b.block_type === "tool_use") {
      const match = sorted.find(
        (r) => r.block_type === "tool_result" && r.round_index === b.round_index
          && r.tool_name === b.tool_name && !usedResultIdxs.has(r._idx)
      );
      if (match) {
        usedResultIdxs.add(match._idx);
        result.push(match);
      }
    }
  }
  // Add any unmatched tool_results
  for (const b of sorted) {
    if (b.block_type === "tool_result" && !usedResultIdxs.has(b._idx)) {
      result.push(b);
    }
  }
  // Deduplicate by _idx
  const seen = new Set();
  return result.filter((b) => {
    if (seen.has(b._idx)) return false;
    seen.add(b._idx);
    return true;
  });
}

const RECALL_SOURCE_LABEL = { search: "①", disclosure: "①", tags: "②" };

function InjectedMemoriesBlock({ memories }) {
  const [open, setOpen] = useState(false);
  if (!memories || memories.length === 0) return null;
  const meta = BLOCK_COLORS.injected_memories;
  const counts = { search: 0, disclosure: 0, tags: 0 };
  memories.forEach((m) => { if (m.recall_source && counts[m.recall_source] !== undefined) counts[m.recall_source]++; });
  const searchTotal = counts.search + counts.disclosure;
  const parts = [];
  if (searchTotal) parts.push(`混合搜索${searchTotal}条①`);
  if (counts.tags) parts.push(`tags扩展${counts.tags}条②`);
  const summary = parts.length ? `共${memories.length}条（${parts.join(" · ")}）` : `${memories.length}条`;
  return (
    <div className="mb-2 rounded-[12px] p-3" style={{ background: meta.bg }}>
      <button className="flex w-full items-center gap-2" onClick={() => setOpen(!open)}>
        <BlockChip block_type="injected_memories" />
        <span className="text-[10px] font-semibold" style={{ color: meta.color }}>
          {summary}
        </span>
        {open ? <ChevronUp size={12} style={{ color: meta.color }} /> : <ChevronDown size={12} style={{ color: meta.color }} />}
      </button>
      {open && (
        <div className="mt-2 space-y-1">
          {memories.map((m) => (
            <p key={m.id} className="text-[11px] leading-relaxed" style={{ color: S.text }}>
              <span style={{ color: meta.color, fontWeight: 600 }}>#{m.id}</span> {m.content}
              {m.recall_source && RECALL_SOURCE_LABEL[m.recall_source] && (
                <span style={{ color: meta.color, fontWeight: 600, marginLeft: 4 }}>{RECALL_SOURCE_LABEL[m.recall_source]}</span>
              )}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function ModelInfoBadge({ info }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  if (!info) return null;
  const label = info.source || "摘要";
  const handleClick = (e) => {
    e.stopPropagation();
    if (!open && ref.current) {
      const rect = ref.current.getBoundingClientRect();
      setPos({ top: rect.bottom + 4, left: rect.left });
    }
    setOpen(!open);
  };
  return (
    <>
      <span
        ref={ref}
        className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold cursor-pointer"
        style={{ background: "rgba(100,140,200,0.15)", color: "#4a7ab5" }}
        onClick={handleClick}
      >
        {label}
      </span>
      {open && (
        <div
          className="fixed z-50 rounded-xl p-2.5 text-[11px] shadow-lg whitespace-nowrap"
          style={{ background: S.bg, border: "1px solid rgba(136,136,160,0.15)", color: S.text, top: pos.top, left: pos.left }}
          onClick={(e) => { e.stopPropagation(); setOpen(false); }}
        >
          <p><span style={{ color: S.textMuted }}>预设:</span> {info.preset_name}</p>
          <p><span style={{ color: S.textMuted }}>模型:</span> {info.model_name}</p>
        </div>
      )}
    </>
  );
}

function CotCard({ item, expanded, onToggle, live, avatarUrl, translateCache, pageAssistantId }) {
  const [expandedBlocks, setExpandedBlocks] = useState(new Set());

  // Reset inner expanded state when card is collapsed
  useEffect(() => {
    if (!expanded) setExpandedBlocks(new Set());
  }, [expanded]);

  // Filter out "usage" blocks only; keep text blocks in natural position
  const displayRounds = item.rounds.map((round) => {
    const roundUsageBlock = round.blocks.find((b) => b.block_type === "round_usage");
    let roundUsage = null;
    if (roundUsageBlock) {
      try { roundUsage = JSON.parse(roundUsageBlock.content); } catch {}
    }
    return {
      ...round,
      blocks: pairToolBlocks(round.blocks.filter((b) => b.block_type !== "usage" && b.block_type !== "round_usage")),
      roundUsage,
    };
  }).filter((round) => round.blocks.length > 0);

  const totalCost = useMemo(() => {
    let cost = 0;
    let currency = "USD";
    for (const r of displayRounds) {
      const u = r.roundUsage || {};
      const totalInput = (u.input || 0) + (u.cache_create || 0) + (u.cache_read || 0);
      const pricing = getPricing(item.model_info?.model_name, totalInput);
      if (!pricing) continue;
      cost += calcRoundCost(pricing, r.roundUsage);
      currency = pricing.currency || "USD";
    }
    return { cost, currency };
  }, [displayRounds, item.model_info]);

  // Determine which blocks are actively streaming (thinking and text tracked separately).
  // Check the LAST raw block type to know what's actively receiving deltas:
  //   - last raw block is "thinking" → thinking is streaming
  //   - last raw block is "text" → text is streaming
  //   - last raw block is tool_use/tool_result → nothing is streaming text/thinking
  // This way thinking collapses as soon as tool_use or text arrives (not waiting for text).
  const { streamingThinkingKey, streamingTextKey } = useMemo(() => {
    if (!live || displayRounds.length === 0) return { streamingThinkingKey: null, streamingTextKey: null };

    const lastOrigRound = item.rounds[item.rounds.length - 1];
    const rawBlocks = (lastOrigRound?.blocks || []).filter((b) => b.block_type !== "usage");
    const lastRawType = rawBlocks.length > 0 ? rawBlocks[rawBlocks.length - 1].block_type : null;

    const lastRound = displayRounds[displayRounds.length - 1];
    let thinkingKey = null;
    let textKey = null;
    for (let i = 0; i < lastRound.blocks.length; i++) {
      const bt = lastRound.blocks[i].block_type;
      if (bt === "thinking") thinkingKey = `${lastRound.round_index}:${i}`;
      if (bt === "text") textKey = `${lastRound.round_index}:${i}`;
    }
    if (lastRawType !== "thinking") thinkingKey = null;
    if (lastRawType !== "text") textKey = null;

    return { streamingThinkingKey: thinkingKey, streamingTextKey: textKey };
  }, [live, displayRounds, item.rounds]);

  const hasContent = displayRounds.length > 0 || (item.injectedMemories && item.injectedMemories.length > 0);

  const toggleBlock = (blockKey) => {
    setExpandedBlocks((prev) => {
      const next = new Set(prev);
      if (next.has(blockKey)) next.delete(blockKey);
      else next.add(blockKey);
      return next;
    });
  };

  // Pin a block as expanded (don't auto-collapse) when user interacts with content inside
  const pinBlock = (blockKey) => {
    setExpandedBlocks((prev) => {
      if (prev.has(blockKey)) return prev;
      const next = new Set(prev);
      next.add(blockKey);
      return next;
    });
  };

  return (
    <div className="rounded-[18px] overflow-hidden" style={{ background: S.bg }}>
      <button className="flex w-full items-center gap-3 p-4" onClick={onToggle}>
        <AvatarIcon avatarUrl={avatarUrl} />
        <div className="flex-1 min-w-0 text-left">
          <div className="flex items-center gap-2 flex-wrap">
            {live && (
              <span
                className="inline-block h-3 w-3 animate-spin rounded-full border-[1.5px]"
                style={{ borderColor: S.accentDark, borderTopColor: "transparent" }}
              />
            )}
            <ModelInfoBadge info={item.model_info} />
            {item.has_tool_calls && (
              <span
                className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold"
                style={{ background: "rgba(232,160,60,0.15)", color: "#b8820a" }}
              >
                工具
              </span>
            )}
            <TokenBadges
              prompt={item.prompt_tokens || 0}
              completion={item.completion_tokens || 0}
              elapsedMs={item.elapsed_ms || 0}
              hasToolCalls={item.has_tool_calls}
              cacheHit={item.cache_hit}
              totalInput={item.total_input || 0}
            />
            {totalCost.cost > 0 && (
              <span
                className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold whitespace-nowrap"
                style={{ background: "rgba(60,180,120,0.12)", color: "#2a8a5a" }}
              >
                {fmtCost(totalCost.cost, totalCost.currency)}
              </span>
            )}
            <span className="text-[10px]" style={{ color: S.textMuted }}>
              {item.created_at || ""}
            </span>
          </div>
          <p className="mt-0.5 truncate text-[13px]" style={{ color: item.error ? "#e53e3e" : S.text }}>
            {item.error ? `API Error: ${item.error}` : item.preview || "(无预览)"}
          </p>
        </div>
        {expanded ? (
          <ChevronUp size={16} style={{ color: S.textMuted, flexShrink: 0 }} />
        ) : (
          <ChevronDown size={16} style={{ color: S.textMuted, flexShrink: 0 }} />
        )}
      </button>

      {expanded && (hasContent || item.error) && (
        <div className="px-4 pb-4">
          {/* Error banner */}
          {item.error && (
            <div className="mb-3 rounded-xl p-3 text-[12px]" style={{ background: "rgba(229,62,62,0.08)", color: "#e53e3e" }}>
              {item.error}
            </div>
          )}
          {/* Injected memories */}
          <InjectedMemoriesBlock memories={item.injectedMemories} />

          {displayRounds.map((round) => (
            <div key={round.round_index} className="mb-3">
              {(
                <div className="mb-2 flex items-center gap-2">
                  <div className="h-px flex-1" style={{ background: "rgba(136,136,160,0.15)" }} />
                  <span className="text-[10px] font-semibold" style={{ color: S.textMuted }}>
                    轮 {round.round_index + 1}
                    {round.roundUsage && (
                      <span className="font-normal ml-1" style={{ color: "rgba(136,136,160,0.6)" }}>
                        ({(() => {
                          const parts = [
                            round.roundUsage.cache_read > 0 && `缓存${fmtTokens(round.roundUsage.cache_read)}`,
                            round.roundUsage.cache_create > 0 && `新建${fmtTokens(round.roundUsage.cache_create)}`,
                          ].filter(Boolean);
                          return parts.length > 0 ? parts.join(" · ") + " · " : "";
                        })()}↑{fmtTokens(round.roundUsage.input)} · ↓{fmtTokens(round.roundUsage.output)}{round.roundUsage.thinking_est > 100 ? ` · 思考≈${fmtTokens(round.roundUsage.thinking_est)}` : ""}{(() => {
                          const u = round.roundUsage;
                          const totalInput = (u.input || 0) + (u.cache_create || 0) + (u.cache_read || 0);
                          const pricing = getPricing(item.model_info?.model_name, totalInput);
                          const c = fmtCost(calcRoundCost(pricing, u), pricing?.currency);
                          return c ? ` · ${c}` : "";
                        })()})
                      </span>
                    )}
                  </span>
                  <div className="h-px flex-1" style={{ background: "rgba(136,136,160,0.15)" }} />
                </div>
              )}
              {round.blocks.flatMap((block, i) => {
                // Split text blocks containing [THINK]...[/THINK] into sub-blocks
                if (block.block_type === "text" && block.content && (block.content.includes("[THINK]") || block.content.includes("<scratchpad>"))) {
                  const parts = block.content.split(/(?:\[THINK\]|<scratchpad>)([\s\S]*?)(?:\[\/THINK\]|<\/THINK>|<\/thinking>|<\/scratchpad>)/g);
                  const subBlocks = [];
                  for (let pi = 0; pi < parts.length; pi++) {
                    const part = parts[pi]?.trim();
                    if (!part) continue;
                    const isThinkPart = pi % 2 === 1;
                    subBlocks.push({
                      ...block,
                      block_type: isThinkPart ? "thinking" : "text",
                      _is_fake: isThinkPart,
                      content: part,
                      _subIndex: `${i}_${pi}`,
                    });
                  }
                  return subBlocks.length > 0 ? subBlocks : [block];
                }
                return [block];
              }).map((block, i) => {
                const blockKey = block._subIndex ? `${round.round_index}:${block._subIndex}` : `${round.round_index}:${i}`;
                const isBlockExpanded = expandedBlocks.has(blockKey) || blockKey === streamingThinkingKey || blockKey === streamingTextKey;

                if (block.block_type === "request_payload") {
                  return (
                    <div
                      key={i}
                      onClick={() => toggleBlock(blockKey)}
                    >
                      <RequestPayloadBlock
                        block={block}
                        collapsed={!isBlockExpanded}
                        requestId={item.request_id}
                        roundIndex={round.round_index}
                      />
                    </div>
                  );
                }

                if (block.block_type === "thinking" || block.block_type === "thinking_fake") {
                  return (
                    <div
                      key={i}
                      onClick={() => toggleBlock(blockKey)}
                    >
                      <ThinkingBlock
                        block={block}
                        cacheKey={`${item.request_id}:${round.round_index}:${i}`}
                        translateCache={translateCache}
                        collapsed={!isBlockExpanded}
                        onInteract={() => pinBlock(blockKey)}
                        assistantId={item.assistant_id ?? pageAssistantId}
                        isFake={block.block_type === "thinking_fake" || !!block._is_fake}
                      />
                    </div>
                  );
                }

                // Sanitize diary write: only show title, hide content
                let displayContent = block.content;
                if (block.block_type === "tool_use" && (block.tool_name === "write_diary" || block.tool_name === "diary")) {
                  try {
                    const args = JSON.parse(block.content);
                    if (block.tool_name === "write_diary" || args.action === "write") {
                      displayContent = `📝 ${args.title || ""}${args.unlock_at ? `\n🔒 ${args.unlock_at}` : ""}`;
                    }
                  } catch { /* keep original */ }
                }
                // Render reminder tool results nicely
                if (block.block_type === "tool_result" && (block.tool_name === "set_reminder" || block.tool_name === "cancel_reminder" || block.tool_name === "list_reminders" || block.tool_name === "reminder")) {
                  try {
                    const data = JSON.parse(block.content);
                    const lines = [];
                    if (data.message) lines.push(data.message);
                    if (data.id) lines.push(`id: #${data.id}`);
                    const pending = data.pending_reminders || data.reminders || [];
                    if (pending.length > 0) {
                      lines.push(`\n⏰ 待提醒 (${pending.length})`);
                      for (const r of pending) {
                        const time = r.remind_at || "";
                        const left = r.minutes_left != null ? `${r.minutes_left}分钟后` : "";
                        const reason = r.reason ? ` | ${r.reason}` : "";
                        lines.push(`  #${r.id}  ${time} (${left})${reason}`);
                      }
                    } else if (data.status === "ok") {
                      lines.push("\n⏰ 暂无待提醒闹钟");
                    }
                    displayContent = lines.join("\n");
                  } catch { /* keep original */ }
                }

                // Render diary list results nicely
                if (block.block_type === "tool_result" && (block.tool_name === "read_diary" || block.tool_name === "diary")) {
                  try {
                    const data = JSON.parse(block.content);
                    if (data.diaries) {
                      const lines = [`📖 交换日记 (${data.total}篇)`];
                      for (const d of data.diaries) {
                        const read = d.read_at ? `✓已读 ${d.read_at}` : d.is_read ? "✓已读" : "";
                        const lock = d.locked ? " 🔒" : "";
                        lines.push(`  #${d.id} ${d.title}${lock}\n    ${d.author} · ${d.created_at}${read ? ` · ${read}` : ""}`);
                      }
                      displayContent = lines.join("\n");
                    } else if (data.content) {
                      displayContent = `📖 #${data.id} ${data.title}\n${data.author} · ${data.created_at}\n\n${data.content}`;
                    }
                  } catch { /* keep original */ }
                }

                const meta = BLOCK_COLORS[block.block_type] || { bg: "rgba(136,136,160,0.08)", color: S.textMuted, label: block.block_type };
                // Info blocks: always inline, not expandable
                if (block.block_type === "info") {
                  return (
                    <div key={i} className="mb-2 rounded-[12px] px-3 py-2 flex items-center gap-2" style={{ background: meta.bg }}>
                      <BlockChip block_type="info" />
                      <span className="flex-1 min-w-0 text-[10px] font-mono" style={{ color: meta.color }}>
                        {displayContent}
                      </span>
                    </div>
                  );
                }
                return (
                  <div
                    key={i}
                    className="mb-2 rounded-[12px] p-3"
                    style={{ background: meta.bg }}
                    onClick={() => toggleBlock(blockKey)}
                  >
                    <div className={isBlockExpanded ? "mb-1 flex items-center gap-2" : "flex items-center gap-2"}>
                      <BlockChip block_type={block.block_type} />
                      {block.tool_name && (
                        <span className="text-[10px] font-mono" style={{ color: meta.color }}>
                          {block.tool_name}
                        </span>
                      )}
                      {!isBlockExpanded && block.block_type === "text" && displayContent && (
                        <span className="flex-1 min-w-0 truncate text-[10px] font-mono opacity-85" style={{ color: meta.color }}>
                          {displayContent.replace(/\n/g, " ").slice(0, 50)}
                        </span>
                      )}
                    </div>
                    {isBlockExpanded && <BlockContent content={displayContent} onInteract={() => pinBlock(blockKey)} />}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Main page ── */

export default function CotViewer() {
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedIds, setExpandedIds] = useState(new Set());
  const [wsConnected, setWsConnected] = useState(false);
  const [liveRequestIds, setLiveRequestIds] = useState(new Set());
  const [error, setError] = useState(null);
  const [avatarUrl, setAvatarUrl] = useState(null);
  const wsRef = useRef(null);
  const translateCacheRef = useRef(new Map());
  const cotListRef = useRef(null);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const [mood, setMood] = useState(null);
  const [moodOpen, setMoodOpen] = useState(false);
  const moodRef = useRef(null);
  const [pendingCount, setPendingCount] = useState(0);
  const manuallyCollapsedRef = useRef(new Set());
  const apiLoadedRef = useRef(false);
  const pendingWsMsgsRef = useRef([]);
  const [assistants, setAssistants] = useState([]);
  const [assistantId, setAssistantId] = useState(null);
  const [avatarMap, setAvatarMap] = useState({});
  const [assistantPickerOpen, setAssistantPickerOpen] = useState(false);
  const assistantPickerRef = useRef(null);
  const assistantIdRef = useRef(assistantId);
  const [monthlyCost, setMonthlyCost] = useState(null);
  const [trimStatus, setTrimStatus] = useState(null);
  const [displayCount, setDisplayCount] = useState(50);

  const [wsToken, setWsToken] = useState(() => localStorage.getItem("whisper_token"));

  const processWsMsg = useRef(null);

  const load = () => {
    setLoading(true);
    setError(null);
    apiLoadedRef.current = false;
    apiFetch("/api/cot/monthly-cost").then(setMonthlyCost).catch(() => {});
    apiFetch("/api/cot/trim-status").then(setTrimStatus).catch(() => {});
    setDisplayCount(50);
    const qs = assistantId ? `/api/cot?limit=500&assistant_id=${assistantId}` : "/api/cot?limit=500";
    apiFetch(qs)
      .then((data) => {
        setItems(Array.isArray(data) ? data : []);
        apiLoadedRef.current = true;
        const pending = pendingWsMsgsRef.current;
        pendingWsMsgsRef.current = [];
        if (pending.length > 0 && processWsMsg.current) {
          for (const msg of pending) processWsMsg.current(msg);
        }
      })
      .catch((err) => {
        console.error("COT load error:", err);
        setError(err.message || "加载失败");
        apiLoadedRef.current = true;
      })
      .finally(() => {
        setLoading(false);
        const t = localStorage.getItem("whisper_token");
        if (t && t !== wsToken) setWsToken(t);
      });
  };

  useEffect(() => { assistantIdRef.current = assistantId; }, [assistantId]);
  useEffect(() => { if (assistantId !== null) load(); }, [assistantId]);

  // Load assistants + avatars + current assistant from latest session
  useEffect(() => {
    Promise.all([
      apiFetch("/api/assistants"),
      apiFetch("/api/sessions?limit=1"),
    ]).then(async ([aData, sData]) => {
      const list = aData.assistants || [];
      setAssistants(list);
      const sess = sData.sessions?.[0];
      const currentId = sess?.assistant_id || list[0]?.id || null;
      setAssistantId(currentId);
      const map = {};
      for (const a of list) {
        try {
          const b64 = await getAvatar(`assistant-avatar-${a.id}`);
          if (b64) map[a.id] = b64;
        } catch {}
      }
      setAvatarMap(map);
      if (currentId && map[currentId]) setAvatarUrl(map[currentId]);
    }).catch(() => {
      // If assistants fetch fails, still trigger load with null assistantId
      load();
    });
  }, []);

  // Load current mood + pending memory count
  useEffect(() => {
    apiFetch("/api/settings/mood")
      .then((data) => setMood(data.mood || "calm"))
      .catch(() => setMood("calm"));
    apiFetch("/api/pending-memories/count")
      .then((data) => setPendingCount(data.count || 0))
      .catch(() => {});
  }, []);

  // Close mood popup on outside click
  useEffect(() => {
    if (!moodOpen) return;
    const handler = (e) => {
      if (moodRef.current && !moodRef.current.contains(e.target)) setMoodOpen(false);
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [moodOpen]);

  // Close assistant picker on outside click
  useEffect(() => {
    if (!assistantPickerOpen) return;
    const handler = (e) => {
      if (assistantPickerRef.current && !assistantPickerRef.current.contains(e.target)) setAssistantPickerOpen(false);
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [assistantPickerOpen]);

  // WebSocket connection for real-time COT push
  useEffect(() => {
    if (!wsToken) {
      console.log("[COT WS] No token yet, waiting...");
      return;
    }

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/ws/cot?token=${encodeURIComponent(wsToken)}`;
    console.log("[COT WS] Connecting to", wsUrl);

    let ws;
    let reconnectTimer;
    let closed = false;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        console.log("[COT WS] Connected");
        setWsConnected(true);
        // Refresh mood on reconnect (may have changed while disconnected)
        apiFetch("/api/settings/mood")
          .then((data) => setMood(data.mood || "calm"))
          .catch(() => {});
      };

      const handleWsMsg = (msg) => {
          const { type, request_id } = msg;

          // Filter by current assistant
          const curAid = assistantIdRef.current;
          if (curAid != null && msg.assistant_id != null && msg.assistant_id !== curAid) return;

          // Mood change from keyword detection
          if (type === "mood_change") {
            setMood(msg.mood);
            return;
          }

          // Helper: ensure item exists, returns [newArray, itemIndex]
          const ensureItem = (prev) => {
            const idx = prev.findIndex((it) => it.request_id === request_id);
            if (idx >= 0) return [prev, idx];
            const now = new Date();
            const newItem = {
              request_id,
              created_at: now.toLocaleDateString("zh-CN", {
                month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
              }),
              preview: "思考中...",
              has_tool_calls: false,
              rounds: [],
              injectedMemories: [],
            };
            const next = [newItem, ...prev];
            return [next, 0];
          };

          // Helper: ensure round exists in rounds array
          const ensureRound = (rounds, roundIndex) => {
            const ri = rounds.findIndex((r) => r.round_index === roundIndex);
            if (ri >= 0) return [rounds, ri];
            const next = [...rounds, { round_index: roundIndex, blocks: [] }];
            next.sort((a, b) => a.round_index - b.round_index);
            return [next, next.findIndex((r) => r.round_index === roundIndex)];
          };

          // Auto-expand helper: only expand if not manually collapsed
          const autoExpand = (rid) => {
            if (!manuallyCollapsedRef.current.has(rid)) {
              setExpandedIds((prev) => {
                if (prev.has(rid)) return prev;
                const next = new Set(prev);
                next.add(rid);
                return next;
              });
            }
          };

          if (type === "tokens_update") {
            setItems((prev) =>
              prev.map((it) =>
                it.request_id === request_id
                  ? { ...it, prompt_tokens: msg.prompt_tokens || 0, completion_tokens: msg.completion_tokens || 0, cache_hit: msg.cache_hit || false, total_input: msg.total_input || 0 }
                  : it
              )
            );
            return;
          }

          if (type === "replay_snapshot") {
            console.log("[COT] replay_snapshot", request_id, "thinking_rounds:", msg.rounds?.length, "text_len:", (msg.text_preview || "").length);
            setItems((prev) => {
              let [arr, idx] = ensureItem(prev);
              arr = [...arr];
              const item = { ...arr[idx] };
              // Replay snapshot is authoritative — always replace thinking content
              if (msg.rounds) {
                for (const r of msg.rounds) {
                  if (!r.thinking) continue;
                  let [rounds, ri] = ensureRound([...item.rounds], r.round_index);
                  const round = { ...rounds[ri], blocks: [...rounds[ri].blocks] };
                  const ti = round.blocks.findIndex((b) => b.block_type === "thinking");
                  if (ti >= 0) {
                    round.blocks[ti] = { ...round.blocks[ti], content: r.thinking };
                  } else {
                    round.blocks.push({ block_type: "thinking", content: r.thinking, tool_name: null });
                  }
                  rounds[ri] = round;
                  item.rounds = rounds;
                }
              }
              // Insert text_preview as a text block in the last round
              if (msg.text_preview) {
                const lastRoundIdx = item.rounds.length > 0
                  ? item.rounds[item.rounds.length - 1].round_index
                  : 0;
                let [rounds2, ri2] = ensureRound([...item.rounds], lastRoundIdx);
                const round2 = { ...rounds2[ri2], blocks: [...rounds2[ri2].blocks] };
                const existingTextIdx = round2.blocks.findIndex((b) => b.block_type === "text");
                if (existingTextIdx >= 0) {
                  round2.blocks[existingTextIdx] = { ...round2.blocks[existingTextIdx], content: msg.text_preview };
                } else {
                  round2.blocks.push({ block_type: "text", content: msg.text_preview, tool_name: null });
                }
                rounds2[ri2] = round2;
                item.rounds = rounds2;
                if (!item.preview || item.preview === "思考中...") {
                  item.preview = msg.text_preview.slice(0, 80);
                }
              }
              if (msg.injected_memories) item.injectedMemories = msg.injected_memories;
              if (msg.prompt_tokens) item.prompt_tokens = msg.prompt_tokens;
              if (msg.completion_tokens) item.completion_tokens = msg.completion_tokens;
              if (msg.cache_hit) item.cache_hit = msg.cache_hit;
              if (msg.total_input) item.total_input = msg.total_input;
              arr[idx] = item;
              return arr;
            });
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
            return;
          }

          if (type === "done") {
            setLiveRequestIds((prev) => {
              const next = new Set(prev);
              next.delete(request_id);
              return next;
            });
            manuallyCollapsedRef.current.delete(request_id);
            setItems((prev) =>
              prev.map((it) =>
                it.request_id === request_id
                  ? { ...it, prompt_tokens: msg.prompt_tokens || 0, completion_tokens: msg.completion_tokens || 0, elapsed_ms: msg.elapsed_ms || 0, cache_hit: msg.cache_hit || false, total_input: msg.total_input || 0, error: msg.error || null }
                  : it
              )
            );
            apiFetch("/api/cot/monthly-cost").then(setMonthlyCost).catch(() => {});
            apiFetch("/api/cot/trim-status").then(setTrimStatus).catch(() => {});
            return;
          }

          if (type === "injected_memories") {
            setItems((prev) => {
              let [arr, idx] = ensureItem(prev);
              arr = [...arr];
              arr[idx] = { ...arr[idx], injectedMemories: msg.memories || [] };
              return arr;
            });
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
            return;
          }

          if (type === "thinking_delta") {
            setItems((prev) => {
              let [arr, idx] = ensureItem(prev);
              arr = [...arr];
              const item = { ...arr[idx] };
              let [rounds, ri] = ensureRound([...item.rounds], msg.round_index);
              const round = { ...rounds[ri], blocks: [...rounds[ri].blocks] };
              // Find last thinking block to append to
              let ti = -1;
              for (let i = round.blocks.length - 1; i >= 0; i--) {
                if (round.blocks[i].block_type === "thinking") { ti = i; break; }
              }
              if (ti >= 0) {
                round.blocks[ti] = { ...round.blocks[ti], content: round.blocks[ti].content + msg.content };
              } else {
                round.blocks.push({ block_type: "thinking", content: msg.content, tool_name: null });
              }
              rounds[ri] = round;
              item.rounds = rounds;
              arr[idx] = item;
              return arr;
            });
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
            return;
          }

          if (type === "text_delta") {
            setItems((prev) => {
              let [arr, idx] = ensureItem(prev);
              arr = [...arr];
              const item = { ...arr[idx] };
              let [rounds, ri] = ensureRound([...item.rounds], msg.round_index);
              const round = { ...rounds[ri], blocks: [...rounds[ri].blocks] };
              // Find last text block to append to
              let ti = -1;
              for (let i = round.blocks.length - 1; i >= 0; i--) {
                if (round.blocks[i].block_type === "text") { ti = i; break; }
              }
              if (ti >= 0) {
                round.blocks[ti] = { ...round.blocks[ti], content: round.blocks[ti].content + msg.content };
              } else {
                round.blocks.push({ block_type: "text", content: msg.content, tool_name: null });
              }
              rounds[ri] = round;
              item.rounds = rounds;
              // Update card preview
              const allText = rounds.flatMap((r) => r.blocks.filter((b) => b.block_type === "text")).map((b) => b.content).join("");
              if (!item.preview || item.preview === "思考中...") {
                item.preview = allText.slice(0, 80);
              }
              arr[idx] = item;
              return arr;
            });
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
            return;
          }

          if (type === "tool_use" || type === "tool_result") {
            setItems((prev) => {
              let [arr, idx] = ensureItem(prev);
              arr = [...arr];
              const item = { ...arr[idx] };
              let [rounds, ri] = ensureRound([...item.rounds], msg.round_index);
              rounds[ri] = { ...rounds[ri], blocks: [...rounds[ri].blocks, { block_type: type, content: msg.content, tool_name: msg.tool_name }] };
              item.rounds = rounds;
              if (type === "tool_use") item.has_tool_calls = true;
              arr[idx] = item;
              return arr;
            });
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
            return;
          }

          // model_info block: set on item, don't add to rounds
          if (msg.block_type === "model_info") {
            try {
              const mi = JSON.parse(msg.content || "{}");
              setItems((prev) => {
                let [arr, idx] = ensureItem(prev);
                arr = [...arr];
                arr[idx] = { ...arr[idx], model_info: mi };
                return arr;
              });
            } catch { /* ignore */ }
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
            return;
          }

          // Backward compat: complete block types (thinking, text) from non-streaming path
          if (msg.block_type) {
            const { round_index, block_type, content, tool_name } = msg;
            setItems((prev) => {
              let [arr, idx] = ensureItem(prev);
              arr = [...arr];
              const item = { ...arr[idx] };
              let [rounds, ri] = ensureRound([...item.rounds], round_index);
              rounds[ri] = { ...rounds[ri], blocks: [...rounds[ri].blocks, { block_type, content, tool_name }] };
              item.rounds = rounds;
              if (block_type === "tool_use") item.has_tool_calls = true;
              if (block_type === "text" && (!item.preview || item.preview === "思考中...")) item.preview = content.slice(0, 80);
              arr[idx] = item;
              return arr;
            });
            setLiveRequestIds((prev) => new Set(prev).add(request_id));
            autoExpand(request_id);
          }
      };

      processWsMsg.current = handleWsMsg;

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (!apiLoadedRef.current) {
            pendingWsMsgsRef.current.push(msg);
            return;
          }
          handleWsMsg(msg);
        } catch { /* ignore malformed */ }
      };

      ws.onclose = (e) => {
        console.log("[COT WS] Disconnected, code:", e.code, "reason:", e.reason);
        setWsConnected(false);
        wsRef.current = null;
        if (!closed) reconnectTimer = setTimeout(connect, 3000);
      };

      ws.onerror = (e) => {
        console.error("[COT WS] Error:", e);
        ws.close();
      };
    };

    connect();

    return () => {
      closed = true;
      clearTimeout(reconnectTimer);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [wsToken]);

  const selectMood = (key) => {
    setMood(key);
    setMoodOpen(false);
    apiFetch("/api/settings/mood", { method: "PUT", body: { mood: key } }).catch(() => {});
  };

  return (
    <div className="flex h-full flex-col" style={{ background: S.bg }}>
      {/* Header */}
      <div
        className="relative flex shrink-0 items-center justify-between px-5 pb-3"
        style={{ paddingTop: "max(1.25rem, env(safe-area-inset-top))" }}
      >
        <button
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}
          onClick={() => navigate("/", { replace: true })}
        >
          <ChevronLeft size={22} style={{ color: S.text }} />
        </button>
        <h1 className="absolute inset-x-0 text-center text-[17px] font-bold pointer-events-none" style={{ color: S.text }}>COT 日志</h1>
        <div className="flex items-center gap-2">
          {wsConnected && (
            <div className="h-2 w-2 rounded-full" style={{ background: "#2a9d5c" }} title="实时连接" />
          )}
          <button
            className="flex h-10 w-10 items-center justify-center rounded-full"
            style={{ background: S.bg, boxShadow: loading ? "var(--inset-shadow)" : "var(--card-shadow-sm)" }}
            onClick={load}
            disabled={loading}
          >
            <RefreshCw size={16} style={{ color: S.accentDark }} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* Mood button + Pending memories button */}
      <div className="shrink-0 pb-3" style={{ paddingLeft: 20, paddingRight: 20 }}>
        <div className="flex items-stretch gap-4 justify-center">
          {/* Mood selector */}
          <div className="relative flex" ref={moodRef}>
            <button
              className="flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-[14px]"
              style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}
              onClick={() => mood && setMoodOpen(!moodOpen)}
            >
              {mood ? (
                <img
                  src={`/miniapp/assets/mood/${mood}.png`}
                  alt={mood}
                  className="h-6 w-6"
                  style={{ imageRendering: "pixelated" }}
                />
              ) : (
                <div className="h-6 w-6" />
              )}
            </button>
            {moodOpen && (
              <div
                className="absolute left-0 top-12 z-50 rounded-[16px] overflow-hidden"
                style={{
                  background: S.bg,
                  boxShadow: "var(--card-shadow-sm)",
                  width: 174,
                }}
              >
                {MOODS.map((m, i) => {
                  const row = Math.floor(i / 3);
                  const col = i % 3;
                  const selected = mood === m.key;
                  return (
                    <button
                      key={m.key}
                      className="inline-flex flex-col items-center justify-center"
                      style={{
                        width: "calc(100% / 3)",
                        padding: "8px 0 6px",
                        background: selected ? "rgba(232,160,191,0.18)" : "transparent",
                        boxShadow: [
                          col < 2 ? "inset -1px 0 0 rgba(136,136,160,0.12)" : "",
                          row < 2 ? "inset 0 -1px 0 rgba(136,136,160,0.12)" : "",
                        ].filter(Boolean).join(", ") || "none",
                      }}
                      onClick={() => selectMood(m.key)}
                    >
                      <img
                        src={`/miniapp/assets/mood/${m.key}.png`}
                        alt={m.label}
                        className="h-7 w-7"
                        style={{
                          imageRendering: "pixelated",
                          filter: selected ? "drop-shadow(0 0 3px #e8a0bf)" : "none",
                        }}
                      />
                      <span
                        className="text-[10px] mt-0.5"
                        style={{ color: selected ? "#d48aab" : S.textMuted }}
                      >
                        {m.label}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          {/* Assistant selector — segmented style */}
          <div className="relative flex" ref={assistantPickerRef}>
            <div
              className="flex items-center rounded-[14px] p-1"
              style={{ boxShadow: "var(--inset-shadow)", background: S.bg, width: 140, justifyContent: "space-between" }}
            >
              <div className="flex items-center justify-center" style={{ width: 52 }}>
                <span className="text-[11px]" style={{ color: S.textMuted, fontFamily: '"Zpix", sans-serif' }}>用户の</span>
              </div>
              <button
                className="flex items-center justify-center rounded-[10px] py-2" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)", color: S.accentDark, width: 80 }}
                onClick={() => setAssistantPickerOpen(!assistantPickerOpen)}
              >
                <span className="text-[14px] font-semibold" style={{ marginRight: 6 }}>
                  {assistants.find((a) => a.id === assistantId)?.name || "—"}
                </span>
                <span className="text-[11px]">▼</span>
              </button>
            </div>
            {assistantPickerOpen && (() => {
              const btnEl = assistantPickerRef.current?.querySelector("button");
              const parentEl = assistantPickerRef.current;
              const offsetRight = btnEl && parentEl ? parentEl.offsetWidth - btnEl.offsetLeft - btnEl.offsetWidth : 0;
              const dropWidth = btnEl ? btnEl.offsetWidth : 80;
              return (
                <div
                  className="absolute top-12 z-50 rounded-[16px] overflow-hidden py-1"
                  style={{ background: S.bg, boxShadow: "0 8px 30px rgba(0,0,0,0.18)", width: dropWidth, right: offsetRight }}
                >
                  {assistants.map((a) => {
                    const selected = a.id === assistantId;
                    return (
                      <button
                        key={a.id}
                        className="flex w-full items-center justify-center px-4 py-2.5"
                        style={{ background: selected ? "rgba(232,160,191,0.18)" : "transparent" }}
                        onClick={() => {
                          setAssistantId(a.id);
                          setAvatarUrl(avatarMap[a.id] || a.avatar_url || null);
                          setAssistantPickerOpen(false);
                        }}
                      >
                        <span className="text-[13px] font-medium" style={{ color: selected ? S.accentDark : S.text }}>{a.name}</span>
                      </button>
                    );
                  })}
                </div>
              );
            })()}
          </div>
          {/* Pending memories button */}
          <div className="relative flex">
            <button
              className="flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-[14px]"
              style={{ background: S.bg, boxShadow: "var(--inset-shadow)" }}
              onClick={() => navigate("/pending-memories")}
            >
              <img
                src="/miniapp/assets/decorations/翻盖机.png"
                alt="摘要提取记忆"
                className="h-6"
                style={{ imageRendering: "pixelated", objectFit: "contain" }}
              />
            </button>
            {pendingCount > 0 && (
              <span
                className="absolute -top-1 -right-1 flex h-4 min-w-[16px] items-center justify-center rounded-full px-1 text-[9px] font-bold text-white"
                style={{ background: "#ef4444" }}
              >
                {pendingCount > 99 ? "99+" : pendingCount}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* List */}
      <div
        className="flex-1 overflow-y-auto px-5 pb-8 pt-1 thin-scrollbar"
        ref={cotListRef}
        onScroll={(e) => setShowScrollTop(e.target.scrollTop > 300)}
      >
        {/* Monthly cost: 2 flex rows, label styled same as trim badge (muted + 600); trim on right */}
        {((monthlyCost?.by_currency?.some(c => c.total_cost > 0)) || trimStatus) && (
          <div className="flex items-center justify-center gap-3 pb-2">
            {monthlyCost?.by_currency?.some(c => c.total_cost > 0) && (
              <div className="flex flex-col gap-0.5">
                {monthlyCost.by_currency.filter(c => c.total_cost > 0).map(entry => {
                  const label = entry.currency === "CNY" ? "GLM" : "CLD";
                  const sym = entry.currency === "CNY" ? "¥" : "$";
                  const since = entry.since || monthlyCost.month;
                  return (
                    <span key={entry.currency} className="text-[10px]" style={{ color: S.textMuted }}>
                      <span style={{ fontWeight: 600, display: "inline-block", minWidth: "2.4em" }}>{label}</span>
                      {" · "}{since}起 · {entry.request_count}次 ·{" "}
                      <span style={{ color: "#2a8a5a", fontWeight: 600 }}>
                        {sym}{entry.total_cost.toFixed(2)}
                      </span>
                    </span>
                  );
                })}
              </div>
            )}
            {trimStatus && trimStatus.trigger > 0 && (
              <div className="flex flex-col gap-0.5">
                <span
                  className="rounded-full px-2 py-0.5 text-[10px] whitespace-nowrap"
                  style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}
                >
                  <span style={{ fontWeight: 600 }}>{trimStatus.dialogue_tokens.toLocaleString()}</span>
                  {" / "}
                  <span style={{ fontWeight: 600 }}>{trimStatus.trigger.toLocaleString()}</span>
                </span>
                <span
                  className="rounded-full px-2 py-0.5 text-[10px] whitespace-nowrap"
                  style={{ background: "rgba(136,136,160,0.1)", color: S.textMuted }}
                >
                  <span style={{ fontWeight: 600 }}>{Math.round(trimStatus.dialogue_tokens * 0.6).toLocaleString()}</span>
                  {" / "}
                  <span style={{ fontWeight: 600 }}>{Math.round(trimStatus.trigger * 0.6).toLocaleString()}</span>
                </span>
              </div>
            )}
          </div>
        )}
        {loading ? (
          <div className="flex justify-center py-16">
            <div className="h-8 w-8 animate-spin rounded-full border-2" style={{ borderColor: S.accent, borderTopColor: "transparent" }} />
          </div>
        ) : error ? (
          <div className="flex flex-col items-center gap-3 py-16">
            <Cpu size={36} style={{ color: "#ef4444", opacity: 0.5 }} />
            <p className="text-[14px]" style={{ color: "#ef4444" }}>{error}</p>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-16">
            <Cpu size={36} style={{ color: S.textMuted, opacity: 0.5 }} />
            <p className="text-[14px]" style={{ color: S.textMuted }}>暂无 COT 记录</p>
          </div>
        ) : (
          <>
            {items.slice(0, displayCount).map((item) => (
              <div key={item.request_id} className="mb-3 rounded-[18px]" style={{ background: S.bg, boxShadow: "var(--card-shadow-sm)" }}>
                <CotCard
                  item={item}
                  expanded={expandedIds.has(item.request_id)}
                  onToggle={() => {
                    setExpandedIds((prev) => {
                      const next = new Set(prev);
                      if (next.has(item.request_id)) {
                        next.delete(item.request_id);
                        manuallyCollapsedRef.current.add(item.request_id);
                      } else {
                        next.add(item.request_id);
                        manuallyCollapsedRef.current.delete(item.request_id);
                      }
                      return next;
                    });
                  }}
                  live={liveRequestIds.has(item.request_id)}
                  avatarUrl={avatarUrl}
                  translateCache={translateCacheRef}
                  pageAssistantId={assistantId}
                />
              </div>
            ))}
            {items.length > displayCount && (
              <button
                className="w-full rounded-[14px] py-3 text-[13px] font-medium mb-3"
                style={{ background: "rgba(136,136,160,0.08)", color: S.textMuted }}
                onClick={() => setDisplayCount((c) => c + 50)}
              >
                加载更多 ({items.length - displayCount} 条)
              </button>
            )}
          </>
        )}
      </div>

      {/* Scroll to top */}
      {showScrollTop && (
        <button
          className="fixed bottom-6 right-6 z-50 flex h-9 w-9 items-center justify-center rounded-full"
          style={{ background: "var(--bg)", boxShadow: "var(--card-shadow)" }}
          onClick={() => cotListRef.current?.scrollTo({ top: 0, behavior: "smooth" })}
        >
          <span className="text-[14px]" style={{ color: "var(--text-muted)" }}>↑</span>
        </button>
      )}

    </div>
  );
}
