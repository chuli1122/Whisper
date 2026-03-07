from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from .config import MINI_APP_URL


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with three buttons."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="₍ᐢ·͈༝·͈ᐢ₎⁺ ⊹˚.⋆", web_app=WebAppInfo(url=MINI_APP_URL)),
                KeyboardButton(text="ずっと一緒·˚", request_location=True),
                KeyboardButton(text="♥ にいにいがすき☆°"),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
