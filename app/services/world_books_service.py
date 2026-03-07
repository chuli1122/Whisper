from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.models import Assistant, Settings, WorldBook

logger = logging.getLogger(__name__)


@dataclass
class MountedBook:
    book_id: int
    position: str
    sort_order: int


class WorldBooksService:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _parse_rule_set_ids(raw: list | None) -> list[MountedBook]:
        """Parse rule_set_ids from assistant/theater_card.

        Supports two formats:
        - New: [{"id": 1, "position": "before", "sort_order": 0}, ...]
        - Legacy: [1, 3, 5] (treated as position=after, sort_order=index)
        """
        if not raw:
            return []
        result: list[MountedBook] = []
        for idx, item in enumerate(raw):
            if isinstance(item, dict):
                try:
                    result.append(MountedBook(
                        book_id=int(item["id"]),
                        position=item.get("position", "after"),
                        sort_order=int(item.get("sort_order", idx)),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            else:
                try:
                    result.append(MountedBook(
                        book_id=int(item),
                        position="after",
                        sort_order=idx,
                    ))
                except (TypeError, ValueError):
                    continue
        return result

    def _get_current_chat_mode(self) -> str:
        row = self.db.query(Settings).filter(Settings.key == "message_mode").first()
        return row.value if row and row.value in ("short", "long", "theater") else "long"

    def get_active_books(
        self,
        assistant_id: int,
        user_message: str | None = None,
        current_mood_tag: str | None = None,
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {"before": [], "after": []}
        assistant = self.db.get(Assistant, assistant_id)
        if not assistant or not assistant.rule_set_ids:
            return result

        mounted = self._parse_rule_set_ids(assistant.rule_set_ids)
        if not mounted:
            return result

        book_ids = [m.book_id for m in mounted]
        books_by_id: dict[int, WorldBook] = {}
        if book_ids:
            books = (
                self.db.query(WorldBook)
                .filter(WorldBook.id.in_(book_ids))
                .all()
            )
            books_by_id = {b.id: b for b in books}

        user_message_lower = user_message.lower() if user_message is not None else None

        before_entries: list[tuple[int, str]] = []
        after_entries: list[tuple[int, str]] = []

        for m in mounted:
            book = books_by_id.get(m.book_id)
            if not book:
                continue

            is_active = False
            if book.activation == "always":
                is_active = True
            elif book.activation == "keyword":
                if user_message_lower is not None:
                    keywords = book.keywords if isinstance(book.keywords, list) else []
                    for keyword in keywords:
                        keyword_text = str(keyword).strip()
                        if keyword_text and keyword_text.lower() in user_message_lower:
                            is_active = True
                            break
            elif book.activation == "mood":
                if current_mood_tag and book.keywords:
                    try:
                        mood_value = current_mood_tag.strip().lower()
                        keywords = book.keywords if isinstance(book.keywords, list) else []
                        keyword_values = [str(k).strip().lower() for k in keywords if str(k).strip()]
                        if mood_value and mood_value in keyword_values:
                            is_active = True
                    except Exception as exc:
                        logger.warning("Mood activation check failed: %s", exc)
            elif book.activation == "message_mode":
                if book.message_mode:
                    current_mode = self._get_current_chat_mode()
                    if current_mode == book.message_mode:
                        is_active = True
            if not is_active:
                continue

            content = (book.content or "").strip()
            if not content:
                continue

            position = m.position if m.position in ("before", "after") else "after"
            if position == "before":
                before_entries.append((m.sort_order, content))
            else:
                after_entries.append((m.sort_order, content))

        before_entries.sort(key=lambda x: x[0])
        after_entries.sort(key=lambda x: x[0])

        result["before"] = [content for _, content in before_entries]
        result["after"] = [content for _, content in after_entries]

        return result
