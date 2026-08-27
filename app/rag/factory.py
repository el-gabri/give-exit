"""Composition root for the configurable RAG stack."""

from typing import cast

from app.core.config import (
    EmbeddingProvider,
    LLMProvider,
    RerankerProvider,
    Settings,
    VectorStoreBackend,
)
from app.rag.chunking import SectionAwareChunker
from app.rag.embeddings import (
    EmbeddingBackend,
    GeminiEmbeddingClient,
    MockEmbeddingClient,
    OpenAIEmbeddingClient,
    SentenceTransformerEmbeddingClient,
)
from app.rag.pipeline import RagPipeline
from app.rag.reranking import Reranker, SentenceTransformerReranker
from app.rag.vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    PostgresVectorStore,
    VectorStore,
    versioned_collection_name,
)

_CONFIGURED_RERANKER = object()

_DEFAULT_EMBEDDING_MODELS: dict[EmbeddingProvider, str] = {
    EmbeddingProvider.OPENAI: "text-embedding-3-small",
    EmbeddingProvider.GEMINI: "gemini-embedding-2",
    EmbeddingProvider.SENTENCE_TRANSFORMERS: "BAAI/bge-m3",
    EmbeddingProvider.MOCK: "mock-hashed-bow-v1",
}


def create_embedding_client(settings: Settings) -> EmbeddingBackend:
    provider = _embedding_provider_for(settings)
    model = _embedding_model_for(settings, provider)
    if provider is EmbeddingProvider.MOCK:
        return MockEmbeddingClient(query_instruction=settings.embedding_query_instruction)
    if provider is EmbeddingProvider.SENTENCE_TRANSFORMERS:
        return SentenceTransformerEmbeddingClient(
            model=model,
            query_instruction=settings.embedding_query_instruction,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
            model_revision=settings.embedding_model_revision,
            show_progress_bar=settings.embedding_show_progress_bar,
        )
    if provider is EmbeddingProvider.GEMINI:
        api_key = _embedding_api_key(settings.gemini_api_key, provider="Gemini")
        return GeminiEmbeddingClient(
            api_key=api_key,
            model=model,
            dimensions=settings.gemini_embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            query_instruction=settings.embedding_query_instruction,
        )
    if provider is EmbeddingProvider.OPENAI:
        api_key = _embedding_api_key(settings.openai_api_key, provider="OpenAI")
        return OpenAIEmbeddingClient(
            api_key=api_key,
            model=model,
            query_instruction=settings.embedding_query_instruction,
            model_revision=settings.embedding_model_revision,
        )
    raise ValueError(f"Unsupported embedding provider: {provider}")


def create_vector_store(
    settings: Settings,
    *,
    embedding_model: str | None = None,
    corpus_version: str | None = None,
) -> VectorStore:
    collection_name = vector_store_index_name(
        settings,
        corpus_version=corpus_version,
        embedding_model=embedding_model,
    )
    return create_vector_store_for_index(settings, index_name=collection_name)


def create_vector_store_for_index(settings: Settings, *, index_name: str) -> VectorStore:
    """Build one backend for an explicit immutable index namespace."""

    if not index_name.strip():
        raise ValueError("index_name must be non-empty")
    if settings.vector_store is VectorStoreBackend.MEMORY:
        return InMemoryVectorStore(index_name=index_name)
    if settings.vector_store is VectorStoreBackend.POSTGRES:
        dsn = (settings.postgres_dsn or "").strip()
        if not dsn:  # guarded by Settings, retained for direct construction clarity
            raise ValueError("LITIGATION_POSTGRES_DSN is required for the Postgres vector store")
        return PostgresVectorStore(dsn=dsn, index_name=index_name)
    settings.chroma_dir.mkdir(parents=True, exist_ok=True)
    return ChromaVectorStore(persist_dir=settings.chroma_dir, collection_name=index_name)


def vector_store_index_name(
    settings: Settings,
    *,
    embedding_model: str | None = None,
    corpus_version: str | None = None,
) -> str:
    """Return the stable namespace shared by compatible vector-store backends."""

    return versioned_collection_name(
        corpus_version or settings.rag_corpus_version,
        embedding_model or _embedding_model_for(settings, _embedding_provider_for(settings)),
        prefix="give-exit-consumer",
    )


