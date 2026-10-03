"""Paired comparison of two evaluation runs.

Two golden runs differ by a handful of cases, so their averages alone cannot
say whether a change helped. This tool pairs the runs case by case and reports,
for every metric and count, the mean difference with a paired-bootstrap
interval. It decides nothing: an interval that straddles zero is the honest
answer that the golden set cannot tell the runs apart.

    python -m app.evaluation.compare before.json after.json [--split development]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.evaluation.intervals import paired_difference_interval
from app.schemas.evaluation import (
    CASE_SPLITS,
    CaseResult,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricInterval,
)

_RUN_FIELDS = (
    "dataset_sha256",
    "case_split",
    "corpus_release_id",
    "query_builder_version",
    "ground_policy_version",
    "agreement_max_rank",
    "ground_verifier",
)
_RETRIEVAL_FIELDS = (
    "retrieval_mode",
    "embedding_model",
    "embedding_model_revision",
    "reranker_model",
)

_Pair = tuple[CaseResult, CaseResult]


@dataclass(frozen=True, slots=True)
class MetricComparison:
    name: str
    pairs: int
    before: float
    after: float
    interval: MetricInterval

    @property
    def difference(self) -> float:
        return round(self.after - self.before, 3)


@dataclass(frozen=True, slots=True)
class CountComparison:
    name: str
    pairs: int
    before: int
    after: int
    low: float
    high: float

    @property
    def difference(self) -> int:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class Comparison:
    pairs: int
    only_before: tuple[str, ...]
    only_after: tuple[str, ...]
    metrics: tuple[MetricComparison, ...]
    counts: tuple[CountComparison, ...]
    one_sided_metrics: tuple[str, ...]
    one_sided_counts: tuple[str, ...]
    unpaired_metrics: tuple[str, ...]
    metadata_differences: tuple[tuple[str, str, str], ...]


def compare_summaries(
    before: EvaluationSummary, after: EvaluationSummary, *, split: str | None = None
) -> Comparison:
    """Pair two runs by case name, optionally within one split of both."""

    earlier = _cases(before, split)
    later = _cases(after, split)
    shared = sorted(earlier.keys() & later.keys())
    if not shared:
        scope = f" in the {split} split" if split else ""
        raise ValueError(f"the two runs share no case{scope}")
    pairs = [(earlier[name], later[name]) for name in shared]
    metrics = _metric_comparisons(pairs)
    before_metrics, after_metrics = _reported(pairs, _scores)
    before_counts, after_counts = _reported(pairs, lambda case: case.counts)
    compared = {item.name for item in metrics}
    return Comparison(
        pairs=len(pairs),
        only_before=tuple(sorted(earlier.keys() - later.keys())),
        only_after=tuple(sorted(later.keys() - earlier.keys())),
        metrics=metrics,
        counts=_count_comparisons(pairs, sorted(before_counts & after_counts)),
        one_sided_metrics=tuple(sorted(before_metrics ^ after_metrics)),
        one_sided_counts=tuple(sorted(before_counts ^ after_counts)),
        unpaired_metrics=tuple(sorted((before_metrics & after_metrics) - compared)),
        metadata_differences=_metadata_differences(before.run, after.run),
    )


def render_comparison(comparison: Comparison) -> str:
    """A text report: pairing, changed run settings, then metric and count tables."""

    lines = [f"pairs: {comparison.pairs}"]
    if comparison.only_before:
        lines.append("only in BEFORE (excluded): " + ", ".join(comparison.only_before))
    if comparison.only_after:
        lines.append("only in AFTER (excluded): " + ", ".join(comparison.only_after))
    if comparison.one_sided_metrics:
        lines.append("reported by one run only: " + ", ".join(comparison.one_sided_metrics))
    if comparison.one_sided_counts:
        lines.append("counts reported by one run only: " + ", ".join(comparison.one_sided_counts))
    if comparison.unpaired_metrics:
        lines.append(
            "reported by both runs but never for the same case: "
            + ", ".join(comparison.unpaired_metrics)
        )
    lines.extend(
        f"changed {name}: {earlier} -> {later}"
        for name, earlier, later in comparison.metadata_differences
    )
    lines += ["", _header("metric")]
    lines.extend(
        f"{item.name[:44]:44} {item.pairs:>5} {item.before:>7.3f} {item.after:>7.3f} "
        f"{item.difference:>+7.3f}  [{item.interval.low:+.3f}, {item.interval.high:+.3f}]"
        for item in comparison.metrics
    )
    if comparison.counts:
        lines += ["", _header("count")]
        lines.extend(
            f"{item.name[:44]:44} {item.pairs:>5} {item.before:>7} {item.after:>7} "
            f"{item.difference:>+7}  [{item.low:+.1f}, {item.high:+.1f}]"
            for item in comparison.counts
        )
    return "\n".join(lines)


def load_summary(path: Path) -> EvaluationSummary:
    """Read one summary written by ``consumer_runner --output``."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "agreement_sweep" in payload:
        raise ValueError(f"{path} is not one evaluation summary; sweep files are not supported")
    return EvaluationSummary.model_validate(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two Consumer evaluation runs case by case."
    )
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--split", choices=CASE_SPLITS, help="compare only one split's cases")
    args = parser.parse_args(argv)
    try:
        comparison = compare_summaries(
            load_summary(args.before), load_summary(args.after), split=args.split
        )
    except ValueError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 2
    print(render_comparison(comparison))
    return 0


