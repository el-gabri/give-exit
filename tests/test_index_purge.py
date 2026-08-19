"""Orphaned-vector cleanup: restarts must not strand indexed case data."""

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.service import ConsumerCaseService
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