def configured_embedding_identity(settings: Settings, *, embedding_model: str | None = None) -> str:
    """Return the versioned embedding-space identity without loading the model."""

    model = embedding_model or _embedding_model_for(settings, _embedding_provider_for(settings))
    revision = (settings.embedding_model_revision or "").strip()
    return f"{model}@{revision}" if revision else model


def create_reranker(settings: Settings) -> Reranker | None:
    if settings.reranker_provider is RerankerProvider.NONE:
        return None
    return SentenceTransformerReranker(
        settings.reranker_model,
        device=settings.reranker_device,
        batch_size=settings.reranker_batch_size,
        model_revision=settings.reranker_model_revision,
    )


def create_rag_pipeline(
    settings: Settings,
    *,
    corpus_version: str | None = None,
    embedder: EmbeddingBackend | None = None,
    reranker: Reranker | None | object = _CONFIGURED_RERANKER,
) -> RagPipeline:
    effective_embedder = embedder or create_embedding_client(settings)
    effective_reranker = (
        create_reranker(settings)
        if reranker is _CONFIGURED_RERANKER
        else cast(Reranker | None, reranker)
    )
    effective_corpus_version = corpus_version or settings.rag_corpus_version
    fallback_model = _embedding_model_for(settings, _embedding_provider_for(settings))
    embedding_model = str(getattr(effective_embedder, "model_name", fallback_model))
    embedding_identity = configured_embedding_identity(settings, embedding_model=embedding_model)
    return RagPipeline(
        embedder=effective_embedder,
        store=create_vector_store(
            settings,
            embedding_model=embedding_identity,
            corpus_version=effective_corpus_version,
        ),
        chunker=SectionAwareChunker(
            target_chars=settings.chunk_target_chars,
            overlap_chars=settings.chunk_overlap_chars,
        ),
        default_k=settings.retrieval_k,
        include_trace_previews=settings.retrieval_trace_include_previews,
        retrieval_mode=settings.retrieval_mode,
        candidate_multiplier=settings.retrieval_candidate_multiplier,
        rrf_constant=settings.retrieval_rrf_constant,
        dense_weight=settings.retrieval_dense_weight,
        lexical_weight=settings.retrieval_lexical_weight,
        reranker=effective_reranker,
        corpus_version=effective_corpus_version,
        embedding_expected_dimension=settings.embedding_expected_dimensions,
        embedding_artifacts_dir=settings.embedding_artifacts_dir,
        embedding_shard_size=settings.embedding_index_shard_size,
        embedding_require_model_revision=settings.embedding_require_model_revision,
        embedding_query_timeout_seconds=settings.embedding_query_timeout_seconds,
        embedding_query_max_concurrency=settings.embedding_query_max_concurrency,
        embedding_circuit_breaker_failures=settings.embedding_circuit_breaker_failures,
        embedding_circuit_breaker_reset_seconds=(
            settings.embedding_circuit_breaker_reset_seconds
        ),
        embedding_query_cache_ttl_seconds=settings.embedding_query_cache_ttl_seconds,
        embedding_query_cache_max_entries=settings.embedding_query_cache_max_entries,
        embedding_lexical_fallback=settings.embedding_lexical_fallback,
    )


def _embedding_provider_for(settings: Settings) -> EmbeddingProvider:
    provider = settings.embedding_provider
    if provider is not EmbeddingProvider.AUTO:
        return provider
    return {
        LLMProvider.MOCK: EmbeddingProvider.MOCK,
        LLMProvider.OPENAI: EmbeddingProvider.OPENAI,
        LLMProvider.GEMINI: EmbeddingProvider.GEMINI,
        # Anthropic has no embeddings API. The local default lets a Claude-only
        # installation work without requiring a second vendor key.
        LLMProvider.ANTHROPIC: EmbeddingProvider.SENTENCE_TRANSFORMERS,
    }[settings.llm_provider]


def _embedding_model_for(settings: Settings, provider: EmbeddingProvider) -> str:
    override = (settings.embedding_model or "").strip()
    if override:
        return override
    try:
        return _DEFAULT_EMBEDDING_MODELS[provider]
    except KeyError as exc:  # pragma: no cover - AUTO is resolved above
        raise ValueError(f"No default model for embedding provider: {provider}") from exc


def _embedding_api_key(value: str | None, *, provider: str) -> str:
    key = (value or "").strip()
    if not key:
        variable = provider.upper()
        raise ValueError(f"LITIGATION_{variable}_API_KEY is required for {provider} embeddings")
    return key
