"""Offline tests for Gemini embeddings and provider-aware AUTO routing."""

import math
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import EmbeddingProvider, LLMProvider, Settings
from app.rag.embeddings import GeminiEmbeddingClient
from app.rag.factory import create_embedding_client


class _FakeEmbeddingModels:
    def __init__(self, *, cardinality_delta: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._cardinality_delta = cardinality_delta

    async def embed_content(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        count = max(0, len(kwargs["contents"]) + self._cardinality_delta)
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=[3.0, 4.0]) for _ in range(count)]
        )


def _fake_client(*, cardinality_delta: int = 0) -> SimpleNamespace:
    models = _FakeEmbeddingModels(cardinality_delta=cardinality_delta)
    return SimpleNamespace(aio=SimpleNamespace(models=models), models=models)


def _call_texts(call: dict[str, Any]) -> list[str]:
    return [str(content.parts[0].text) for content in call["contents"]]


async def test_gemini_embedding_2_preserves_cardinality_and_purpose_prefixes() -> None:
    fake = _fake_client()
    client = GeminiEmbeddingClient(
        "test-key",
        dimensions=2,
        batch_size=2,
        query_instruction="Brazilian legal retrieval:",
        client=fake,
    )

    documents = await client.embed_documents(["artigo 1", "artigo 2", "artigo 3"])
    [query] = await client.embed_queries(["cobranca indevida"])

    assert len(documents) == 3
    assert all(
        math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0) for vector in documents
    )
    assert math.isclose(math.sqrt(sum(value * value for value in query)), 1.0)
    assert len(fake.models.calls) == 3
    assert _call_texts(fake.models.calls[0]) == [
        "title: none | text: artigo 1",
        "title: none | text: artigo 2",
    ]
    assert _call_texts(fake.models.calls[1]) == ["title: none | text: artigo 3"]
    assert _call_texts(fake.models.calls[2]) == [
        "task: search result | query: Brazilian legal retrieval: cobranca indevida"
    ]
    assert fake.models.calls[0]["config"].output_dimensionality == 2
    assert fake.models.calls[0]["config"].task_type is None
    assert "dimensions=2" in client._model_revision


async def test_gemini_embedding_001_uses_provider_task_types() -> None:
    fake = _fake_client()
    client = GeminiEmbeddingClient(
        "test-key",
        model="gemini-embedding-001",
        dimensions=2,
        client=fake,
    )

    await client.embed_documents(["documento"])
    await client.embed_query("consulta")

    assert fake.models.calls[0]["config"].task_type == "RETRIEVAL_DOCUMENT"
    assert fake.models.calls[1]["config"].task_type == "RETRIEVAL_QUERY"
    assert _call_texts(fake.models.calls[0]) == ["documento"]
    assert _call_texts(fake.models.calls[1]) == ["consulta"]


async def test_gemini_embedding_rejects_aggregated_or_missing_vectors() -> None:
    client = GeminiEmbeddingClient(
        "test-key",
        dimensions=2,
        client=_fake_client(cardinality_delta=-1),
    )

    with pytest.raises(ValueError, match="cardinality mismatch"):
        await client.embed_documents(["primeiro", "segundo"])


def test_auto_uses_gemini_embeddings_with_the_same_api_key() -> None:
    settings = Settings(
        llm_provider=LLMProvider.GEMINI,
        gemini_api_key="gemini-test",
        embedding_provider=EmbeddingProvider.AUTO,
        _env_file=None,
    )

    embedder = create_embedding_client(settings)

    assert isinstance(embedder, GeminiEmbeddingClient)
    assert embedder.model_name == "gemini-embedding-2"
    assert embedder._dimensions == 768


def test_auto_uses_local_embeddings_for_claude_without_a_second_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeLocalEmbeddingClient:
        def __init__(self, model: str, **kwargs: Any) -> None:
            captured["model"] = model
            captured.update(kwargs)
            self.model_name = model

    monkeypatch.setattr(
        "app.rag.factory.SentenceTransformerEmbeddingClient",
        FakeLocalEmbeddingClient,
    )
    settings = Settings(
        llm_provider=LLMProvider.ANTHROPIC,
        anthropic_api_key="anthropic-test",
        embedding_provider=EmbeddingProvider.AUTO,
        _env_file=None,
    )

    embedder = create_embedding_client(settings)

    assert embedder.model_name == "BAAI/bge-m3"
    assert captured["model"] == "BAAI/bge-m3"


def test_explicit_gemini_embeddings_require_the_gemini_key() -> None:
    settings = Settings(
        llm_provider=LLMProvider.OPENAI,
        openai_api_key="openai-test",
        embedding_provider=EmbeddingProvider.GEMINI,
        gemini_api_key=None,
        _env_file=None,
    )

    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        create_embedding_client(settings)
