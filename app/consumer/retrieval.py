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
