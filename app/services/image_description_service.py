from __future__ import annotations

import io
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.models import Settings

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
