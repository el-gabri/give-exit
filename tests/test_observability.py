"""Tests for the run store."""

import json
from pathlib import Path

from app.observability import store as store_module
from app.observability.store import RunRecord, RunStore
from app.schemas.report import RunMetrics
from app.schemas.trace import (
    AgentStatus,
    AgentTrace,
    RetrievalTrace,
    RetrievedItemTrace,
)


def _record(run_id: str, cost: float, success: bool = True) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        doc_id=f"doc-{run_id}",
        filename="x.pdf",
        success=success,
        metrics=RunMetrics(total_cost_usd=cost, total_tokens=100, agents_run=5),
    )


def test_run_store_roundtrip_and_totals(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record("a", 0.01))
    store.append(_record("b", 0.02, success=False))

    runs = store.list_runs()
    assert len(runs) == 2
    assert {r.run_id for r in runs} == {"a", "b"}

    totals = store.totals()
    assert totals["runs"] == 2
    assert totals["failures"] == 1
    assert totals["total_cost_usd"] == 0.03
    assert totals["total_tokens"] == 200


def test_rewritten_run_is_counted_once_at_its_latest_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record("a", 0.01, success=False))
    # A human review decision appends a second record under the same run id.
    reviewed = _record("a", 0.02)
    reviewed.outcome = "succeeded"
    store.append(reviewed)

    runs = store.list_runs()
    totals = store.totals()

    assert len(runs) == 1
    assert runs[0].outcome == "succeeded"
    assert totals["runs"] == 1
    assert totals["total_cost_usd"] == 0.02
    assert totals["failures"] == 0
    assert store.get("a") is not None
    assert store.get("a").outcome == "succeeded"


def test_empty_store(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    assert store.list_runs() == []
    assert store.totals()["runs"] == 0


def test_run_store_persists_retrieval_audit_and_reads_old_records(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    retrieval = RetrievalTrace(
        batch_id="batch-1",
        agent="legal_analysis",
        doc_id="doc-a",
        query_index=0,
        query="danos morais",
        query_sha256="a" * 64,
        requested_k=3,
        returned_count=1,
        embedding_model="mock-v1",
        vector_store="InMemoryVectorStore",
        index_version="section-aware-v1",
        results=[
            RetrievedItemTrace(
                rank=1,
                chunk_id="doc-a:0001",
                doc_id="doc-a",
                page_start=2,
                page_end=2,
                score=0.91,
                content_sha256="b" * 64,
                text_preview="pedido de indenizacao",
                selected_for_merge=True,
                merged_rank=1,
                included_in_context=True,
            )
        ],
    )
    record = _record("audit", 0.01).model_copy(
        update={
            "traces": [
                AgentTrace(
                    agent="legal_analysis",
                    status=AgentStatus.SUCCESS,
                    retrievals=[retrieval],
                )
            ]
        }
    )
    store.append(record)

    loaded = store.get("audit")

    assert loaded is not None
    assert loaded.traces[0].retrievals[0] == retrieval
    assert store.get("missing") is None


def test_security_outcomes_are_not_counted_as_successes_or_failures(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record("blocked", 0.0).model_copy(update={"outcome": "blocked"}))
    store.append(
        _record("review", 0.0).model_copy(update={"outcome": "review_required"})
    )

    totals = store.totals()

    assert totals["blocked"] == 1
    assert totals["review_required"] == 1
    assert totals["failures"] == 0


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))


def test_reads_legacy_record_and_skips_corrupt_final_line(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "runs.jsonl"
    legacy = {
        "run_id": "legacy",
        "doc_id": "doc-legacy",
        "filename": "legacy.pdf",
        "finished_at": "2025-01-01T12:00:00Z",
        "success": True,
        "metrics": {
            "total_duration_ms": 125.0,
            "total_tokens": 42,
            "total_cost_usd": 0.001,
            "models_used": ["legacy-model"],
            "prompt_versions": ["legacy:1"],
            "agents_run": 1,
        },
    }
    path.write_text(
        json.dumps(legacy) + '\n{"run_id": "truncated"', encoding="utf-8"
    )
    recording_logger = _RecordingLogger()
    monkeypatch.setattr(store_module, "logger", recording_logger)
    store = RunStore(path)

    runs = store.list_runs()
    loaded = store.get("legacy")

    assert [record.run_id for record in runs] == ["legacy"]
    assert loaded is not None
    assert loaded.outcome is None
    assert loaded.errors == []
    assert loaded.traces == []
    assert loaded.metrics.retrieval_queries == 0
    assert any(
        event == "run_store_line_skipped"
        and fields["reason"] == "invalid_json"
        and fields["line_number"] == 2
        for event, fields in recording_logger.events
    )


def test_skips_schema_invalid_record_with_warning(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_text(
        _record("valid", 0.01).model_dump_json()
        + "\n"
        + json.dumps({"run_id": "missing-required-fields"})
        + "\n",
        encoding="utf-8",
    )
    recording_logger = _RecordingLogger()
    monkeypatch.setattr(store_module, "logger", recording_logger)

    runs = RunStore(path).list_runs()

    assert [record.run_id for record in runs] == ["valid"]
    assert any(
        event == "run_store_line_skipped"
        and fields["reason"] == "validation_error"
        and fields["line_number"] == 2
        for event, fields in recording_logger.events
    )
