import os
import time
import logging
from itertools import cycle

import httpx
from tavily import TavilyClient

logger = logging.getLogger(__name__)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
_jina_keys = [k.strip() for k in os.environ.get("JINA_API_KEYS", "").split(",") if k.strip()]
_jina_cycle = cycle(_jina_keys) if _jina_keys else None


def _next_jina_key() -> str:
    if _jina_cycle is None:
        raise RuntimeError("JINA_API_KEYS not configured")
    return next(_jina_cycle)


# ---------------------------------------------------------------------------
# web_fetch cache
# ---------------------------------------------------------------------------
_fetch_cache: dict[str, dict] = {}  # url → {"content", "title", "timestamp"}
_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
def web_search(payload: dict) -> dict:
    query = payload.get("query", "")
    if not query:
        return {"error": "query is required"}
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        resp = client.search(query, max_results=5, search_depth="basic", include_answer=False)
        results = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
            for r in resp.get("results", [])
        ]
        return {"query": query, "results": results}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------
def web_fetch(payload: dict) -> dict:
    url = payload.get("url", "")
    offset = payload.get("offset", 0)
    if not url:
        return {"error": "url is required"}

    # Check cache
    cached = _fetch_cache.get(url)
    if cached and time.time() - cached["timestamp"] < _CACHE_TTL:
        full_content = cached["content"]
        title = cached["title"]
    else:
        try:
            key = _next_jina_key()
            resp = httpx.get(
                f"https://r.jina.ai/{url}",
                headers={
                    "Accept": "application/json",
                    "X-Return-Format": "markdown",
                    "Authorization": f"Bearer {key}",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            full_content = data.get("data", {}).get("content", "") or ""
            title = data.get("data", {}).get("title", "") or ""
            _fetch_cache[url] = {"content": full_content, "title": title, "timestamp": time.time()}
        except Exception as e:
            return {"error": str(e)}

    # Slice content
    chunk = full_content[offset:offset + 4000]
    has_more = offset + 4000 < len(full_content)
    if has_more:
        next_offset = offset + 4000
        chunk += f"\n\n---\n还有更多内容未显示，如需继续阅读请调用 web_fetch(url, offset={next_offset})"
    return {
        "title": title,
        "url": url,
        "content": chunk,
        "total_length": len(full_content),
        "current_offset": offset,
        "has_more": has_more,
    }
