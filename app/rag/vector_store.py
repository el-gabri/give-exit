"""Vector store port + adapters (Chroma for runtime, in-memory for tests).

The in-memory adapter is not a toy: it proves the port is complete (two
independent implementations) and documents exactly what a Pinecone/Qdrant
adapter would need to provide.
"""

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.schemas.rag import Chunk, RetrievedChunk


@runtime_checkable
class VectorStore(Protocol):
    """Persistence + similarity search over chunks."""

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...

    async def query(self, vector: list[float], doc_id: str, k: int) -> list[RetrievedChunk]: ...

    async def delete_document(self, doc_id: str) -> None: ...


@runtime_checkable
class LexicalVectorStore(Protocol):
    """Optional lexical candidate generator over the same stored chunks."""

    async def lexical_query(self, query: str, doc_id: str, k: int) -> list[RetrievedChunk]: ...


@runtime_checkable
class DocumentReplacingVectorStore(Protocol):
    """Store capable of replacing a document without deleting it first."""

    async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...


@runtime_checkable
class DocumentListingVectorStore(Protocol):
    """Store that can enumerate the documents it holds, enabling orphan cleanup."""

    async def list_document_ids(self) -> set[str]: ...


class InMemoryVectorStore:
    """Reference implementation with exact cosine similarity."""

    def __init__(self, *, index_name: str = "memory") -> None:
        self._rows: dict[str, tuple[Chunk, list[float]]] = {}
        self._index_name = index_name

    @property
    def index_name(self) -> str:
        return self._index_name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._rows[chunk.chunk_id] = (chunk, vector)

    async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        doc_ids = {chunk.doc_id for chunk in chunks}
        if len(doc_ids) != 1:
            raise ValueError("replace_document requires exactly one doc_id")
        [doc_id] = doc_ids
        replacement = {
            chunk.chunk_id: (chunk, vector) for chunk, vector in zip(chunks, vectors, strict=True)
        }
        retained = {
            chunk_id: row for chunk_id, row in self._rows.items() if row[0].doc_id != doc_id
        }
        self._rows = {**retained, **replacement}

    async def query(self, vector: list[float], doc_id: str, k: int) -> list[RetrievedChunk]:
        candidates = [
            (chunk, _cosine(vector, stored))
            for chunk, stored in self._rows.values()
            if chunk.doc_id == doc_id
        ]
        candidates.sort(key=lambda pair: (-pair[1], pair[0].chunk_id))
        return [RetrievedChunk(chunk=chunk, score=score) for chunk, score in candidates[:k]]

    async def lexical_query(self, query: str, doc_id: str, k: int) -> list[RetrievedChunk]:
        chunks = [chunk for chunk, _ in self._rows.values() if chunk.doc_id == doc_id]
        return _bm25_rank(query, chunks, k)

    async def list_document_ids(self) -> set[str]:
        return {chunk.doc_id for chunk, _ in self._rows.values()}

    async def delete_document(self, doc_id: str) -> None:
        self._rows = {cid: row for cid, row in self._rows.items() if row[0].doc_id != doc_id}


