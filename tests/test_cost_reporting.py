"""Cost totals must disclose spend they cannot observe."""

from pathlib import Path

from app.llm.base import LLMCallMetadata, TokenUsage
from app.observability.store import RunRecord, RunStore
from app.schemas.report import RunMetrics
from app.schemas.trace import AgentStatus, AgentTrace
from app.services.composer import _build_metrics


def _trace(model: str, cost: float | None) -> AgentTrace:
    return AgentTrace(
        agent="classifier",
        status=AgentStatus.SUCCESS,
        duration_ms=10.0,
        llm_meta=LLMCallMetadata(
            provider="openai",
            model=model,
            latency_ms=10.0,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
            cost_usd=cost,
        ),
    )


def test_unpriced_calls_are_counted_not_silently_zeroed() -> None:
    metrics = _build_metrics(
        [_trace("gpt-4o-mini", 0.01), _trace("some-future-model", None)]
    )

    assert metrics.total_cost_usd == 0.01
    assert metrics.unpriced_calls == 1
    assert metrics.unpriced_models == ["some-future-model"]


def test_fully_priced_run_reports_no_gap() -> None:
    metrics = _build_metrics([_trace("gpt-4o-mini", 0.01)])

    assert metrics.unpriced_calls == 0
    assert metrics.unpriced_models == []


def test_totals_flag_an_incomplete_cost_picture(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(
        RunRecord(
            run_id="a",
            doc_id="d",
            filename="x.pdf",
            success=True,
            metrics=RunMetrics(
                total_cost_usd=0.01,
                unpriced_calls=2,
                unpriced_models=["some-future-model"],
            ),
        )
    )

    totals = store.totals()

    assert totals["cost_is_complete"] is False
    assert totals["unpriced_calls"] == 2
    assert totals["unpriced_models"] == ["some-future-model"]


def test_totals_report_a_complete_cost_picture_when_all_models_are_priced(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(
        RunRecord(
            run_id="a",
            doc_id="d",
            filename="x.pdf",
            success=True,
            metrics=RunMetrics(total_cost_usd=0.01),
        )
    )

    totals = store.totals()

    assert totals["cost_is_complete"] is True
    assert totals["unpriced_calls"] == 0
