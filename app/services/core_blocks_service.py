from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.models import Assistant, CoreBlock, CoreBlockHistory
from app.utils import TZ_EAST8


class CoreBlocksService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_block(
        self, block_type: str, assistant_id: int | None = None
    ) -> CoreBlock | None:
        if block_type == "human":
            return (
                self.db.query(CoreBlock)
                .filter(
                    CoreBlock.block_type == "human",
                    CoreBlock.assistant_id == assistant_id,
                )
                .first()
            )

        if block_type == "persona":
            return (
                self.db.query(CoreBlock)
                .filter(
                    CoreBlock.block_type == "persona",
                    CoreBlock.assistant_id == assistant_id,
                )
                .first()
            )
        return None

    def update_block(
        self, block_type: str, content: str, assistant_id: int | None = None
    ) -> CoreBlock:
        block = self.get_block(block_type, assistant_id)
        now_utc = datetime.now(TZ_EAST8)
        if block:
            history = CoreBlockHistory(
                core_block_id=block.id,
                block_type=block.block_type,
                assistant_id=block.assistant_id,
                content=block.content,
                version=block.version,
            )
            self.db.add(history)
            block.content = content
            block.version += 1
            block.updated_at = now_utc
        else:
            block = CoreBlock(
                block_type=block_type,
                assistant_id=assistant_id,
                content=content,
                version=1,
                updated_at=now_utc,
            )
            self.db.add(block)
        self.db.commit()
        self.db.refresh(block)
        return block

    def get_blocks_for_prompt(self, assistant_id: int) -> str:
        assistant = self.db.get(Assistant, assistant_id)
        if not assistant:
            return ""

        sections: list[str] = []
        human_block = self.get_block("human", assistant_id)
        if human_block and human_block.content and human_block.content.strip():
            sections.append(f"[About the user - what I know about her]\n{human_block.content.strip()}")

        persona_block = self.get_block("persona", assistant_id)
        if persona_block and persona_block.content and persona_block.content.strip():
            sections.append(
                f"[About myself - who I am]\n{persona_block.content.strip()}"
            )

        return "\n\n".join(sections)
