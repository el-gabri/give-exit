"""REST routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from app.api.jobs import (
    AnalysisJobManager,
    InvalidPdfUploadError,
    Job,
    UploadTooLargeError,
)
from app.api.schemas import JobCreated, JobState, JobStatus
from app.api.security import enforce_upload_rate_limit
from app.observability.store import RunStore
from app.reporting.markdown import render_markdown
from app.schemas.report import LitigationReport
from app.schemas.trace import AgentTrace, RetrievalTrace

router = APIRouter()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


def get_job_manager(request: Request) -> AnalysisJobManager:
    return request.app.state.job_manager


def get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store


JobManagerDep = Annotated[AnalysisJobManager, Depends(get_job_manager)]
RunStoreDep = Annotated[RunStore, Depends(get_run_store)]


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post(
    "/analyses",
    response_model=JobCreated,
    status_code=202,
    dependencies=[Depends(enforce_upload_rate_limit)],
)
async def create_analysis(file: UploadFile, manager: JobManagerDep) -> JobCreated:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF files are accepted")
    try:
        job = await manager.submit_upload(
            file.filename or "upload.pdf", file, max_upload_bytes=MAX_UPLOAD_BYTES
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail="File exceeds 20 MB limit") from exc
    except InvalidPdfUploadError as exc:
        raise HTTPException(status_code=422, detail="File is not a valid PDF") from exc
    finally:
        await file.close()
    return JobCreated(job_id=job.job_id, status_url=f"/analyses/{job.job_id}")


def _get_job_or_404(manager: AnalysisJobManager, job_id: str) -> Job:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _retrievals(traces: list[AgentTrace]) -> list[RetrievalTrace]:
    """Flatten and deterministically order nested agent retrieval traces."""
    return sorted(
        (retrieval for trace in traces for retrieval in trace.retrievals),
        key=lambda retrieval: (
            retrieval.created_at,
            retrieval.agent,
            retrieval.query_index,
        ),
    )


@router.get("/analyses/{job_id}", response_model=JobStatus)
async def get_analysis(job_id: str, manager: JobManagerDep) -> JobStatus:
    job = _get_job_or_404(manager, job_id)
    return JobStatus(
        job_id=job.job_id,
        filename=job.filename,
        state=job.state,
        stages=job.stages(),
        errors=job.errors,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


@router.get("/analyses/{job_id}/report", response_model=LitigationReport)
async def get_report(job_id: str, manager: JobManagerDep) -> LitigationReport:
    job = _get_job_or_404(manager, job_id)
    if job.state in (JobState.QUEUED, JobState.RUNNING):
        raise HTTPException(status_code=409, detail="Analysis still in progress")
    if job.result is None or job.result.report is None:
        raise HTTPException(status_code=422, detail={"errors": job.errors})
    return job.result.report


@router.get(
    "/analyses/{job_id}/retrievals",
    response_model=list[RetrievalTrace],
)
async def get_analysis_retrievals(
    job_id: str, manager: JobManagerDep
) -> list[RetrievalTrace]:
    """Return the complete ranked retrieval audit for an analysis."""
    job = _get_job_or_404(manager, job_id)
    if job.state in (JobState.QUEUED, JobState.RUNNING):
        raise HTTPException(status_code=409, detail="Analysis still in progress")
    if job.result is None:
        raise HTTPException(status_code=422, detail={"errors": job.errors})
    return _retrievals(job.result.traces)


@router.get("/analyses/{job_id}/report.md", response_class=PlainTextResponse)
async def get_report_markdown(job_id: str, manager: JobManagerDep) -> str:
    report = await get_report(job_id, manager)
    return render_markdown(report)


@router.get("/analyses/{job_id}/report.docx")
async def get_report_docx(job_id: str, manager: JobManagerDep) -> Response:
    from app.reporting.convert import render_docx

    report = await get_report(job_id, manager)
    return Response(
        content=render_docx(render_markdown(report)),
        media_type="application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="relatorio_{report.doc_id}.docx"'
        },
    )


@router.get("/analyses/{job_id}/report.pdf")
async def get_report_pdf(job_id: str, manager: JobManagerDep) -> Response:
    from app.reporting.convert import render_pdf

    report = await get_report(job_id, manager)
    return Response(
        content=render_pdf(render_markdown(report)),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="relatorio_{report.doc_id}.pdf"'
        },
    )


@router.get("/runs")
async def list_runs(store: RunStoreDep, limit: int = 20) -> list[dict]:
    return [
        record.model_dump(mode="json", exclude={"traces"})
        for record in store.list_runs(limit=limit)
    ]


@router.get("/runs/totals")
async def run_totals(store: RunStoreDep) -> dict:
    return store.totals()


@router.get(
    "/runs/{run_id}/retrievals",
    response_model=list[RetrievalTrace],
)
async def get_run_retrievals(run_id: str, store: RunStoreDep) -> list[RetrievalTrace]:
    """Return persisted retrieval traces even after in-memory jobs expire."""
    record = store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _retrievals(record.traces)
