"""In-process async job manager.

Why in-process (see ADR 0009): a single-node deployment needs no broker.
The manager's public surface (submit / get) is broker-agnostic, so moving
to Redis + workers later replaces this class, not its callers.

Progress tracking uses LangGraph value streaming: after each superstep we
inspect which state fields became non-null and mark the corresponding
pipeline stage as done - the UI polls this to animate agent execution.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile

from app.api.schemas import JobState, StageState, StageStatus
from app.core.logging import get_logger
from app.ingestion.service import DocumentIngestionService
from app.observability.store import RunRecord, RunStore
from app.orchestration.state import AnalysisState
from app.rag.pipeline import RagPipeline
from app.schemas.report import EvidenceQualityStatus, RunMetrics
from app.schemas.review import HumanReviewDecision
from app.schemas.security import PromptInjectionAssessment, SecurityAction
from app.security.telemetry import TelemetryRedactor

logger = get_logger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_HEADER_BYTES = 16


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds the configured size limit."""


class InvalidPdfUploadError(ValueError):
    """Raised when an upload does not contain a PDF header."""


class ReviewNotAllowedError(ValueError):
    """Raised when a review decision targets a job not awaiting review."""


class JobCapacityExceededError(RuntimeError):
    """Raised when all execution and queue slots are already reserved."""


class ReviewPersistenceError(RuntimeError):
    """Raised when an accepted human decision cannot be durably recorded."""


class JobDeadlineExceededError(TimeoutError):
    """Raised when a run exceeds its wall-clock budget."""


# Ordered pipeline stages and the state predicate that marks each as done.
STAGE_PREDICATES: list[tuple[str, str]] = [
    ("security_scan", "security_assessment"),
    ("index", "chunks"),
    ("classify", "classification"),
    ("extract", "extraction"),
    ("analyze", "legal_analysis"),
    ("enrich", "enrichment"),
    ("risk", "risk"),
    ("strategy", "strategy"),
    ("compose", "report"),
]

ERROR_STAGE_BY_AGENT = {
    "security_scan": "security_scan",
    "index": "index",
    "classifier": "classify",
    "entity_extraction": "extract",
    "legal_analysis": "analyze",
    "risk_assessment": "risk",
    "strategy": "strategy",
}

SECURITY_SKIPPED_STAGES = {
    "index",
    "classify",
    "extract",
    "analyze",
    "enrich",
    "risk",
    "strategy",
}


@dataclass
class Job:
    job_id: str
    filename: str
    state: JobState = JobState.QUEUED
    doc_id: str | None = None
    review: HumanReviewDecision | None = None
    done_stages: set[str] = field(default_factory=set)
    failed_stages: set[str] = field(default_factory=set)
    skipped_stages: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    result: AnalysisState | None = None

    def stages(self) -> list[StageStatus]:
        statuses: list[StageStatus] = []
        running_assigned = False
        for name, _ in STAGE_PREDICATES:
            if name in self.failed_stages:
                state = StageState.FAILED
            elif name in self.done_stages:
                state = StageState.DONE
            elif name in self.skipped_stages:
                state = StageState.SKIPPED
            elif self.state is JobState.RUNNING and not running_assigned:
                state = StageState.RUNNING
                running_assigned = True
            else:
                state = StageState.PENDING
            statuses.append(StageStatus(name=name, state=state))
        return statuses


