# Cross-Generation Vector Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a legal embedding generation is built, reuse the vectors of chunks whose text is unchanged from a verified earlier generation, so that a corpus release re-embeds only what is new.

**Architecture:** The canonical-serialization and checksum helpers move out of `embedding_generation.py` into `app/consumer/embedding_artifacts.py`. A new `app/consumer/vector_reuse.py` indexes the verified shards of earlier generations by chunk-text SHA-256. `EmbeddingGenerationManager.build_and_activate` looks vectors up there, runs a canary before trusting them, embeds only the misses, and records provenance in each JSONL line and in a v2 manifest. `preindex_legal` gains `--no-reuse`.

**Tech Stack:** Python 3.10+ syntax, Pydantic v2, pytest (`asyncio_mode = "auto"`), gzip JSONL artifacts, ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-09-12-lgpd-codigo-civil-corpus-design.md`, section 6.5 (vector reuse) and section 6.6 (rollout).

**Delivery order:** second of three plans. It must be merged before Plan C's reindex. It changes neither the corpus nor online retrieval.

## Global Constraints

- A vector is reused only when the SHA-256 of the chunk text matches, and only from a generation that meets all of these:
  - status `validated` or `active`;
  - `provenance == "embedded"`;
  - `contract.document_identity()` equal to the current contract's, which includes the output dimension.
- Reuse needs a pinned output dimension (`LITIGATION_EMBEDDING_EXPECTED_DIMENSIONS`, 2560 in `.env.example`). Without it, reuse is off.
- `adopted_existing_vectors` generations are never reuse sources.
- The canary re-embeds the **2** reused texts with the lowest text hashes and requires cosine **≥ 0.9999**. On failure the build stops: the manifest status becomes `failed`, the error is recorded, and the message suggests `--no-reuse`.
- Reuse must not change `generation_id`. `provenance` stays `embedded`.
- New manifests are written with `schema_version = "embedding-generation-manifest-v2"`. v1 manifests stay readable.
- `--force` rebuilds from scratch without reuse. `--no-reuse` disables reuse but keeps the resume of the generation's own verified shards.
- Existing tests in `tests/test_embedding_generation.py` must pass unchanged.
- Lint as CI does: `python -m ruff check app tests frontend`. Types: `python -m mypy app`. `python` means the project virtualenv interpreter; run commands from the repository root.
- New modules `app.consumer.embedding_artifacts` and `app.consumer.vector_reuse` join `[tool.coverage.run] source` at 100% branch coverage.
- Commit messages follow Conventional Commits and end with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA`

## File Structure

| File | Responsibility |
|---|---|
| `app/consumer/embedding_artifacts.py` (new) | Canonical JSON, float32 canonicalization, chunk/vector checksums, atomic writes |
| `app/consumer/vector_reuse.py` (new) | `ReusableVectorIndex` over verified shards of earlier generations; cosine; canary constants |
| `app/consumer/embedding_generation.py` (modify) | Uses the artifact helpers; reuse, canary and provenance in `build_and_activate` |
| `app/schemas/embedding.py` (modify) | `ReuseCanary`; manifest v2 fields; shard `reused_chunk_count` |
| `app/consumer/legal_index.py` (modify) | `reuse` flag; `reused_vectors` and `reuse_sources` in the result |
| `app/consumer/preindex_legal.py` (modify) | `--no-reuse` flag and reuse summary output |
| `tests/test_embedding_artifacts.py`, `tests/test_vector_reuse.py` (new) | Helper behaviour; reuse, canary and source-selection behaviour |
| `docs/adr/0017-cross-generation-vector-reuse.md` (new), `docs/adr/0014-resumable-embedding-generations.md`, `docs/architecture.md`, `README.md`, `README-pt.md` (modify) | Decision record and operator documentation |

---

### Task 1: Extract the artifact helpers

**Files:**
- Create: `app/consumer/embedding_artifacts.py`
- Modify: `app/consumer/embedding_generation.py`: remove `_float32_vector`, `_canonical_json_bytes`, `_chunk_ids_sha256`, `_chunks_sha256`, `_vectors_sha256` and `_atomic_write`, and use the public helpers
- Modify: `pyproject.toml` (`[tool.coverage.run] source`)
- Test: `tests/test_embedding_artifacts.py`

