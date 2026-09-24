"""HTTP contract for the consumer extrajudicial-notice workflow."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.security import (
    enforce_case_rate_limit,
    enforce_message_rate_limit,
    enforce_notice_rate_limit,
    enforce_upload_rate_limit,
)
from app.api.uploads import UploadTooLargeError, write_upload_in_chunks
from app.consumer.schemas import (
    ConsumerCaseSnapshot,
    ConsumerEvidence,
    ConsumerNotice,
    ConsumerPromptNotice,
    EvidenceStatus,
)
from app.consumer.service import (
    ConsumerCaseNotReadyError,
    ConsumerCaseService,
    ConsumerEvidenceLimitError,
    ConsumerPromptNoticeError,
    ConsumerRetrievalError,
)
from app.consumer.store import ConsumerCaseCapacityError
from app.ingestion.service import DocumentTextUnavailableError
from app.reporting.convert import render_docx, render_pdf
from app.schemas.trace import RetrievalTrace

router = APIRouter(prefix="/consumer", tags=["consumer"])

PromptNoticeFormat = Literal["json", "markdown"]

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
    service: ConsumerCaseService = request.app.state.consumer_service
    return service


def get_uploads_dir(request: Request) -> Path:
    uploads_dir: Path = request.app.state.consumer_uploads_dir
    return uploads_dir


def get_max_upload_bytes(request: Request) -> int:
    max_upload_bytes: int = request.app.state.max_upload_bytes
    return max_upload_bytes


ConsumerServiceDep = Annotated[ConsumerCaseService, Depends(get_consumer_service)]
UploadsDirDep = Annotated[Path, Depends(get_uploads_dir)]
MaxUploadBytesDep = Annotated[int, Depends(get_max_upload_bytes)]
CaseToken = Annotated[
    str,
    Header(
        alias="X-Consumer-Case-Token",
        min_length=20,
        description="Opaque possession token returned only when the case is created",
    ),
]


async def consumer_case_not_found(request: Request, exc: Exception) -> JSONResponse:
    """One 404 for unknown cases and wrong tokens, from every consumer route.

    Registered on the application for ``ConsumerCaseNotFoundError``, so a route
    never has to translate it and case-id enumeration learns nothing.
    """
    del request, exc
    return JSONResponse(status_code=404, content={"detail": "Consumer case not found"})


@router.post(
    "/cases",
    response_model=ConsumerCaseCreated,
    status_code=201,
    dependencies=[Depends(enforce_case_rate_limit)],
)
async def create_consumer_case(service: ConsumerServiceDep) -> ConsumerCaseCreated:
    try:
        snapshot, token, assistant = service.create_case()
    except ConsumerCaseCapacityError as exc:
        raise _capacity_exhausted() from exc
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
    return service.get_case(case_id, token)


@router.post(
    "/cases/{case_id}/messages",
    response_model=ConsumerChatTurn,
    dependencies=[Depends(enforce_message_rate_limit)],
)
async def add_consumer_message(
    case_id: str,
    payload: ConsumerMessageRequest,
    token: CaseToken,
    service: ConsumerServiceDep,
) -> ConsumerChatTurn:
    snapshot, assistant = service.add_message(
        case_id,
        token,
        payload.text,
        client_message_id=payload.client_message_id,
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
        return service.update_facts(case_id, token, values, facts_confirmed=confirmed)
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
    max_upload_bytes: MaxUploadBytesDep,
) -> ConsumerDocumentAdded:
    if Path(file.filename or "").suffix.casefold() not in EVIDENCE_MEDIA_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Formato não suportado. Envie um arquivo PDF, PNG ou JPG.",
        )
    service.get_case(case_id, token)
    snapshot, document = await _receive_evidence(
        case_id,
        token,
        file=file,
        service=service,
        uploads_dir=uploads_dir,
        max_upload_bytes=max_upload_bytes,
    )
    return ConsumerDocumentAdded(case=snapshot, document=document)


async def _receive_evidence(
    case_id: str,
    token: str,
    *,
    file: UploadFile,
    service: ConsumerCaseService,
    uploads_dir: Path,
    max_upload_bytes: int,
) -> tuple[ConsumerCaseSnapshot, ConsumerEvidence]:
    """Stream, verify and ingest one upload into a case; the raw file never stays."""
    filename = Path(file.filename or "evidencia").name
    suffix = Path(filename).suffix.casefold()
    media_type = EVIDENCE_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(
            status_code=422,
            detail="Formato não suportado. Envie um arquivo PDF, PNG ou JPG.",
        )
    await asyncio.to_thread(uploads_dir.mkdir, parents=True, exist_ok=True)
    upload_path = uploads_dir / f"{uuid.uuid4().hex}{suffix}"
    try:
        header = await write_upload_in_chunks(
            file=file,
            path=upload_path,
            max_upload_bytes=max_upload_bytes,
        )
        if not _has_expected_signature(suffix, header):
            raise HTTPException(
                status_code=422,
                detail="O conteúdo do arquivo não corresponde ao formato informado.",
            )
        return await service.add_document(
            case_id,
            token,
            filename=filename,
            path=upload_path,
            media_type=media_type,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=f"O arquivo excede o limite de {max_upload_bytes // (1024 * 1024)} MB.",
        ) from exc
    except ConsumerEvidenceLimitError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Este atendimento já contém o número máximo de {exc.limit} documentos. "
                "Inicie um novo atendimento para enviar outras evidências."
            ),
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


@router.post(
    "/prompt-notices",
    response_model=ConsumerPromptNotice,
    status_code=201,
    responses={201: {"content": {"text/markdown": {}}}},
    # The request creates a case, uploads and generates, so it spends from
    # every one of those budgets.
    dependencies=[
        Depends(enforce_case_rate_limit),
        Depends(enforce_upload_rate_limit),
        Depends(enforce_notice_rate_limit),
    ],
)
async def generate_prompt_notice(
    service: ConsumerServiceDep,
    uploads_dir: UploadsDirDep,
    max_upload_bytes: MaxUploadBytesDep,
    text: Annotated[str, Form(min_length=1, max_length=20_000)],
    file: Annotated[UploadFile | None, File()] = None,
    response_format: Annotated[PromptNoticeFormat, Query(alias="format")] = "json",
) -> ConsumerPromptNotice | Response:
    """Draft a notice from one free-text request and an optional evidence file.

    Facts are extracted without a review step and evidence is optional; the
    notice records both in ``generation_mode`` and its warnings. The case is
    kept like any other: ``case_id`` and ``case_token`` open the exports and
    the retrieval audit under ``/consumer/cases/{case_id}`` until the case
    expires or is deleted. ``format=markdown`` returns only the draft text,
    with the case credentials in the ``X-Consumer-Case-Id`` and
    ``X-Consumer-Case-Token`` headers.
    """
    try:
        case_id, token = service.start_prompt_case(text)
    except ConsumerPromptNoticeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConsumerCaseCapacityError as exc:
        raise _capacity_exhausted() from exc
    try:
        notice = await _draft_prompt_notice(
            case_id,
            token,
            file=file,
            service=service,
            uploads_dir=uploads_dir,
            max_upload_bytes=max_upload_bytes,
        )
    except BaseException:
        # The client never received this case's credentials; keep nothing.
        await service.delete_case(case_id, token)
        raise
    if response_format == "markdown":
        return PlainTextResponse(
            notice.full_text,
            status_code=201,
            media_type="text/markdown; charset=utf-8",
            headers={"X-Consumer-Case-Id": case_id, "X-Consumer-Case-Token": token},
        )
    return ConsumerPromptNotice(case_id=case_id, case_token=token, notice=notice)


async def _draft_prompt_notice(
    case_id: str,
    token: str,
    *,
    file: UploadFile | None,
    service: ConsumerCaseService,
    uploads_dir: Path,
    max_upload_bytes: int,
) -> ConsumerNotice:
    if file is not None and (file.filename or "").strip():
        _, document = await _receive_evidence(
            case_id,
            token,
            file=file,
            service=service,
            uploads_dir=uploads_dir,
            max_upload_bytes=max_upload_bytes,
        )
        if document.status is not EvidenceStatus.ACCEPTED:
            raise HTTPException(
                status_code=422,
                detail=(
                    "O anexo não pôde ser usado automaticamente. Envie um PDF, PNG ou JPG "
                    "legível e sem instruções dirigidas à IA, ou gere sem anexo."
                ),
            )
    try:
        return await service.generate_prompt_notice(case_id, token)
    except ConsumerRetrievalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/cases/{case_id}/notice",
    response_model=ConsumerNotice,
    dependencies=[Depends(enforce_notice_rate_limit)],
)
async def generate_consumer_notice(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> ConsumerNotice:
    try:
        return await service.generate_notice(case_id, token)
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
    return _notice_attachment(
        case_id,
        await asyncio.to_thread(render_pdf, notice.full_text),
        extension="pdf",
        media_type="application/pdf",
    )


@router.get("/cases/{case_id}/notice.docx")
async def get_consumer_notice_docx(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> Response:
    notice = await get_consumer_notice(case_id, token, service)
    return _notice_attachment(
        case_id,
        await asyncio.to_thread(render_docx, notice.full_text),
        extension="docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.delete("/cases/{case_id}", status_code=204)
async def delete_consumer_case(
    case_id: str, token: CaseToken, service: ConsumerServiceDep
) -> Response:
    await service.delete_case(case_id, token)
    return Response(status_code=204)


def _capacity_exhausted() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "O serviço está com a capacidade de atendimentos simultâneos esgotada. "
            "Tente novamente em alguns minutos."
        ),
    )


def _notice_attachment(
    case_id: str, content: bytes, *, extension: str, media_type: str
) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="notificacao_{case_id}.{extension}"'
        },
    )
