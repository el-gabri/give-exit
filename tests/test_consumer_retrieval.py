import asyncio
from typing import cast

from app.consumer.legal_policy import LEGAL_WINDOW_AGENT, strongly_supported_chunk_ids
from app.consumer.retrieval import (
    LEGAL_CORROBORATION_AGENT,
    LEGAL_RETRIEVAL_AGENT,
    MAX_QUERY_CHARS,
    build_corroboration_queries,
    build_evidence_queries,
    build_legal_queries,
    build_legal_query_plan,
    legal_query_windows,
    retrieve_legal_candidates,
    truncated_narrative_fields,
)
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.service import ConsumerCaseService
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.pipeline import RagPipeline
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace
from app.security.prompt_injection import PromptInjectionDetector


def _facts(**updates: object) -> ConsumerCaseFacts:
    payload: dict[str, object] = {
        "complaint_summary": "A empresa cobrou duas vezes a mesma compra.",
        "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
    }
    payload.update(updates)
    return ConsumerCaseFacts.model_validate(payload)


def test_legal_queries_are_two_and_driven_by_the_narrative() -> None:
    queries = build_legal_queries(_facts())

    assert len(queries) == 2
    assert all("cobrou duas vezes" in query for query in queries)
    assert "devolução" in queries[0]
    assert "devolução" in queries[1]


def test_no_query_is_a_constant_across_cases() -> None:
    """A case-independent query would feed every notice the same chunks."""
    first = build_legal_queries(_facts(complaint_summary="Cobrança em duplicidade."))
    second = build_legal_queries(_facts(complaint_summary="Produto nunca entregue."))

    assert not set(first) & set(second)


def test_retrieval_queries_are_bounded_and_whitespace_normalized() -> None:
    facts = _facts(complaint_summary=(" cobrança   repetida " * 400))

    queries = [*build_legal_queries(facts), *build_evidence_queries(facts)]

    assert queries
    assert all(len(query) <= MAX_QUERY_CHARS for query in queries)
    assert all("  " not in query for query in queries)


def test_a_long_complaint_keeps_the_remedy_as_its_own_query() -> None:
    """A long complaint can never push the remedy out of the search (ADR 0026)."""
    facts = _facts(
        complaint_summary=("relato muito longo " * 500),
        desired_resolution="Quero devolução integral comprovada.",
    )

    plan = build_legal_query_plan(facts)

    assert plan.ranking == ("Quero devolução integral comprovada.",)
    assert plan.windows
    assert build_legal_queries(facts) == [*plan.ranking, *plan.windows]


def _sentences(count: int, *, start: int = 0) -> str:
    return " ".join(
        f"Frase número {number} do relato sobre a compra feita na loja."
        for number in range(start, start + count)
    )


def test_a_complaint_that_fits_keeps_the_two_narrative_queries() -> None:
    fits = _facts(complaint_summary=("a" * 1_049) + ".")
    longer = _facts(complaint_summary=("a" * 1_050) + ".")

    assert build_legal_query_plan(fits).windows == ()
    assert len(build_legal_query_plan(fits).ranking) == 2
    assert build_legal_query_plan(longer).windows == (("a" * 1_050) + ".",)


def test_a_long_complaint_is_read_in_sentence_windows() -> None:
    late = "A loja cancelou o pedido e manteve o anúncio mais caro."
    complaint = f"{_sentences(20)} {late}"
    assert complaint.index(late) > 1_050

    windows = legal_query_windows(complaint)

    assert any(late in window for window in windows)
    assert all(len(window) <= 600 for window in windows)
    assert all(window.endswith(".") for window in windows)
    assert " ".join(windows) == complaint


def test_windows_keep_the_start_and_the_end_within_the_budget() -> None:
    complaint = _sentences(70)
    assert len(complaint) > 3_000  # well past the 2,400-character budget

    windows = legal_query_windows(complaint)

    assert sum(map(len, windows)) <= 2_400
    assert windows[0].startswith("Frase número 0 ")
    assert windows[-1].endswith("Frase número 69 do relato sobre a compra feita na loja.")
    # The kept windows are the start of the complaint and its last window.
    assert complaint.startswith(" ".join(windows[:-1]))
    assert complaint.endswith(windows[-1])


def test_one_overlong_sentence_is_one_bounded_window() -> None:
    complaint = "palavra " * 400 + "fim."

    windows = legal_query_windows(complaint)

    assert len(windows) == 1
    assert len(windows[0]) <= MAX_QUERY_CHARS


def test_a_repeated_paragraph_is_searched_once() -> None:
    """Two identical windows would corroborate each other with one formulation."""
    paragraph = _sentences(9)
    windows = legal_query_windows(f"{paragraph} {paragraph}")

    assert len(windows) == len(set(windows))



def test_evidence_queries_are_the_claim_and_the_requested_resolution() -> None:
    """Only the account's own words: framing like "Evidência que comprova" used to
    match every uploaded page lexically."""
    queries = build_evidence_queries(_facts())

    assert queries == [
        "A empresa cobrou duas vezes a mesma compra.",
        "Quero a devolução do valor pago em duplicidade.",
    ]


