"""Consumer schema behavior and citation-boundary tests."""

import hashlib
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import (
    ConsumerCaseFacts,
    EvidenceCitation,
    LegalAuthorityCitation,
)


def test_consumer_facts_report_only_drafting_critical_gaps() -> None:
    empty = ConsumerCaseFacts()
    assert empty.missing_fields() == [
        "bank_name",
        "consumer_name",
        "complaint_summary",
        "incident_date_or_period",
        "desired_resolution",
    ]

    complete = ConsumerCaseFacts(
        consumer_name="Pessoa Consumidora",
        bank_name="Banco Exemplo",
        complaint_summary="Uma compra não reconhecida apareceu na fatura.",
        incident_date_or_period="julho de 2026",
        desired_resolution="Estorno e bloqueio da cobrança.",
    )
    assert complete.missing_fields() == []


def test_consumer_facts_keep_selected_evidence_value_reference() -> None:
    facts = ConsumerCaseFacts(
        direct_loss_amount=Decimal("200"),
        direct_loss_reference_id="a" * 24,
    )

    assert facts.direct_loss_amount == Decimal("200")
    assert facts.direct_loss_reference_id == "a" * 24

    with pytest.raises(ValidationError):
        ConsumerCaseFacts(direct_loss_reference_id="short")


def test_evidence_and_legal_authority_are_structurally_distinct() -> None:
    quote = "A fatura identifica a cobrança contestada."
    evidence = EvidenceCitation(
        evidence_id="evidence-1",
        filename="fatura.pdf",
        page=2,
        quote=quote,
        chunk_id="evidence-doc:0001",
        content_sha256=hashlib.sha256(quote.encode()).hexdigest(),
    )
    provision = get_default_legal_corpus().get("br-cdc-art-42")
    authority = LegalAuthorityCitation.from_provision(provision)

    assert evidence.quote == quote
    assert not hasattr(evidence, "official_url")
    assert authority.official_url.startswith("https://www.planalto.gov.br/")
    assert not hasattr(authority, "quote")


def test_tampered_legal_summary_hash_is_rejected() -> None:
    provision = get_default_legal_corpus().get("br-cdc-art-42")

    with pytest.raises(ValidationError, match="does not match summary"):
        provision.model_copy(
            update={"summary": "Resumo adulterado"},
        ).model_validate(provision.model_copy(update={"summary": "Resumo adulterado"}).model_dump())
