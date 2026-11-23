"""A lazily loaded query model is warmed at startup, not inside the first request."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api.main import create_app
from app.consumer.service import ConsumerCaseService
from app.core.config import LLMProvider, Settings, VectorStoreBackend
from app.rag.embeddings import MockEmbeddingClient, SentenceTransformerEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore


def _fake_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch, *, encoded: list[list[str]], fail: bool = False
) -> list[str]:
    loaded: list[str] = []

    class FakeModel:
        def __init__(self, model: str, **kwargs: object) -> None:
            loaded.append(model)

        def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            if fail:
                raise RuntimeError("out of memory")
            encoded.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeModel)
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(try_to_load_from_cache=lambda repo, name, revision=None: f"/c/{name}"),
    )
    return loaded


async def test_sentence_transformer_warm_up_loads_the_model_and_runs_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded: list[list[str]] = []
    loaded = _fake_sentence_transformers(monkeypatch, encoded=encoded)
    client = SentenceTransformerEmbeddingClient(
        "modelo-juridico", query_instruction="Recupere dispositivos.", local_files_only=True
    )

    assert client.warm is False
    await client.warm_up()

    assert loaded == ["modelo-juridico"]
    assert len(encoded) == 1 and len(encoded[0]) == 1
    # The query runs through the same instruction template as a real query.
    assert encoded[0][0].startswith("Instruct: Recupere dispositivos.\nQuery: ")
    assert client.warm is True


async def test_a_failed_first_encode_leaves_the_model_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sentence_transformers(monkeypatch, encoded=[], fail=True)
    client = SentenceTransformerEmbeddingClient("modelo-juridico", local_files_only=True)

    with pytest.raises(RuntimeError):
        await client.warm_up()

    assert client.warm is False


class _WarmableEmbedder(MockEmbeddingClient):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.warm = False
        self._fail = fail

    async def warm_up(self) -> None:
        if self._fail:
            raise RuntimeError("model files missing")
        self.warm = True


async def test_the_pipeline_warms_only_an_embedder_that_loads_lazily() -> None:
    eager = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    lazy = RagPipeline(_WarmableEmbedder(), InMemoryVectorStore())

    assert eager.query_embedding_warm is True
    assert await eager.warm_up_query_embedding() is False
    assert lazy.query_embedding_warm is False
    assert await lazy.warm_up_query_embedding() is True
    assert lazy.query_embedding_warm is True


async def test_a_failed_warm_up_is_logged_and_never_raised() -> None:
    service = object.__new__(ConsumerCaseService)
    service._rag = RagPipeline(_WarmableEmbedder(fail=True), InMemoryVectorStore())

    await service.warm_up_query_embedding()

    assert service.query_embedding_warm is False


async def test_a_successful_warm_up_marks_the_service_warm() -> None:
    service = object.__new__(ConsumerCaseService)
    service._rag = RagPipeline(_WarmableEmbedder(), InMemoryVectorStore())

    await service.warm_up_query_embedding()

    assert service.query_embedding_warm is True


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
        **overrides,
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_startup_warms_the_query_model_unless_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    calls: list[str] = []

    async def record(self: ConsumerCaseService) -> None:
        calls.append("warm_up")

    monkeypatch.setattr(ConsumerCaseService, "warm_up_query_embedding", record)
    app = create_app(_settings(tmp_path, embedding_warm_up_on_startup=enabled))

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert calls == (["warm_up"] if enabled else [])


async def test_shutdown_cancels_a_warm_up_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def slow(self: ConsumerCaseService) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(ConsumerCaseService, "warm_up_query_embedding", slow)
    app = create_app(_settings(tmp_path))

    async with app.router.lifespan_context(app):
        await started.wait()

    assert cancelled == [True]


async def test_health_reports_whether_the_query_model_is_warm(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = (await client.get("/health")).json()

    # The offline embedder has no model to load.
    assert health["query_embedding_warm"] is True
