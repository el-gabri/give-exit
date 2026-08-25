"""Orphaned-vector cleanup: restarts must not strand indexed case data."""

import pytest

from app.consumer.legal_corpus import LEGAL_CHUNKING_VERSION, get_default_legal_corpus
from app.consumer.service import (
    ConsumerCaseService,
    ConsumerLegalCorpusNotReadyError,
)
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.security.prompt_injection import PromptInjectionDetector


def _stray_document() -> ParsedDocument:
    return ParsedDocument(
        filename="evidencia.pdf",
        pages=[DocumentPage(number=1, text="Comprovante de pagamento de R$ 100,00.")],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )


async def test_purge_removes_only_documents_without_a_live_case() -> None:
    corpus = get_default_legal_corpus()
    rag = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=rag,
        legal_corpus=corpus,
    )
    await rag.index_chunks(corpus.as_chunks())
    stray = _stray_document()
    await rag.index_document(stray)

    purged = await service.purge_orphaned_documents()

    assert purged == 1
    remaining = await rag.list_document_ids()
    assert stray.doc_id not in remaining
    assert corpus.as_parsed_document().doc_id in remaining


async def test_purge_is_a_noop_on_an_empty_index() -> None:
    rag = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=rag,
        legal_corpus=get_default_legal_corpus(),
    )

    assert await service.purge_orphaned_documents() == 0


async def test_legal_index_is_reused_across_service_instances() -> None:
    class CountingEmbeddingClient(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__()
            self.document_calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.document_calls += 1
            return await super().embed_documents(texts)

    corpus = get_default_legal_corpus()
    embedder = CountingEmbeddingClient()
    rag = RagPipeline(embedder, InMemoryVectorStore())
    first = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=rag,
        legal_corpus=corpus,
    )

    indexed = await first.prepare_legal_corpus()
    second = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=rag,
        legal_corpus=corpus,
    )
    reused = await second.prepare_legal_corpus()

    assert indexed.action == "indexed"
    assert reused.action == "reused"
    assert embedder.document_calls == 1
    configuration = rag.retrieval_configuration(
        requested_k=1,
        doc_id=corpus.as_parsed_document().doc_id,
    )
    assert configuration["chunking_version"] == LEGAL_CHUNKING_VERSION


async def test_notice_path_fails_fast_when_legal_index_is_missing() -> None:
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=RagPipeline(MockEmbeddingClient(), InMemoryVectorStore()),
        legal_corpus=get_default_legal_corpus(),
    )

    assert await service.legal_corpus_ready() is False
    with pytest.raises(ConsumerLegalCorpusNotReadyError, match="indexada"):
        await service._ensure_legal_corpus_indexed()
