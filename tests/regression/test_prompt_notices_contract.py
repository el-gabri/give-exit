"""R-P2: POST /consumer/prompt-notices text-only returns 201 with notice text."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.main import create_app
from app.core.config import LLMProvider, Settings, VectorStoreBackend

# Offline hashed embedder grounds this complaint within the agreement depth
# (same wording as tests/test_consumer_prompt_notice.py — corpus v5 / ADR 0022).
NUBANK = (
    "O Nubank cobrou duas vezes a mesma fatura em julho de 2026 e eu paguei as duas "
    "cobranças. Quero a devolução em dobro do valor pago a mais."
)


class _FakeOcr:
    def ocr_image(self, image_bytes: bytes) -> str:
        return "COMPROVANTE DE COMPRA com protocolo ABC-123 e valor de R$ 100,00."


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
    )


@pytest.fixture
async def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    monkeypatch.setattr("app.api.main.create_default_ocr_engine", lambda: _FakeOcr())
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        await app.state.consumer_service.prepare_legal_corpus()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=120
        ) as client:
            yield client, app


async def test_prompt_notices_text_only_returns_201_with_notice_full_text(
    api: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    client, _ = api

    response = await client.post("/consumer/prompt-notices", data={"text": NUBANK})

    assert response.status_code == 201, response.text
    payload = response.json()
    notice = payload["notice"]
    text = notice.get("full_text") or notice.get("text")
    assert isinstance(text, str) and text.strip()
