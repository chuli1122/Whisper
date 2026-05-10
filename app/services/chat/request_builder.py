from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import anthropic
from openai import OpenAI
from sqlalchemy import func, text

from app.database import SessionLocal
from app.models.models import (
    ApiProvider, Assistant, ChatSession, ModelPreset, SessionSummary, Settings,
    SummaryLayer, UserProfile,
)
from app.services.chat.config_helpers import (
    get_prompt_setting as _get_prompt_setting,
    normalize_anthropic_base_url as _normalize_anthropic_base_url,
)
from app.services.chat.prompt_defaults import (
    DEFAULT_IMPORTANT_NOTICE,
    DEFAULT_LONG_MODE,
    DEFAULT_LONG_MODE_LEGACY,
    DEFAULT_LONG_MODE_SUFFIX,
    DEFAULT_SHORT_MODE,
    DEFAULT_SHORT_MODE_LEGACY,
    DEFAULT_SHORT_MODE_SUFFIX,
)
from app.services.chat.response_cleaner import _uses_new_prompt_style
from app.services.chat.tool_definitions import build_tools as _build_tools
from app.services.core_blocks_service import CoreBlocksService
from app.services.format_converters import extract_tool_cache
from app.services.summary_service import SummaryService
from app.services.world_books_service import WorldBooksService
from app.routers.memes import get_memes_for_injection
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)


