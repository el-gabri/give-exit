"""Golden query vectors cached per embedding contract, for evaluation only.

A configured evaluation embeds the same deterministic golden queries on every
run, and on a CPU the 4B JUÁ model cannot embed all of them in one process.
This cache stores each query's vector once, keyed by the SHA-256 of the query
text, in a directory named after the embedding contract, so later runs read
vectors instead of loading the model.

Only evaluation uses it. The production query path never persists a query:
queries there are consumers' complaints.

Layout: ``<data_dir>/evaluation/query_vectors/<contract_id>/`` holds
``contract.json`` and an append-only ``vectors.jsonl``. No query text is
stored. A crash can leave a partial last line; the next load drops it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.consumer.embedding_artifacts import atomic_write
from app.core.hashing import canonical_json_sha256, sha256_hex
from app.rag.embeddings import (
    EmbeddingBackend,
    embed_document_texts,
    embed_query_texts,
    validate_embedding_vectors,
)

CONTRACT_FILENAME = "contract.json"
VECTORS_FILENAME = "vectors.jsonl"
# Recorded in a pipeline's contract but irrelevant to the vectors themselves.
_NON_VECTOR_FIELDS = frozenset({"require_model_revision"})
_SHA256_HEX_LENGTH = 64


class QueryVectorCacheMiss(RuntimeError):
    """Golden queries that are not cached when the model may not be loaded."""

    def __init__(self, missing: int) -> None:
        self.missing = missing
        super().__init__(
            f"{missing} golden query vector(s) are not cached; "
            "fill the cache with `python -m app.evaluation.query_vectors`"
        )


def cache_contract(configuration: Mapping[str, object]) -> dict[str, object] | None:
    """The vector-determining part of an embedding contract; None when unpinned.

    A model without an exact revision can change under the same name, so its
    vectors are never cached.
    """

    if not str(configuration.get("model_revision") or "").strip():
        return None
    return {
        key: value
        for key, value in sorted(configuration.items())
        if key not in _NON_VECTOR_FIELDS
    }


def contract_id(contract: Mapping[str, object]) -> str:
    """The cache directory name for one contract."""
    return canonical_json_sha256(dict(contract))[:16]


class QueryVectorCache:
    """Vectors of golden queries for one embedding contract, as JSON lines."""

    def __init__(self, directory: Path, contract: Mapping[str, object]) -> None:
        self._directory = directory
        self._contract = dict(contract)
        dimension = self._contract.get("output_dimension")
        self._dimension = int(str(dimension)) if dimension is not None else None
        directory.mkdir(parents=True, exist_ok=True)
        self._check_contract()
        self._vectors = self._load()

    @classmethod
    def open(cls, root: Path, contract: Mapping[str, object]) -> QueryVectorCache:
        return cls(root / contract_id(contract), contract)

    @property
    def directory(self) -> Path:
        return self._directory

    def __len__(self) -> int:
        return len(self._vectors)

    def get(self, query: str) -> list[float] | None:
        vector = self._vectors.get(sha256_hex(query))
        return list(vector) if vector is not None else None

    def add(self, queries: Sequence[str], vectors: Sequence[list[float]]) -> None:
        """Persist new vectors; a query already cached keeps its first vector."""

        validate_embedding_vectors(
            list(vectors), expected_count=len(queries), expected_dimension=self._dimension
        )
        new: dict[str, list[float]] = {}
        for query, vector in zip(queries, vectors, strict=True):
            key = sha256_hex(query)
            if key not in self._vectors and key not in new:
                new[key] = [float(value) for value in vector]
        if not new:
            return
        path = self._directory / VECTORS_FILENAME
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(_record(key, vector) for key, vector in new.items()))
            handle.flush()
            os.fsync(handle.fileno())
        self._vectors.update(new)

    def _check_contract(self) -> None:
        path = self._directory / CONTRACT_FILENAME
        if not path.exists():
            rendered = json.dumps(self._contract, indent=2, sort_keys=True) + "\n"
            atomic_write(path, rendered.encode("utf-8"))
            return
        if json.loads(path.read_text(encoding="utf-8")) != self._contract:
            raise ValueError(f"{path} records a different embedding contract")

    def _load(self) -> dict[str, list[float]]:
        path = self._directory / VECTORS_FILENAME
        if not path.exists():
            return {}
        lines = path.read_text(encoding="utf-8").split("\n")
        tail = lines.pop()  # empty when the file ends with a newline
        vectors: dict[str, list[float]] = {}
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            parsed = self._parse(line)
            if parsed is None:
                raise ValueError(f"{path}:{number} is not a cached query vector")
            vectors.setdefault(parsed[0], parsed[1])
        if tail.strip():
            # An unterminated last line is what a crash mid-append leaves. It is
            # kept only if whole, and the file is rewritten so the next append
            # starts on a fresh line.
            parsed = self._parse(tail)
            if parsed is not None:
                vectors.setdefault(parsed[0], parsed[1])
            rewritten = "".join(_record(key, vector) for key, vector in vectors.items())
            atomic_write(path, rewritten.encode("utf-8"))
        return vectors

    def _parse(self, line: str) -> tuple[str, list[float]] | None:
        try:
            record = json.loads(line)
            key = str(record["query_sha256"])
            vector = [float(value) for value in record["vector"]]
        except (ValueError, TypeError, KeyError):
            return None
        if len(key) != _SHA256_HEX_LENGTH:
            return None
        validate_embedding_vectors([vector], expected_count=1, expected_dimension=self._dimension)
        return key, vector


class CachedQueryEmbedder:
    """An embedding backend that answers queries from a bound QueryVectorCache.

    Unbound, it passes every call through: a model without a pinned revision is
    never cached. Every attribute it does not define is read from the real
    embedder, so a pipeline built on it records the same model, revision,
    instruction and formatter versions as one built without it.
    """

    def __init__(self, inner: EmbeddingBackend, *, require_cached: bool = False) -> None:
        self._inner = inner
        self._require_cached = require_cached
        self._cache: QueryVectorCache | None = None

    @property
    def model_name(self) -> str:
        return str(getattr(self._inner, "model_name", type(self._inner).__name__))

    @property
    def cache(self) -> QueryVectorCache | None:
        return self._cache

    def bind(self, cache: QueryVectorCache) -> None:
        self._cache = cache

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.__dict__["_inner"], name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_documents(texts)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await embed_document_texts(self._inner, texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        cache = self._cache
        if cache is None:
            return await embed_query_texts(self._inner, texts)
        missing = list(dict.fromkeys(text for text in texts if cache.get(text) is None))
        if missing:
            if self._require_cached:
                raise QueryVectorCacheMiss(len(missing))
            cache.add(missing, await embed_query_texts(self._inner, missing))
        vectors = [cache.get(text) for text in texts]
        return [vector for vector in vectors if vector is not None]


def _record(key: str, vector: list[float]) -> str:
    return json.dumps({"query_sha256": key, "vector": vector}, separators=(",", ":")) + "\n"
