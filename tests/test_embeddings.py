"""Purpose-specific embedding framing regressions."""

import sys
from types import SimpleNamespace

import pytest

from app.rag.embeddings import SentenceTransformerEmbeddingClient, _with_instruction


def test_query_instruction_is_separated_from_query_text() -> None:
    framed = _with_instruction(
        "negativação indevida",
        "Instruct: Recupere dispositivos legais brasileiros. Query:",
    )

    assert framed == (
        "Instruct: Recupere dispositivos legais brasileiros. "
        "Query: negativação indevida"
    )


def test_missing_query_instruction_leaves_text_unchanged() -> None:
    assert _with_instruction("cobrança não reconhecida", None) == (
        "cobrança não reconhecida"
    )


async def test_sentence_transformer_loads_weights_only_on_first_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FakeModel:
        def __init__(self, model: str, *, device: str | None, revision: str | None) -> None:
            instances.append((model, device, revision))

        def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeModel),
    )
    client = SentenceTransformerEmbeddingClient(
        "modelo-juridico",
        device="cpu",
        batch_size=3,
        model_revision="revision-1",
    )

    assert instances == []
    assert await client.embed_query("consulta") == [1.0, 0.0]
    assert await client.embed_documents(["documento"]) == [[1.0, 0.0]]
    assert instances == [("modelo-juridico", "cpu", "revision-1")]
