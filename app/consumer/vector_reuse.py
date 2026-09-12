"""Reuse document vectors across legal embedding generations.

A new corpus release renames every chunk, because the document id hashes the
whole corpus, but most chunk texts are unchanged and a document vector depends
only on the text and on the document side of the embedding contract. This
module indexes the verified shards of earlier embedded generations by
chunk-text hash so a build embeds only what is new (ADR 0017).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.consumer.embedding_artifacts import (
    chunk_ids_sha256,
    chunks_sha256,
    float32_vector,
    vectors_sha256,
)
from app.core.logging import get_logger
from app.schemas.embedding import (
    EmbeddingContract,
    EmbeddingGenerationManifest,
    EmbeddingGenerationStatus,
    EmbeddingShardManifest,
)
from app.schemas.rag import Chunk

logger = get_logger(__name__)

CANARY_SIZE = 2
CANARY_MIN_COSINE = 0.9999
_REUSABLE_STATUSES = frozenset(
    {EmbeddingGenerationStatus.VALIDATED, EmbeddingGenerationStatus.ACTIVE}
)


class ReuseCanaryError(RuntimeError):
    """Reused vectors do not match what the current model produces."""


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity; vectors of different length never match."""

    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


@dataclass(frozen=True, slots=True)
class ReusedVector:
    """A verified float32 vector and exactly where it came from."""

    vector: tuple[float, ...]
    generation_id: str
    chunk_id: str
    artifact_sha256: str

    def source(self) -> dict[str, str]:
        return {
            "generation_id": self.generation_id,
            "chunk_id": self.chunk_id,
            "artifact_sha256": self.artifact_sha256,
        }


class ReusableVectorIndex:
    """Chunk-text hash to a verified vector from an earlier embedded generation."""

    def __init__(self, vectors: dict[str, ReusedVector], sources: tuple[str, ...]) -> None:
        self._vectors = vectors
        self.sources = sources

    @classmethod
    def empty(cls) -> ReusableVectorIndex:
        return cls({}, ())

    @classmethod
    def from_artifacts(
        cls,
        artifacts_dir: Path,
        contract: EmbeddingContract,
        *,
        exclude_generation_id: str,
    ) -> ReusableVectorIndex:
        if contract.output_dimension is None:
            # Without a pinned dimension the document identity is incomplete.
            return cls.empty()
        candidates: list[tuple[Path, EmbeddingGenerationManifest]] = []
        for manifest_path in sorted(artifacts_dir.glob("*/manifest.json")):
            manifest = _load_manifest(manifest_path)
            if manifest is not None and _is_source(manifest, contract, exclude_generation_id):
                candidates.append((manifest_path.parent, manifest))
        candidates.sort(key=lambda item: _preference(item[1]))

        vectors: dict[str, ReusedVector] = {}
        sources: list[str] = []
        for generation_dir, manifest in candidates:
            contributed = False
            for shard in manifest.shards:
                entries = _verified_source_shard(generation_dir, shard)
                if entries is None:
                    logger.warning(
                        "embedding_reuse_shard_skipped",
                        generation_id=manifest.generation_id,
                        shard_index=shard.shard_index,
                    )
                    continue
                for chunk, vector in entries:
                    key = text_sha256(chunk.text)
                    if key in vectors:
                        continue
                    vectors[key] = ReusedVector(
                        vector=tuple(vector),
                        generation_id=manifest.generation_id,
                        chunk_id=chunk.chunk_id,
                        artifact_sha256=shard.artifact_sha256,
                    )
                    contributed = True
            if contributed:
                sources.append(manifest.generation_id)
        return cls(vectors, tuple(sources))

    def __len__(self) -> int:
        return len(self._vectors)

    def lookup(self, text: str) -> ReusedVector | None:
        return self._vectors.get(text_sha256(text))

    def require(self, text: str) -> ReusedVector:
        hit = self.lookup(text)
        if hit is None:
            raise KeyError("no reusable vector for this chunk text")
        return hit


def _load_manifest(path: Path) -> EmbeddingGenerationManifest | None:
    try:
        return EmbeddingGenerationManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("embedding_reuse_manifest_unreadable", path=str(path))
        return None


def _is_source(
    manifest: EmbeddingGenerationManifest,
    contract: EmbeddingContract,
    exclude_generation_id: str,
) -> bool:
    return (
        manifest.generation_id != exclude_generation_id
        and manifest.status in _REUSABLE_STATUSES
        and manifest.provenance == "embedded"
        and manifest.contract.document_identity() == contract.document_identity()
    )


def _preference(manifest: EmbeddingGenerationManifest) -> tuple[int, float, str]:
    finished: datetime = manifest.activated_at or manifest.validated_at or manifest.created_at
    active_first = 0 if manifest.status is EmbeddingGenerationStatus.ACTIVE else 1
    return (active_first, -finished.timestamp(), manifest.generation_id)


def _verified_source_shard(
    generation_dir: Path,
    shard: EmbeddingShardManifest,
) -> list[tuple[Chunk, list[float]]] | None:
    if shard.artifact_file != f"embeddings-{shard.shard_index:04d}.jsonl.gz":
        return None
    try:
        compressed = (generation_dir / shard.artifact_file).read_bytes()
        if hashlib.sha256(compressed).hexdigest() != shard.artifact_sha256:
            return None
        entries = [
            (
                Chunk.model_validate(payload["chunk"]),
                float32_vector([float(value) for value in payload["vector"]]),
            )
            for payload in map(json.loads, gzip.decompress(compressed).splitlines())
        ]
    except (EOFError, KeyError, OSError, TypeError, ValueError):
        return None
    chunks = [chunk for chunk, _ in entries]
    vectors = [vector for _, vector in entries]
    if (
        len(entries) != shard.chunk_count
        or chunk_ids_sha256(chunks) != shard.chunk_ids_sha256
        or chunks_sha256(chunks) != shard.chunks_sha256
        or vectors_sha256(vectors) != shard.vectors_sha256
        or any(len(vector) != shard.output_dimension for vector in vectors)
    ):
        return None
    return entries
