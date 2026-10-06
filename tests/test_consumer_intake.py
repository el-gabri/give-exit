"""Tests for the supplier-neutral consumer intake."""

import pytest

from app.consumer.intake import (
    extract_explicit_facts,
    next_assistant_message,
    recommended_documents,
)
from app.consumer.schemas import ConsumerCaseFacts


def test_extracts_non_bank_supplier() -> None:
    extraction = extract_explicit_facts(
        "A empresa Mercado Livre não entregou o produto em julho de 2026.",
        ConsumerCaseFacts(),
    )

    assert extraction.bank_name == "Mercado Livre"


def test_extracts_lowercase_multiword_supplier_without_trailing_sentence() -> None:
    cases = (
        (
            "A empresa mercado livre não entregou o produto.",
            "Mercado Livre",
        ),
        (
            "Comprei na loja casas bahia, que cancelou o pedido.",
            "Casas Bahia",
        ),
        (
            "A plataforma mercado pago realizou uma cobrança.",
            "Mercado Pago",
        ),
    )

    for message, expected in cases:
        extraction = extract_explicit_facts(message, ConsumerCaseFacts())

        assert extraction.bank_name == expected


def test_common_verbs_are_not_extracted_as_supplier_names() -> None:
    for message in (
        "A empresa me cobrou indevidamente.",
        "A loja cancelou o pedido sem explicação.",
    ):
        extraction = extract_explicit_facts(message, ConsumerCaseFacts())

        assert extraction.bank_name is None


def test_next_question_uses_supplier_neutral_language() -> None:
    message = next_assistant_message(ConsumerCaseFacts(), has_evidence=False)

    assert "empresa" in message.casefold()
    assert "banco" not in message.casefold()


def test_recommended_documents_are_one_generic_list() -> None:
    documents = recommended_documents()

    assert documents
    assert all("banco" not in document.casefold() for document in documents)


def test_intake_never_asks_for_a_problem_type() -> None:
    message = next_assistant_message(ConsumerCaseFacts(), has_evidence=False)

    assert "tipo de problema" not in message.casefold()


@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("A cobrança veio no dia 2 de Outubro.", "2 de Outubro"),
        ("A cobrança veio em 2 de outubro de 2026.", "2 de outubro de 2026"),
        ("O Nubank debitou R$ 100,00 em julho de 2026.", "julho de 2026"),
        ("A fatura de 10/03/2026 veio errada.", "10/03/2026"),
        ("Comprei uma passagem da Viação 1001 e não viajei.", None),
        # The first real prompt-notice test: "1001" names the bus company.
        (
            "Fui vítima de uma compra não reconhecida no valor de R$ 149,99. Uma viagem de "
            "São Paulo para o Rio de Janeiro no dia 2 de Outubro, pela viação 1001. "
            "Eu jamais fiz esta compra.",
            "2 de Outubro",
        ),
    ],
)
def test_extracts_the_incident_date_but_not_a_bare_number(text: str, period: str | None) -> None:
    extraction = extract_explicit_facts(text, ConsumerCaseFacts())

    assert extraction.incident_date_or_period == period


def test_chat_amount_is_not_promoted_to_confirmed_direct_loss() -> None:
    extraction = extract_explicit_facts(
        "A loja cobrou R$ 500,00, mas depois estornou integralmente.",
        ConsumerCaseFacts(),
    )

    assert extraction.direct_loss_amount is None


def test_request_sentences_become_the_resolution_and_leave_the_account() -> None:
    """A notice drafted without review must not repeat the whole message as a request."""
    extraction = extract_explicit_facts(
        "O Nubank debitou R$ 100,00 em julho de 2026 sem autorização. "
        "Quero o estorno imediato da cobrança.",
        ConsumerCaseFacts(),
    )

    assert extraction.complaint_summary == (
        "O Nubank debitou R$ 100,00 em julho de 2026 sem autorização."
    )
    assert extraction.desired_resolution == "Quero o estorno imediato da cobrança."


def test_a_message_that_only_asks_keeps_it_as_the_account_too() -> None:
    extraction = extract_explicit_facts("Quero meu dinheiro de volta.", ConsumerCaseFacts())

    assert extraction.complaint_summary == "Quero meu dinheiro de volta."
    assert extraction.desired_resolution == "Quero meu dinheiro de volta."
