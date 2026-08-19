"""Tests for bounded upload persistence and partial job statuses."""

import asyncio
from io import BytesIO
from pathlib import Path

import fitz
import pytest
from fastapi import UploadFile

from app.api.jobs import AnalysisJobManager, Job, UploadTooLargeError, _write_upload_in_chunks
from app.api.schemas import JobState, StageState
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.observability.store import RunStore
from app.orchestration.graph import build_analysis_graph
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore


async def test_upload_is_written_in_chunks(tmp_path: Path) -> None:
    path = tmp_path / "upload.pdf"
    upload = UploadFile(filename="upload.pdf", file=BytesIO(b"%PDF-1.7 content"))

    header = await _write_upload_in_chunks(upload, path, max_upload_bytes=1024)

    assert header.startswith(b"%PDF")
    assert path.read_bytes() == b"%PDF-1.7 content"


async def test_upload_over_limit_is_rejected_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "too-large.pdf"
    upload = UploadFile(filename="too-large.pdf", file=BytesIO(b"%PDF-1.7 content"))

    with pytest.raises(UploadTooLargeError):
        await _write_upload_in_chunks(upload, path, max_upload_bytes=4)

    assert not path.exists()


def test_partial_job_exposes_failed_stage_without_hiding_completed_work() -> None:
    job = Job(job_id="job", filename="x.pdf", state=JobState.PARTIAL)
    job.done_stages.update({"index", "compose"})
    job.failed_stages.add("classify")

    states = {stage.name: stage.state for stage in job.stages()}

    assert states["index"] is StageState.DONE
    assert states["classify"] is StageState.FAILED
    assert states["compose"] is StageState.DONE


def _pdf_upload() -> UploadFile:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72),
        "DOS FATOS\n\nCobrancas indevidas do Banco Exemplo S.A.\n\n"
        "DOS PEDIDOS\n\nDanos morais de R$ 20.000,00.",
        fontsize=11,
    )
    data = doc.tobytes()
    doc.close()
    return UploadFile(filename="peticao.pdf", file=BytesIO(data))


def _manager(tmp_path: Path, rag: RagPipeline, retain_index: bool) -> AnalysisJobManager:
    return AnalysisJobManager(
        ingestion=DocumentIngestionService(),
        graph=build_analysis_graph(MockLLMClient(), rag),
        run_store=RunStore(tmp_path / "runs.jsonl"),
        uploads_dir=tmp_path / "uploads",
        rag=rag,
        retain_index=retain_index,
    )


async def _wait_finished(manager: AnalysisJobManager, job_id: str) -> Job:
    for _ in range(200):
        job = manager.get(job_id)
        assert job is not None
        if job.finished_at is not None:
            return job
        await asyncio.sleep(0.05)
    raise TimeoutError("job did not finish in time")


async def test_document_index_is_deleted_when_job_finishes(tmp_path: Path) -> None:
    rag = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    manager = _manager(tmp_path, rag, retain_index=False)

    job = await manager.submit_upload("peticao.pdf", _pdf_upload(), max_upload_bytes=2**20)
    finished = await _wait_finished(manager, job.job_id)

    assert finished.state is JobState.SUCCEEDED
    assert finished.doc_id is not None
    assert await rag.retrieve("cobrancas indevidas", doc_id=finished.doc_id, k=3) == []


async def test_document_index_is_kept_when_retention_enabled(tmp_path: Path) -> None:
    rag = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    manager = _manager(tmp_path, rag, retain_index=True)

    job = await manager.submit_upload("peticao.pdf", _pdf_upload(), max_upload_bytes=2**20)
    finished = await _wait_finished(manager, job.job_id)

    assert finished.state is JobState.SUCCEEDED
    assert finished.doc_id is not None
    assert await rag.retrieve("cobrancas indevidas", doc_id=finished.doc_id, k=3)


def test_security_block_exposes_downstream_stages_as_skipped() -> None:
    job = Job(job_id="job", filename="x.pdf", state=JobState.SUCCEEDED)
    job.done_stages.update({"security_scan", "compose"})
    job.skipped_stages.update(
        {"index", "classify", "extract", "analyze", "enrich", "risk", "strategy"}
    )

    states = {stage.name: stage.state for stage in job.stages()}

    assert states["security_scan"] is StageState.DONE
    assert states["index"] is StageState.SKIPPED
    assert states["strategy"] is StageState.SKIPPED
    assert states["compose"] is StageState.DONE
