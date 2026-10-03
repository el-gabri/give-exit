"""Case-bootstrap confidence intervals for evaluation averages.

A golden run averages per-case scores over a few dozen cases, so one case
flipping moves an average by several hundredths. These intervals resample the
cases with replacement and report the percentile range of the resampled means:
how far an average can be trusted to separate two runs.

Indices are drawn as ``int(rng.random() * n)`` from ``random.Random(seed)``
rather than with ``randrange`` or ``choices``, whose algorithms are not
promised to stay fixed across Python releases, so the same scores and seed give
the same interval on every supported interpreter.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from functools import lru_cache

from app.schemas.evaluation import CaseResult, EvaluationSummary, MetricInterval

DEFAULT_RESAMPLES = 2_000
DEFAULT_LEVEL = 0.95
DEFAULT_SEED = 0


def bootstrap_interval(
    scores: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    level: float = DEFAULT_LEVEL,
    seed: int = DEFAULT_SEED,
) -> MetricInterval:
    """Percentile bootstrap interval of the mean of ``scores``."""

    if not scores:
        raise ValueError("a bootstrap interval needs at least one score")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie strictly between 0 and 1")
    values = [float(score) for score in scores]
    count = len(values)
    means = sorted(
        sum(values[index] for index in sample) / count
        for sample in _resample_indices(count, resamples, seed)
    )
    tail = (1.0 - level) / 2.0
    return MetricInterval(
        low=round(means[_order_statistic(tail, resamples)], 3),
        high=round(means[_order_statistic(1.0 - tail, resamples)], 3),
        level=level,
        resamples=resamples,
        seed=seed,
    )


def paired_difference_interval(
    before: Sequence[float],
    after: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    level: float = DEFAULT_LEVEL,
    seed: int = DEFAULT_SEED,
) -> MetricInterval:
    """Bootstrap interval of the mean per-pair difference ``after - before``."""

    if len(before) != len(after):
        raise ValueError("paired scores need the same number of cases")
    return bootstrap_interval(
        [later - earlier for earlier, later in zip(before, after, strict=True)],
        resamples=resamples,
        level=level,
        seed=seed,
    )


def metric_intervals(cases: Iterable[CaseResult]) -> dict[str, MetricInterval]:
    """One interval per metric, over the same per-case scores its average uses."""

    scores: dict[str, list[float]] = {}
    for case in cases:
        for metric in case.metrics:
            scores.setdefault(metric.name, []).append(metric.score)
    return {name: bootstrap_interval(values) for name, values in sorted(scores.items())}


def with_intervals(summary: EvaluationSummary) -> EvaluationSummary:
    """The summary with intervals for its averages and for each split's.

    Slice and category groups get none: most of them hold one or two cases.
    """

    by_split = {
        name: group.model_copy(
            update={
                "intervals": metric_intervals(
                    case for case in summary.cases if case.split == name
                )
            }
        )
        for name, group in summary.by_split.items()
    }
    return summary.model_copy(
        update={"intervals": metric_intervals(summary.cases), "by_split": by_split}
    )


def _order_statistic(quantile: float, count: int) -> int:
    """The 0-based index of the ``quantile`` order statistic among ``count`` means."""
    # Rounding first keeps 0.975 * 2000 from landing on 1949.9999999.
    return min(count - 1, math.floor(round(quantile * count, 9)))


@lru_cache(maxsize=32)
def _resample_indices(count: int, resamples: int, seed: int) -> tuple[tuple[int, ...], ...]:
    rng = random.Random(seed)
    return tuple(
        tuple(int(rng.random() * count) for _ in range(count)) for _ in range(resamples)
    )
