"""Purpose-specific embedding framing regressions."""

import sys
from types import SimpleNamespace

import pytest

from app.rag.embeddings import (
    SentenceTransformerEmbeddingClient,
    _with_instruction,
    instruct_query,
    query_task_description,
)


def test_query_uses_the_instruct_template_the_model_declares() -> None:
    """``Instruct: {task}\\nQuery: {query}``, per the JUA model card.

    The newline goes between the task and ``Query:``; the query itself follows
    on the same line. Putting it after ``Query:`` instead sent every query in a
    shape the model was not tuned on.
    """
    framed = instruct_query(
        "negativação indevida",
        "Recupere dispositivos legais brasileiros.",
    )

    assert framed == (
        "Instruct: Recupere dispositivos legais brasileiros.\nQuery: negativação indevida"
    )


def test_legacy_instruction_with_template_scaffolding_is_not_doubled() -> None:
    """Configurations predating the task-only setting must keep working."""
    legacy = instruct_query(
        "negativação indevida",
        "Instruct: Recupere dispositivos legais brasileiros. Query:",
    )

    assert legacy == (
        "Instruct: Recupere dispositivos legais brasileiros.\nQuery: negativação indevida"
    )
    assert legacy.count("Instruct:") == 1
    assert legacy.count("Query:") == 1


def test_query_task_description_strips_only_the_scaffolding() -> None:
    assert query_task_description("Instruct: Tarefa X. Query:") == "Tarefa X."
    assert query_task_description("Tarefa X.") == "Tarefa X."
    assert query_task_description("   ") == ""
    assert query_task_description(None) == ""


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


def test_api_providers_keep_the_plain_instruction_prefix() -> None:
    """The JUA template belongs to the model that declares it, not to everyone.

    OpenAI and Gemini embeddings are not tuned on Instruct:/Query:, so wrapping
    an operator's instruction in it would only add tokens they never learned.
    """
    assert _with_instruction("cobranca indevida", "Represent this legal query") == (
        "Represent this legal query cobranca indevida"
    )
