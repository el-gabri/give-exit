"""Golden query vectors cached per embedding contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.query_vectors import (
    CONTRACT_FILENAME,
    VECTORS_FILENAME,
    CachedQueryEmbedder,
    QueryVectorCache,
    QueryVectorCacheMiss,
    cache_contract,
    contract_id,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.rag import Chunk

CONTRACT: dict[str, object] = {
    "model_repository": "mock-hashed-bow-v1:128",
    "model_revision": "rev-1",
    "output_dimension": 128,
    "normalization": "l2",
    "document_formatter_version": "plain",
    "query_formatter_version": "instruction-prefix-v2",
    "query_instruction_sha256": None,
}


class _CountingEmbedder(MockEmbeddingClient):
    def __init__(self) -> None:
        super().__init__(query_instruction="Consulta jurídica")
        self.query_batches: list[list[str]] = []

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_batches.append(list(texts))
        return await super().embed_queries(texts)


async def _vectors(*texts: str) -> list[list[float]]:
    return await MockEmbeddingClient().embed_queries(list(texts))


def test_only_a_pinned_contract_is_cached_and_its_id_is_stable() -> None:
    configuration = {**CONTRACT, "require_model_revision": True}

    assert cache_contract(configuration) == CONTRACT
    assert cache_contract({**configuration, "model_revision": None}) is None
    assert cache_contract({**configuration, "model_revision": "  "}) is None
    assert contract_id(CONTRACT) == contract_id(dict(reversed(list(CONTRACT.items()))))
    assert len(contract_id(CONTRACT)) == 16
    assert contract_id({**CONTRACT, "model_revision": "rev-2"}) != contract_id(CONTRACT)


async def test_vectors_survive_a_reopen_without_query_text(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    vectors = await _vectors("primeira consulta", "segunda consulta")
    cache.add(["primeira consulta", "segunda consulta"], vectors)

    reopened = QueryVectorCache.open(tmp_path, CONTRACT)

    assert len(reopened) == 2
    assert reopened.get("primeira consulta") == vectors[0]
    assert reopened.get("terceira") is None
    assert "primeira" not in (reopened.directory / VECTORS_FILENAME).read_text(encoding="utf-8")
    contract_file = reopened.directory / CONTRACT_FILENAME
    assert json.loads(contract_file.read_text(encoding="utf-8")) == CONTRACT



async def test_a_query_already_cached_keeps_its_first_vector(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    first, other = await _vectors("consulta", "outra coisa")
    cache.add(["consulta"], [first])

    cache.add(["consulta", "consulta"], [other, other])

    assert cache.get("consulta") == first
    lines = (cache.directory / VECTORS_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

async def test_a_partial_last_line_is_dropped_and_the_file_repaired(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["inteira"], await _vectors("inteira"))
    path = cache.directory / VECTORS_FILENAME
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"query_sha256": "abc", "vector": [0.1, 0.')

    repaired = QueryVectorCache.open(tmp_path, CONTRACT)
    repaired.add(["depois"], await _vectors("depois"))

    assert len(QueryVectorCache.open(tmp_path, CONTRACT)) == 2
    assert path.read_bytes().endswith(b"\n")


async def test_a_whole_unterminated_line_is_kept(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["unica"], await _vectors("unica"))
    path = cache.directory / VECTORS_FILENAME
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    assert QueryVectorCache.open(tmp_path, CONTRACT).get("unica") is not None
    assert path.read_bytes().endswith(b"\n")


async def test_windows_line_endings_and_blank_lines_load(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["a", "b"], await _vectors("a", "b"))
    path = cache.directory / VECTORS_FILENAME
    path.write_bytes(b"\r\n" + path.read_bytes().replace(b"\n", b"\r\n"))

    assert len(QueryVectorCache.open(tmp_path, CONTRACT)) == 2


async def test_corruption_and_a_foreign_contract_are_refused(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["a"], await _vectors("a"))
    path = cache.directory / VECTORS_FILENAME
    good = path.read_bytes()

    path.write_bytes(b"not json\n" + good)
    with pytest.raises(ValueError, match=":1 is not a cached query vector"):
        QueryVectorCache.open(tmp_path, CONTRACT)
    short_key = json.dumps({"query_sha256": "abc", "vector": [1.0] + [0.0] * 127})
    path.write_bytes(short_key.encode("utf-8") + b"\n" + good)
    with pytest.raises(ValueError, match=":1 is not a cached query vector"):
        QueryVectorCache.open(tmp_path, CONTRACT)
    with pytest.raises(ValueError, match="different embedding contract"):
        QueryVectorCache(cache.directory, {**CONTRACT, "model_revision": "rev-2"})


async def test_vectors_are_validated(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)

    with pytest.raises(ValueError, match="dimension"):
        cache.add(["curto"], [[1.0]])
    with pytest.raises(ValueError, match="normalized"):
        cache.add(["longo"], [[1.0] * 128])
    stored = json.dumps({"query_sha256": "f" * 64, "vector": [1.0] * 128})
    (cache.directory / VECTORS_FILENAME).write_text(stored + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="normalized"):
        QueryVectorCache.open(tmp_path, CONTRACT)
    free = QueryVectorCache.open(tmp_path / "free", {**CONTRACT, "output_dimension": None})
    free.add(["x"], await _vectors("x"))
    assert len(free) == 1


async def test_the_wrapper_embeds_each_missing_query_once(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))

    first = await embedder.embed_queries(["a", "a", "b"])
    second = await embedder.embed_queries(["b", "a"])

    assert inner.query_batches == [["a", "b"]]
    assert first[0] == first[1] == second[1]
    assert second[0] == first[2]
    assert await embedder.embed_query("a") == first[0]


async def test_a_strict_wrapper_never_calls_the_model(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner, require_cached=True)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))

    with pytest.raises(QueryVectorCacheMiss, match="2 golden query vector"):
        await embedder.embed_queries(["a", "b", "a"])
    assert inner.query_batches == []


async def test_an_unbound_wrapper_passes_through_and_documents_always_do() -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)

    await embedder.embed_queries(["a"])

    assert embedder.cache is None
    assert inner.query_batches == [["a"]]
    assert await embedder.embed(["texto"]) == await inner.embed_documents(["texto"])


async def test_a_pipeline_on_the_wrapper_records_the_real_embedder(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))
    pipeline = RagPipeline(embedder, InMemoryVectorStore())
    await pipeline.index_chunks(
        [
            Chunk(
                chunk_id="d:0",
                doc_id="d",
                text="cobrança indevida na fatura",
                page_start=1,
                page_end=1,
            )
        ]
    )

    _, trace = await pipeline.retrieve_with_trace(
        "cobrança indevida", doc_id="d", agent="test", k=1
    )

    assert trace.embedding_model == inner.model_name
    assert trace.embedding_query_instruction == "Consulta jurídica"
    configuration = pipeline.embedding_contract_configuration()
    assert configuration["query_formatter_version"] == inner.query_format_version
    with pytest.raises(AttributeError):
        _ = embedder.__missing_dunder__
