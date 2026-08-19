"""Quality metrics.

Groundedness and hallucination are computed MECHANICALLY: a conclusion's
citations either quote the source document or they do not. No LLM opinion
involved (see ADR 0008). Completeness and accuracy compare extraction
output against golden labels.

Retrieval metrics use human-authored page/passages as relevance judgments,
then resolve those judgments to the chunks produced by the current chunker.
This keeps the golden data stable when chunk sizes or chunk ids change.
"""

import math
from collections.abc import Sequence

from app.schemas.common import ConfidentConclusion
from app.schemas.evaluation import MetricResult
from app.schemas.lawsuit import LawsuitExtraction, PartyRole
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.report import LitigationReport
from app.services.citations import normalize_text, quote_verifies


def citation_supported(quote: str, document_text: str) -> bool:
    """True if the quote is substantive and really occurs in the document.

    Deliberately the same rule the composer applies at runtime, so a citation
    the product would strip never counts as grounded here.
    """
    return quote_verifies(quote, document_text)


def relevant_chunk_ids(
    chunks: Sequence[Chunk],
    *,
    page_ranges: Sequence[tuple[int, int]],
    passages: Sequence[str],
) -> set[str]:
    """Compatibility helper returning the union of relevance-equivalent groups."""
    return set().union(
        *relevant_chunk_groups(
            chunks,
            page_ranges=page_ranges,
            passages=passages,
        )
    )


def relevant_chunk_groups(
    chunks: Sequence[Chunk],
    *,
    page_ranges: Sequence[tuple[int, int]],
    passages: Sequence[str],
) -> list[set[str]]:
    """Resolve each stable judgment unit to equivalent current chunks.

    Multiple overlapping chunks can contain the same passage. They form one
    relevance group rather than inflating the Recall@K denominator when overlap
    or chunk size changes.
    """
    normalized_passages = [normalize_text(passage) for passage in passages]
    normalized_passages = [passage for passage in normalized_passages if passage]

    def page_matches(chunk: Chunk, ranges: Sequence[tuple[int, int]]) -> bool:
        return not ranges or any(
            chunk.page_start <= end and start <= chunk.page_end
            for start, end in ranges
        )

    if normalized_passages:
        return [
            {
                chunk.chunk_id
                for chunk in chunks
                if page_matches(chunk, page_ranges)
                and passage in normalize_text(chunk.text)
            }
            for passage in normalized_passages
        ]

    groups: list[set[str]] = []
    for start, end in page_ranges:
        groups.append(
            {
                chunk.chunk_id
                for chunk in chunks
                if chunk.page_start <= end and start <= chunk.page_end
            }
        )
    return groups


def _relevance_groups(value: set[str] | Sequence[set[str]]) -> list[set[str]]:
    if isinstance(value, set):
        return [{chunk_id} for chunk_id in sorted(value)]
    return [set(group) for group in value]


