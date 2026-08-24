from app.consumer.retrieval import (
    MAX_QUERY_CHARS,
    build_evidence_queries,
    build_legal_queries,
    infer_retrieval_category,
    is_consumer_scope,
)
from app.consumer.schemas import ConsumerCaseFacts, ConsumerIssueCategory


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
    assert is_consumer_scope(
        category="other",
        complaint="A loja não entregou o produto que comprei.",
    )
    assert is_consumer_scope(
        category="product_defect",
        complaint="O aparelho parou de funcionar.",
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


def test_evidence_queries_include_claim_and_requested_resolution() -> None:
    queries = build_evidence_queries(_facts())

    assert len(queries) == 2
    assert "cobrou duas vezes" in queries[0]
    assert "devolução" in queries[1]
