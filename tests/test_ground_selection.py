"""The pure ground selector shared by the notice service and the evaluator."""

from __future__ import annotations

import hashlib

from app.consumer.ground_selection import (
    chunk_query_occurrences,
    issue_label,
    select_legal_grounds,
)
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import ConsumerCaseFacts, ConsumerIssueCategory
from app.consumer.service import ConsumerCaseService
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace


def _facts(
    complaint: str = "A empresa cobrou duas vezes a mesma compra.",
) -> ConsumerCaseFacts:
    return ConsumerCaseFacts.model_validate(
        {
            "issue_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
            "complaint_summary": complaint,
            "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
        }
    )


def _trace(
    results: list[RetrievedChunk],
    *,
    query_index: int = 0,
    error: str | None = None,
) -> RetrievalTrace:
    query = f"consulta {query_index}"
    return RetrievalTrace(
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
        error=error,
        results=[
            RetrievedItemTrace(
                rank=rank,
                chunk_id=item.chunk.chunk_id,
                doc_id=item.chunk.doc_id,
                page_start=item.chunk.page_start,
                page_end=item.chunk.page_end,
                score=item.score,
                content_sha256=hashlib.sha256(item.chunk.text.encode()).hexdigest(),
            )
            for rank, item in enumerate(results, start=1)
        ],
    )


def _chunk_for_unit(unit_id: str, *, include_inactive: bool = False) -> Chunk:
    return next(
        chunk
        for chunk in get_default_legal_corpus().as_chunks(include_inactive=include_inactive)
        if chunk.metadata.get("unit_id") == unit_id
    )


def _everything_supported(traces: list[RetrievalTrace]) -> frozenset[str]:
    return frozenset(item.chunk_id for trace in traces for item in trace.results)


def test_service_wrapper_returns_exactly_the_pure_selection() -> None:
    corpus = get_default_legal_corpus()
    service = object.__new__(ConsumerCaseService)
    service._legal_corpus = corpus
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.03)]]
    traces = [_trace(results[0])]

    via_service = service._legal_grounds(results, _facts(), traces)
    direct = select_legal_grounds(corpus, _facts(), results, traces)

    assert via_service == direct
    assert [ground.authority.unit_id for ground in direct] == [
        "br-cdc-art-42-paragrafo-unico"
    ]


def test_support_gate_is_injectable() -> None:
    corpus = get_default_legal_corpus()
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.0001)]]
    traces = [_trace(results[0])]

    assert select_legal_grounds(corpus, _facts(), results, traces) == []
    grounds = select_legal_grounds(
        corpus, _facts(), results, traces, support=_everything_supported
    )
    assert [ground.authority.unit_id for ground in grounds] == [
        "br-cdc-art-42-paragrafo-unico"
    ]


def test_chunks_without_retrieval_agreement_are_skipped() -> None:
    corpus = get_default_legal_corpus()
    supported = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    unsupported = _chunk_for_unit("br-cdc-art-43-paragrafo-2")
    results = [
        [
            RetrievedChunk(chunk=unsupported, score=0.03),
            RetrievedChunk(chunk=supported, score=0.029),
        ]
    ]

    grounds = select_legal_grounds(
        corpus,
        _facts(),
        results,
        [_trace(results[0])],
        support=lambda _traces: frozenset({supported.chunk_id}),
    )

    assert [ground.authority.unit_id for ground in grounds] == [
        "br-cdc-art-42-paragrafo-unico"
    ]


def test_out_of_scope_complaint_has_no_grounds() -> None:
    corpus = get_default_legal_corpus()
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.03)]]
    facts = _facts("Meu empregador não pagou meu salário nem o vale-transporte.")

    assert select_legal_grounds(corpus, facts, results, [_trace(results[0])]) == []


def test_supported_ids_without_results_yield_no_grounds() -> None:
    corpus = get_default_legal_corpus()

    grounds = select_legal_grounds(
        corpus,
        _facts(),
        [[]],
        [],
        support=lambda _traces: frozenset({"any-chunk"}),
    )

    assert grounds == []


def test_inactive_provisions_and_units_are_never_cited() -> None:
    corpus = get_default_legal_corpus()
    vetoed_article = next(
        chunk
        for chunk in corpus.as_chunks(include_inactive=True)
        if chunk.metadata.get("provision_id") == "br-cdc-art-11"
    )
    vetoed_unit = _chunk_for_unit("br-cdc-art-51-inciso-v", include_inactive=True)
    results = [
        [
            RetrievedChunk(chunk=vetoed_article, score=0.03),
            RetrievedChunk(chunk=vetoed_unit, score=0.03),
        ]
    ]

    grounds = select_legal_grounds(
        corpus, _facts(), results, [_trace(results[0])], support=_everything_supported
    )

    assert grounds == []


def test_failed_traces_do_not_count_as_query_support() -> None:
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    ok = _trace([RetrievedChunk(chunk=chunk, score=0.03)], query_index=0)
    failed = _trace([RetrievedChunk(chunk=chunk, score=0.03)], query_index=1, error="boom")

    assert chunk_query_occurrences([ok, failed]) == {chunk.chunk_id: 1}


def test_issue_label_defaults_to_a_generic_consumer_dispute() -> None:
    assert issue_label(ConsumerCaseFacts()) == "controvérsia de consumo"
    assert issue_label(_facts()) == "cobrança não reconhecida ou indevida"
