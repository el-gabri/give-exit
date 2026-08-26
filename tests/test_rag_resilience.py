"""Query-embedding timeout, cache and circuit-breaker policies."""

from __future__ import annotations

import asyncio

import pytest

from app.rag.embeddings import MockEmbeddingClient
from app.rag.resilience import EmbeddingUnavailableError, QueryEmbeddingGuard


def _guard(
    embedder: MockEmbeddingClient,
    *,
    timeout_seconds: float = 1.0,
    circuit_breaker_failures: int = 2,
) -> QueryEmbeddingGuard:
    return QueryEmbeddingGuard(
        embedder,
        timeout_seconds=timeout_seconds,
        max_concurrency=1,
        circuit_breaker_failures=circuit_breaker_failures,
        circuit_breaker_reset_seconds=60,
        cache_ttl_seconds=300,
        cache_max_entries=16,
        expected_dimension=128,
    )


async def test_query_hash_cache_avoids_duplicate_model_calls() -> None:
    class CountingEmbedder(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            return await super().embed(texts)

    embedder = CountingEmbedder()
    guard = _guard(embedder)

    first = await guard.embed(["cobrança indevida"])
    second = await guard.embed(["cobrança indevida"])

    assert embedder.calls == 1
    assert first.cache_hits == 0
    assert second.cache_hits == 1
    assert second.vectors == first.vectors


async def test_circuit_opens_after_the_configured_failure_threshold() -> None:
    class BrokenEmbedder(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            raise RuntimeError("provider details must not escape")

    embedder = BrokenEmbedder()
    guard = _guard(embedder, circuit_breaker_failures=1)

    with pytest.raises(EmbeddingUnavailableError, match="query embedding failed: RuntimeError"):
        await guard.embed(["consulta um"])
    with pytest.raises(EmbeddingUnavailableError, match="circuit breaker is open"):
        await guard.embed(["consulta dois"])

    assert embedder.calls == 1


async def test_timeout_keeps_capacity_reserved_until_local_work_finishes() -> None:
    release = asyncio.Event()

    class SlowEmbedder(MockEmbeddingClient):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            await release.wait()
            return await super().embed(texts)

    guard = _guard(SlowEmbedder(), timeout_seconds=0.001)

    with pytest.raises(EmbeddingUnavailableError, match="exceeded"):
        await guard.embed(["consulta lenta"])
    with pytest.raises(EmbeddingUnavailableError, match="concurrency limit"):
        await guard.embed(["segunda consulta"])

    release.set()
    await asyncio.sleep(0.01)
