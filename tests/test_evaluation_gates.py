"""The evaluation CLI must fail a build when a metric regresses."""

import pytest

from app.evaluation.__main__ import check_gates, parse_args
from app.schemas.evaluation import CaseResult, EvaluationSummary, MetricResult


def _summary(**scores: float) -> EvaluationSummary:
    return EvaluationSummary.from_cases(
        [
            CaseResult(
                case_name="case",
                metrics=[MetricResult(name=name, score=score) for name, score in scores.items()],
            )
        ]
    )


def test_metric_above_its_floor_passes() -> None:
    args = parse_args(["eval_data", "--min", "retrieval_recall@3=1.0"])

    assert check_gates(_summary(**{"retrieval_recall@3": 1.0}), args) == []


def test_metric_below_its_floor_fails() -> None:
    args = parse_args(["eval_data", "--min", "retrieval_recall@3=1.0"])

    violations = check_gates(_summary(**{"retrieval_recall@3": 0.8}), args)

    assert len(violations) == 1
    assert "retrieval_recall@3" in violations[0]


def test_ceiling_gate_catches_a_rising_bad_metric() -> None:
    args = parse_args(["eval_data", "--max", "hallucination_rate=0.2"])

    assert check_gates(_summary(hallucination_rate=0.1), args) == []
    assert check_gates(_summary(hallucination_rate=0.5), args)


def test_gating_an_absent_metric_is_a_failure_not_a_silent_pass() -> None:
    args = parse_args(["eval_data", "--min", "never_computed=0.5"])

    violations = check_gates(_summary(groundedness=1.0), args)

    assert len(violations) == 1
    assert "not produced" in violations[0]


def test_case_errors_fail_the_run_when_requested() -> None:
    args = parse_args(["eval_data", "--require-no-errors"])
    summary = EvaluationSummary.from_cases(
        [CaseResult(case_name="broken", errors=["index: boom"])]
    )

    assert check_gates(summary, args)
    assert check_gates(summary, parse_args(["eval_data"])) == []


def test_malformed_threshold_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["eval_data", "--min", "no_equals_sign"])
    with pytest.raises(SystemExit):
        parse_args(["eval_data", "--min", "metric=not_a_number"])
