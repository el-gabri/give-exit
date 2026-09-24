"""Cross-generation reuse of legal document vectors."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.consumer import preindex_legal
from app.consumer.embedding_generation import EmbeddingGenerationManager
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_index import (
    adopt_legal_corpus_index,
    legal_corpus_is_indexed,
    preindex_legal_corpus,
)
from app.consumer.vector_reuse import (
    CANARY_SIZE,
    ReusableVectorIndex,
    ReuseCanaryError,
    cosine,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.embedding import (
    EmbeddingContract,
    EmbeddingGenerationManifest,
    EmbeddingGenerationStatus,
)


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


class CountingEmbedder(MockEmbeddingClient):
    def __init__(self) -> None:
        super().__init__()
        self.document_texts = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_texts += len(texts)
        return await super().embed_documents(texts)


class DriftingEmbedder(CountingEmbedder):
    """Same declared contract, different vectors: a silent formatter change."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await super().embed_documents(texts)
        return [vector[1:] + vector[:1] for vector in vectors]


class OtherFormatterEmbedder(MockEmbeddingClient):
    document_format_version = "prefixed-document-v9"


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


async def test_a_new_generation_reuses_every_identical_chunk_text(tmp_path: Path) -> None:
    source_corpus, target_corpus = _corpus(40), _corpus(60)
    source_pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    await preindex_legal_corpus(source_pipeline, source_corpus)
    source_id = _manifest(source_pipeline, source_corpus).generation_id
    embedder = CountingEmbedder()
    pipeline = _pipeline(tmp_path, embedder)

    result = await preindex_legal_corpus(pipeline, target_corpus)

    source_chunks = len(source_corpus.as_chunks())
    target_chunks = len(target_corpus.as_chunks())
    manifest = _manifest(pipeline, target_corpus)
    assert result.reused_vectors == source_chunks
    assert result.reuse_sources == (source_id,)
    assert manifest.reused_chunk_count == source_chunks
    assert manifest.reuse_sources == [source_id]
    assert manifest.reuse_canary is not None and manifest.reuse_canary.passed
    assert embedder.document_texts == target_chunks - source_chunks + CANARY_SIZE
    assert manifest.provenance == "embedded"
    assert manifest.status is EmbeddingGenerationStatus.ACTIVE
    assert await legal_corpus_is_indexed(pipeline, target_corpus)
    generation_dir = _manifest_path(pipeline, target_corpus).parent
    lines = [
        line
        for shard in manifest.shards
        for line in gzip.decompress(
            (generation_dir / shard.artifact_file).read_bytes()
        ).splitlines()
    ]
    sources = [json.loads(line).get("source") for line in lines]
    assert sum(source is not None for source in sources) == source_chunks
    assert {source["generation_id"] for source in sources if source} == {source_id}


async def test_a_later_generation_prefers_the_newest_source(tmp_path: Path) -> None:
    first, second, third = _corpus(40), _corpus(50), _corpus(60)
    await preindex_legal_corpus(_pipeline(tmp_path, MockEmbeddingClient()), first)
    second_pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    await preindex_legal_corpus(second_pipeline, second)
    second_id = _manifest(second_pipeline, second).generation_id
    pipeline = _pipeline(tmp_path, MockEmbeddingClient())

    result = await preindex_legal_corpus(pipeline, third)

    assert result.reused_vectors == len(second.as_chunks())
    assert result.reuse_sources == (second_id,)


async def test_adopted_generations_are_never_reuse_sources(tmp_path: Path) -> None:
    source_corpus, target_corpus = _corpus(40), _corpus(60)
    chunks = sorted(source_corpus.as_chunks(), key=lambda chunk: chunk.chunk_id)
    vectors = await MockEmbeddingClient().embed_documents([chunk.text for chunk in chunks])
    await adopt_legal_corpus_index(
        _pipeline(tmp_path, MockEmbeddingClient()),
        source_corpus,
        list(zip(chunks, vectors, strict=True)),
        source_index_name="legacy-index",
        attested_model_revision="mock-hashed-bow-v1",
    )
    embedder = CountingEmbedder()
    pipeline = _pipeline(tmp_path, embedder)

    result = await preindex_legal_corpus(pipeline, target_corpus)

    assert result.reused_vectors == 0
    assert embedder.document_texts == len(target_corpus.as_chunks())
    assert _manifest(pipeline, target_corpus).reuse_canary is None


async def test_a_different_document_formatter_disables_reuse(tmp_path: Path) -> None:
    await preindex_legal_corpus(_pipeline(tmp_path, MockEmbeddingClient()), _corpus(40))
    pipeline = _pipeline(tmp_path, OtherFormatterEmbedder())

    result = await preindex_legal_corpus(pipeline, _corpus(60))

    assert result.reused_vectors == 0


async def test_a_failed_canary_aborts_the_build_and_no_reuse_recovers(tmp_path: Path) -> None:
    source_corpus, target_corpus = _corpus(40), _corpus(60)
    await preindex_legal_corpus(_pipeline(tmp_path, MockEmbeddingClient()), source_corpus)
    drifting = _pipeline(tmp_path, DriftingEmbedder())

    with pytest.raises(ReuseCanaryError, match="--no-reuse"):
        await preindex_legal_corpus(drifting, target_corpus)

    failed = _manifest(drifting, target_corpus)
    assert failed.status is EmbeddingGenerationStatus.FAILED
    assert failed.reuse_canary is not None and not failed.reuse_canary.passed
    assert "ReuseCanaryError" in (failed.error or "")

    recovered = await preindex_legal_corpus(drifting, target_corpus, reuse=False)

    assert recovered.reused_vectors == 0
    assert _manifest(drifting, target_corpus).reuse_canary is None
    assert await legal_corpus_is_indexed(drifting, target_corpus)


def test_preindex_cli_exposes_no_reuse() -> None:
    assert preindex_legal._parser().parse_args(["--no-reuse"]).no_reuse is True


def test_no_reuse_only_applies_to_building(capsys: pytest.CaptureFixture[str]) -> None:
    assert preindex_legal.main(["--no-reuse", "--check"]) == 1
    assert "--no-reuse" in capsys.readouterr().err