def retrieval_metrics_at_k(
    rankings: Sequence[Sequence[RetrievedChunk]],
    relevant_ids_by_query: Sequence[set[str] | Sequence[set[str]]],
    *,
    k: int,
) -> list[MetricResult]:
    """Compute macro ranking metrics over query-level relevance groups."""
    if k < 1:
        raise ValueError("k must be at least 1")
    if len(rankings) != len(relevant_ids_by_query):
        raise ValueError("rankings and relevance judgments must have equal length")
    if not rankings:
        return []

    scores: dict[str, list[float]] = {
        "retrieval_precision": [],
        "retrieval_recall": [],
        "retrieval_hit_rate": [],
        "retrieval_mrr": [],
        "retrieval_ndcg": [],
    }
    total_hits = 0
    total_relevant = 0

    for retrieved, raw_groups in zip(
        rankings, relevant_ids_by_query, strict=True
    ):
        groups = _relevance_groups(raw_groups)
        if not groups or any(not group for group in groups):
            raise ValueError("retrieval relevance judgment resolved to no current chunk")

        seen_chunk_ids: set[str] = set()
        groups_hit: set[int] = set()
        relevance: list[bool] = []
        for item in retrieved[:k]:
            chunk_id = item.chunk.chunk_id
            if chunk_id in seen_chunk_ids:
                relevance.append(False)
                continue
            seen_chunk_ids.add(chunk_id)
            matched_groups = {
                index for index, group in enumerate(groups) if chunk_id in group
            }
            new_groups = matched_groups - groups_hit
            relevance.append(bool(new_groups))
            groups_hit.update(matched_groups)

        hits = sum(relevance)
        relevant_units_hit = len(groups_hit)
        total_hits += relevant_units_hit
        total_relevant += len(groups)

        scores["retrieval_precision"].append(hits / k)
        scores["retrieval_recall"].append(relevant_units_hit / len(groups))
        scores["retrieval_hit_rate"].append(1.0 if relevant_units_hit else 0.0)
        first_relevant_rank = next(
            (rank for rank, is_relevant in enumerate(relevance, start=1) if is_relevant),
            None,
        )
        scores["retrieval_mrr"].append(
            1.0 / first_relevant_rank if first_relevant_rank else 0.0
        )

        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, is_relevant in enumerate(relevance, start=1)
            if is_relevant
        )
        ideal_hits = min(len(groups), k)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
        scores["retrieval_ndcg"].append(dcg / idcg if idcg else 0.0)

    query_count = len(rankings)
    details = (
        f"macro average over {query_count} queries at k={k}; "
        f"{total_hits}/{total_relevant} relevance units retrieved"
    )

    return [
        MetricResult(
            name=f"{name}@{k}",
            score=round(sum(values) / query_count, 3),
            details=details,
        )
        for name, values in scores.items()
    ]


def _all_conclusions(report: LitigationReport) -> list[ConfidentConclusion]:
    conclusions: list[ConfidentConclusion] = []
    if report.classification:
        conclusions.append(report.classification.conclusion)
    conclusions.extend(c.assessment for c in report.main_claims)
    conclusions.extend(report.evidence_found)
    if report.legal_risks:
        conclusions.append(report.legal_risks.overall)
        conclusions.extend(r.conclusion for r in report.legal_risks.risks)
    if report.suggested_strategy:
        conclusions.append(report.suggested_strategy.overall_approach)
        conclusions.append(report.suggested_strategy.settlement)
        conclusions.extend(d.assessment for d in report.suggested_strategy.defenses)
    return conclusions


def groundedness(report: LitigationReport, document_text: str) -> MetricResult:
    """Fraction of cited quotes that verify against the source document."""
    quotes = [
        citation.quote
        for conclusion in _all_conclusions(report)
        for citation in conclusion.citations
    ]
    if not quotes:
        return MetricResult(
            name="groundedness", score=0.0, details="no citations produced"
        )
    supported = sum(1 for q in quotes if citation_supported(q, document_text))
    return MetricResult(
        name="groundedness",
        score=round(supported / len(quotes), 3),
        details=f"{supported}/{len(quotes)} citations verified in source",
    )


def hallucination_rate(report: LitigationReport, document_text: str) -> MetricResult:
    """1 - groundedness: fraction of citations that do NOT verify.

    A fabricated quote is the strongest observable signal of hallucination.
    """
    grounded = groundedness(report, document_text)
    score = 1.0 - grounded.score if "no citations" not in grounded.details else 1.0
    return MetricResult(
        name="hallucination_rate",
        score=round(score, 3),
        details=f"complement of groundedness ({grounded.details})",
    )


def citation_coverage(report: LitigationReport) -> MetricResult:
    """Fraction of conclusions that carry at least one citation."""
    conclusions = _all_conclusions(report)
    if not conclusions:
        return MetricResult(name="citation_coverage", score=0.0, details="no conclusions")
    cited = sum(1 for c in conclusions if c.citations)
    return MetricResult(
        name="citation_coverage",
        score=round(cited / len(conclusions), 3),
        details=f"{cited}/{len(conclusions)} conclusions cite the document",
    )


