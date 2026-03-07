from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from .config import BOTS_CONFIG

logger = logging.getLogger(__name__)

# Single dispatcher shared by all bots (context passed via feed_update kwargs)
dp = Dispatcher(storage=MemoryStorage())

# Bot instances keyed by bot_key ("acheng", "ahuai")
bots: dict[str, Bot] = {}

for _key, _cfg in BOTS_CONFIG.items():
    _token = _cfg["token"]
    if not _token:
        logger.warning("No token for bot %s, skipping", _key)
        continue
    bots[_key] = Bot(
        token=_token,
        default=DefaultBotProperties(parse_mode=None),
    )
