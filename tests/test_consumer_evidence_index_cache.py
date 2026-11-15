"""Evidence indexing should happen once per immutable combined-document version."""

from app.consumer.service import ConsumerCaseService
from app.consumer.store import ConsumerCaseStore
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.security.prompt_injection import PromptInjectionDetector


class _CountingEmbedder(MockEmbeddingClient):
    def __init__(self) -> None:
        super().__init__()
        self.document_calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        return await super().embed_documents(texts)


def _document(text: str) -> ParsedDocument:
    return ParsedDocument(
        filename="evidencia.pdf",
        pages=[DocumentPage(number=1, text=text)],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )


async def test_reuses_current_evidence_vectors_and_replaces_changed_evidence() -> None:
    embedder = _CountingEmbedder()
    rag = RagPipeline(embedder, InMemoryVectorStore())
    store = ConsumerCaseStore()
    record, _ = store.create()
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=rag,
        store=store,
    )
    first = _document("Comprovante de pagamento de R$ 100,00.")

    assert await service._ensure_evidence_indexed(record, first) is False
    assert embedder.document_calls == 1
    assert await service._ensure_evidence_indexed(record, first) is True
    assert embedder.document_calls == 1

    second = _document("Comprovante de pagamento de R$ 200,00.")
    assert await service._ensure_evidence_indexed(record, second) is False
    assert embedder.document_calls == 2
    remaining = await rag.list_document_ids()
    assert len(remaining) == 1
    assert await _indexed_texts(rag, next(iter(remaining))) == [
        "Comprovante de pagamento de R$ 200,00."
    ]


async def test_two_cases_with_the_same_evidence_do_not_share_an_index() -> None:
    """Deleting one case must not take another case's evidence vectors with it.

    The index id was the hash of the evidence text alone, so two cases that
    uploaded the same file shared one id: deleting, expiring or re-indexing
    either case removed the vectors of both, and the survivor's next notice
    failed for lack of documentary support.
    """
    rag = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    store = ConsumerCaseStore()
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=rag,
        store=store,
    )
    deleted, deleted_token = store.create()
    survivor, _ = store.create()
    evidence = _document("Comprovante de pagamento de R$ 100,00.")
    await service._ensure_evidence_indexed(deleted, evidence)
    await service._ensure_evidence_indexed(survivor, evidence)

    await service.delete_case(deleted.case_id, deleted_token)

    survivor_doc_id = survivor.active_evidence_document_id
    assert survivor_doc_id is not None
    assert await rag.list_document_ids() == {survivor_doc_id}
    assert await _indexed_texts(rag, survivor_doc_id) == [
        "Comprovante de pagamento de R$ 100,00."
    ]


async def _indexed_texts(rag: RagPipeline, doc_id: str) -> list[str]:
    results = await rag.retrieve("comprovante de pagamento", doc_id=doc_id, k=5)
    return [item.chunk.text for item in results]
