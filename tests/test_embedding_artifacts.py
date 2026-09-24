"""Canonical serialization and checksums shared by generations and reuse."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.consumer.embedding_artifacts import (
    atomic_write,
    canonical_json_bytes,
    chunk_ids_sha256,
    chunks_sha256,
    float32_vector,
    vectors_sha256,
)
from app.consumer.legal_corpus import get_default_legal_corpus


def test_float32_canonicalization_is_idempotent_and_drops_negative_zero() -> None:
    values = float32_vector([0.1, -0.0, 1.0])

    assert values == [0.10000000149011612, 0.0, 1.0]
    assert str(values[1]) == "0.0"
    assert float32_vector(values) == values


def test_canonical_json_is_sorted_compact_utf8_and_rejects_nan() -> None:
    assert canonical_json_bytes({"b": 1, "a": "ç"}) == '{"a":"ç","b":1}'.encode()
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("nan")})


def test_checksums_follow_order_and_content() -> None:
    chunks = get_default_legal_corpus().as_chunks()[:3]

    assert chunk_ids_sha256(chunks) != chunk_ids_sha256(list(reversed(chunks)))
    assert chunks_sha256(chunks) == chunks_sha256([chunk.model_copy() for chunk in chunks])
    assert vectors_sha256([[0.1, 0.2]]) == vectors_sha256(
        [[0.10000000149011612, 0.20000000298023224]]
    )


def test_atomic_write_creates_parents_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifact.bin"

    atomic_write(target, b"one")
    atomic_write(target, b"two")

    assert target.read_bytes() == b"two"
    assert list(target.parent.iterdir()) == [target]
