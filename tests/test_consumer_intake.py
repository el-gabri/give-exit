"""Tests for the supplier-neutral consumer intake."""

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


def test_chat_amount_is_not_promoted_to_confirmed_direct_loss() -> None:
    extraction = extract_explicit_facts(
        "A loja cobrou R$ 500,00, mas depois estornou integralmente.",
        ConsumerCaseFacts(),
    )

    assert extraction.direct_loss_amount is None
