"""Deterministic retrieval-query construction for the consumer journey.

The consumer's own account must drive legal retrieval.  No fixed vocabulary
is injected into every query: measurement on the offline evaluation stack
showed that a fixed lexicon appended to every query dilutes the case-specific
signal and scored worse than no lexicon on every metric (article_recall@5,
recall@5 and ndcg@5), with a monotonic dose-response as more generic
vocabulary was added.  Keeping this logic deterministic also makes retrieval
evaluation and audit replay possible without an LLM call.

The two ranking queries are one formulation of the narrative: the second is
the bare complaint and remedy, and the first wraps it in a short framing.
Splitting the complaint into one query per sentence, alone or on top of these
two, was measured on the offline stack and lowered recall@5 (0.142 to at most
0.100) while adding uncorroborated grounds to notices, so it is not used for
ranking. Independent formulations are still needed when retrieval degrades to
a single channel: there the complaint and the requested remedy are searched
separately, only to corroborate candidates the ranking queries found.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from app.consumer.schemas import ConsumerCaseFacts
from app.rag.pipeline import RagPipeline
from app.schemas.rag import RetrievedChunk
from app.schemas.trace import RetrievalTrace

MAX_QUERY_CHARS = 2_000
# Legal candidates per ranking query; the notice cites at most this many grounds.
LEGAL_REQUESTED_K = 8
LEGAL_RETRIEVAL_AGENT = "consumer_legal_authorities"
LEGAL_CORROBORATION_AGENT = "consumer_legal_corroboration"

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
    # A traffic fine or a vehicle tax is owed to the State, not to a supplier.
    "multa de trânsito",
    "multa de transito",
    "infração de trânsito",
    "infracao de transito",
    "detran",
    "ipva",
    # Lending one's own money to someone is a private loan, not consumption.
    # "Emprestei meu cartão" still names a card dispute, so only money counts.
    "emprestei dinheiro",
    "dinheiro emprestado",
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
            _join_non_empty(
                f"Situação de consumo relatada: {narrative}",
                "Localizar dispositivos legais diretamente aplicáveis.",
            )
        ),
        _bounded(narrative),
    ]
    return _unique_non_empty(queries)


def build_corroboration_queries(facts: ConsumerCaseFacts) -> list[str]:
    """The complaint and the requested remedy as separate, independent queries.

    Neither text contains the other, so a chunk both rank highly has support
    from two formulations of the case rather than two framings of one.
    """

    return _unique_non_empty(
        [
            _bounded_component(facts.complaint_summary, MAX_QUERY_CHARS),
            _bounded_component(facts.desired_resolution, MAX_QUERY_CHARS),
        ]
    )


async def retrieve_legal_candidates(
    rag: RagPipeline,
    facts: ConsumerCaseFacts,
    *,
    doc_id: str,
    k: int,
) -> tuple[list[list[RetrievedChunk]], list[RetrievalTrace]]:
    """Rank legal candidates, adding corroboration traces when a channel is missing.

    Healthy hybrid retrieval supports a chunk through dense/lexical agreement,
    so it needs nothing more. When retrieval fell back to one channel, the
    complaint and the remedy are searched separately; their traces feed the
    support gate but never add candidates, so the ranking stays the one the
    ranking queries produced.
    """

    result_sets, traces = await rag.retrieve_many_with_traces(
        build_legal_queries(facts), doc_id=doc_id, agent=LEGAL_RETRIEVAL_AGENT, k=k, mode="hybrid"
    )
    if all(trace.retrieval_mode == "hybrid" and not trace.degraded_mode for trace in traces):
        return result_sets, traces
    queries = build_corroboration_queries(facts)
    if len(queries) < 2:
        return result_sets, traces
    _, corroborating = await rag.retrieve_many_with_traces(
        queries, doc_id=doc_id, agent=LEGAL_CORROBORATION_AGENT, k=k, mode="hybrid"
    )
    return result_sets, [*traces, *corroborating]


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
    """Build evidence lookups from the confirmed facts, in the consumer's own terms.

    The allegation and the requested remedy describe the problem; protocols,
    the incident date and the amounts are what receipts, statements and
    screenshots actually print, often without a single word of the account.
    Generic framing such as "Evidência que comprova..." is deliberately absent:
    it matched every uploaded page lexically and made the retrieval-agreement
    gate hold for any attachment.
    """

    return _unique_non_empty(
        [*build_corroboration_queries(facts), _bounded(" ".join(_case_identifiers(facts)))]
    )


def _case_identifiers(facts: ConsumerCaseFacts) -> list[str]:
    amounts = (facts.direct_loss_amount, facts.improper_payment_amount)
    return _unique_non_empty(
        [
            *facts.prior_protocols,
            facts.incident_date_or_period or "",
            *(_brazilian_amount(amount) for amount in amounts if amount is not None),
        ]
    )


def _brazilian_amount(amount: Decimal) -> str:
    """``Decimal("9208.8")`` as printed on Brazilian documents: ``9.208,80``."""
    return f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _join_non_empty(*values: str | None) -> str:
    """Join sentences without doubling a period the consumer already typed."""
    parts = [value for value in (_clean(item) for item in values) if value]
    return " ".join(
        part if index == len(parts) - 1 or part.endswith((".", "!", "?")) else f"{part}."
        for index, part in enumerate(parts)
    )


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
