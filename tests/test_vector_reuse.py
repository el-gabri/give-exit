"""Cross-generation reuse of legal document vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.consumer.embedding_generation import EmbeddingGenerationManager
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_index import preindex_legal_corpus
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.embedding import EmbeddingGenerationManifest


def _pipeline(
    tmp_path: Path,
    embedder: MockEmbeddingClient,
    *,
    dimension: int | None = 128,
) -> RagPipeline:
    return RagPipeline(
        embedder=embedder,
        store=InMemoryVectorStore(index_name="legal-reuse-test"),
        corpus_version="reuse-test",
        embedding_expected_dimension=dimension,
        embedding_artifacts_dir=tmp_path / "generations",
        embedding_shard_size=25,
        embedding_require_model_revision=True,
    )


def _corpus(provisions: int) -> LegalCorpus:
    return LegalCorpus(get_default_legal_corpus().provisions[:provisions])


def _manifest_path(pipeline: RagPipeline, corpus: LegalCorpus) -> Path:
    return EmbeddingGenerationManager(pipeline, corpus).manifest_path


def _manifest(pipeline: RagPipeline, corpus: LegalCorpus) -> EmbeddingGenerationManifest:
    return EmbeddingGenerationManifest.model_validate_json(
        _manifest_path(pipeline, corpus).read_text(encoding="utf-8")
    )


async def test_new_manifests_are_v2_and_v1_manifests_stay_readable(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    corpus = _corpus(40)
    await preindex_legal_corpus(pipeline, corpus)
    payload = json.loads(_manifest_path(pipeline, corpus).read_text(encoding="utf-8"))

    assert payload["schema_version"] == "embedding-generation-manifest-v2"
    payload["schema_version"] = "embedding-generation-manifest-v1"
    for field in ("reused_chunk_count", "reuse_sources", "reuse_canary"):
        payload.pop(field)
    for shard in payload["shards"]:
        shard.pop("reused_chunk_count")

    restored = EmbeddingGenerationManifest.model_validate(payload)

    assert restored.reused_chunk_count == 0
    assert restored.reuse_canary is None
    assert all(shard.reused_chunk_count == 0 for shard in restored.shards)


async def test_adopted_manifests_cannot_claim_reused_vectors(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    corpus = _corpus(40)
    await preindex_legal_corpus(pipeline, corpus)
    payload = json.loads(_manifest_path(pipeline, corpus).read_text(encoding="utf-8"))
    payload.update(
        provenance="adopted_existing_vectors",
        source_index_name="legacy",
        attested_source_model_revision=payload["contract"]["model_revision"],
        reused_chunk_count=1,
    )

    with pytest.raises(ValidationError, match="adopted vectors cannot be reused"):
        EmbeddingGenerationManifest.model_validate(payload)
