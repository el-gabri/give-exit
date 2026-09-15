"""Bounded, privacy-safe execution for latency-sensitive query embeddings."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.core.hashing import sha256_hex
from app.core.logging import get_logger
from app.rag.embeddings import (
    EmbeddingBackend,
    embed_query_texts,
    validate_embedding_vectors,
)

logger = get_logger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """Semantic retrieval is temporarily unavailable and may use a safe fallback."""


@dataclass(frozen=True, slots=True)
class QueryEmbeddingResult:
    vectors: list[list[float]]
    cache_hits: int


class QueryEmbeddingGuard:
    """Apply timeout, concurrency, validation, cache and circuit-breaker policies."""

    def __init__(
        self,
        embedder: EmbeddingBackend,
        *,
        timeout_seconds: float,
        max_concurrency: int,
        queue_timeout_seconds: float,
        circuit_breaker_failures: int,
        circuit_breaker_reset_seconds: float,
        cache_ttl_seconds: float,
        cache_max_entries: int,
        expected_dimension: int | None,
    ) -> None:
        self._embedder = embedder
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(max_concurrency)
        self._queue_timeout_seconds = queue_timeout_seconds
        self._failure_threshold = circuit_breaker_failures
        self._reset_seconds = circuit_breaker_reset_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_max_entries = cache_max_entries
        self._expected_dimension = expected_dimension
        self._cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._background_tasks: set[asyncio.Task[list[list[float]]]] = set()

    async def embed(self, queries: list[str]) -> QueryEmbeddingResult:
        if not queries:
            return QueryEmbeddingResult(vectors=[], cache_hits=0)

        now = time.monotonic()
        vectors = [self._cached_vector(query, now=now) for query in queries]
        missing_indexes = [index for index, vector in enumerate(vectors) if vector is None]
        if missing_indexes:
            missing_queries = [queries[index] for index in missing_indexes]
            encoded = await self._embed_uncached(missing_queries, now=now)
            for index, query, vector in zip(
                missing_indexes, missing_queries, encoded, strict=True
            ):
                vectors[index] = vector
                self._store_cache(query, vector, now=time.monotonic())

        completed = [vector for vector in vectors if vector is not None]
        if len(completed) != len(queries):  # pragma: no cover - defensive invariant
            raise RuntimeError("query embedding guard produced an incomplete batch")
        return QueryEmbeddingResult(
            vectors=completed, cache_hits=len(queries) - len(missing_indexes)
        )

    async def _embed_uncached(self, queries: list[str], *, now: float) -> list[list[float]]:
        """Embed cache misses under the circuit-breaker, queue and timeout policies."""
        if now < self._circuit_open_until:
            raise EmbeddingUnavailableError("embedding circuit breaker is open")
        # Wait for a slot instead of rejecting immediately. A single slow
        # local model makes overlapping requests the normal case, and an
        # instant rejection would silently downgrade an ordinary concurrent
        # notice to lexical-only retrieval.
        # `except asyncio.TimeoutError`, not the builtin: before Python 3.11
        # asyncio.wait_for raises its own asyncio.exceptions.TimeoutError,
        # which is NOT a subclass of the builtin TimeoutError (they were
        # only unified into one class in 3.11+). requires-python allows
        # 3.10, and `except TimeoutError` here silently falls through to
        # the generic `except Exception` below on that version.
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self._queue_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise EmbeddingUnavailableError(
                "embedding concurrency limit is currently exhausted after "
                f"{self._queue_timeout_seconds:g}s"
            ) from exc

        task = asyncio.create_task(embed_query_texts(self._embedder, queries))
        release_in_callback = False
        try:
            encoded = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._timeout_seconds,
            )
            dimension = validate_embedding_vectors(
                encoded,
                expected_count=len(queries),
                expected_dimension=self._expected_dimension,
            )
            if self._expected_dimension is None:
                self._expected_dimension = dimension
        except asyncio.TimeoutError as exc:  # not builtin TimeoutError - see note above
            release_in_callback = True
            self._track_background_task(task)
            self._record_failure()
            raise EmbeddingUnavailableError(
                f"query embedding exceeded {self._timeout_seconds:g}s"
            ) from exc
        except Exception as exc:
            self._record_failure()
            raise EmbeddingUnavailableError(
                f"query embedding failed: {type(exc).__name__}"
            ) from exc
        finally:
            if not release_in_callback:
                self._slots.release()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        return [[float(value) for value in vector] for vector in encoded]

    def _track_background_task(self, task: asyncio.Task[list[list[float]]]) -> None:
        self._background_tasks.add(task)

        def _completed(done: asyncio.Task[list[list[float]]]) -> None:
            self._background_tasks.discard(done)
            self._slots.release()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.exception()

        task.add_done_callback(_completed)

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._circuit_open_until = time.monotonic() + self._reset_seconds
            logger.warning(
                "embedding_circuit_opened",
                failures=self._consecutive_failures,
                reset_seconds=self._reset_seconds,
            )

    def _cached_vector(self, query: str, *, now: float) -> list[float] | None:
        if self._cache_ttl_seconds <= 0 or self._cache_max_entries <= 0:
            return None
        key = sha256_hex(query)
        cached = self._cache.get(key)
        if cached is None:
            return None
        expires_at, vector = cached
        if expires_at <= now:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return list(vector)

    def _store_cache(self, query: str, vector: list[float], *, now: float) -> None:
        if self._cache_ttl_seconds <= 0 or self._cache_max_entries <= 0:
            return
        key = sha256_hex(query)
        self._cache[key] = (now + self._cache_ttl_seconds, list(vector))
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)
