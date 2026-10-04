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

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.retrieval import build_legal_queries
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.scope import is_consumer_scope
from app.core.config import Settings
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.schemas.evaluation import ConsumerLegalGoldenDataset

if TYPE_CHECKING:
    from app.rag.pipeline import RagPipeline
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
QUERY_VECTOR_DIRECTORY = Path("evaluation") / "query_vectors"


def query_vector_root(settings: Settings) -> Path:
    """Where the configured evaluation keeps its query-vector caches."""
    return settings.data_dir / QUERY_VECTOR_DIRECTORY


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


@dataclass(frozen=True, slots=True)
class CaseCoverage:
    """How many of one case's queries were already cached and how many were embedded."""

    case_id: str
    cached: int
    embedded: int


def golden_queries(dataset: ConsumerLegalGoldenDataset) -> dict[str, list[str]]:
    """The production ranking queries of every case the scope gate lets through."""

    return {
        case.case_id: build_legal_queries(
            ConsumerCaseFacts(
                complaint_summary=case.complaint, desired_resolution=case.desired_resolution
            )
        )
        for case in dataset.cases
        if is_consumer_scope(complaint=case.complaint)
    }


def missing_golden_queries(
    embedder: CachedQueryEmbedder, dataset: ConsumerLegalGoldenDataset
) -> int:
    """How many distinct golden queries of ``dataset`` the bound cache lacks."""

    cache = embedder.cache
    if cache is None:
        raise ValueError("the configured embedding model has no pinned revision; nothing is cached")
    queries = {query for texts in golden_queries(dataset).values() for query in texts}
    return sum(cache.get(query) is None for query in queries)


async def fill_query_vectors(
    embedder: CachedQueryEmbedder,
    queries_by_case: Mapping[str, list[str]],
    *,
    embed: bool = True,
) -> list[CaseCoverage]:
    """Embed each case's missing queries in one call, persisting before the next case.

    The embedder is called directly, not through the pipeline's query guard, so
    a slow CPU model cannot time out into a lexical fallback. With
    ``embed=False`` nothing is embedded and the result only reports coverage.
    """

    cache = embedder.cache
    if cache is None:
        raise ValueError("the configured embedding model has no pinned revision; nothing is cached")
    coverage: list[CaseCoverage] = []
    for case_id, queries in queries_by_case.items():
        cached = sum(cache.get(query) is not None for query in queries)
        embedded = len(queries) - cached if embed else 0
        if embedded:
            await embedder.embed_queries(queries)
        coverage.append(CaseCoverage(case_id, cached, embedded))
    return coverage


def _configured_embedder() -> tuple[CachedQueryEmbedder, RagPipeline]:
    from app.evaluation.consumer_retrievers import cached_configured_pipeline

    pipeline, embedder = cached_configured_pipeline(get_default_legal_corpus())
    return embedder, pipeline


async def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fill the golden query-vector cache for the configured embedding model, "
            "one case at a time."
        )
    )
    parser.add_argument("dataset", nargs="?", default="eval_data/consumer_legal_retrieval")
    parser.add_argument(
        "--case", action="append", default=[], metavar="CASE_ID", help="only this case (repeatable)"
    )
    parser.add_argument(
        "--check", action="store_true", help="embed nothing; exit 1 if any query is missing"
    )
    args = parser.parse_args(argv)
    queries = golden_queries(load_consumer_legal_dataset(Path(args.dataset)))
    if args.case:
        unknown = sorted(set(args.case) - queries.keys())
        if unknown:
            parser.error("not an in-scope golden case: " + ", ".join(unknown))
        queries = {case_id: queries[case_id] for case_id in args.case}
    embedder, pipeline = _configured_embedder()
    try:
        cache = embedder.cache
        if cache is None:
            print(
                "query_vectors: the configured embedding model has no pinned revision; "
                "nothing is cached",
                file=sys.stderr,
            )
            return 2
        coverage = await fill_query_vectors(embedder, queries, embed=not args.check)
    finally:
        pipeline.close()
    for item in coverage:
        print(f"{item.case_id}: {item.cached} cached, {item.embedded} embedded")
    total = sum(len(texts) for texts in queries.values())
    covered = sum(item.cached + item.embedded for item in coverage)
    print(f"coverage: {covered}/{total} golden queries cached in {cache.directory}")
    return 1 if covered < total else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_cli()))


def _record(key: str, vector: list[float]) -> str:
    return json.dumps({"query_sha256": key, "vector": vector}, separators=(",", ":")) + "\n"
