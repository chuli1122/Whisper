from __future__ import annotations

import os

# Bot tokens — one per assistant
BOT_TOKEN_ACHENG: str = os.getenv("TELEGRAM_BOT_TOKEN_ACHENG", "")
BOT_TOKEN_AHUAI: str = os.getenv("TELEGRAM_BOT_TOKEN_AHUAI", "")

# Only respond to this chat_id. 0 = not configured (allow all in dev).
_raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "0")
ALLOWED_CHAT_ID: int = int(_raw_chat_id) if _raw_chat_id.lstrip("-").isdigit() else 0

# Mini App URLs
_default_base_url = os.getenv("WEBHOOK_BASE_URL", "http://localhost:8002").rstrip("/")
MINI_APP_URL: str = os.getenv("MINI_APP_URL", f"{_default_base_url}/miniapp/#/cot")
MINI_APP_BASE_URL: str = os.getenv("MINI_APP_BASE_URL", f"{_default_base_url}/miniapp/")

# Webhook
WEBHOOK_BASE_URL: str = os.getenv("WEBHOOK_BASE_URL", _default_base_url)

# Defaults
DEFAULT_BUFFER_SECONDS: float = 15.0

# Per-bot configuration: bot_key → { token, assistant_id, webhook_path }
BOTS_CONFIG: dict[str, dict] = {
    "ahuai": {
        "token": BOT_TOKEN_AHUAI,
        "assistant_id": 1,
        "webhook_path": "/telegram/webhook/ahuai",
    },
    "acheng": {
        "token": BOT_TOKEN_ACHENG,
        "assistant_id": 2,
        "webhook_path": "/telegram/webhook/acheng",
    },
}