def test_evidence_queries_search_for_the_identifiers_documents_print() -> None:
    """A statement reads "PROTOCOLO-123 ... R$ 9.208,80", not the consumer's account."""
    facts = _facts(
        prior_protocols=["PROTOCOLO-123"],
        incident_date_or_period="julho de 2026",
        direct_loss_amount="9208.80",
        improper_payment_amount="9208.80",
    )

    assert build_evidence_queries(facts)[-1] == "PROTOCOLO-123 julho de 2026 9.208,80"


def test_corroboration_queries_are_independent_formulations() -> None:
    complaint, resolution = build_corroboration_queries(_facts())

    assert complaint not in resolution and resolution not in complaint
    assert build_corroboration_queries(_facts(desired_resolution=None)) == [complaint]


def _lexical_only_trace(query: str, index: int, chunk_ids: list[str]) -> RetrievalTrace:
    """A degraded trace whose top results are ``chunk_ids``, in rank order."""
    return RetrievalTrace(
        batch_id="batch",
        agent="consumer_legal_authorities",
        doc_id="legal-doc",
        query_index=index,
        query=query,
        query_sha256="0" * 64,
        requested_k=8,
        returned_count=len(chunk_ids),
        retrieval_mode="hybrid",
        degraded_mode="lexical_only",
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        score_type="bm25_score",
        results=[
            RetrievedItemTrace(
                rank=rank,
                chunk_id=chunk_id,
                doc_id="legal-doc",
                page_start=1,
                page_end=1,
                score=1.0 / rank,
                channel_ranks={"lexical": rank},
                content_sha256="0" * 64,
            )
            for rank, chunk_id in enumerate(chunk_ids, start=1)
        ],
    )


def test_a_long_complaints_windows_corroborate_each_other_but_not_themselves() -> None:
    """Without the dense channel, two windows are two independent formulations.

    Windows partition the complaint, so neither contains the other; one window
    ranking a chunk is still a single formulation.
    """
    facts = _facts(complaint_summary=_sentences(30))
    plan = build_legal_query_plan(facts)
    first, second = plan.windows[:2]
    (remedy,) = plan.ranking

    supported = strongly_supported_chunk_ids(
        [
            _lexical_only_trace(first, 0, ["only-one-window-ranks-it", "two-windows-rank-it"]),
            _lexical_only_trace(second, 1, ["two-windows-rank-it"]),
            _lexical_only_trace(remedy, 2, ["unrelated"]),
        ]
    )

    assert supported == {"two-windows-rank-it"}


def test_a_long_remedy_is_not_corroborated_by_the_narrative_that_repeats_it() -> None:
    """The narrative keeps the first 500 characters of the remedy; same rule."""
    facts = _facts(
        desired_resolution=("Quero o estorno integral e a correção do cadastro. " * 20)
    )
    assert len(facts.desired_resolution or "") > 900
    framed, bare = build_legal_queries(facts)
    complaint, remedy = build_corroboration_queries(facts)

    supported = strongly_supported_chunk_ids(
        [
            _lexical_only_trace(framed, 0, ["only-the-remedy-ranks-it", "both-rank-it"]),
            _lexical_only_trace(bare, 1, ["only-the-remedy-ranks-it", "both-rank-it"]),
            _lexical_only_trace(complaint, 2, ["both-rank-it"]),
            _lexical_only_trace(remedy, 3, ["only-the-remedy-ranks-it", "both-rank-it"]),
        ]
    )

    assert supported == {"both-rank-it"}


def test_evidence_queries_keep_the_account_beyond_the_narrative_budget() -> None:
    """Evidence lookups are not ranking queries: they search the whole account."""
    late_detail = "protocolo de atendimento registrado na ouvidoria"
    facts = _facts(complaint_summary=("relato do problema " * 70) + late_detail)
    assert (facts.complaint_summary or "").index(late_detail) > 1_050

    assert late_detail in build_evidence_queries(facts)[0]


def test_narrative_join_does_not_double_the_consumer_period() -> None:
    queries = build_legal_queries(_facts())

    assert all(".." not in query for query in queries)
    assert queries[1] == (
        "A empresa cobrou duas vezes a mesma compra. "
        "Quero a devolução do valor pago em duplicidade."
    )


class _TraceRecordingRag:
    """Records retrieval batches and returns degraded or healthy traces."""

    def __init__(self, *, degraded: bool) -> None:
        self.batches: list[tuple[str, list[str]]] = []
        self._degraded = degraded

    async def retrieve_many_with_traces(self, queries, *, doc_id, agent, k, mode):
        from app.schemas.trace import RetrievalTrace

        self.batches.append((agent, list(queries)))
        traces = [
            RetrievalTrace(
                batch_id=agent,
                agent=agent,
                doc_id=doc_id,
                query_index=index,
                query=query,
                query_sha256="0" * 64,
                requested_k=k,
                returned_count=0,
                retrieval_mode=mode,
                embedding_model="test",
                vector_store="memory",
                index_version="test",
                degraded_mode="lexical_only" if self._degraded else None,
            )
            for index, query in enumerate(queries)
        ]
        return [[] for _ in queries], traces


