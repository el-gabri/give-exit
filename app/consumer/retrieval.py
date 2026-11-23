"""Deterministic retrieval-query construction for the consumer journey.

The consumer's own account must drive legal retrieval.  No fixed vocabulary
is injected into every query: measurement on the offline evaluation stack
showed that a fixed lexicon appended to every query dilutes the case-specific
signal and scored worse than no lexicon on every metric (article_recall@5,
recall@5 and ndcg@5), with a monotonic dose-response as more generic
vocabulary was added.  Keeping this logic deterministic also makes retrieval
evaluation and audit replay possible without an LLM call.

For a complaint that fits the narrative (1,050 characters), the two ranking
queries are one formulation of it: the second is the bare complaint and
remedy, and the first wraps it in a short framing. Splitting such a short
complaint into one query per sentence, alone or on top of these two, lowered
recall@5 on the offline stack (0.142 to at most 0.100) and added
uncorroborated grounds to notices. Independent formulations are still needed
when retrieval degrades to a single channel: there the complaint and the
requested remedy are searched separately, only to corroborate candidates the
ranking queries found.

A longer complaint lost everything past the narrative's cut. The first real
long prompt complaint's forced-fulfilment sentence started at character
1,109, and its notice cited the refund it refused. Such a complaint is now
searched in whole-sentence windows plus the remedy, within today's embedding
cost (ADR 0026). The support gate asks a provision only windows found for
corroboration (``legal_policy``). Windows are independent formulations, so
degraded retrieval corroborates across them without extra queries.

On ten long versions of labelled development cases, measured on the
configured stack:
- right articles cited went from 5 to 10;
- known-wrong citations stayed at 3;
- refusals went from 2 to 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.consumer.legal_policy import LEGAL_WINDOW_AGENT
from app.consumer.schemas import ConsumerCaseFacts
from app.rag.pipeline import RagPipeline
from app.schemas.rag import RetrievedChunk
from app.schemas.trace import RetrievalTrace

MAX_QUERY_CHARS = 2_000
# How much of the complaint and of the requested remedy the narrative keeps.
# Space is reserved for both, so a long complaint cannot push the remedy out.
_NARRATIVE_COMPLAINT_CHARS = 1_050
_NARRATIVE_RESOLUTION_CHARS = 500
# Legal candidates per ranking query; the notice cites at most this many grounds.
LEGAL_REQUESTED_K = 8
LEGAL_RETRIEVAL_AGENT = "consumer_legal_authorities"
LEGAL_CORROBORATION_AGENT = "consumer_legal_corroboration"
# A complaint longer than the narrative is read in whole-sentence windows of at
# most this many characters, and the windows together stay within the budget:
# with a remedy, about the cost of the two narrative queries (ADR 0026).
_WINDOW_CHARS = 600
_WINDOW_BUDGET_CHARS = 2_400
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class LegalQueryPlan:
    """A case's ranking queries, and the sentence windows of a long complaint."""

    # The two narrative queries, or the remedy alone for a long complaint.
    ranking: tuple[str, ...]
    windows: tuple[str, ...] = ()

    @property
    def queries(self) -> list[str]:
        return [*self.ranking, *self.windows]


def build_legal_queries(facts: ConsumerCaseFacts) -> list[str]:
    """Build bounded, replayable legal queries from confirmed case facts."""

    return build_legal_query_plan(facts).queries


def build_legal_query_plan(facts: ConsumerCaseFacts) -> LegalQueryPlan:
    return build_legal_query_plan_for_case(
        complaint=facts.complaint_summary or "",
        desired_resolution=facts.desired_resolution or "",
    )


def build_legal_queries_for_case(*, complaint: str, desired_resolution: str) -> list[str]:
    """Build the same production queries for a golden-dataset case."""

    return build_legal_query_plan_for_case(
        complaint=complaint, desired_resolution=desired_resolution
    ).queries


def build_legal_query_plan_for_case(*, complaint: str, desired_resolution: str) -> LegalQueryPlan:
    """The narrative for a complaint that fits it; the remedy and windows otherwise."""

    windows = legal_query_windows(complaint)
    if not windows:
        return LegalQueryPlan(ranking=tuple(_narrative_queries(complaint, desired_resolution)))
    remedy = _bounded_component(desired_resolution, _NARRATIVE_RESOLUTION_CHARS)
    return LegalQueryPlan(ranking=tuple(_unique_non_empty([remedy])), windows=tuple(windows))


def legal_query_windows(complaint: str | None) -> list[str]:
    """A long complaint's whole-sentence windows within the budget; none when it fits.

    Past the budget, the last window stays (the latest state of the dispute)
    and windows join from the start (what was bought and what went wrong)
    while they fit. A repeated window is searched once: two copies of one
    passage would corroborate each other.
    """

    normalized = _clean(complaint)
    if len(normalized) <= _NARRATIVE_COMPLAINT_CHARS:
        return []
    return _unique_non_empty([_bounded(window) for window in _within_budget(_packed(normalized))])


