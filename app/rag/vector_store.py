"""Vector store port + adapters (Chroma for runtime, in-memory for tests).

The in-memory adapter is not a toy: it proves the port is complete (two
independent implementations) and documents exactly what a Pinecone/Qdrant
adapter would need to provide.
"""

import asyncio
import json
import math
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.hashing import sha256_hex
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


@runtime_checkable
class DocumentExportingVectorStore(Protocol):
    """Store that can export one document for validation or controlled migration."""

    async def export_document(self, doc_id: str) -> list[tuple[Chunk, list[float]]]: ...


@runtime_checkable
class ClosableVectorStore(Protocol):
    """Store holding connections that its owner releases before exiting."""

    def close(self) -> None: ...


class InMemoryVectorStore:
    """Reference implementation with exact cosine similarity."""

    def __init__(self, *, index_name: str = "memory") -> None:
        self._rows: dict[str, tuple[Chunk, list[float]]] = {}
        self._index_name = index_name
        self._lexical_tokens = _LexicalTokenCache()

    @property
    def index_name(self) -> str:
        return self._index_name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._rows[chunk.chunk_id] = (chunk, vector)
        self._lexical_tokens.forget({chunk.doc_id for chunk in chunks})

    async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        doc_id = _single_doc_id(chunks)
        replacement = {
            chunk.chunk_id: (chunk, vector) for chunk, vector in zip(chunks, vectors, strict=True)
        }
        retained = {
            chunk_id: row for chunk_id, row in self._rows.items() if row[0].doc_id != doc_id
        }
        self._rows = {**retained, **replacement}
        self._lexical_tokens.forget({doc_id})

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
        tokenizer = self._lexical_tokens.tokenizer(doc_id)
        ranked = _bm25_rank(query, chunks, k, tokenize=tokenizer)
        self._lexical_tokens.remember(doc_id, tokenizer)
        return ranked

    async def list_document_ids(self) -> set[str]:
        return {chunk.doc_id for chunk, _ in self._rows.values()}

    async def export_document(self, doc_id: str) -> list[tuple[Chunk, list[float]]]:
        return sorted(
            (
                (chunk, list(vector))
                for chunk, vector in self._rows.values()
                if chunk.doc_id == doc_id
            ),
            key=lambda item: item[0].chunk_id,
        )

    async def delete_document(self, doc_id: str) -> None:
        self._rows = {cid: row for cid, row in self._rows.items() if row[0].doc_id != doc_id}
        self._lexical_tokens.forget({doc_id})


