"""Selection of the legal grounds a notice may cite.

Retrieval produces candidates; this module decides which of them become
``LegalGround`` objects. It is a pure function of the corpus, the confirmed
facts and the retrieval results, so the notice service and the notice-path
evaluator run exactly the same selection.
"""

from __future__ import annotations

from collections.abc import Callable

from app.consumer.legal_corpus import LegalCorpus
from app.consumer.legal_policy import (
    enforce_cdc_anchor,
    precedence_window,
    provision_is_eligible,
    strongly_supported_chunk_ids,
)
from app.consumer.retrieval import is_consumer_scope
from app.consumer.schemas import ConsumerCaseFacts, LegalGround, LegalProvision, ProvisionStatus
from app.schemas.rag import RetrievedChunk
from app.schemas.trace import RetrievalTrace

# A notice cites law; a weakly ranked article is worse than a shorter notice.
# Only the strongest merged hits are eligible, and the gate in legal_policy
# must show dense/lexical agreement (or independent-query corroboration when
# a trace lacks one of the channels).
#
# The relative floor applies only to reranker and single-channel scores. On
# reciprocal-rank fusion it can never bind: the best fused score is
# (w_dense + w_lexical) / (c + 1), and any chunk both channels returned within
# the candidate depth K scores at least (w_dense + w_lexical) / (c + K), which
# is above half the best whenever K <= c + 2 (62 at the default c = 60).
MAX_GROUND_CANDIDATES = 8
MIN_GROUND_SCORE_RATIO = 0.5
MAX_LEGAL_GROUNDS = 8

SupportGate = Callable[[list[RetrievalTrace]], frozenset[str]]


def select_legal_grounds(
    corpus: LegalCorpus,
    facts: ConsumerCaseFacts,
    result_sets: list[list[RetrievedChunk]],
    traces: list[RetrievalTrace] | None = None,
    *,
    support: SupportGate = strongly_supported_chunk_ids,
) -> list[LegalGround]:
    """Return the legal grounds a notice would cite for these results.

    ``support`` is the retrieval-agreement gate. It is a parameter so the
    service can resolve it at call time and experiments can replace it.
    """

    if not is_consumer_scope(complaint=facts.complaint_summary or ""):
        return []
    # Retrieval queries are built from the narrative alone (see
    # build_legal_queries); no intake label routes or gates which articles
    # may be cited.
    strongly_supported = support(traces or [])
    if not strongly_supported:
        return []
    merged = merge_results(result_sets)
    if not merged:
        return []
    score_floor = _score_floor(merged, traces or [])
    provision_candidates = _provision_candidates(
        corpus,
        _eligible_candidates(corpus, merged, strongly_supported),
        chunk_query_occurrences(traces or []),
    )

    provision_candidates.sort(key=lambda item: (item[0], item[3].chunk.chunk_id))
    lexical_only = any(trace.degraded_mode == "lexical_only" for trace in traces or [])
    window = precedence_window(
        [(corpus.provision_for_chunk(item[3]), item) for item in provision_candidates],
        window=MAX_GROUND_CANDIDATES,
        lexical_only=lexical_only,
    )
    selected: list[tuple[LegalProvision, LegalGround]] = []
    for provision, (_, provision_score, rank, result) in window:
        if provision_score < score_floor:
            continue
        authority = corpus.authority_for_chunk(result, retrieval_rank=rank)
        selected.append(
            (
                provision,
                LegalGround(
                    authority=authority,
                    application_to_facts=(
                        f"O texto oficial em {provision.citation_label} foi localizado "
                        "pela política de recuperação a partir do relato do consumidor. "
                        "Sua aplicabilidade ao caso não foi decidida pelo sistema e deve "
                        "ser validada por profissional habilitado contra os fatos e "
                        "documentos citados."
                    ),
                ),
            )
        )
        if len(selected) >= MAX_LEGAL_GROUNDS:
            break
    return [ground for _, ground in enforce_cdc_anchor(selected)]


def _score_floor(merged: list[RetrievedChunk], traces: list[RetrievalTrace]) -> float:
    """Half the best score, except on fused ranks where the gate already dominates."""

    if all(trace.score_type == "rrf_score" for trace in traces if trace.error is None):
        return float("-inf")
    return merged[0].score * MIN_GROUND_SCORE_RATIO


def _eligible_candidates(
    corpus: LegalCorpus,
    merged: list[RetrievedChunk],
    strongly_supported: frozenset[str],
) -> dict[str, list[tuple[int, RetrievedChunk]]]:
    """Group corroborated, in-force and eligible hits by provision, with their rank."""

    candidates_by_provision: dict[str, list[tuple[int, RetrievedChunk]]] = {}
    for rank, result in enumerate(merged, start=1):
        if result.chunk.chunk_id not in strongly_supported:
            continue
        for provision in corpus.provisions_for_chunk(result):
            if provision.status is not ProvisionStatus.ACTIVE:
                continue
            if not provision_is_eligible(provision):
                continue
            unit = corpus.unit_for_chunk(result)
            if unit is not None and unit.status is not ProvisionStatus.ACTIVE:
                continue
            candidates_by_provision.setdefault(provision.provision_id, []).append(
                (rank, result)
            )
    return candidates_by_provision


def _provision_candidates(
    corpus: LegalCorpus,
    candidates_by_provision: dict[str, list[tuple[int, RetrievedChunk]]],
    query_occurrences: dict[str, int],
) -> list[tuple[int, float, int, RetrievedChunk]]:
    """One candidate per provision: its best rank and score, and the unit to quote."""

    provision_candidates: list[tuple[int, float, int, RetrievedChunk]] = []
    for ranked_candidates in candidates_by_provision.values():
        selected_rank, selected = min(
            ranked_candidates,
            key=lambda item: (
                -query_occurrences.get(item[1].chunk.chunk_id, 0),
                -int(corpus.unit_for_chunk(item[1]) is not None),
                -item[1].score,
                item[1].chunk.chunk_id,
            ),
        )
        # A provision keeps the strongest position earned by any sibling,
        # while the quoted unit is the one corroborated across the most
        # independent formulations of the confirmed facts.
        provision_candidates.append(
            (
                min(rank for rank, _ in ranked_candidates),
                max(item.score for _, item in ranked_candidates),
                selected_rank,
                selected,
            )
        )
    return provision_candidates


def merge_results(result_sets: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    best: dict[str, RetrievedChunk] = {}
    for results in result_sets:
        for result in results:
            current = best.get(result.chunk.chunk_id)
            if current is None or result.score > current.score:
                best[result.chunk.chunk_id] = result
    return sorted(best.values(), key=lambda item: (-item.score, item.chunk.chunk_id))


def chunk_query_occurrences(traces: list[RetrievalTrace]) -> dict[str, int]:
    """Count independent queries that returned each chunk.

    Scores from separate RRF calls share a scale, but taking only their maximum
    erases the stronger signal that a statutory unit survived several distinct
    formulations of the confirmed facts.
    """

    occurrences: dict[str, set[tuple[str, int]]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        query_key = (trace.batch_id, trace.query_index)
        for item in trace.results:
            occurrences.setdefault(item.chunk_id, set()).add(query_key)
    return {chunk_id: len(queries) for chunk_id, queries in occurrences.items()}
