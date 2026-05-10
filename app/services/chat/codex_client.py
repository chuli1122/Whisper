"""Codex translation layer — chat.completions ↔ OpenAI Responses API.

Provides a client interface that mimics `openai.OpenAI` just enough for
`chat_service` to talk to `https://chatgpt.com/backend-api/codex/responses`
using a ChatGPT subscription OAuth access token. Translates chat.completions
requests to the Responses API on the way in, and Responses SSE events back to
chat.completions streaming chunks on the way out.

Reasoning summaries arrive via the `delta.reasoning_content` extension (same
convention as OpenRouter / DeepSeek), which the existing chat_service flow
already picks up via `_extract_reasoning_delta`.

Decision log:
- `store: false` always — we don't want OpenAI-side reasoning persistence;
  full conversation history is sent every turn.
- No `previous_response_id` — orthogonal to the no-store decision.
- `reasoning.summary = "auto"` so the server streams summary deltas that we
  can display as thinking blocks.
"""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
_USER_AGENT = "ai-companion-codex/1.0"


class CodexAPIError(Exception):
    pass


def thinking_budget_to_effort(budget: int | None) -> str | None:
    """Map Claude-style thinking_budget int to Responses reasoning.effort string.

    UI limits the codex dropdown to 0 / 1024 / 2048 / 4096, but be tolerant of
    larger values (legacy presets) — anything at or above 4096 means "high".
    """
    if not budget or budget <= 0:
        return None
    if budget <= 1024:
        return "low"
    if budget <= 2048:
        return "medium"
    return "high"


# ── Public client ────────────────────────────────────────────────────────────

class CodexClient:
    """Mimics `openai.OpenAI` for the `chat.completions.create` surface.

    Args:
        access_token: ChatGPT OAuth access_token (JWT).
        account_id: value for the `chatgpt-account-id` header.
        base_url: override the codex API root (defaults to chatgpt.com).
        timeout: per-request timeout.
        on_token_refresh: optional zero-arg callback invoked on 401; must
            return a fresh access_token (or None to give up).
    """

    def __init__(
        self,
        *,
        access_token: str,
        account_id: str,
        base_url: str | None = None,
        timeout: float | None = None,
        on_token_refresh: Callable[[], str | None] | None = None,
    ) -> None:
        self._access_token = access_token
        self._account_id = account_id
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._on_token_refresh = on_token_refresh
        self.chat = _Chat(self)


class _Chat:
    def __init__(self, client: CodexClient) -> None:
        self.completions = _Completions(client)