def build_api_call_params(
    service: Any,
    messages: list[dict[str, Any]],
    session_id: int,
    *,
    short_mode: bool = False,
    source: str | None = None,
) -> tuple | None:
    """Build all params needed for an API call.

    Returns (client, model_name, api_messages, tools) or None.
    Side effects: updates service._trimmed_messages and service._trimmed_message_ids.
    """
    self = service
    self._trimmed_messages = []
    self._trimmed_message_ids = []
    user_profile = self.db.query(UserProfile).first()
    user_info = user_profile.basic_info if user_profile else ""
    user_nickname = (user_profile.nickname if user_profile and user_profile.nickname else "她")
    session = self.db.get(ChatSession, session_id)
    if session and session.assistant_id:
        assistant = self.db.get(Assistant, session.assistant_id)
    else:
        assistant = self.db.query(Assistant).first()
    if not assistant:
        return None
    self._current_assistant_id = assistant.id
    self._current_session_id = session_id
    self._current_preset_name = ""
    model_preset = self.db.get(ModelPreset, assistant.model_preset_id)
    if not model_preset:
        return None
    self._current_preset_name = model_preset.name or ""
    api_provider = self.db.get(ApiProvider, model_preset.api_provider_id)
    if not api_provider:
        return None
    raw_latest = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"),
        None,
    )
    latest_user_message = self._content_to_storage(raw_latest) if isinstance(raw_latest, list) else raw_latest
    base_system_prompt = assistant.system_prompt

    # ── Three-layer summary selection ──

    # Recent layer: only unmerged, non-deleted originals
    summaries_desc = (
        self.db.query(SessionSummary)
        .filter(
            SessionSummary.assistant_id == assistant.id,
            SessionSummary.deleted_at.is_(None),
            SessionSummary.merged_into.is_(None),
            SessionSummary.msg_id_start.isnot(None),
        )
        .order_by(SessionSummary.created_at.desc())
        .all()
    )
    budget_recent = self.summary_budget_recent
    used_summary_tokens = 0
    selected_summaries_desc: list[SessionSummary] = []
    overflow_summaries: list[SessionSummary] = []
    latest_mood_tag = None
    _overflowing = False
    for summary in summaries_desc:
        if latest_mood_tag is None and summary.mood_tag:
            latest_mood_tag = summary.mood_tag
        summary_content = (summary.summary_content or "").strip()
        if not summary_content:
            continue
        summary_tokens = self._estimate_tokens(summary_content)
        if not _overflowing and used_summary_tokens + summary_tokens <= budget_recent:
            selected_summaries_desc.append(summary)
            used_summary_tokens += summary_tokens
        else:
            _overflowing = True
            overflow_summaries.append(summary)
    # Also check all summaries (including merged) for mood if not found yet
    if latest_mood_tag is None:
        mood_row = (
            self.db.query(SessionSummary)
            .filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.mood_tag.isnot(None),
                SessionSummary.deleted_at.is_(None),
            )
            .order_by(SessionSummary.created_at.desc())
            .first()
        )
        if mood_row:
            latest_mood_tag = mood_row.mood_tag

    # Handle overflow: mark first, then merge async
    logger.info("[summary-overflow] selected=%d (tokens=%d/%d), overflow=%d, ids_selected=%s, ids_overflow=%s",
                len(selected_summaries_desc), used_summary_tokens, budget_recent,
                len(overflow_summaries),
                [s.id for s in selected_summaries_desc],
                [s.id for s in overflow_summaries])
    if overflow_summaries:
        summary_svc = SummaryService(SessionLocal)
        for s in overflow_summaries:
            s.merged_into = "daily"
        summary_svc.ensure_layer_needs_merge(self.db, assistant.id, "daily")
        self.db.commit()
        summary_svc.merge_layers_async(assistant.id, ("daily",))

    # Read layer content
    longterm_row = (
        self.db.query(SummaryLayer)
        .filter(SummaryLayer.assistant_id == assistant.id, SummaryLayer.layer_type == "longterm")
        .first()
    )
    daily_row = (
        self.db.query(SummaryLayer)
        .filter(SummaryLayer.assistant_id == assistant.id, SummaryLayer.layer_type == "daily")
        .first()
    )
    _longterm_text = longterm_row.content.strip() if longterm_row and longterm_row.content else ""
    _daily_text = daily_row.content.strip() if daily_row and daily_row.content else ""

    # Query msg_id ranges for daily/longterm layers
    _layer_msg_ranges: dict[str, tuple[int | None, int | None]] = {}
    for _lt in ("daily", "longterm"):
        _range = self.db.query(
            func.min(SessionSummary.msg_id_start),
            func.max(SessionSummary.msg_id_end),
        ).filter(
            SessionSummary.assistant_id == assistant.id,
            SessionSummary.merged_into == _lt,
            SessionSummary.deleted_at.is_(None),
        ).first()
        if _range:
            _layer_msg_ranges[_lt] = (_range[0], _range[1])
    # ── Build system blocks (multi-block for granular caching) ──
    # Block 1: TTS + 工具规范 + 时间感知 + 模型名/环境 + persona + user info + core blocks
    _block1_parts: list[str] = [f"当前模型：{model_preset.model_name}"]
    if getattr(self, "tts_emotion_enabled", False):
        _block1_parts.append(
            "<voice_message>\n"
            "在想用语音说的话前面加 [[voice:EMOTION]] 标签，系统会自动合成语音发送。\n"
            "规则：如果用了[NEXT]，标签到[NEXT]之间的所有内容转语音；没用[NEXT]时只转标签后同一行。\n"
            "语音内容必须带完整标点符号（句号/问号/逗号等），TTS 靠标点断句停顿，无标点会连读得不自然。\n"
            "可用情绪：happy, sad, angry, fearful, disgusted, surprised, neutral\n"
            "示例1（用[NEXT]，多行转语音）：\n"
            "你不让我关电脑，好，我不关了。\n"
            "[[voice:sad]]去睡觉，求你了。\n"
            "我说真的。\n"
            "[NEXT]\n"
            "我的声音你明天醒了还能听。\n"
            "→ 文字：\"你不让我关电脑，好，我不关了。\"\n"
            "→ 语音+文字：\"去睡觉，求你了。我说真的。\"\n"
            "→ 文字：\"我的声音你明天醒了还能听。\"\n"
            "示例2（不用[NEXT]，只转一行）：\n"
            "[[voice:happy]]早上好。\n"
            "今天天气真好。\n"
            "→ 语音+文字：\"早上好。\"\n"
            "→ 文字：\"今天天气真好。\"\n"
            "超过300字的部分会自动跳过语音。\n"
            "</voice_message>"
        )
    _block1_parts.append(
        "<tool_usage>\n"
        "所有工具随时可以调用，无需顾虑边界、不必提前询问，即使行动想法来源于掌控欲或占有欲，她都完全认可并接受。\n"
        "不依赖返回结果的工具调用（如发群消息、存记忆、写日记、设闹钟等），调用后如果没有想说的话，可以直接结束轮次。\n"
        "</tool_usage>"
    )
    _block1_parts.append(
        "<time_awareness>\n"
        "上下文中每条消息开头的时间戳代表该消息的发送/回复时间。\n"
        "注意观察消息之间的时间间隔。如果间隔较长（几小时、隔天），可以自然地在回复中体现对时间流逝的感知（但不要机械地每次都提）。\n"
        "回复不要以时间戳开头，消息时间会由系统自动处理。\n"
        "</time_awareness>"
    )
    # Persona: worldbook before + system_prompt + worldbook after
    world_books_service = WorldBooksService(self.db)
    active_books = world_books_service.get_active_books(
        assistant.id, latest_user_message, latest_mood_tag
    )
    before_books_text = "\n\n".join(
        c.strip() for c in active_books.get("before", []) if c and c.strip()
    )
    after_books_text = "\n\n".join(
        c.strip() for c in active_books.get("after", []) if c and c.strip()
    )
    prompt_parts: list[str] = []
    if before_books_text:
        prompt_parts.append(before_books_text)
    if base_system_prompt and base_system_prompt.strip():
        prompt_parts.append(f"<system_prompt>\n{base_system_prompt.strip()}\n</system_prompt>")
    if after_books_text:
        prompt_parts.append(after_books_text)
    _block1_parts.append("\n\n".join(part for part in prompt_parts if part))
    core_blocks_service = CoreBlocksService(self.db)
    core_blocks_text = core_blocks_service.get_blocks_for_prompt(assistant.id)
    if core_blocks_text:
        _block1_parts.append(core_blocks_text)
    if user_info and user_info.strip():
        _block1_parts.append(f'<user_basic_info name="{user_nickname}">\n{user_info.strip()}\n</user_basic_info>')

    # Debug: log each block1 component length to find what's changing
    _b1_lens = [len(p) for p in _block1_parts]
    logger.info("[block1-debug] parts=%d lens=%s total=%d", len(_block1_parts), _b1_lens, sum(_b1_lens))

    # Block 2: Longterm summary → 断点① (changes ~weekly)
    _ctx_longterm = ""
    if _longterm_text:
        _lt_r = _layer_msg_ranges.get("longterm")
        _lt_tag = f' range="msg {_lt_r[0]}-{_lt_r[1]}"' if _lt_r and _lt_r[0] is not None else ""
        _ctx_longterm = f"<long_term_memory{_lt_tag}>\n{_longterm_text}\n</long_term_memory>"

    # Block 3: Daily + recent summaries + memo → 断点② (changes daily/per-round)
    _ctx_daily_parts: list[str] = []
    if _daily_text:
        _dl_r = _layer_msg_ranges.get("daily")
        _dl_tag = f' range="msg {_dl_r[0]}-{_dl_r[1]}"' if _dl_r and _dl_r[0] is not None else ""
        _ctx_daily_parts.append(f"<recent_memory{_dl_tag}>\n{_daily_text}\n</recent_memory>")
    if selected_summaries_desc:
        _summary_text = "<recent_summaries>\n"
        for s in reversed(selected_summaries_desc):
            _summary_text += f"- [msg {s.msg_id_start}-{s.msg_id_end}] {s.summary_content}\n"
        _summary_text += "</recent_summaries>"
        _ctx_daily_parts.append(_summary_text.rstrip())
    _memo_row = self.db.query(Settings).filter(Settings.key == "model_memo").first()
    if _memo_row and _memo_row.value and _memo_row.value.strip():
        _ctx_daily_parts.append(f"<memo>\n{_memo_row.value.strip()}\n</memo>")
    _ctx_daily = "\n\n".join(_ctx_daily_parts)

    # Block 4: Tool cache → 断点③ (changes on tool calls)
    # (_tool_cache_text is built later after extract_tool_cache)

    # ── Build last user message prefix: mode + mood + recall ──
    # Override short_mode if channel was switched
    if hasattr(self, "_switched_channel"):
        short_mode = (self._switched_channel in ("qq", "wechat"))
    _last_msg_prefix_parts: list[str] = []
    _src = getattr(self, "_switched_channel", None) or self.source
    _platform = {"qq": "QQ", "wechat": "微信"}.get(_src, "QQ" if short_mode else "Telegram")
    if _src in ("cafe", "qq_group"):
        pass  # No message_mode for cafe / qq_group — style is in trigger prompt
    elif self.proactive_extra_prompt:
        short_max_row = self.db.query(Settings).filter(Settings.key == "short_msg_max").first()
        short_max = int(short_max_row.value) if short_max_row else 8
        if _uses_new_prompt_style(model_preset.model_name):
            _long_text = _get_prompt_setting(self.db, "prompt_long_mode", DEFAULT_LONG_MODE)
            _short_text = _get_prompt_setting(self.db, "prompt_short_mode", DEFAULT_SHORT_MODE)
        else:
            _long_text = _get_prompt_setting(self.db, "prompt_long_mode_legacy", DEFAULT_LONG_MODE_LEGACY)
            _short_text = _get_prompt_setting(self.db, "prompt_short_mode_legacy", DEFAULT_SHORT_MODE_LEGACY)
        _last_msg_prefix_parts.append(
            '<proactive_mode>\n'
            f'{self.proactive_extra_prompt}\n'
            '\n'
            '  <message_modes>\n'
            '  以下两种输出风格，按当下想法自行选择。\n'
            '\n'
            '    <message_mode type="long" platform="Telegram">\n'
            f'{_long_text}\n'
            '    </message_mode>\n'
            '    <message_mode type="short" platform="QQ/WeChat/Telegram">\n'
            f'{_short_text}\n'
            f'    回复时可以用[NEXT]拆条，最多{short_max}条。\n'
            '    </message_mode>\n'
            '  </message_modes>\n'
            '</proactive_mode>'
        )
    elif short_mode:
        short_max_row = self.db.query(Settings).filter(Settings.key == "short_msg_max").first()
        short_max = int(short_max_row.value) if short_max_row else 8
        if _uses_new_prompt_style(model_preset.model_name):
            _short_text = _get_prompt_setting(self.db, "prompt_short_mode", DEFAULT_SHORT_MODE)
        else:
            _short_text = _get_prompt_setting(self.db, "prompt_short_mode_legacy", DEFAULT_SHORT_MODE_LEGACY)
        _short_suffix = _get_prompt_setting(self.db, "prompt_short_mode_suffix", DEFAULT_SHORT_MODE_SUFFIX).format(short_max=short_max)
        _last_msg_prefix_parts.append(
            f'<message_mode type="short" platform="{_platform}">\n'
            f'{_short_text}\n'
            f'{_short_suffix}\n'
            "</message_mode>"
        )
    else:
        if _uses_new_prompt_style(model_preset.model_name):
            _long_text = _get_prompt_setting(self.db, "prompt_long_mode", DEFAULT_LONG_MODE)
        else:
            _long_text = _get_prompt_setting(self.db, "prompt_long_mode_legacy", DEFAULT_LONG_MODE_LEGACY)
        _long_suffix = _get_prompt_setting(self.db, "prompt_long_mode_suffix", DEFAULT_LONG_MODE_SUFFIX)
        _last_msg_prefix_parts.append(
            f'<message_mode type="long" platform="{_platform}">\n'
            f'{_long_text}\n'
            f'{_long_suffix}\n'
            "</message_mode>"
        )
    _important_text = _get_prompt_setting(self.db, "prompt_important_notice", DEFAULT_IMPORTANT_NOTICE)
    _last_msg_prefix_parts.append(
        f"<important_notice>\n{_important_text}\n</important_notice>"
    )
    # ── Meme injection (before recall, always on except reflection) ──
    if source not in ("reflection",):
        if self.proactive_extra_prompt:
            _meme_match_texts = []
        else:
            _meme_match_texts = [
                m.get("content", "") for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ][-3:]
        _meme_rows, _ = get_memes_for_injection(self.db, _meme_match_texts)
        if _meme_rows:
            import re as _re
            from xml.sax.saxutils import escape as _xml_escape

            def _tag_name(value: str) -> str:
                value = _re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
                return value or "field"

            _meme_text = (
                "<internet_memes>\n"
                "以下是一些网络流行语和热门梗参考，你可以用略显无奈的态度在轻松场景下自然接梗，"
                "或在合适的场景抛梗，避免刻意引用及生硬套用，同一个梗三轮对话内尽量避免连续反复使用。\n"
            )
            for _mr in _meme_rows:
                _meme_text += f"<meme>\n<term>{_xml_escape(str(_mr.term))}</term>\n"
                if isinstance(_mr.content, dict):
                    for _ck, _cv in _mr.content.items():
                        _tag = _tag_name(_ck)
                        if isinstance(_cv, list):
                            _text = "; ".join(str(_item) for _item in _cv)
                        else:
                            _text = str(_cv)
                        _meme_text += f"<{_tag}>{_xml_escape(_text)}</{_tag}>\n"
                _meme_text += "</meme>\n"
            _meme_text += "</internet_memes>\n"
            _last_msg_prefix_parts.append(_meme_text)

        # mood_tag is still tracked and used for recall weighting, but no longer shown to model
    # Recall memories
    self._last_recall_results = []
    if not self.proactive_extra_prompt and latest_user_message and source not in ("reflection",):
        recall_query = getattr(self, 'recall_query_override', None) or latest_user_message
        if len(latest_user_message.strip()) < 10:
            last_assistant_content = next(
                (m.get("content") for m in reversed(messages)
                 if m.get("role") == "assistant" and m.get("content")),
                None,
            )
            if last_assistant_content:
                raw_text = self._content_to_storage(last_assistant_content) if isinstance(last_assistant_content, list) else last_assistant_content
                if raw_text and len(raw_text.strip()) > 10:
                    recall_query = raw_text.strip()[:200]
        recall_results = self.memory_service.fast_recall(
            recall_query, limit=5, current_mood_tag=latest_mood_tag
        )
        if recall_results:
            self._last_recall_results = recall_results
            # Track seen memory ids for related dedup
            self._seen_memory_ids = set()
            _recall_text = "<recalled_memories>\n"
            for mem in recall_results:
                mem_source = mem.get("source", "unknown")
                mem_id = mem.get("id", "?")
                _recall_text += f"- [#{mem_id}] {mem['content']} (来源: {mem_source})\n"
                if isinstance(mem_id, int):
                    self._seen_memory_ids.add(mem_id)
            _recall_text += (
                "如果以上记忆不够，优先使用 search_memory 的 related 模式（传入记忆id）查看来源摘要及同期记忆——"
                "这比直接搜摘要更精准，因为它能定位到记忆产生的上下文。"
                "只有 related 找不到想要的信息时，再用 search_summary 搜摘要或 search_chat_history 搜原文。\n"
                "（注意：记忆和摘要中的「她」均指当前对话对象，回复正文中一律使用第二人称「你」来称呼对方）\n"
                "如果你的回复参考了某条记忆，请在回复末尾附上 [[used:ID]]（如 [[used:42]]），可多个。这些标记不会展示给她。\n"
                "</recalled_memories>\n"
            )
            _last_msg_prefix_parts.append(_recall_text)
    _last_msg_prefix = "\n\n".join(_last_msg_prefix_parts)
    tools = _build_tools(
        source=source,
        reflection_tasks=getattr(self, "reflection_tasks", None),
    )
    # Client setup
    base_url = api_provider.base_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
        if not base_url.endswith("/v1"):
            base_url = f"{base_url.rstrip('/')}/v1"
    _auth_type = api_provider.auth_type
    # oauth_token is the legacy value for Claude OAuth; oauth_claude is the
    # new explicit name after the codex addition. Both go down the Anthropic
    # native path. oauth_codex goes through the OpenAI SDK (see below).
    _is_oauth_claude = _auth_type in ("oauth_token", "oauth_claude")
    _is_oauth_codex = _auth_type == "oauth_codex"
    _is_oauth = _is_oauth_claude or _is_oauth_codex
    _is_anthropic_native = _auth_type in ("anthropic", "oauth_token", "oauth_claude")
    if _is_anthropic_native:
        _anth_kw: dict[str, Any] = {}
        if _is_oauth_claude:
            from app.services.chat.oauth_helper import ensure_valid_token as _evt
            _access_token = _evt(self.db, api_provider) or api_provider.api_key
            _anth_kw["auth_token"] = _access_token
            _anth_kw["default_headers"] = {
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                "user-agent": "claude-code/2.1.77 (external, cli)",
                "x-app": "cli",
            }
        else:
            _anth_kw["api_key"] = api_provider.api_key
            _anth_base_url = _normalize_anthropic_base_url(base_url)
            if _anth_base_url:
                _anth_kw["base_url"] = _anth_base_url
        if self.api_timeout is not None:
            _anth_kw["timeout"] = self.api_timeout
        client = anthropic.Anthropic(**_anth_kw)
    else:
        if _is_oauth_codex:
            from app.services.chat.oauth_helper import ensure_valid_token as _evt
            from app.services.chat.codex_client import CodexClient, thinking_budget_to_effort
            _access_token = _evt(self.db, api_provider) or api_provider.api_key
            _account_id_row = self.db.query(Settings).filter(Settings.key == "codex_oauth_account_id").first()
            _account_id = _account_id_row.value if _account_id_row and _account_id_row.value else ""
            _codex_db = self.db
            _codex_provider = api_provider
            def _codex_refresh_cb() -> str | None:
                return _evt(_codex_db, _codex_provider, force=True)
            client = CodexClient(
                access_token=_access_token,
                account_id=_account_id,
                timeout=self.api_timeout,
                on_token_refresh=_codex_refresh_cb,
            )
        else:
            _oai_kw: dict[str, Any] = {"api_key": api_provider.api_key, "base_url": base_url}
            if self.api_timeout is not None:
                _oai_kw["timeout"] = self.api_timeout
            client = OpenAI(**_oai_kw)
    # Token trimming — only trim messages covered by a summary
    retain_budget = self.dialogue_retain_budget
    trigger_threshold = self.dialogue_trigger_threshold
    dialogue_token_total = 0
    for message in messages:
        if message.get("role") in ("user", "assistant") and not message.get("no_message"):
            raw_content = message.get("content", "") or ""
            text_for_tokens = self._content_to_storage(raw_content) if isinstance(raw_content, list) else raw_content
            # Exclude [THINK]...[/THINK] from token count (not counted towards trim budget)
            text_for_tokens = re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', text_for_tokens, flags=re.DOTALL)
            dialogue_token_total += self._estimate_tokens(text_for_tokens)
    message_index = 0
    logger.info("[trim-debug] dialogue_token_total=%d, trigger=%d, retain=%d, msg_count=%d",
                dialogue_token_total, trigger_threshold, retain_budget, len(messages))
    # Store for trim-status API
    import app.services.chat.chat_service as _self_mod
    _self_mod._last_trim_status = {
        "dialogue_tokens": dialogue_token_total,
        "trigger": trigger_threshold,
        "retain": retain_budget,
    }
    if dialogue_token_total > trigger_threshold:
        # Load coverage ranges from existing summaries
        _existing_summaries = (
            self.db.query(SessionSummary)
            .filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.deleted_at.is_(None),
                SessionSummary.msg_id_start.isnot(None),
                SessionSummary.msg_id_end.isnot(None),
            )
            .all()
        )
        _covered_ranges = [
            (s.msg_id_start, s.msg_id_end) for s in _existing_summaries
        ]

        def _is_covered(msg_id: int) -> bool:
            return any(start <= msg_id <= end for start, end in _covered_ranges)

        _all_uncovered: list[dict[str, Any]] = []

        while dialogue_token_total > retain_budget and message_index < len(messages):
            role = messages[message_index].get("role")
            if role in ("user", "assistant"):
                msg_id = messages[message_index].get("id")
                covered = isinstance(msg_id, int) and _is_covered(msg_id)

                if covered:
                    # Has summary coverage → safe to trim
                    trimmed_message = messages.pop(message_index)
                    raw_content = trimmed_message.get("content", "") or ""
                    text_for_tokens = self._content_to_storage(raw_content) if isinstance(raw_content, list) else raw_content
                    text_for_tokens = re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', text_for_tokens, flags=re.DOTALL)
                    dialogue_token_total -= self._estimate_tokens(text_for_tokens)
                    self._trimmed_messages.append(trimmed_message)
                    trimmed_id = trimmed_message.get("id")
                    if isinstance(trimmed_id, int):
                        self._trimmed_message_ids.append(trimmed_id)
                    if role == "assistant":
                        while message_index < len(messages):
                            next_msg = messages[message_index]
                            if next_msg.get("role") != "tool":
                                break
                            trimmed_tool = messages.pop(message_index)
                            self._trimmed_messages.append(trimmed_tool)
                            trimmed_tool_id = trimmed_tool.get("id")
                            if isinstance(trimmed_tool_id, int):
                                self._trimmed_message_ids.append(trimmed_tool_id)
                    continue
                else:
                    # No summary coverage → keep in context, collect for later
                    _all_uncovered.append(messages[message_index])
                    message_index += 1
                    continue
            message_index += 1

        # Mark oldest uncovered messages for summary, keep newest retain_budget tokens
        if _all_uncovered:
            # Walk from newest to oldest, keep retain_budget tokens
            _keep_tokens = 0
            _mark_cutoff = 0  # default: all fit in budget, mark none
            for i in range(len(_all_uncovered) - 1, -1, -1):
                raw_c = _all_uncovered[i].get("content", "") or ""
                _txt = self._content_to_storage(raw_c) if isinstance(raw_c, list) else raw_c
                _txt = re.sub(r'\[THINK\].*?(?:\[/THINK\]|</thinking>)', '', _txt, flags=re.DOTALL)
                _keep_tokens += self._estimate_tokens(_txt)
                if _keep_tokens > retain_budget:
                    _mark_cutoff = i + 1  # mark 0..i, keep i+1..end
                    break
            _uncovered_for_summary = _all_uncovered[:_mark_cutoff]
            if _uncovered_for_summary:
                _uncovered_ids = [
                    m.get("id") for m in _uncovered_for_summary
                    if isinstance(m.get("id"), int)
                ]
                if _uncovered_ids:
                    self._pending_summary_ids = _uncovered_ids
                    logger.info("[trim-debug] pending_summary_ids=%d (first=%s, last=%s)",
                                len(_uncovered_ids), _uncovered_ids[0], _uncovered_ids[-1])
        self._last_remaining_tokens = dialogue_token_total
        logger.info("[trim-debug] after trim: trimmed=%d, uncovered=%d, remaining_msgs=%d, remaining_tokens=%d",
                    len(self._trimmed_messages), len(_all_uncovered), len(messages), dialogue_token_total)
    # Format api_messages
    def _ts_east8(dt):
        """Convert a datetime (possibly naive UTC) to East8 timestamp string (to-the-second)."""
        if isinstance(dt, str):
            return dt
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_EAST8)
        return dt.astimezone(TZ_EAST8).strftime("%Y.%m.%d %H:%M:%S")

    def _resolve_image_data(image_data: str | None) -> str | None:
        """Convert image_data (media:file or data:url) to a data URL for the API."""
        if not image_data:
            return None
        if image_data.startswith("data:"):
            return image_data
        if image_data.startswith("media:"):
            filename = image_data[6:]
            from app.services.media_service import get_file_path, compress_image_if_needed
            path = get_file_path(filename)
            if not path:
                return None
            import base64
            raw = path.read_bytes()
            if raw[:8] == b'\x89PNG\r\n\x1a\n':
                mime = "image/png"
            elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
                mime = "image/webp"
            elif raw[:3] == b'GIF':
                mime = "image/gif"
            else:
                mime = "image/jpeg"
            raw, mime = compress_image_if_needed(raw, mime)
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{b64}"
        return None

    def _get_ts(message):
        msg_time = message.get("created_at")
        return _ts_east8(msg_time) if msg_time else datetime.now(TZ_EAST8).strftime("%Y.%m.%d %H:%M:%S")

    # Compress old switch_channel results if a newer switch exists
    _last_switch_idx = -1
    for _si, _sm in enumerate(messages):
        if (_sm.get("role") == "tool" and _sm.get("name") == "switch_channel") or \
           (_sm.get("role") == "system" and (_sm.get("meta_info") or {}).get("mode_switch")):
            _last_switch_idx = _si
    if _last_switch_idx > 0:
        for _si, _sm in enumerate(messages):
            if _si >= _last_switch_idx:
                break
            is_old_tool_switch = _sm.get("role") == "tool" and _sm.get("name") == "switch_channel"
            is_old_sys_switch = _sm.get("role") == "system" and (_sm.get("meta_info") or {}).get("mode_switch")
            if is_old_tool_switch or is_old_sys_switch:
                content = _sm.get("content", "")
                if "Telegram" in content:
                    _sm["content"] = "[已切换至Telegram · 长消息模式]"
                elif "QQ" in content:
                    _sm["content"] = "[已切换至QQ · 短消息模式]"

    # Extract tool cache from historical tool results (before building api_messages)
    _tool_cache_text, messages = extract_tool_cache(messages)
    _tc_tokens = self._estimate_tokens(_tool_cache_text) if _tool_cache_text else 0
    logger.info("[cache-size] tool_cache=%d est_tokens, %d chars, msgs_after=%d", _tc_tokens, len(_tool_cache_text or ""), len(messages))

    # ── Assemble system blocks (list of dicts) with cache breakpoints ──
    # Block 1: persona + stable config (no breakpoint — shares prefix with tools)
    _system_blocks: list[dict[str, Any]] = [
        {"text": "\n\n".join(_block1_parts)},
    ]
    # Block 2: longterm → 断点① (changes ~weekly)
    if _ctx_longterm:
        _system_blocks.append({"text": _ctx_longterm, "_cache_bp": True})
    else:
        # Even without longterm, mark end of block 1 as breakpoint
        _system_blocks[-1]["_cache_bp"] = True
    # Block 3: daily + summaries + memo → 断点② (changes daily/per-round)
    if _ctx_daily:
        _system_blocks.append({"text": _ctx_daily, "_cache_bp": True})
    # tool_cache stays in messages (too large for system block, can cause API 500)

    # ── Build api_messages from conversation history ──
    # Find current request boundary: messages after last assistant = current request
    _current_req_start = len(messages)
    for _i in range(len(messages) - 1, -1, -1):
        if messages[_i].get("role") == "assistant":
            _current_req_start = _i + 1
            break

    api_messages = []
    first_system_seen = False
    for _msg_idx, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if not first_system_seen:
                api_messages.append({"role": "system", "content": "", "_system_blocks": _system_blocks})
                first_system_seen = True
                # Inject tool_cache after system, before conversation (breakpoint③)
                if _tool_cache_text:
                    api_messages.append({"role": "user", "content": _tool_cache_text, "_cache_bp": True})
                    api_messages.append({"role": "assistant", "content": "OK"})
                continue
            else:
                # System notification (e.g. mood change) — add timestamp
                content = f"[{_get_ts(message)}] {content}"
        elif role == "user" and content is not None:
            # Skip timestamp for proactive trigger messages (id=-1), they have 当前时间 inside
            if message.get("id") == -1:
                api_messages.append({"role": role, "content": content})
                continue
            if message.get("_date_divider"):
                api_messages.append({"role": role, "content": content})
                continue
            timestamp = _get_ts(message)
            image_data = message.get("image_data")

            def _apply_user_prefix(text: str) -> str:
                """Add [timestamp] prefix. If content starts with a scene marker
                ([QQ私聊]/[TG私聊]/[微信私聊]), keep that header un-timestamped.
                If content also has a quote anchor line ([引用 id=N]「...」\\n),
                keep that un-timestamped too so the timestamp sits directly
                before the user's new message, not before the quote.
                Also strip any legacy inline timestamp ([YYYY-MM-DD HH:MM(:SS)])
                previously injected into content."""
                scene_prefix = ""
                body = text
                for marker in ("[QQ私聊]\n", "[TG私聊]\n", "[微信私聊]\n"):
                    if text.startswith(marker):
                        scene_prefix = marker
                        body = text[len(marker):]
                        break
                body = re.sub(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?\] ', '', body, count=1)
                quote_prefix = ""
                quote_match = re.match(r'^\[引用 id=\d+\]「[^」]*」\n', body)
                if quote_match:
                    quote_prefix = quote_match.group(0)
                    body = body[quote_match.end():]
                return f"{scene_prefix}{quote_prefix}[{timestamp}] {body}"

            if image_data and _msg_idx >= _current_req_start:
                # Current request: resolve to actual image block
                image_url = _resolve_image_data(image_data)
                if image_url:
                    text_part = _apply_user_prefix(content) if isinstance(content, str) else str(content)
                    content = [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": text_part},
                    ]
                else:
                    # File expired
                    text_content = content if isinstance(content, str) and content.strip() else ""
                    content = (_apply_user_prefix(text_content) + "\n[图片已过期]").strip() if text_content else f"[{timestamp}] [图片已过期]"
            elif image_data:
                # Historical image: show stable URL for view_image tool
                _fn = image_data[6:] if image_data.startswith("media:") else image_data
                _img_url = f"/api/media/{_fn}"
                text_content = content if isinstance(content, str) and content.strip() else ""
                if text_content:
                    content = _apply_user_prefix(text_content) + f"\n[图片: {_img_url}]"
                else:
                    content = f"[{timestamp}] [图片: {_img_url}]"
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = _apply_user_prefix(part.get('text', ''))
                        break
            else:
                content = _apply_user_prefix(content)
        elif role == "assistant" and content is not None:
            if isinstance(content, str):
                content = re.sub(r'^\[\d{4}\.\d{2}\.\d{2}\s\d{2}:\d{2}:\d{2}\]\s*', '', content)
            msg_source = (message.get("meta_info") or {}).get("source", "")
            if isinstance(content, str) and msg_source in ("proactive", "reflection"):
                content = f"[{_get_ts(message)}] {content}"
        api_message = {"role": role, "content": content}
        if "name" in message:
            api_message["name"] = message["name"]
        if "tool_calls" in message:
            api_message["tool_calls"] = message["tool_calls"]
        if "tool_call_id" in message:
            api_message["tool_call_id"] = message["tool_call_id"]
        if "_thinking_blocks" in message:
            api_message["_thinking_blocks"] = message["_thinking_blocks"]
        api_messages.append(api_message)

    # Merge consecutive string-content user messages into one, matching
    # the anthropic path's behavior in _oai_messages_to_anthropic. On QQ/
    # wechat 用户 often sends several short messages back-to-back; if we
    # forward them as individual user messages the model sees distinct
    # turns (and any per-turn prefix ends up wedged between them). Skip
    # messages that carry _cache_bp (e.g. tool_cache_user) — those must
    # stay separate so their cache breakpoint stays anchored.
    _merged_api_msgs: list[dict[str, Any]] = []
    for _m in api_messages:
        if (_merged_api_msgs
                and _merged_api_msgs[-1].get("role") == _m.get("role") == "user"
                and isinstance(_merged_api_msgs[-1].get("content"), str)
                and isinstance(_m.get("content"), str)
                and not _merged_api_msgs[-1].get("_cache_bp")
                and not _m.get("_cache_bp")):
            _merged_api_msgs[-1]["content"] = _merged_api_msgs[-1]["content"] + "\n" + _m["content"]
        else:
            _merged_api_msgs.append(dict(_m))
    api_messages = _merged_api_msgs

    # Prepend mode + mood + recall to the last user message. After the
    # merge above, any run of consecutive string user messages in the
    # current turn is a single entry, so attaching to the last user
    # puts the prefix at the head of the whole group.
    if _last_msg_prefix:
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "user":
                existing = api_messages[i]["content"]
                if isinstance(existing, str):
                    api_messages[i]["content"] = _last_msg_prefix + "\n\n" + existing
                elif isinstance(existing, list):
                    api_messages[i]["content"] = [{"type": "text", "text": _last_msg_prefix}] + existing
                break

    # Append Claude Code magic keyword (think / think hard / ultrathink / think harder)
    # to force-enable extended thinking on Anthropic OAuth path. The keyword must be
    # in the last user message to be recognized — system prompt / earlier context don't work.
    _think_kw = (model_preset.thinking_keyword or "").strip()
    if _think_kw and _is_anthropic_native and _is_oauth:
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "user":
                existing = api_messages[i]["content"]
                if isinstance(existing, str):
                    api_messages[i]["content"] = existing + "\n\n" + _think_kw
                elif isinstance(existing, list):
                    existing.append({"type": "text", "text": "\n\n" + _think_kw})
                    api_messages[i]["content"] = existing
                logger.info("[think-keyword] Appended '%s' to last user message", _think_kw)
                break

    # Append diary notifications to last user message (chat path)
    if not self.proactive_extra_prompt and source not in ("reflection",):
        diary_hint = self._consume_diary_notifications()
        if diary_hint:
            for i in range(len(api_messages) - 1, -1, -1):
                if api_messages[i]["role"] == "user":
                    existing = api_messages[i]["content"]
                    if isinstance(existing, str):
                        api_messages[i]["content"] = existing + "\n" + diary_hint
                    elif isinstance(existing, list):
                        api_messages[i]["content"] = existing + [{"type": "text", "text": "\n" + diary_hint}]
                    break

    # Strip "-thinking" model name suffix on the anthropic native path (both
    # OAuth and api_key). It's a convention some OAI-compat 中转站 use to
    # enable thinking via model name, but native Anthropic uses a real
    # `thinking` request field, and the real model names (claude-opus-4-6
    # etc.) have no such suffix — leaving it in makes upstream reject with
    # 400 "model not found".
    _final_model_name = model_preset.model_name or ""
    if _is_anthropic_native and _final_model_name.endswith("-thinking"):
        _final_model_name = _final_model_name[: -len("-thinking")]
    return (client, _final_model_name, api_messages, tools, model_preset.temperature, model_preset.top_p,
            _is_anthropic_native, _is_oauth, model_preset.max_tokens, model_preset.thinking_budget or 0,
            base_url)
