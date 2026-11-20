"""R-P1: GET /health returns 200 with a status key."""

from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.core.config import LLMProvider, Settings, VectorStoreBackend


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
    )


@pytest.fixture
async def client(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_health_returns_200_with_status(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert "status" in payload
    assert "product" in payload
