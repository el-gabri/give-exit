"""Persistent record of pipeline runs (JSONL).

Append-only JSONL keeps this dependency-free and greppable; the API and UI
read it to show run history and cost dashboards. Swapping to Postgres or a
telemetry backend later means implementing these three methods elsewhere.
"""

import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.core.logging import get_logger
from app.schemas.report import RunMetrics
from app.schemas.review import HumanReviewDecision
from app.schemas.trace import AgentTrace, RetrievalTrace, RetrievedItemTrace
from app.security.telemetry import TelemetryRedactor

logger = get_logger(__name__)

_SAFE_ISSUE_TYPE = re.compile(r"^[a-z0-9_.]{1,80}$")
_ERROR_TYPE_PREFIX = re.compile(
    r"^(?P<error_type>[A-Za-z_][A-Za-z0-9_]{0,79}(?:Error|Exception))(?::|$)"
)


def _error_type_marker(error: str) -> str:
    """Reduce arbitrary exception text to a bounded diagnostic category."""
    match = _ERROR_TYPE_PREFIX.match(error.strip())
    error_type = match.group("error_type") if match is not None else "unclassified"
    return f"[ERROR_TYPE:{error_type}]"


class RunRecord(BaseModel):
    """One pipeline execution, as persisted."""

    run_id: str
    doc_id: str
    filename: str
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool
    outcome: str | None = None
    errors: list[str] = Field(default_factory=list)
    metrics: RunMetrics
    traces: list[AgentTrace] = Field(default_factory=list)
    review: HumanReviewDecision | None = None


_SAFE_VALIDATION_LOCATIONS = frozenset(
    RunRecord.model_fields
    | RunMetrics.model_fields
    | HumanReviewDecision.model_fields
    | AgentTrace.model_fields
    | RetrievalTrace.model_fields
    | RetrievedItemTrace.model_fields
)


