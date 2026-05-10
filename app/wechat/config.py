from __future__ import annotations

import os

# iLink API base URL
ILINK_BASE_URL: str = os.getenv("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")

# Only respond to these WeChat user IDs (comma-separated, format: xxx@im.wechat).
# Empty string means no restriction.
_raw = os.getenv("WECHAT_ALLOWED_USER_IDS", "")
WECHAT_ALLOWED_USER_IDS: set[str] = {
    x.strip() for x in _raw.split(",") if x.strip()
}

# WeChat is always 助手A
WECHAT_ASSISTANT_ID: int = 2
