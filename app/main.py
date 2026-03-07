from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO)

import jwt
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.routers import (
    api_providers,
    assistants,
    auth,
    chat,
    core_blocks,
    cot,
    diary,
    maintenance,
    memories,
    messages,
    model_presets,
    settings,
    sessions,
    theater,
    upload,
    user_profile,
    world_books,
    pending_memories,
)
from app.routers.auth import require_auth_token
from app.telegram.router import router as telegram_router
from app.telegram.bot_instance import bots
from app.telegram.config import BOTS_CONFIG, WEBHOOK_BASE_URL
from app.cot_broadcaster import cot_broadcaster
from app.database import engine
from app.models.models import Base

logger = logging.getLogger(__name__)

app = FastAPI(title="Chuli Home Backend", docs_url=None, redoc_url=None)


def _run_migrations(eng) -> None:
    """Add columns that create_all won't add to existing tables."""
    from sqlalchemy import text, inspect
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
            logger.info("Added qq_message_id JSONB column to messages")
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
    # world_books.message_mode
    if "world_books" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("world_books")]
        if "message_mode" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE world_books ADD COLUMN message_mode VARCHAR(16)"))
            logger.info("Added message_mode column to world_books")

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


@app.on_event("startup")
async def on_startup() -> None:
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.warning("create_all failed (tables may already exist): %s", exc)
    try:
        _run_migrations(engine)
    except Exception as exc:
        logger.warning("migration failed: %s", exc)
    cot_broadcaster.set_loop(asyncio.get_running_loop())
    print(f"[startup] bots to register: {list(bots.keys())}")
    from aiogram.types import MenuButtonWebApp, WebAppInfo
    from app.telegram.config import MINI_APP_URL

    for key, bot in bots.items():
        webhook_url = f"{WEBHOOK_BASE_URL}{BOTS_CONFIG[key]['webhook_path']}"
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=True)
            print(f"[startup] Webhook set for {key}: {webhook_url}")
            logger.info("Telegram webhook set for %s: %s", key, webhook_url)
        except Exception as exc:
            print(f"[startup] Webhook FAILED for {key}: {exc}")
            logger.warning("Failed to set webhook for %s: %s", key, exc)
        # Set menu button (default for all private chats)
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="WHISPER",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                ),
            )
            print(f"[startup] Menu button set for {key}")
        except Exception as exc:
            print(f"[startup] Menu button FAILED for {key}: {exc}")
            logger.warning("Failed to set menu button for %s: %s", key, exc)
    # Start proactive message loop
    from app.services.proactive_service import proactive_loop
    asyncio.create_task(proactive_loop())
    logger.info("Proactive message loop started")
    # Start daily summary merge cron
    from app.services.summary_service import daily_merge_cron
    asyncio.create_task(daily_merge_cron())
    logger.info("Daily summary merge cron started")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for key, bot in bots.items():
        try:
            await bot.delete_webhook()
        except Exception:
            pass
        try:
            await bot.session.close()
        except Exception:
            pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
auth_deps = [Depends(require_auth_token)]
app.include_router(chat.router, prefix="/api", tags=["chat"], dependencies=auth_deps)
app.include_router(messages.router, prefix="/api", tags=["messages"], dependencies=auth_deps)
app.include_router(sessions.router, prefix="/api", tags=["sessions"], dependencies=auth_deps)
app.include_router(assistants.router, prefix="/api", tags=["assistants"], dependencies=auth_deps)
app.include_router(user_profile.router, prefix="/api", tags=["user_profile"], dependencies=auth_deps)
app.include_router(memories.router, prefix="/api", tags=["memories"], dependencies=auth_deps)
app.include_router(core_blocks.router, prefix="/api", tags=["core_blocks"], dependencies=auth_deps)
app.include_router(world_books.router, prefix="/api", tags=["world_books"], dependencies=auth_deps)
app.include_router(diary.router, prefix="/api", tags=["diary"], dependencies=auth_deps)
app.include_router(maintenance.router, prefix="/api", tags=["maintenance"], dependencies=auth_deps)
app.include_router(settings.router, prefix="/api", tags=["settings"], dependencies=auth_deps)
app.include_router(theater.router, prefix="/api", tags=["theater"], dependencies=auth_deps)
app.include_router(api_providers.router, prefix="/api", tags=["api_providers"], dependencies=auth_deps)
app.include_router(model_presets.router, prefix="/api", tags=["model_presets"], dependencies=auth_deps)
app.include_router(cot.router, prefix="/api", tags=["cot"], dependencies=auth_deps)
app.include_router(pending_memories.router, prefix="/api", tags=["pending_memories"], dependencies=auth_deps)
app.include_router(upload.router, prefix="/api", tags=["upload"], dependencies=auth_deps)
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(telegram_router, tags=["telegram"])
from app.qq.router import router as qq_router
app.include_router(qq_router, tags=["qq"])

