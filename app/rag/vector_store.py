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
import threading
import unicodedata
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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

    @property
    def index_name(self) -> str:
        return self._index_name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        def _upsert() -> None:
            self._collection.upsert(
                ids=[c.chunk_id for c in chunks],
                embeddings=vectors,
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
                embeddings=vectors,
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

    async def export_entries(self) -> list[tuple[Chunk, list[float]]]:
        """Return stored chunks and vectors for an explicit backend migration.

        This method is intentionally not part of ``VectorStore``: ordinary
        request handling never needs to read raw embeddings from persistence.
        """

        def _export() -> list[tuple[Chunk, list[float]]]:
            result = self._collection.get(
                include=["documents", "metadatas", "embeddings"],
            )
            documents = result["documents"]
            metadatas = result["metadatas"]
            embeddings = result["embeddings"]
            if documents is None or metadatas is None or embeddings is None:
                raise RuntimeError("Chroma omitted entries required for migration")
            return [
                (
                    _restore_chunk(chunk_id, text, metadata),
                    [float(value) for value in embedding],
                )
                for chunk_id, text, metadata, embedding in zip(
                    result["ids"], documents, metadatas, embeddings, strict=True
                )
            ]

        return await asyncio.to_thread(_export)

    async def export_document(self, doc_id: str) -> list[tuple[Chunk, list[float]]]:
        """Export one isolated document with its stored vectors."""

        def _export() -> list[tuple[Chunk, list[float]]]:
            result = self._collection.get(
                where={"doc_id": doc_id},
                include=["documents", "metadatas", "embeddings"],
            )
            documents = result["documents"]
            metadatas = result["metadatas"]
            embeddings = result["embeddings"]
            if documents is None or metadatas is None or embeddings is None:
                raise RuntimeError("Chroma omitted entries required for document export")
            entries = [
                (
                    _restore_chunk(chunk_id, text, metadata),
                    [float(value) for value in embedding],
                )
                for chunk_id, text, metadata, embedding in zip(
                    result["ids"], documents, metadatas, embeddings, strict=True
                )
            ]
            return sorted(entries, key=lambda item: item[0].chunk_id)

        return await asyncio.to_thread(_export)


class PostgresVectorStore:
    """pgvector-backed persistence with exact cosine retrieval.

    The table stores every provenance field in a JSONB payload and scopes rows
    by the versioned index namespace. It deliberately uses an unconstrained
    ``vector`` column: a namespace is immutable for one embedding space, while
    different namespaces can safely have different dimensions in one table.
    The Consumer corpus is small (460 chunks), so exact search is both fast
    and avoids an index that could silently mix incompatible dimensions.
    """

    TABLE = "give_exit_vector_chunks"

    def __init__(self, *, dsn: str, index_name: str) -> None:
        if not dsn.strip():
            raise ValueError("Postgres DSN must be non-empty")
        self._dsn = dsn
        self._index_name = index_name
        self._schema_ready = False
        self._schema_lock = threading.Lock()

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
        doc_ids = {chunk.doc_id for chunk in chunks}
        if len(doc_ids) != 1:
            raise ValueError("replace_document requires exactly one doc_id")
        await asyncio.to_thread(self._replace_document_sync, next(iter(doc_ids)), rows)

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

    def _connect(self) -> Any:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "Postgres vector storage requires `pip install -e \".[postgres]\"`"
            ) from exc
        return psycopg.connect(self._dsn)

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
            self._schema_ready = True

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
            return [
                RetrievedChunk(
                    chunk=_restore_postgres_chunk(chunk_id, content, payload), score=float(score)
                )
                for chunk_id, content, payload, score in cursor.fetchall()
            ]

    def _lexical_query_sync(self, query: str, doc_id: str, k: int) -> list[RetrievedChunk]:
        self._ensure_schema_sync()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH parsed_query AS (
                    SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery AS value
                    FROM unnest(
                        tsvector_to_array(to_tsvector('portuguese'::regconfig, %s))
                    ) AS lexeme
                )
                SELECT chunk_id, content, chunk_payload,
                       ts_rank_cd(search_vector, parsed_query.value) AS score
                FROM {self.TABLE}, parsed_query
                WHERE namespace = %s
                  AND doc_id = %s
                  AND search_vector @@ parsed_query.value
                ORDER BY score DESC, chunk_id ASC
                LIMIT %s
                """,
                (query, self._index_name, doc_id, k),
            )
            return [
                RetrievedChunk(
                    chunk=_restore_postgres_chunk(chunk_id, content, payload),
                    score=float(score),
                )
                for chunk_id, content, payload, score in cursor.fetchall()
            ]

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
    namespace, chunk_id, doc_id, content, section, page_start, page_end, chunk_payload, embedding
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector)
ON CONFLICT (namespace, chunk_id) DO UPDATE SET
    doc_id = EXCLUDED.doc_id,
    content = EXCLUDED.content,
    section = EXCLUDED.section,
    page_start = EXCLUDED.page_start,
    page_end = EXCLUDED.page_end,
    chunk_payload = EXCLUDED.chunk_payload,
    embedding = EXCLUDED.embedding
"""


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
    tokens = re.findall(r"[a-z0-9]+", _normalize_lexical_text(text))
    return [token for token in tokens if token not in _PORTUGUESE_STOPWORDS]
