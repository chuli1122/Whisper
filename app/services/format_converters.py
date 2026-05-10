"""Anthropic / OpenAI format converter utilities.

Extracted from chat_service.py — pure functions that convert between
OpenAI-style and Anthropic-style message/tool schemas.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Estimate token count: CJK ≈ 1.5 tok, other ≈ 0.25 tok."""
    if not text:
        return 0
    cjk_count = 0
    other_count = 0
    for char in text:
        codepoint = ord(char)
        if 0x4E00 <= codepoint <= 0x9FFF:
            cjk_count += 1
        else:
            other_count += 1
    quarter_tokens = cjk_count * 6 + other_count
    return (quarter_tokens + 3) // 4


# ── Constants ──

_CACHE_BREAK = "\n\n<!-- CACHE_BREAK -->\n\n"

# ── Tool definitions ──

def _oai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI-format tool definitions to Anthropic format."""
    result = []
    for tool in tools:
        if tool.get("type") == "function":
            fn = tool["function"]
            result.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
    return result

# ── Reasoning extraction ──

def _extract_reasoning_delta(delta: Any) -> str:
    """Extract reasoning text from an OpenAI-format streaming delta (OpenRouter)."""
    # Try simple string fields first
    for attr in ("reasoning", "reasoning_content"):
        val = getattr(delta, attr, None)
        if val and isinstance(val, str):
            return val
    # Try reasoning_details array (OpenRouter standard)
    details = getattr(delta, "reasoning_details", None)
    if details and isinstance(details, list):
        parts = []
        for item in details:
            text = getattr(item, "text", None) or getattr(item, "summary", None)
            if text:
                parts.append(text)
        if parts:
            return "".join(parts)
    return ""

def _extract_reasoning_from_message(message: Any) -> str:
    """Extract reasoning text from an OpenAI-format message (OpenRouter non-streaming)."""
    # Try simple string fields first
    for attr in ("reasoning", "reasoning_content"):
        val = getattr(message, attr, None)
        if val and isinstance(val, str):
            return val
    # Try reasoning_details array (OpenRouter standard)
    details = getattr(message, "reasoning_details", None)
    if details and isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, str):
                parts.append(item)
            else:
                text = getattr(item, "text", None) or getattr(item, "summary", None)
                if text:
                    parts.append(text)
        if parts:
            return "".join(parts)
    return ""

# ── Tool result cache ──

_TOOL_CACHE_THRESHOLD = 500  # 超过此字数的工具结果进缓存区（索引替换）
_ALWAYS_CACHE_TOOLS: set[str] = set()  # 不看阈值直接进缓存的工具（当前无）

def _build_tool_index(tool_name: str, result_json: str, content_len: int) -> str:
    """为单条工具结果生成索引摘要。"""
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return f"{tool_name} — {content_len}字"

    parts: list[str] = []

    # web_fetch: title + url
    if "title" in data and "url" in data:
        parts.append(f"\"{data['title']}\" | {data['url']}")
    # search-type: query + result count
    elif "query" in data:
        q = data["query"]
        results = data.get("results", [])
        parts.append(f"查询: \"{q}\" | {len(results)}条结果")
    # get_summary_by_id: just id
    elif "summary_content" in data and "id" in data:
        parts.append(f"#{data['id']}")

    info = f"（{parts[0]}）" if parts else ""
    return f"{tool_name}{info} — {content_len}字"


_TOOL_CACHE_MAX_TOKENS = 30000


def extract_tool_cache(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """将历史轮次的工具结果搬到缓存区，原位替换为索引。

    - 有 id (msg_id) 的 tool 消息 = 已持久化的历史结果
    - 已压缩的（不以 { 开头）留原位
    - _ALWAYS_CACHE_TOOLS 中的工具不看阈值，直接进缓存
    - 其他原始 JSON 超过 _TOOL_CACHE_THRESHOLD 字才进缓存
    - _cache_key 去重：同一 key 只保留最新的缓存条目（如 forum:read:xxx）
    - 超过 _TOOL_CACHE_MAX_TOKENS 时从最早的开始清理
    - 返回 (缓存区文本, 处理后的messages)
    """
    # First pass: parse JSON once, detect _cache_key duplicates, cache parsed results
    cache_key_last_msg_id: dict[str, int] = {}  # _cache_key → last msg_id with that key
    _parsed_cache: dict[int, tuple[dict, str | None]] = {}  # msg_id → (parsed_data, cache_key)

    for msg in messages:
        if msg.get("role") != "tool" or not isinstance(msg.get("id"), int):
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.startswith("{"):
            continue
        try:
            data = json.loads(content)
            ck = data.get("_cache_key")
            _parsed_cache[msg["id"]] = (data, ck)
            if ck:
                cache_key_last_msg_id[ck] = msg["id"]
        except (json.JSONDecodeError, TypeError):
            pass

    # Second pass: build cache entries and index replacements
    # Track (cache_entry_text, result_msg_index, tool_name, index_text) for each cached item
    cache_items: list[tuple[str, int, str, str]] = []  # (entry_text, result_idx, tool_name, index_text)
    result_messages: list[dict[str, Any]] = []
    first_index = True

    for msg in messages:
        tool_name = msg.get("name") or (msg.get("meta_info") or {}).get("tool_name", "unknown")
        is_raw_json = (
            isinstance(msg.get("content"), str)
            and msg["content"].startswith("{")
        )
        if (
            msg.get("role") == "tool"
            and isinstance(msg.get("id"), int)
            and is_raw_json
            and tool_name not in ("switch_channel", "cafe_chat")
            and (len(msg["content"]) > _TOOL_CACHE_THRESHOLD or tool_name in _ALWAYS_CACHE_TOOLS)
        ):
            msg_id = msg["id"]
            content = msg["content"]
            content_len = len(content)

            # Use pre-parsed data from first pass
            _parsed = _parsed_cache.get(msg_id)
            _data, _ck = _parsed if _parsed else (None, None)

            if _ck and cache_key_last_msg_id.get(_ck) != msg_id:
                # Older duplicate — drop from cache, replace with short note
                index_line = f"[已更新] {tool_name} — 见最新版本"
                replaced = {**msg, "content": index_line}
                result_messages.append(replaced)
                continue

            # Strip _cache_key from cached content (model doesn't need to see it)
            if _ck and _data:
                _data.pop("_cache_key", None)
                content = json.dumps(_data, ensure_ascii=False)
                content_len = len(content)

            # 缓存区条目
            entry_text = f"[ref:msg{msg_id}] {tool_name}\n{content}"

            # 索引替换
            index_text = _build_tool_index(tool_name, content, content_len)
            index_line = f"[ref:msg{msg_id}] {index_text}"
            if first_index:
                index_line = f"标记为[ref:msgN]的工具结果为索引，完整内容见上方[工具结果缓存]区域。\n{index_line}"
                first_index = False
            replaced = {**msg, "content": index_line}
            result_messages.append(replaced)

            cache_items.append((entry_text, len(result_messages) - 1, tool_name, index_text))
        else:
            result_messages.append(msg)

    if not cache_items:
        return "", result_messages

    # Enforce size limit: drop oldest entries until total <= _TOOL_CACHE_MAX_TOKENS
    entry_tokens = [estimate_tokens(entry) for entry, _, _, _ in cache_items]
    total_tokens = sum(entry_tokens)
    drop_count = 0
    while total_tokens > _TOOL_CACHE_MAX_TOKENS and drop_count < len(cache_items):
        total_tokens -= entry_tokens[drop_count]
        drop_count += 1

    # Update dropped entries: replace index with short cleaned-up note
    for i in range(drop_count):
        _, result_idx, t_name, idx_text = cache_items[i]
        # Remove [ref:msgN] prefix from index_text, add [已清理]
        clean_line = f"{idx_text} [已清理]"
        result_messages[result_idx] = {**result_messages[result_idx], "content": clean_line}

    # If the first cached entry was dropped, re-add preamble to first surviving entry
    if drop_count > 0 and drop_count < len(cache_items):
        _, surv_idx, _, _ = cache_items[drop_count]
        surv_content = result_messages[surv_idx]["content"]
        if not surv_content.startswith("标记为"):
            result_messages[surv_idx] = {
                **result_messages[surv_idx],
                "content": f"标记为[ref:msgN]的工具结果为索引，完整内容见上方[工具结果缓存]区域。\n{surv_content}",
            }

    # Build final cache text from surviving entries
    surviving = [entry for entry, _, _, _ in cache_items[drop_count:]]
    if not surviving:
        return "", result_messages
    cache_text = "[工具结果缓存]\n\n" + "\n\n".join(surviving)
    return cache_text, result_messages


# ── Cache control ──

def _apply_cache_control_oai(
    api_messages: list[dict[str, Any]],
    *,
    use_blocks: bool = False,
    skip_message_bp: bool = False,
) -> None:
    """Handle cache markers for OpenAI/OpenRouter providers.

    use_blocks=True  -> convert system _system_blocks to content blocks with cache_control,
                        honor _cache_bp markers on user messages (e.g. tool_cache_user),
                        AND inject a message-level cache breakpoint on the second-to-last
                        user message (OpenRouter Anthropic — matches anthropic path).
    use_blocks=False -> just strip internal markers, keep plain strings (safe for all providers).
    skip_message_bp  -> do NOT auto-inject the message-level breakpoint (caller will
                        place it explicitly at a pinned index). System / tool_cache_user
                        cache_control still gets applied.
    """
    for msg in api_messages:
        # Backward compat: strip any remaining CACHE_BREAK sentinel
        if msg.get("role") == "system" and isinstance(msg.get("content"), str) and _CACHE_BREAK in msg["content"]:
            msg["content"] = msg["content"].replace(_CACHE_BREAK, "\n\n").strip()
        # Handle _system_blocks for multi-block system prompt
        _blocks = msg.pop("_system_blocks", None)
        if msg.get("role") == "system" and _blocks:
            if use_blocks:
                # OpenRouter Anthropic: convert to content blocks with cache_control
                content_blocks = []
                for blk in _blocks:
                    entry: dict[str, Any] = {"type": "text", "text": blk["text"]}
                    if blk.get("_cache_bp"):
                        entry["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                    content_blocks.append(entry)
                msg["content"] = content_blocks
            else:
                # Plain text: concatenate all blocks
                msg["content"] = "\n\n".join(blk["text"] for blk in _blocks)
        elif use_blocks and msg.get("role") == "system" and isinstance(msg.get("content"), str):
            msg["content"] = [
                {"type": "text", "text": msg["content"]},
            ]
        # Honor _cache_bp on user messages (tool_cache_user etc.) — mirrors the
        # anthropic path's _oai_messages_to_anthropic handler. Without this, the
        # OAI path has no stable message-level cache checkpoint and cache_read
        # collapses to system-only as soon as breakpoint③ (tool_cache) exists.
        if use_blocks and msg.pop("_cache_bp", False) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }]
            elif isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    last["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        else:
            # Strip any remaining _cache_bp markers (plain OpenAI path, etc.)
            msg.pop("_cache_bp", None)

    # OpenRouter Anthropic: inject message-level cache breakpoint on the
    # second-to-last user so the current turn's history also gets cached for
    # the next turn. Used alongside the tool_cache_user breakpoint above.
    # Skipped when the caller pins the bp index explicitly (round 1+).
    if use_blocks and not skip_message_bp:
        _inject_oai_message_cache_bp(api_messages)


def _reapply_oai_message_bp_at_idx(api_messages: list[dict[str, Any]], idx: int) -> None:
    """Apply a message-level cache breakpoint at the given absolute index.

    Used in round 1+ of a streaming request to pin the message breakpoint to
    the exact index it occupied in round 0, mirroring the anthropic path's
    `self._stream_cache_bp_idx` mechanism. This prevents cache_control from
    drifting (which is part of the Anthropic cache key).
    """
    if idx is None or idx < 0 or idx >= len(api_messages):
        return
    m = api_messages[idx]
    if m.get("role") != "user":
        return
    content = m.get("content")
    if isinstance(content, str):
        m["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = {"type": "ephemeral", "ttl": "1h"}


def _inject_oai_message_cache_bp(api_messages: list[dict[str, Any]]) -> None:
    """Add cache breakpoint on the second-to-last user message (mutates in place).

    Mirrors inject_anthropic_cache_breakpoint but in OpenAI message format.
    OpenRouter accepts cache_control inside OpenAI content blocks and forwards
    it to upstream Anthropic providers (anthropic / google-vertex / bedrock).
    """
    user_msg_count = 0
    for m in reversed(api_messages):
        if m.get("role") != "user":
            continue
        user_msg_count += 1
        if user_msg_count != 2:
            continue
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        break

# ── Message conversion ──

def _oai_messages_to_anthropic(api_messages: list[dict[str, Any]]) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract system prompt and convert messages to Anthropic format.

    OpenAI roles handled:
    - system (first)  -> system= parameter (multi-block with cache_control)
    - system (others) -> converted to user messages
    - user            -> user (with multimodal support)
    - assistant       -> assistant (with tool_use blocks if tool_calls present)
    - tool            -> user with tool_result blocks (consecutive merged)
    """
    system_blocks: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    first_system_seen = False

    for msg in api_messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if not first_system_seen:
                first_system_seen = True
                # Multi-block system prompt from _system_blocks
                _blocks = msg.get("_system_blocks")
                if _blocks:
                    for blk in _blocks:
                        entry: dict[str, Any] = {"type": "text", "text": blk["text"]}
                        if blk.get("_cache_bp"):
                            entry["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                        system_blocks.append(entry)
                elif content:
                    system_blocks.append({"type": "text", "text": content})
            else:
                raw.append({"role": "user", "content": content or ""})
            continue

        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content or "",
            }
            # Merge consecutive tool results into one user message
            if (raw and raw[-1]["role"] == "user"
                    and isinstance(raw[-1]["content"], list)
                    and raw[-1]["content"]
                    and isinstance(raw[-1]["content"][0], dict)
                    and raw[-1]["content"][0].get("type") == "tool_result"):
                raw[-1]["content"].append(block)
            else:
                raw.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            oai_tool_calls = msg.get("tool_calls")
            if oai_tool_calls:
                blocks: list[dict[str, Any]] = []
                # Preserve thinking blocks from previous rounds (Anthropic extended thinking)
                _tbs = msg.get("_thinking_blocks", [])
                if _tbs:
                    logger.info("[thinking-pass] Passing %d thinking blocks to round", len(_tbs))
                for tb in _tbs:
                    blocks.append(tb)
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in oai_tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                raw.append({"role": "assistant", "content": blocks})
            else:
                _tbs = msg.get("_thinking_blocks", [])
                if _tbs:
                    logger.info("[thinking-pass] Passing %d thinking blocks to final assistant msg", len(_tbs))
                    blocks = list(_tbs)
                    if content:
                        blocks.append({"type": "text", "text": content})
                    raw.append({"role": "assistant", "content": blocks})
                else:
                    raw.append({"role": "assistant", "content": content or ""})
            continue

        if role == "user":
            _is_cache_bp = msg.get("_cache_bp", False)
            if isinstance(content, list):
                anth_parts: list[dict[str, Any]] = []
                for part in content:
                    ptype = part.get("type")
                    if ptype == "text":
                        anth_parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            try:
                                meta, data = url.split(",", 1)
                                media_type = meta.split(":")[1].split(";")[0]
                                anth_parts.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": media_type, "data": data},
                                })
                            except Exception:
                                pass
                _entry = {"role": "user", "content": anth_parts}
                if _is_cache_bp:
                    _entry["_cache_bp"] = True
                raw.append(_entry)
            else:
                _entry = {"role": "user", "content": content or ""}
                if _is_cache_bp:
                    _entry["_cache_bp"] = True
                raw.append(_entry)
            continue

    # Merge consecutive same-role messages.
    # - user: converted system notifications adjacent to real user messages (join with \n)
    # - assistant: short-mode DB stores one row per segment; rejoin with [NEXT] so
    #   the model sees a single turn with its own segmentation token
    # Note: don't merge cache-breakpoint messages (they must stay separate)
    messages: list[dict[str, Any]] = []
    for msg in raw:
        if (messages and messages[-1]["role"] == msg["role"] == "user"
                and isinstance(messages[-1]["content"], str)
                and isinstance(msg["content"], str)
                and not messages[-1].get("_cache_bp")
                and not msg.get("_cache_bp")):
            messages[-1]["content"] = messages[-1]["content"] + "\n" + msg["content"]
        elif (messages and messages[-1]["role"] == msg["role"] == "assistant"
                and isinstance(messages[-1].get("content"), str)
                and isinstance(msg.get("content"), str)):
            messages[-1]["content"] = messages[-1]["content"] + "\n[NEXT]\n" + msg["content"]
        elif (messages and messages[-1]["role"] == msg["role"] == "assistant"
                and isinstance(messages[-1].get("content"), list)
                and isinstance(msg.get("content"), str)):
            prev = messages[-1]["content"]
            merged = False
            for block in reversed(prev):
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] += "\n[NEXT]\n" + msg["content"]
                    merged = True
                    break
            if not merged:
                prev.append({"type": "text", "text": msg["content"]})
        else:
            messages.append(dict(msg))

    # Validate tool_result blocks -- Anthropic requires each tool_result to have
    # a matching tool_use in the IMMEDIATELY PRECEDING assistant message.
    _cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            # Find the preceding assistant message's tool_use IDs
            _prev_tool_use_ids: set[str] = set()
            for prev in reversed(_cleaned):
                if prev["role"] == "assistant":
                    if isinstance(prev.get("content"), list):
                        _prev_tool_use_ids = {
                            b["id"] for b in prev["content"]
                            if isinstance(b, dict) and b.get("type") == "tool_use"
                        }
                    break
            filtered_blocks = [
                b for b in msg["content"]
                if not (isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") not in _prev_tool_use_ids)
            ]
            if not filtered_blocks:
                continue  # drop empty user message (all tool_results were orphaned)
            msg = {**msg, "content": filtered_blocks}
        _cleaned.append(msg)
    messages = _cleaned

    # Anthropic requires last message to be from user (e.g. receive-mode has no trailing user msg)
    if messages and messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "[系统占位：她未发送新消息，请继续你要做的事]"})

    # Apply cache_control on breakpoint messages (convert string content to blocks)
    for msg in messages:
        if msg.pop("_cache_bp", False) and msg["role"] == "user":
            content = msg["content"]
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
            elif isinstance(content, list) and content:
                content[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}

    # System blocks already built with cache_control from _system_blocks
    if not system_blocks:
        system_blocks = [{"type": "text", "text": ""}]
    return system_blocks, messages
