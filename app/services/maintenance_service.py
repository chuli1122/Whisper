from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.models import Memory
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)


class MaintenanceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def cleanup_expired_memories(self, threshold: float = 0.05) -> int:
        now_utc = datetime.now(TZ_EAST8)
        candidates = (
            self.db.query(Memory)
            .filter(
                Memory.klass.in_(["ephemeral", "task"]),
                Memory.deleted_at.is_(None),
                Memory.is_pending.is_(False),
            )
            .all()
        )
        deleted_count = 0
        for memory in candidates:
            created_at = memory.created_at
            if created_at is None:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=TZ_EAST8)
            else:
                created_at = created_at.astimezone(timezone.utc)

            last_access_ts = memory.last_access_ts
            if last_access_ts is not None:
                if last_access_ts.tzinfo is None:
                    last_access_ts = last_access_ts.replace(tzinfo=TZ_EAST8)
                else:
                    last_access_ts = last_access_ts.astimezone(timezone.utc)
                age_base = last_access_ts
            else:
                age_base = created_at

            age_days = (now_utc - age_base).total_seconds() / 86400.0
            base = min(
                max((memory.importance or 0.5) + (memory.manual_boost or 0.0), 0.0), 1.0
            )
            halflife = memory.halflife_days or 60.0
            boost = 1 + 0.35 * math.log(1 + (memory.hits or 0))
            decayed_score = (
                base * math.exp(-math.log(2) / halflife * age_days) * boost
            )
            if decayed_score < threshold:
                memory.deleted_at = now_utc
                deleted_count += 1

        self.db.commit()
        return deleted_count

    def merge_similar_memories(self, similarity_threshold: float = 0.90) -> int:
        pair_rows = self.db.execute(
            text(
                """
SELECT a.id AS id_a,
       b.id AS id_b,
       a.created_at AS created_at_a,
       b.created_at AS created_at_b,
       1 - (a.embedding <=> b.embedding) AS similarity
FROM memories a
JOIN memories b ON a.id < b.id
WHERE a.embedding IS NOT NULL
  AND b.embedding IS NOT NULL
  AND a.deleted_at IS NULL
  AND b.deleted_at IS NULL
  AND a.is_pending = FALSE
  AND b.is_pending = FALSE
  AND 1 - (a.embedding <=> b.embedding) > :threshold
ORDER BY similarity DESC
LIMIT 50
"""
            ),
            {"threshold": similarity_threshold},
        ).all()

        deleted_ids: set[int] = set()
        deleted_count = 0
        for row in pair_rows:
            id_a = row.id_a
            id_b = row.id_b
            if id_a in deleted_ids or id_b in deleted_ids:
                continue

            created_at_a = row.created_at_a
            created_at_b = row.created_at_b
            if created_at_a is None or created_at_b is None:
                delete_id = id_a if id_a < id_b else id_b
            else:
                if created_at_a.tzinfo is None:
                    created_at_a = created_at_a.replace(tzinfo=TZ_EAST8)
                else:
                    created_at_a = created_at_a.astimezone(timezone.utc)
                if created_at_b.tzinfo is None:
                    created_at_b = created_at_b.replace(tzinfo=TZ_EAST8)
                else:
                    created_at_b = created_at_b.astimezone(timezone.utc)

                if created_at_a < created_at_b:
                    delete_id = id_a
                elif created_at_b < created_at_a:
                    delete_id = id_b
                else:
                    delete_id = id_a if id_a < id_b else id_b

            memory = self.db.get(Memory, delete_id)
            if not memory:
                deleted_ids.add(delete_id)
                continue

            memory.deleted_at = datetime.now(TZ_EAST8)
            deleted_ids.add(delete_id)
            deleted_count += 1

        self.db.commit()
        return deleted_count

    def cleanup_trash(self, retention_days: int = 30) -> int:
        cutoff = datetime.now(TZ_EAST8) - timedelta(days=retention_days)
        rows = (
            self.db.query(Memory)
            .filter(
                Memory.deleted_at.is_not(None),
                Memory.deleted_at < cutoff,
            )
            .all()
        )
        deleted_count = 0
        for row in rows:
            self.db.delete(row)
            deleted_count += 1
        self.db.commit()
        return deleted_count

    def run_all(self) -> dict[str, int]:
        result = {"expired_cleaned": 0, "similar_merged": 0, "trash_cleaned": 0}

        try:
            result["expired_cleaned"] = self.cleanup_expired_memories()
        except Exception as exc:
            logger.warning("cleanup_expired_memories failed: %s", exc)
            result["expired_cleaned"] = -1

        try:
            result["similar_merged"] = self.merge_similar_memories()
        except Exception as exc:
            logger.warning("merge_similar_memories failed: %s", exc)
            result["similar_merged"] = -1

        try:
            result["trash_cleaned"] = self.cleanup_trash()
        except Exception as exc:
            logger.warning("cleanup_trash failed: %s", exc)
            result["trash_cleaned"] = -1

        try:
            from app.services.media_service import cleanup_old_files
            result["media_cleaned"] = cleanup_old_files()
        except Exception as exc:
            logger.warning("cleanup_old_files failed: %s", exc)
            result["media_cleaned"] = -1

        return result