class _Completions:
    def __init__(self, client: CodexClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return _dispatch_create(self._client, kwargs)


# ── Dispatch ────────────────────────────────────────────────────────────────

def _dispatch_create(client: CodexClient, chat_req: dict[str, Any]) -> Any:
    if not client._access_token:
        raise CodexAPIError("codex provider has no access_token — 先去 API 设置里粘贴 auth.json 导入")
    if not client._account_id:
        raise CodexAPIError("codex provider has no chatgpt-account-id — 先导入 auth.json")

    stream = bool(chat_req.get("stream"))
    body = _translate_request(chat_req)
    body["stream"] = stream
    body["store"] = False  # hard policy

    url = f"{client._base_url}/responses"
    refreshed_once = False
    last_error: str | None = None

    # 重试策略 (参考 oauth_helper.refresh_oauth_token_sync):
    # - 网络异常 / 429 / 5xx 最多 3 次, 1s/2s backoff
    # - 401 + on_token_refresh 时单独刷新一次后重试, 不计入 retry budget
    # - 其他 4xx 立即失败 (重试无意义)
    # 流式 retry 安全: 重试只在 status != 200 时发生, 此时 body 还未开始流, 没有数据被消费
    for attempt in range(3):
        if attempt > 0:
            time.sleep(2 ** (attempt - 1))
        headers = _build_headers(client, stream)
        try:
            resp = requests.post(url, headers=headers, json=body, stream=stream, timeout=client._timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = type(exc).__name__
            logger.warning("[codex_client] attempt %d network error: %s", attempt + 1, exc)
            continue

        if resp.status_code == 401 and client._on_token_refresh and not refreshed_once:
            refreshed_once = True
            saved = resp.text[:500] if not stream else "(streaming)"
            try:
                resp.close()
            except Exception:
                pass
            new_token = client._on_token_refresh()
            if not new_token or new_token == client._access_token:
                raise CodexAPIError(f"codex responses API 401 (token refresh failed): {saved}")
            client._access_token = new_token
            headers = _build_headers(client, stream)
            try:
                resp = requests.post(url, headers=headers, json=body, stream=stream, timeout=client._timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = type(exc).__name__
                logger.warning("[codex_client] post-401-refresh network error: %s", exc)
                continue

        if resp.status_code == 200:
            if stream:
                return _stream_chunks(resp)
            return _non_stream_completion(resp.json())

        if resp.status_code in (429, 502, 503, 504):
            body_preview = resp.text[:200] if not stream else "(streaming)"
            try:
                resp.close()
            except Exception:
                pass
            last_error = f"HTTP {resp.status_code}: {body_preview}"
            logger.warning("[codex_client] attempt %d retryable: %s", attempt + 1, last_error)
            continue

        # 其他 4xx / 异常 status,立即失败
        body_preview = resp.text[:500] if not stream else "(streaming)"
        try:
            resp.close()
        except Exception:
            pass
        raise CodexAPIError(f"codex responses API {resp.status_code}: {body_preview}")

    raise CodexAPIError(f"codex responses API gave up after 3 attempts (last: {last_error})")


def _build_headers(client: CodexClient, stream: bool) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {client._access_token}",
        "chatgpt-account-id": client._account_id,
        "OpenAI-Beta": "responses=experimental",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": _USER_AGENT,
    }


# ── Request translation: chat.completions → Responses ──────────────────────

def _translate_request(chat_req: dict[str, Any]) -> dict[str, Any]:
    """Map chat.completions params to Responses API schema."""
    out: dict[str, Any] = {"model": chat_req["model"]}

    messages = chat_req.get("messages") or []
    instructions, input_items = _translate_messages(messages)
    if instructions:
        out["instructions"] = instructions
    out["input"] = input_items

    tools = chat_req.get("tools")
    if tools:
        out["tools"] = _translate_tools(tools)
        tc = chat_req.get("tool_choice")
        if tc is not None:
            out["tool_choice"] = _translate_tool_choice(tc)
    # else: omit tool_choice entirely — Responses API rejects tool_choice with no tools
    if chat_req.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = chat_req["parallel_tool_calls"]

    reasoning = chat_req.get("reasoning")
    if reasoning:
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        if effort:
            out["reasoning"] = {"effort": effort, "summary": reasoning.get("summary") or "auto"}

    for key in ("temperature", "top_p", "max_output_tokens"):
        if chat_req.get(key) is not None:
            out[key] = chat_req[key]
    if "max_output_tokens" not in out and chat_req.get("max_tokens") is not None:
        out["max_output_tokens"] = chat_req["max_tokens"]

    return out


def _translate_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Head-of-list 'system' messages become `instructions`; rest become `input` items."""
    instructions_parts: list[str] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        text = _flatten_text(messages[i].get("content"))
        if text:
            instructions_parts.append(text)
        i += 1
    instructions = "\n\n".join(instructions_parts)

    input_items: list[dict[str, Any]] = []
    for msg in messages[i:]:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            # System message in middle of conversation — fold into a user note.
            text = _flatten_text(content)
            if text:
                input_items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"[system] {text}"}],
                })
            continue

        if role == "user":
            input_items.append({
                "type": "message",
                "role": "user",
                "content": _content_to_input_parts(content),
            })
            continue

        if role == "assistant":
            text = _flatten_text(content)
            if text:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments") or "",
                })
            continue

        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id"),
                "output": _flatten_text(content),
            })
            continue

        logger.warning("[codex_client] unknown message role=%r, skipping", role)

    return instructions, input_items


def _content_to_input_parts(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return [{"type": "input_text", "text": ""}]
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    parts.append({"type": "input_text", "text": item})
                continue
            itype = item.get("type")
            if itype in ("text", "input_text"):
                parts.append({"type": "input_text", "text": item.get("text", "")})
            elif itype == "image_url":
                img = item.get("image_url")
                url = img.get("url", "") if isinstance(img, dict) else (img if isinstance(img, str) else "")
                parts.append({"type": "input_image", "image_url": url})
            elif itype == "input_image":
                parts.append(item)
            else:
                parts.append({"type": "input_text", "text": json.dumps(item, ensure_ascii=False)})
        return parts or [{"type": "input_text", "text": ""}]
    return [{"type": "input_text", "text": str(content)}]


def _flatten_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(item, str):
                out.append(item)
        return "".join(out)
    return str(content)


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function") or {}
            entry: dict[str, Any] = {
                "type": "function",
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            }
            if fn.get("strict"):
                entry["strict"] = True
            out.append(entry)
        else:
            out.append(t)
    return out


def _translate_tool_choice(tc: Any) -> Any:
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict) and tc.get("type") == "function":
        fn = tc.get("function") or {}
        return {"type": "function", "name": fn.get("name")}
    return tc


# ── Response translation: SSE → chat.completions chunks ────────────────────

def _stream_chunks(resp: requests.Response) -> Iterator[Any]:
    state = _StreamState()
    try:
        for event_type, data in _iter_sse_events(resp):
            chunk = state.on_event(event_type, data)
            if chunk is not None:
                yield chunk
        final = state.finalize()
        if final is not None:
            yield final
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _iter_sse_events(resp: requests.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event_name = ""
    data_lines: list[str] = []
    for raw in resp.iter_lines(decode_unicode=False):
        if raw is None:
            continue
        if raw == b"" or raw == b"\r":
            # End of event
            if data_lines:
                raw_data = "\n".join(data_lines)
                data_lines = []
                if raw_data.strip() == "[DONE]":
                    event_name = ""
                    continue
                try:
                    payload = json.loads(raw_data)
                except json.JSONDecodeError:
                    logger.warning("[codex_client] non-JSON SSE data: %r", raw_data[:200])
                    event_name = ""
                    continue
                etype = event_name or (payload.get("type") if isinstance(payload, dict) else "")
                yield etype, payload if isinstance(payload, dict) else {}
            event_name = ""
            continue
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())


class _StreamState:
    """Accumulates Responses events, emits chat.completions chunks."""

    def __init__(self) -> None:
        self._tool_calls: dict[int, dict[str, Any]] = {}  # output_index → {id, name, arguments}
        self._usage: dict[str, Any] | None = None
        self._finish_reason: str | None = None
        self._final_emitted = False

    def on_event(self, etype: str, data: dict[str, Any]) -> Any | None:
        if etype == "response.output_text.delta":
            delta = data.get("delta") or ""
            return _content_chunk(delta) if delta else None

        if etype == "response.reasoning_summary_text.delta":
            delta = data.get("delta") or ""
            return _reasoning_chunk(delta) if delta else None

        if etype == "response.output_item.added":
            item = data.get("item") or {}
            idx = data.get("output_index")
            if idx is None:
                return None
            if item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id") or ""
                name = item.get("name") or ""
                self._tool_calls[idx] = {
                    "id": call_id,
                    "name": name,
                    "arguments": item.get("arguments") or "",
                }
                return _tool_call_chunk(idx, call_id or None, name or None, "")
            return None

        if etype == "response.function_call_arguments.delta":
            idx = data.get("output_index")
            if idx is None:
                return None
            entry = self._tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            delta = data.get("delta") or ""
            entry["arguments"] += delta
            return _tool_call_chunk(idx, None, None, delta) if delta else None

        if etype == "response.output_item.done":
            item = data.get("item") or {}
            idx = data.get("output_index")
            if idx is None or item.get("type") != "function_call":
                return None
            entry = self._tool_calls.get(idx)
            if entry is None:
                return None
            full = item.get("arguments") or ""
            streamed = entry.get("arguments") or ""
            if full and full != streamed:
                remaining = full[len(streamed):] if full.startswith(streamed) else full
                entry["arguments"] = full
                if remaining:
                    return _tool_call_chunk(idx, None, None, remaining)
            return None

        if etype == "response.completed":
            response_obj = data.get("response") or {}
            self._usage = response_obj.get("usage")
            outputs = response_obj.get("output") or []
            has_fn = any(
                isinstance(o, dict) and o.get("type") == "function_call" for o in outputs
            )
            self._finish_reason = "tool_calls" if has_fn else "stop"
            return None

        if etype in ("response.failed", "error"):
            err = data.get("error") if isinstance(data, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err or data)
            raise CodexAPIError(f"codex responses stream failed: {msg}")

        return None

    def finalize(self) -> Any | None:
        if self._final_emitted:
            return None
        self._final_emitted = True
        usage_obj = _translate_usage(self._usage) if self._usage else None
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    index=0,
                    delta=SimpleNamespace(content=None, tool_calls=None, reasoning_content=None),
                    finish_reason=self._finish_reason or "stop",
                )
            ],
            usage=usage_obj,
        )


def _content_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(content=text, tool_calls=None, reasoning_content=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _reasoning_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(content=None, tool_calls=None, reasoning_content=text),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _tool_call_chunk(idx: int, tool_call_id: str | None, name: str | None, arguments_delta: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=idx,
                            id=tool_call_id,
                            type="function",
                            function=SimpleNamespace(name=name, arguments=arguments_delta),
                        )
                    ],
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _translate_usage(responses_usage: dict[str, Any] | None) -> Any:
    if not responses_usage:
        return None
    input_tokens = int(responses_usage.get("input_tokens") or 0)
    output_tokens = int(responses_usage.get("output_tokens") or 0)
    total = int(responses_usage.get("total_tokens") or (input_tokens + output_tokens))
    details = responses_usage.get("input_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=cached,
            cache_write_tokens=0,
        ),
    )


def _non_stream_completion(data: dict[str, Any]) -> Any:
    """Build a ChatCompletion-like object from a non-streaming Responses response."""
    output = data.get("output") or []
    content_text = ""
    reasoning_text = ""
    tool_calls: list[Any] = []
    for idx, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    content_text += part.get("text", "")
        elif itype == "reasoning":
            for part in item.get("summary") or []:
                if isinstance(part, dict):
                    reasoning_text += part.get("text", "")
        elif itype == "function_call":
            tool_calls.append(
                SimpleNamespace(
                    id=item.get("call_id") or item.get("id"),
                    type="function",
                    function=SimpleNamespace(
                        name=item.get("name"),
                        arguments=item.get("arguments") or "",
                    ),
                    index=idx,
                )
            )

    finish_reason = "tool_calls" if tool_calls else "stop"
    message = SimpleNamespace(
        role="assistant",
        content=content_text or None,
        tool_calls=tool_calls or None,
        reasoning_content=reasoning_text or None,
    )
    return SimpleNamespace(
        id=data.get("id"),
        model=data.get("model"),
        choices=[SimpleNamespace(index=0, message=message, finish_reason=finish_reason)],
        usage=_translate_usage(data.get("usage")),
    )
