"""R-P5: With api_auth_key set, /health stays public; consumer routes require the key."""

from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.core.config import LLMProvider, Settings, VectorStoreBackend


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
        **overrides,
    )


@pytest.fixture
async def secured_client(tmp_path: Path):
    app = create_app(_settings(tmp_path, api_auth_key="test-key-123"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_health_stays_public_when_api_auth_key_configured(
    secured_client: httpx.AsyncClient,
) -> None:
    response = await secured_client.get("/health")

    assert response.status_code == 200


async def test_prompt_notices_rejected_without_api_key(
    secured_client: httpx.AsyncClient,
) -> None:
    response = await secured_client.post(
        "/consumer/prompt-notices",
        data={"text": "cobranca indevida"},
    )

    assert response.status_code == 401


async def test_prompt_notices_with_valid_api_key_is_not_auth_failure(
    secured_client: httpx.AsyncClient,
) -> None:
    response = await secured_client.post(
        "/consumer/prompt-notices",
        headers={"X-API-Key": "test-key-123"},
        data={"text": "cobranca indevida"},
    )

    assert response.status_code != 401