# ── WebSocket: real-time COT push ──
@app.websocket("/ws/cot")
async def ws_cot(ws: WebSocket):
    # Accept first so the 101 upgrade always happens (avoids proxy issues)
    await ws.accept()
    logger.info("[WS COT] Connection accepted from %s", ws.client)

    # Validate token (parsed manually to avoid DI issues with WebSocket)
    token = ws.query_params.get("token", "")
    secret = os.getenv("WHISPER_SECRET") or os.getenv("WHISPER_PASSWORD")
    if not secret:
        logger.warning("[WS COT] No auth secret configured, closing")
        await ws.close(code=4001, reason="Auth not configured")
        return
    if not token:
        logger.warning("[WS COT] No token provided, closing")
        await ws.close(code=4002, reason="Missing token")
        return
    try:
        jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        logger.warning("[WS COT] Invalid token: %s", e)
        await ws.close(code=4003, reason="Invalid token")
        return

    logger.info("[WS COT] Authenticated, registering client")
    cot_broadcaster.connect(ws)
    await cot_broadcaster.replay_to(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        cot_broadcaster.disconnect(ws)


# ── WebSocket: remote terminal bridge ──
@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    await ws.accept()
    logger.info("[WS Terminal] Connection accepted from %s", ws.client)

    token = ws.query_params.get("token", "")
    secret = os.getenv("WHISPER_SECRET") or os.getenv("WHISPER_PASSWORD")
    if not secret or not token:
        await ws.close(code=4002, reason="Missing auth")
        return
    try:
        jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        logger.warning("[WS Terminal] Invalid token: %s", e)
        await ws.close(code=4003, reason="Invalid token")
        return

    from app.services.terminal_bridge import bridge
    import json as _json

    if bridge.is_online():
        await ws.close(code=4009, reason="Another terminal already connected")
        return

    loop = asyncio.get_event_loop()
    bridge.set_connection(ws, loop)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = _json.loads(raw)
                req_id = data.get("id")
                result = _json.dumps(data.get("result", {}))
                if req_id:
                    bridge.on_result(req_id, result)
            except _json.JSONDecodeError:
                logger.warning("[WS Terminal] Bad JSON: %s", raw[:200])
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("[WS Terminal] Error: %s", e)
    finally:
        bridge.clear_connection()


# Serve uploaded static files
_static_dir = Path(__file__).parent.parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Serve miniapp frontend (HTML with no-cache, assets with long cache)
_miniapp_dir = Path(__file__).parent.parent / "miniapp" / "dist"
if _miniapp_dir.is_dir():
    from starlette.responses import FileResponse

    @app.get("/miniapp/{full_path:path}")
    async def _serve_miniapp(full_path: str = ""):
        file_path = _miniapp_dir / full_path if full_path else _miniapp_dir / "index.html"
        if file_path.is_file():
            headers = {}
            if file_path.suffix == ".html" or not full_path:
                headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return FileResponse(file_path, headers=headers)
        # SPA fallback
        return FileResponse(_miniapp_dir / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/")
async def root():
    return {"status": "online", "message": "阿怀正在听。"}
