from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


class EmbeddingService:
    _api_key: str | None = None

    def __init__(self) -> None:
        self._load_api_key()

    @classmethod
    def _load_api_key(cls) -> str:
        if cls._api_key is not None:
            return cls._api_key
        api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("SILICONFLOW_API_KEY is not configured")
        cls._api_key = api_key
        return api_key

    def get_embedding(self, text: str) -> list[float] | None:
        try:
            api_key = self._load_api_key()
            url = "https://api.siliconflow.cn/v1/embeddings"
            payload = {
                "model": "BAAI/bge-m3",
                "input": [text],
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            values = data.get("data", [])
            if not values or "embedding" not in values[0]:
                raise RuntimeError("SiliconFlow embedding response missing 'embedding'")
            embedding = values[0]["embedding"]
            if not isinstance(embedding, list):
                raise RuntimeError("SiliconFlow embedding response has invalid format")
            if len(embedding) != 1024:
                raise RuntimeError(
                    f"SiliconFlow embedding dimension mismatch: expected 1024, got {len(embedding)}"
                )
            return embedding
        except Exception as exc:
            logger.warning("SiliconFlow embedding failed, fallback to None: %s", exc)
            return None

    def rerank(
        self, query: str, documents: list[str], top_n: int = 5
    ) -> list[dict[str, Any]]:
        if not documents:
            return []
        try:
            api_key = self._load_api_key()
            url = "https://api.siliconflow.cn/v1/rerank"
            payload = {
                "model": "Qwen/Qwen3-Reranker-8B",
                "query": query,
                "documents": documents,
                "top_n": top_n,
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            results = data.get("results", [])
            if isinstance(results, list):
                return results
            return []
        except Exception as exc:
            logger.warning("SiliconFlow rerank failed, fallback to original order: %s", exc)
            return []
