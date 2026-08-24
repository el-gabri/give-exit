"""Focused tests for the classifier's bounded, auditable RAG context."""

from app.agents.classifier import (
    CLASSIFICATION_K,
    CLASSIFICATION_QUERIES,
    ClassifierAgent,
)
from app.llm.mock_client import MockLLMClient
from app.orchestration.state import AnalysisState
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument


async def test_classifier_uses_bounded_chunk_context_with_retrieval_traces() -> None:
    document = ParsedDocument(
        filename="peticao.pdf",
        pages=[
            DocumentPage(
                number=1,
                text=(
                    "DOS FATOS\n\nA autora relata cobrancas indevidas em uma "
                    "relacao de consumo bancaria."
                ),
            ),
            DocumentPage(
                number=2,
                text=(
                    "DOS PEDIDOS\n\nRequer restituicao e indenizacao por danos "
                    "morais com fundamento no Codigo de Defesa do Consumidor."
                ),
            ),
        ],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    rag = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=InMemoryVectorStore(),
        default_k=9,
    )
    chunks = await rag.index_document(document)
    agent = ClassifierAgent(MockLLMClient(), rag)

    built = await agent.build_user_prompt(
        AnalysisState(document=document, chunks=chunks)
    )

    assert len(built.retrievals) == len(CLASSIFICATION_QUERIES) == 3
    assert [trace.query for trace in built.retrievals] == CLASSIFICATION_QUERIES
    assert {trace.agent for trace in built.retrievals} == {"classifier"}
    assert {trace.requested_k for trace in built.retrievals} == {CLASSIFICATION_K}
    included_ids = {
        result.chunk_id
        for trace in built.retrievals
        for result in trace.results
        if result.included_in_context
    }
    assert included_ids
    assert included_ids <= {chunk.chunk_id for chunk in chunks}
    assert all(f'chunk_id="{chunk_id}"' in built.text for chunk_id in included_ids)
