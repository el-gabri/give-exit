import asyncio
from typing import cast

from app.consumer.retrieval import (
    LEGAL_CORROBORATION_AGENT,
    LEGAL_RETRIEVAL_AGENT,
    MAX_QUERY_CHARS,
    build_corroboration_queries,
    build_evidence_queries,
    build_legal_queries,
    is_consumer_scope,
    retrieve_legal_candidates,
)
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.service import ConsumerCaseService
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.pipeline import RagPipeline
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


def test_long_complaint_does_not_remove_resolution() -> None:
    facts = _facts(
        complaint_summary=("relato muito longo " * 500),
        desired_resolution="Quero devolução integral comprovada.",
    )

    queries = build_legal_queries(facts)

    assert all("devolução integral comprovada" in query for query in queries)


def test_scope_gate_abstains_only_on_strong_non_consumer_signals() -> None:
    assert not is_consumer_scope(
        complaint="Meu vizinho bloqueia a garagem.",
    )
    assert not is_consumer_scope(
        complaint="Meu empregador não pagou meu salário nem o vale-transporte.",
    )
    assert not is_consumer_scope(
        complaint=(
            "A empresa onde trabalho está há dois meses sem pagar meu salário e "
            "também não depositou o vale-transporte combinado."
        ),
    )
    assert not is_consumer_scope(
        complaint=(
            "A empresa não fez o pagamento do meu salário previsto no contrato "
            "de trabalho."
        ),
    )
    assert not is_consumer_scope(
        complaint="Meu empregador nao pagou meu salario na loja em que trabalho.",
    )
    assert not is_consumer_scope(
        complaint="Meu empregador não liberou o seguro-desemprego nem pagou meu salário.",
    )
    assert not is_consumer_scope(
        complaint="O empregador alterou meu cartão de ponto e não pagou as horas extras.",
    )
    assert not is_consumer_scope(
        complaint="Meu empregador não fez a entrega do EPI exigido para o trabalho.",
    )
    assert is_consumer_scope(
        complaint="A loja não entregou o produto que comprei.",
    )
    assert is_consumer_scope(
        complaint="O aparelho parou de funcionar.",
    )
    assert is_consumer_scope(
        complaint=(
            "A loja onde trabalho fez uma cobrança no meu cartão por uma compra "
            "que eu não fiz."
        ),
    )
    assert not is_consumer_scope(
        complaint=(
            "Meu empregador fez uma cobrança indevida no contracheque e não pagou "
            "meu salário."
        ),
    )
    assert not is_consumer_scope(
        complaint="Comprei o uniforme obrigatório e meu empregador não me reembolsou.",
    )
    assert is_consumer_scope(
        complaint="O banco bloqueou minha conta-salário e não libera meu salário.",
    )
    assert not is_consumer_scope(
        complaint=(
            "Meu empregador fez uma cobrança sobre o banco de horas e não pagou "
            "meu salário."
        ),
    )
    assert not is_consumer_scope(
        complaint=(
            "O banco fica perto do meu trabalho. Meu empregador bloqueou minha conta "
            "no sistema de ponto e não pagou meu salário."
        ),
    )
    assert is_consumer_scope(
        complaint=(
            "Meu empregador não pagou meu salário. A operadora cancelou meu serviço "
            "de internet."
        ),
    )
    assert not is_consumer_scope(
        complaint=(
            "Comprei o uniforme obrigatório na loja em que trabalho, mas meu "
            "empregador não me reembolsou o salário."
        ),
    )
    assert not is_consumer_scope(
        complaint="A companhia não depositou meu sala\u0301rio.",
    )


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


def test_scope_gate_decides_from_the_narrative_alone() -> None:
    """No category exists any more; the narrative is the only input."""
    assert not is_consumer_scope(
        complaint="Meu empregador não pagou meu salário nem registrou a hora extra.",
    )
    assert not is_consumer_scope(
        complaint="Meu empregador descontou o vale-transporte do meu salário.",
    )
    assert not is_consumer_scope(complaint="Meu vizinho construiu um muro no meu terreno.")
    assert is_consumer_scope(
        complaint="A operadora cobrou pelo serviço de internet que nunca funcionou.",
    )
    assert is_consumer_scope(
        complaint="O banco bloqueou minha conta bancária e não libera meu saldo.",
    )


