"""Cross-generation reuse of legal document vectors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.consumer.embedding_generation import EmbeddingGenerationManager
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_index import preindex_legal_corpus
from app.consumer.vector_reuse import ReusableVectorIndex, cosine
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.embedding import EmbeddingContract, EmbeddingGenerationManifest


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


def _contract(dimension: int | None = 128) -> EmbeddingContract:
    return EmbeddingContract(
        model_repository="mock-hashed-bow-v1:128",
        model_revision="mock-hashed-bow-v1",
        output_dimension=dimension,
        document_formatter_version="plain-document-v1",
        query_formatter_version="instruction-prefix-v2",
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


async def test_source_selection_rules(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    corpus = _corpus(40)
    await preindex_legal_corpus(pipeline, corpus)
    manifest = _manifest(pipeline, corpus)
    artifacts = tmp_path / "generations"
    chunk = corpus.as_chunks()[0]

    def index(contract: EmbeddingContract, *, exclude: str) -> ReusableVectorIndex:
        return ReusableVectorIndex.from_artifacts(
            artifacts, contract, exclude_generation_id=exclude
        )

    everything = index(manifest.contract, exclude="other")
    hit = everything.lookup(chunk.text)

    assert len(everything) == len(corpus.as_chunks())
    assert everything.sources == (manifest.generation_id,)
    assert hit is not None and hit.generation_id == manifest.generation_id
    assert hit.source()["chunk_id"] == chunk.chunk_id
    assert len(index(manifest.contract, exclude=manifest.generation_id)) == 0
    other_identity = manifest.contract.model_copy(
        update={"document_formatter_version": "prefixed-document-v9"}
    )
    assert len(index(other_identity, exclude="x")) == 0
    path = _manifest_path(pipeline, corpus)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert len(index(manifest.contract, exclude="other")) == 0


async def test_invalid_source_shards_are_skipped(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    corpus = _corpus(40)
    await preindex_legal_corpus(pipeline, corpus)
    manifest = _manifest(pipeline, corpus)
    assert len(manifest.shards) >= 4
    path = _manifest_path(pipeline, corpus)
    generation_dir = path.parent
    payload = json.loads(path.read_text(encoding="utf-8"))
    first = generation_dir / payload["shards"][0]["artifact_file"]
    first.write_bytes(first.read_bytes() + b"corrupt")
    payload["shards"][1]["artifact_file"] = "../escape.jsonl.gz"
    third = generation_dir / payload["shards"][2]["artifact_file"]
    third.write_bytes(b"not gzip")
    payload["shards"][2]["artifact_sha256"] = hashlib.sha256(b"not gzip").hexdigest()
    payload["shards"][3]["vectors_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    skipped = sum(shard.chunk_count for shard in manifest.shards[:4])

    index = ReusableVectorIndex.from_artifacts(
        tmp_path / "generations", manifest.contract, exclude_generation_id="other"
    )

    assert len(index) == len(corpus.as_chunks()) - skipped


def test_reuse_needs_a_pinned_dimension_and_readable_manifests(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")

    unreadable = ReusableVectorIndex.from_artifacts(
        tmp_path, _contract(), exclude_generation_id="x"
    )
    unpinned = ReusableVectorIndex.from_artifacts(
        tmp_path, _contract(None), exclude_generation_id="x"
    )

    assert len(unreadable) == 0
    assert len(unpinned) == 0
    with pytest.raises(KeyError):
        ReusableVectorIndex.empty().require("texto")
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([1.0, 0.0], [2.0, 0.0]) == 1.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