class ChromaVectorStore:
    """VectorStore backed by a persistent ChromaDB collection.

    Chroma's client is synchronous; calls are wrapped in ``asyncio.to_thread``
    to keep the async contract honest.
    """

    COLLECTION = "lawsuits"

    def __init__(self, persist_dir: Path, *, collection_name: str | None = None) -> None:
        import chromadb

        self._index_name = collection_name or self.COLLECTION
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=self._index_name,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def index_name(self) -> str:
        return self._index_name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        def _upsert() -> None:
            self._collection.upsert(
                ids=[c.chunk_id for c in chunks],
                embeddings=vectors,  # type: ignore[arg-type]
                documents=[c.text for c in chunks],
                metadatas=[_chunk_metadata(c) for c in chunks],
            )

        await asyncio.to_thread(_upsert)

    async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Upsert first, then remove stale ids so provider failure preserves old data."""

        if not chunks:
            return
        doc_ids = {chunk.doc_id for chunk in chunks}
        if len(doc_ids) != 1:
            raise ValueError("replace_document requires exactly one doc_id")
        [doc_id] = doc_ids

        def _replace() -> None:
            existing = self._collection.get(where={"doc_id": doc_id}, include=[])
            existing_ids = set(existing["ids"])
            self._collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=vectors,  # type: ignore[arg-type]
                documents=[chunk.text for chunk in chunks],
                metadatas=[_chunk_metadata(chunk) for chunk in chunks],
            )
            stale_ids = sorted(existing_ids - {chunk.chunk_id for chunk in chunks})
            if stale_ids:
                self._collection.delete(ids=stale_ids)

        await asyncio.to_thread(_replace)

    async def query(self, vector: list[float], doc_id: str, k: int) -> list[RetrievedChunk]:
        def _query() -> list[RetrievedChunk]:
            result = self._collection.query(
                query_embeddings=[vector],  # type: ignore[arg-type]
                n_results=k,
                where={"doc_id": doc_id},  # per-document isolation
                include=["documents", "metadatas", "distances"],
            )
            documents = result["documents"]
            metadatas = result["metadatas"]
            distances = result["distances"]
            if documents is None or metadatas is None or distances is None:
                raise RuntimeError("Chroma omitted requested retrieval fields")
            retrieved: list[RetrievedChunk] = []
            for chunk_id, text, meta, distance in zip(
                result["ids"][0],
                documents[0],
                metadatas[0],
                distances[0],
                strict=True,
            ):
                chunk = _restore_chunk(chunk_id, text, meta)
                # cosine distance -> similarity
                retrieved.append(RetrievedChunk(chunk=chunk, score=1.0 - distance))
            return retrieved

        return await asyncio.to_thread(_query)

    async def lexical_query(self, query: str, doc_id: str, k: int) -> list[RetrievedChunk]:
        def _query() -> list[RetrievedChunk]:
            result = self._collection.get(
                where={"doc_id": doc_id},
                include=["documents", "metadatas"],
            )
            chunks = [
                _restore_chunk(chunk_id, text, meta)
                for chunk_id, text, meta in zip(
                    result["ids"],
                    result["documents"] or [],
                    result["metadatas"] or [],
                    strict=True,
                )
            ]
            return _bm25_rank(query, chunks, k)

        return await asyncio.to_thread(_query)

    async def list_document_ids(self) -> set[str]:
        def _list() -> set[str]:
            result = self._collection.get(include=["metadatas"])
            return {
                str(metadata["doc_id"])
                for metadata in result["metadatas"] or []
                if metadata and metadata.get("doc_id")
            }

        return await asyncio.to_thread(_list)

    async def delete_document(self, doc_id: str) -> None:
        await asyncio.to_thread(self._collection.delete, where={"doc_id": doc_id})


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (norm_a * norm_b)


def versioned_collection_name(
    corpus_version: str, embedding_model: str, *, prefix: str = "lawsuits"
) -> str:
    """Build a stable Chroma-safe namespace for one incompatible vector space."""
    raw = f"{corpus_version}:{embedding_model}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", raw.casefold()).strip("-")[:40]
    return f"{prefix}-{slug or 'index'}-{digest}"


def _chunk_metadata(chunk: Chunk) -> dict[str, str | int]:
    """Keep queryable fields plus a lossless payload for future provenance."""
    payload = chunk.model_dump(mode="json", exclude={"text"})
    return {
        "doc_id": chunk.doc_id,
        "section": chunk.section or "",
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "_chunk_payload": json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }


def _restore_chunk(chunk_id: str, text: str, metadata: Mapping[str, object]) -> Chunk:
    payload = metadata.get("_chunk_payload")
    if isinstance(payload, str):
        decoded = json.loads(payload)
        if isinstance(decoded, dict):
            decoded["chunk_id"] = chunk_id
            decoded["text"] = text
            return Chunk.model_validate(decoded)
    return Chunk(
        chunk_id=chunk_id,
        doc_id=str(metadata["doc_id"]),
        text=text,
        section=str(metadata.get("section") or "") or None,
        page_start=int(str(metadata["page_start"])),
        page_end=int(str(metadata["page_end"])),
    )


def _bm25_rank(query: str, chunks: list[Chunk], k: int) -> list[RetrievedChunk]:
    """Small deterministic BM25 implementation with no runtime dependency."""
    if k < 1 or not chunks:
        return []
    query_terms = Counter(_lexical_tokens(query))
    if not query_terms:
        return []

    documents = [_lexical_tokens(chunk.text) for chunk in chunks]
    document_frequency = {
        term: sum(term in set(document) for document in documents) for term in query_terms
    }
    average_length = sum(len(document) for document in documents) / len(documents)
    k1 = 1.5
    b = 0.75
    ranked: list[RetrievedChunk] = []
    for chunk, document in zip(chunks, documents, strict=True):
        frequencies = Counter(document)
        score = 0.0
        for term, query_frequency in query_terms.items():
            frequency = frequencies[term]
            if frequency == 0:
                continue
            doc_frequency = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (len(documents) - doc_frequency + 0.5) / (doc_frequency + 0.5)
            )
            length_normalization = frequency + k1 * (
                1 - b + b * len(document) / (average_length or 1.0)
            )
            score += (
                query_frequency
                * inverse_document_frequency
                * frequency
                * (k1 + 1)
                / length_normalization
            )
        if score > 0:
            ranked.append(RetrievedChunk(chunk=chunk, score=score))
    ranked.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
    return ranked[:k]


def _lexical_tokens(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]+", without_accents)
