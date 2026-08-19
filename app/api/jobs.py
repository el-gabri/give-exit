"""In-process async job manager.

Why in-process (see ADR 0009): a single-node deployment needs no broker.
The manager's public surface (submit / get) is broker-agnostic, so moving
to Redis + workers later replaces this class, not its callers.

Progress tracking uses LangGraph value streaming: after each superstep we
inspect which state fields became non-null and mark the corresponding
pipeline stage as done - the UI polls this to animate agent execution.
"""

import asyncio
import uuid
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
from app.schemas.report import RunMetrics
from app.schemas.security import SecurityAction

logger = get_logger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_HEADER_BYTES = 16


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds the configured size limit."""


class InvalidPdfUploadError(ValueError):
    """Raised when an upload does not contain a PDF header."""


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
    ) -> None:
        self._ingestion = ingestion
        self._graph = graph
        self._run_store = run_store
        self._uploads_dir = uploads_dir
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._retain_uploads = retain_uploads
        self._rag = rag
        self._retain_index = retain_index

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def submit_upload(self, filename: str, file: UploadFile, max_upload_bytes: int) -> Job:
        """Persist an upload in bounded chunks, then queue its analysis.

        The upload is intentionally written before a job is exposed. This
        prevents arbitrary-sized requests from being materialized in memory
        and ensures failed validation leaves no visible job behind.
        """
        job = Job(job_id=uuid.uuid4().hex, filename=filename)
        pdf_path = self._uploads_dir / f"{job.job_id}.pdf"
        try:
            header = await _write_upload_in_chunks(
                file=file, path=pdf_path, max_upload_bytes=max_upload_bytes
            )
            if not header.startswith(b"%PDF"):
                raise InvalidPdfUploadError("File is not a valid PDF")
        except Exception:
            await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
            raise

        self._jobs[job.job_id] = job
        task = asyncio.create_task(self._execute(job, pdf_path))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("job_submitted", job_id=job.job_id, filename=filename)
        return job

    async def _execute(self, job: Job, pdf_path: Path) -> None:
        job.state = JobState.RUNNING
        doc_id: str | None = None
        try:
            document = await self._ingestion.ingest(pdf_path)
            doc_id = job.doc_id = document.doc_id
            last_state: AnalysisState | None = None
            async for chunk in self._graph.astream(  # type: ignore[attr-defined]
                AnalysisState(document=document), stream_mode="values"
            ):
                last_state = AnalysisState(**chunk) if isinstance(chunk, dict) else chunk
                self._update_stages(job, last_state)
            job.result = last_state
            job.errors = list(last_state.errors) if last_state else ["no result"]
            if last_state and last_state.report:
                assessment = last_state.security_assessment
                if (
                    assessment is not None
                    and assessment.scan_complete
                    and assessment.recommended_action is SecurityAction.HUMAN_REVIEW
                ):
                    job.state = JobState.REVIEW_REQUIRED
                elif (
                    assessment is not None
                    and assessment.scan_complete
                    and assessment.recommended_action is SecurityAction.BLOCK
                ):
                    job.state = JobState.BLOCKED
                else:
                    job.state = JobState.PARTIAL if job.errors else JobState.SUCCEEDED
            else:
                job.state = JobState.FAILED
        except Exception as exc:
            logger.exception("job_crashed", job_id=job.job_id)
            job.errors.append(f"{type(exc).__name__}: {exc}")
            job.state = JobState.FAILED
        finally:
            job.finished_at = datetime.now(timezone.utc)
            self._persist_run(job)
            if not self._retain_uploads:
                await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
            await self._delete_index(job, doc_id)

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
        except Exception:
            logger.exception("index_cleanup_failed", job_id=job.job_id, doc_id=doc_id)

    def _update_stages(self, job: Job, state: AnalysisState) -> None:
        for stage_name, state_field in STAGE_PREDICATES:
            value = getattr(state, state_field)
            if value:
                job.done_stages.add(stage_name)
        assessment = state.security_assessment
        if (
            state.report
            and assessment is not None
            and not assessment.recommended_action.allows_automated_analysis
        ):
            job.skipped_stages.update(SECURITY_SKIPPED_STAGES)
        for error in state.errors:
            agent_name = error.split(":", maxsplit=1)[0]
            if failed_stage := ERROR_STAGE_BY_AGENT.get(agent_name):
                job.failed_stages.add(failed_stage)

    def _persist_run(self, job: Job) -> None:
        metrics = job.result.report.metrics if job.result and job.result.report else RunMetrics()
        traces = job.result.traces if job.result else []
        doc_id = job.result.document.doc_id if job.result else ""
        try:
            self._run_store.append(
                RunRecord(
                    run_id=job.job_id,
                    doc_id=doc_id,
                    filename=job.filename,
                    success=job.state is JobState.SUCCEEDED,
                    outcome=job.state.value,
                    errors=job.errors,
                    metrics=metrics,
                    traces=traces,
                )
            )
        except Exception:
            logger.exception("run_persist_failed", job_id=job.job_id)


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