class ChromaVectorStore:
    """VectorStore backed by a persistent ChromaDB collection.

    Chroma's client is synchronous; calls are wrapped in ``asyncio.to_thread``
    to keep the async contract honest. This adapter intentionally supports the
    embedded ``PersistentClient`` only. It never connects to a Chroma server
    and never accepts a collection-provided embedding function.
    """

    COLLECTION = "consumer-documents"

    def __init__(self, persist_dir: Path, *, collection_name: str | None = None) -> None:
        import chromadb

        self._index_name = collection_name or self.COLLECTION
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=self._index_name,
            metadata={"hnsw:space": "cosine"},
            # Embeddings are computed by our configured adapter and supplied
            # explicitly on every operation. Do not deserialize or execute an
            # embedding function stored in collection configuration.
            embedding_function=None,
        )
        self._lexical_tokens = _LexicalTokenCache()

    @property
    def index_name(self) -> str:
        return self._index_name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        await asyncio.to_thread(self._upsert_sync, chunks, vectors)
        self._lexical_tokens.forget({chunk.doc_id for chunk in chunks})

    async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Upsert first, then remove stale ids so provider failure preserves old data."""

        if not chunks:
            return
        doc_id = _single_doc_id(chunks)

        def _replace() -> None:
            existing = self._collection.get(where={"doc_id": doc_id}, include=[])
            existing_ids = set(existing["ids"])
            self._upsert_sync(chunks, vectors)
            stale_ids = sorted(existing_ids - {chunk.chunk_id for chunk in chunks})
            if stale_ids:
                self._collection.delete(ids=stale_ids)

        await asyncio.to_thread(_replace)
        self._lexical_tokens.forget({doc_id})

    def _upsert_sync(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        self._collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            embeddings=vectors,
            documents=[chunk.text for chunk in chunks],
            metadatas=[_chunk_metadata(chunk) for chunk in chunks],
        )

    async def query(self, vector: list[float], doc_id: str, k: int) -> list[RetrievedChunk]:
        def _query() -> list[RetrievedChunk]:
            result = self._collection.query(
                query_embeddings=[vector],
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
        tokenizer = self._lexical_tokens.tokenizer(doc_id)

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
            return _bm25_rank(query, chunks, k, tokenize=tokenizer)

        ranked = await asyncio.to_thread(_query)
        self._lexical_tokens.remember(doc_id, tokenizer)
        return ranked

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
        self._lexical_tokens.forget({doc_id})

    async def export_entries(self) -> list[tuple[Chunk, list[float]]]:
        """Return stored chunks and vectors for an explicit backend migration.

        This method is intentionally not part of ``VectorStore``: ordinary
        request handling never needs to read raw embeddings from persistence.
        """

        return await asyncio.to_thread(self._stored_entries, None, "migration")

    async def export_document(self, doc_id: str) -> list[tuple[Chunk, list[float]]]:
        """Export one isolated document with its stored vectors."""

        entries = await asyncio.to_thread(self._stored_entries, doc_id, "document export")
        return sorted(entries, key=lambda item: item[0].chunk_id)

    def _stored_entries(
        self, doc_id: str | None, purpose: str
    ) -> list[tuple[Chunk, list[float]]]:
        """Stored chunks and vectors of one document, or of the whole collection."""
        result = self._collection.get(
            where={"doc_id": doc_id} if doc_id is not None else None,
            include=["documents", "metadatas", "embeddings"],
        )
        documents = result["documents"]
        metadatas = result["metadatas"]
        embeddings = result["embeddings"]
        if documents is None or metadatas is None or embeddings is None:
            raise RuntimeError(f"Chroma omitted entries required for {purpose}")
        return [
            (
                _restore_chunk(chunk_id, text, metadata),
                [float(value) for value in embedding],
            )
            for chunk_id, text, metadata, embedding in zip(
                result["ids"], documents, metadatas, embeddings, strict=True
            )
        ]


class PostgresVectorStore:
    """pgvector-backed persistence with exact cosine retrieval.

    The table stores every provenance field in a JSONB payload and scopes rows
    by the versioned index namespace. It deliberately uses an unconstrained
    ``vector`` column: a namespace is immutable for one embedding space, while
    different namespaces can safely have different dimensions in one table.
    The Consumer corpus is small (a few thousand chunks), so exact search is
    both fast and avoids an index that could silently mix incompatible
    dimensions.

    Lexical search is the same BM25 the in-process adapters run, over the same
    tokens, which are computed in Python at write time and stored per row. It
    used to rank with ``ts_rank_cd`` over a Portuguese ``tsvector`` - stemmed,
    without inverse document frequency - and on the golden queries its top 8
    shared about a fifth of its chunks with the BM25 top 8 that the offline
    evaluation measures, so the evaluated lexical channel was not the shipped
    one.

    Connections come from a small pool when ``psycopg_pool`` is installed;
    opening one per query cost as much as the query itself.
    """

    TABLE = "give_exit_vector_chunks"

    def __init__(self, *, dsn: str, index_name: str, pool_max_size: int = 8) -> None:
        if not dsn.strip():
            raise ValueError("Postgres DSN must be non-empty")
        if pool_max_size < 1:
            raise ValueError("pool_max_size must be positive")
        self._dsn = dsn
        self._index_name = index_name
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._pool_max_size = pool_max_size
        self._pool: Any | None = None
        self._pool_lock = threading.Lock()

    @property
    def index_name(self) -> str:
        return self._index_name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        rows = _postgres_rows(chunks, vectors, namespace=self._index_name)
        if not rows:
            return
        await asyncio.to_thread(self._upsert_sync, rows)

    async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        rows = _postgres_rows(chunks, vectors, namespace=self._index_name)
        if not rows:
            return
        await asyncio.to_thread(self._replace_document_sync, _single_doc_id(chunks), rows)

    async def query(self, vector: list[float], doc_id: str, k: int) -> list[RetrievedChunk]:
        if k < 1:
            return []
        return await asyncio.to_thread(self._query_sync, _vector_literal(vector), doc_id, k)

    async def lexical_query(self, query: str, doc_id: str, k: int) -> list[RetrievedChunk]:
        if k < 1:
            return []
        return await asyncio.to_thread(self._lexical_query_sync, query, doc_id, k)

    async def list_document_ids(self) -> set[str]:
        return await asyncio.to_thread(self._list_document_ids_sync)

    async def export_document(self, doc_id: str) -> list[tuple[Chunk, list[float]]]:
        return await asyncio.to_thread(self._export_document_sync, doc_id)

    async def delete_document(self, doc_id: str) -> None:
        await asyncio.to_thread(self._delete_document_sync, doc_id)

    def close(self) -> None:
        """Close pooled connections; the store reopens a pool on next use."""
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    def _connect(self) -> Any:
        """A context manager yielding a connection that commits on success."""
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "Postgres vector storage requires `pip install -e \".[postgres]\"`"
            ) from exc
        pool = self._connection_pool(psycopg)
        return pool.connection() if pool is not None else psycopg.connect(self._dsn)

    def _connection_pool(self, psycopg: Any) -> Any | None:
        if self._pool is not None:
            return self._pool
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:  # pragma: no cover - pool is part of the postgres extra
            return None
        with self._pool_lock:
            if self._pool is None:
                # Connect once directly before pooling. A pool retries in the
                # background, so an unreachable server or rejected credentials
                # would surface only as "PoolTimeout: couldn't get a connection
                # after 30.00 sec" instead of the server's own error, at once.
                psycopg.connect(self._dsn).close()
                self._pool = ConnectionPool(
                    self._dsn, min_size=1, max_size=self._pool_max_size, open=True
                )
            return self._pool

    def _ensure_schema_sync(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                if cursor.fetchone() is None:
                    raise RuntimeError(
                        "pgvector is not enabled in this database; run `CREATE EXTENSION vector;`"
                    )
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE} (
                        namespace TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        doc_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        section TEXT,
                        page_start INTEGER NOT NULL,
                        page_end INTEGER NOT NULL,
                        chunk_payload JSONB NOT NULL,
                        embedding vector NOT NULL,
                        PRIMARY KEY (namespace, chunk_id)
                    )
                    """
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self.TABLE}_namespace_doc_id_idx
                    ON {self.TABLE} (namespace, doc_id)
                    """
                )
                cursor.execute(
                    f"""
                    ALTER TABLE {self.TABLE}
                    ADD COLUMN IF NOT EXISTS search_vector tsvector
                    GENERATED ALWAYS AS (
                        to_tsvector('portuguese'::regconfig, coalesce(content, ''))
                    ) STORED
                    """
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self.TABLE}_search_vector_idx
                    ON {self.TABLE} USING GIN (search_vector)
                    """
                )
                cursor.execute(
                    f"ALTER TABLE {self.TABLE} ADD COLUMN IF NOT EXISTS lexical_tokens TEXT[]"
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self.TABLE}_lexical_tokens_idx
                    ON {self.TABLE} USING GIN (lexical_tokens)
                    """
                )
                self._backfill_lexical_tokens(cursor)
            self._schema_ready = True

    def _backfill_lexical_tokens(self, cursor: Any) -> None:
        """Tokenize rows written before lexical tokens were stored."""
        cursor.execute(
            f"SELECT namespace, chunk_id, content FROM {self.TABLE} WHERE lexical_tokens IS NULL"
        )
        rows = cursor.fetchall()
        if rows:
            cursor.executemany(
                f"UPDATE {self.TABLE} SET lexical_tokens = %s "
                "WHERE namespace = %s AND chunk_id = %s",
                [
                    (portuguese_lexical_tokens(str(content)), namespace, chunk_id)
                    for namespace, chunk_id, content in rows
                ],
            )

    def _upsert_sync(self, rows: list[tuple[object, ...]]) -> None:
        self._ensure_schema_sync()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(_POSTGRES_UPSERT_SQL, rows)

    def _replace_document_sync(self, doc_id: str, rows: list[tuple[object, ...]]) -> None:
        self._ensure_schema_sync()
        chunk_ids = [str(row[1]) for row in rows]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(_POSTGRES_UPSERT_SQL, rows)
            cursor.execute(
                f"""
                DELETE FROM {self.TABLE}
                WHERE namespace = %s AND doc_id = %s AND NOT (chunk_id = ANY(%s))
                """,
                (self._index_name, doc_id, chunk_ids),
            )

    def _query_sync(self, vector: str, doc_id: str, k: int) -> list[RetrievedChunk]:
        self._ensure_schema_sync()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT chunk_id, content, chunk_payload,
                       1 - (embedding <=> %s::vector) AS score
                FROM {self.TABLE}
                WHERE namespace = %s AND doc_id = %s
                ORDER BY embedding <=> %s::vector, chunk_id ASC
                LIMIT %s
                """,
                (vector, self._index_name, doc_id, vector, k),
            )
            return self._retrieved(cursor.fetchall())

    @staticmethod
    def _retrieved(rows: list[tuple[Any, ...]]) -> list[RetrievedChunk]:
        return [
            RetrievedChunk(
                chunk=_restore_postgres_chunk(chunk_id, content, payload),
                score=float(score),
            )
            for chunk_id, content, payload, score in rows
        ]

    def _lexical_query_sync(self, query: str, doc_id: str, k: int) -> list[RetrievedChunk]:
        query_terms = Counter(portuguese_lexical_tokens(query))
        if not query_terms:
            return []
        self._ensure_schema_sync()
        terms = list(query_terms)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                _POSTGRES_BM25_SQL,
                (
                    self._index_name,
                    doc_id,
                    terms,
                    [query_terms[term] for term in terms],
                    self._index_name,
                    doc_id,
                    terms,
                    terms,
                    _BM25_K1,
                    _BM25_K1,
                    _BM25_B,
                    _BM25_B,
                    k,
                ),
            )
            return self._retrieved(cursor.fetchall())

    def _export_document_sync(self, doc_id: str) -> list[tuple[Chunk, list[float]]]:
        self._ensure_schema_sync()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT chunk_id, content, chunk_payload, embedding::text
                FROM {self.TABLE}
                WHERE namespace = %s AND doc_id = %s
                ORDER BY chunk_id ASC
                """,
                (self._index_name, doc_id),
            )
            return [
                (
                    _restore_postgres_chunk(chunk_id, content, payload),
                    _parse_vector_text(vector_text),
                )
                for chunk_id, content, payload, vector_text in cursor.fetchall()
            ]

    def _list_document_ids_sync(self) -> set[str]:
        self._ensure_schema_sync()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT doc_id FROM {self.TABLE} WHERE namespace = %s",
                (self._index_name,),
            )
            return {str(row[0]) for row in cursor.fetchall()}

    def _delete_document_sync(self, doc_id: str) -> None:
        self._ensure_schema_sync()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self.TABLE} WHERE namespace = %s AND doc_id = %s",
                (self._index_name, doc_id),
            )


_POSTGRES_UPSERT_SQL = f"""
INSERT INTO {PostgresVectorStore.TABLE} (
    namespace, chunk_id, doc_id, content, section, page_start, page_end, chunk_payload,
    embedding, lexical_tokens
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector, %s)
ON CONFLICT (namespace, chunk_id) DO UPDATE SET
    doc_id = EXCLUDED.doc_id,
    content = EXCLUDED.content,
    section = EXCLUDED.section,
    page_start = EXCLUDED.page_start,
    page_end = EXCLUDED.page_end,
    chunk_payload = EXCLUDED.chunk_payload,
    embedding = EXCLUDED.embedding,
    lexical_tokens = EXCLUDED.lexical_tokens
