from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import asyncio
import logging
import os

import jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.cot_broadcaster import cot_broadcaster
from app.lifecycle import on_shutdown, on_startup
from app.router_registry import register_routes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Companion Backend", docs_url=None, redoc_url=None)
app.add_event_handler("startup", on_startup)
app.add_event_handler("shutdown", on_shutdown)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_routes(app)


@app.websocket("/ws/cot")
async def ws_cot(ws: WebSocket):
    # Accept first so the 101 upgrade always happens.
    await ws.accept()
    logger.info("[WS COT] Connection accepted from %s", ws.client)

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

    logger.info("[WS COT] Authenticated, replaying then registering client")
    await cot_broadcaster.replay_to(ws)
    cot_broadcaster.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        cot_broadcaster.disconnect(ws)


@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    await ws.accept()
    logger.info("[WS Terminal] Connection accepted from %s", ws.client)

    token = ws.query_params.get("token", "")
    device_name = ws.query_params.get("name", "default")
    if not token:
        await ws.close(code=4002, reason="Missing auth")
        return

    secret = os.getenv("WHISPER_SECRET") or os.getenv("WHISPER_PASSWORD")
    jwt_ok = False
    if secret:
        try:
            jwt.decode(token, secret, algorithms=["HS256"])
            jwt_ok = True
        except jwt.InvalidTokenError:
            pass
    if not jwt_ok:
        from app.database import SessionLocal
        from app.routers.auth import verify_device_token

        db = SessionLocal()
        try:
            if not verify_device_token(token, db):
                logger.warning("[WS Terminal] Invalid token")
                await ws.close(code=4003, reason="Invalid token")
                return
        finally:
            db.close()

    from app.services.terminal_bridge import bridge
    import json as _json

    if bridge.is_online(device_name):
        await ws.close(code=4009, reason=f"Device '{device_name}' already connected")
        return

    loop = asyncio.get_event_loop()
    bridge.set_connection(device_name, ws, loop)
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
        bridge.clear_connection(device_name)


_static_dir = Path(__file__).parent.parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

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
        return FileResponse(
            _miniapp_dir / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )


@app.get("/")
async def root():
    from starlette.responses import RedirectResponse

    return RedirectResponse("/miniapp/")
