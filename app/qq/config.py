from __future__ import annotations

import os

# NapCat HTTP API (same VPS, Docker mapped port)
NAPCAT_API_URL: str = os.getenv("NAPCAT_API_URL", "http://localhost:3000")

# Only respond to these QQ user IDs (comma-separated). {0} means no restriction.
_raw = os.getenv("QQ_ALLOWED_USER_IDS", "0")
QQ_ALLOWED_USER_IDS: set[int] = {
    int(x) for x in _raw.split(",") if x.strip() and x.strip().isdigit()
}

# QQ is always 阿澄
QQ_ASSISTANT_ID: int = 2