class RunStore:
    """Append-only JSONL store of RunRecords."""

    def __init__(
        self,
        path: Path,
        telemetry_redactor: TelemetryRedactor | None = None,
    ) -> None:
        self._path = path
        self._telemetry = telemetry_redactor or TelemetryRedactor()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: RunRecord) -> None:
        safe_record = self._privacy_safe_record(record)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(safe_record.model_dump_json() + "\n")

    def _privacy_safe_record(self, record: RunRecord) -> RunRecord:
        """Minimize personal data before it enters the durable JSONL ledger."""

        review = record.review
        if review is not None:
            review = review.model_copy(
                update={
                    "reviewer": self._telemetry.reference(
                        review.reviewer, namespace="reviewer"
                    ),
                    # Reviewer comments are unconstrained natural language and
                    # cannot be made safe by identifier regexes. Keep them only
                    # in the live job; the durable decision metadata is enough
                    # to prove who decided what and against which assessment.
                    "comment": None,
                }
            )
        return record.model_copy(
            update={
                "filename": self._telemetry.filename_reference(record.filename),
                # Exception messages are arbitrary text and may reproduce case
                # facts. Persist only a bounded type marker; the live job keeps
                # the full diagnostic during its configured retention window.
                "errors": [_error_type_marker(error) for error in record.errors],
                "traces": [self._privacy_safe_trace(trace) for trace in record.traces],
                "review": review,
            }
        )

    def _privacy_safe_trace(self, trace: AgentTrace) -> AgentTrace:
        return trace.model_copy(
            update={
                "error": (
                    _error_type_marker(trace.error)
                    if trace.error is not None
                    else None
                ),
                "retrievals": [
                    self._privacy_safe_retrieval(item) for item in trace.retrievals
                ],
            }
        )

    def _privacy_safe_retrieval(self, trace: RetrievalTrace) -> RetrievalTrace:
        return trace.model_copy(
            update={
                # The digest already supports correlation and integrity checks;
                # persisting the natural-language query would duplicate case
                # allegations and identifiers in a second data store.
                "query": f"[QUERY_REDACTED:{trace.query_sha256[:12]}]",
                "error": (
                    _error_type_marker(trace.error)
                    if trace.error is not None
                    else None
                ),
                "agent_error": (
                    _error_type_marker(trace.agent_error)
                    if trace.agent_error is not None
                    else None
                ),
                "results": [
                    self._privacy_safe_result(item) for item in trace.results
                ],
            }
        )

    def _privacy_safe_result(self, item: RetrievedItemTrace) -> RetrievedItemTrace:
        # Section labels and previews are arbitrary source text. Durable
        # retrieval audit retains hashes, locations and ranks, while live
        # in-memory traces remain available for short-lived diagnostics.
        return item.model_copy(
            update={
                "section": None,
                "text_preview": None,
                # Arbitrary metadata values may originate in uploaded or
                # future external sources. Dedicated source_* fields, hashes,
                # page location and ranking retain the auditable provenance.
                "source_metadata": {},
            }
        )

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        """Return the most recent runs, one entry per run id.

        A run id can be written more than once (a review decision appends a
        new record), so only its latest state is reported.
        """
        latest: dict[str, RunRecord] = {}
        for record in self._records():
            current = latest.get(record.run_id)
            if current is None or record.finished_at >= current.finished_at:
                latest[record.run_id] = record
        return sorted(latest.values(), key=lambda r: r.finished_at, reverse=True)[:limit]

    def get(self, run_id: str) -> RunRecord | None:
        """Return the newest durable record for a run id.

        A run can be written more than once - a human review decision appends
        a new record under the same id - so the last match wins.
        """
        for record in self._records(reverse=True):
            if record.run_id == run_id:
                return record
        return None

    def _records(self, *, reverse: bool = False) -> Iterator[RunRecord]:
        """Yield valid records while isolating damage to individual JSONL lines.

        Forward iteration streams the file so a long history is never held in
        memory at once. Reverse iteration has to buffer, and is only used for
        single-record lookups.
        """
        if not self._path.exists():
            return

        if reverse:
            with self._path.open("r", encoding="utf-8") as handle:
                indexed_lines = list(enumerate(handle.read().splitlines(), start=1))
            indexed_lines.reverse()
            for line_number, line in indexed_lines:
                if line.strip():
                    record = self._parse_line(line, line_number=line_number)
                    if record is not None:
                        yield record
            return

        with self._path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                record = self._parse_line(line, line_number=line_number)
                if record is not None:
                    yield record

    def _parse_line(self, line: str, *, line_number: int) -> RunRecord | None:
        """Parse one record, preserving compatibility through Pydantic defaults."""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            self._warn_skipped_line(line_number, "invalid_json", exc)
            return None

        try:
            return RunRecord.model_validate(payload)
        except ValidationError as exc:
            self._warn_skipped_line(line_number, "validation_error", exc)
            return None

    def _warn_skipped_line(
        self, line_number: int, reason: str, error: Exception
    ) -> None:
        validation_issues: list[dict[str, object]] = []
        if isinstance(error, ValidationError):
            for issue in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ):
                raw_type = issue.get("type")
                issue_type = (
                    raw_type
                    if isinstance(raw_type, str)
                    and _SAFE_ISSUE_TYPE.fullmatch(raw_type)
                    else "validation_error"
                )
                location = []
                for component in issue.get("loc", ()):
                    if isinstance(component, int) or (
                        isinstance(component, str)
                        and component in _SAFE_VALIDATION_LOCATIONS
                    ):
                        location.append(component)
                    else:
                        location.append("<field>")
                validation_issues.append(
                    {"type": issue_type, "location": location}
                )
        logger.warning(
            "run_store_line_skipped",
            path_ref=self._telemetry.reference(
                str(self._path), namespace="run_store_path"
            ),
            line_number=line_number,
            reason=reason,
            exception_type=type(error).__name__,
            validation_issues=validation_issues,
        )

    def totals(self) -> dict[str, Any]:
        """Aggregate cost/token totals across the whole history.

        Streams the ledger and keeps only the latest record per run id, so
        neither history length nor repeated writes distort the totals.
        """
        latest: dict[str, RunRecord] = {}
        for record in self._records():
            current = latest.get(record.run_id)
            if current is None or record.finished_at >= current.finished_at:
                latest[record.run_id] = record
        runs = list(latest.values())
        outcomes = [
            run.outcome or ("succeeded" if run.success else "failed") for run in runs
        ]
        unpriced_calls = sum(r.metrics.unpriced_calls for r in runs)
        return {
            "runs": len(runs),
            "total_cost_usd": round(sum(r.metrics.total_cost_usd for r in runs), 6),
            # Spend for models absent from the price table is not observable,
            # so the total above is a lower bound whenever this is non-zero.
            "cost_is_complete": unpriced_calls == 0,
            "unpriced_calls": unpriced_calls,
            "unpriced_models": sorted(
                {model for r in runs for model in r.metrics.unpriced_models}
            ),
            "total_tokens": sum(r.metrics.total_tokens for r in runs),
            "retrieval_queries": sum(r.metrics.retrieval_queries for r in runs),
            "retrieval_results": sum(r.metrics.retrieval_results for r in runs),
            "retrieval_duration_ms": round(
                sum(r.metrics.retrieval_duration_ms for r in runs), 1
            ),
            "failures": sum(
                outcome in {"failed", "partial"} for outcome in outcomes
            ),
            "blocked": outcomes.count("blocked"),
            "review_required": outcomes.count("review_required"),
            "rejected": outcomes.count("rejected"),
        }
