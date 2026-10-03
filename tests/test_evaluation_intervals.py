"""Bootstrap intervals for evaluation averages and paired differences."""

from __future__ import annotations

from typing import Any

import pytest

from app.evaluation.intervals import (
    bootstrap_interval,
    metric_intervals,
    paired_difference_interval,
    with_intervals,
)
from app.schemas.evaluation import CaseResult, EvaluationSummary, MetricResult


def test_a_hand_checked_interval() -> None:
    # random.Random(0) draws 0.844, 0.758, 0.421, 0.259, 0.511, 0.405, 0.784,
    # 0.303. With two cases the resampled index pairs are (1, 1), (0, 0),
    # (1, 0) and (1, 0), so the means of [0, 1] are 1, 0, 0.5 and 0.5. At level
    # 0.5 the bounds are the 1st and 3rd of the four sorted means (0-based).
    interval = bootstrap_interval([0.0, 1.0], resamples=4, level=0.5)

    assert (interval.low, interval.high) == (0.5, 1.0)
    assert (interval.resamples, interval.level, interval.seed) == (4, 0.5, 0)


def test_identical_scores_give_a_zero_width_interval() -> None:
    constant = bootstrap_interval([0.25, 0.25, 0.25])
    single = bootstrap_interval([0.7])

    assert (constant.low, constant.high) == (0.25, 0.25)
    assert (single.low, single.high) == (0.7, 0.7)


def test_intervals_are_deterministic_and_bracket_the_mean() -> None:
    scores = [0.0, 0.0, 1.0, 0.5, 1.0, 0.0, 0.25]

    interval = bootstrap_interval(scores)

    assert interval == bootstrap_interval(scores)
    assert interval.low <= sum(scores) / len(scores) <= interval.high
    assert 0.0 <= interval.low < interval.high <= 1.0


@pytest.mark.parametrize(
    ("scores", "options", "message"),
    [
        ([], {}, "at least one score"),
        ([0.5], {"resamples": 0}, "resamples"),
        ([0.5], {"level": 1.0}, "level"),
    ],
)
def test_invalid_requests_are_rejected(
    scores: list[float], options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        bootstrap_interval(scores, **options)


def test_paired_differences() -> None:
    before = [0.0, 0.5, 1.0]

    shifted = paired_difference_interval(before, [0.1, 0.6, 1.1])
    same = paired_difference_interval(before, before)

    assert (shifted.low, shifted.high) == (0.1, 0.1)
    assert (same.low, same.high) == (0.0, 0.0)
    with pytest.raises(ValueError, match="same number"):
        paired_difference_interval([0.1], [0.1, 0.2])


def _result(name: str, split: str, score: float, *slices: str) -> CaseResult:
    return CaseResult(
        case_name=name,
        split=split,
        slices=slices,
        metrics=[MetricResult(name="m", score=score)],
    )


def test_summaries_get_intervals_overall_and_per_split_only() -> None:
    summary = with_intervals(
        EvaluationSummary.from_cases(
            [
                _result("a", "development", 1.0, "supplier:bank"),
                _result("b", "development", 1.0),
                _result("c", "holdout", 0.0),
            ]
        )
    )

    assert set(summary.intervals) == {"m"}
    assert summary.by_split["development"].intervals["m"].low == 1.0
    assert summary.by_split["holdout"].intervals["m"].high == 0.0
    assert summary.by_slice["supplier:bank"].intervals == {}
    assert metric_intervals([]) == {}
