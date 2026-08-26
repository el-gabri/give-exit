"""Ready-to-run retrievers for the Consumer legal golden dataset."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_index import legal_corpus_is_indexed
from app.core.config import RetrievalMode, Settings
from app.rag.embeddings import MockEmbeddingClient
from app.rag.factory import create_rag_pipeline
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.rag import RetrievedChunk

PipelineFactory = Callable[[LegalCorpus], RagPipeline]


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
        return await pipeline.retrieve(
            query,
            doc_id=doc_id,
            k=k,
            mode=RetrievalMode.HYBRID,
        )

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

    async def _ready(self) -> tuple[RagPipeline, str]:
        if self._pipeline is not None and self._doc_id is not None:
            return self._pipeline, self._doc_id
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pipeline is None or self._doc_id is None:
                corpus = get_default_legal_corpus()
                pipeline = self._factory(corpus)
                chunks = corpus.as_chunks()
                if pipeline.embedding_artifacts_dir is None:
                    await pipeline.index_chunks(chunks)
                elif not await legal_corpus_is_indexed(pipeline, corpus):
                    raise RuntimeError(
                        "the configured legal embedding generation is not active; "
                        "run `python -m app.consumer.preindex_legal` first"
                    )
                self._pipeline = pipeline
                self._doc_id = chunks[0].doc_id
                self._corpus = corpus
        assert self._pipeline is not None and self._doc_id is not None
        return self._pipeline, self._doc_id


def _offline_pipeline(corpus: LegalCorpus) -> RagPipeline:
    corpus_version = f"{corpus.release_id}-{corpus.corpus_sha256[:12]}"
    return RagPipeline(
        embedder=MockEmbeddingClient(),
        store=InMemoryVectorStore(index_name=f"consumer-{corpus_version}"),
        retrieval_mode=RetrievalMode.HYBRID,
        corpus_version=corpus_version,
    )


def _configured_pipeline(corpus: LegalCorpus) -> RagPipeline:
    """Build the provider selected by LITIGATION_EMBEDDING_* settings."""

    return create_rag_pipeline(
        Settings(),
        corpus_version=f"{corpus.release_id}-{corpus.corpus_sha256[:12]}",
    )


offline_hybrid_retriever = _LazyConsumerRetriever(
    _offline_pipeline,
    retriever_id="offline_mock_bm25_hybrid",
)
configured_hybrid_retriever = _LazyConsumerRetriever(
    _configured_pipeline,
    retriever_id="configured_hybrid",
)
