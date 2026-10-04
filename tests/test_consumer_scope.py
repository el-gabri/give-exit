"""The relationship-class scope gate (spec 2026-10-04, ADR 0024)."""

from pathlib import Path

import pytest

from app.consumer.scope import NonConsumerRelationship, assess_scope, is_consumer_scope
from app.evaluation.consumer_golden import load_consumer_legal_dataset

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")
R = NonConsumerRelationship


@pytest.mark.parametrize(
    ("complaint", "relationship", "signal"),
    [
        (
            "A Receita Federal reteve meu imposto e não explica o motivo.",
            R.STATE,
            "imposto",
        ),
        ("Moro de aluguel e o vazamento continua.", R.TENANCY, "moro de aluguel"),
        ("Meu patrão não depositou o FGTS.", R.EMPLOYMENT, "fgts"),
        ("Meu ex-marido não paga a pensão alimentícia.", R.FAMILY, "pensao alimenticia"),
        (
            "O outro motorista fugiu sem pagar o conserto.",
            R.PRIVATE_PARTIES,
            "o outro motorista",
        ),
        ("Meu cliente não pagou a reforma que fiz.", R.COMPLAINANT_SUPPLIER, "meu cliente"),
    ],
)
def test_each_relationship_class_makes_the_gate_abstain(
    complaint: str, relationship: NonConsumerRelationship, signal: str
) -> None:
    assessment = assess_scope(complaint=complaint)

    assert not assessment.in_scope
    assert assessment.relationship is relationship
    assert assessment.signal == signal
    assert is_consumer_scope(complaint=complaint) is False


def test_a_consumer_clause_overrides_a_class_signal() -> None:
    assessment = assess_scope(
        complaint="Comprei um sofá numa loja e a entrega no meu condomínio atrasou."
    )

    assert assessment.in_scope
    assert assessment.relationship is None and assessment.signal is None


def test_a_complaint_without_signals_stays_in_scope() -> None:
    assert assess_scope(complaint="O aparelho parou de funcionar.").in_scope


def test_concessionaires_stay_in_scope() -> None:
    assert is_consumer_scope(
        complaint="A distribuidora de energia trocou meu medidor e a conta triplicou."
    )


def test_a_car_rental_is_not_tenancy() -> None:
    assert is_consumer_scope(
        complaint="Aluguei um carro na locadora e cobraram uma diária a mais."
    )


def test_common_consumer_phrases_are_not_class_signals() -> None:
    assert is_consumer_scope(complaint="A operadora cortou minha internet sem aviso prévio.")
    assert is_consumer_scope(
        complaint="A operadora marcou três visitas técnicas e ninguém apareceu."
    )


def test_a_company_in_a_complainant_supplier_clause_is_the_customer() -> None:
    # "uma empresa que me contratou" names the complainant's customer: the
    # complainant is the professional side, so the override must not fire.
    assert not is_consumer_scope(
        complaint="Faço reformas e uma empresa que me contratou não pagou o serviço."
    )


def test_buying_from_an_individual_is_a_private_sale() -> None:
    assert not is_consumer_scope(complaint="Comprei uma bicicleta de uma pessoa e ela quebrou.")


def test_labour_keeps_its_card_charge_exception() -> None:
    # A worker's consumer dispute with the employer's store still needs a
    # personal card or invoice charge, as before the move.
    assert not is_consumer_scope(
        complaint="Comprei o uniforme na loja do meu empregador e não fui reembolsado."
    )


def test_no_labelled_in_scope_case_abstains() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    lost = [
        case.case_id
        for case in dataset.cases
        if case.relevant and not is_consumer_scope(complaint=case.complaint)
    ]

    assert lost == []


def test_development_out_of_scope_cases_meet_the_bar() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    new_ids = {case.case_id for case in dataset.cases[43:]}
    out = [c for c in dataset.cases if c.split == "development" and c.no_applicable_ground]
    new_out = [c for c in out if c.case_id in new_ids]
    old_out = [c for c in out if c.case_id not in new_ids]

    assert len(new_out) == 20 and len(old_out) == 4
    assert all(not is_consumer_scope(complaint=c.complaint) for c in old_out)
    abstained = sum(not is_consumer_scope(complaint=c.complaint) for c in new_out)
    # 16 of 20: the four that stay in scope name no class signal by design
    # (proprietaria_quer_que_eu_saia, casa_que_o_pai_deixou,
    # cliente_da_costureira_nao_pagou) or have no class at all
    # (bicicleta_furtada). The optional LLM scope check is their backstop.
    assert abstained == 16
