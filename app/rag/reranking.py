"""Optional second-stage rankers for retrieval candidates."""

import asyncio
from typing import Any, Protocol, runtime_checkable

from app.rag.hub_loading import load_cache_first
from app.schemas.rag import RetrievedChunk


@runtime_checkable
class Reranker(Protocol):
    """Reorder already retrieved chunks for one natural-language query."""

    @property
    def model_name(self) -> str: ...

    async def rerank(
        self, query: str, candidates: list[RetrievedChunk], k: int
    ) -> list[RetrievedChunk]: ...


class SentenceTransformerReranker:
    """Cross-encoder reranker loaded only when explicitly configured."""

    def __init__(
        self,
        model: str,
        *,
        device: str | None = None,
        batch_size: int = 8,
        model_revision: str | None = None,
        local_files_only: bool = False,
    ) -> None:
        try:
            import sentence_transformers
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError(
                "sentence-transformers is required for local reranking; "
                "install the 'local-embeddings' optional dependency"
            ) from exc

        self._model_name = model
        self._batch_size = batch_size
        self._model_revision = model_revision
        self._predict_lock = asyncio.Lock()
        # CrossEncoder takes a token argument only in recent sentence-transformers
        # releases; reranker models are public, so only the cache mode is set.
        self._model: Any = load_cache_first(
            lambda local_only: sentence_transformers.CrossEncoder(
                model, device=device, revision=model_revision, local_files_only=local_only
            ),
            repo_id=model,
            revision=model_revision,
            required_files=("config.json",),
            offline=local_files_only,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    async def rerank(
        self, query: str, candidates: list[RetrievedChunk], k: int
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []
        async with self._predict_lock:
            scores = await asyncio.to_thread(
                self._model.predict,
                [(query, item.chunk.text) for item in candidates],
                show_progress_bar=False,
                batch_size=self._batch_size,
            )
        rescored = [
            item.model_copy(update={"score": float(score)})
            for item, score in zip(candidates, scores, strict=True)
        ]
        rescored.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return rescored[:k]
