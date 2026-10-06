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


@pytest.mark.parametrize(
    "complaint",
    [
        "Aluguei um carro na locadora e cobraram uma diária a mais.",
        "Aluguei um carro na locadora. No contrato de locação havia uma franquia abusiva.",
        "Aluguei um carro e a locadora cobrou avarias que já existiam. Como locatário, "
        "pedi as fotos da vistoria e negaram.",
    ],
)
def test_a_car_rental_is_not_tenancy(complaint: str) -> None:
    assert is_consumer_scope(complaint=complaint)


def test_common_consumer_phrases_are_not_class_signals() -> None:
    assert is_consumer_scope(complaint="A operadora cortou minha internet sem aviso prévio.")
    assert is_consumer_scope(
        complaint="A operadora marcou três visitas técnicas e ninguém apareceu."
    )


@pytest.mark.parametrize(
    "complaint",
    [
        "A operadora fez a suspensão da minha linha sem aviso.",
        "Comprei um carro zero e a suspensão quebrou.",
        "O aplicativo fez o compartilhamento dos meus dados pessoais sem meu consentimento.",
        "Tenho plano pelo sindicato. Negaram minha cirurgia.",
    ],
)
def test_a_class_signal_inside_a_longer_word_is_not_a_signal(complaint: str) -> None:
    # "suspensão" holds "pensão", "compartilhamento" holds "partilha" and
    # "sindicato" holds "síndica": signals match whole words only.
    assert is_consumer_scope(complaint=complaint)


def test_a_class_signal_still_matches_its_plural() -> None:
    assessment = assess_scope(complaint="Tenho dois inquilinos que não pagam.")

    assert not assessment.in_scope
    assert assessment.relationship is R.TENANCY


@pytest.mark.parametrize(
    "complaint",
    [
        "Recebi uma mensagem de uma pessoa que dizia ser do banco e acabei fazendo um Pix. "
        "O banco se recusa a devolver.",
        "Um amigo me indicou a academia. A academia cobrou a matrícula duas vezes.",
        "O manobrista do estacionamento bateu no meu carro e ninguém quer pagar o conserto.",
        "A companhia aérea extraviou minha mala e eu estava indo ver minha filha.",
        "Sou autônomo e contratei um plano de saúde da operadora que negou a cirurgia.",
        "O hospital particular cobrou a internação que o plano já tinha pago. "
        "Fui demitido no mês passado.",
    ],
)
def test_a_person_or_event_mentioned_in_a_consumer_complaint_keeps_it_in_scope(
    complaint: str,
) -> None:
    assert is_consumer_scope(complaint=complaint)


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


# Moved from the retrieval tests: the labour, traffic-fine, private-loan and
# neighbour complaints the first gate abstained on, with its card-charge and
# bank-account exceptions. The narrative is the gate's only input.
@pytest.mark.parametrize(
    "complaint",
    [
        "Meu vizinho bloqueia a garagem.",
        "Meu vizinho construiu um muro no meu terreno.",
        "Meu empregador não pagou meu salário nem o vale-transporte.",
        "Meu empregador não pagou meu salário nem registrou a hora extra.",
        "Meu empregador descontou o vale-transporte do meu salário.",
        "A empresa onde trabalho está há dois meses sem pagar meu salário e também não "
        "depositou o vale-transporte combinado.",
        "A empresa não fez o pagamento do meu salário previsto no contrato de trabalho.",
        "Meu empregador nao pagou meu salario na loja em que trabalho.",
        "Meu empregador não liberou o seguro-desemprego nem pagou meu salário.",
        "O empregador alterou meu cartão de ponto e não pagou as horas extras.",
        "Meu empregador não fez a entrega do EPI exigido para o trabalho.",
        "Meu empregador fez uma cobrança indevida no contracheque e não pagou meu salário.",
        "Meu empregador fez uma cobrança sobre o banco de horas e não pagou meu salário.",
        "O banco fica perto do meu trabalho. Meu empregador bloqueou minha conta no sistema "
        "de ponto e não pagou meu salário.",
        "Comprei o uniforme obrigatório e meu empregador não me reembolsou.",
        "Comprei o uniforme obrigatório na loja em que trabalho, mas meu empregador não me "
        "reembolsou o salário.",
        "A companhia não depositou meu sala\u0301rio.",
        "Recebi uma multa de trânsito por excesso de velocidade em uma cidade onde eu nunca "
        "estive com o meu carro.",
        "Emprestei dinheiro para um amigo e ele não me devolve.",
    ],
)
def test_earlier_non_consumer_complaints_still_abstain(complaint: str) -> None:
    assert not is_consumer_scope(complaint=complaint)


@pytest.mark.parametrize(
    "complaint",
    [
        # A contractual penalty is not a traffic fine, and lending a card to a
        # relative does not remove the bank from the dispute.
        "A academia quer cobrar multa de cancelamento do plano anual.",
        "Emprestei meu cartão para minha mãe e o banco cobrou duas vezes a mesma compra "
        "na fatura.",
        "A loja não entregou o produto que comprei.",
        "A operadora cobrou pelo serviço de internet que nunca funcionou.",
        "A loja onde trabalho fez uma cobrança no meu cartão por uma compra que eu não fiz.",
        "O banco bloqueou minha conta-salário e não libera meu salário.",
        "O banco bloqueou minha conta bancária e não libera meu saldo.",
        "Meu empregador não pagou meu salário. A operadora cancelou meu serviço de internet.",
    ],
)
def test_earlier_consumer_complaints_stay_in_scope(complaint: str) -> None:
    assert is_consumer_scope(complaint=complaint)


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
