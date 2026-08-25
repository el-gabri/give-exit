"""Small system routes shared by every deployment."""

from fastapi import APIRouter, Request

from app.consumer.service import ConsumerCaseService

router = APIRouter()


@router.get("/health", tags=["system"])
async def health(request: Request) -> dict[str, str | bool]:
    service: ConsumerCaseService = request.app.state.consumer_service
    return {
        "status": "ok",
        "product": "give-exit-consumer",
        "legal_corpus_ready": await service.legal_corpus_ready(),
    }
