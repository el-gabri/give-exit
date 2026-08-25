"""Small system routes shared by every deployment."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "product": "give-exit-consumer"}
