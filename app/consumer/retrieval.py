"""Deterministic retrieval-query construction for the consumer journey.

The consumer's own account must drive legal retrieval.  No fixed vocabulary
is injected into every query: measurement on the offline evaluation stack
showed that a fixed lexicon appended to every query dilutes the case-specific
signal and scored worse than no lexicon on every metric (article_recall@5,
recall@5 and ndcg@5), with a monotonic dose-response as more generic
vocabulary was added.  Keeping this logic deterministic also makes retrieval
evaluation and audit replay possible without an LLM call.
"""

from __future__ import annotations

import re
import unicodedata

from app.consumer.schemas import ConsumerCaseFacts

MAX_QUERY_CHARS = 2_000

# The intake is intentionally permissive, but a few domains are clearly not
# consumer relationships.  This deterministic gate is conservative: it only
# abstains on strong negative signals without any supplier/product/service
# signal.  Ambiguous cases remain eligible for human review instead of being
# silently rejected.
# In a mixed-domain clause, a transaction word alone does not identify which
# relationship the complaint is about. The bank is handled separately because
# "banco de horas" is an employment term, not a consumer counterparty.
_CONSUMER_COUNTERPARTY_SIGNALS = (
    "fornecedor",
    "loja",
    "operadora",
)
_CONSUMER_TRANSACTION_SERVICE_SIGNALS = (
    "assinatura",
    "cartão",
    "cobrança",
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
_NON_CONSUMER_SIGNALS = (
    "banco de horas",
    "benefício trabalhista",
    "beneficio trabalhista",
    "demissão",
    "demissao",
    "empregador",
    "herança",
    "heranca",
    "hora extra",
    "horas extras",
    "inventário",
    "inventario",
    "contracheque",
    "em que trabalho",
    "onde trabalho",
    "meu vizinho",
    "salário",
    "salario",
    "vale-transporte",
    "vale transporte",
    "vínculo empregatício",
    "vinculo empregaticio",
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

def build_legal_queries(facts: ConsumerCaseFacts) -> list[str]:
    """Build bounded, replayable legal queries from confirmed case facts."""

    return build_legal_queries_for_case(
        complaint=facts.complaint_summary or "",
        desired_resolution=facts.desired_resolution or "",
    )


def build_legal_queries_for_case(*, complaint: str, desired_resolution: str) -> list[str]:
    """Build the same production queries for a golden-dataset case."""

    # Reserve space for every signal instead of allowing a long complaint to
    # truncate the requested remedy.
    bounded_complaint = _bounded_component(complaint, 1_050)
    bounded_resolution = _bounded_component(desired_resolution, 500)
    narrative = _join_non_empty(bounded_complaint, bounded_resolution)
    queries = [
        _bounded(
            "Situação de consumo relatada: "
            f"{narrative}. Localizar dispositivos legais diretamente aplicáveis."
        ),
        _bounded(narrative),
    ]
    return _unique_non_empty(queries)


def is_consumer_scope(*, complaint: str) -> bool:
    """Return whether a case can safely use the consumer-law corpus.

    This is a high-precision abstention rule, not a legal merits classifier.
    It prevents known non-consumer disputes from being dressed in CDC grounds
    while leaving uncertain cases for human review. The narrative is the only
    input: the intake taxonomy that used to short-circuit this gate was
    keyword-inferred from this same text, so it could never contradict it.
    """

    normalized_complaint = _scope_normalize(complaint)
    has_non_consumer_signal = _scope_contains_any(
        normalized_complaint, _NON_CONSUMER_SIGNALS
    )
    if has_non_consumer_signal:
        return any(
            _clause_has_consumer_relationship(clause)
            for clause in _scope_clauses(normalized_complaint)
        )
    return True


def build_evidence_queries(facts: ConsumerCaseFacts) -> list[str]:
    """Build evidence lookups tied to the allegation and requested remedy."""

    complaint = _clean(facts.complaint_summary)
    resolution = _clean(facts.desired_resolution)
    return _unique_non_empty(
        [
            _bounded(
                f"Evidência que comprova os fatos, datas, valores e comunicações: {complaint}"
            ),
            _bounded(
                "Documento que comprova o prejuízo, a tentativa de solução e a providência "
                f"solicitada: {resolution}"
            ),
        ]
    )


def _join_non_empty(*values: str | None) -> str:
    return ". ".join(value for value in (_clean(item) for item in values) if value)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


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


def _clause_has_consumer_relationship(clause: str) -> bool:
    bank_counterparty = "banco" in clause and "banco de horas" not in clause
    bank_account_dispute = (
        bank_counterparty
        and _scope_contains_any(clause, _BANK_ACCOUNT_SIGNALS)
        and _scope_contains_any(clause, _BANK_ACCESS_FAILURE_SIGNALS)
    )
    if bank_account_dispute:
        return True

    has_counterparty = bank_counterparty or _scope_contains_any(
        clause, _CONSUMER_COUNTERPARTY_SIGNALS
    )
    if not has_counterparty or not _scope_contains_any(
        clause, _CONSUMER_TRANSACTION_SERVICE_SIGNALS
    ):
        return False
    if not _scope_contains_any(clause, _NON_CONSUMER_SIGNALS):
        return True

    # A worker can still have a separate consumer dispute with the employer's
    # store. Requiring an explicit personal card or invoice charge preserves
    # that case without treating a workplace purchase as a CDC relationship.
    return _scope_contains_any(clause, ("cobrança",)) and _scope_contains_any(
        clause, ("cartão", "fatura")
    )


def _bounded(value: str) -> str:
    return _bounded_component(value, MAX_QUERY_CHARS)


def _bounded_component(value: str | None, max_chars: int) -> str:
    normalized = _clean(value)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rsplit(" ", 1)[0]


def _unique_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _clean(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result