def _header(kind: str) -> str:
    return f"{kind:44} {'pairs':>5} {'before':>7} {'after':>7} {'diff':>7}  95% interval"


def _cases(summary: EvaluationSummary, split: str | None) -> dict[str, CaseResult]:
    return {
        case.case_name: case for case in summary.cases if split is None or case.split == split
    }


def _scores(case: CaseResult) -> dict[str, float]:
    return {metric.name: metric.score for metric in case.metrics}


def _metric_comparisons(pairs: list[_Pair]) -> tuple[MetricComparison, ...]:
    paired: dict[str, tuple[list[float], list[float]]] = {}
    for earlier, later in pairs:
        before_scores, after_scores = _scores(earlier), _scores(later)
        for name in before_scores.keys() & after_scores.keys():
            values = paired.setdefault(name, ([], []))
            values[0].append(before_scores[name])
            values[1].append(after_scores[name])
    return tuple(
        MetricComparison(
            name=name,
            pairs=len(before),
            before=round(sum(before) / len(before), 3),
            after=round(sum(after) / len(after), 3),
            interval=paired_difference_interval(before, after),
        )
        for name, (before, after) in sorted(paired.items())
    )


def _count_comparisons(pairs: list[_Pair], names: list[str]) -> tuple[CountComparison, ...]:
    """Counts both runs report. A case without one counts 0 (a failed case has none)."""
    comparisons: list[CountComparison] = []
    for name in names:
        before = [earlier.counts.get(name, 0) for earlier, _ in pairs]
        after = [later.counts.get(name, 0) for _, later in pairs]
        # The interval of a total is the pair count times that of the mean.
        mean = paired_difference_interval(before, after)
        comparisons.append(
            CountComparison(
                name=name,
                pairs=len(pairs),
                before=sum(before),
                after=sum(after),
                low=round(mean.low * len(pairs), 1),
                high=round(mean.high * len(pairs), 1),
            )
        )
    return tuple(comparisons)


def _reported(
    pairs: list[_Pair], names: Callable[[CaseResult], Mapping[str, object]]
) -> tuple[set[str], set[str]]:
    """The names each run reports for any shared case.

    A name only one run reports is listed rather than compared: reading it as 0
    in the other run would invent a difference, for instance against a result
    file written before that metric or count existed.
    """
    before = {name for earlier, _ in pairs for name in names(earlier)}
    after = {name for _, later in pairs for name in names(later)}
    return before, after


def _metadata_differences(
    before: EvaluationRunMetadata | None, after: EvaluationRunMetadata | None
) -> tuple[tuple[str, str, str], ...]:
    earlier, later = _metadata(before), _metadata(after)
    return tuple(
        (name, earlier[name], later[name]) for name in earlier if earlier[name] != later[name]
    )


def _metadata(run: EvaluationRunMetadata | None) -> dict[str, str]:
    if run is None:
        return {name: "-" for name in (*_RUN_FIELDS, *_RETRIEVAL_FIELDS)}
    values: dict[str, object] = {name: getattr(run, name) for name in _RUN_FIELDS}
    values.update({name: getattr(run.retrieval, name) for name in _RETRIEVAL_FIELDS})
    return {name: "-" if value is None else str(value) for name, value in values.items()}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
