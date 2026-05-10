"""OAuth token management (Claude Code + Codex): billing header injection and auto-refresh."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Billing header (required since Claude Code 2.1.76+; Anthropic OAuth only) ──

_CC_VERSION = "2.1.77"
_CC_SALT = "59cf53e54c78"
_CC_ENTRYPOINT = "cli"
_CC_CCH = "00000"


def _sampled_js_utf16(text: str, indices: list[int]) -> str:
    """Sample UTF-16 code units at given indices, matching JS String behavior."""
    utf16_units = []
    for ch in text:
        code = ord(ch)
        if code <= 0xFFFF:
            utf16_units.append(code)
        else:
            code -= 0x10000
            utf16_units.append(0xD800 + (code >> 10))
            utf16_units.append(0xDC00 + (code & 0x3FF))
    result = []
    for idx in indices:
        if idx < len(utf16_units):
            result.append(chr(utf16_units[idx]))
        else:
            result.append("0")
    return "".join(result)


def _billing_version_hash(first_user_text: str) -> str:
    sampled = _sampled_js_utf16(first_user_text, [4, 7, 20])
    raw = f"{_CC_SALT}{sampled}{_CC_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:3]


def build_billing_system_block(first_user_text: str) -> dict[str, str]:
    h = _billing_version_hash(first_user_text)
    text = (
        f"x-anthropic-billing-header: cc_version={_CC_VERSION}.{h}; "
        f"cc_entrypoint={_CC_ENTRYPOINT}; cch={_CC_CCH};"
    )
    return {"type": "text", "text": text}


def inject_billing_header(kwargs: dict[str, Any]) -> None:
    """Inject billing header as first system block in Anthropic kwargs (mutates in place)."""
    system = kwargs.get("system")
    stable_text = ""
    if isinstance(system, list) and system:
        stable_text = system[0].get("text", "")[:50] if isinstance(system[0], dict) else ""
    elif isinstance(system, str):
        stable_text = system[:50]

    billing_block = build_billing_system_block(stable_text)
    logger.info("[billing-debug] header=%s", billing_block.get("text", "")[:80])

    system = kwargs.get("system")
    if system is None:
        kwargs["system"] = [billing_block]
    elif isinstance(system, list):
        system = [b for b in system
                  if not (isinstance(b, dict)
                          and b.get("text", "").lstrip().startswith("x-anthropic-billing-header:"))]
        system.insert(0, billing_block)
        kwargs["system"] = system
    elif isinstance(system, str):
        kwargs["system"] = [billing_block, {"type": "text", "text": system}]


# ── OAuth provider registry ──────────────────────────────────────────────────

_PROVIDERS: dict[str, dict[str, Any]] = {
    "claude": {
        "token_url": "https://platform.claude.com/v1/oauth/token",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "body_format": "json",
        "scope": None,  # Anthropic refresh doesn't need scope
        "settings_refresh_key": "oauth_refresh_token",
        "settings_expires_key": "oauth_expires_at",
    },
    "codex": {
        "token_url": "https://auth.openai.com/oauth/token",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "body_format": "form",
        "scope": "openid profile email offline_access",
        "settings_refresh_key": "codex_oauth_refresh_token",
        "settings_expires_key": "codex_oauth_expires_at",
    },
}

# auth_type → provider key mapping. "oauth_token" is the legacy value (always
# meant Claude), kept for backward compat until migrate-in-place runs.
_AUTH_TYPE_TO_PROVIDER = {
    "oauth_token": "claude",
    "oauth_claude": "claude",
    "oauth_codex": "codex",
}


def provider_from_auth_type(auth_type: str | None) -> str | None:
    if not auth_type:
        return None
    return _AUTH_TYPE_TO_PROVIDER.get(auth_type)


# ── Token cache + status ─────────────────────────────────────────────────────

# In-memory cache: provider_id → {access, refresh, expires_at, provider_name}
_token_cache: dict[int, dict] = {}
_bg_refresh_in_progress: set[int] = set()

_BG_REFRESH_THRESHOLD = 300  # 5 min before expiry → trigger background refresh.
# Past experience: pre-refresh 30 min early didn't prevent 401s (Anthropic
# revokes tokens earlier than local expires_at thinks). The 401 auto-retry path
# is the real safety net; pre-refresh just reduces 401 frequency. 5 min leaves
# enough time for async refresh but stops wasting refreshes on tokens with
# 30+ min remaining.


def get_cached_token(provider_id: int) -> dict | None:
    cached = _token_cache.get(provider_id)
    if cached and cached["expires_at"] > time.time() + 300:
        return cached
    return None


def get_token_status(provider_id: int) -> dict:
    cached = _token_cache.get(provider_id)
    if not cached:
        return {"expires_at": None, "seconds_left": None, "refreshing": provider_id in _bg_refresh_in_progress}
    seconds_left = cached["expires_at"] - time.time()
    return {
        "expires_at": cached["expires_at"],
        "seconds_left": int(seconds_left),
        "refreshing": provider_id in _bg_refresh_in_progress,
    }


def set_cached_token(
    provider_id: int,
    access: str,
    refresh: str,
    expires_at: float,
    provider_name: str,
    *,
    persist: bool = True,
) -> None:
    """Cache token info in memory and optionally persist expires_at to DB."""
    _token_cache[provider_id] = {
        "access": access,
        "refresh": refresh,
        "expires_at": expires_at,
        "provider_name": provider_name,
    }
    if persist:
        try:
            from app.database import SessionLocal
            from app.models.models import Settings
            cfg = _PROVIDERS[provider_name]
            settings_key = cfg["settings_expires_key"]
            db = SessionLocal()
            try:
                row = db.query(Settings).filter(Settings.key == settings_key).first()
                if row:
                    row.value = str(expires_at)
                else:
                    db.add(Settings(key=settings_key, value=str(expires_at)))
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.debug("Failed to persist %s expires_at", provider_name, exc_info=True)


# ── OAuth refresh ────────────────────────────────────────────────────────────


def refresh_oauth_token_sync(refresh_token: str, provider_name: str) -> dict | None:
    """Refresh an OAuth token (sync). Returns {access, refresh, expires_at} or None.

    Retries on network timeouts and 5xx up to 3 times with 1s/2s backoff.
    4xx returns None immediately (refresh_token likely invalidated).
    """
    import requests as _req

    if provider_name not in _PROVIDERS:
        logger.error("Unknown OAuth provider: %s", provider_name)
        return None
    cfg = _PROVIDERS[provider_name]

    if cfg["body_format"] == "json":
        request_kwargs = {
            "json": {
                "grant_type": "refresh_token",
                "client_id": cfg["client_id"],
                "refresh_token": refresh_token,
            },
            "headers": {"Content-Type": "application/json", "Accept": "application/json"},
        }
    else:  # form-urlencoded
        body = {
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "refresh_token": refresh_token,
        }
        if cfg.get("scope"):
            body["scope"] = cfg["scope"]
        request_kwargs = {
            "data": body,
            "headers": {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        }

    last_error: str | None = None
    for attempt in range(3):
        if attempt > 0:
            time.sleep(2 ** (attempt - 1))
        try:
            resp = _req.post(cfg["token_url"], timeout=10, **request_kwargs)
            if resp.status_code == 200:
                data = resp.json()
                expires_at = time.time() + data["expires_in"] - 300  # 5 min buffer
                return {
                    "access": data["access_token"],
                    # OpenAI may not always rotate refresh_token on every refresh; keep old if absent
                    "refresh": data.get("refresh_token", refresh_token),
                    "expires_at": expires_at,
                }
            if 500 <= resp.status_code < 600:
                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "[%s] OAuth refresh attempt %d: %s %s",
                    provider_name, attempt + 1, resp.status_code, resp.text[:200],
                )
                continue
            logger.error("[%s] OAuth token refresh failed: %s %s", provider_name, resp.status_code, resp.text)
            return None
        except (_req.Timeout, _req.ConnectionError) as exc:
            last_error = type(exc).__name__
            logger.warning("[%s] OAuth refresh attempt %d network error: %s", provider_name, attempt + 1, exc)
            continue
        except Exception:
            logger.exception("[%s] OAuth token refresh unexpected error", provider_name)
            return None

    logger.error("[%s] OAuth token refresh gave up after 3 attempts (last: %s)", provider_name, last_error)
    return None


def _do_refresh_and_update(provider_id: int, refresh_token: str, provider_name: str) -> None:
    """Background thread: refresh token and update DB + cache."""
    from app.database import SessionLocal
    from app.models.models import ApiProvider, Settings

    try:
        result = refresh_oauth_token_sync(refresh_token, provider_name)
        if not result:
            logger.error("[%s] Background OAuth refresh failed", provider_name)
            return
        cfg = _PROVIDERS[provider_name]
        db = SessionLocal()
        try:
            provider = db.query(ApiProvider).filter(ApiProvider.id == provider_id).first()
            refresh_setting = db.query(Settings).filter(Settings.key == cfg["settings_refresh_key"]).first()
            if provider and refresh_setting:
                provider.api_key = result["access"]
                refresh_setting.value = result["refresh"]
                db.commit()
        finally:
            db.close()
        set_cached_token(provider_id, result["access"], result["refresh"], result["expires_at"], provider_name)
        logger.info("[%s] Background OAuth refresh done, new expires_at=%s", provider_name, result["expires_at"])
    finally:
        _bg_refresh_in_progress.discard(provider_id)


def ensure_valid_token(db_session, api_provider=None, force: bool = False) -> str | None:
    """Check if OAuth token needs refresh, refresh if needed, update DB. Returns valid access_token.

    - If api_provider is None, auto-picks the first OAuth provider in DB (back-compat).
    - Token valid and not near expiry: return immediately, no refresh
    - Token within _BG_REFRESH_THRESHOLD of expiry: return immediately + kick off background refresh
    - Token expired / not in cache: synchronous refresh (blocks)
    - force=True: skip cache entirely and do a synchronous refresh (for 401 recovery)
    """
    from app.models.models import ApiProvider, Settings

    if api_provider is None:
        api_provider = (
            db_session.query(ApiProvider)
            .filter(ApiProvider.auth_type.in_(list(_AUTH_TYPE_TO_PROVIDER.keys())))
            .first()
        )
    if api_provider is None:
        return None

    provider_name = provider_from_auth_type(api_provider.auth_type)
    if not provider_name:
        return None
    cfg = _PROVIDERS[provider_name]

    cached = _token_cache.get(api_provider.id)
    now = time.time()

    # Restore cache from DB after restart
    if not cached:
        expires_row = db_session.query(Settings).filter(Settings.key == cfg["settings_expires_key"]).first()
        if expires_row and expires_row.value:
            try:
                db_expires = float(expires_row.value)
                if db_expires > now + 300:
                    refresh_setting = db_session.query(Settings).filter(Settings.key == cfg["settings_refresh_key"]).first()
                    set_cached_token(
                        api_provider.id,
                        api_provider.api_key,
                        refresh_setting.value if refresh_setting else "",
                        db_expires,
                        provider_name,
                        persist=False,
                    )
                    cached = _token_cache.get(api_provider.id)
                    logger.info("[%s] OAuth cache restored from DB, expires_at=%.0f, seconds_left=%.0f",
                                provider_name, db_expires, db_expires - now)
            except (ValueError, TypeError):
                pass

    if not force and cached and cached["expires_at"] > now + 300:
        seconds_left = cached["expires_at"] - now
        if seconds_left < _BG_REFRESH_THRESHOLD and api_provider.id not in _bg_refresh_in_progress:
            refresh_setting = db_session.query(Settings).filter(Settings.key == cfg["settings_refresh_key"]).first()
            if refresh_setting and refresh_setting.value:
                _bg_refresh_in_progress.add(api_provider.id)
                logger.info("[%s] OAuth token expires in %.0fs, starting background refresh",
                            provider_name, seconds_left)
                t = threading.Thread(
                    target=_do_refresh_and_update,
                    args=(api_provider.id, refresh_setting.value, provider_name),
                    daemon=True,
                )
                t.start()
        return cached["access"]

    # Token expired or not in cache — synchronous refresh
    refresh_setting = db_session.query(Settings).filter(Settings.key == cfg["settings_refresh_key"]).first()
    if not refresh_setting or not refresh_setting.value:
        logger.warning("[%s] No refresh_token in settings, cannot auto-refresh", provider_name)
        return api_provider.api_key

    result = refresh_oauth_token_sync(refresh_setting.value, provider_name)
    if not result:
        logger.error("[%s] OAuth token refresh failed, using existing token", provider_name)
        # Cache with estimated expiry so status page shows something
        set_cached_token(api_provider.id, api_provider.api_key, refresh_setting.value, time.time() + 86400, provider_name)
        return api_provider.api_key

    api_provider.api_key = result["access"]
    refresh_setting.value = result["refresh"]
    db_session.commit()
    logger.info("[%s] OAuth token refreshed (sync), expires_at=%s", provider_name, result["expires_at"])

    set_cached_token(api_provider.id, result["access"], result["refresh"], result["expires_at"], provider_name)
    return result["access"]