class AnalysisJobManager:
    """Owns job lifecycle: accept upload, run pipeline, expose status."""

    def __init__(
        self,
        ingestion: DocumentIngestionService,
        graph: object,
        run_store: RunStore,
        uploads_dir: Path,
        retain_uploads: bool = False,
        rag: RagPipeline | None = None,
        retain_index: bool = False,
        job_timeout_seconds: float = 0.0,
        max_retained_jobs: int = 200,
        max_concurrent_jobs: int = 4,
        max_queued_jobs: int = 16,
        max_review_required_jobs: int = 20,
        review_required_ttl_seconds: float = 86_400.0,
        telemetry_redactor: TelemetryRedactor | None = None,
    ) -> None:
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be positive")
        if max_queued_jobs < 0:
            raise ValueError("max_queued_jobs cannot be negative")
        if max_review_required_jobs < 1:
            raise ValueError("max_review_required_jobs must be positive")
        if review_required_ttl_seconds <= 0:
            raise ValueError("review_required_ttl_seconds must be positive")
        self._ingestion = ingestion
        self._graph = graph
        self._run_store = run_store
        self._uploads_dir = uploads_dir
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._durably_persisted_job_ids: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._retain_uploads = retain_uploads
        self._rag = rag
        self._retain_index = retain_index
        self._job_timeout_seconds = job_timeout_seconds
        self._max_retained_jobs = max(1, max_retained_jobs)
        self._max_concurrent_jobs = max_concurrent_jobs
        self._max_queued_jobs = max_queued_jobs
        self._max_review_required_jobs = max_review_required_jobs
        self._review_required_ttl_seconds = review_required_ttl_seconds
        self._execution_slots = asyncio.Semaphore(max_concurrent_jobs)
        self._reserved_executions = 0
        self._running_executions = 0
        self._telemetry = telemetry_redactor or TelemetryRedactor()

    def get(self, job_id: str) -> Job | None:
        self._evict_review_required_jobs()
        return self._jobs.get(job_id)

    def _reserve_execution(self) -> None:
        capacity = self._max_concurrent_jobs + self._max_queued_jobs
        if self._reserved_executions >= capacity:
            logger.warning(
                "job_capacity_exceeded",
                running=self._running_executions,
                reserved=self._reserved_executions,
                capacity=capacity,
            )
            raise JobCapacityExceededError(
                "Analysis capacity is full; retry after a running job finishes"
            )
        # submit_upload/review run on one event loop and do not await between
        # this check and increment, so the reservation is atomic here.
        self._reserved_executions += 1

    def _release_execution(self) -> None:
        if self._reserved_executions < 1:  # pragma: no cover - internal invariant
            raise RuntimeError("execution capacity released without a reservation")
        self._reserved_executions -= 1

    async def _dispatch_upload(self, job: Job, pdf_path: Path) -> None:
        acquired = False
        try:
            await self._execution_slots.acquire()
            acquired = True
            self._running_executions += 1
            await self._execute(job, pdf_path)
        except asyncio.CancelledError:
            if not acquired:
                job.errors.append("CancelledError: queued analysis was cancelled")
                job.state = JobState.FAILED
                self._finalize(job)
                if not self._retain_uploads:
                    await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
            raise
        finally:
            if acquired:
                self._running_executions -= 1
                self._execution_slots.release()
            self._release_execution()

    async def _dispatch_resume(
        self, job: Job, initial_state: AnalysisState
    ) -> None:
        acquired = False
        try:
            await self._execution_slots.acquire()
            acquired = True
            self._running_executions += 1
            await self._resume(job, initial_state)
        except asyncio.CancelledError:
            if not acquired:
                job.errors.append("CancelledError: queued review resume was cancelled")
                job.state = JobState.FAILED
                self._finalize(job)
            raise
        finally:
            if acquired:
                self._running_executions -= 1
                self._execution_slots.release()
            self._release_execution()

    def _evict_finished_jobs(self) -> None:
        """Drop the oldest finished jobs once the registry exceeds its cap.

        Each job holds a whole AnalysisState, including every chunk of the
        document, so an unbounded registry is a slow memory leak. Durable run
        history lives in the run store, and its retrieval traces stay
        reachable at /runs/{run_id}/retrievals after eviction.
        """
        finished = sorted(
            (
                job
                for job in self._jobs.values()
                if job.finished_at is not None
                and job.state is not JobState.REVIEW_REQUIRED
            ),
            key=lambda job: job.finished_at or job.created_at,
        )
        excess = len(self._jobs) - self._max_retained_jobs
        for job in finished[: max(0, excess)]:
            del self._jobs[job.job_id]
            logger.info("job_evicted", job_id=job.job_id)

    def _evict_review_required_jobs(
        self, *, now: datetime | None = None
    ) -> None:
        """Bound halted jobs after their durable run record has been written.

        REVIEW_REQUIRED jobs retain their complete AnalysisState for a possible
        resume, so they have a dedicated TTL and capacity bound. Eviction only
        drops the live object; the append-only RunStore remains the audit
        source for the completed security-gated attempt.
        """
        current_time = now or datetime.now(timezone.utc)
        pending = sorted(
            (
                job
                for job in self._jobs.values()
                if job.state is JobState.REVIEW_REQUIRED
                and job.finished_at is not None
                and job.job_id in self._durably_persisted_job_ids
            ),
            key=lambda job: (job.finished_at or job.created_at, job.job_id),
        )
        expired = {
            job.job_id
            for job in pending
            if (current_time - (job.finished_at or job.created_at)).total_seconds()
            >= self._review_required_ttl_seconds
        }
        survivors = [job for job in pending if job.job_id not in expired]
        over_capacity = max(0, len(survivors) - self._max_review_required_jobs)
        capacity_evictions = {job.job_id for job in survivors[:over_capacity]}

        for job in pending:
            reason: str | None = None
            if job.job_id in expired:
                reason = "expired"
            elif job.job_id in capacity_evictions:
                reason = "capacity"
            if reason is not None and self._jobs.pop(job.job_id, None) is not None:
                logger.info(
                    "review_required_job_evicted",
                    job_id=job.job_id,
                    reason=reason,
                )

    async def submit_upload(self, filename: str, file: UploadFile, max_upload_bytes: int) -> Job:
        """Persist an upload in bounded chunks, then queue its analysis.

        The upload is intentionally written before a job is exposed. This
        prevents arbitrary-sized requests from being materialized in memory
        and ensures failed validation leaves no visible job behind.
        """
        self._evict_review_required_jobs()
        self._reserve_execution()
        reservation_transferred = False
        job = Job(job_id=uuid.uuid4().hex, filename=filename)
        pdf_path = self._uploads_dir / f"{job.job_id}.pdf"
        try:
            header = await _write_upload_in_chunks(
                file=file, path=pdf_path, max_upload_bytes=max_upload_bytes
            )
            if not header.startswith(b"%PDF"):
                raise InvalidPdfUploadError("File is not a valid PDF")

            self._jobs[job.job_id] = job
            self._evict_finished_jobs()
            task = asyncio.create_task(self._dispatch_upload(job, pdf_path))
            reservation_transferred = True
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except BaseException:
            await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
            raise
        finally:
            if not reservation_transferred:
                self._release_execution()

        logger.info(
            "job_submitted",
            job_id=job.job_id,
            file_ref=self._telemetry.filename_reference(filename),
        )
        return job

    def review(
        self,
        job_id: str,
        *,
        approved: bool,
        reviewer: str,
        comment: str | None = None,
    ) -> Job | None:
        """Apply a reviewer's decision to a job halted as review_required.

        Approval re-runs the pipeline with the decision in the graph state so
        the security router lets analysis proceed with findings still masked.
        Each decision appends a new record to the run ledger under the same
        run id; readers see the latest outcome.
        """
        self._evict_review_required_jobs()
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.state is not JobState.REVIEW_REQUIRED or job.result is None:
            raise ReviewNotAllowedError("job is not awaiting human review")
        assessment = job.result.security_assessment
        if (
            assessment is None
            or not assessment.scan_complete
            or assessment.recommended_action is not SecurityAction.HUMAN_REVIEW
        ):
            raise ReviewNotAllowedError(
                "job does not contain a completed human-review security assessment"
            )

        if approved:
            self._reserve_execution()

        try:
            decision = HumanReviewDecision(
                job_id=job.job_id,
                doc_id=job.result.document.doc_id,
                security_assessment_sha256=_security_assessment_sha256(assessment),
                approved=approved,
                reviewer=reviewer,
                comment=comment,
            )
            self._persist_review_decision(job, decision)
        except Exception as exc:
            if approved:
                self._release_execution()
            logger.error(
                "review_decision_persist_failed",
                job_id=job.job_id,
                exception_type=type(exc).__name__,
            )
            raise ReviewPersistenceError(
                "Human review decision could not be durably recorded"
            ) from exc

        job.review = decision
        if not approved:
            job.state = JobState.REJECTED
            self._finalize(job)
            logger.info(
                "job_review_rejected",
                job_id=job.job_id,
                reviewer_ref=self._telemetry.reference(reviewer, namespace="reviewer"),
            )
            return job

        document = job.result.document
        job.state = JobState.QUEUED
        job.finished_at = None
        job.done_stages.clear()
        job.failed_stages.clear()
        job.skipped_stages.clear()
        job.errors = []
        job.result = None
        try:
            task = asyncio.create_task(
                self._dispatch_resume(
                    job, AnalysisState(document=document, human_review=decision)
                )
            )
        except Exception as exc:  # pragma: no cover - requires a broken event loop
            self._release_execution()
            job.errors.append(f"{type(exc).__name__}: review resume was not scheduled")
            job.state = JobState.FAILED
            self._finalize(job)
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            "job_review_approved",
            job_id=job.job_id,
            reviewer_ref=self._telemetry.reference(reviewer, namespace="reviewer"),
        )
        return job

    async def _execute(self, job: Job, pdf_path: Path) -> None:
        async def run() -> None:
            document = await self._ingestion.ingest(pdf_path)
            job.doc_id = document.doc_id
            await self._run_graph(job, AnalysisState(document=document))

        try:
            await self._run_guarded(job, run())
        finally:
            self._finalize(job)
            if not self._retain_uploads:
                await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
            await self._delete_index(job, job.doc_id)

    async def _resume(self, job: Job, initial_state: AnalysisState) -> None:
        try:
            await self._run_guarded(job, self._run_graph(job, initial_state))
        finally:
            self._finalize(job)
            await self._delete_index(job, job.doc_id)

    async def _run_guarded(self, job: Job, work: Awaitable[None]) -> None:
        """Run one attempt under the deadline, recording failure on the job."""
        job.state = JobState.RUNNING
        try:
            if self._job_timeout_seconds > 0:
                await asyncio.wait_for(work, timeout=self._job_timeout_seconds)
            else:
                await work
        except asyncio.CancelledError:
            job.errors.append("CancelledError: analysis execution was cancelled")
            job.state = JobState.FAILED
            raise
        except TimeoutError:
            # Whatever the stream already produced stays on the job, so the
            # status still shows which stages completed before the cutoff.
            logger.warning(
                "job_deadline_exceeded",
                job_id=job.job_id,
                timeout_s=self._job_timeout_seconds,
            )
            error = JobDeadlineExceededError(
                f"analysis exceeded the {self._job_timeout_seconds:g}s budget"
            )
            job.errors.append(f"{type(error).__name__}: {error}")
            job.state = JobState.FAILED
        except Exception as exc:
            logger.error(
                "job_crashed",
                job_id=job.job_id,
                exception_type=type(exc).__name__,
            )
            job.errors.append(f"{type(exc).__name__}: {exc}")
            job.state = JobState.FAILED

    async def _run_graph(self, job: Job, initial_state: AnalysisState) -> None:
        async for chunk in self._graph.astream(  # type: ignore[attr-defined]
            initial_state, stream_mode="values"
        ):
            state = AnalysisState(**chunk) if isinstance(chunk, dict) else chunk
            job.result = state
            self._update_stages(job, state)
        job.errors = list(job.result.errors) if job.result else ["no result"]
        job.state = _final_state(job, job.result)

    def _finalize(self, job: Job) -> None:
        job.finished_at = datetime.now(timezone.utc)
        persisted = self._persist_run(job)
        if job.state is JobState.REVIEW_REQUIRED and not persisted:
            # A resumable security halt contains the full parsed document. If
            # its audit record cannot be written, keeping that state would let
            # a disk outage bypass both retention governance and auditability.
            # Fail closed and discard the rich state; the bounded status record
            # remains available to tell the client to resubmit after recovery.
            job.state = JobState.FAILED
            job.result = None
            job.errors = [
                "RunPersistenceError: review-required state was not persisted"
            ]
            logger.error(
                "review_required_persistence_failed_closed",
                job_id=job.job_id,
            )
        # Persist first so memory pressure or TTL never discards audit history.
        self._evict_review_required_jobs(now=job.finished_at)
        self._evict_finished_jobs()

    async def _delete_index(self, job: Job, doc_id: str | None) -> None:
        """Remove the document's chunks once the run no longer queries them.

        The index is content-addressed, so a concurrent job analyzing the very
        same file shares the doc_id; deletion is skipped while such a sibling
        is still running.
        """
        if self._retain_index or self._rag is None or doc_id is None:
            return
        for other in self._jobs.values():
            if (
                other.job_id != job.job_id
                and other.state in (JobState.QUEUED, JobState.RUNNING)
                and other.doc_id == doc_id
            ):
                return
        try:
            await self._rag.delete_document(doc_id)
        except Exception as exc:
            logger.error(
                "index_cleanup_failed",
                job_id=job.job_id,
                doc_id=doc_id,
                exception_type=type(exc).__name__,
            )

    def _update_stages(self, job: Job, state: AnalysisState) -> None:
        for stage_name, state_field in STAGE_PREDICATES:
            value = getattr(state, state_field)
            if value:
                job.done_stages.add(stage_name)
        assessment = state.security_assessment
        review_approved = state.human_review is not None and state.human_review.approved
        if (
            state.report
            and assessment is not None
            and not assessment.recommended_action.allows_automated_analysis
            and not review_approved
        ):
            job.skipped_stages.update(SECURITY_SKIPPED_STAGES)
        for error in state.errors:
            agent_name = error.split(":", maxsplit=1)[0]
            if failed_stage := ERROR_STAGE_BY_AGENT.get(agent_name):
                job.failed_stages.add(failed_stage)

    def _persist_run(self, job: Job) -> bool:
        try:
            self._run_store.append(self._build_run_record(job))
            self._durably_persisted_job_ids.add(job.job_id)
        except Exception as exc:
            logger.error(
                "run_persist_failed",
                job_id=job.job_id,
                exception_type=type(exc).__name__,
            )
            return False
        return True

    def _persist_review_decision(
        self, job: Job, decision: HumanReviewDecision
    ) -> None:
        """Append the immutable decision before applying its state transition."""
        outcome = "review_approved" if decision.approved else "review_rejected"
        self._run_store.append(
            self._build_run_record(job, outcome=outcome, review=decision)
        )
        self._durably_persisted_job_ids.add(job.job_id)

    @staticmethod
    def _build_run_record(
        job: Job,
        *,
        outcome: str | None = None,
        review: HumanReviewDecision | None = None,
    ) -> RunRecord:
        metrics = job.result.report.metrics if job.result and job.result.report else RunMetrics()
        traces = job.result.traces if job.result else []
        effective_review = review or job.review
        doc_id = (
            job.result.document.doc_id
            if job.result
            else job.doc_id or (effective_review.doc_id if effective_review else "")
        )
        effective_outcome = outcome or job.state.value
        return RunRecord(
            run_id=job.job_id,
            doc_id=doc_id,
            filename=job.filename,
            success=effective_outcome == JobState.SUCCEEDED.value,
            outcome=effective_outcome,
            errors=list(job.errors),
            metrics=metrics,
            traces=traces,
            review=effective_review,
        )

