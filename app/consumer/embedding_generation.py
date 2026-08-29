"""Resumable, checksummed generation of the immutable Consumer legal index."""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.consumer.legal_corpus import LEGAL_CHUNKING_IDENTITY, LegalCorpus
from app.core.logging import get_logger
from app.rag.embeddings import validate_embedding_vectors
from app.rag.pipeline import RagPipeline
from app.schemas.embedding import (
    EmbeddingContract,
    EmbeddingGenerationManifest,
    EmbeddingGenerationStatus,
    EmbeddingShardManifest,
)
from app.schemas.rag import Chunk

logger = get_logger(__name__)

# Vector databases may round-trip IEEE-754 float32 values through a second
# float32 normalization/serialization step. Chroma's cosine collection, for
# example, can move a component by one ULP while preserving the vector. Keep
# the checksum as the fast, exact path and permit only float32-scale drift.
_PERSISTED_VECTOR_REL_TOLERANCE = 2e-7
_PERSISTED_VECTOR_ABS_TOLERANCE = 1e-8


class EmbeddingGenerationManager:
    """Build, resume, validate and activate one legal embedding generation."""

    def __init__(self, rag: RagPipeline, corpus: LegalCorpus) -> None:
        artifacts_dir = rag.embedding_artifacts_dir
        if artifacts_dir is None:
            raise ValueError("embedding artifact persistence is not configured")
        self._rag = rag
        self._corpus = corpus
        self._chunks = sorted(corpus.as_chunks(), key=lambda chunk: chunk.chunk_id)
        self._shard_size = rag.embedding_shard_size
        self._contract = _embedding_contract(rag)
        self._generation_id = _generation_id(
            rag.index_name,
            corpus,
            self._chunks,
            self._contract,
            self._shard_size,
        )
        self._generation_dir = artifacts_dir / self._generation_id
        self._manifest_path = self._generation_dir / "manifest.json"

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    async def is_ready(self) -> bool:
        manifest = self._load_manifest(required=False)
        if manifest is None or manifest.status is not EmbeddingGenerationStatus.ACTIVE:
            return False
        try:
            self._validate_manifest_identity(manifest)
            _, artifact_vectors = self._load_complete_generation(manifest)
            entries = await self._rag.export_document(self._document_id)
            persisted_vectors = validated_vectors_for_chunks(
                self._chunks,
                entries,
                expected_dimension=manifest.contract.output_dimension,
            )
            if not _persisted_vectors_match(artifact_vectors, persisted_vectors):
                raise ValueError("active vectors do not match the generation artifacts")
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    async def build_and_activate(self, *, force: bool = False) -> EmbeddingGenerationManifest:
        manifest = self._new_manifest(provenance="embedded")
        if not force:
            existing = self._load_manifest(required=False)
            if existing is not None:
                self._validate_manifest_identity(existing)
                manifest = existing
        manifest.status = EmbeddingGenerationStatus.BUILDING
        manifest.error = None
        self._save_manifest(manifest)

        completed = {shard.shard_index: shard for shard in manifest.shards}
        for shard_index, chunks in enumerate(_shards(self._chunks, self._shard_size)):
            shard = completed.get(shard_index)
            if shard is not None and self._verified_shard_entries(shard, chunks) is not None:
                logger.info(
                    "embedding_shard_reused",
                    generation_id=self._generation_id,
                    shard_index=shard_index,
                    chunks=len(chunks),
                )
                continue
            try:
                vectors = await self._rag.embed_document_batch(
                    [chunk.text for chunk in chunks]
                )
                dimension = validate_embedding_vectors(
                    vectors,
                    expected_count=len(chunks),
                    expected_dimension=manifest.contract.output_dimension,
                )
                if manifest.contract.output_dimension is None:
                    manifest.contract = manifest.contract.model_copy(
                        update={"output_dimension": dimension}
                    )
                shard_manifest = self._write_shard(shard_index, chunks, vectors)
                manifest.shards = sorted(
                    [item for item in manifest.shards if item.shard_index != shard_index]
                    + [shard_manifest],
                    key=lambda item: item.shard_index,
                )
                manifest.completed_chunk_count = sum(
                    item.chunk_count for item in manifest.shards
                )
                manifest.updated_at = _now()
                manifest.error = None
                self._save_manifest(manifest)
                logger.info(
                    "embedding_shard_completed",
                    generation_id=self._generation_id,
                    shard_index=shard_index,
                    completed_chunks=manifest.completed_chunk_count,
                    expected_chunks=manifest.expected_chunk_count,
                    output_dimension=dimension,
                )
            except Exception as exc:
                manifest.status = EmbeddingGenerationStatus.FAILED
                manifest.error = f"{type(exc).__name__}: {exc}"
                manifest.updated_at = _now()
                self._save_manifest(manifest)
                raise

        chunks, vectors = self._load_complete_generation(manifest)
        manifest.status = EmbeddingGenerationStatus.VALIDATED
        manifest.validated_at = _now()
        manifest.updated_at = _now()
        self._save_manifest(manifest)

        await self._rag.index_precomputed_chunks(chunks, vectors)
        persisted = await self._rag.export_document(self._document_id)
        persisted_vectors = validated_vectors_for_chunks(
            self._chunks,
            persisted,
            expected_dimension=manifest.contract.output_dimension,
        )
        if not _persisted_vectors_match(vectors, persisted_vectors):
            raise RuntimeError("persisted vectors do not match the validated generation")
        self._rag.register_indexed_document(
            self._document_id,
            chunking_version=LEGAL_CHUNKING_IDENTITY,
            embedding_generation_id=self._generation_id,
        )
        manifest.status = EmbeddingGenerationStatus.ACTIVE
        manifest.activated_at = _now()
        manifest.updated_at = _now()
        manifest.error = None
        self._save_manifest(manifest)
        return manifest

    async def adopt_and_activate(
        self,
        entries: list[tuple[Chunk, list[float]]],
        *,
        source_index_name: str,
        attested_model_revision: str,
    ) -> EmbeddingGenerationManifest:
        """Promote validated legacy vectors without pretending their revision was recorded."""

        configured_revision = self._contract.model_revision
        if attested_model_revision.strip() != configured_revision:
            raise ValueError(
                "the attested source revision must equal the configured embedding revision"
            )
        vectors = validated_vectors_for_chunks(
            self._chunks,
            entries,
            expected_dimension=self._contract.output_dimension,
        )
        manifest = self._new_manifest(
            provenance="adopted_existing_vectors",
            source_index_name=source_index_name,
            attested_source_model_revision=attested_model_revision,
        )
        dimension = validate_embedding_vectors(
            vectors,
            expected_count=len(self._chunks),
            expected_dimension=self._contract.output_dimension,
        )
        manifest.contract = manifest.contract.model_copy(update={"output_dimension": dimension})
        for shard_index, chunks in enumerate(_shards(self._chunks, self._shard_size)):
            start = shard_index * self._shard_size
            shard_vectors = vectors[start : start + len(chunks)]
            manifest.shards.append(
                self._write_shard(shard_index, chunks, shard_vectors)
            )
        manifest.completed_chunk_count = len(self._chunks)
        manifest.status = EmbeddingGenerationStatus.VALIDATED
        manifest.validated_at = _now()
        manifest.updated_at = _now()
        self._save_manifest(manifest)

        await self._rag.index_precomputed_chunks(self._chunks, vectors)
        persisted = await self._rag.export_document(self._document_id)
        persisted_vectors = validated_vectors_for_chunks(
            self._chunks,
            persisted,
            expected_dimension=dimension,
        )
        if not _persisted_vectors_match(vectors, persisted_vectors):
            raise RuntimeError("persisted vectors do not match the adopted generation")
        self._rag.register_indexed_document(
            self._document_id,
            chunking_version=LEGAL_CHUNKING_IDENTITY,
            embedding_generation_id=self._generation_id,
        )
        manifest.status = EmbeddingGenerationStatus.ACTIVE
        manifest.activated_at = _now()
        manifest.updated_at = _now()
        self._save_manifest(manifest)
        return manifest

    @property
    def _document_id(self) -> str:
        return self._corpus.as_parsed_document().doc_id

    def _new_manifest(
        self,
        *,
        provenance: Literal["embedded", "adopted_existing_vectors"],
        source_index_name: str | None = None,
        attested_source_model_revision: str | None = None,
    ) -> EmbeddingGenerationManifest:
        return EmbeddingGenerationManifest(
            generation_id=self._generation_id,
            index_name=self._rag.index_name,
            source_index_name=source_index_name,
            attested_source_model_revision=attested_source_model_revision,
            provenance=provenance,
            corpus_release_id=self._corpus.release_id,
            corpus_sha256=self._corpus.corpus_sha256,
            document_id=self._document_id,
            chunking_version=LEGAL_CHUNKING_IDENTITY,
            expected_chunk_count=len(self._chunks),
            expected_chunk_ids_sha256=_chunk_ids_sha256(self._chunks),
            expected_chunks_sha256=_chunks_sha256(self._chunks),
            contract=self._contract,
            shard_size=self._shard_size,
            expected_shard_count=math.ceil(len(self._chunks) / self._shard_size),
            package_versions=_package_versions(),
            hardware=_hardware_metadata(),
        )

    def _validate_manifest_identity(self, manifest: EmbeddingGenerationManifest) -> None:
        expected = self._new_manifest(
            provenance=manifest.provenance,
            source_index_name=manifest.source_index_name,
            attested_source_model_revision=manifest.attested_source_model_revision,
        )
        immutable_fields = (
            "generation_id",
            "index_name",
            "corpus_release_id",
            "corpus_sha256",
            "document_id",
            "chunking_version",
            "expected_chunk_count",
            "expected_chunk_ids_sha256",
            "expected_chunks_sha256",
            "shard_size",
            "expected_shard_count",
        )
        mismatched = [
            field
            for field in immutable_fields
            if getattr(manifest, field) != getattr(expected, field)
        ]
        expected_contract = expected.contract.model_copy(
            update={"output_dimension": manifest.contract.output_dimension}
        )
        if manifest.contract != expected_contract:
            mismatched.append("contract")
        if mismatched:
            raise ValueError(
                "embedding manifest is incompatible with runtime: " + ", ".join(mismatched)
            )

    def _load_complete_generation(
        self,
        manifest: EmbeddingGenerationManifest,
    ) -> tuple[list[Chunk], list[list[float]]]:
        if len(manifest.shards) != manifest.expected_shard_count:
            raise ValueError("embedding generation does not contain every expected shard")
        if manifest.completed_chunk_count != manifest.expected_chunk_count:
            raise ValueError("embedding generation has an incomplete chunk count")
        if sum(shard.chunk_count for shard in manifest.shards) != len(self._chunks):
            raise ValueError("embedding shard counts do not cover the canonical corpus")
        entries: list[tuple[Chunk, list[float]]] = []
        for shard_index, chunks in enumerate(_shards(self._chunks, self._shard_size)):
            selected = next(
                (item for item in manifest.shards if item.shard_index == shard_index),
                None,
            )
            if selected is None:
                raise ValueError(f"embedding generation is missing shard {shard_index}")
            loaded = self._verified_shard_entries(selected, chunks)
            if loaded is None:
                raise ValueError(f"embedding shard {shard_index} failed checksum validation")
            entries.extend(loaded)
        vectors = validated_vectors_for_chunks(
            self._chunks,
            entries,
            expected_dimension=manifest.contract.output_dimension,
        )
        return self._chunks, vectors

    def _write_shard(
        self,
        shard_index: int,
        chunks: list[Chunk],
        vectors: list[list[float]],
    ) -> EmbeddingShardManifest:
        float32_vectors = [_float32_vector(vector) for vector in vectors]
        dimension = validate_embedding_vectors(
            float32_vectors,
            expected_count=len(chunks),
            expected_dimension=self._contract.output_dimension,
        )
        payload = b"\n".join(
            _canonical_json_bytes(
                {"chunk": chunk.model_dump(mode="json"), "vector": vector}
            )
            for chunk, vector in zip(chunks, float32_vectors, strict=True)
        )
        compressed = gzip.compress(payload, compresslevel=6, mtime=0)
        filename = f"embeddings-{shard_index:04d}.jsonl.gz"
        path = self._generation_dir / filename
        _atomic_write(path, compressed)
        return EmbeddingShardManifest(
            shard_index=shard_index,
            artifact_file=filename,
            artifact_sha256=hashlib.sha256(compressed).hexdigest(),
            chunk_count=len(chunks),
            chunk_ids_sha256=_chunk_ids_sha256(chunks),
            chunks_sha256=_chunks_sha256(chunks),
            vectors_sha256=_vectors_sha256(float32_vectors),
            output_dimension=dimension,
        )

    def _verified_shard_entries(
        self,
        shard: EmbeddingShardManifest,
        expected_chunks: list[Chunk],
    ) -> list[tuple[Chunk, list[float]]] | None:
        expected_filename = f"embeddings-{shard.shard_index:04d}.jsonl.gz"
        if shard.artifact_file != expected_filename:
            return None
        path = self._generation_dir / shard.artifact_file
        try:
            compressed = path.read_bytes()
            if hashlib.sha256(compressed).hexdigest() != shard.artifact_sha256:
                return None
            raw = gzip.decompress(compressed)
            entries: list[tuple[Chunk, list[float]]] = []
            for line in raw.splitlines():
                payload = json.loads(line)
                entries.append(
                    (
                        Chunk.model_validate(payload["chunk"]),
                        [float(value) for value in payload["vector"]],
                    )
                )
            vectors = validated_vectors_for_chunks(
                expected_chunks,
                entries,
                expected_dimension=shard.output_dimension,
            )
            if _vectors_sha256(vectors) != shard.vectors_sha256:
                return None
            if _chunk_ids_sha256(expected_chunks) != shard.chunk_ids_sha256:
                return None
            if _chunks_sha256(expected_chunks) != shard.chunks_sha256:
                return None
            return entries
        except (KeyError, OSError, TypeError, ValueError, gzip.BadGzipFile, json.JSONDecodeError):
            return None

    def _load_manifest(self, *, required: bool) -> EmbeddingGenerationManifest | None:
        try:
            return EmbeddingGenerationManifest.model_validate_json(
                self._manifest_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            if required:
                raise
            return None

    def _save_manifest(self, manifest: EmbeddingGenerationManifest) -> None:
        manifest.updated_at = _now()
        _atomic_write(
            self._manifest_path,
            manifest.model_dump_json(indent=2).encode("utf-8"),
        )


def _embedding_contract(rag: RagPipeline) -> EmbeddingContract:
    configuration = rag.embedding_contract_configuration()
    revision = str(configuration["model_revision"] or "").strip()
    if bool(configuration["require_model_revision"]) and not revision:
        raise ValueError(
            "the legal embedding generation requires an exact model revision; "
            "set LITIGATION_EMBEDDING_MODEL_REVISION"
        )
    normalization = str(configuration["normalization"])
    if normalization != "l2":
        raise ValueError("legal embedding generations require explicit L2 normalization")
    return EmbeddingContract(
        model_repository=str(configuration["model_repository"]),
        model_revision=revision or "unversioned",
        output_dimension=(
            int(configuration["output_dimension"])
            if configuration["output_dimension"] is not None
            else None
        ),
        normalization="l2",
        document_formatter_version=str(configuration["document_formatter_version"]),
        query_formatter_version=str(configuration["query_formatter_version"]),
        query_instruction_sha256=(
            str(configuration["query_instruction_sha256"])
            if configuration["query_instruction_sha256"] is not None
            else None
        ),
    )


def validated_vectors_for_chunks(
    expected_chunks: list[Chunk],
    entries: list[tuple[Chunk, list[float]]],
    *,
    expected_dimension: int | None,
) -> list[list[float]]:
    expected_by_id = {chunk.chunk_id: chunk for chunk in expected_chunks}
    actual_by_id = {chunk.chunk_id: (chunk, vector) for chunk, vector in entries}
    if len(actual_by_id) != len(entries):
        raise ValueError("embedding entries contain duplicate chunk ids")
    if set(actual_by_id) != set(expected_by_id):
        missing = sorted(set(expected_by_id) - set(actual_by_id))[:5]
        extra = sorted(set(actual_by_id) - set(expected_by_id))[:5]
        raise ValueError(f"embedding chunk coverage mismatch: missing={missing}, extra={extra}")
    for chunk_id, expected in expected_by_id.items():
        actual, _ = actual_by_id[chunk_id]
        if _canonical_json_bytes(actual.model_dump(mode="json")) != _canonical_json_bytes(
            expected.model_dump(mode="json")
        ):
            raise ValueError(f"stored chunk does not match canonical corpus: {chunk_id}")
    vectors = [actual_by_id[chunk.chunk_id][1] for chunk in expected_chunks]
    validate_embedding_vectors(
        vectors,
        expected_count=len(expected_chunks),
        expected_dimension=expected_dimension,
    )
    return vectors


def _generation_id(
    index_name: str,
    corpus: LegalCorpus,
    chunks: list[Chunk],
    contract: EmbeddingContract,
    shard_size: int,
) -> str:
    payload = {
        "index_name": index_name,
        "corpus_release_id": corpus.release_id,
        "corpus_sha256": corpus.corpus_sha256,
        "document_id": corpus.as_parsed_document().doc_id,
        "chunking_version": LEGAL_CHUNKING_IDENTITY,
        "chunks_sha256": _chunks_sha256(chunks),
        "contract": contract.model_dump(mode="json"),
        "shard_size": shard_size,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()[:16]


def _shards(chunks: list[Chunk], size: int) -> list[list[Chunk]]:
    return [chunks[start : start + size] for start in range(0, len(chunks), size)]


def _chunk_ids_sha256(chunks: list[Chunk]) -> str:
    return hashlib.sha256("\n".join(chunk.chunk_id for chunk in chunks).encode("utf-8")).hexdigest()


def _chunks_sha256(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(_canonical_json_bytes(chunk.model_dump(mode="json")))
        digest.update(b"\n")
    return digest.hexdigest()


def _vectors_sha256(vectors: list[list[float]]) -> str:
    canonical = [_float32_vector(vector) for vector in vectors]
    return hashlib.sha256(_canonical_json_bytes(canonical)).hexdigest()


def _persisted_vectors_match(
    expected: list[list[float]],
    persisted: list[list[float]],
) -> bool:
    """Validate a store round-trip without requiring byte-identical decimals.

    Artifact checksums remain exact. This bounded comparison applies only
    after the store has returned the canonical chunks with the expected vector
    count and dimensions, and accommodates at most float32-scale serialization
    drift. Materially changed components still fail closed.
    """

    if len(expected) != len(persisted):
        return False
    if _vectors_sha256(expected) == _vectors_sha256(persisted):
        return True
    for expected_vector, persisted_vector in zip(expected, persisted, strict=True):
        if len(expected_vector) != len(persisted_vector):
            return False
        expected_float32 = _float32_vector(expected_vector)
        persisted_float32 = _float32_vector(persisted_vector)
        if not all(
            math.isclose(
                expected_value,
                persisted_value,
                rel_tol=_PERSISTED_VECTOR_REL_TOLERANCE,
                abs_tol=_PERSISTED_VECTOR_ABS_TOLERANCE,
            )
            for expected_value, persisted_value in zip(
                expected_float32,
                persisted_float32,
                strict=True,
            )
        ):
            return False
    return True


def _float32_vector(vector: list[float]) -> list[float]:
    """Canonicalize the manifest's declared float32 storage contract."""

    normalized: list[float] = []
    for value in vector:
        converted = struct.unpack("!f", struct.pack("!f", float(value)))[0]
        normalized.append(0.0 if converted == 0.0 else converted)
    return normalized


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _package_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("sentence-transformers", "transformers", "torch", "chromadb", "psycopg"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def _hardware_metadata() -> dict[str, str]:
    return {
        "platform": sys.platform,
        "machine": platform.machine() or "unknown",
        "processor": platform.processor() or "unknown",
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)
