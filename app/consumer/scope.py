"""Whether a complaint may be answered with consumer law (spec 2026-10-04, ADR 0024).

The gate recognises relationships the law treats as non-consumer: the State as
authority, landlord and tenant or condominium, employment, family and
succession, two private individuals, and a complainant who is the professional
side. A complaint that names one of them abstains unless a clause also shows a
consumer relationship. Everything else stays in scope: a missing word must
never cost a consumer the notice. The optional LLM scope check
(``scope_verifier``) is the backstop for relationships no signal names.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

from pydantic import BaseModel, ConfigDict


class NonConsumerRelationship(str, Enum):
    STATE = "state"
    TENANCY = "tenancy"
    EMPLOYMENT = "employment"
    FAMILY = "family"
    PRIVATE_PARTIES = "private_parties"
    COMPLAINANT_SUPPLIER = "complainant_supplier"


class ScopeAssessment(BaseModel):
    """The deterministic decision and, when it abstains, the class and phrase why."""

    model_config = ConfigDict(frozen=True)

    in_scope: bool
    relationship: NonConsumerRelationship | None = None
    signal: str | None = None


# The complainant's occupation alone does not make a company named beside it
# their customer: a self-employed person still buys a health plan as a consumer.
_SELF_EMPLOYED_SIGNALS = ("sou autônomo", "sou autônoma")


# Signals are written from what defines each relationship in law, not from
# evaluation cases, and adjusted only on development evidence (ledgered).
# They match whole words, so "suspensão" is not "pensão". "aluguel",
# "locação" and "locatário" alone are not tenancy (car rentals are consumer
# contracts), "aviso prévio" / "visita" are not employment or family, and a
# friend or a child merely mentioned is not a relationship (consumer
# complaints use all of them every day).
_RELATIONSHIP_SIGNALS: dict[NonConsumerRelationship, tuple[str, ...]] = {
    NonConsumerRelationship.STATE: (
        "imposto",
        "receita federal",
        "iptu",
        "ipva",
        "inss",
        "previdência social",
        "benefício previdenciário",
        "prefeitura",
        "multa de trânsito",
        "infração de trânsito",
        "detran",
        "concurso público",
        "polícia federal",
        "creche pública",
        "creche municipal",
        "escola pública",
        "escola municipal",
        "escola estadual",
        "hospital público",
        "posto de saúde",
    ),
    NonConsumerRelationship.TENANCY: (
        "moro de aluguel",
        "aluguel do apartamento",
        "aluguel da casa",
        "locação do imóvel",
        "locação do apartamento",
        "locação da casa",
        "imóvel alugado",
        "inquilino",
        "inquilina",
        "senhorio",
        "dono do apartamento",
        "dona do apartamento",
        "dono da casa",
        "dona da casa",
        "proprietário do imóvel",
        "proprietária do imóvel",
        "condomínio",
        "síndico",
        "síndica",
        "taxa condominial",
    ),
    NonConsumerRelationship.EMPLOYMENT: (
        "banco de horas",
        "benefício trabalhista",
        "demissão",
        "empregador",
        "hora extra",
        "horas extras",
        "contracheque",
        "em que trabalho",
        "onde trabalho",
        "salário",
        "vale-transporte",
        "vale transporte",
        "vínculo empregatício",
        "carteira de trabalho",
        "carteira assinada",
        "assinou minha carteira",
        "fgts",
        "patrão",
        "patroa",
        "mandado embora",
        "mandada embora",
        "fui demitido",
        "fui demitida",
    ),
    NonConsumerRelationship.FAMILY: (
        "pensão alimentícia",
        "pensão",
        "guarda do meu filho",
        "guarda da minha filha",
        "direito de visita",
        "deixa ver meu filho",
        "deixa ver minha filha",
        "ex-mulher",
        "ex-marido",
        "ex-companheiro",
        "ex-companheira",
        "divórcio",
        "herança",
        "inventário",
        "partilha",
        "testamento",
    ),
    NonConsumerRelationship.PRIVATE_PARTIES: (
        "meu vizinho",
        "minha vizinha",
        "emprestei dinheiro",
        "dinheiro emprestado",
        "de um particular",
        "de uma pessoa",
        "vendedor particular",
        "o outro motorista",
        "bateu no meu carro",
        "bateu na minha moto",
        "bateu na traseira",
        "vendi meu",
        "vendi minha",
    ),
    NonConsumerRelationship.COMPLAINANT_SUPPLIER: (
        "meu cliente",
        "minha cliente",
        "meus clientes",
        "minhas clientes",
        "minha empresa",
        "minha loja",
        "meu negócio",
        "meu sócio",
        "minha sócia",
        "me contratou",
        "me contrataram",
        "prestei serviço",
        *_SELF_EMPLOYED_SIGNALS,
        "vendi para",
    ),
}
# A business counterparty in the same clause as a transaction or service makes
# the clause a consumer relationship (the override). "empresa", "aplicativo"
# and the others name the professional side; "locadora" keeps car rentals in.
_CONSUMER_COUNTERPARTY_SIGNALS = (
    "fornecedor",
    "loja",
    "operadora",
    "empresa",
    "aplicativo",
    "plataforma",
    "concessionária",
    "distribuidora",
    "companhia",
    "locadora",
    "estacionamento",
    "hospital particular",
)
_CONSUMER_TRANSACTION_SERVICE_SIGNALS = (
    "assinatura",
    "cartão",
    "cobrança",
    "cobrou",
    "cobraram",
    "cobrando",
    "compra",
    "comprei",
    "contratei",
    "contrato",
    "crédito",
    "débito",
    "entrega",
    "empréstimo",
    "fatura",
    "financiamento",
    "internet",
    "juros",
    "pagamento",
    "paguei",
    "plano de saúde",
    "produto",
    "seguro",
    "serviço",
)

_BANK_ACCOUNT_SIGNALS = (
    "conta bancária",
    "conta bancaria",
    "conta-salário",
    "conta-salario",
    "conta salário",
    "conta salario",
    "minha conta",
    "meu saldo",
)

_BANK_ACCESS_FAILURE_SIGNALS = (
    "bloque",
    "não libera",
    "nao libera",
    "retid",
    "indisponível",
    "indisponivel",
)

_SCOPE_CLAUSE_BOUNDARY = re.compile(
    r"(?:[.!?;\n]+|,\s+(?:contudo|entretanto|mas|porem)\s+)"
)


def _scope_normalize(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").casefold())
    normalized = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^\S\n]+", " ", normalized).strip()


def _scope_contains_any(value: str, signals: tuple[str, ...]) -> bool:
    return any(_scope_normalize(signal) in value for signal in signals)


def _scope_clauses(normalized_complaint: str) -> tuple[str, ...]:
    return tuple(
        clause.strip()
        for clause in _SCOPE_CLAUSE_BOUNDARY.split(normalized_complaint)
        if clause.strip()
    )


def _whole_words(signal: str) -> re.Pattern[str]:
    # A plural "s" still matches: "inquilinos" is "inquilino".
    return re.compile(rf"(?<!\w){re.escape(_scope_normalize(signal))}s?(?!\w)")


_CLASS_SIGNALS: tuple[tuple[NonConsumerRelationship, str, re.Pattern[str]], ...] = tuple(
    (relationship, _scope_normalize(signal), _whole_words(signal))
    for relationship, signals in _RELATIONSHIP_SIGNALS.items()
    for signal in signals
)


def _has_class_signal(
    value: str,
    relationship: NonConsumerRelationship,
    *,
    except_signals: tuple[str, ...] = (),
) -> bool:
    excluded = {_scope_normalize(signal) for signal in except_signals}
    return any(
        pattern.search(value)
        for signal_relationship, signal, pattern in _CLASS_SIGNALS
        if signal_relationship is relationship and signal not in excluded
    )


def _bank_named(value: str) -> bool:
    return "banco" in value and "banco de horas" not in value


def assess_scope(*, complaint: str) -> ScopeAssessment:
    """The gate's decision, with the class and phrase that made it abstain."""

    normalized = _scope_normalize(complaint)
    # Private parties are two individuals, neither acting professionally: a
    # named business (the bank a scammer impersonated, the parking lot whose
    # valet hit the car) contradicts the class.
    business_named = _bank_named(normalized) or _scope_contains_any(
        normalized, _CONSUMER_COUNTERPARTY_SIGNALS
    )
    match = next(
        (
            (relationship, signal)
            for relationship, signal, pattern in _CLASS_SIGNALS
            if pattern.search(normalized)
            and not (business_named and relationship is NonConsumerRelationship.PRIVATE_PARTIES)
        ),
        None,
    )
    if match is None:
        return ScopeAssessment(in_scope=True)
    if any(_clause_has_consumer_relationship(clause) for clause in _scope_clauses(normalized)):
        return ScopeAssessment(in_scope=True)
    relationship, signal = match
    return ScopeAssessment(in_scope=False, relationship=relationship, signal=signal)


