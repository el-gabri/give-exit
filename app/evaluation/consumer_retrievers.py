"""Ready-to-run retrievers for the Consumer legal golden dataset."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_index import legal_corpus_is_indexed
from app.core.config import RetrievalMode, Settings
from app.evaluation.query_vectors import (
    CachedQueryEmbedder,
    QueryVectorCache,
    QueryVectorCacheMiss,
    cache_contract,
    missing_golden_queries,
    query_vector_root,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.factory import create_embedding_client, create_rag_pipeline
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.evaluation import ConsumerLegalGoldenDataset
from app.schemas.rag import RetrievedChunk

PipelineFactory = Callable[[LegalCorpus], RagPipeline]


class DegradedRetrievalError(RuntimeError):
    """An evaluation query fell back from hybrid retrieval to one channel."""


class _LazyConsumerRetriever:
    """Index one corpus once and reuse it for every golden query."""

    def __init__(self, factory: PipelineFactory, *, retriever_id: str) -> None:
        self._factory = factory
        self._retriever_id = retriever_id
        self._pipeline: RagPipeline | None = None
        self._doc_id: str | None = None
        self._corpus: LegalCorpus | None = None
        self._lock: asyncio.Lock | None = None

    async def __call__(self, query: str, k: int) -> list[RetrievedChunk]:
        pipeline, doc_id = await self._ready()
        results, trace = await pipeline.retrieve_with_trace(
            query, doc_id=doc_id, agent="direct", k=k, mode=RetrievalMode.HYBRID
        )
        if trace.degraded_mode:
            # Scored as hybrid, a lexical-only fallback would pass for the
            # configured stack's result; a failed case makes the run exit 2.
            raise DegradedRetrievalError(f"retrieval degraded: {trace.degraded_mode}")
        return results

    async def evaluation_configuration(
        self, requested_k: int
    ) -> dict[str, str | int | float | None]:
        """Describe the exact stack used by the evaluation runner."""
        pipeline, _ = await self._ready()
        configuration = pipeline.retrieval_configuration(
            requested_k=requested_k,
            mode=RetrievalMode.HYBRID,
            doc_id=self._doc_id,
        )
        configuration["retriever_id"] = self._retriever_id
        if self._corpus is not None:
            configuration["corpus_release_id"] = self._corpus.release_id
            configuration["corpus_sha256"] = self._corpus.corpus_sha256
        return configuration

    def close(self) -> None:
        """Release the stack's store connections; a later query reopens them."""
        if self._pipeline is not None:
            self._pipeline.close()

    async def _ready(self) -> tuple[RagPipeline, str]:
        if self._pipeline is not None and self._doc_id is not None:
            return self._pipeline, self._doc_id
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pipeline is None or self._doc_id is None:
                corpus = get_default_legal_corpus()
                self._pipeline = await prepare_evaluation_pipeline(self._factory, corpus)
                self._doc_id = corpus.document_id
                self._corpus = corpus
        assert self._pipeline is not None and self._doc_id is not None
        return self._pipeline, self._doc_id


def offline_pipeline(corpus: LegalCorpus) -> RagPipeline:
    corpus_version = f"{corpus.release_id}-{corpus.corpus_sha256[:12]}"
    return RagPipeline(
        embedder=MockEmbeddingClient(),
        store=InMemoryVectorStore(index_name=f"consumer-{corpus_version}"),
        retrieval_mode=RetrievalMode.HYBRID,
        corpus_version=corpus_version,
    )


_require_cached_queries = False


def configure_query_vectors(*, require_cached: bool) -> None:
    """Whether configured evaluations may load the model for uncached queries.

    The default reads through the golden query-vector cache and embeds what is
    missing. ``require_cached`` turns a miss into an error, so a run is
    guaranteed never to load the embedding model.
    """
    global _require_cached_queries
    _require_cached_queries = require_cached


def cached_configured_pipeline(
    corpus: LegalCorpus, settings: Settings | None = None
) -> tuple[RagPipeline, CachedQueryEmbedder]:
    """The configured stack, its query embedder reading the golden cache."""

    effective = settings or Settings()
    embedder = CachedQueryEmbedder(
        create_embedding_client(effective), require_cached=_require_cached_queries
    )
    pipeline = create_rag_pipeline(
        effective,
        corpus_version=f"{corpus.release_id}-{corpus.corpus_sha256[:12]}",
        embedder=embedder,
    )
    contract = cache_contract(pipeline.embedding_contract_configuration())
    if contract is not None:
        embedder.bind(QueryVectorCache.open(query_vector_root(effective), contract))
    elif _require_cached_queries:
        pipeline.close()
        raise ValueError(
            "golden query vectors are cached only for a pinned model revision; "
            "set LITIGATION_EMBEDDING_MODEL_REVISION"
        )
    return pipeline, embedder


def enforce_cached_queries(dataset: ConsumerLegalGoldenDataset, *, configured: bool) -> None:
    """Make a configured run fail on uncached golden queries, before any query runs.

    Inside a run, a cache miss surfaces below the pipeline's query guard, which
    turns it into a lexical-only fallback that scores as an abstention. Checking
    coverage first turns it into an error naming the command that fills it.
    """

    if not configured:
        raise ValueError("--require-cached-queries applies only to the configured stack")
    configure_query_vectors(require_cached=True)
    pipeline, embedder = cached_configured_pipeline(get_default_legal_corpus())
    try:
        missing = missing_golden_queries(embedder, dataset)
    finally:
        pipeline.close()
    if missing:
        raise ValueError(str(QueryVectorCacheMiss(missing)))


def configured_pipeline(corpus: LegalCorpus, settings: Settings | None = None) -> RagPipeline:
    """Build the provider selected by LITIGATION_EMBEDDING_* settings."""

    pipeline, _ = cached_configured_pipeline(corpus, settings)
    return pipeline

async def prepare_evaluation_pipeline(
    factory: PipelineFactory,
    corpus: LegalCorpus,
) -> RagPipeline:
    """Build one evaluation stack whose legal corpus is ready to search.

    Offline stacks index the corpus in memory. A configured stack must already
    hold the active embedding generation; evaluation never re-embeds it.
    """

    pipeline = factory(corpus)
    if pipeline.embedding_artifacts_dir is None:
        await pipeline.index_chunks(corpus.as_chunks())
    elif not await legal_corpus_is_indexed(pipeline, corpus):
        raise RuntimeError(
            "the configured legal embedding generation is not active; "
            "run `python -m app.consumer.preindex_legal` first"
        )
    return pipeline


offline_hybrid_retriever = _LazyConsumerRetriever(
    offline_pipeline,
    retriever_id="offline_mock_bm25_hybrid",
)
configured_hybrid_retriever = _LazyConsumerRetriever(
    configured_pipeline,
    retriever_id="configured_hybrid",
)
