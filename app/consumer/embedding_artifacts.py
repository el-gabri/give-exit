"""Canonical serialization and checksums for embedding-generation artifacts.

Generation manifests, shard files and the cross-generation reuse index must
agree byte for byte on how chunks and vectors are serialized and hashed, so
those rules live here.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

from app.core.hashing import canonical_json_bytes, canonical_json_sha256, sha256_hex
from app.schemas.rag import Chunk


def float32_vector(vector: list[float]) -> list[float]:
    """Canonicalize the manifest's declared float32 storage contract."""

    normalized: list[float] = []
    for value in vector:
        converted = struct.unpack("!f", struct.pack("!f", float(value)))[0]
        normalized.append(0.0 if converted == 0.0 else converted)
    return normalized


def chunk_ids_sha256(chunks: list[Chunk]) -> str:
    return sha256_hex("\n".join(chunk.chunk_id for chunk in chunks))


def chunks_sha256(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(canonical_json_bytes(chunk.model_dump(mode="json")))
        digest.update(b"\n")
    return digest.hexdigest()


def vectors_sha256(vectors: list[list[float]]) -> str:
    return canonical_json_sha256([float32_vector(vector) for vector in vectors])


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