async def test_healthy_legal_retrieval_runs_only_the_ranking_queries() -> None:
    rag = _TraceRecordingRag(degraded=False)

    result_sets, traces = await retrieve_legal_candidates(
        cast(RagPipeline, rag), _facts(), doc_id="legal-doc", k=8
    )

    assert [agent for agent, _ in rag.batches] == [LEGAL_RETRIEVAL_AGENT]
    assert len(result_sets) == len(traces) == 2


async def test_degraded_legal_retrieval_adds_independent_corroboration() -> None:
    rag = _TraceRecordingRag(degraded=True)

    result_sets, traces = await retrieve_legal_candidates(
        cast(RagPipeline, rag), _facts(), doc_id="legal-doc", k=8
    )

    assert rag.batches[1] == (LEGAL_CORROBORATION_AGENT, build_corroboration_queries(_facts()))
    # Corroboration feeds the gate only; candidates still come from ranking.
    assert len(result_sets) == 2
    assert len(traces) == 4


async def test_degraded_retrieval_without_a_remedy_has_nothing_to_corroborate() -> None:
    rag = _TraceRecordingRag(degraded=True)

    _, traces = await retrieve_legal_candidates(
        cast(RagPipeline, rag), _facts(desired_resolution=None), doc_id="legal-doc", k=8
    )

    assert [agent for agent, _ in rag.batches] == [LEGAL_RETRIEVAL_AGENT]
    assert len(traces) == len(build_legal_queries(_facts(desired_resolution=None)))


async def test_long_complaint_retrieval_runs_windows_as_their_own_batch() -> None:
    rag = _TraceRecordingRag(degraded=False)
    facts = _facts(complaint_summary=_sentences(30))
    plan = build_legal_query_plan(facts)

    result_sets, traces = await retrieve_legal_candidates(
        cast(RagPipeline, rag), facts, doc_id="legal-doc", k=8
    )

    assert rag.batches == [
        (LEGAL_RETRIEVAL_AGENT, list(plan.ranking)),
        (LEGAL_WINDOW_AGENT, list(plan.windows)),
    ]
    assert len(result_sets) == len(traces) == 1 + len(plan.windows)


async def test_degraded_long_complaint_adds_no_corroboration_queries() -> None:
    """Its windows and remedy already are independent formulations."""
    rag = _TraceRecordingRag(degraded=True)
    facts = _facts(complaint_summary=_sentences(30))

    await retrieve_legal_candidates(cast(RagPipeline, rag), facts, doc_id="legal-doc", k=8)

    assert [agent for agent, _ in rag.batches] == [LEGAL_RETRIEVAL_AGENT, LEGAL_WINDOW_AGENT]


async def test_long_complaint_without_remedy_searches_windows_only() -> None:
    rag = _TraceRecordingRag(degraded=True)
    facts = _facts(complaint_summary=_sentences(30), desired_resolution=None)

    await retrieve_legal_candidates(cast(RagPipeline, rag), facts, doc_id="legal-doc", k=8)

    assert [agent for agent, _ in rag.batches] == [LEGAL_WINDOW_AGENT]


def test_truncated_fields_report_what_is_searched() -> None:
    within_budget = _sentences(30)
    beyond_budget = _sentences(70)
    kept = sum(map(len, legal_query_windows(beyond_budget)))
    overlong = "palavra " * 400 + "fim."

    assert truncated_narrative_fields(_facts()) == {}
    assert len(within_budget) > 1_050
    assert truncated_narrative_fields(_facts(complaint_summary=within_budget)) == {}
    assert truncated_narrative_fields(_facts(complaint_summary=beyond_budget)) == {
        "complaint_summary": (len(beyond_budget), kept)
    }
    assert truncated_narrative_fields(_facts(desired_resolution="b " * 300)) == {
        "desired_resolution": (599, 500)
    }
    assert truncated_narrative_fields(_facts(complaint_summary=overlong)) == {
        "complaint_summary": (len(overlong.strip()), len(legal_query_windows(overlong)[0]))
    }


async def test_notice_source_retrieval_does_not_compete_for_one_embedding_slot() -> None:
    class RetrievalProbe:
        def __init__(self) -> None:
            self.active_calls = 0
            self.max_active_calls = 0
            self.agents: list[str] = []

        async def retrieve_many_with_traces(self, queries, *, agent, **kwargs):
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            self.agents.append(agent)
            try:
                await asyncio.sleep(0.01)
                return [[] for _ in queries], []
            finally:
                self.active_calls -= 1

    probe = RetrievalProbe()
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=cast(RagPipeline, probe),
    )

    await service._retrieve_notice_support(
        facts=_facts(),
        evidence_doc_id="evidence-doc",
    )

    assert probe.max_active_calls == 1
    assert probe.agents == ["consumer_legal_authorities", "consumer_case_evidence"]
