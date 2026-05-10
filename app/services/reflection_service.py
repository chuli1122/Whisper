from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from openai import OpenAI
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, sessionmaker

from app.models.models import (
    ApiProvider,
    Assistant,
    Memory,
    MemoryVersion,
    Message,
    ModelPreset,
    PendingReflectionChange,
    ReflectionLog,
    Settings,
)
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 30


class ReflectionService:
    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory

    # ── public API ──────────────────────────────────────────────────────

    def run_reflection(
        self,
        assistant_id: int = 2,
        count: int | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> dict:
        """Force run reflection regardless of threshold.

        Args:
            assistant_id: which assistant personality to use
            count: if given, reflect on the last N memories (by created_at desc)
            start: 1-based position (oldest=1), combined with end for range
            end: 1-based position (inclusive)
        """
        db = self.session_factory()
        try:
            assistant = db.get(Assistant, assistant_id)
            if not assistant:
                raise ValueError(f"Assistant {assistant_id} not found")

            if start is not None and end is not None:
                memories = self._get_memories_by_position(db, start, end)
            elif count is not None and count > 0:
                memories = self._get_last_n_memories(db, count)
            else:
                since = self._last_reflection_time(db)
                memories = self._get_recent_memories(db, since)
            if not memories:
                logger.info("[reflection] no memories to reflect on")
                return {"changes": [], "reasoning": "没有需要反思的记忆"}

            # figure out which memories were user-edited
            edited_ids: set[int] = set()
            for m in memories:
                if self._check_user_edited(db, m.id):
                    edited_ids.add(m.id)

            system_prompt, user_prompt = self._build_prompt(
                db, assistant, memories, edited_ids
            )
            model_result = self._call_model(db, assistant, system_prompt, user_prompt)

            if model_result is None:
                logger.warning("[reflection] model returned no usable result")
                return {"changes": [], "reasoning": "模型返回无法解析的结果"}

            changes = model_result.get("changes", [])
            reasoning = model_result.get("reasoning", "")

            # Save log first to get ID
            preset = db.get(ModelPreset, assistant.model_preset_id)
            model_name = preset.model_name if preset else "unknown"
            log_entry = ReflectionLog(
                memory_count=len(memories),
                changes={"changes": changes, "reasoning": reasoning},
                model_used=model_name,
            )
            db.add(log_entry)
            db.flush()  # get log_entry.id

            # Save as pending changes instead of applying directly
            pending = self._save_pending_changes(db, changes, log_entry.id)

            db.commit()

            logger.info(
                "[reflection] done – %d memories reviewed, %d pending changes saved",
                len(memories),
                len(pending),
            )
            return {"changes": pending, "reasoning": reasoning}
        finally:
            db.close()

    # ── internals ───────────────────────────────────────────────────────

    def _get_threshold(self, db: Session) -> int:
        row = (
            db.query(Settings)
            .filter(Settings.key == "reflection_threshold")
            .first()
        )
        if row:
            try:
                return int(row.value)
            except (ValueError, TypeError):
                pass
        return _DEFAULT_THRESHOLD

    def _last_reflection_time(self, db: Session) -> datetime | None:
        last = (
            db.query(ReflectionLog)
            .order_by(desc(ReflectionLog.created_at))
            .first()
        )
        return last.created_at if last else None

    def _count_new_memories(self, db: Session, since: datetime | None) -> int:
        q = db.query(func.count(Memory.id)).filter(Memory.deleted_at.is_(None))
        if since is not None:
            q = q.filter(Memory.created_at > since)
        return q.scalar() or 0

    def _get_recent_memories(
        self, db: Session, since: datetime | None
    ) -> list[Memory]:
        q = db.query(Memory).filter(Memory.deleted_at.is_(None))
        if since is not None:
            q = q.filter(Memory.created_at > since)
        return q.order_by(Memory.created_at.asc()).all()

    def _get_last_n_memories(self, db: Session, count: int) -> list[Memory]:
        rows = (
            db.query(Memory)
            .filter(Memory.deleted_at.is_(None))
            .order_by(Memory.created_at.desc())
            .limit(count)
            .all()
        )
        return list(reversed(rows))  # return in chronological order

    def _get_memories_by_position(
        self, db: Session, start: int, end: int
    ) -> list[Memory]:
        """Get memories by 1-based position (oldest=1). start and end inclusive."""
        offset = max(0, start - 1)
        limit = max(1, end - offset)
        return (
            db.query(Memory)
            .filter(Memory.deleted_at.is_(None))
            .order_by(Memory.created_at.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def _check_user_edited(self, db: Session, memory_id: int) -> bool:
        return (
            db.query(MemoryVersion)
            .filter(
                MemoryVersion.memory_id == memory_id,
                MemoryVersion.changed_by == "admin",
            )
            .first()
            is not None
        )

    def _build_prompt(
        self,
        db: Session,
        assistant: Assistant,
        memories: list[Memory],
        edited_ids: set[int],
    ) -> tuple[str, str]:
        # system prompt = assistant personality + reflection role
        system_prompt = (
            f"{assistant.system_prompt}\n\n"
            "---\n"
            "你现在进入「记忆反思」模式。\n"
            "你需要审视最近新增的记忆条目，找出需要修正的问题。\n"
            "你的目标是让记忆库保持准确、简洁、无重复。"
        )

        # build memory list
        lines: list[str] = []
        for m in memories:
            edited_tag = " [她手动编辑过]" if m.id in edited_ids else ""
            lines.append(
                f"- ID={m.id} | klass={m.klass} | disclosure={m.disclosure or '无'} "
                f"| source={m.source}{edited_tag}\n"
                f"  内容: {m.content}"
            )
        memory_block = "\n".join(lines)

        user_prompt = (
            f"以下是最近新增的 {len(memories)} 条记忆：\n\n"
            f"{memory_block}\n\n"
            "请仔细审视这些记忆，找出以下问题：\n"
            "1. **重复/高度相似的记忆** → 合并（merge）：把内容合并到更完整的那条，删除另一条\n"
            "2. **过时或不准确的信息** → 更新（update）：修正内容\n"
            "3. **klass 分类错误** → 更新（update）：修正 klass\n"
            "4. **disclosure 标注缺失或错误** → 更新（update）：修正 disclosure\n"
            "5. **无价值/噪音记忆** → 删除（delete）\n\n"
            "⚠️ 重要规则：\n"
            "- 单条记忆不超过100字\n"
            "- 标记为 [她手动编辑过] 的记忆，可以合并，但不要随意修改其内容，除非明显事实错误\n"
            "- 合并时，保留信息更完整的那条作为 merge_into 目标\n"
            "- 如果没有需要修改的，返回空 changes 数组即可\n\n"
            "请用 JSON 格式返回结果，不要包含其他文字：\n"
            "```json\n"
            "{\n"
            '  "changes": [\n'
            '    {"action": "update", "memory_id": 123, "content": "新内容", "klass": "新分类", "disclosure": "新disclosure"},\n'
            '    {"action": "delete", "memory_id": 456},\n'
            '    {"action": "merge", "memory_id": 789, "merge_into": 123, "content": "合并后的完整内容"}\n'
            "  ],\n"
            '  "reasoning": "简要说明你做了什么修改以及原因"\n'
            "}\n"
            "```\n"
            "其中 update 的 content/klass/disclosure 都是可选的，只传需要修改的字段。"
        )

        return system_prompt, user_prompt

    def _call_model(
        self,
        db: Session,
        assistant: Assistant,
        system_prompt: str,
        user_prompt: str,
    ) -> dict | None:
        preset = db.get(ModelPreset, assistant.model_preset_id)
        if not preset:
            raise ValueError(
                f"Model preset not found: id={assistant.model_preset_id}"
            )

        api_provider = db.get(ApiProvider, preset.api_provider_id)
        if not api_provider:
            raise ValueError(
                f"API provider not found for preset_id={preset.id}"
            )

        base_url = api_provider.base_url
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
            if not base_url.endswith("/v1"):
                base_url = f"{base_url.rstrip('/')}/v1"

        client = OpenAI(api_key=api_provider.api_key, base_url=base_url)

        params: dict[str, Any] = {
            "model": preset.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": preset.max_tokens,
        }
        if preset.temperature is not None:
            params["temperature"] = preset.temperature
        if preset.top_p is not None:
            params["top_p"] = preset.top_p
        if preset.thinking_budget and preset.thinking_budget > 0:
            params["extra_body"] = {
                "reasoning": {"max_tokens": preset.thinking_budget}
            }

        logger.info(
            "[reflection] calling model %s (preset=%s)",
            preset.model_name,
            preset.name,
        )

        try:
            response = client.chat.completions.create(**params)
        except Exception:
            logger.exception("[reflection] model call failed")
            return None

        if not response.choices:
            logger.warning("[reflection] model returned no choices")
            return None

        raw = response.choices[0].message.content or ""
        logger.debug("[reflection] raw response: %s", raw[:500])
        return self._parse_json(raw)

    def _parse_json(self, raw: str) -> dict | None:
        """Parse JSON from model response with repair logic."""
        cleaned = raw.strip()
        # strip markdown fences
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json") :]
        elif cleaned.startswith("```"):
            cleaned = cleaned[len("```") :]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")]
        cleaned = cleaned.strip()

        # try direct parse
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # try raw_decode
        try:
            decoder = json.JSONDecoder()
            result, _ = decoder.raw_decode(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # repair: fix missing commas, trailing commas
        repaired = re.sub(r'"\s*\n\s*"', '",\n"', cleaned)
        repaired = re.sub(r'(\})\s*\n\s*"', '},\n"', repaired)
        repaired = re.sub(r'(\])\s*\n\s*"', '],\n"', repaired)
        repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
        try:
            result = json.loads(repaired)
            if isinstance(result, dict):
                logger.info("[reflection] JSON repair succeeded")
                return result
        except json.JSONDecodeError:
            pass

        # last resort: raw_decode on repaired
        try:
            decoder = json.JSONDecoder()
            result, _ = decoder.raw_decode(repaired)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        logger.error("[reflection] failed to parse JSON from model response")
        return None

    def _save_pending_changes(self, db: Session, changes: list[dict], log_id: int) -> list[dict]:
        """Save proposed changes as pending instead of applying directly."""
        saved: list[dict] = []
        for change in changes:
            action = change.get("action")
            memory_id = change.get("memory_id")
            if not action or not memory_id:
                continue

            memory = db.get(Memory, memory_id)
            if not memory or memory.deleted_at is not None:
                continue

            new_content = change.get("content")
            if new_content and len(new_content) > 120:
                continue

            pending = PendingReflectionChange(
                reflection_log_id=log_id,
                action=action,
                memory_id=memory_id,
                old_content=memory.content,
                old_klass=memory.klass,
                old_disclosure=memory.disclosure,
            )

            if action == "update":
                pending.proposed_content = change.get("content")
                pending.proposed_klass = change.get("klass")
                pending.proposed_disclosure = change.get("disclosure")
                pending.proposed_tags = change.get("tags")
            elif action == "delete":
                pass
            elif action == "merge":
                merge_into_id = change.get("merge_into")
                if not merge_into_id:
                    continue
                target = db.get(Memory, merge_into_id)
                if not target or target.deleted_at is not None:
                    continue
                pending.merge_into_id = merge_into_id
                pending.proposed_content = change.get("content")
                pending.merge_target_old_content = target.content
            else:
                continue

            db.add(pending)
            saved.append(change)

        return saved

    def _apply_changes(self, db: Session, changes: list[dict]) -> list[dict]:
        """Apply each change and return list of actually applied changes."""
        applied: list[dict] = []
        now = datetime.now(TZ_EAST8)

        for change in changes:
            action = change.get("action")
            memory_id = change.get("memory_id")
            if not action or not memory_id:
                logger.warning("[reflection] skipping invalid change: %s", change)
                continue

            memory = db.get(Memory, memory_id)
            if not memory or memory.deleted_at is not None:
                logger.warning(
                    "[reflection] memory %s not found or already deleted", memory_id
                )
                continue

            try:
                # Validate content length (same limit as normal save)
                new_content = change.get("content")
                if new_content and len(new_content) > 120:
                    logger.warning(
                        "[reflection] skipping %s on memory %d: content too long (%d > 120)",
                        action, memory_id, len(new_content),
                    )
                    continue

                # Record old state for front-end comparison
                change["old_content"] = memory.content
                change["old_klass"] = memory.klass
                change["old_disclosure"] = memory.disclosure

                if action == "update":
                    self._apply_update(db, memory, change, now)
                    # Record new state after update
                    change["new_content"] = memory.content
                    change["new_klass"] = memory.klass
                    change["new_disclosure"] = memory.disclosure
                    applied.append(change)
                    logger.info("[reflection] updated memory %d", memory_id)

                elif action == "delete":
                    memory.deleted_at = now
                    applied.append(change)
                    logger.info("[reflection] soft-deleted memory %d", memory_id)

                elif action == "merge":
                    merge_into_id = change.get("merge_into")
                    if not merge_into_id:
                        logger.warning(
                            "[reflection] merge missing merge_into for memory %d",
                            memory_id,
                        )
                        continue
                    target = db.get(Memory, merge_into_id)
                    if not target or target.deleted_at is not None:
                        logger.warning(
                            "[reflection] merge target %d not found or deleted",
                            merge_into_id,
                        )
                        continue
                    change["old_content"] = memory.content
                    change["merge_target_old_content"] = target.content
                    self._apply_merge(db, memory, target, change, now)
                    change["merge_target_new_content"] = target.content
                    applied.append(change)
                    logger.info(
                        "[reflection] merged memory %d into %d",
                        memory_id,
                        merge_into_id,
                    )

                else:
                    logger.warning("[reflection] unknown action: %s", action)

            except Exception:
                logger.exception(
                    "[reflection] failed to apply %s on memory %d",
                    action,
                    memory_id,
                )

        return applied

    def _apply_update(
        self, db: Session, memory: Memory, change: dict, now: datetime
    ) -> None:
        """Create version snapshot then update the memory."""
        # check if anything actually changed
        changed = False
        if "content" in change and change["content"] != memory.content:
            changed = True
        if "klass" in change and change["klass"] != memory.klass:
            changed = True
        if "disclosure" in change and change["disclosure"] != memory.disclosure:
            changed = True
        if "tags" in change and change["tags"] != memory.tags:
            changed = True

        if not changed:
            return

        # snapshot before change
        db.add(
            MemoryVersion(
                memory_id=memory.id,
                content=memory.content,
                klass=memory.klass,
                tags=memory.tags,
                disclosure=memory.disclosure,
                changed_by="reflection",
            )
        )
        # apply updates
        if "content" in change:
            # Preserve timestamp prefix if original has one but new content doesn't
            ts_match = re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", memory.content)
            new_content = change["content"]
            if ts_match and not re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", new_content):
                new_content = ts_match.group() + new_content
            memory.content = new_content
        if "klass" in change:
            memory.klass = change["klass"]
        if "disclosure" in change:
            memory.disclosure = change["disclosure"]
        if "tags" in change:
            memory.tags = change["tags"]
        memory.updated_at = now

    def _apply_merge(
        self,
        db: Session,
        source: Memory,
        target: Memory,
        change: dict,
        now: datetime,
    ) -> None:
        """Merge source into target: update target content, soft-delete source."""
        # snapshot target before merge
        db.add(
            MemoryVersion(
                memory_id=target.id,
                content=target.content,
                klass=target.klass,
                tags=target.tags,
                disclosure=target.disclosure,
                changed_by="reflection",
            )
        )
        # update target with merged content
        if "content" in change:
            ts_match = re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", target.content)
            new_content = change["content"]
            if ts_match and not re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", new_content):
                new_content = ts_match.group() + new_content
            target.content = new_content
        target.updated_at = now

        # soft-delete source
        source.deleted_at = now

    def _store_reflection_notice(self, db: Session, applied: list[dict]) -> None:
        """Store a brief notice of what was changed, for injection into next chat."""
        parts: list[str] = []
        for c in applied:
            mid = c.get("memory_id")
            action = c.get("action")
            if action == "update":
                fields = []
                if c.get("old_content") != c.get("new_content"):
                    fields.append("内容")
                if c.get("old_klass") != c.get("new_klass"):
                    fields.append("分类")
                if c.get("old_disclosure") != c.get("new_disclosure"):
                    fields.append("disclosure")
                parts.append(f"更新了 #{mid} 的{'、'.join(fields or ['内容'])}")
            elif action == "delete":
                parts.append(f"删除了 #{mid}")
            elif action == "merge":
                parts.append(f"合并了 #{mid} → #{c.get('merge_into')}")
        notice = "；".join(parts)
        row = db.query(Settings).filter(Settings.key == "reflection_notice").first()
        if row:
            row.value = notice
        else:
            db.add(Settings(key="reflection_notice", value=notice))


# ── Auto-reflection loop ──────────────────────────────────────────────────

_IDLE_MINUTES = 10
_CHECK_INTERVAL = 300  # 5 minutes

def _build_reflection_prompt(
    now: str, count: int, start: int, end: int, memory_list: str, tasks: dict
) -> str:
    lines = [
        "[系统提醒]",
        f"当前时间：{now}",
        f"记忆库有{count}条需要整理的记忆（位置{start}-{end}）：\n",
        memory_list,
        "\n根据你们之间的相处和对她的了解，审视这些记忆并提交修改。",
    ]

    task_lines = []
    constraint_lines = []

    if tasks.get("disclosure"):
        task_lines.append("为没有 disclosure 的记忆补充 disclosure（什么情境下应该想起这条记忆），已有的如果觉得不准确可以修正。")
    if tasks.get("merge"):
        task_lines.append("找出内容重复或高度相似的记忆进行合并。合并后的单条记忆不超过100字。")
    else:
        constraint_lines.append("不要合并或删除记忆。")
    if tasks.get("outdated"):
        task_lines.append("更新已经过时或不再准确的信息。")
    else:
        constraint_lines.append("不要重写记忆内容。")
    if tasks.get("classify"):
        task_lines.append("修正分类不准确的记忆，修正tags（不超过3个精准关键词）。")

    # If only disclosure is selected, restrict changes
    if tasks.get("disclosure") and not any(tasks.get(k) for k in ("merge", "outdated", "classify")):
        constraint_lines.append("不要修改记忆的内容、分类、tags。")

    lines.extend(task_lines)
    lines.extend(constraint_lines)

    # JSON output format instructions
    lines.append("")
    lines.append("请用 JSON 格式返回结果，不要包含其他文字：")
    lines.append("```json")
    lines.append("{")
    lines.append('  "changes": [')

    # Build format examples based on selected tasks
    examples = []
    if any(tasks.get(k) for k in ("outdated", "classify", "disclosure")):
        update_fields = []
        if tasks.get("outdated"):
            update_fields.append('"content": "新内容"')
        if tasks.get("classify"):
            update_fields.append('"klass": "新分类"')
            update_fields.append('"tags": {"topic": ["关键词"]}')
        if tasks.get("disclosure"):
            update_fields.append('"disclosure": "触发条件"')
        examples.append('    {"action": "update", "memory_id": ID, ' + ", ".join(update_fields) + "}")
    if tasks.get("merge"):
        examples.append('    {"action": "merge", "memory_id": 被合并ID, "merge_into": 目标ID, "content": "合并后内容"}')

    lines.append(",\n".join(examples))
    lines.append("  ],")
    lines.append('  "reasoning": "简要说明修改原因"')
    lines.append("}")
    lines.append("```")
    lines.append("其中 update 的各字段都是可选的，只传需要修改的字段。如果没有需要修改的，返回空 changes 数组。")

    return "\n".join(lines)


def _check_should_trigger(db: Session) -> dict | None:
    """Check if auto-reflection should trigger. Returns trigger info or None."""
    # Check if reflection is enabled
    enabled_row = db.query(Settings).filter(Settings.key == "reflection_enabled").first()
    if enabled_row and enabled_row.value and enabled_row.value.lower() not in ("true", "1", "yes"):
        return None

    # Check threshold
    threshold_row = db.query(Settings).filter(Settings.key == "reflection_threshold").first()
    threshold = _DEFAULT_THRESHOLD
    if threshold_row:
        try:
            threshold = int(threshold_row.value)
        except (ValueError, TypeError):
            pass

    # Count new memories since last reflection
    last_log = db.query(ReflectionLog).order_by(desc(ReflectionLog.created_at)).first()
    since = last_log.created_at if last_log else None
    q = db.query(func.count(Memory.id)).filter(Memory.deleted_at.is_(None), Memory.is_pending == False)
    if since is not None:
        q = q.filter(Memory.created_at > since)
    new_count = q.scalar() or 0

    if new_count < threshold:
        return None

    # Check idle: last message >= 10 min ago
    last_msg_time = db.query(func.max(Message.created_at)).scalar()
    if last_msg_time is not None:
        if last_msg_time.tzinfo is None:
            last_msg_time = last_msg_time.replace(tzinfo=TZ_EAST8)
        if datetime.now(TZ_EAST8) - last_msg_time < timedelta(minutes=_IDLE_MINUTES):
            return None

    # Latest memories first, cap at 100 per batch
    total = db.query(func.count(Memory.id)).filter(Memory.deleted_at.is_(None), Memory.is_pending == False).scalar() or 0
    batch = min(new_count, 100)
    end = total
    start = max(1, total - batch + 1)

    return {"count": batch, "start": start, "end": end}


_reflection_running = False


async def reflection_loop() -> None:
    """Background loop that checks and triggers auto-reflection."""
    global _reflection_running
    from app.database import SessionLocal

    logger.info("[reflection] auto-reflection loop started")

    while True:
        await asyncio.sleep(_CHECK_INTERVAL)
        if _reflection_running:
            logger.info("[reflection] skipping check, reflection already running")
            continue
        try:
            db = SessionLocal()
            try:
                trigger_info = _check_should_trigger(db)
            finally:
                db.close()

            if trigger_info is None:
                continue

            logger.info(
                "[reflection] auto-trigger: %d new memories, range %d-%d",
                trigger_info["count"], trigger_info["start"], trigger_info["end"],
            )

            _reflection_running = True
            try:
                await _send_reflection_trigger(trigger_info)
            finally:
                _reflection_running = False

        except Exception:
            _reflection_running = False
            logger.exception("[reflection] auto-reflection loop error")


async def _send_reflection_trigger(trigger_info: dict) -> None:
    """Send reflection trigger using summary model (direct JSON, no tool use)."""
    from app.database import SessionLocal
    from app.services.summary_service import _call_model_raw

    ASSISTANT_ID = 2

    db = SessionLocal()
    try:
        assistant = db.get(Assistant, ASSISTANT_ID)
        if not assistant:
            logger.warning("[reflection] assistant %d not found", ASSISTANT_ID)
            return

        # Resolve summary model preset
        preset = None
        if assistant.summary_model_preset_id:
            preset = db.get(ModelPreset, assistant.summary_model_preset_id)
        if not preset and assistant.summary_fallback_preset_id:
            preset = db.get(ModelPreset, assistant.summary_fallback_preset_id)
        if not preset:
            preset = db.get(ModelPreset, assistant.model_preset_id)
        if not preset:
            logger.error("[reflection] no model preset available")
            return

        now = datetime.now(TZ_EAST8)

        # Query memories in the specified range
        start_pos = trigger_info["start"]
        end_pos = trigger_info["end"]
        offset = start_pos - 1
        limit = end_pos - start_pos + 1
        from sqlalchemy import text as sa_text
        rows = db.execute(sa_text(
            "SELECT id, content, klass, tags, disclosure FROM memories "
            "WHERE deleted_at IS NULL AND is_pending = FALSE "
            "ORDER BY created_at ASC LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset}).all()
        memory_lines = []
        for r in rows:
            parts = [f"#{r.id} [{r.klass}] {r.content}"]
            if r.disclosure:
                parts.append(f"  disclosure: {r.disclosure}")
            if r.tags:
                tags = r.tags if isinstance(r.tags, dict) else json.loads(r.tags) if r.tags else {}
                topic = tags.get("topic", [])
                if topic:
                    parts.append(f"  tags: {', '.join(topic)}")
            memory_lines.append("\n".join(parts))
        memory_list = "\n".join(memory_lines)

        tasks = trigger_info.get("tasks", {"disclosure": True, "merge": True, "outdated": True, "classify": True})

        # Build system prompt from assistant personality
        system_prompt = (
            f"{assistant.system_prompt}\n\n"
            "---\n"
            "你现在进入「记忆反思」模式。\n"
            "你需要审视记忆条目，找出需要修正的问题。\n"
            "你的目标是让记忆库保持准确、简洁、无重复。"
        )

        # Build user prompt with task-specific instructions and JSON format requirement
        user_prompt = _build_reflection_prompt(
            now=now.strftime("%Y-%m-%d %H:%M"),
            count=trigger_info["count"],
            start=trigger_info["start"],
            end=trigger_info["end"],
            memory_list=memory_list,
            tasks=tasks,
        )

        logger.info(
            "[reflection] calling summary model %s (preset=%s) for range %d-%d",
            preset.model_name, preset.name, start_pos, end_pos,
        )

        # Call summary model (COT records written automatically by _call_model_raw)
        raw = await asyncio.to_thread(
            _call_model_raw, db, preset, system_prompt, user_prompt,
            timeout=120, source="反思",
        )

        logger.debug("[reflection] raw response: %s", raw[:500] if raw else "(empty)")

        # Parse JSON from response
        svc = ReflectionService.__new__(ReflectionService)
        result = svc._parse_json(raw)
        if result is None:
            logger.warning("[reflection] failed to parse model response as JSON")
            log_entry = ReflectionLog(
                memory_count=trigger_info["count"],
                changes={"changes": [], "reasoning": "模型返回无法解析的JSON"},
                model_used=preset.model_name,
            )
            db.add(log_entry)
            db.commit()
            return

        changes = result.get("changes", [])
        reasoning = result.get("reasoning", "")

        # Save reflection log first to get ID
        log_entry = ReflectionLog(
            memory_count=trigger_info["count"],
            changes={"changes": changes, "reasoning": reasoning},
            model_used=preset.model_name,
        )
        db.add(log_entry)
        db.flush()

        # Save as pending changes instead of applying directly
        svc.session_factory = None
        pending = svc._save_pending_changes(db, changes, log_entry.id)

        db.commit()

        logger.info(
            "[reflection] completed (range %d-%d) – %d pending changes saved",
            start_pos, end_pos, len(pending),
        )

    except Exception:
        logger.exception("[reflection] reflection trigger failed")
    finally:
        db.close()
