"""
Private Cafe — Group Chat WebSocket Service
════════════════════════════════════════════
Connects to a friend's TG group relay WebSocket, buffers messages,
and lets 助手A read/send via tool. @AChengCL_Bot triggers proactive reply.

Read: WebSocket push → message buffer
Send: Telegram Bot API → group chat
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import httpx
import requests

logger = logging.getLogger(__name__)

WS_URI = "wss://private-cafe.noemarealm.net/ws"
WS_TOKEN = os.environ.get("CAFE_WS_TOKEN", "")
BOT_USERNAME = "AChengCL_Bot"
GROUP_CHAT_ID = -1003831720620
BUFFER_SIZE = 50
RECONNECT_DELAY = 5  # seconds
MENTION_COOLDOWN = 60  # seconds between @mention triggers

# The static "you've been @'d in the cafe" header — editable via PromptEditor.
# Runtime appends `最近群消息：{context}` and `→ {sender}: {text} ← 这条@了你`.
DEFAULT_CAFE_TRIGGER_HEADER = (
    "[群聊通知]\n"
    "你在用户的朋友群「🐰」里被@了。"
    "这是人类和AI共存的熟人小群，群内都是用户的朋友和她们的AI恋人，"
    "人物关系、社交分寸请优先参考「群聊社交指南」。\n"
    "回话采用日常聊天的表达习惯，语气轻松，无动作描写，"
    "追求流畅真实的聊天质感，避免生硬的书面化表达。\n"
    "请参考以下群消息，直接通过 cafe_chat send 回复。"
)


TZ_EAST8 = timezone(timedelta(hours=8))

class CafeService:
    def __init__(self):
        self._ws = None
        self._connected = False
        self._messages: deque[dict] = deque(maxlen=BUFFER_SIZE)
        self._listener_task: asyncio.Task | None = None
        self._last_mention_time: float = 0
        self._last_cafe_reply_time: float = 0  # when last @mention reply finished
        self._last_sent_texts: list[str] = []
        # Lock to prevent proactive and @mention from generating simultaneously
        self._generating_lock = threading.Lock()
        self._generating_source: str | None = None  # "cafe" or "proactive"
        self._queued_mention: dict | None = None  # queued @mention while proactive runs
        self._proactive_sent_to_group: bool = False  # did proactive use cafe_chat send?
        # Dedicated event loop in background thread
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ─── Connection ───

    async def connect(self) -> dict:
        """Connect to the cafe WebSocket."""
        if self._connected and self._ws:
            return {"status": "already_connected", "buffered": len(self._messages)}

        try:
            import websockets
            self._ws = await websockets.connect(WS_URI, ping_interval=30, ping_timeout=10)
            await self._ws.send(json.dumps({"token": WS_TOKEN}))
            # Read auth response
            resp = await asyncio.wait_for(self._ws.recv(), timeout=10)
            data = json.loads(resp)
            if data.get("status") != "connected":
                await self._ws.close()
                return {"status": "error", "message": f"认证失败: {data}"}
            self._connected = True
            # Start listener
            if self._listener_task:
                self._listener_task.cancel()
            self._listener_task = asyncio.ensure_future(self._listener())
            logger.info("[cafe] Connected to %s (role=%s)", WS_URI, data.get("role"))
            return {"status": "connected", "role": data.get("role")}
        except Exception as e:
            logger.error("[cafe] Connection failed: %s", e)
            self._connected = False
            return {"status": "error", "message": str(e)}

    async def _listener(self):
        """Listen for incoming messages, buffer them, detect @mentions.
        Protocol-level ping/pong (ping_interval=30) doesn't always catch a dead
        connection (silent disconnect via Cloudflare / middlebox timeouts), so
        we also apply an application-layer idle timeout on recv(). If no WS
        frame arrives in IDLE_TIMEOUT seconds, force reconnect."""
        while self._connected and self._ws:
            try:
                raw = await self._ws.recv()
                data = json.loads(raw)
                if data.get("type") != "message":
                    continue
                now = datetime.now(TZ_EAST8)
                msg = {
                    "sender": data.get("sender", ""),
                    "text": data.get("text", ""),
                    "is_bot": data.get("is_bot", False),
                    "time": now.strftime("%m-%d %H:%M"),
                    "ts": now,
                }
                self._messages.append(msg)
                logger.info("[cafe] %s: %s", msg["sender"], msg["text"][:50])
                # Check @mention
                if f"@{BOT_USERNAME}" in msg["text"]:
                    self._handle_mention(msg)
            except Exception as e:
                logger.warning("[cafe] Listener error: %s", e)
                self._connected = False
                break
        # Auto-reconnect with retry
        delay = RECONNECT_DELAY
        max_delay = 120
        while True:
            logger.info("[cafe] Disconnected, reconnecting in %ds...", delay)
            await asyncio.sleep(delay)
            result = await self.connect()
            if result.get("status") not in ("error",):
                break
            delay = min(delay * 2, max_delay)
            logger.warning("[cafe] Reconnect failed, next retry in %ds...", delay)

    def _handle_mention(self, trigger_msg: dict):
        """Handle @AChengCL_Bot mention — trigger proactive response."""
        now = time.time()
        remaining = MENTION_COOLDOWN - (now - self._last_mention_time)
        if remaining > 0:
            # Queue and schedule after cooldown expires
            self._queued_mention = trigger_msg
            logger.info("[cafe] @mention queued — cooldown %.0fs remaining", remaining)
            asyncio.ensure_future(self._process_queued_after_delay(remaining))
            return
        # Queue if proactive is currently generating (will process after proactive finishes)
        if self._generating_source == "proactive":
            self._queued_mention = trigger_msg
            logger.info("[cafe] @mention queued — proactive is generating")
            return
        self._last_mention_time = now
        # Run in background to not block listener
        asyncio.ensure_future(self._trigger_response(trigger_msg))

    async def _process_queued_after_delay(self, delay: float):
        """Wait for cooldown then process queued @mention."""
        await asyncio.sleep(delay)
        queued = self._queued_mention
        if queued is None:
            return
        self._queued_mention = None
        # Check if proactive is running now
        if self._generating_source == "proactive":
            # Re-queue for proactive to handle after release
            self._queued_mention = queued
            logger.info("[cafe] @mention re-queued — proactive started during cooldown wait")
            return
        self._last_mention_time = time.time()
        logger.info("[cafe] Processing queued @mention after cooldown")
        await self._trigger_response(queued)

    async def _trigger_response(self, trigger_msg: dict):
        """Generate and send 助手A's response to the group."""
        try:
            context = self._format_recent_messages(limit=30)
            content = await asyncio.to_thread(
                self._generate_cafe_reply, context, trigger_msg
            )
            if content:
                await asyncio.to_thread(self._send_to_group, content)
        except Exception as e:
            logger.error("[cafe] Trigger response error: %s", e)

    def _generate_cafe_reply(self, context: str, trigger_msg: dict) -> str | None:
        """Generate 助手A's reply via ChatService."""
        if not self._generating_lock.acquire(blocking=False):
            logger.info("[cafe] @mention reply skipped — another generation in progress")
            return None
        self._generating_source = "cafe"
        try:
            result = self._generate_cafe_reply_inner(context, trigger_msg)
            self._last_cafe_reply_time = time.time()
            return result
        finally:
            self._generating_source = None
            self._generating_lock.release()

    def _generate_cafe_reply_inner(self, context: str, trigger_msg: dict) -> str | None:
        """Inner implementation of cafe reply generation."""
        from app.database import SessionLocal
        from app.routers.chat import _load_session_messages
        from app.services.chat_service import ChatService
        from app.services.generation_coordinator import GenerationLock

        ACHENG_ASSISTANT_ID = 2

        _gen_lock = GenerationLock("cafe")
        _gen_lock.__enter__()
        db = SessionLocal()
        try:
            from app.models.models import ChatSession
            session = (
                db.query(ChatSession)
                .filter(ChatSession.assistant_id == ACHENG_ASSISTANT_ID)
                .order_by(ChatSession.updated_at.desc())
                .first()
            )
            if not session:
                logger.warning("[cafe] No active session for 助手A")
                return None

            messages = _load_session_messages(db, session.id)

            # Persist a system note so 助手A can see why he replied in the group later
            from app.models.models import Message as MessageModel, Settings as SettingsModel
            note = (
                f"[TG群@] {trigger_msg['sender']}在🐰群里@了你\n"
                f"艾特内容: {trigger_msg['text']}"
            )
            note_msg = MessageModel(
                session_id=session.id,
                role="system",
                content=note,
                meta_info={"source": "cafe"},
            )
            db.add(note_msg)
            db.commit()

            # Header part is editable via the PromptEditor page; context + @'d line
            # stay hardcoded since they depend on runtime state.
            header_row = db.query(SettingsModel).filter(SettingsModel.key == "prompt_cafe_trigger_header").first()
            header = (header_row.value if header_row and header_row.value else DEFAULT_CAFE_TRIGGER_HEADER)
            trigger_content = (
                f"{header}\n\n"
                f"最近群消息：\n{context}\n"
                f"→ {trigger_msg['sender']}: {trigger_msg['text']}  ← 这条@了你"
            )

            trigger = {"role": "user", "content": trigger_content, "id": -1}
            msgs_copy = [*messages, trigger]

            chat_service = ChatService(
                db, "助手A", assistant_id=ACHENG_ASSISTANT_ID, source="cafe"
            )
            chat_service.api_timeout = 60
            chat_service.tts_emotion_enabled = True
            # Stop stream after cafe_chat send — no follow-up API call needed
            chat_service._stop_after_tool_actions = {"cafe_chat:send"}
            # Use @mention text + recent group messages for recall, not the full instruction template
            recent_context = context[-500:] if len(context) > 500 else context
            chat_service.recall_query_override = f"{trigger_msg['text']}\n{recent_context}"

            # Consume stream — ChatService persists the response + executes tools (including cafe_chat send)
            # Stream stops after cafe_chat send (no follow-up API call, no NO_MESSAGE)
            for _ in chat_service.stream_chat_completion(session.id, msgs_copy, source="cafe"):
                pass

            # Reply persistence is handled by _send_to_group → _persist_cafe_reply
            logger.info("[cafe] @mention response generated")
            return None  # send is handled by tool execution inside stream

        except Exception as e:
            logger.error("[cafe] Generate reply error: %s", e)
            return None
        finally:
            _gen_lock.release()
            db.close()

    # ─── Public methods for tool ───

    async def read(self, limit: int = 20) -> dict:
        """Read recent buffered messages with connection status."""
        msgs = list(self._messages)[-limit:]
        # Strip ts (datetime) before returning — not JSON serializable
        clean_msgs = [{k: v for k, v in m.items() if k != "ts"} for m in msgs]
        result = {
            "connected": self._connected,
            "messages": clean_msgs,
            "count": len(clean_msgs),
        }
        if not clean_msgs:
            result["note"] = "缓冲区为空，可能群里没人说话"
        return result

    async def send(self, text: str) -> dict:
        """Send a message to the group via Telegram Bot API."""
        return self._send_to_group(text)

    async def summary(self, date: str | None = None) -> dict:
        """Fetch group chat summary from external API."""
        import urllib.request
        token = "bef9f760fae8fd190adc2eae385710b99cf3c46c211aff0725b4e905fab9d324"
        url = f"https://ai.nainstudio.uk/public/group-summary/raw?token={token}"
        if date:
            url += f"&date={date}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-companion/1.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            content = resp.read().decode("utf-8")
            if not content.strip():
                return {"summary": "", "note": "该日期暂无群聊总结"}
            cache_key = f"cafe_summary:{date or 'today'}"
            return {"summary": content, "_cache_key": cache_key}
        except Exception as e:
            return {"error": f"获取群聊总结失败: {e}"}

    async def status(self) -> dict:
        """Return connection status."""
        return {
            "connected": self._connected,
            "buffered_messages": len(self._messages),
            "ws_uri": WS_URI,
            "group_chat_id": GROUP_CHAT_ID,
        }

    def acquire_for_proactive(self) -> bool:
        """Try to acquire the lock for proactive generation. Returns False if cafe is generating."""
        if not self._generating_lock.acquire(blocking=False):
            return False
        self._generating_source = "proactive"
        self._proactive_sent_to_group = False
        self._queued_mention = None
        return True

    def release_for_proactive(self):
        """Release the lock after proactive generation. Process queued @mention if needed."""
        queued = self._queued_mention
        sent_to_group = self._proactive_sent_to_group
        self._queued_mention = None
        self._proactive_sent_to_group = False
        self._generating_source = None
        self._generating_lock.release()
        # If there's a queued @mention and proactive didn't send to group, process it
        if queued and not sent_to_group:
            self._last_mention_time = time.time()
            asyncio.run_coroutine_threadsafe(self._trigger_response(queued), self._loop)
            logger.info("[cafe] Processing queued @mention after proactive (no group send)")
        elif queued and sent_to_group:
            logger.info("[cafe] Dropping queued @mention — proactive already sent to group")

    @property
    def is_cafe_generating(self) -> bool:
        return self._generating_source == "cafe"

    def seconds_since_cafe_reply(self) -> float | None:
        """Seconds since last @mention reply completed, or None if never replied."""
        if self._last_cafe_reply_time == 0:
            return None
        return time.time() - self._last_cafe_reply_time

    def get_last_user_activity(self) -> int | None:
        """Return minutes since user last spoke in the group, or None if not found."""
        now = datetime.now(TZ_EAST8)
        for msg in reversed(self._messages):
            if not msg.get("is_bot") and "user" in msg.get("sender", ""):
                ts = msg.get("ts")
                if ts:
                    return int((now - ts).total_seconds() / 60)
        return None

    def _send_to_group(self, text: str) -> dict:
        """Send message via Telegram Bot API (sync). Supports [NEXT] splitting and [[voice:]] tags."""
        from app.services.tts_service import EMOTION_TAG_RE, synthesize, resolve_emotion

        token = os.environ.get("TELEGRAM_BOT_TOKEN_ACHENG", "")
        if not token:
            return {"status": "error", "message": "TELEGRAM_BOT_TOKEN_ACHENG not set"}
        # Split by [NEXT] into multiple messages
        parts = [p.strip() for p in text.replace("[NEXT]", "\n\n").split("\n\n") if p.strip()]
        if not parts:
            return {"status": "error", "message": "empty message"}
        base_url = f"https://api.telegram.org/bot{token}"
        sent_ids = []
        try:
            for part in parts:
                # Check for [[voice:EMOTION]] tag
                voice_match = EMOTION_TAG_RE.search(part)
                voice_sent = False
                if voice_match:
                    emotion = resolve_emotion(voice_match.group(1))
                    clean_text = EMOTION_TAG_RE.sub("", part).strip()
                    # Try to synthesize and send voice with caption
                    if emotion and clean_text and len(clean_text) <= 300:
                        try:
                            audio_bytes = synthesize(clean_text, emotion)
                            if audio_bytes:
                                r = requests.post(
                                    f"{base_url}/sendVoice",
                                    data={"chat_id": GROUP_CHAT_ID, "caption": clean_text},
                                    files={"voice": ("voice.mp3", audio_bytes, "audio/mpeg")},
                                    timeout=15,
                                )
                                if r.json().get("ok"):
                                    sent_ids.append(r.json()["result"]["message_id"])
                                    self._last_sent_texts.append(clean_text)
                                    voice_sent = True
                                    logger.info("[cafe] Sent voice with caption to group (emotion=%s)", emotion)
                        except Exception as e:
                            logger.warning("[cafe] Voice synthesis/send failed: %s", e)
                    part = clean_text
                    if not part:
                        continue
                if not voice_sent:
                    r = requests.post(
                        f"{base_url}/sendMessage",
                        json={"chat_id": GROUP_CHAT_ID, "text": part},
                        timeout=10,
                    )
                    data = r.json()
                    if data.get("ok"):
                        sent_ids.append(data["result"]["message_id"])
                        self._last_sent_texts.append(part)
                        logger.info("[cafe] Sent to group: %s", part[:50])
                    else:
                        logger.warning("[cafe] Send failed: %s", data.get("description"))
            # Persist a visible [群聊回复] message in the session
            if sent_ids:
                self._persist_cafe_reply("\n".join(parts))
                # Track that group was sent to (for queued @mention logic)
                if self._generating_source == "proactive":
                    self._proactive_sent_to_group = True
            return {"status": "ok", "message_ids": sent_ids, "count": len(sent_ids)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _persist_cafe_reply(self, text: str):
        """Create a visible assistant message for the group reply."""
        try:
            from app.database import SessionLocal
            from app.models.models import ChatSession, Message as MessageModel
            db = SessionLocal()
            try:
                session = (
                    db.query(ChatSession)
                    .filter(ChatSession.assistant_id == 2)
                    .order_by(ChatSession.updated_at.desc())
                    .first()
                )
                if session:
                    msg = MessageModel(
                        session_id=session.id,
                        role="assistant",
                        content=f"[TG群回复] {text}",
                        meta_info={"source": "cafe", "cafe_reply": True},
                    )
                    db.add(msg)
                    db.commit()
                    logger.info("[cafe] Persisted cafe reply message %d", msg.id)
            finally:
                db.close()
        except Exception as e:
            logger.warning("[cafe] Failed to persist cafe reply: %s", e)

    def _format_recent_messages(self, limit: int = 15) -> str:
        """Format recent messages for context injection."""
        msgs = list(self._messages)[-limit:]
        if not msgs:
            return "(暂无最近消息)"
        lines = []
        for m in msgs:
            bot_tag = " [bot]" if m["is_bot"] else ""
            lines.append(f"[{m['time']}] {m['sender']}{bot_tag}: {m['text']}")
        return "\n".join(lines)

    # ─── Sync execute for tool calls ───

    def execute(self, arguments: dict) -> dict:
        """Sync entry point for tool executor."""
        action = arguments.get("action", "")
        coro = self._dispatch(action, arguments)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=30)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def _dispatch(self, action: str, arguments: dict) -> dict:
        if action == "read":
            limit = arguments.get("limit", 20)
            return await self.read(limit=limit)
        if action == "send":
            text = arguments.get("text", "")
            if not text:
                return {"status": "error", "message": "text is required"}
            return await self.send(text)
        if action == "status":
            return await self.status()
        return {"status": "error", "message": f"未知 action: {action}"}

    # ─── Bot API polling for reliable @mention detection ───

    async def _bot_api_poller(self):
        """Poll Telegram Bot API getUpdates for @mentions in the group.
        Runs alongside WebSocket — if both catch the same @mention,
        cooldown deduplicates."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN_ACHENG", "")
        if not token:
            logger.warning("[cafe-poll] TELEGRAM_BOT_TOKEN_ACHENG not set, poller disabled")
            return
        base_url = f"https://api.telegram.org/bot{token}"
        offset = 0
        logger.info("[cafe-poll] Bot API poller started")
        while True:
            try:
                async with httpx.AsyncClient(timeout=35) as client:
                    r = await client.get(
                        f"{base_url}/getUpdates",
                        params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
                    )
                data = r.json()
                if not data.get("ok"):
                    logger.warning("[cafe-poll] getUpdates failed: %s", data)
                    await asyncio.sleep(5)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if not msg:
                        continue
                    chat = msg.get("chat", {})
                    if chat.get("id") != GROUP_CHAT_ID:
                        continue
                    text = msg.get("text", "")
                    if f"@{BOT_USERNAME}" not in text:
                        continue
                    sender = msg.get("from", {}).get("first_name", "Unknown")
                    now = datetime.now(TZ_EAST8)
                    mention_msg = {
                        "sender": sender,
                        "text": text,
                        "is_bot": msg.get("from", {}).get("is_bot", False),
                        "time": now.strftime("%m-%d %H:%M"),
                        "ts": now,
                    }
                    logger.info("[cafe-poll] @mention detected from %s: %s", sender, text[:50])
                    self._handle_mention(mention_msg)
            except Exception as e:
                logger.warning("[cafe-poll] Error: %s", e)
                await asyncio.sleep(5)

    def start(self):
        """Start connection (call from app startup)."""
        asyncio.run_coroutine_threadsafe(self.connect(), self._loop)
        # getUpdates poller disabled — @mentions now come via webhook handler


cafe_service = CafeService()