def _packed(normalized: str) -> list[str]:
    """Whole sentences packed greedily into windows of at most ``_WINDOW_CHARS``."""

    windows: list[str] = []
    current = ""
    for sentence in _SENTENCE_BOUNDARY_RE.split(normalized):
        candidate = f"{current} {sentence}" if current else sentence
        if current and len(candidate) > _WINDOW_CHARS:
            windows.append(current)
            current = sentence
        else:
            current = candidate
    windows.append(current)
    return windows


def _within_budget(windows: list[str]) -> list[str]:
    if sum(map(len, windows)) <= _WINDOW_BUDGET_CHARS:
        return windows
    *head, last = windows
    kept: list[str] = []
    used = len(last)
    for window in head:
        if used + len(window) > _WINDOW_BUDGET_CHARS:
            break
        kept.append(window)
        used += len(window)
    return [*kept, last]


def _narrative_queries(complaint: str, desired_resolution: str) -> list[str]:
    bounded_complaint = _bounded_component(complaint, _NARRATIVE_COMPLAINT_CHARS)
    bounded_resolution = _bounded_component(desired_resolution, _NARRATIVE_RESOLUTION_CHARS)
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


def truncated_narrative_fields(facts: ConsumerCaseFacts) -> dict[str, tuple[int, int]]:
    """The facts legal retrieval does not read whole: each one's length and the
    characters it searches.

    A long complaint is searched in windows within a budget, and a long remedy
    up to 500 characters. A refusal names the cut, so the consumer can put the
    essential where it is read.
    """

    cut: dict[str, tuple[int, int]] = {}
    complaint = _clean(facts.complaint_summary)
    searched = _searched_complaint_chars(complaint)
    if searched < len(complaint):
        cut["complaint_summary"] = (len(complaint), searched)
    remedy = _clean(facts.desired_resolution)
    if len(remedy) > _NARRATIVE_RESOLUTION_CHARS:
        cut["desired_resolution"] = (len(remedy), _NARRATIVE_RESOLUTION_CHARS)
    return cut


def _searched_complaint_chars(complaint: str) -> int:
    """How many characters of a normalized complaint the ranking queries read."""

    if len(complaint) <= _NARRATIVE_COMPLAINT_CHARS:
        return len(complaint)
    windows = _packed(complaint)
    kept = _within_budget(windows)
    if kept == windows and all(len(window) <= MAX_QUERY_CHARS for window in windows):
        return len(complaint)
    return sum(len(_bounded(window)) for window in kept)


def build_corroboration_queries(facts: ConsumerCaseFacts) -> list[str]:
    """The complaint and the requested remedy as separate, independent queries.

    Only a complaint that fits the narrative needs them: a long complaint's
    windows and remedy already are independent formulations.

    Neither text contains the other, so a chunk both rank highly has support
    from two formulations of the case rather than two framings of one.

    Each is cut exactly where the narrative cuts it. The support gate tells a
    ranking query from independent corroboration by containment, so every
    ranking query must contain both of these. A longer complaint searched
    whole was not part of the narrative any more and passed for a second,
    independent signal.
    """

    return _unique_non_empty(
        [
            _bounded_component(facts.complaint_summary, _NARRATIVE_COMPLAINT_CHARS),
            _bounded_component(facts.desired_resolution, _NARRATIVE_RESOLUTION_CHARS),
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

    A long complaint's windows run as their own batch, under their own agent,
    so they get their own embedding call and the gate can tell them apart.
    They need no extra corroboration queries: they already are independent
    formulations of the complaint.
    """

    plan = build_legal_query_plan(facts)
    result_sets: list[list[RetrievedChunk]] = []
    traces: list[RetrievalTrace] = []
    if plan.ranking:  # empty only for a long complaint without a remedy
        result_sets, traces = await rag.retrieve_many_with_traces(
            list(plan.ranking), doc_id=doc_id, agent=LEGAL_RETRIEVAL_AGENT, k=k, mode="hybrid"
        )
    if plan.windows:
        window_sets, window_traces = await rag.retrieve_many_with_traces(
            list(plan.windows), doc_id=doc_id, agent=LEGAL_WINDOW_AGENT, k=k, mode="hybrid"
        )
        return [*result_sets, *window_sets], [*traces, *window_traces]
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

    These are not ranking queries, so the account and the remedy are searched
    whole, beyond what the narrative keeps of them.
    """

    return _unique_non_empty(
        [
            _bounded_component(facts.complaint_summary, MAX_QUERY_CHARS),
            _bounded_component(facts.desired_resolution, MAX_QUERY_CHARS),
            _bounded(" ".join(_case_identifiers(facts))),
        ]
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
