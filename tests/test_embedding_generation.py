"""Durable legal-embedding generation, resume and promotion contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.consumer.embedding_generation import EmbeddingGenerationManager
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_index import (
    adopt_legal_corpus_index,
    legal_corpus_is_indexed,
    preindex_legal_corpus,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import ChromaVectorStore, InMemoryVectorStore
from app.schemas.embedding import (
    EmbeddingContract,
    EmbeddingGenerationManifest,
    EmbeddingGenerationStatus,
)


def _pipeline(
    tmp_path: Path,
    embedder: MockEmbeddingClient,
    store: InMemoryVectorStore,
    *,
    shard_size: int = 200,
) -> RagPipeline:
    return RagPipeline(
        embedder=embedder,
        store=store,
        corpus_version="generation-test",
        embedding_expected_dimension=128,
        embedding_artifacts_dir=tmp_path / "generations",
        embedding_shard_size=shard_size,
        embedding_require_model_revision=True,
    )


async def test_generation_is_checksummed_active_and_strongly_validated(
    tmp_path: Path,
) -> None:
    corpus = get_default_legal_corpus()
    store = InMemoryVectorStore(index_name="legal-generation-test")
    pipeline = _pipeline(
        tmp_path,
        MockEmbeddingClient(),
        store,
    )

    result = await preindex_legal_corpus(pipeline, corpus)
    manager = EmbeddingGenerationManager(pipeline, corpus)
    manifest = EmbeddingGenerationManifest.model_validate_json(
        manager.manifest_path.read_text(encoding="utf-8")
    )

    assert result.generation_id == manager.generation_id
    assert manifest.status is EmbeddingGenerationStatus.ACTIVE
    assert manifest.completed_chunk_count == len(corpus.as_chunks())
    assert manifest.expected_shard_count == 3
    assert manifest.contract.output_dimension == 128
    assert await legal_corpus_is_indexed(pipeline, corpus) is True
    configuration = pipeline.retrieval_configuration(
        requested_k=1,
        doc_id=corpus.as_parsed_document().doc_id,
    )
    assert configuration["embedding_generation_id"] == manager.generation_id
    _, trace = await pipeline.retrieve_with_trace(
        "cobrança indevida",
        doc_id=corpus.as_parsed_document().doc_id,
        agent="generation-audit-test",
        k=1,
    )
    assert trace.embedding_generation_id == manager.generation_id

    entries = await pipeline.export_document(corpus.as_parsed_document().doc_id)
    first_chunk, first_vector = entries[0]
    await store.upsert([first_chunk], [[-value for value in first_vector]])
    assert await legal_corpus_is_indexed(pipeline, corpus) is False
    await store.upsert([first_chunk], [first_vector])
    assert await legal_corpus_is_indexed(pipeline, corpus) is True

    first_artifact = manager.manifest_path.parent / manifest.shards[0].artifact_file
    first_artifact.write_bytes(first_artifact.read_bytes() + b"corrupt")

    assert await legal_corpus_is_indexed(pipeline, corpus) is False


async def test_failed_generation_resumes_only_missing_shards(tmp_path: Path) -> None:
    class FailsOnSecondShard(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated interruption")
            return await super().embed_documents(texts)

    class CountingEmbedder(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            return await super().embed_documents(texts)

    corpus = get_default_legal_corpus()
    store = InMemoryVectorStore(index_name="legal-resume-test")
    failing = _pipeline(tmp_path, FailsOnSecondShard(), store)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        await preindex_legal_corpus(failing, corpus)

    manager = EmbeddingGenerationManager(failing, corpus)
    failed_manifest = EmbeddingGenerationManifest.model_validate_json(
        manager.manifest_path.read_text(encoding="utf-8")
    )
    assert failed_manifest.status is EmbeddingGenerationStatus.FAILED
    assert failed_manifest.completed_chunk_count == 200
    assert failed_manifest.error == "RuntimeError: simulated interruption"

    healthy_embedder = CountingEmbedder()
    resumed = _pipeline(tmp_path, healthy_embedder, store)
    result = await preindex_legal_corpus(resumed, corpus)

    assert result.action == "indexed"
    assert healthy_embedder.calls == 2
    assert await legal_corpus_is_indexed(resumed, corpus) is True


async def test_legacy_vectors_require_explicit_revision_attestation(
    tmp_path: Path,
) -> None:
    corpus = get_default_legal_corpus()
    chunks = sorted(corpus.as_chunks(), key=lambda chunk: chunk.chunk_id)
    source_embedder = MockEmbeddingClient()
    vectors = await source_embedder.embed_documents([chunk.text for chunk in chunks])
    destination = _pipeline(
        tmp_path,
        MockEmbeddingClient(),
        InMemoryVectorStore(index_name="legal-adoption-test"),
    )

    with pytest.raises(ValueError, match="attested source revision"):
        await adopt_legal_corpus_index(
            destination,
            corpus,
            list(zip(chunks, vectors, strict=True)),
            source_index_name="legacy-index",
            attested_model_revision="wrong-revision",
        )

    result = await adopt_legal_corpus_index(
        destination,
        corpus,
        list(zip(chunks, vectors, strict=True)),
        source_index_name="legacy-index",
        attested_model_revision="mock-hashed-bow-v1",
    )
    assert result.manifest_path is not None
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["provenance"] == "adopted_existing_vectors"
    assert payload["source_index_name"] == "legacy-index"
    assert payload["attested_source_model_revision"] == "mock-hashed-bow-v1"
    assert payload["status"] == "active"
    assert await legal_corpus_is_indexed(destination, corpus) is True


async def test_chroma_float32_roundtrip_keeps_generation_ready(tmp_path: Path) -> None:
    pytest.importorskip("chromadb")
    corpus = get_default_legal_corpus()
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=ChromaVectorStore(
            tmp_path / "chroma",
            collection_name="legal-generation-chroma-roundtrip",
        ),
        corpus_version="generation-test",
        embedding_expected_dimension=128,
        embedding_artifacts_dir=tmp_path / "generations",
        embedding_shard_size=200,
        embedding_require_model_revision=True,
    )

    result = await preindex_legal_corpus(pipeline, corpus)

    assert result.action == "indexed"
    assert await legal_corpus_is_indexed(pipeline, corpus) is True


def test_query_instruction_does_not_change_the_generation_identity() -> None:
    """Rewording a query instruction must not invalidate document vectors.

    Documents are never framed with a query instruction, so every persisted
    vector is bit-identical across the two configurations. Including the
    query fields in the identity turned a two-line wording change into a
    full re-embed of the corpus.
    """
    base = {
        "model_repository": "ufca-llms/jua-4B-mixed",
        "model_revision": "57f491c1718171c0ad71d723c4f6b2030684c4eb",
        "output_dimension": 2560,
        "document_formatter_version": "plain-document-v1",
    }
    old = EmbeddingContract(
        **base,
        query_formatter_version="instruction-prefix-v2",
        query_instruction_sha256="a" * 64,
    )
    new = EmbeddingContract(
        **base,
        query_formatter_version="instruct-query-v3",
        query_instruction_sha256="b" * 64,
    )

    assert old.document_identity() == new.document_identity()
    assert old != new


def test_document_side_changes_still_change_the_identity() -> None:
    """The guard must keep firing for anything that does move the vectors."""
    base = {
        "model_repository": "ufca-llms/jua-4B-mixed",
        "model_revision": "57f491c1718171c0ad71d723c4f6b2030684c4eb",
        "output_dimension": 2560,
        "document_formatter_version": "plain-document-v1",
        "query_formatter_version": "instruct-query-v3",
    }
    reference = EmbeddingContract(**base)

    for field, value in (
        ("model_repository", "other/model"),
        ("model_revision", "0" * 40),
        ("output_dimension", 1024),
        ("document_formatter_version", "prefixed-document-v2"),
    ):
        altered = EmbeddingContract(**{**base, field: value})
        assert altered.document_identity() != reference.document_identity(), field
