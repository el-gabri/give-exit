"""Paired comparison of two evaluation runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.compare import compare_summaries, load_summary, main, render_comparison
from app.schemas.evaluation import (
    CaseResult,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricResult,
    RetrievalEvaluationConfiguration,
)

_SHA = "0" * 64


def _run(**overrides: object) -> EvaluationRunMetadata:
    payload: dict[str, object] = {
        "dataset_id": "seed",
        "dataset_version": "2.2.0",
        "dataset_sha256": _SHA,
        "dataset_review_status": "requires_legal_review",
        "corpus_release_id": "v4",
        "corpus_sha256": _SHA,
        "query_builder_version": "q7",
        "queries_per_case": 2,
        "cutoffs": (8,),
        "retrieval": RetrievalEvaluationConfiguration(retriever_id="r", requested_k=8),
        "agreement_max_rank": 13,
    }
    payload.update(overrides)
    return EvaluationRunMetadata.model_validate(payload)


def _case(name: str, split: str, recall: float, grounds: int, **extra: float) -> CaseResult:
    metrics = [MetricResult(name="recall", score=recall)]
    metrics += [MetricResult(name=metric, score=value) for metric, value in extra.items()]
    return CaseResult(case_name=name, split=split, metrics=metrics, counts={"grounds": grounds})


def _summary(cases: list[CaseResult], **run: object) -> EvaluationSummary:
    return EvaluationSummary.from_cases(cases, run=_run(**run))


def _write(path: Path, summary: EvaluationSummary) -> Path:
    path.write_text(summary.model_dump_json(), encoding="utf-8")
    return path


def test_runs_are_paired_case_by_case() -> None:
    before = _summary(
        [
            _case("a", "development", 0.0, 1),
            _case("b", "development", 0.5, 2),
            _case("gone", "holdout", 1.0, 0),
        ]
    )
    after = _summary(
        [
            _case("a", "development", 1.0, 2),
            _case("b", "development", 0.5, 3),
            _case("new", "holdout", 0.0, 0),
        ],
        agreement_max_rank=12,
    )

    comparison = compare_summaries(before, after)

    assert comparison.pairs == 2
    assert comparison.only_before == ("gone",)
    assert comparison.only_after == ("new",)
    [recall] = comparison.metrics
    assert (recall.name, recall.pairs, recall.before, recall.after) == ("recall", 2, 0.25, 0.75)
    assert recall.difference == 0.5
    assert recall.interval.low <= 0.5 <= recall.interval.high
    [grounds] = comparison.counts
    assert (grounds.before, grounds.after, grounds.difference) == (3, 5, 2)
    assert (grounds.low, grounds.high) == (2.0, 2.0)
    assert comparison.metadata_differences == (("agreement_max_rank", "13", "12"),)
    rendered = render_comparison(comparison)
    assert "only in BEFORE (excluded): gone" in rendered
    assert "only in AFTER (excluded): new" in rendered


def test_a_split_keeps_only_cases_in_that_split_in_both_runs() -> None:
    before = _summary([_case("a", "development", 0.0, 0), _case("moved", "development", 0.0, 0)])
    after = _summary([_case("a", "development", 1.0, 0), _case("moved", "holdout", 1.0, 0)])

    comparison = compare_summaries(before, after, split="development")

    assert comparison.pairs == 1
    assert comparison.only_before == ("moved",)


def test_metrics_one_run_never_reports_are_named() -> None:
    before = _summary([_case("a", "development", 0.0, 0)])
    after = _summary([_case("a", "development", 0.0, 0, precision=0.5)])

    comparison = compare_summaries(before, after)

    assert [item.name for item in comparison.metrics] == ["recall"]
    assert comparison.one_sided_metrics == ("precision",)
    assert "reported by one run only: precision" in render_comparison(comparison)



def test_counts_and_metrics_one_run_cannot_pair_are_named_not_invented() -> None:
    # A result file written before a count existed must not read as 0 -> N.
    before = EvaluationSummary.from_cases(
        [
            CaseResult(
                case_name="a",
                metrics=[MetricResult(name="precision", score=0.5)],
                counts={"grounds": 1},
            ),
            CaseResult(case_name="b", counts={"grounds": 0}),
        ]
    )
    after = EvaluationSummary.from_cases(
        [
            CaseResult(case_name="a", counts={"grounds": 2, "labelled": 1}),
            CaseResult(
                case_name="b",
                metrics=[MetricResult(name="precision", score=1.0)],
                counts={"grounds": 1, "labelled": 1},
            ),
        ]
    )

    comparison = compare_summaries(before, after)

    assert [count.name for count in comparison.counts] == ["grounds"]
    assert comparison.one_sided_counts == ("labelled",)
    assert comparison.metrics == ()
    assert comparison.unpaired_metrics == ("precision",)
    rendered = render_comparison(comparison)
    assert "counts reported by one run only: labelled" in rendered
    assert "reported by both runs but never for the same case: precision" in rendered

def test_runs_without_metadata_or_counts_compare_too() -> None:
    plain = EvaluationSummary.from_cases(
        [CaseResult(case_name="a", metrics=[MetricResult(name="recall", score=1.0)])]
    )

    comparison = compare_summaries(plain, plain)

    assert comparison.metadata_differences == ()
    assert comparison.counts == ()
    assert "count" not in render_comparison(comparison).split()
    with pytest.raises(ValueError, match="in the holdout split"):
        compare_summaries(plain, plain, split="holdout")


def test_runs_without_a_shared_case_cannot_be_compared() -> None:
    with pytest.raises(ValueError, match="share no case"):
        compare_summaries(
            _summary([_case("a", "development", 0.0, 0)]),
            _summary([_case("b", "development", 0.0, 0)]),
        )


def test_a_result_written_before_splits_existed_still_loads(tmp_path: Path) -> None:
    legacy = _summary([_case("a", "development", 0.5, 1)]).model_dump(mode="json")
    for case in legacy["cases"]:
        del case["split"]
    del legacy["by_split"], legacy["intervals"]
    legacy["run"].pop("case_split")
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    summary = load_summary(path)

    assert summary.cases[0].split is None
    assert compare_summaries(summary, summary).pairs == 1


def test_cli_prints_the_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _write(tmp_path / "before.json", _summary([_case("a", "development", 0.0, 1)]))
    after = _write(
        tmp_path / "after.json",
        _summary([_case("a", "development", 1.0, 2)], agreement_max_rank=12),
    )

    assert main([str(before), str(after)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "pairs: 1"
    assert "changed agreement_max_rank: 13 -> 12" in lines
    assert any(line.startswith("recall") and "+1.000" in line for line in lines)
    assert any(line.startswith("grounds") and "+1" in line for line in lines)


def test_cli_refuses_sweep_files_and_disjoint_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sweep = tmp_path / "sweep.json"
    sweep.write_text(json.dumps({"agreement_sweep": {}}), encoding="utf-8")
    one = _write(tmp_path / "one.json", _summary([_case("a", "development", 0.0, 0)]))
    other = _write(tmp_path / "other.json", _summary([_case("b", "development", 0.0, 0)]))

    assert main([str(sweep), str(one)]) == 2
    assert "sweep files are not supported" in capsys.readouterr().err
    assert main([str(one), str(other)]) == 2
    assert "share no case" in capsys.readouterr().err