**Interfaces:**
- Produces, in `app.consumer.embedding_artifacts`:
  - `canonical_json_bytes(value: Any) -> bytes`
  - `float32_vector(vector: list[float]) -> list[float]`
  - `chunk_ids_sha256(chunks: list[Chunk]) -> str`
  - `chunks_sha256(chunks: list[Chunk]) -> str`
  - `vectors_sha256(vectors: list[list[float]]) -> str`
  - `atomic_write(path: Path, data: bytes) -> None`

- [ ] **Step 1: Create the working branch**

```bash
git switch -c feat/vector-reuse
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_embedding_artifacts.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_embedding_artifacts.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.consumer.embedding_artifacts'`.

- [ ] **Step 4: Create the module**

Create `app/consumer/embedding_artifacts.py`. The function bodies are moved verbatim from `embedding_generation.py`:

```python
"""Canonical serialization and checksums for embedding-generation artifacts.

Generation manifests, shard files and the cross-generation reuse index must
agree byte for byte on how chunks and vectors are serialized and hashed, so
those rules live here.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

from app.schemas.rag import Chunk


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def float32_vector(vector: list[float]) -> list[float]:
    """Canonicalize the manifest's declared float32 storage contract."""

    normalized: list[float] = []
    for value in vector:
        converted = struct.unpack("!f", struct.pack("!f", float(value)))[0]
        normalized.append(0.0 if converted == 0.0 else converted)
    return normalized


def chunk_ids_sha256(chunks: list[Chunk]) -> str:
    return hashlib.sha256("\n".join(chunk.chunk_id for chunk in chunks).encode("utf-8")).hexdigest()


def chunks_sha256(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(canonical_json_bytes(chunk.model_dump(mode="json")))
        digest.update(b"\n")
    return digest.hexdigest()


def vectors_sha256(vectors: list[list[float]]) -> str:
    canonical = [float32_vector(vector) for vector in vectors]
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
```

- [ ] **Step 5: Use it from `embedding_generation.py`**

In `app/consumer/embedding_generation.py`:
1. Delete the functions `_float32_vector`, `_canonical_json_bytes`, `_chunk_ids_sha256`, `_chunks_sha256`, `_vectors_sha256` and `_atomic_write`.
2. Add:

```python
from app.consumer.embedding_artifacts import (
    atomic_write,
    canonical_json_bytes,
    chunk_ids_sha256,
    chunks_sha256,
    float32_vector,
    vectors_sha256,
)
```

3. Rename every remaining call: `_float32_vector(` → `float32_vector(`, `_canonical_json_bytes(` → `canonical_json_bytes(`, `_chunk_ids_sha256(` → `chunk_ids_sha256(`, `_chunks_sha256(` → `chunks_sha256(`, `_vectors_sha256(` → `vectors_sha256(`, `_atomic_write(` → `atomic_write(`.
4. Remove the imports that are now unused (`os`, `struct`, and `json` if nothing else uses it). `python -m ruff check` will list them.

In `pyproject.toml`, set:

```toml
source = [
    "app.consumer.embedding_artifacts",
    "app.consumer.ground_selection",
    "app.rag.reranking",
]
```