def extraction_accuracy(
    extraction: LawsuitExtraction | None, expected: dict
) -> MetricResult:
    """Field-level agreement with golden labels."""
    if extraction is None:
        return MetricResult(name="extraction_accuracy", score=0.0, details="no extraction")

    checks: list[tuple[str, bool]] = []

    def _contains(haystack: str | None, needle: str) -> bool:
        return haystack is not None and normalize_text(needle) in normalize_text(haystack)

    if "lawsuit_type" in expected:
        pass  # scored separately in classification_accuracy
    if "case_number" in expected and expected["case_number"] is not None:
        checks.append(("case_number", extraction.case_number == expected["case_number"]))
    if "claim_value_amount" in expected and expected["claim_value_amount"] is not None:
        actual = extraction.claim_value.amount if extraction.claim_value else None
        checks.append(("claim_value", actual == expected["claim_value_amount"]))
    if "plaintiff" in expected:
        names = [p.name for p in extraction.parties if p.role is PartyRole.PLAINTIFF]
        checks.append(
            ("plaintiff", any(_contains(n, expected["plaintiff"]) for n in names))
        )
    if "defendant" in expected:
        names = [p.name for p in extraction.parties if p.role is PartyRole.DEFENDANT]
        checks.append(
            ("defendant", any(_contains(n, expected["defendant"]) for n in names))
        )
    for request in expected.get("main_requests_contains", []):
        checks.append(
            (
                f"request:{request}",
                any(_contains(r, request) for r in extraction.main_requests),
            )
        )

    if not checks:
        return MetricResult(name="extraction_accuracy", score=0.0, details="no golden labels")
    passed = sum(1 for _, ok in checks if ok)
    failed = [name for name, ok in checks if not ok]
    return MetricResult(
        name="extraction_accuracy",
        score=round(passed / len(checks), 3),
        details=f"{passed}/{len(checks)} checks passed"
        + (f"; failed: {', '.join(failed)}" if failed else ""),
    )


def completeness(extraction: LawsuitExtraction | None, expected: dict) -> MetricResult:
    """Fraction of expected-present fields the pipeline actually filled."""
    if extraction is None:
        return MetricResult(name="completeness", score=0.0, details="no extraction")
    expected_fields = [
        key
        for key in ("case_number", "claim_value_amount", "plaintiff", "defendant")
        if expected.get(key) is not None
    ]
    if expected.get("main_requests_contains"):
        expected_fields.append("main_requests")
    if not expected_fields:
        return MetricResult(name="completeness", score=1.0, details="nothing expected")

    found = 0
    for field in expected_fields:
        if field == "claim_value_amount":
            found += extraction.claim_value is not None
        elif field == "plaintiff":
            found += any(p.role is PartyRole.PLAINTIFF for p in extraction.parties)
        elif field == "defendant":
            found += any(p.role is PartyRole.DEFENDANT for p in extraction.parties)
        elif field == "main_requests":
            found += bool(extraction.main_requests)
        else:
            found += getattr(extraction, field) is not None
    return MetricResult(
        name="completeness",
        score=round(found / len(expected_fields), 3),
        details=f"{found}/{len(expected_fields)} expected fields present",
    )


def classification_accuracy(
    report: LitigationReport, expected: dict
) -> MetricResult | None:
    if "lawsuit_type" not in expected:
        return None
    actual = (
        report.classification.lawsuit_type.value if report.classification else None
    )
    correct = actual == expected["lawsuit_type"]
    return MetricResult(
        name="classification_accuracy",
        score=1.0 if correct else 0.0,
        details=f"expected={expected['lawsuit_type']} actual={actual}",
    )