def is_consumer_scope(*, complaint: str) -> bool:
    """Return whether a case can safely use the consumer-law corpus.

    This is a high-precision abstention rule, not a legal merits classifier.
    It prevents known non-consumer disputes from being dressed in CDC grounds
    while leaving uncertain cases for human review. The narrative is the only
    input.
    """

    return assess_scope(complaint=complaint).in_scope


def _clause_has_consumer_relationship(clause: str) -> bool:
    bank_counterparty = _bank_named(clause)
    bank_account_dispute = (
        bank_counterparty
        and _scope_contains_any(clause, _BANK_ACCOUNT_SIGNALS)
        and _scope_contains_any(clause, _BANK_ACCESS_FAILURE_SIGNALS)
    )
    if bank_account_dispute:
        return True
    # When the complainant is the professional side, a company named in the
    # same clause is the complainant's customer, not a supplier.
    if _has_class_signal(
        clause,
        NonConsumerRelationship.COMPLAINANT_SUPPLIER,
        except_signals=_SELF_EMPLOYED_SIGNALS,
    ):
        return False

    has_counterparty = bank_counterparty or _scope_contains_any(
        clause, _CONSUMER_COUNTERPARTY_SIGNALS
    )
    if not has_counterparty or not _scope_contains_any(
        clause, _CONSUMER_TRANSACTION_SERVICE_SIGNALS
    ):
        return False
    if not _has_class_signal(clause, NonConsumerRelationship.EMPLOYMENT):
        return True

    # A worker can still have a separate consumer dispute with the employer's
    # store. Requiring an explicit personal card or invoice charge preserves
    # that case without treating a workplace purchase as a CDC relationship.
    return _scope_contains_any(clause, ("cobrança",)) and _scope_contains_any(
        clause, ("cartão", "fatura")
    )
