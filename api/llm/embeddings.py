"""fastembed, in-process (01_architecture §9). No daemon, no API cost.

The ONNX model runs on CPU and is blocking, so it is called through asyncio.to_thread — the event
loop keeps serving other conversations while a query is embedded (~50 ms).

Default model is the spec's bge-small-en-v1.5 (384 dims). It is English-only; Arabic queries embed
poorly. `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` is also 384-dim and
multilingual — switching is an EMBEDDING_MODEL change plus a re-embed of knowledge_chunks.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol

from api.db.models.knowledge import EMBEDDING_DIMS


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding  # heavy import; load on first use only

                self._model = TextEmbedding(model_name=self._model_name, cache_dir=self._cache_dir)
            return self._model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        vectors = [list(map(float, v)) for v in self._load().embed(texts)]
        for v in vectors:
            if len(v) != EMBEDDING_DIMS:
                raise ValueError(
                    f"embedding model returned {len(v)} dims, expected {EMBEDDING_DIMS}"
                )
        return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts)
