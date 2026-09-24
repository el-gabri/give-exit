"""The pure ground selector shared by the notice service and the evaluator."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from app.consumer.ground_selection import (
    chunk_query_occurrences,
    select_legal_grounds,
)
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.service import ConsumerCaseService
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace


def _facts(
    complaint: str = "A empresa cobrou duas vezes a mesma compra.",
) -> ConsumerCaseFacts:
    return ConsumerCaseFacts.model_validate(
        {
            "complaint_summary": complaint,
            "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
        }
    )


def _trace(
    results: list[RetrievedChunk],
    *,
    query_index: int = 0,
    error: str | None = None,
    channels: tuple[str, ...] = ("dense", "lexical"),
    query: str | None = None,
    retrieval_mode: str = "hybrid",
    degraded_mode: str | None = None,
) -> RetrievalTrace:
    query = query or f"consulta {query_index}"
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
        retrieval_mode=retrieval_mode,
        degraded_mode=degraded_mode,
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
                channel_ranks={channel: rank for channel in channels},
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
    traces = [_trace(results[0], channels=("dense",))]

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


def test_ground_rationale_does_not_name_an_issue_category() -> None:
    corpus = get_default_legal_corpus()
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.03)]]
    grounds = select_legal_grounds(corpus, _facts(), results, [_trace(results[0])])

    assert grounds
    for ground in grounds:
        assert "a partir do relato do consumidor" in ground.application_to_facts
        assert "cobrança não reconhecida" not in ground.application_to_facts


@lru_cache(maxsize=1)
def _mixed_corpus() -> LegalCorpus:
    return get_default_legal_corpus()


def _mixed_chunk(unit_id: str) -> Chunk:
    return next(
        chunk for chunk in _mixed_corpus().as_chunks() if chunk.metadata["unit_id"] == unit_id
    )


def test_complementary_grounds_are_capped_and_keep_cdc_in_the_window() -> None:
    complementary = [
        _mixed_chunk(unit_id)
        for unit_id in (
            "br-cc-art-876-caput",
            "br-cc-art-884-caput",
            "br-cc-art-927-caput",
            "br-cc-art-944-caput",
            "br-lgpd-art-42-caput",
        )
    ]
    ranked = [*complementary, _mixed_chunk("br-cdc-art-42-paragrafo-unico")]
    results = [
        [
            RetrievedChunk(chunk=chunk, score=0.03 - index * 0.0001)
            for index, chunk in enumerate(ranked)
        ]
    ]

    grounds = select_legal_grounds(
        _mixed_corpus(), _facts(), results, [_trace(results[0])], support=_everything_supported
    )

    law_ids = [ground.authority.law_id for ground in grounds]
    assert law_ids.count("br-cdc") == 1
    assert sum(law_id in {"br-cc", "br-lgpd"} for law_id in law_ids) == 3


def test_without_a_cdc_ground_complementary_sources_are_dropped() -> None:
    results = [[RetrievedChunk(chunk=_mixed_chunk("br-cc-art-876-caput"), score=0.03)]]

    grounds = select_legal_grounds(
        _mixed_corpus(), _facts(), results, [_trace(results[0])], support=_everything_supported
    )

    assert grounds == []


def test_degraded_retrieval_cites_no_complementary_source() -> None:
    results = [
        [
            RetrievedChunk(chunk=_mixed_chunk("br-cc-art-876-caput"), score=0.03),
            RetrievedChunk(chunk=_mixed_chunk("br-cdc-art-42-paragrafo-unico"), score=0.029),
        ]
    ]
    degraded = _trace(results[0]).model_copy(update={"degraded_mode": "lexical_only"})

    grounds = select_legal_grounds(
        _mixed_corpus(), _facts(), results, [degraded], support=_everything_supported
    )

    assert [ground.authority.law_id for ground in grounds] == ["br-cdc"]


def test_channel_agreement_holds_for_any_fusion_weights() -> None:
    """A hit at the gate depth in both channels is supported with a light lexical weight.

    The former score-inferred gate compared the fused score with the best
    single-channel score, so 1/73 + 0.1/73 fell below 1/61 and a genuine
    two-channel hit was discarded.
    """
    from app.consumer.legal_policy import AGREEMENT_MAX_RANK, strongly_supported_chunk_ids

    depth = AGREEMENT_MAX_RANK
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    trace = _trace([RetrievedChunk(chunk=chunk, score=1.1 / (60 + depth))]).model_copy(
        update={"lexical_weight": 0.1}
    )
    item = trace.results[0].model_copy(
        update={"channel_ranks": {"dense": depth, "lexical": depth}}
    )
    trace = trace.model_copy(update={"results": [item]})

    assert strongly_supported_chunk_ids([trace]) == {chunk.chunk_id}


def test_one_channel_hits_in_healthy_hybrid_mode_need_both_channels() -> None:
    from app.consumer.legal_policy import strongly_supported_chunk_ids

    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    traces = [
        _trace([RetrievedChunk(chunk=chunk, score=1 / 61)], query_index=index,
               query=query, channels=("dense",))
        for index, query in enumerate(("cobrou duas vezes", "quero o dinheiro de volta"))
    ]

    # Two independent queries do not stand in for the missing lexical channel.
    assert strongly_supported_chunk_ids(traces) == frozenset()


def test_degraded_corroboration_needs_two_independent_queries() -> None:
    from app.consumer.legal_policy import strongly_supported_chunk_ids

    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    narrative = "A loja cobrou duas vezes. Quero o dinheiro de volta."

    def degraded(query: str, index: int) -> RetrievalTrace:
        return _trace(
            [RetrievedChunk(chunk=chunk, score=8.0)],
            query_index=index,
            query=query,
            channels=("lexical",),
            degraded_mode="lexical_only",
        )

    nested = [degraded(narrative, 0), degraded("A loja cobrou duas vezes", 1)]
    independent = [*nested, degraded("Quero o dinheiro de volta", 2)]

    # The narrative contains its clause, so the pair is one signal, not two.
    assert strongly_supported_chunk_ids(nested) == frozenset()
    assert strongly_supported_chunk_ids(independent) == {chunk.chunk_id}


def test_dense_only_traces_use_independent_query_corroboration() -> None:
    from app.consumer.legal_policy import strongly_supported_chunk_ids

    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    traces = [
        _trace(
            [RetrievedChunk(chunk=chunk, score=0.9)],
            query_index=index,
            query=query,
            channels=("dense",),
            retrieval_mode="dense",
        )
        for index, query in enumerate(("A loja cobrou duas vezes", "Quero o dinheiro de volta"))
    ]

    assert strongly_supported_chunk_ids(traces) == {chunk.chunk_id}


def test_corroboration_ignores_hits_below_the_top_three() -> None:
    from app.consumer.legal_policy import strongly_supported_chunk_ids

    target = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    fillers = [
        _chunk_for_unit(unit_id)
        for unit_id in (
            "br-cdc-art-43-paragrafo-2",
            "br-cdc-art-43-paragrafo-4",
            "br-cdc-art-6-inciso-iii",
        )
    ]
    traces = [
        _trace(
            [*(RetrievedChunk(chunk=chunk, score=9.0) for chunk in fillers),
             RetrievedChunk(chunk=target, score=1.0)],
            query_index=index,
            query=query,
            channels=("lexical",),
            degraded_mode="lexical_only",
        )
        for index, query in enumerate(("A loja cobrou duas vezes", "Quero o dinheiro de volta"))
    ]

    assert target.chunk_id not in strongly_supported_chunk_ids(traces)


def test_reranked_scores_keep_the_relative_floor() -> None:
    corpus = get_default_legal_corpus()
    strong = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    weak = _chunk_for_unit("br-cdc-art-43-paragrafo-2")
    results = [
        [RetrievedChunk(chunk=strong, score=0.9), RetrievedChunk(chunk=weak, score=0.2)]
    ]
    trace = _trace(results[0]).model_copy(update={"score_type": "reranker_score"})

    grounds = select_legal_grounds(corpus, _facts(), results, [trace])

    assert [ground.authority.unit_id for ground in grounds] == [
        "br-cdc-art-42-paragrafo-unico"
    ]


def test_agreement_must_fall_within_the_depth_of_both_channels() -> None:
    from app.consumer.legal_policy import AGREEMENT_MAX_RANK, strongly_supported_chunk_ids

    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    trace = _trace([RetrievedChunk(chunk=chunk, score=0.02)])

    def with_ranks(dense: int, lexical: int) -> RetrievalTrace:
        item = trace.results[0].model_copy(
            update={"channel_ranks": {"dense": dense, "lexical": lexical}}
        )
        return trace.model_copy(update={"results": [item]})

    assert strongly_supported_chunk_ids([with_ranks(AGREEMENT_MAX_RANK, 1)]) == {
        chunk.chunk_id
    }
    assert strongly_supported_chunk_ids([with_ranks(1, AGREEMENT_MAX_RANK + 1)]) == frozenset()
