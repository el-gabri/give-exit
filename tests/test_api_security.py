"""API access controls: shared-key auth and upload rate limiting."""

from pathlib import Path

import fitz
import httpx
import pytest

from app.api.main import create_app
from app.api.security import SlidingWindowRateLimiter
from app.core.config import DeploymentMode, LLMProvider, Settings, VectorStoreBackend


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72),
        "DOS FATOS\n\nCobrancas indevidas.\n\nDOS PEDIDOS\n\nDanos morais.",
        fontsize=11,
    )
    data = doc.tobytes()
    doc.close()
    return data


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


@pytest.fixture
async def throttled_client(tmp_path: Path):
    app = create_app(_settings(tmp_path, upload_rate_limit_per_minute=2))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_health_stays_public_with_auth_enabled(secured_client: httpx.AsyncClient) -> None:
    assert (await secured_client.get("/health")).status_code == 200


async def test_routes_require_the_configured_key(secured_client: httpx.AsyncClient) -> None:
    upload = {"file": ("peticao.pdf", _pdf_bytes(), "application/pdf")}

    assert (await secured_client.post("/analyses", files=upload)).status_code == 401
    assert (await secured_client.get("/runs")).status_code == 401
    assert (await secured_client.post("/consumer/cases")).status_code == 401
    assert (
        await secured_client.post("/analyses", files=upload, headers={"X-API-Key": "wrong"})
    ).status_code == 401

    accepted = await secured_client.post(
        "/analyses", files=upload, headers={"X-API-Key": "test-key-123"}
    )
    assert accepted.status_code == 202


async def test_upload_rate_limit_returns_429(throttled_client: httpx.AsyncClient) -> None:
    upload = {"file": ("peticao.pdf", _pdf_bytes(), "application/pdf")}

    assert (await throttled_client.post("/analyses", files=upload)).status_code == 202
    assert (await throttled_client.post("/analyses", files=upload)).status_code == 202
    assert (await throttled_client.post("/analyses", files=upload)).status_code == 429
    # Read-only routes remain unthrottled.
    assert (await throttled_client.get("/runs")).status_code == 200


def test_sliding_window_recovers_after_the_window_passes() -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(2, window_seconds=60.0, clock=lambda: now[0])

    assert limiter.allow("client")
    assert limiter.allow("client")
    assert not limiter.allow("client")
    assert limiter.allow("other-client")

    now[0] = 61.0
    assert limiter.allow("client")


def test_zero_limit_disables_the_limiter() -> None:
    limiter = SlidingWindowRateLimiter(0)
    assert all(limiter.allow("client") for _ in range(50))


def test_production_mode_requires_api_authentication(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="LITIGATION_API_AUTH_KEY"):
        _settings(tmp_path, deployment_mode=DeploymentMode.PRODUCTION)


def test_production_mode_accepts_configured_api_authentication(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        deployment_mode=DeploymentMode.PRODUCTION,
        api_auth_key="production-secret",
    )

    assert settings.deployment_mode is DeploymentMode.PRODUCTION
