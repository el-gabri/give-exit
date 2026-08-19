"""RAG facade used by the rest of the application.

Two operations: index a parsed document, retrieve context for a question.
Everything else (chunking policy, embedding provider, store backend) is
injected - agents never know which vector database is running.
"""

import asyncio
import hashlib
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping
from typing import cast

from app.core.config import RetrievalMode
from app.core.logging import get_logger
from app.rag.chunking import SectionAwareChunker
from app.rag.embeddings import (
    EmbeddingBackend,
    embed_document_texts,
    embed_query_texts,
)
from app.rag.reranking import Reranker
from app.rag.vector_store import (
    DocumentListingVectorStore,
    DocumentReplacingVectorStore,
    LexicalVectorStore,
    VectorStore,
)
from app.schemas.document import ParsedDocument
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.security import PromptInjectionAssessment
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace
from app.security.sanitization import SANITIZER_VERSION, sanitized_document

logger = get_logger(__name__)

AUDIT_PREVIEW_CHARS = 240


class RetrievalBatchError(RuntimeError):
    """A failed retrieval batch whose query-level audit remains available."""

    def __init__(self, cause: Exception, traces: list[RetrievalTrace]) -> None:
        self.cause = cause
        self.traces = traces
        failed = sum(trace.error is not None for trace in traces)
        super().__init__(
            f"{failed}/{len(traces)} retrieval queries failed; first error: {_error_text(cause)}"
        )


