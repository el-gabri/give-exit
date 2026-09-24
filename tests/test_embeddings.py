"""Purpose-specific embedding framing regressions."""

import sys
from pathlib import Path
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
        def __init__(
            self,
            model: str,
            *,
            device: str | None,
            revision: str | None,
            token: str | None,
            local_files_only: bool,
        ) -> None:
            instances.append((model, device, revision, token, local_files_only))

        def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeModel),
    )
    # Offline loading requires the model to be complete in the local cache.
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(try_to_load_from_cache=lambda repo, name, revision=None: f"/c/{name}"),
    )
    client = SentenceTransformerEmbeddingClient(
        "modelo-juridico",
        device="cpu",
        batch_size=3,
        model_revision="revision-1",
        hf_token="hf_test",
        local_files_only=True,
    )

    assert instances == []
    assert await client.embed_query("consulta") == [1.0, 0.0]
    assert await client.embed_documents(["documento"]) == [[1.0, 0.0]]
    assert instances == [("modelo-juridico", "cpu", "revision-1", "hf_test", True)]


def test_hugging_face_settings_are_read_under_their_own_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Hugging Face libraries read only the environment, never .env."""
    from app.core.config import EmbeddingProvider, Settings
    from app.rag.factory import create_embedding_client

    for name in ("HF_TOKEN", "HF_HUB_OFFLINE", "LITIGATION_HF_TOKEN", "LITIGATION_HF_HUB_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("HF_TOKEN=hf_from_dotenv\nHF_HUB_OFFLINE=1\n", encoding="utf-8")

    settings = Settings(
        _env_file=env_file,
        embedding_provider=EmbeddingProvider.SENTENCE_TRANSFORMERS,
        embedding_model="modelo-juridico",
    )
    client = create_embedding_client(settings)

    assert settings.hf_token == "hf_from_dotenv"
    assert settings.hf_hub_offline is True
    assert "hf_from_dotenv" not in repr(settings)
    assert isinstance(client, SentenceTransformerEmbeddingClient)
    assert client._hf_token == "hf_from_dotenv"
    assert client._local_files_only is True


@pytest.mark.parametrize("value", ["<your_huggind_face_token>", "", "   "])
def test_a_placeholder_hugging_face_token_means_anonymous_access(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("HF_TOKEN", value)

    assert Settings(_env_file=None).hf_token is None


def test_api_providers_keep_the_plain_instruction_prefix() -> None:
    """The JUA template belongs to the model that declares it, not to everyone.

    OpenAI and Gemini embeddings are not tuned on Instruct:/Query:, so wrapping
    an operator's instruction in it would only add tokens they never learned.
    """
    assert _with_instruction("cobranca indevida", "Represent this legal query") == (
        "Represent this legal query cobranca indevida"
    )
