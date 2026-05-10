"""Response text cleaning and API response parsing."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.memory_service import ToolCall

logger = logging.getLogger(__name__)


# ── Text cleaning ──

def extract_used_memory_ids(raw_content: str) -> list[int]:
    """Extract [[used:ID]] markers and return list of memory IDs.

    Tolerates non-standard forms: [[used:#42]], [[used:42,#43]], [[used: 42, 43]].
    """
    ids: list[int] = []
    for match in re.finditer(r'\[\[used:([^\]]+)\]\]', raw_content):
        for num in re.findall(r'\d+', match.group(1)):
            ids.append(int(num))
    return ids


def split_think_and_text(raw_content: str) -> list[tuple[str, str]]:
    """Split content into alternating (type, content) segments.

    Returns list of ("thinking", text) and ("text", text) tuples in order.
    """
    segments: list[tuple[str, str]] = []
    parts = re.split(r'(?:\[THINK\]|<scratchpad>)(.*?)(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', raw_content, flags=re.DOTALL)
    # parts alternates: text, think_content, text, think_content, text, ...
    for i, part in enumerate(parts):
        stripped = part.strip()
        if not stripped:
            continue
        if i % 2 == 0:
            segments.append(("text", stripped))
        else:
            segments.append(("thinking", stripped))
    return segments


def clean_response_text(raw_content: str, *, short_mode: bool, is_proactive: bool) -> str:
    """Strip metadata markers and format text for delivery.

    - Removes [[used:ID]], [#N], (来源: xxx), leading timestamps
    - [THINK]...[/THINK] preserved in DB for cross-request visibility (stripped at send time)
    - Long mode: collapse triple+ blank lines to double (readability).
    - Short mode: blank lines → [NEXT] so _persist_message splits into N DB rows.
    - Proactive mode: preserve blank lines verbatim.
    """
    # Extract scratchpad / legacy [THINK] blocks before processing so metadata
    # stripping / whitespace collapsing never touches their internal contents.
    _think_blocks: list[str] = []
    def _save_think(m: re.Match) -> str:
        _think_blocks.append(m.group(0))
        return f"\x00THINK{len(_think_blocks) - 1}\x00"
    content = re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', _save_think, raw_content, flags=re.DOTALL)

    content = re.sub(r'\[\[used:[^\]]+\]\]', '', content).strip()
    content = re.sub(r'\[#\s*\d+\s*\]\s*', '', content)
    content = re.sub(r'^\s*-\s*\(来源:\s*\w+\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'\(来源:\s*\w+\)\s*$', '', content, flags=re.MULTILINE)
    # Non-proactive replies should never expose routing tags. Proactive replies
    # keep them until proactive_service chooses the delivery channel.
    if not is_proactive:
        content = re.sub(r'\[?VIA[:\uff1a](telegram|qq|wechat)\]?', '', content, flags=re.IGNORECASE)
    # Strip leading timestamp if model copied the format
    content = re.sub(r'^\[\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}\]\s*', '', content)
    content = content.strip()
    if not content:
        # Might be only [THINK] blocks — restore them
        if _think_blocks:
            return "\n".join(_think_blocks)
        return ""
    if not (short_mode or is_proactive):
        # Long message mode: only collapse triple+ blank lines to double
        content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)
    elif short_mode:
        # Blank lines separate QQ/WeChat messages; convert to [NEXT] so _persist_message splits DB rows 1:1
        content = re.sub(r'\n\s*\n', '\n', content)
        content = re.sub(r'\n', '\n[NEXT]\n', content)

    # Restore [THINK] blocks
    for i, tb in enumerate(_think_blocks):
        content = content.replace(f"\x00THINK{i}\x00", tb)
    return content


def clean_tool_call_text(raw_content: str) -> str:
    """Clean assistant text that accompanies tool calls (intermediate or final)."""
    content = re.sub(r'\[\[used:[^\]]+\]\]', '', raw_content).strip()
    content = re.sub(r'\[#\s*\d+\s*\]\s*', '', content)
    content = re.sub(r'\(来源:\s*\w+\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\[\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}\]\s*', '', content)
    content = re.sub(r'\n\s*\n', '\n', content).strip()
    return content


# ── API kwargs building (shared by streaming & non-streaming) ──

def inject_anthropic_cache_breakpoint(anth_msgs: list[dict]) -> None:
    """Add cache breakpoint on second-to-last user message (mutates in place)."""
    user_msg_count = 0
    for m in reversed(anth_msgs):
        if m["role"] == "user":
            user_msg_count += 1
            if user_msg_count == 2:
                if isinstance(m["content"], str):
                    m["content"] = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
                elif isinstance(m["content"], list) and m["content"]:
                    m["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                break


# budget_tokens → effort mapping for adaptive-thinking models (opus-4-7, opus-4-6, sonnet-4-6).
# Anthropic 官方说没有精确 1:1 映射 (cli.js 17804/17823)，按数字大小递增对应五档。
_BUDGET_TO_EFFORT = {
    1024: "low",
    2048: "medium",
    4096: "high",
    8192: "xhigh",
    16384: "max",
}


def _uses_adaptive_thinking(model_name: str) -> bool:
    """Only opus-4-7 requires adaptive (returns 400 on enabled+budget_tokens).
    opus-4-6 / sonnet-4-6 stay on enabled+budget_tokens — adaptive lets the model
    skip thinking on its own judgment; enabled with a budget forces thinking."""
    return "opus-4-7" in model_name.lower()


def _uses_new_prompt_style(model_name: str) -> bool:
    """Use the newer (non-legacy) prompt variants — `prompt_long_mode` /
    `prompt_short_mode` — instead of the `*_legacy` pair.
    Opus-4-7 is the original trigger; GPT-5 family (codex) follows the
    same set since 用户 maintains a single 4.7-style prompt for both."""
    m = model_name.lower()
    return "opus-4-7" in m or "gpt-5" in m


def _rejects_sampling_params(model_name: str) -> bool:
    """opus-4-7 returns 400 if temperature/top_p/top_k are sent (cli.js 17806-17816)."""
    return "opus-4-7" in model_name.lower()


def build_anthropic_kwargs(
    model_name: str, anth_msgs: list, anth_system: list,
    anth_tools: list, *, max_tokens: int, thinking_budget: int,
    top_p: float | None, is_oauth: bool = False,
) -> dict[str, Any]:
    """Build kwargs dict for client.messages.create / .stream."""
    kwargs: dict[str, Any] = {"model": model_name, "messages": anth_msgs}
    if thinking_budget > 0:
        kwargs["max_tokens"] = max_tokens + thinking_budget
        if _uses_adaptive_thinking(model_name):
            # Pick nearest known budget for effort mapping (default to "high").
            effort = _BUDGET_TO_EFFORT.get(thinking_budget) or _BUDGET_TO_EFFORT[
                min(_BUDGET_TO_EFFORT, key=lambda k: abs(k - thinking_budget))
            ]
            # display=summarized restores 4.6-style visible thinking content
            # (default on opus-4-7 is "omitted", silent change per cli.js 17835).
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": effort}
            logger.info("[build-anth] thinking=adaptive effort=%s (budget=%d)", effort, thinking_budget)
        else:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
            logger.info("[build-anth] thinking=%s", kwargs["thinking"])
    else:
        kwargs["max_tokens"] = max_tokens
    if anth_system:
        kwargs["system"] = anth_system
    if anth_tools:
        kwargs["tools"] = anth_tools
        kwargs["tool_choice"] = {"type": "auto"}
    if top_p is not None and not _rejects_sampling_params(model_name):
        kwargs["top_p"] = top_p
    if is_oauth:
        from app.services.chat.oauth_helper import inject_billing_header
        inject_billing_header(kwargs)
    return kwargs


def build_openai_kwargs(
    model_name: str, api_messages: list, tools: list, *,
    temperature: float | None, top_p: float | None,
    thinking_budget: int, stream: bool = False,
) -> dict[str, Any]:
    """Build kwargs dict for client.chat.completions.create."""
    for msg in api_messages:
        msg.pop("_thinking_blocks", None)
    params: dict[str, Any] = {
        "model": model_name,
        "messages": api_messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    if stream:
        params["stream"] = True
        params["stream_options"] = {"include_usage": True}
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    if thinking_budget > 0:
        params["extra_body"] = {
            "reasoning": {"max_tokens": thinking_budget},
            "enable_thinking": True,
            "thinking_budget": thinking_budget,
        }
    return params


# ── Streaming helpers ──

def finalize_tool_calls_acc(tool_calls_acc: dict[int, dict]) -> tuple[list[dict], list[ToolCall]]:
    """Convert accumulated streaming tool call deltas into final payloads and ToolCall objects."""
    tool_calls_payload = []
    parsed_tool_calls = []
    for idx in sorted(tool_calls_acc.keys()):
        tc = tool_calls_acc[idx]
        tool_calls_payload.append({
            "id": tc["id"], "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments"]},
        })
        try:
            parsed_tool_calls.append(ToolCall(
                name=tc["name"],
                arguments=json.loads(tc["arguments"] or "{}"),
                id=tc["id"],
            ))
        except json.JSONDecodeError as e:
            logger.error("Failed to parse tool call arguments for %s: %s", tc["name"], e)
            parsed_tool_calls.append(ToolCall(name=tc["name"], arguments={}, id=tc["id"]))
    return tool_calls_payload, parsed_tool_calls


def extract_thinking_blocks(final_msg: Any) -> list[dict[str, Any]]:
    """Extract thinking blocks (signature only) from Anthropic response.

    The thinking text field is ignored by Anthropic (it reconstructs from
    signature), so we store only the signature to save tokens and keep the
    cached prefix stable.
    """
    blocks = []
    for b in final_msg.content:
        if b.type == "thinking":
            sig = getattr(b, "signature", None)
            if sig:
                blocks.append({"type": "thinking", "thinking": "", "signature": sig})
        elif b.type == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": b.data})
    return blocks


# ── API response parsing ──

@dataclass
class ParsedResponse:
    """Parsed result from an LLM API response."""
    text_content: str = ""
    thinking_content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_calls_payload: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    input_raw: int = 0  # total input including cache


def parse_anthropic_response(response: Any) -> ParsedResponse:
    """Parse an Anthropic messages.create() response."""
    result = ParsedResponse()

    if hasattr(response, "usage") and response.usage:
        _cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        _cc = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        _raw = getattr(response.usage, "input_tokens", 0)
        result.prompt_tokens = _raw
        result.input_raw = _raw + _cr + _cc
        result.completion_tokens = getattr(response.usage, "output_tokens", 0)

    for block in response.content:
        if block.type == "thinking":
            result.thinking_content += getattr(block, "thinking", "")
        elif block.type == "text":
            result.text_content += block.text
        elif block.type == "tool_use":
            result.tool_calls_payload.append({
                "id": block.id,
                "type": "function",
                "function": {"name": block.name, "arguments": json.dumps(block.input)},
            })
            result.tool_calls.append(ToolCall(name=block.name, arguments=block.input, id=block.id))

    return result


def parse_openai_response(response: Any) -> ParsedResponse | None:
    """Parse an OpenAI chat.completions.create() response.

    Returns None if response has no choices.
    """
    result = ParsedResponse()

    if hasattr(response, "usage") and response.usage:
        _p = getattr(response.usage, "prompt_tokens", 0) or 0
        _details = getattr(response.usage, "prompt_tokens_details", None)
        def _get_detail(key: str) -> int:
            if _details is None:
                return 0
            v = getattr(_details, key, None)
            if v is None and isinstance(_details, dict):
                v = _details.get(key)
            return int(v or 0)
        _cached = _get_detail("cached_tokens")
        _cache_write = _get_detail("cache_write_tokens")
        _p -= _cached + _cache_write
        result.prompt_tokens = _p
        result.input_raw = getattr(response.usage, "prompt_tokens", 0) or 0
        result.completion_tokens = getattr(response.usage, "completion_tokens", 0)

    if not response.choices:
        return None

    from app.services.format_converters import _extract_reasoning_from_message
    choice = response.choices[0].message
    result.thinking_content = _extract_reasoning_from_message(choice)
    result.text_content = choice.content or ""

    if getattr(choice, "tool_calls", None):
        for tc in choice.tool_calls:
            result.tool_calls_payload.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            })
            result.tool_calls.append(ToolCall(
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments or "{}"),
                id=tc.id,
            ))

    return result
