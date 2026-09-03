import asyncio
from typing import cast

from app.consumer.retrieval import (
    MAX_QUERY_CHARS,
    build_evidence_queries,
    build_legal_queries,
    infer_retrieval_category,
    is_consumer_scope,
)
from app.consumer.schemas import ConsumerCaseFacts, ConsumerIssueCategory
from app.consumer.service import ConsumerCaseService
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.pipeline import RagPipeline
from app.security.prompt_injection import PromptInjectionDetector


def _facts(**updates: object) -> ConsumerCaseFacts:
    payload: dict[str, object] = {
        "issue_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
        "complaint_summary": "A empresa cobrou duas vezes a mesma compra.",
        "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
    }
    payload.update(updates)
    return ConsumerCaseFacts.model_validate(payload)


def test_legal_queries_are_driven_by_consumer_narrative() -> None:
    queries = build_legal_queries(_facts())

    assert len(queries) == 3
    assert all("cobrou duas vezes" in query for query in queries[:2])
    assert "cobrou duas vezes" not in queries[2]
    assert any("artigo 42" in query for query in queries)
    assert any("devolução" in query for query in queries)


def test_retrieval_queries_are_bounded_and_whitespace_normalized() -> None:
    facts = _facts(complaint_summary=(" cobrança   repetida " * 400))

    queries = [*build_legal_queries(facts), *build_evidence_queries(facts)]

    assert queries
    assert all(len(query) <= MAX_QUERY_CHARS for query in queries)
    assert all("  " not in query for query in queries)


def test_long_complaint_does_not_remove_resolution_or_category_expansion() -> None:
    facts = _facts(
        complaint_summary=("relato muito longo " * 500),
        desired_resolution="Quero devolução integral comprovada.",
    )

    queries = build_legal_queries(facts)

    assert all("devolução integral comprovada" in query for query in queries[:2])
    assert "artigo 42" in queries[1]
    assert "artigo 42" in queries[2]


def test_scope_gate_abstains_only_on_strong_non_consumer_signals() -> None:
    assert not is_consumer_scope(
        category="no_consumer_relationship",
        complaint="Meu vizinho bloqueia a garagem.",
    )
    assert not is_consumer_scope(
        category="other",
        complaint="Meu empregador não pagou meu salário nem o vale-transporte.",
    )
    assert not is_consumer_scope(
        category="other",
        complaint=(
            "A empresa onde trabalho está há dois meses sem pagar meu salário e "
            "também não depositou o vale-transporte combinado."
        ),
    )
    assert not is_consumer_scope(
        category="unauthorized_charge",
        complaint=(
            "A empresa não fez o pagamento do meu salário previsto no contrato "
            "de trabalho."
        ),
    )
    assert not is_consumer_scope(
        category="other",
        complaint="Meu empregador nao pagou meu salario na loja em que trabalho.",
    )
    assert not is_consumer_scope(
        category="other",
        complaint="Meu empregador não liberou o seguro-desemprego nem pagou meu salário.",
    )
    assert not is_consumer_scope(
        category="other",
        complaint="O empregador alterou meu cartão de ponto e não pagou as horas extras.",
    )
    assert not is_consumer_scope(
        category="other",
        complaint="Meu empregador não fez a entrega do EPI exigido para o trabalho.",
    )
    assert is_consumer_scope(
        category="other",
        complaint="A loja não entregou o produto que comprei.",
    )
    assert is_consumer_scope(
        category="product_defect",
        complaint="O aparelho parou de funcionar.",
    )
    assert is_consumer_scope(
        category="other",
        complaint=(
            "A loja onde trabalho fez uma cobrança no meu cartão por uma compra "
            "que eu não fiz."
        ),
    )


def test_lay_intake_category_is_refined_for_legal_retrieval() -> None:
    facts = _facts(
        issue_category=ConsumerIssueCategory.SERVICE_FAILURE,
        complaint_summary="Comprei pela internet e desisti da compra em sete dias.",
    )

    assert infer_retrieval_category("service_failure", facts.complaint_summary or "") == (
        "right_of_withdrawal"
    )
    assert "artigo 49" in build_legal_queries(facts)[1]


def test_retrieval_category_uses_confirmed_real_world_phrasing() -> None:
    cases = (
        (
            ConsumerIssueCategory.SERVICE_FAILURE,
            "A geladeira nova parou de gelar depois de dez dias.",
            "Quero o dinheiro de volta.",
            "substituição restituição abatimento",
        ),
        (
            ConsumerIssueCategory.SERVICE_FAILURE,
            "A loja cancelou a compra alegando falta de estoque.",
            "Quero receber o produto anunciado.",
            "entrega forçada",
        ),
        (
            ConsumerIssueCategory.OTHER,
            "Recebi o tênis há cinco dias, mas não gostei do modelo.",
            "Quero desistir da compra.",
            "direito de arrependimento",
        ),
        (
            ConsumerIssueCategory.OTHER,
            "A propaganda prometia acesso ilimitado, mas ele dura três meses.",
            "Quero cancelar sem multa.",
            "publicidade enganosa",
        ),
        (
            ConsumerIssueCategory.LOAN_OR_INTEREST,
            "A loja só aprovaria o financiamento se eu contratasse o seguro.",
            "Quero retirar o seguro.",
            "vantagem manifestamente excessiva",
        ),
        (
            ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
            "A empresa de cobrança liga para colegas e ameaça me expor.",
            "Quero que parem as ameaças.",
            "ameaça constrangimento",
        ),
        (
            ConsumerIssueCategory.OTHER,
            "A cláusula que impede cancelamento estava em letras minúsculas.",
            "Quero cancelar sem a multa escondida.",
            "contrato de adesão",
        ),
    )

    for intake_category, complaint, resolution, expected_anchor in cases:
        queries = build_legal_queries(
            _facts(
                issue_category=intake_category,
                complaint_summary=complaint,
                desired_resolution=resolution,
            )
        )
        assert expected_anchor in queries[-1]


def test_evidence_queries_include_claim_and_requested_resolution() -> None:
    queries = build_evidence_queries(_facts())

    assert len(queries) == 2
    assert "cobrou duas vezes" in queries[0]
    assert "devolução" in queries[1]


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
        legal_queries=["consulta jurídica"],
        evidence_queries=["consulta documental"],
        evidence_doc_id="evidence-doc",
    )

    assert probe.max_active_calls == 1
    assert probe.agents == ["consumer_legal_authorities", "consumer_case_evidence"]


def test_concrete_category_cannot_bypass_the_consumer_scope_gate() -> None:
    """The narrative decides scope, not the inferred category.

    The intake taxonomy is keyword-inferred from the same free text, so a
    labour dispute easily lands in a concrete consumer category. Letting the
    category short-circuit the check made this gate unreachable for every value
    except "other".
    """
    assert not is_consumer_scope(
        category="service_failure",
        complaint="Meu empregador não pagou meu salário nem registrou a hora extra.",
    )
    assert not is_consumer_scope(
        category="unauthorized_charge",
        complaint="Meu empregador descontou o vale-transporte do meu salário.",
    )
    # A genuine consumer narrative in the same category still passes.
    assert is_consumer_scope(
        category="service_failure",
        complaint="A operadora cobrou pelo serviço de internet que nunca funcionou.",
    )
