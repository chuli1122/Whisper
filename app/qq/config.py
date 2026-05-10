from __future__ import annotations

import os

# NapCat HTTP API (same VPS, Docker mapped port)
NAPCAT_API_URL: str = os.getenv("NAPCAT_API_URL", "http://localhost:3000")

# Only respond to these QQ user IDs (comma-separated). {0} means no restriction.
_raw = os.getenv("QQ_ALLOWED_USER_IDS", "0")
QQ_ALLOWED_USER_IDS: set[int] = {
    int(x) for x in _raw.split(",") if x.strip() and x.strip().isdigit()
}

# Assistant used for QQ messages in the demo.
QQ_ASSISTANT_ID: int = int(os.getenv("QQ_ASSISTANT_ID", "1"))

# Group-mention trigger: fires when any sender in QQ_GROUP_ALLOWED_SENDERS @-mentions QQ_BOT_UIN in QQ_GROUP_ID.
QQ_GROUP_ID: int = int(os.getenv("QQ_GROUP_ID", "0"))
QQ_BOT_UIN: int = int(os.getenv("QQ_BOT_UIN", "0"))
QQ_OWNER_UID: int = int(os.getenv("QQ_OWNER_UID", "0"))

# Whitelisted QQ ids that can trigger a reply by @-ing the bot in the group.
QQ_GROUP_ALLOWED_SENDERS: set[int] = {
    int(x) for x in os.getenv("QQ_GROUP_ALLOWED_SENDERS", "0").split(",")
    if x.strip().isdigit()
}
