"""HTTP contract for the consumer extrajudicial-notice workflow."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.jobs import UploadTooLargeError, _write_upload_in_chunks
from app.api.security import enforce_upload_rate_limit
from app.consumer.schemas import (
    ConsumerCaseSnapshot,
    ConsumerEvidence,
    ConsumerIssueCategory,
    ConsumerNotice,
)
from app.consumer.service import (
    ConsumerCaseNotReadyError,
    ConsumerCaseService,
    ConsumerRetrievalError,
)
from app.consumer.store import ConsumerCaseNotFoundError
from app.ingestion.service import DocumentTextUnavailableError
from app.reporting.convert import render_docx, render_pdf
from app.schemas.trace import RetrievalTrace

router = APIRouter(prefix="/consumer", tags=["consumer"])
ResultT = TypeVar("ResultT")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
EVIDENCE_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _has_expected_signature(suffix: str, header: bytes) -> bool:
    if suffix == ".pdf":
        return header.startswith(b"%PDF")
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    return suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff")


class ConsumerCaseCreated(BaseModel):
    case_id: str
    case_token: str
    case: ConsumerCaseSnapshot
    assistant_message: str


class ConsumerMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)


class ConsumerChatTurn(BaseModel):
    case: ConsumerCaseSnapshot
    assistant_message: str


class ConsumerFactsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_name: str | None = Field(default=None, max_length=200)
    bank_name: str | None = Field(default=None, max_length=200)
    issue_category: ConsumerIssueCategory | None = None
    complaint_summary: str | None = Field(default=None, max_length=10_000)
    incident_date_or_period: str | None = Field(default=None, max_length=500)
    prior_protocols: list[str] | None = None
    direct_loss_amount: Decimal | None = Field(default=None, ge=0)
    direct_loss_reference_id: str | None = Field(default=None, min_length=16, max_length=64)
    improper_payment_amount: Decimal | None = Field(default=None, ge=0)
    article_42_double_repayment_requested: bool | None = None
    unsuccessful_scenario_cost_amount: Decimal | None = Field(default=None, ge=0)
    desired_resolution: str | None = Field(default=None, max_length=2_000)
    response_deadline_business_days: int | None = Field(default=None, ge=1, le=60)
    facts_confirmed: bool | None = None


class ConsumerDocumentAdded(BaseModel):
    case: ConsumerCaseSnapshot
    document: ConsumerEvidence


def get_consumer_service(request: Request) -> ConsumerCaseService:
    return request.app.state.consumer_service


def get_uploads_dir(request: Request) -> Path:
    return request.app.state.consumer_uploads_dir


ConsumerServiceDep = Annotated[ConsumerCaseService, Depends(get_consumer_service)]
UploadsDirDep = Annotated[Path, Depends(get_uploads_dir)]
CaseToken = Annotated[
    str,
    Header(
        alias="X-Consumer-Case-Token",
        min_length=20,
        description="Opaque possession token returned only when the case is created",
    ),
]


@router.post("/cases", response_model=ConsumerCaseCreated, status_code=201)
async def create_consumer_case(service: ConsumerServiceDep) -> ConsumerCaseCreated:
    snapshot, token, assistant = service.create_case()
    return ConsumerCaseCreated(
        case_id=snapshot.case_id,
        case_token=token,
        case=snapshot,
        assistant_message=assistant,
    )


@router.get("/cases/{case_id}", response_model=ConsumerCaseSnapshot)
async def get_consumer_case(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> ConsumerCaseSnapshot:
    return _authorized(lambda: service.get_case(case_id, token))


@router.post("/cases/{case_id}/messages", response_model=ConsumerChatTurn)
async def add_consumer_message(
    case_id: str,
    payload: ConsumerMessageRequest,
    token: CaseToken,
    service: ConsumerServiceDep,
) -> ConsumerChatTurn:
    snapshot, assistant = _authorized(
        lambda: service.add_message(
            case_id,
            token,
            payload.text,
            client_message_id=payload.client_message_id,
        )
    )
    return ConsumerChatTurn(case=snapshot, assistant_message=assistant)


@router.patch("/cases/{case_id}/facts", response_model=ConsumerCaseSnapshot)
async def update_consumer_facts(
    case_id: str,
    payload: ConsumerFactsPatch,
    token: CaseToken,
    service: ConsumerServiceDep,
) -> ConsumerCaseSnapshot:
    values = payload.model_dump(exclude_unset=True)
    confirmed = values.pop("facts_confirmed", None)
    try:
        return _authorized(
            lambda: service.update_facts(case_id, token, values, facts_confirmed=confirmed)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/cases/{case_id}/documents",
    response_model=ConsumerDocumentAdded,
    status_code=201,
    dependencies=[Depends(enforce_upload_rate_limit)],
)
async def add_consumer_document(
    case_id: str,
    file: UploadFile,
    token: CaseToken,
    service: ConsumerServiceDep,
    uploads_dir: UploadsDirDep,
) -> ConsumerDocumentAdded:
    filename = Path(file.filename or "evidencia").name
    suffix = Path(filename).suffix.casefold()
    media_type = EVIDENCE_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(
            status_code=422,
            detail="Formato não suportado. Envie um arquivo PDF, PNG ou JPG.",
        )

    _authorized(lambda: service.get_case(case_id, token))
    uploads_dir.mkdir(parents=True, exist_ok=True)
    upload_path = uploads_dir / f"{uuid.uuid4().hex}{suffix}"
    try:
        header = await _write_upload_in_chunks(
            file=file,
            path=upload_path,
            max_upload_bytes=MAX_UPLOAD_BYTES,
        )
        if not _has_expected_signature(suffix, header):
            raise HTTPException(
                status_code=422,
                detail="O conteúdo do arquivo não corresponde ao formato informado.",
            )
        snapshot, document = await service.add_document(
            case_id,
            token,
            filename=filename,
            path=upload_path,
            media_type=media_type,
        )
    except ConsumerCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consumer case not found") from exc
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail="O arquivo excede o limite de 20 MB.",
        ) from exc
    except DocumentTextUnavailableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=("O arquivo está corrompido, ilegível ou excede o limite de resolução aceito."),
        ) from exc
    finally:
        await file.close()
        await asyncio.to_thread(upload_path.unlink, missing_ok=True)
    return ConsumerDocumentAdded(case=snapshot, document=document)


@router.post("/cases/{case_id}/notice", response_model=ConsumerNotice)
async def generate_consumer_notice(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> ConsumerNotice:
    try:
        return await service.generate_notice(case_id, token)
    except ConsumerCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consumer case not found") from exc
    except ConsumerCaseNotReadyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "Consumer case is not ready", "missing": exc.missing},
        ) from exc
    except ConsumerRetrievalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/cases/{case_id}/notice", response_model=ConsumerNotice)
async def get_consumer_notice(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> ConsumerNotice:
    try:
        return service.get_notice(case_id, token)
    except ConsumerCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consumer case not found") from exc
    except ConsumerCaseNotReadyError as exc:
        raise HTTPException(status_code=404, detail="Notice not found") from exc


@router.get("/cases/{case_id}/notice/retrievals", response_model=list[RetrievalTrace])
async def get_consumer_notice_retrievals(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> list[RetrievalTrace]:
    return (await get_consumer_notice(case_id, token, service)).retrievals


@router.get("/cases/{case_id}/notice.md", response_class=PlainTextResponse)
async def get_consumer_notice_markdown(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> str:
    return (await get_consumer_notice(case_id, token, service)).full_text


@router.get("/cases/{case_id}/notice.pdf")
async def get_consumer_notice_pdf(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> Response:
    notice = await get_consumer_notice(case_id, token, service)
    return Response(
        content=render_pdf(notice.full_text),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="notificacao_{case_id}.pdf"'},
    )


@router.get("/cases/{case_id}/notice.docx")
async def get_consumer_notice_docx(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> Response:
    notice = await get_consumer_notice(case_id, token, service)
    return Response(
        content=render_docx(notice.full_text),
        media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        headers={"Content-Disposition": f'attachment; filename="notificacao_{case_id}.docx"'},
    )


@router.delete("/cases/{case_id}", status_code=204)
async def delete_consumer_case(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> Response:
    try:
        await service.delete_case(case_id, token)
    except ConsumerCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consumer case not found") from exc
    return Response(status_code=204)


def _authorized(operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except ConsumerCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consumer case not found") from exc
