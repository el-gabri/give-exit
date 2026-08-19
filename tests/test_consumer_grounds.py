"""Relevance floor applied before a retrieved article becomes cited authority."""

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import ConsumerCaseFacts, ConsumerIssueCategory
from app.consumer.service import (
    MAX_GROUND_CANDIDATES,
    MIN_GROUND_SCORE_RATIO,
    ConsumerCaseService,
)
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.rag import RetrievedChunk
from app.security.prompt_injection import PromptInjectionDetector


def _service() -> ConsumerCaseService:
    return ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=RagPipeline(MockEmbeddingClient(), InMemoryVectorStore()),
        legal_corpus=get_default_legal_corpus(),
    )


def _facts() -> ConsumerCaseFacts:
    return ConsumerCaseFacts.model_validate(
        {
            "issue_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
            "complaint_summary": "A empresa cobrou duas vezes a mesma compra.",
            "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
        }
    )


def _corpus_chunks(count: int) -> list:
    """Chunks that resolve to real provisions, so grounds can be built."""
    corpus = get_default_legal_corpus()
    service = _service()
    resolved = [
        chunk
        for chunk in corpus.as_chunks()
        if service._legal_corpus.provisions_for_chunk(RetrievedChunk(chunk=chunk, score=1.0))
    ]
    assert len(resolved) >= count, "corpus should expose enough resolvable chunks"
    return resolved[:count]


def test_weakly_ranked_articles_are_not_cited_as_authority() -> None:
    service = _service()
    strong, weak = _corpus_chunks(2)
    results = [
        [
            RetrievedChunk(chunk=strong, score=1.0),
            # Below half the top score: retrieved, but not authority.
            RetrievedChunk(chunk=weak, score=MIN_GROUND_SCORE_RATIO / 2),
        ]
    ]

    grounds = service._legal_grounds(results, _facts())

    assert grounds, "the top-ranked article must still ground the notice"
    cited = {ground.authority.chunk_id for ground in grounds}
    assert weak.chunk_id not in cited


def test_top_ranked_article_always_survives_the_floor() -> None:
    service = _service()
    [only] = _corpus_chunks(1)

    grounds = service._legal_grounds([[RetrievedChunk(chunk=only, score=0.0001)]], _facts())

    assert len(grounds) >= 1


def test_candidates_beyond_the_window_are_ignored() -> None:
    service = _service()
    chunks = _corpus_chunks(MAX_GROUND_CANDIDATES + 2)
    # Strictly descending scores that all clear the floor, so the candidate
    # window is the only thing that can bound the result.
    ranked = [
        RetrievedChunk(chunk=chunk, score=1.0 - index * 0.01)
        for index, chunk in enumerate(chunks)
    ]
    assert ranked[-1].score > ranked[0].score * MIN_GROUND_SCORE_RATIO

    grounds = service._legal_grounds([ranked], _facts())

    eligible = {item.chunk.chunk_id for item in ranked[:MAX_GROUND_CANDIDATES]}
    assert {ground.authority.chunk_id for ground in grounds} <= eligible
    assert ranked[-1].chunk.chunk_id not in {g.authority.chunk_id for g in grounds}


def test_no_results_yield_no_grounds() -> None:
    assert _service()._legal_grounds([], _facts()) == []