"""

_BM25_K1 = 1.5
_BM25_B = 0.75

# BM25 exactly as ``_bm25_rank`` computes it: the corpus statistics cover every
# chunk of the document, term frequencies come from the stored token arrays,
# and ties break on chunk id.
_POSTGRES_BM25_SQL = f"""
WITH corpus AS (
    SELECT count(*)::float8 AS documents,
           avg(cardinality(lexical_tokens))::float8 AS average_length
    FROM {PostgresVectorStore.TABLE}
    WHERE namespace = %s AND doc_id = %s
),
query_terms AS (
    SELECT term, query_frequency
    FROM unnest(%s::text[], %s::int[]) AS query(term, query_frequency)
),
candidates AS (
    SELECT chunk_id, content, chunk_payload, lexical_tokens,
           cardinality(lexical_tokens)::float8 AS length
    FROM {PostgresVectorStore.TABLE}
    WHERE namespace = %s AND doc_id = %s AND lexical_tokens && %s::text[]
),
term_frequencies AS (
    SELECT candidates.chunk_id, token AS term, count(*)::float8 AS frequency
    FROM candidates, unnest(candidates.lexical_tokens) AS token
    WHERE token = ANY(%s::text[])
    GROUP BY candidates.chunk_id, token
),
document_frequencies AS (
    SELECT term, count(*)::float8 AS frequency
    FROM term_frequencies
    GROUP BY term
),
scores AS (
    SELECT term_frequencies.chunk_id,
           sum(
               query_terms.query_frequency
               * ln(1 + (corpus.documents - document_frequencies.frequency + 0.5)
                        / (document_frequencies.frequency + 0.5))
               * term_frequencies.frequency * (%s + 1)
               / (term_frequencies.frequency
                  + %s * (1 - %s + %s * candidates.length
                          / coalesce(nullif(corpus.average_length, 0), 1)))
           ) AS score
    FROM term_frequencies
    JOIN query_terms USING (term)
    JOIN document_frequencies USING (term)
    JOIN candidates USING (chunk_id)
    CROSS JOIN corpus
    GROUP BY term_frequencies.chunk_id
)
SELECT candidates.chunk_id, candidates.content, candidates.chunk_payload, scores.score
FROM scores JOIN candidates USING (chunk_id)
WHERE scores.score > 0
ORDER BY scores.score DESC, candidates.chunk_id ASC
LIMIT %s
"""


def _single_doc_id(chunks: list[Chunk]) -> str:
    doc_ids = {chunk.doc_id for chunk in chunks}
    if len(doc_ids) != 1:
        raise ValueError("replace_document requires exactly one doc_id")
    [doc_id] = doc_ids
    return doc_id


def _postgres_rows(
    chunks: list[Chunk], vectors: list[list[float]], *, namespace: str
) -> list[tuple[object, ...]]:
    if len(chunks) != len(vectors):
        raise ValueError("one vector is required for every chunk")
    return [
        (
            namespace,
            chunk.chunk_id,
            chunk.doc_id,
            chunk.text,
            chunk.section,
            chunk.page_start,
            chunk.page_end,
            json.dumps(
                chunk.model_dump(mode="json", exclude={"text"}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            _vector_literal(vector),
            portuguese_lexical_tokens(chunk.text),
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def _vector_literal(vector: list[float]) -> str:
    if not vector:
        raise ValueError("embedding vectors must not be empty")
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding vectors must contain only finite values")
    return "[" + ",".join(repr(value) for value in values) + "]"


def _parse_vector_text(value: object) -> list[float]:
    """Parse pgvector's stable text representation without a global adapter."""

    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("PostgreSQL returned an invalid vector representation") from exc
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("PostgreSQL returned an empty vector representation")
    vector = [float(item) for item in parsed]
    if not all(math.isfinite(item) for item in vector):
        raise RuntimeError("PostgreSQL returned non-finite vector values")
    return vector