(`app.consumer.ground_selection` comes from Plan A. If Plan A is not merged yet, leave it out.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_embedding_artifacts.py tests/test_embedding_generation.py -q`
Expected: all pass. The existing generation tests prove the move changed no behaviour.

Run: `python -m pytest tests/test_embedding_artifacts.py --cov=app.consumer.embedding_artifacts --cov-branch --cov-report=term-missing -q`
Expected: 100%.

- [ ] **Step 7: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/consumer/embedding_artifacts.py app/consumer/embedding_generation.py tests/test_embedding_artifacts.py pyproject.toml
git commit -F - <<'EOF'
refactor(embeddings): move artifact serialization and checksums to a module

The cross-generation reuse index needs the same canonical JSON, float32 and
checksum rules as generation shards.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 2: Manifest v2 fields

**Files:**
- Modify: `app/schemas/embedding.py`
- Test: `tests/test_vector_reuse.py` (new file, first tests)

**Interfaces:**
- Produces, in `app.schemas.embedding`:
  - `class ReuseCanary(BaseModel)` with `text_sha256s: tuple[str, ...]`, `min_cosine: float`, `threshold: float`, `passed: bool` (frozen, `extra="forbid"`)
  - `EmbeddingShardManifest.reused_chunk_count: int = 0`
  - `EmbeddingGenerationManifest.schema_version` accepts `"embedding-generation-manifest-v1"` and `"embedding-generation-manifest-v2"` (default v2)
  - `EmbeddingGenerationManifest.reused_chunk_count: int = 0`, `.reuse_sources: list[str] = []`, `.reuse_canary: ReuseCanary | None = None`
  - Validator: an `adopted_existing_vectors` manifest must have `reused_chunk_count == 0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vector_reuse.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_vector_reuse.py -q`
Expected: FAIL. The written manifest has `schema_version` v1, and `payload.pop("reused_chunk_count")` raises `KeyError`.

- [ ] **Step 3: Implement the schema changes**

In `app/schemas/embedding.py`:

1. Add after `class EmbeddingContract`:

```python
class ReuseCanary(BaseModel):
    """Evidence that reused vectors still live in the current embedding space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text_sha256s: tuple[str, ...] = Field(min_length=1)
    min_cosine: float
    threshold: float = Field(gt=0, le=1)
    passed: bool
```

2. In `class EmbeddingShardManifest`, after `output_dimension: int = Field(ge=1)`, add:

```python
    reused_chunk_count: int = Field(default=0, ge=0)
```

3. In `class EmbeddingGenerationManifest`, replace the `schema_version` declaration with:

```python
    schema_version: Literal[
        "embedding-generation-manifest-v1",
        "embedding-generation-manifest-v2",
    ] = "embedding-generation-manifest-v2"
```

and after `error: str | None = None`, add:

```python
    reused_chunk_count: int = Field(default=0, ge=0)
    reuse_sources: list[str] = Field(default_factory=list)
    reuse_canary: ReuseCanary | None = None
```

4. In `_validate_provenance_fields`, inside the `if self.provenance == "adopted_existing_vectors":` branch, after the revision check, add:

```python
            if self.reused_chunk_count:
                raise ValueError("adopted vectors cannot be reused from other generations")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_vector_reuse.py tests/test_embedding_generation.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/schemas/embedding.py tests/test_vector_reuse.py
git commit -F - <<'EOF'
feat(embeddings): add v2 generation manifest fields for vector reuse

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 3: Reusable-vector index

**Files:**
- Create: `app/consumer/vector_reuse.py`
- Modify: `pyproject.toml` (coverage source)
- Test: `tests/test_vector_reuse.py` (append)

**Interfaces:**
- Consumes: `canonical_json_bytes`, `chunk_ids_sha256`, `chunks_sha256`, `float32_vector`, `vectors_sha256` (Task 1); manifest v2 (Task 2).
- Produces, in `app.consumer.vector_reuse`:
  - `CANARY_SIZE = 2`, `CANARY_MIN_COSINE = 0.9999`
  - `class ReuseCanaryError(RuntimeError)`
  - `text_sha256(text: str) -> str`
  - `cosine(left: Sequence[float], right: Sequence[float]) -> float`, which is 0.0 when the lengths differ
  - `@dataclass(frozen=True, slots=True) class ReusedVector` with `vector: tuple[float, ...]`, `generation_id: str`, `chunk_id: str`, `artifact_sha256: str` and `source() -> dict[str, str]`
  - `class ReusableVectorIndex` with `empty()`, `from_artifacts(artifacts_dir: Path, contract: EmbeddingContract, *, exclude_generation_id: str)`, `__len__`, `lookup(text: str) -> ReusedVector | None`, `require(text: str) -> ReusedVector` (raises `KeyError`) and the attribute `sources: tuple[str, ...]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vector_reuse.py` (merge the imports into the existing import block):

```python
import gzip
import hashlib

from app.consumer.vector_reuse import ReusableVectorIndex, cosine
from app.schemas.embedding import EmbeddingContract


def _contract(dimension: int | None = 128) -> EmbeddingContract:
    return EmbeddingContract(
        model_repository="mock-hashed-bow-v1:128",
        model_revision="mock-hashed-bow-v1",
        output_dimension=dimension,
        document_formatter_version="plain-document-v1",
        query_formatter_version="instruction-prefix-v2",
    )


async def test_source_selection_rules(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, MockEmbeddingClient())
    corpus = _corpus(40)
    await preindex_legal_corpus(pipeline, corpus)
    manifest = _manifest(pipeline, corpus)
    artifacts = tmp_path / "generations"
    chunk = corpus.as_chunks()[0]

    index = ReusableVectorIndex.from_artifacts(
        artifacts, manifest.contract, exclude_generation_id="other"
    )
    hit = index.lookup(chunk.text)

    assert len(index) == len(corpus.as_chunks())
    assert index.sources == (manifest.generation_id,)
    assert hit is not None and hit.generation_id == manifest.generation_id
    assert hit.source()["chunk_id"] == chunk.chunk_id
    assert (
        len(
            ReusableVectorIndex.from_artifacts(
                artifacts, manifest.contract, exclude_generation_id=manifest.generation_id
            )
        )
        == 0
    )
    other_identity = manifest.contract.model_copy(
        update={"document_formatter_version": "prefixed-document-v9"}
    )
    assert (
        len(ReusableVectorIndex.from_artifacts(artifacts, other_identity, exclude_generation_id="x"))
        == 0
    )
    path = _manifest_path(pipeline, corpus)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        len(
            ReusableVectorIndex.from_artifacts(
                artifacts, manifest.contract, exclude_generation_id="other"
            )
        )
        == 0
    )


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

    assert len(ReusableVectorIndex.from_artifacts(tmp_path, _contract(), exclude_generation_id="x")) == 0
    assert (
        len(ReusableVectorIndex.from_artifacts(tmp_path, _contract(None), exclude_generation_id="x"))
        == 0
    )
    with pytest.raises(KeyError):
        ReusableVectorIndex.empty().require("texto")
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([1.0, 0.0], [2.0, 0.0]) == 1.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
```

`gzip` is used by the Task 4 tests appended below; importing it now keeps the import block in one place.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_vector_reuse.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.consumer.vector_reuse'`.

- [ ] **Step 3: Create the module**

Create `app/consumer/vector_reuse.py`:

```python
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
    dot = sum(a * b for a, b in zip(left, right))
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
```

`gzip.BadGzipFile` and `json.JSONDecodeError` both subclass the exceptions caught above (`OSError` and `ValueError`), so a truncated or non-gzip file is skipped, not raised.

In `pyproject.toml`, add `"app.consumer.vector_reuse",` to `[tool.coverage.run] source`, keeping alphabetical order.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_vector_reuse.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/consumer/vector_reuse.py tests/test_vector_reuse.py pyproject.toml
git commit -F - <<'EOF'
feat(embeddings): index verified vectors of earlier generations by text hash

Only validated or active embedded generations with the same document
identity qualify; adopted generations, unreadable manifests and shards that
fail any checksum are skipped.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 4: Reuse, canary and provenance in generation builds

**Files:**
- Modify: `app/consumer/embedding_generation.py` (`build_and_activate`, `_write_shard`, a new `_check_reuse_canary`)
- Modify: `app/consumer/legal_index.py` (`LegalIndexResult`, `preindex_legal_corpus`)
- Test: `tests/test_vector_reuse.py` (append)

**Interfaces:**
- Consumes: `ReusableVectorIndex`, `ReuseCanaryError`, `cosine`, `text_sha256`, `CANARY_SIZE`, `CANARY_MIN_COSINE` (Task 3); `ReuseCanary` (Task 2).
- Produces:
  - `EmbeddingGenerationManager.build_and_activate(*, force: bool = False, reuse: bool = True) -> EmbeddingGenerationManifest`
  - `preindex_legal_corpus(rag, corpus, *, force: bool = False, reuse: bool = True) -> LegalIndexResult`
  - `LegalIndexResult.reused_vectors: int = 0`, `LegalIndexResult.reuse_sources: tuple[str, ...] = ()`
  - Every reused JSONL line carries `"source": {"generation_id", "chunk_id", "artifact_sha256"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vector_reuse.py` (merge the imports into the import block):

```python
from app.consumer.legal_index import adopt_legal_corpus_index, legal_corpus_is_indexed
from app.consumer.vector_reuse import CANARY_SIZE, ReuseCanaryError
from app.schemas.embedding import EmbeddingGenerationStatus


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
    sources = [
        json.loads(line).get("source")
        for shard in manifest.shards
        for line in gzip.decompress((generation_dir / shard.artifact_file).read_bytes()).splitlines()
    ]
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

    result = await preindex_legal_corpus(_pipeline(tmp_path, OtherFormatterEmbedder()), _corpus(60))

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_vector_reuse.py -q`
Expected: FAIL. `LegalIndexResult` has no attribute `reused_vectors`, and `preindex_legal_corpus` rejects the `reuse` keyword.

- [ ] **Step 3: Implement reuse in the generation manager**

In `app/consumer/embedding_generation.py`:

1. Add imports:

```python
from app.consumer.vector_reuse import (
    CANARY_MIN_COSINE,
    CANARY_SIZE,
    ReusableVectorIndex,
    ReuseCanaryError,
    cosine,
    text_sha256,
)
from app.schemas.embedding import ReuseCanary
```

(merge `ReuseCanary` into the existing `from app.schemas.embedding import (...)` block).

2. Replace `build_and_activate` from its signature down to the line `chunks, vectors = self._load_complete_generation(manifest)`, keeping everything from that line onward unchanged:

```python
    async def build_and_activate(
        self,
        *,
        force: bool = False,
        reuse: bool = True,
    ) -> EmbeddingGenerationManifest:
        manifest = self._new_manifest(provenance="embedded")
        if not force:
            existing = self._load_manifest(required=False)
            if existing is not None:
                self._validate_manifest_identity(existing)
                manifest = existing
        manifest.schema_version = "embedding-generation-manifest-v2"
        if manifest.reused_chunk_count == 0:
            manifest.reuse_canary = None
        manifest.status = EmbeddingGenerationStatus.BUILDING
        manifest.error = None
        self._save_manifest(manifest)

        completed = {shard.shard_index: shard for shard in manifest.shards}
        pending: list[tuple[int, list[Chunk]]] = []
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
            pending.append((shard_index, chunks))

        reuse_index = ReusableVectorIndex.empty()
        if reuse and not force and pending:
            reuse_index = ReusableVectorIndex.from_artifacts(
                self._generation_dir.parent,
                self._contract,
                exclude_generation_id=self._generation_id,
            )
        try:
            await self._check_reuse_canary(manifest, reuse_index, pending)
        except Exception as exc:
            manifest.status = EmbeddingGenerationStatus.FAILED
            manifest.error = f"{type(exc).__name__}: {exc}"
            manifest.updated_at = _now()
            self._save_manifest(manifest)
            raise

        for shard_index, chunks in pending:
            try:
                hits = [reuse_index.lookup(chunk.text) for chunk in chunks]
                missing = [
                    chunk.text for chunk, hit in zip(chunks, hits, strict=True) if hit is None
                ]
                fresh = iter(await self._rag.embed_document_batch(missing) if missing else [])
                vectors = [list(hit.vector) if hit is not None else next(fresh) for hit in hits]
                dimension = validate_embedding_vectors(
                    vectors,
                    expected_count=len(chunks),
                    expected_dimension=manifest.contract.output_dimension,
                )
                if manifest.contract.output_dimension is None:
                    manifest.contract = manifest.contract.model_copy(
                        update={"output_dimension": dimension}
                    )
                shard_manifest = self._write_shard(
                    shard_index,
                    chunks,
                    vectors,
                    sources=[hit.source() if hit is not None else None for hit in hits],
                )
                manifest.shards = sorted(
                    [item for item in manifest.shards if item.shard_index != shard_index]
                    + [shard_manifest],
                    key=lambda item: item.shard_index,
                )
                manifest.completed_chunk_count = sum(
                    item.chunk_count for item in manifest.shards
                )
                manifest.reused_chunk_count = sum(
                    item.reused_chunk_count for item in manifest.shards
                )
                manifest.reuse_sources = sorted(
                    {*manifest.reuse_sources, *(hit.generation_id for hit in hits if hit)}
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
                    reused_chunks=shard_manifest.reused_chunk_count,
                    output_dimension=dimension,
                )
            except Exception as exc:
                manifest.status = EmbeddingGenerationStatus.FAILED
                manifest.error = f"{type(exc).__name__}: {exc}"
                manifest.updated_at = _now()
                self._save_manifest(manifest)
                raise

```

3. Add this method to the class, right after `build_and_activate`:

```python
    async def _check_reuse_canary(
        self,
        manifest: EmbeddingGenerationManifest,
        reuse_index: ReusableVectorIndex,
        pending: list[tuple[int, list[Chunk]]],
    ) -> None:
        """Re-embed a few reused texts and refuse vectors from another space."""

        reused_texts = sorted(
            {
                (text_sha256(chunk.text), chunk.text)
                for _, chunks in pending
                for chunk in chunks
                if reuse_index.lookup(chunk.text) is not None
            }
        )
        if not reused_texts:
            return
        probe = reused_texts[:CANARY_SIZE]
        fresh = await self._rag.embed_document_batch([text for _, text in probe])
        cosines = [
            cosine(float32_vector(vector), reuse_index.require(text).vector)
            for (_, text), vector in zip(probe, fresh, strict=True)
        ]
        manifest.reuse_canary = ReuseCanary(
            text_sha256s=tuple(digest for digest, _ in probe),
            min_cosine=min(cosines),
            threshold=CANARY_MIN_COSINE,
            passed=min(cosines) >= CANARY_MIN_COSINE,
        )
        self._save_manifest(manifest)
        if not manifest.reuse_canary.passed:
            raise ReuseCanaryError(
                "reused vectors differ from the current model "
                f"(minimum cosine {min(cosines):.6f} < {CANARY_MIN_COSINE}); "
                "rerun with --no-reuse"
            )
```

4. Replace `_write_shard` with:

```python
    def _write_shard(
        self,
        shard_index: int,
        chunks: list[Chunk],
        vectors: list[list[float]],
        *,
        sources: list[dict[str, str] | None] | None = None,
    ) -> EmbeddingShardManifest:
        float32_vectors = [float32_vector(vector) for vector in vectors]
        dimension = validate_embedding_vectors(
            float32_vectors,
            expected_count=len(chunks),
            expected_dimension=self._contract.output_dimension,
        )
        origins: list[dict[str, str] | None] = sources or [None] * len(chunks)
        records: list[bytes] = []
        for chunk, vector, origin in zip(chunks, float32_vectors, origins, strict=True):
            record: dict[str, Any] = {"chunk": chunk.model_dump(mode="json"), "vector": vector}
            if origin is not None:
                record["source"] = origin
            records.append(canonical_json_bytes(record))
        compressed = gzip.compress(b"\n".join(records), compresslevel=6, mtime=0)
        filename = f"embeddings-{shard_index:04d}.jsonl.gz"
        atomic_write(self._generation_dir / filename, compressed)
        return EmbeddingShardManifest(
            shard_index=shard_index,
            artifact_file=filename,
            artifact_sha256=hashlib.sha256(compressed).hexdigest(),
            chunk_count=len(chunks),
            chunk_ids_sha256=chunk_ids_sha256(chunks),
            chunks_sha256=chunks_sha256(chunks),
            vectors_sha256=vectors_sha256(float32_vectors),
            output_dimension=dimension,
            reused_chunk_count=sum(origin is not None for origin in origins),
        )
```

- [ ] **Step 4: Pass the flag through the legal index**

In `app/consumer/legal_index.py`:

1. In `LegalIndexResult`, after `manifest_path: Path | None = None`, add:

```python
    reused_vectors: int = 0
    reuse_sources: tuple[str, ...] = ()
```

2. Change the signature of `preindex_legal_corpus` to

```python
async def preindex_legal_corpus(
    rag: RagPipeline,
    corpus: LegalCorpus,
    *,
    force: bool = False,
    reuse: bool = True,
) -> LegalIndexResult:
```

and replace its `if rag.embedding_artifacts_dir is not None:` block (the one that builds a generation) with:

```python
    if rag.embedding_artifacts_dir is not None:
        manager = EmbeddingGenerationManager(rag, corpus)
        manifest = await manager.build_and_activate(force=force, reuse=reuse)
        return LegalIndexResult(
            action="indexed",
            doc_id=doc_id,
            chunks=len(chunks),
            generation_id=manifest.generation_id,
            manifest_path=manager.manifest_path,
            reused_vectors=manifest.reused_chunk_count,
            reuse_sources=tuple(manifest.reuse_sources),
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_vector_reuse.py tests/test_embedding_generation.py tests/test_embedding_artifacts.py -q`
Expected: all pass.

Run: `python -m pytest tests/test_vector_reuse.py tests/test_embedding_artifacts.py --cov=app.consumer.vector_reuse --cov=app.consumer.embedding_artifacts --cov-branch --cov-report=term-missing -q`
Expected: 100% for both modules.

- [ ] **Step 6: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/consumer/embedding_generation.py app/consumer/legal_index.py tests/test_vector_reuse.py
git commit -F - <<'EOF'
feat(embeddings): reuse unchanged chunk vectors when building a generation

A canary re-embeds two reused texts and aborts on cosine < 0.9999. Reused
lines record their source generation, chunk and artifact; the manifest
records reused counts, sources and the canary.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 5: CLI flag and documentation

**Files:**
- Modify: `app/consumer/preindex_legal.py`
- Create: `docs/adr/0017-cross-generation-vector-reuse.md`
- Modify: `docs/adr/0014-resumable-embedding-generations.md` (status section)
- Modify: `docs/architecture.md` (the "Offline legal indexing is a resumable batch path" paragraph and the ADR index)
- Modify: `README.md` (the paragraph that starts "The first JUÁ CPU run can take tens of minutes"), `README-pt.md` (the paragraph that starts "O primeiro comando pode levar dezenas de minutos")
- Test: `tests/test_vector_reuse.py` (append)

**Interfaces:**
- Consumes: `preindex_legal_corpus(..., reuse=...)` and `LegalIndexResult.reused_vectors`/`.reuse_sources` (Task 4).
- Produces: CLI flag `--no-reuse`. It may not be combined with `--check` or `--adopt-source-index`; that combination returns exit code 1 with a message.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vector_reuse.py` (merge the import):

```python
from app.consumer import preindex_legal


def test_preindex_cli_exposes_no_reuse() -> None:
    assert preindex_legal._parser().parse_args(["--no-reuse"]).no_reuse is True


def test_no_reuse_only_applies_to_building(capsys: pytest.CaptureFixture[str]) -> None:
    assert preindex_legal.main(["--no-reuse", "--check"]) == 1
    assert "--no-reuse" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_vector_reuse.py -q -k "no_reuse"`
Expected: FAIL. argparse exits with code 2 on the unrecognized `--no-reuse`.

- [ ] **Step 3: Implement the flag**

In `app/consumer/preindex_legal.py`:

1. In `_parser()`, add before `return parser`:

```python
    parser.add_argument(
        "--no-reuse",
        action="store_true",
        help=(
            "não reaproveita vetores de gerações anteriores; a retomada da própria "
            "geração continua valendo"
        ),
    )
```

2. Add the parameter `no_reuse: bool` to `_run` (after `attested_source_revision`). As the first statement of its body, before the existing checks, add:

```python
    if no_reuse and (check or adopt_source_index):
        raise ValueError("--no-reuse só se aplica à construção de uma geração")
```

3. Replace `result = await preindex_legal_corpus(rag, corpus, force=force)` with `result = await preindex_legal_corpus(rag, corpus, force=force, reuse=not no_reuse)`, and after the existing `print(f"Geração: ...")` line, inside the same `if`, add:

```python
        print(
            f"Vetores reaproveitados de gerações anteriores: {result.reused_vectors} "
            f"(fontes: {', '.join(result.reuse_sources) or 'nenhuma'})"
        )
```

4. In `main`, pass `no_reuse=args.no_reuse` to `_run`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_vector_reuse.py -q`
Expected: all pass.

- [ ] **Step 5: Write ADR 0017 and update the documentation**

Create `docs/adr/0017-cross-generation-vector-reuse.md`:

```markdown
# ADR 0017: Reuse document vectors across embedding generations

## Status

Accepted · Date: 2026-09-12 · Amends: [ADR 0014](0014-resumable-embedding-generations.md)

## Context

A legal corpus release renames every chunk: `ParsedDocument.doc_id` hashes the
whole corpus text and chunk ids embed it. Under ADR 0014 a new release
therefore re-embeds every chunk, even when almost all chunk texts are
unchanged. The Consumer corpus is growing from 472 to about 2,460 chunks, and
the pinned 4B model runs on CPU; the last full generation took about two days
of wall-clock time. Every later amendment of an indexed statute would repeat
that cost.

A document vector depends only on the chunk text and on the document side of
the embedding contract: model repository and revision, dimension, dtype,
normalization and document formatter version.

## Decision

1. When building a generation, index the verified shards of earlier
   generations that are validated or active, have provenance `embedded`, and
   share the current document identity, including a pinned output dimension.
   Key their vectors by the SHA-256 of the chunk text.
2. Never reuse vectors from `adopted_existing_vectors` generations; their
   model revision is only attested.
3. Before using any reused vector, re-embed the two reused texts with the
   lowest hashes and require cosine ≥ 0.9999; otherwise fail the build and
   suggest `--no-reuse`. This catches a document-formatter change without a
   version bump and numerical drift across platforms.
4. Record provenance per vector. Reused JSONL lines carry `source`
   (generation id, chunk id, artifact SHA-256), shards count reused vectors,
   and the v2 manifest records `reused_chunk_count`, `reuse_sources` and the
   canary.
5. Reuse changes neither `provenance: embedded` nor the generation id.
   `--force` rebuilds without reuse; `--no-reuse` disables reuse and keeps
   resume.

## Consequences

- (+) A release that changes a few articles re-embeds only their chunks; the
  LGPD and Civil Code expansion reuses the 472 existing vectors.
- (+) Every reused vector remains traceable to the generation and artifact
  that produced it.
- (-) Reuse trusts that the document identity captures everything that moves
  vectors; the canary samples only two texts.
- (-) A generation must be kept for as long as it is a reuse source.
```

In `docs/adr/0014-resumable-embedding-generations.md`, replace the status body `Accepted` with:

```markdown
Accepted · Amended by [ADR 0017](0017-cross-generation-vector-reuse.md) (cross-generation vector reuse)
```

In `docs/architecture.md`:
1. Append this sentence to the paragraph that begins "Offline legal indexing is a resumable batch path": `A new generation reuses the vectors of chunks whose text is unchanged from a verified earlier embedded generation with the same document identity, after a two-text canary confirms the embedding space (ADR 0017).`
2. In the "ADR index" list, after the `0013` entry, add:

```markdown
- [0014](adr/0014-resumable-embedding-generations.md) — resumable embedding generations
- [0015](adr/0015-bounded-notice-prose-composer.md) — bounded notice prose composer
- [0017](adr/0017-cross-generation-vector-reuse.md) — cross-generation vector reuse
```

In `README.md`, after the paragraph that starts "The first JUÁ CPU run can take tens of minutes", add:

```markdown
A new corpus release reuses the vectors of every chunk whose text did not
change, taken from verified earlier generations of the same model, revision
and document formatter. Two reused texts are re-embedded first as a canary;
if they differ, the build stops and suggests `--no-reuse`, which recomputes
everything while keeping resume.
```

In `README-pt.md`, after the paragraph that starts "O primeiro comando pode levar dezenas de minutos", add:

```markdown
Um novo release do corpus reaproveita os vetores de todo chunk cujo texto não
mudou, vindos de gerações anteriores verificadas do mesmo modelo, revisão e
formatador de documento. Antes, dois textos reaproveitados são re-embedados
como canário; se divergirem, a construção para e sugere `--no-reuse`, que
recalcula tudo mantendo a retomada.
```

- [ ] **Step 6: Full verification**

Run: `python -m pytest -q`
Expected: all pass.
Run: `python -m pytest --cov --cov-report=term-missing -q`
Expected: 100% on every module listed in `[tool.coverage.run] source`.
Run: `python -m ruff check app tests frontend`, `python -m mypy app`, `lint-imports`, `vulture app --min-confidence 90`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add app/consumer/preindex_legal.py tests/test_vector_reuse.py docs/adr/0017-cross-generation-vector-reuse.md docs/adr/0014-resumable-embedding-generations.md docs/architecture.md README.md README-pt.md
git commit -F - <<'EOF'
feat(preindex): add --no-reuse and document cross-generation vector reuse

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

## Notes for the executor

- `test_invalid_source_shards_are_skipped` requires at least 4 source shards. `_corpus(40)` with shard size 25 must produce at least 76 chunks; the assertion `len(manifest.shards) >= 4` guards this. If a future corpus change shrinks it, raise the provision count instead of weakening the assertion.
- Do not run `preindex_legal` against the real configured index in this plan. The real reindex happens in Plan C, Task 10.
