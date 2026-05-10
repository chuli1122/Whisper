import logging

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)


def run_migrations(eng) -> None:
    """Add columns that create_all will not add to existing tables."""
    insp = inspect(eng)
    # session_summaries.deleted_at
    if "session_summaries" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("session_summaries")]
        if "deleted_at" not in cols:
            with eng.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE session_summaries ADD COLUMN deleted_at TIMESTAMPTZ"
                ))
            logger.info("Added deleted_at column to session_summaries")
    # messages columns
    if "messages" in insp.get_table_names():
        cols = {c["name"]: c for c in insp.get_columns("messages")}
        if "summary_group_id" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN summary_group_id INTEGER"))
            logger.info("Added summary_group_id column to messages")
        # Backfill summary_group_id from existing session_summaries
        if "session_summaries" in insp.get_table_names():
            with eng.begin() as conn:
                result = conn.execute(text(
                    "UPDATE messages m SET summary_group_id = s.id "
                    "FROM session_summaries s "
                    "WHERE m.session_id = s.session_id "
                    "AND m.id BETWEEN s.msg_id_start AND s.msg_id_end "
                    "AND m.summary_group_id IS NULL "
                    "AND s.deleted_at IS NULL"
                ))
                if result.rowcount:
                    logger.info("Backfilled summary_group_id for %d messages", result.rowcount)
        if "telegram_message_id" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN telegram_message_id JSONB"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_messages_tgmid_gin ON messages USING GIN(telegram_message_id)"
                ))
            logger.info("Added telegram_message_id JSONB column to messages")
        else:
            col_type = str(cols["telegram_message_id"]["type"]).upper()
            if "JSON" not in col_type:
                with eng.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE messages ALTER COLUMN telegram_message_id "
                        "TYPE JSONB USING CASE WHEN telegram_message_id IS NOT NULL "
                        "THEN jsonb_build_array(telegram_message_id) ELSE NULL END"
                    ))
                    conn.execute(text("DROP INDEX IF EXISTS ix_messages_telegram_message_id"))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_messages_tgmid_gin ON messages USING GIN(telegram_message_id)"
                    ))
                logger.info("Migrated telegram_message_id from BIGINT to JSONB")
        if "qq_message_id" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN qq_message_id JSONB"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_messages_qqmid_gin ON messages USING GIN(qq_message_id)"
                ))
            logger.info("Added qq_message_id column to messages")
        if "wechat_message_id" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN wechat_message_id JSONB"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_messages_wxmid_gin ON messages USING GIN(wechat_message_id)"
                ))
            logger.info("Added wechat_message_id column to messages")
        if "image_data" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN image_data TEXT"))
            logger.info("Added image_data column to messages")
    # diary new columns
    if "diary" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("diary")]
        with eng.begin() as conn:
            if "assistant_id" not in cols:
                conn.execute(text("ALTER TABLE diary ADD COLUMN assistant_id INTEGER REFERENCES assistants(id)"))
            if "author" not in cols:
                conn.execute(text("ALTER TABLE diary ADD COLUMN author VARCHAR(16) NOT NULL DEFAULT 'assistant'"))
            if "unlock_at" not in cols:
                conn.execute(text("ALTER TABLE diary ADD COLUMN unlock_at TIMESTAMPTZ"))
            if "deleted_at" not in cols:
                conn.execute(text("ALTER TABLE diary ADD COLUMN deleted_at TIMESTAMPTZ"))
            if "read_at" not in cols:
                conn.execute(text("ALTER TABLE diary ADD COLUMN read_at TIMESTAMPTZ"))
            if "notified_at" not in cols:
                conn.execute(text("ALTER TABLE diary ADD COLUMN notified_at TIMESTAMPTZ"))
    # world_books.message_mode
    if "world_books" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("world_books")]
        if "message_mode" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE world_books ADD COLUMN message_mode VARCHAR(16)"))
            logger.info("Added message_mode column to world_books")

    # memes table
    if "memes" not in insp.get_table_names():
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE memes ("
                "  id SERIAL PRIMARY KEY,"
                "  term VARCHAR(100) NOT NULL,"
                "  category VARCHAR(50),"
                "  type VARCHAR(50),"
                "  content JSONB NOT NULL DEFAULT '{}',"
                "  keywords TEXT[],"
                "  created_at TIMESTAMPTZ DEFAULT now(),"
                "  updated_at TIMESTAMPTZ DEFAULT now()"
                ")"
            ))
        logger.info("Created memes table")
    else:
        cols = {c["name"]: c for c in insp.get_columns("memes")}
        if "content" in cols and "JSON" not in str(cols["content"]["type"]).upper():
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE memes ALTER COLUMN content DROP DEFAULT"))
                conn.execute(text(
                    "ALTER TABLE memes ALTER COLUMN content TYPE JSONB "
                    "USING CASE "
                    "WHEN content IS NULL OR btrim(content::text) = '' THEN '{}'::jsonb "
                    "ELSE jsonb_build_object('usage', content::text) "
                    "END"
                ))
                conn.execute(text("ALTER TABLE memes ALTER COLUMN content SET DEFAULT '{}'::jsonb"))
            logger.info("Migrated memes.content to JSONB")

    # model_presets.thinking_budget
    if "model_presets" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("model_presets")]
        if "thinking_budget" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE model_presets ADD COLUMN thinking_budget INTEGER NOT NULL DEFAULT 0"))
            logger.info("Added thinking_budget column to model_presets")

    # session_summaries.merged_into
    if "session_summaries" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("session_summaries")]
        if "merged_into" not in cols:
            with eng.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE session_summaries ADD COLUMN merged_into VARCHAR(20)"
                ))
            logger.info("Added merged_into column to session_summaries")

    # pending_memories.tags
    if "pending_memories" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("pending_memories")]
        if "tags" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE pending_memories ADD COLUMN tags JSONB NOT NULL DEFAULT '{}'"))
            logger.info("Added tags column to pending_memories")

    # memories.updated_at
    if "memories" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("memories")]
        if "updated_at" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE memories ADD COLUMN updated_at TIMESTAMPTZ"))
            logger.info("Added updated_at column to memories")

    # memories.is_pending
    if "memories" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("memories")]
        if "is_pending" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE memories ADD COLUMN is_pending BOOLEAN NOT NULL DEFAULT FALSE"))
            logger.info("Added is_pending column to memories")

    # pending_memories.memory_id
    if "pending_memories" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("pending_memories")]
        if "memory_id" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE pending_memories ADD COLUMN memory_id INTEGER REFERENCES memories(id)"))
            logger.info("Added memory_id column to pending_memories")

    # agent memory/chat tables: add server-side default for created_at
    for tbl in ("yoru_memory", "yoru_chat", "rin_memory", "rin_chat"):
        if tbl in insp.get_table_names():
            with eng.begin() as conn:
                conn.execute(text(f"ALTER TABLE {tbl} ALTER COLUMN created_at SET DEFAULT now()"))
            logger.info("Set DB-level default on %s.created_at", tbl)