def _restore_postgres_chunk(chunk_id: object, text: object, payload: object) -> Chunk:
    if not isinstance(payload, dict):
        raise RuntimeError("Postgres returned an invalid chunk payload")
    restored = dict(payload)
    restored["chunk_id"] = str(chunk_id)
    restored["text"] = str(text)
    return Chunk.model_validate(restored)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (norm_a * norm_b)


def versioned_collection_name(
    corpus_version: str, embedding_model: str, *, prefix: str = "give-exit-consumer"
) -> str:
    """Build a stable Chroma-safe namespace for one incompatible vector space."""
    raw = f"{corpus_version}:{embedding_model}"
    digest = sha256_hex(raw)[:12]
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


def _normalize_lexical_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


# Mirrors PostgreSQL's Snowball Portuguese stopword dictionary. Keeping these
# terms out of the dependency-free BM25 adapter prevents a function-word-only
# overlap from being interpreted as an independent lexical retrieval signal.
_PORTUGUESE_STOPWORDS = frozenset(
    re.findall(
        r"[a-z0-9]+",
        _normalize_lexical_text(
            """
            de a o que e do da em um para com não uma os no se na por mais as dos
            como mas ao ele das à seu sua ou quando muito nos já eu também só pelo
            pela até isso ela entre depois sem mesmo aos seus quem nas me esse eles
            você essa num nem suas meu às minha numa pelos elas qual nós lhe deles
            essas esses pelas este dele tu te vocês vos lhes meus minhas teu tua teus
            tuas nosso nossa nossos nossas dela delas esta estes estas aquele aquela
            aqueles aquelas isto aquilo estou está estamos estão estive esteve
            estivemos estiveram estava estávamos estavam estivera estivéramos esteja
            estejamos estejam estivesse estivéssemos estivessem estiver estivermos
            estiverem hei há havemos hão houve houvemos houveram houvera houvéramos
            haja hajamos hajam houvesse houvéssemos houvessem houver houvermos
            houverem houverei haverá houveremos houverão houveria houveríamos
            houveriam sou somos são era éramos eram fui foi fomos foram fora fôramos
            seja sejamos sejam fosse fôssemos fossem for formos forem serei será
            seremos serão seria seríamos seriam tenho tem temos tém tinha tínhamos
            tinham tive teve tivemos tiveram tivera tivéramos tenha tenhamos tenham
            tivesse tivéssemos tivessem tiver tivermos tiverem terei terá teremos
            terão teria teríamos teriam
            """
        ),
    )
)


