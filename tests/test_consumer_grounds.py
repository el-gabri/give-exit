"""Relevance and policy gates before retrieved law becomes cited authority."""

import hashlib

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import provision_is_eligible
from app.consumer.retrieval import infer_retrieval_category
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
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace
from app.security.prompt_injection import PromptInjectionDetector


def _service() -> ConsumerCaseService:
    return ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=RagPipeline(MockEmbeddingClient(), InMemoryVectorStore()),
        legal_corpus=get_default_legal_corpus(),
    )


def _facts(
    *,
    category: ConsumerIssueCategory = ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
    complaint: str = "A empresa cobrou duas vezes a mesma compra.",
) -> ConsumerCaseFacts:
    return ConsumerCaseFacts.model_validate(
        {
            "issue_category": category,
            "complaint_summary": complaint,
            "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
        }
    )


def _corpus_chunks(count: int, facts: ConsumerCaseFacts | None = None) -> list:
    """Chunks that resolve to real provisions, so grounds can be built."""
    corpus = get_default_legal_corpus()
    service = _service()
    selected_facts = facts or _facts()
    category = infer_retrieval_category(
        selected_facts.issue_category.value if selected_facts.issue_category else "other",
        selected_facts.complaint_summary or "",
    )
    resolved = [
        chunk
        for chunk in corpus.as_chunks()
        if any(
            provision_is_eligible(category, provision.provision_id)
            for provision in service._legal_corpus.provisions_for_chunk(
                RetrievedChunk(chunk=chunk, score=1.0)
            )
        )
    ]
    assert len(resolved) >= count, "corpus should expose enough resolvable chunks"
    return resolved[:count]


def _traces(result_sets: list[list[RetrievedChunk]]) -> list[RetrievalTrace]:
    traces: list[RetrievalTrace] = []
    for query_index, results in enumerate(result_sets):
        query = f"consulta {query_index}"
        traces.append(
            RetrievalTrace(
                batch_id="batch",
                agent="consumer_legal_authorities",
                doc_id=results[0].chunk.doc_id if results else "legal-doc",
                query_index=query_index,
                query=query,
                query_sha256=hashlib.sha256(query.encode()).hexdigest(),
                requested_k=max(1, len(results)),
                candidate_k=max(1, len(results)),
                returned_count=len(results),
                retrieval_mode="hybrid",
                embedding_model="test",
                vector_store="memory",
                index_version="test",
                chunking_version="test",
                score_type="rrf_score",
                rrf_constant=60,
                dense_weight=1.0,
                lexical_weight=1.0,
                results=[
                    RetrievedItemTrace(
                        rank=rank,
                        chunk_id=result.chunk.chunk_id,
                        doc_id=result.chunk.doc_id,
                        page_start=result.chunk.page_start,
                        page_end=result.chunk.page_end,
                        score=result.score,
                        content_sha256=hashlib.sha256(
                            result.chunk.text.encode()
                        ).hexdigest(),
                    )
                    for rank, result in enumerate(results, start=1)
                ],
            )
        )
    return traces


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

    grounds = service._legal_grounds(results, _facts(), _traces(results))

    assert grounds, "the top-ranked article must still ground the notice"
    cited = {ground.authority.chunk_id for ground in grounds}
    assert weak.chunk_id not in cited


def test_low_scoring_top_article_does_not_bypass_the_absolute_support_gate() -> None:
    service = _service()
    [only] = _corpus_chunks(1)

    results = [[RetrievedChunk(chunk=only, score=0.0001)]]
    grounds = service._legal_grounds(results, _facts(), _traces(results))

    assert grounds == []


def test_candidates_beyond_the_window_are_ignored() -> None:
    service = _service()
    facts = _facts(
        category=ConsumerIssueCategory.OTHER,
        complaint="O contrato de adesão está ilegível e contém cláusula abusiva oculta.",
    )
    chunks = _corpus_chunks(MAX_GROUND_CANDIDATES + 2, facts)
    # Strictly descending scores that all clear the floor, so the candidate
    # window is the only thing that can bound the result.
    ranked = [
        RetrievedChunk(chunk=chunk, score=1.0 - index * 0.01)
        for index, chunk in enumerate(chunks)
    ]
    assert ranked[-1].score > ranked[0].score * MIN_GROUND_SCORE_RATIO

    results = [ranked]
    grounds = service._legal_grounds(results, facts, _traces(results))

    eligible = {item.chunk.chunk_id for item in ranked[:MAX_GROUND_CANDIDATES]}
    assert {ground.authority.chunk_id for ground in grounds} <= eligible
    assert ranked[-1].chunk.chunk_id not in {g.authority.chunk_id for g in grounds}


def test_no_results_yield_no_grounds() -> None:
    assert _service()._legal_grounds([], _facts(), []) == []


def test_retrieved_but_category_ineligible_article_is_not_cited() -> None:
    service = _service()
    ineligible = next(
        chunk
        for chunk in get_default_legal_corpus().as_chunks()
        if chunk.metadata.get("provision_id") == "br-cdc-art-49"
    )
    results = [[RetrievedChunk(chunk=ineligible, score=0.03)]]

    assert service._legal_grounds(results, _facts(), _traces(results)) == []