class RagPipeline:
    """Index and retrieve within a single document's boundary."""

    def __init__(
        self,
        embedder: EmbeddingBackend,
        store: VectorStore,
        chunker: SectionAwareChunker | None = None,
        default_k: int = 6,
        include_trace_previews: bool = False,
        retrieval_mode: RetrievalMode | str = RetrievalMode.DENSE,
        candidate_multiplier: int = 4,
        rrf_constant: int = 60,
        dense_weight: float = 1.0,
        lexical_weight: float = 1.0,
        reranker: Reranker | None = None,
        corpus_version: str = "documents-v1",
    ) -> None:
        if default_k < 1:
            raise ValueError("default_k must be positive")
        if candidate_multiplier < 1:
            raise ValueError("candidate_multiplier must be positive")
        if rrf_constant < 1:
            raise ValueError("rrf_constant must be positive")
        if dense_weight <= 0 or lexical_weight <= 0:
            raise ValueError("retrieval weights must be positive")
        self._embedder = embedder
        self._store = store
        self._chunker = chunker or SectionAwareChunker()
        self._default_k = default_k
        self._retrieval_mode = RetrievalMode(retrieval_mode)
        self._candidate_multiplier = candidate_multiplier
        self._rrf_constant = rrf_constant
        self._dense_weight = dense_weight
        self._lexical_weight = lexical_weight
        self._reranker = reranker
        self._embedding_model = str(getattr(embedder, "model_name", type(embedder).__name__))
        self._embedding_model_revision = _component_revision(embedder)
        query_instruction = getattr(embedder, "_query_instruction", None)
        self._embedding_query_instruction = (
            str(query_instruction).strip() if query_instruction else None
        )
        self._vector_store_name = type(store).__name__
        self._index_name = str(getattr(store, "index_name", type(store).__name__))
        self._reranker_name = (
            str(getattr(reranker, "model_name", type(reranker).__name__))
            if reranker is not None
            else None
        )
        self._reranker_model_revision = _component_revision(reranker)
        self._corpus_version = corpus_version
        self._base_index_version = (
            f"{SANITIZER_VERSION}:"
            f"corpus={corpus_version}:embedding={self._embedding_model}:"
            f"index={self._index_name}:reranker={self._reranker_name or 'none'}"
        )
        self._index_version = f"{self._chunker.index_version}:{self._base_index_version}"
        self._document_chunking_versions: dict[str, str] = {}
        self._include_trace_previews = include_trace_previews

    def retrieval_configuration(
        self,
        *,
        requested_k: int,
        mode: RetrievalMode | str | None = None,
        doc_id: str | None = None,
    ) -> dict[str, str | int | float | None]:
        """Return the effective, serialization-safe retrieval configuration."""
        if requested_k < 1:
            raise ValueError("requested_k must be positive")
        effective_mode = self._retrieval_mode if mode is None else RetrievalMode(mode)
        chunking_version = self._document_chunking_versions.get(
            doc_id or "", self._chunker.index_version
        )
        candidate_k = self._candidate_k(requested_k, effective_mode)
        instruction_hash = (
            hashlib.sha256(self._embedding_query_instruction.encode("utf-8")).hexdigest()
            if self._embedding_query_instruction is not None
            else None
        )
        return {
            "retrieval_mode": effective_mode.value,
            "requested_k": requested_k,
            "candidate_k": candidate_k,
            "candidate_multiplier": self._candidate_multiplier,
            "embedding_model": self._embedding_model,
            "embedding_model_revision": self._embedding_model_revision,
            "embedding_query_instruction": self._embedding_query_instruction,
            "embedding_query_instruction_sha256": instruction_hash,
            "vector_store": self._vector_store_name,
            "index_version": f"{chunking_version}:{self._base_index_version}",
            "rrf_constant": (
                self._rrf_constant if effective_mode is RetrievalMode.HYBRID else None
            ),
            "dense_weight": (
                self._dense_weight if effective_mode is RetrievalMode.HYBRID else None
            ),
            "lexical_weight": (
                self._lexical_weight if effective_mode is RetrievalMode.HYBRID else None
            ),
            "reranker_model": self._reranker_name,
            "reranker_model_revision": self._reranker_model_revision,
            "corpus_version": self._corpus_version,
            "index_name": self._index_name,
            "chunking_version": chunking_version,
        }

    def _candidate_k(self, requested_k: int, mode: RetrievalMode) -> int:
        if mode is RetrievalMode.HYBRID or self._reranker is not None:
            return requested_k * self._candidate_multiplier
        return requested_k

    async def index_document(
        self,
        document: ParsedDocument,
        security_assessment: PromptInjectionAssessment | None = None,
    ) -> list[Chunk]:
        """Chunk, embed and store a document. Idempotent per doc_id."""
        safe_document = sanitized_document(document, security_assessment)
        chunks = self._chunker.chunk(safe_document, doc_id=document.doc_id)
        if not chunks:
            await self._store.delete_document(document.doc_id)
            logger.warning("index_empty_document", doc_id=document.doc_id)
            return []
        await self.index_chunks(chunks)
        logger.info(
            "document_indexed",
            doc_id=document.doc_id,
            chunks=len(chunks),
            sections=len({c.section for c in chunks}),
        )
        return chunks

    async def index_chunks(
        self, chunks: list[Chunk], *, replace_documents: bool = True
    ) -> list[Chunk]:
        """Embed and index already structured chunks without re-chunking them.

        This is the ingestion entry point for versioned legal corpora. It
        preserves every ``Chunk.metadata`` field and replaces all affected
        document namespaces only after embeddings have succeeded.
        """
        if not chunks:
            return []
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("chunk ids must be unique within an indexing batch")

        vectors = await embed_document_texts(self._embedder, [chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(
                f"embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks"
            )
        document_chunks: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            document_chunks[chunk.doc_id].append(chunk)
        chunking_versions: dict[str, str] = {}
        for doc_id, selected_chunks in document_chunks.items():
            declared_versions = {
                str(chunk.metadata.get("chunking_version"))
                for chunk in selected_chunks
                if chunk.metadata.get("chunking_version")
            }
            chunking_versions[doc_id] = (
                next(iter(declared_versions))
                if len(declared_versions) == 1
                else self._chunker.index_version
            )
        if replace_documents and isinstance(self._store, DocumentReplacingVectorStore):
            vector_by_chunk_id = dict(zip(chunk_ids, vectors, strict=True))
            for doc_id in sorted(document_chunks):
                selected_chunks = document_chunks[doc_id]
                await self._store.replace_document(
                    selected_chunks,
                    [vector_by_chunk_id[chunk.chunk_id] for chunk in selected_chunks],
                )
        else:
            if replace_documents:
                for doc_id in sorted(document_chunks):
                    await self._store.delete_document(doc_id)
            await self._store.upsert(chunks, vectors)
        self._document_chunking_versions.update(chunking_versions)
        logger.info(
            "chunks_indexed",
            documents=len(document_chunks),
            chunks=len(chunks),
            index_version=(
                f"{self._document_chunking_versions.get(doc_id, self._chunker.index_version)}:"
                f"{self._base_index_version}"
            ),
            chunking_version=self._document_chunking_versions.get(
                doc_id, self._chunker.index_version
            ),
        )
        return chunks

    async def list_document_ids(self) -> set[str]:
        """Enumerate indexed documents so retention jobs can find orphans."""
        if not isinstance(self._store, DocumentListingVectorStore):
            raise TypeError("vector store does not support document listing")
        return await self._store.list_document_ids()

    async def delete_document(self, doc_id: str) -> None:
        """Remove every indexed chunk for ``doc_id``.

        Case-oriented features use this public operation to honor deletion and
        retention requests without reaching through the RAG facade into a
        concrete vector-store adapter.
        """
        await self._store.delete_document(doc_id)
        logger.info("document_index_deleted", doc_id=doc_id)

    async def retrieve(
        self,
        query: str,
        doc_id: str,
        k: int | None = None,
        *,
        mode: RetrievalMode | str | None = None,
    ) -> list[RetrievedChunk]:
        """Return the k chunks of ``doc_id`` most relevant to ``query``."""
        results, _ = await self.retrieve_with_trace(
            query, doc_id=doc_id, agent="direct", k=k, mode=mode
        )
        return results

    async def retrieve_with_trace(
        self,
        query: str,
        *,
        doc_id: str,
        agent: str,
        k: int | None = None,
        mode: RetrievalMode | str | None = None,
    ) -> tuple[list[RetrievedChunk], RetrievalTrace]:
        """Return one ranked result set and its durable provenance trace."""
        result_sets, traces = await self.retrieve_many_with_traces(
            [query], doc_id=doc_id, agent=agent, k=k, mode=mode
        )
        return result_sets[0], traces[0]

    async def retrieve_many(
        self,
        queries: list[str],
        doc_id: str,
        k: int | None = None,
        *,
        mode: RetrievalMode | str | None = None,
    ) -> list[list[RetrievedChunk]]:
        """Retrieve context for several queries with one embedding request.

        Agent prompts commonly ask several focused questions of the same
        document. Batching their embeddings reduces provider round-trips and
        querying the store concurrently preserves the async latency benefit.
        """
        results, _ = await self.retrieve_many_with_traces(
            queries, doc_id=doc_id, agent="direct", k=k, mode=mode
        )
        return results

    async def retrieve_many_with_traces(
        self,
        queries: list[str],
        *,
        doc_id: str,
        agent: str,
        k: int | None = None,
        mode: RetrievalMode | str | None = None,
    ) -> tuple[list[list[RetrievedChunk]], list[RetrievalTrace]]:
        """Retrieve a query batch and retain every ranked result for auditing."""
        if not queries:
            return [], []

        requested_k = self._default_k if k is None else k
        if requested_k < 1:
            raise ValueError("k must be positive")
        effective_mode = self._retrieval_mode if mode is None else RetrievalMode(mode)
        if effective_mode is RetrievalMode.HYBRID and not isinstance(
            self._store, LexicalVectorStore
        ):
            raise TypeError("hybrid retrieval requires a lexical-capable vector store")
        score_type = _score_type(effective_mode, reranked=self._reranker is not None)
        candidate_k = self._candidate_k(requested_k, effective_mode)
        batch_id = uuid.uuid4().hex
        batch_started = time.perf_counter()
        embedding_started = time.perf_counter()
        try:
            vectors = await embed_query_texts(self._embedder, queries)
        except Exception as exc:
            embedding_duration_ms = (time.perf_counter() - embedding_started) * 1000
            batch_duration_ms = (time.perf_counter() - batch_started) * 1000
            traces = [
                self._build_retrieval_trace(
                    agent=agent,
                    batch_id=batch_id,
                    doc_id=doc_id,
                    query=query,
                    query_index=query_index,
                    requested_k=requested_k,
                    results=[],
                    embedding_duration_ms=embedding_duration_ms,
                    search_duration_ms=0.0,
                    batch_duration_ms=batch_duration_ms,
                    score_type=score_type,
                    retrieval_mode=effective_mode,
                    candidate_k=candidate_k,
                    error=_error_text(exc),
                )
                for query_index, query in enumerate(queries)
            ]
            logger.warning(
                "retrieval_embedding_failed",
                doc_id=doc_id,
                agent=agent,
                queries=len(queries),
                error_type=type(exc).__name__,
            )
            raise RetrievalBatchError(exc, traces) from exc
        embedding_duration_ms = (time.perf_counter() - embedding_started) * 1000

        if len(vectors) != len(queries):
            cardinality_error = ValueError(
                f"embedding provider returned {len(vectors)} vectors for {len(queries)} queries"
            )
            batch_duration_ms = (time.perf_counter() - batch_started) * 1000
            traces = [
                self._build_retrieval_trace(
                    agent=agent,
                    batch_id=batch_id,
                    doc_id=doc_id,
                    query=query,
                    query_index=query_index,
                    requested_k=requested_k,
                    results=[],
                    embedding_duration_ms=embedding_duration_ms,
                    search_duration_ms=0.0,
                    batch_duration_ms=batch_duration_ms,
                    score_type=score_type,
                    retrieval_mode=effective_mode,
                    candidate_k=candidate_k,
                    error=_error_text(cardinality_error),
                )
                for query_index, query in enumerate(queries)
            ]
            raise RetrievalBatchError(cardinality_error, traces) from cardinality_error

        async def query_store(
            query: str,
            vector: list[float],
        ) -> tuple[list[RetrievedChunk], float, Exception | None]:
            started = time.perf_counter()
            try:
                if effective_mode is RetrievalMode.HYBRID:
                    lexical_store = cast(LexicalVectorStore, self._store)
                    dense_items, lexical_items = await asyncio.gather(
                        self._store.query(vector, doc_id=doc_id, k=candidate_k),
                        lexical_store.lexical_query(query, doc_id=doc_id, k=candidate_k),
                    )
                    items = reciprocal_rank_fusion(
                        [dense_items, lexical_items],
                        k=candidate_k,
                        constant=self._rrf_constant,
                        weights=[self._dense_weight, self._lexical_weight],
                    )
                else:
                    items = await self._store.query(vector, doc_id=doc_id, k=candidate_k)
                if self._reranker is not None:
                    items = await self._reranker.rerank(query, items, requested_k)
                else:
                    items = items[:requested_k]
            except Exception as exc:
                return [], (time.perf_counter() - started) * 1000, exc
            return items, (time.perf_counter() - started) * 1000, None

        timed_results = await asyncio.gather(
            *(query_store(query, vector) for query, vector in zip(queries, vectors, strict=True))
        )
        batch_duration_ms = (time.perf_counter() - batch_started) * 1000
        results = [items for items, _, _ in timed_results]
        traces = [
            self._build_retrieval_trace(
                agent=agent,
                batch_id=batch_id,
                doc_id=doc_id,
                query=query,
                query_index=query_index,
                requested_k=requested_k,
                results=items,
                embedding_duration_ms=embedding_duration_ms,
                search_duration_ms=search_duration_ms,
                batch_duration_ms=batch_duration_ms,
                score_type=score_type,
                retrieval_mode=effective_mode,
                candidate_k=candidate_k,
                error=_error_text(error) if error is not None else None,
            )
            for query_index, (
                query,
                (items, search_duration_ms, error),
            ) in enumerate(zip(queries, timed_results, strict=True))
        ]
        failures = [error for _, _, error in timed_results if error is not None]
        logger.info(
            "chunks_retrieved_batch",
            doc_id=doc_id,
            agent=agent,
            queries=len(queries),
            results=sum(len(items) for items in results),
            query_hashes=[trace.query_sha256[:12] for trace in traces],
            top_scores=[
                round(trace.results[0].score, 4) if trace.results else None for trace in traces
            ],
            embedding_duration_ms=round(embedding_duration_ms, 1),
            retrieval_mode=effective_mode.value,
            score_type=score_type,
            failed_queries=len(failures),
        )
        if failures:
            raise RetrievalBatchError(failures[0], traces)
        return results, traces

    def _build_retrieval_trace(
        self,
        *,
        agent: str,
        batch_id: str,
        doc_id: str,
        query: str,
        query_index: int,
        requested_k: int,
        results: list[RetrievedChunk],
        embedding_duration_ms: float,
        search_duration_ms: float,
        batch_duration_ms: float,
        score_type: str,
        retrieval_mode: RetrievalMode,
        candidate_k: int,
        error: str | None = None,
    ) -> RetrievalTrace:
        return RetrievalTrace(
            agent=agent,
            batch_id=batch_id,
            doc_id=doc_id,
            query_index=query_index,
            query=query,
            query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            requested_k=requested_k,
            candidate_k=candidate_k,
            candidate_multiplier=self._candidate_multiplier,
            returned_count=len(results),
            retrieval_mode=retrieval_mode.value,
            embedding_model=self._embedding_model,
            embedding_model_revision=self._embedding_model_revision,
            embedding_query_instruction=self._embedding_query_instruction,
            embedding_query_instruction_sha256=(
                hashlib.sha256(self._embedding_query_instruction.encode("utf-8")).hexdigest()
                if self._embedding_query_instruction is not None
                else None
            ),
            vector_store=self._vector_store_name,
            index_version=(
                f"{self._document_chunking_versions.get(doc_id, self._chunker.index_version)}:"
                f"{self._base_index_version}"
            ),
            chunking_version=self._document_chunking_versions.get(
                doc_id, self._chunker.index_version
            ),
            score_type=score_type,
            rrf_constant=(self._rrf_constant if retrieval_mode is RetrievalMode.HYBRID else None),
            dense_weight=(self._dense_weight if retrieval_mode is RetrievalMode.HYBRID else None),
            lexical_weight=(
                self._lexical_weight if retrieval_mode is RetrievalMode.HYBRID else None
            ),
            reranker_model=self._reranker_name,
            reranker_model_revision=self._reranker_model_revision,
            embedding_duration_ms=embedding_duration_ms,
            search_duration_ms=search_duration_ms,
            total_duration_ms=embedding_duration_ms + search_duration_ms,
            batch_duration_ms=batch_duration_ms,
            error=error,
            results=[
                RetrievedItemTrace(
                    rank=rank,
                    chunk_id=item.chunk.chunk_id,
                    doc_id=item.chunk.doc_id,
                    section=item.chunk.section,
                    page_start=item.chunk.page_start,
                    page_end=item.chunk.page_end,
                    score=item.score,
                    content_sha256=hashlib.sha256(item.chunk.text.encode("utf-8")).hexdigest(),
                    source_metadata=item.chunk.metadata,
                    source_url=_metadata_text(item.chunk.metadata, "official_url", "source_url"),
                    source_release_id=_metadata_text(
                        item.chunk.metadata, "corpus_release_id", "source_release_id"
                    ),
                    source_snapshot_sha256=_metadata_text(
                        item.chunk.metadata, "source_snapshot_sha256"
                    ),
                    source_content_sha256=_metadata_text(item.chunk.metadata, "content_sha256"),
                    source_provision_id=_metadata_text(item.chunk.metadata, "provision_id"),
                    source_unit_id=_metadata_text(item.chunk.metadata, "unit_id"),
                    text_preview=(
                        _audit_preview(item.chunk.text) if self._include_trace_previews else None
                    ),
                )
                for rank, item in enumerate(results, start=1)
            ],
        )


def reciprocal_rank_fusion(
    rankings: list[list[RetrievedChunk]],
    *,
    k: int,
    constant: int = 60,
    weights: list[float] | None = None,
) -> list[RetrievedChunk]:
    """Fuse rankings without assuming their raw scores share a scale."""
    if k < 1:
        return []
    if constant < 1:
        raise ValueError("RRF constant must be positive")
    effective_weights = weights or [1.0] * len(rankings)
    if len(effective_weights) != len(rankings):
        raise ValueError("one RRF weight is required per ranking")
    if any(weight <= 0 for weight in effective_weights):
        raise ValueError("RRF weights must be positive")

    chunks: dict[str, Chunk] = {}
    fused_scores: defaultdict[str, float] = defaultdict(float)
    for ranking, weight in zip(rankings, effective_weights, strict=True):
        seen: set[str] = set()
        for rank, item in enumerate(ranking, start=1):
            chunk_id = item.chunk.chunk_id
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            chunks[chunk_id] = item.chunk
            fused_scores[chunk_id] += weight / (constant + rank)

    fused = [
        RetrievedChunk(chunk=chunks[chunk_id], score=score)
        for chunk_id, score in fused_scores.items()
    ]
    fused.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
    return fused[:k]


def _score_type(mode: RetrievalMode, *, reranked: bool) -> str:
    if reranked:
        return "reranker_score"
    if mode is RetrievalMode.HYBRID:
        return "rrf_score"
    return "cosine_similarity"


def _audit_preview(text: str) -> str:
    """Return a bounded preview suitable for persisted audit records."""
    normalized = " ".join(text.split())
    if len(normalized) <= AUDIT_PREVIEW_CHARS:
        return normalized
    return normalized[: AUDIT_PREVIEW_CHARS - 1].rstrip() + "…"


def _metadata_text(metadata: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _component_revision(component: object | None) -> str | None:
    """Best-effort immutable model revision without depending on one provider."""
    if component is None:
        return None
    for name in ("model_revision", "revision", "_model_revision", "_revision"):
        value = getattr(component, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()

    model = getattr(component, "_model", None)
    config = getattr(model, "config", None)
    for candidate in (config, model):
        for name in ("_commit_hash", "commit_hash", "revision"):
            value = getattr(candidate, name, None)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _error_text(error: Exception) -> str:
    """Stable, compact error text suitable for a persisted trace."""
    return f"{type(error).__name__}: {error}"
