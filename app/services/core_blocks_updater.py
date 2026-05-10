from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session, sessionmaker

from app.models.models import (
    ApiProvider,
    Assistant,
    CoreBlock,
    CoreBlockCandidate,
    CoreBlockHistory,
    ModelPreset,
    SessionSummary,
)
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)


_DEFAULT_AUTO_REWRITE_THRESHOLD = 10


class CoreBlocksUpdater:
    def __init__(self, session_factory: sessionmaker, adopt_threshold: int = 2) -> None:
        self.session_factory = session_factory
        self.adopt_threshold = max(adopt_threshold, 1)

    def collect_signals_from_summary(
        self, session_summary_id: int, assistant_id: int
    ) -> dict[str, int]:
        db: Session = self.session_factory()
        stats = {"created": 0, "merged": 0, "adopted": 0, "duplicate": 0}
        try:
            summary = db.get(SessionSummary, session_summary_id)
            assistant = db.get(Assistant, assistant_id)
            if not summary or not assistant:
                return stats
            summary_text = (summary.summary_content or "").strip()
            if not summary_text:
                return stats

            candidates = self._extract_candidates(db, assistant, summary_text)
            if not candidates:
                return stats

            for item in candidates:
                block_type = str(item.get("block_type", "")).strip().lower()
                content = str(item.get("content", "")).strip()
                outcome = self._upsert_candidate(
                    db=db,
                    assistant=assistant,
                    source_summary_id=summary.id,
                    block_type=block_type,
                    content=content,
                )
                if outcome in stats:
                    stats[outcome] += 1

            db.commit()

            # Auto-rewrite if adopted count meets threshold
            try:
                self._maybe_auto_rewrite(db, assistant_id)
            except Exception:
                logger.exception("Auto-rewrite check failed")

            return stats
        except Exception:
            db.rollback()
            logger.exception(
                "Core block signal collection failed (summary_id=%s, assistant_id=%s).",
                session_summary_id,
                assistant_id,
            )
            return stats
        finally:
            db.close()

    def _maybe_auto_rewrite(self, db: Session, assistant_id: int) -> None:
        """Check if adopted candidates have reached threshold and auto-trigger rewrite."""
        from app.models.models import Settings

        # Check if core blocks auto-rewrite is enabled
        enabled_row = db.query(Settings).filter(Settings.key == "core_blocks_enabled").first()
        if enabled_row and enabled_row.value and enabled_row.value.lower() not in ("true", "1", "yes"):
            return

        # Read threshold from Settings
        threshold = _DEFAULT_AUTO_REWRITE_THRESHOLD
        row = db.query(Settings).filter(Settings.key == "core_blocks_auto_rewrite_threshold").first()
        if row:
            try:
                threshold = int(row.value)
            except (ValueError, TypeError):
                pass

        adopted_count = (
            db.query(CoreBlockCandidate)
            .filter(
                CoreBlockCandidate.status == "adopted",
                CoreBlockCandidate.assistant_id == assistant_id,
            )
            .count()
        )
        if adopted_count >= threshold:
            logger.info(
                "[core_blocks] Auto-rewrite triggered: %d adopted (threshold=%d)",
                adopted_count,
                threshold,
            )
            # rewrite_adopted_candidates creates its own db session
            import threading
            threading.Thread(
                target=self.rewrite_adopted_candidates,
                kwargs={"assistant_id": assistant_id},
                daemon=True,
            ).start()

    def rewrite_adopted_candidates(
        self, assistant_id: int | None = None
    ) -> dict[str, int]:
        db: Session = self.session_factory()
        result = {"rewritten_blocks": 0, "processed_candidates": 0}
        try:
            query = db.query(CoreBlockCandidate).filter(
                CoreBlockCandidate.status == "adopted"
            )
            if assistant_id is not None:
                query = query.filter(CoreBlockCandidate.assistant_id == assistant_id)
            adopted_rows = (
                query.order_by(
                    CoreBlockCandidate.assistant_id.asc(),
                    CoreBlockCandidate.block_type.asc(),
                    CoreBlockCandidate.created_at.asc(),
                    CoreBlockCandidate.id.asc(),
                ).all()
            )
            if not adopted_rows:
                return result

            grouped: dict[tuple[int | None, str], list[CoreBlockCandidate]] = {}
            for row in adopted_rows:
                key = (row.assistant_id, row.block_type)
                grouped.setdefault(key, []).append(row)

            processed_ids: list[int] = []
            for (target_assistant_id, block_type), rows in grouped.items():
                assistant = None
                if target_assistant_id is not None:
                    assistant = db.get(Assistant, target_assistant_id)
                if assistant is None:
                    assistant = db.query(Assistant).order_by(Assistant.id.asc()).first()

                block = (
                    db.query(CoreBlock)
                    .filter(
                        CoreBlock.block_type == block_type,
                        CoreBlock.assistant_id == target_assistant_id,
                    )
                    .first()
                )
                current_content = (block.content if block else "") or ""
                candidate_contents = [
                    (row.content or "").strip() for row in rows if (row.content or "").strip()
                ]
                if not candidate_contents:
                    processed_ids.extend([row.id for row in rows])
                    continue

                rewritten_content = self._rewrite_block_content(
                    db=db,
                    assistant=assistant,
                    block_type=block_type,
                    current_content=current_content,
                    candidate_contents=candidate_contents,
                )
                if not rewritten_content:
                    processed_ids.extend([row.id for row in rows])
                    continue

                now_utc = datetime.now(TZ_EAST8)
                if block:
                    if rewritten_content != (block.content or ""):
                        history = CoreBlockHistory(
                            core_block_id=block.id,
                            block_type=block.block_type,
                            assistant_id=block.assistant_id,
                            content=block.content or "",
                            version=block.version,
                        )
                        db.add(history)
                        block.content = rewritten_content
                        block.version += 1
                        block.updated_at = now_utc
                        result["rewritten_blocks"] += 1
                else:
                    block = CoreBlock(
                        block_type=block_type,
                        assistant_id=target_assistant_id,
                        content=rewritten_content,
                        version=1,
                        updated_at=now_utc,
                    )
                    db.add(block)
                    result["rewritten_blocks"] += 1

                processed_ids.extend([row.id for row in rows])

            if processed_ids:
                db.query(CoreBlockCandidate).filter(
                    CoreBlockCandidate.id.in_(processed_ids)
                ).delete(synchronize_session=False)
            result["processed_candidates"] = len(processed_ids)
            db.commit()
            return result
        except Exception:
            db.rollback()
            logger.exception(
                "Core block rewrite failed (assistant_id=%s).",
                assistant_id,
            )
            return result
        finally:
            db.close()

    def _extract_candidates(
        self, db: Session, assistant: Assistant, summary_text: str
    ) -> list[dict[str, Any]]:
        human_block = (
            db.query(CoreBlock)
            .filter(
                CoreBlock.block_type == "human",
                CoreBlock.assistant_id == assistant.id,
            )
            .first()
        )
        persona_block = (
            db.query(CoreBlock)
            .filter(
                CoreBlock.block_type == "persona",
                CoreBlock.assistant_id == assistant.id,
            )
            .first()
        )
        current_human = (human_block.content if human_block else "") or ""
        current_persona = (persona_block.content if persona_block else "") or ""

        system_prompt = (
            "从摘要文本中提取稳定的 core-block 更新信号。"
            "只返回 JSON，格式：{\"candidates\":[{\"block_type\":\"human|persona\",\"content\":\"...\"}]}。"
            "只提取跨会话持久有效的更新，跳过临时闲聊。"
            "human block：AI 视角下对用户的理解——行为模式、情绪习惯、沟通风格、互动特点。"
            "不要包含固定档案信息（生日、性别、星座、饮食禁忌等），那些属于 basic info。"
            "persona block：AI 在相处中形成的自我认知——自己的性格特点、行为倾向、情绪模式，以及与用户之间形成的默契和不言而喻的共识。"
        )
        user_prompt = (
            f"Assistant name: {assistant.name}\n"
            f"[Current human block]\n{current_human}\n\n"
            f"[Current persona block]\n{current_persona}\n\n"
            f"[New summary]\n{summary_text}"
        )

        payload = self._call_json_with_fallback(
            db=db,
            assistant=assistant,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if not payload:
            return []

        raw_candidates = payload.get("candidates", [])
        if not isinstance(raw_candidates, list):
            return []

        valid: list[dict[str, Any]] = []
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("block_type", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if block_type not in {"human", "persona"}:
                continue
            if not content:
                continue
            valid.append({"block_type": block_type, "content": content})
        return valid

    def _upsert_candidate(
        self,
        db: Session,
        assistant: Assistant,
        source_summary_id: int,
        block_type: str,
        content: str,
    ) -> str:
        if block_type not in {"human", "persona"} or not content:
            return "duplicate"

        existing_block = (
            db.query(CoreBlock)
            .filter(
                CoreBlock.block_type == block_type,
                CoreBlock.assistant_id == assistant.id,
            )
            .first()
        )

        if existing_block:
            relation = self._classify_relation(
                db=db,
                assistant=assistant,
                first_text=content,
                second_text=existing_block.content or "",
            )
            if relation == "duplicate":
                candidate = CoreBlockCandidate(
                    block_type=block_type,
                    assistant_id=assistant.id,
                    content=content,
                    source_summary_id=source_summary_id,
                    status="duplicate",
                    occurrence_count=1,
                )
                db.add(candidate)
                return "duplicate"

        pending_rows = (
            db.query(CoreBlockCandidate)
            .filter(
                CoreBlockCandidate.block_type == block_type,
                CoreBlockCandidate.assistant_id == assistant.id,
                CoreBlockCandidate.status == "pending",
            )
            .order_by(CoreBlockCandidate.created_at.desc(), CoreBlockCandidate.id.desc())
            .all()
        )
        for row in pending_rows:
            relation = self._classify_relation(
                db=db,
                assistant=assistant,
                first_text=content,
                second_text=row.content or "",
            )
            if relation == "duplicate":
                row.occurrence_count = (row.occurrence_count or 1) + 1
                row.source_summary_id = source_summary_id
                if row.occurrence_count >= self.adopt_threshold:
                    row.status = "adopted"
                    return "adopted"
                return "merged"

        status = "pending"
        if self.adopt_threshold <= 1:
            status = "adopted"
        candidate = CoreBlockCandidate(
            block_type=block_type,
            assistant_id=assistant.id,
            content=content,
            source_summary_id=source_summary_id,
            status=status,
            occurrence_count=1,
        )
        db.add(candidate)
        return "created" if status == "pending" else "adopted"

    def _rewrite_block_content(
        self,
        db: Session,
        assistant: Assistant | None,
        block_type: str,
        current_content: str,
        candidate_contents: list[str],
    ) -> str:
        candidate_lines = "\n".join(f"- {item}" for item in candidate_contents)
        if not assistant:
            merged = [current_content.strip()] + [item.strip() for item in candidate_contents]
            return "\n".join(item for item in merged if item)

        system_prompt = (
            "将 core memory block 重写为简洁一致的文本。"
            "只返回 JSON：{\"content\": \"...\"}，不要 markdown。"
            "human block 用 AI 的视角写对用户的理解，persona block 用 AI 的视角写对自己的认知。"
            "控制在 500 字以内。"
        )
        user_prompt = (
            f"Target block_type: {block_type}\n"
            f"Assistant: {assistant.name}\n"
            f"[Current block]\n{current_content}\n\n"
            f"[Adopted candidates]\n{candidate_lines}\n\n"
            "Rewrite into one clean block without duplicates or contradictions."
        )
        payload = self._call_json_with_fallback(
            db=db,
            assistant=assistant,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if payload and isinstance(payload, dict):
            rewritten = str(payload.get("content", "")).strip()
            if rewritten:
                char_count = len(rewritten)
                if char_count > 600:
                    logger.warning(
                        "Core block rewrite exceeded 600 chars (block_type=%s, assistant_id=%s, chars=%d).",
                        block_type,
                        assistant.id if assistant else None,
                        char_count,
                    )
                return rewritten
        merged = [current_content.strip()] + [item.strip() for item in candidate_contents]
        return "\n".join(item for item in merged if item)

    def _classify_relation(
        self,
        db: Session,
        assistant: Assistant,
        first_text: str,
        second_text: str,
    ) -> str:
        left = " ".join(first_text.lower().split())
        right = " ".join(second_text.lower().split())
        if not left or not right:
            return "different"
        if left == right:
            return "duplicate"
        if left in right or right in left:
            return "duplicate"

        system_prompt = (
            "判断两条信息之间的关系。"
            "只返回 JSON：{\"relation\": \"duplicate|conflict|different\"}。"
        )
        user_prompt = f"statement_a: {first_text}\nstatement_b: {second_text}"
        payload = self._call_json_with_fallback(
            db=db,
            assistant=assistant,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if not payload:
            return "different"
        relation = str(payload.get("relation", "")).strip().lower()
        if relation in {"duplicate", "conflict", "different"}:
            return relation
        return "different"

    def _call_json_with_fallback(
        self,
        db: Session,
        assistant: Assistant,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any] | None:
        primary = self._resolve_primary_preset(db, assistant)
        if not primary:
            return None
        fallback = self._resolve_fallback_preset(db, assistant)

        try:
            return self._call_model_json(db, primary, system_prompt, user_prompt)
        except Exception:
            logger.exception(
                "Primary core-block model call failed (assistant_id=%s, preset_id=%s).",
                assistant.id,
                primary.id,
            )
            if fallback and fallback.id != primary.id:
                try:
                    return self._call_model_json(
                        db, fallback, system_prompt, user_prompt
                    )
                except Exception:
                    logger.exception(
                        "Fallback core-block model call failed (assistant_id=%s, preset_id=%s).",
                        assistant.id,
                        fallback.id,
                    )
        return None

    def _resolve_primary_preset(
        self, db: Session, assistant: Assistant
    ) -> ModelPreset | None:
        if assistant.summary_model_preset_id:
            preset = db.get(ModelPreset, assistant.summary_model_preset_id)
            if preset:
                return preset
        summary_named = db.query(ModelPreset).filter(ModelPreset.name == "summary").first()
        if summary_named:
            return summary_named
        return db.get(ModelPreset, assistant.model_preset_id)

    def _resolve_fallback_preset(
        self, db: Session, assistant: Assistant
    ) -> ModelPreset | None:
        if not assistant.summary_fallback_preset_id:
            return None
        return db.get(ModelPreset, assistant.summary_fallback_preset_id)

    def _call_model_json(
        self,
        db: Session,
        preset: ModelPreset,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        api_provider = db.get(ApiProvider, preset.api_provider_id)
        if not api_provider:
            raise ValueError(f"API provider not found for preset_id={preset.id}")

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
        response = client.chat.completions.create(**params)
        if not response.choices:
            raise ValueError("Core-block model response contained no choices.")
        content = response.choices[0].message.content or ""
        cleaned_content = content.strip()
        if cleaned_content.startswith("```json"):
            cleaned_content = cleaned_content[len("```json") :]
        elif cleaned_content.startswith("```"):
            cleaned_content = cleaned_content[len("```") :]
        if cleaned_content.endswith("```"):
            cleaned_content = cleaned_content[: -len("```")]
        cleaned_content = cleaned_content.strip()
        # Handle "Extra data" — model may return multiple JSON objects
        try:
            payload = json.loads(cleaned_content)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            payload, _ = decoder.raw_decode(cleaned_content)
        if not isinstance(payload, dict):
            raise ValueError("Core-block model response is not a JSON object.")
        return payload