class _SearchTokenizer:
    """Tokenizes the chunks of one lexical search, reusing cached tokens."""

    def __init__(self, cached: Mapping[str, tuple[str, ...]], writes: int) -> None:
        self._cached = cached
        self.writes = writes
        self.used: dict[str, tuple[str, ...]] = {}

    def __call__(self, chunk: Chunk) -> tuple[str, ...]:
        tokens = self._cached.get(chunk.text)
        if tokens is None:
            tokens = tuple(portuguese_lexical_tokens(chunk.text))
        self.used[chunk.text] = tokens
        return tokens


class _LexicalTokenCache:
    """Chunk tokens one store keeps between lexical searches, per document.

    Tokenizing every chunk dominated each lexical query. Tokens are keyed by
    chunk text, so they cannot go stale. Every write through the store drops
    the documents it wrote, so text a case deletion removed from the index
    does not outlive it here, and a search that raced any write does not
    store its tokens.
    """

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, tuple[str, ...]]] = {}
        self._writes = 0

    def tokenizer(self, doc_id: str) -> _SearchTokenizer:
        return _SearchTokenizer(self._documents.get(doc_id, {}), self._writes)

    def remember(self, doc_id: str, tokenizer: _SearchTokenizer) -> None:
        if tokenizer.used and tokenizer.writes == self._writes:
            self._documents[doc_id] = tokenizer.used

    def forget(self, doc_ids: Iterable[str]) -> None:
        self._writes += 1
        for doc_id in doc_ids:
            self._documents.pop(doc_id, None)


def _bm25_rank(
    query: str,
    chunks: list[Chunk],
    k: int,
    *,
    tokenize: Callable[[Chunk], Sequence[str]],
) -> list[RetrievedChunk]:
    """Small deterministic BM25 implementation with no runtime dependency."""
    if k < 1 or not chunks:
        return []
    query_terms = Counter(portuguese_lexical_tokens(query))
    if not query_terms:
        return []

    documents = [tokenize(chunk) for chunk in chunks]
    vocabularies = [set(document) for document in documents]
    document_frequency = {
        term: sum(term in vocabulary for vocabulary in vocabularies) for term in query_terms
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


def portuguese_lexical_tokens(text: str) -> list[str]:
    """Normalize text with the same stopword policy as local BM25 retrieval."""

    tokens = re.findall(r"[a-z0-9]+", _normalize_lexical_text(text))
    return [token for token in tokens if token not in _PORTUGUESE_STOPWORDS]
