"""Composition helpers shared by the Consumer API and maintenance commands."""

from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.core.config import Settings
from app.rag.factory import create_embedding_client, create_rag_pipeline, create_reranker
from app.rag.pipeline import RagPipeline


def create_consumer_rag(
    settings: Settings,
    *,
    legal_corpus: LegalCorpus | None = None,
) -> tuple[LegalCorpus, RagPipeline]:
    """Build the exact versioned RAG stack used by Consumer notice generation."""

    corpus = legal_corpus or get_default_legal_corpus()
    embedder = create_embedding_client(settings)
    reranker = create_reranker(settings)
    rag = create_rag_pipeline(
        settings,
        corpus_version=f"{corpus.release_id}-{corpus.corpus_sha256[:12]}",
        embedder=embedder,
        reranker=reranker,
    )
    return corpus, rag