def _security_assessment_sha256(assessment: PromptInjectionAssessment) -> str:
    payload = json.dumps(
        assessment.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _final_state(job: Job, last_state: AnalysisState | None) -> JobState:
    """Map the terminal graph state onto the job outcome.

    Only complete scans drive review/block outcomes, and an approved human
    review bypasses HUMAN_REVIEW alone - a BLOCK verdict always stands.
    """
    if not (last_state and last_state.report):
        return JobState.FAILED
    assessment = last_state.security_assessment
    if assessment is not None and assessment.scan_complete:
        action = assessment.recommended_action
        if action is SecurityAction.BLOCK:
            return JobState.BLOCKED
        review_approved = (
            last_state.human_review is not None and last_state.human_review.approved
        )
        if action is SecurityAction.HUMAN_REVIEW and not review_approved:
            return JobState.REVIEW_REQUIRED
    if job.errors:
        return JobState.PARTIAL
    if (
        last_state.report.evidence_quality.status
        is EvidenceQualityStatus.HUMAN_REVIEW_REQUIRED
    ):
        return JobState.PARTIAL
    return JobState.SUCCEEDED


async def _write_upload_in_chunks(file: UploadFile, path: Path, max_upload_bytes: int) -> bytes:
    """Write an UploadFile without ever holding more than one chunk in memory."""
    total_bytes = 0
    header = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        total_bytes += len(chunk)
        if total_bytes > max_upload_bytes:
            raise UploadTooLargeError(f"File exceeds {max_upload_bytes} bytes")
        if len(header) < UPLOAD_HEADER_BYTES:
            header.extend(chunk[: UPLOAD_HEADER_BYTES - len(header)])
        await asyncio.to_thread(_append_bytes, path, chunk)
    return bytes(header)


def _append_bytes(path: Path, chunk: bytes) -> None:
    with path.open("ab") as destination:
        destination.write(chunk)
