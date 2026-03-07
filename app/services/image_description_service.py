from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from openai import OpenAI
import anthropic
from sqlalchemy.orm import Session, sessionmaker

from app.models.models import (
    ApiProvider,
    Assistant,
    Message,
    ModelPreset,
    Settings,
)

logger = logging.getLogger(__name__)

# ── File extraction constants ────────────────────────────────────────────────

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".json", ".csv", ".ts", ".html", ".css",
    ".yaml", ".yml", ".xml", ".sh", ".bash", ".sql", ".log", ".ini", ".cfg",
    ".conf", ".toml", ".env", ".jsx", ".tsx", ".java", ".go", ".rs", ".c",
    ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".r", ".lua",
}


# ── Token estimation (same logic as ChatService) ────────────────────────────

def _estimate_tokens(text: str) -> int:
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


# ── File content extraction ──────────────────────────────────────────────────

def extract_file_content(filename: str, data: bytes) -> str:
    """Extract text content from file bytes based on extension."""
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""
    elif ext == ".pdf":
        return _extract_pdf(data)
    else:
        return ""


def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages)
    except ImportError:
        logger.warning("pdfplumber not installed, cannot extract PDF")
        return ""
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return ""


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text so its estimated token count is ≤ max_tokens."""
    if _estimate_tokens(text) <= max_tokens:
        return text
    # Binary search for the right cutoff
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "\n...(内容已截断)"


def get_trigger_threshold(db: Session) -> int:
    """Read dialogue_trigger_threshold from Settings."""
    try:
        row = db.query(Settings).filter(Settings.key == "dialogue_trigger_threshold").first()
        if row:
            return int(row.value)
    except Exception:
        pass
    return 16000


# ── Model resolution (mirrors summary_service pattern) ──────────────────────

def _resolve_primary_preset(db: Session, assistant: Assistant) -> ModelPreset | None:
    if assistant.summary_model_preset_id:
        preset = db.get(ModelPreset, assistant.summary_model_preset_id)
        if preset:
            return preset
    summary_named = db.query(ModelPreset).filter(ModelPreset.name == "summary").first()
    if summary_named:
        return summary_named
    return db.get(ModelPreset, assistant.model_preset_id)


def _resolve_fallback_preset(db: Session, assistant: Assistant) -> ModelPreset | None:
    if not assistant.summary_fallback_preset_id:
        return None
    return db.get(ModelPreset, assistant.summary_fallback_preset_id)


# ── Multimodal API call ─────────────────────────────────────────────────────

def _call_model_with_images(
    db: Session,
    preset: ModelPreset,
    system_prompt: str,
    images: list[dict[str, str]],
    user_text: str,
) -> str:
    """Call model with text + images.

    images: list of {"media_type": "image/jpeg", "data": "<base64>"}
    Returns raw text response.
    """
    api_provider = db.get(ApiProvider, preset.api_provider_id)
    if not api_provider:
        raise ValueError(f"API provider not found for preset_id={preset.id}")

    if api_provider.auth_type == "oauth_token":
        # Anthropic native
        anth_client = anthropic.Anthropic(
            auth_token=api_provider.api_key,
            default_headers={
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14",
                "user-agent": "claude-cli/2.1.2 (external, cli)",
                "x-app": "cli",
            },
        )
        content_blocks: list[dict[str, Any]] = []
        for img in images:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
        content_blocks.append({"type": "text", "text": user_text})

        anth_kwargs: dict[str, Any] = {
            "model": preset.model_name,
            "system": system_prompt,
            "messages": [{"role": "user", "content": content_blocks}],
            "max_tokens": preset.max_tokens,
        }
        if preset.temperature is not None:
            anth_kwargs["temperature"] = preset.temperature

        response = anth_client.messages.create(**anth_kwargs)
        result = ""
        for block in response.content:
            if block.type == "text":
                result += block.text
        return result
    else:
        # OpenAI-compatible
        base_url = api_provider.base_url
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
            if not base_url.endswith("/v1"):
                base_url = f"{base_url.rstrip('/')}/v1"
        oai_client = OpenAI(api_key=api_provider.api_key, base_url=base_url)

        content_parts: list[dict[str, Any]] = []
        for img in images:
            data_url = f"data:{img['media_type']};base64,{img['data']}"
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
        content_parts.append({"type": "text", "text": user_text})

        params: dict[str, Any] = {
            "model": preset.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts},
            ],
            "max_tokens": preset.max_tokens,
        }
        if preset.temperature is not None:
            params["temperature"] = preset.temperature

        response = oai_client.chat.completions.create(**params)
        if not response.choices:
            raise ValueError("Response contained no choices.")
        return response.choices[0].message.content or ""


def _call_model_text(
    db: Session,
    preset: ModelPreset,
    system_prompt: str,
    user_text: str,
) -> str:
    """Call model with text only (for file summarization). Reuses summary_service logic."""
    from app.services.summary_service import _call_model_raw

    return _call_model_raw(db, preset, system_prompt, user_text)


# ── Image description ────────────────────────────────────────────────────────

def describe_images(
    session_factory: sessionmaker,
    session_id: int,
    assistant_id: int,
) -> None:
    """Batch-describe all pending images in a session.

    Queries messages with non-null image_data, calls model to describe them,
    updates content from [图片] to [图片：description], clears image_data.
    """
    db: Session = session_factory()
    try:
        pending = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.image_data.isnot(None),
            )
            .order_by(Message.id.asc())
            .all()
        )
        if not pending:
            return

        logger.info(
            "[ImageDesc] Processing %d images (session=%s, assistant=%s)",
            len(pending), session_id, assistant_id,
        )

        assistant = db.get(Assistant, assistant_id)
        if not assistant:
            logger.warning("[ImageDesc] Assistant not found: %s", assistant_id)
            return

        primary = _resolve_primary_preset(db, assistant)
        fallback = _resolve_fallback_preset(db, assistant)
        if not primary:
            logger.warning("[ImageDesc] No model preset available")
            return

        # Build image list
        images: list[dict[str, str]] = []
        for msg in pending:
            data_url = msg.image_data or ""
            if not data_url.startswith("data:"):
                continue
            try:
                meta_part, b64_data = data_url.split(",", 1)
                media_type = meta_part.split(":")[1].split(";")[0]
                images.append({"media_type": media_type, "data": b64_data})
            except Exception:
                logger.warning("[ImageDesc] Failed to parse image_data for msg %d", msg.id)

        if not images:
            return

        user_prompt = (
            f"请简短描述以下{len(images)}张图片的内容，每张一行。\n"
            "格式：\n"
            "图片1: 描述\n"
            "图片2: 描述\n"
            "...\n"
            "每个描述不超过30字，只写画面内容。"
        )
        system_prompt = "你是图片描述助手。简短准确地描述图片内容。"

        result: str | None = None
        try:
            result = _call_model_with_images(db, primary, system_prompt, images, user_prompt)
        except Exception:
            logger.exception("[ImageDesc] Primary model failed")
            if fallback and fallback.id != primary.id:
                try:
                    result = _call_model_with_images(db, fallback, system_prompt, images, user_prompt)
                except Exception:
                    logger.exception("[ImageDesc] Fallback model also failed")

        if not result:
            logger.warning("[ImageDesc] All models failed, image_data preserved for retry")
            return

        # Parse descriptions
        descriptions = _parse_descriptions(result, len(pending))

        # Update messages
        for i, msg in enumerate(pending):
            desc = descriptions[i] if i < len(descriptions) else "图片"
            # Replace [图片] marker in content
            old_content = msg.content or ""
            if "[图片]" in old_content:
                msg.content = old_content.replace("[图片]", f"[图片：{desc}]", 1)
            else:
                msg.content = f"[图片：{desc}]"
            msg.image_data = None

        db.commit()
        logger.info("[ImageDesc] Described %d images successfully", len(pending))

    except Exception:
        logger.exception("[ImageDesc] Failed (session=%s)", session_id)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _parse_descriptions(text: str, expected_count: int) -> list[str]:
    """Parse model output like '图片1: xxx\n图片2: yyy' into a list of descriptions."""
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    descriptions: list[str] = []
    for line in lines:
        # Try to match "图片N: description" or "N: description" or "N. description"
        match = re.match(r"(?:图片)?(\d+)[.:：]\s*(.+)", line)
        if match:
            descriptions.append(match.group(2).strip())
        elif len(descriptions) < expected_count:
            # Fallback: treat the whole line as a description
            descriptions.append(line)
    # Pad if model returned fewer lines
    while len(descriptions) < expected_count:
        descriptions.append("图片")
    return descriptions


# ── File summarization ───────────────────────────────────────────────────────

def summarize_file_messages(
    session_factory: sessionmaker,
    session_id: int,
    assistant_id: int,
) -> None:
    """Find messages with file content that needs summarization and summarize them."""
    db: Session = session_factory()
    try:
        # Find messages marked for file summary
        pending = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.meta_info.op("->>")(
                    "needs_file_summary"
                ) == "true",
            )
            .order_by(Message.id.asc())
            .all()
        )
        if not pending:
            return

        logger.info(
            "[FileSum] Processing %d file messages (session=%s)",
            len(pending), session_id,
        )

        assistant = db.get(Assistant, assistant_id)
        if not assistant:
            return

        primary = _resolve_primary_preset(db, assistant)
        fallback = _resolve_fallback_preset(db, assistant)
        if not primary:
            return

        for msg in pending:
            meta = msg.meta_info or {}
            file_name = meta.get("file_name", "unknown")
            content = msg.content or ""

            # Extract the file content part
            # Format: "[文件：filename]\n文件内容" or "caption\n\n[文件：filename]\n文件内容"
            file_marker_pattern = re.compile(r"\[文件：([^\]]+)\]\n")
            match = file_marker_pattern.search(content)
            if not match:
                # No file marker found, skip
                meta.pop("needs_file_summary", None)
                msg.meta_info = {**meta}
                continue

            marker_end = match.end()
            caption_part = content[:match.start()].strip()
            file_content = content[marker_end:]

            if not file_content.strip():
                meta.pop("needs_file_summary", None)
                msg.meta_info = {**meta}
                continue

            # Summarize
            system_prompt = "你是文件概括助手。请用中文简短概括以下文件的主要内容，不超过500字。只输出概括内容。"
            user_text = f"文件名：{file_name}\n\n{file_content}"

            summary: str | None = None
            try:
                summary = _call_model_text(db, primary, system_prompt, user_text)
            except Exception:
                logger.exception("[FileSum] Primary model failed for msg %d", msg.id)
                if fallback and fallback.id != primary.id:
                    try:
                        summary = _call_model_text(db, fallback, system_prompt, user_text)
                    except Exception:
                        logger.exception("[FileSum] Fallback also failed for msg %d", msg.id)

            if summary and summary.strip():
                if caption_part:
                    msg.content = f"{caption_part}\n\n[文件：{file_name}] {summary.strip()}"
                else:
                    msg.content = f"[文件：{file_name}] {summary.strip()}"
            else:
                if caption_part:
                    msg.content = f"{caption_part}\n\n[文件：{file_name}，概括失败，原文过长已丢弃]"
                else:
                    msg.content = f"[文件：{file_name}，概括失败，原文过长已丢弃]"

            meta.pop("needs_file_summary", None)
            msg.meta_info = {**meta}

        db.commit()
        logger.info("[FileSum] Summarized %d file messages", len(pending))

    except Exception:
        logger.exception("[FileSum] Failed (session=%s)", session_id)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
