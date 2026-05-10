"""Mood / emotion detection utilities extracted from chat_service."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NEGATIVE_MOOD_TAGS = [
    "sad",
    "angry",
    "anxious",
    "tired",
    "emo",
]

# ── Real-time emotion keyword detection ──────────────────────────────────────
# Keyword groups → detected mood. Checked against current user message.
EMOTION_KEYWORDS: dict[str, list[str]] = {
    "angry": ["生气", "气死", "发火", "怒", "烦死", "讨厌", "恨", "滚"],
    "sad": ["难受", "伤心", "哭", "眼泪", "心疼", "崩溃", "绝望", "想死", "不想活"],
    "anxious": ["焦虑", "害怕", "担心", "紧张", "慌", "怕", "不安"],
    "tired": ["累", "困", "疲", "不想动", "好乏", "没力气"],
    "emo": ["孤独", "寂寞", "无聊", "空虚", "没意思", "不想", "算了"],
    "happy": ["开心", "高兴", "嘿嘿", "哈哈", "好耶", "太好了", "喜欢"],
    "flirty": ["想你", "抱抱", "亲亲", "哥哥", "呜呜", "嘤", "宝宝", "爱你", "么么"],
}

# Mood → klass weight multipliers for recall
MOOD_KLASS_WEIGHTS: dict[str, dict[str, float]] = {
    "angry": {"conflict": 1.5, "bond": 1.3},
    "sad": {"bond": 1.5, "relationship": 1.2},
    "anxious": {"bond": 1.3},
    "tired": {"bond": 1.2},
    "emo": {"bond": 1.3, "relationship": 1.2},
    "flirty": {"bond": 1.5, "preference": 1.2},
}


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

def _load_emotion_config(db: Session) -> tuple[dict[str, list[str]], dict[str, dict[str, float]]]:
    """Load emotion keywords and weights from Settings, falling back to hardcoded defaults."""
    keywords = dict(EMOTION_KEYWORDS)
    weights = dict(MOOD_KLASS_WEIGHTS)
    try:
        kw_row = db.query(Settings).filter(Settings.key == "emotion_keywords").first()
        if kw_row and kw_row.value:
            keywords = json.loads(kw_row.value)
    except Exception:
        pass
    try:
        wt_row = db.query(Settings).filter(Settings.key == "mood_klass_weights").first()
        if wt_row and wt_row.value:
            weights = json.loads(wt_row.value)
    except Exception:
        pass
    return keywords, weights


def _detect_mood_from_text(text: str, db: Session | None = None) -> str | None:
    """Detect mood from user message using keyword hit count."""
    if db is not None:
        keywords, _ = _load_emotion_config(db)
    else:
        keywords = EMOTION_KEYWORDS
    text_lower = text.lower()
    best_mood = None
    best_count = 0
    for mood, kws in keywords.items():
        count = 0
        for kw in kws:
            count += text_lower.count(kw)
        if count > best_count:
            best_count = count
            best_mood = mood
    return best_mood
