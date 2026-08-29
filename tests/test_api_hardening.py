"""Resource bounds for a network-reachable deployment.

Every test here pins a limit that an audit found missing: request bodies,
per-client request rates, the size of the in-process case store and the
identity rate limiting is keyed on.
"""

from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.api.security import (
    SlidingWindowRateLimiter,
    client_identifier,
)
from app.core.config import LLMProvider, Settings, VectorStoreBackend


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
        **overrides,
    )


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120)


@pytest.fixture
async def bounded_client(tmp_path: Path):
    app = create_app(_settings(tmp_path, max_upload_bytes=1024 * 1024))
    async with app.router.lifespan_context(app), await _client(app) as client:
        yield client


async def test_oversized_body_is_refused_before_it_is_buffered(
    bounded_client: httpx.AsyncClient,
) -> None:
    """The size ceiling must hold at the transport, not only inside the route.

    Starlette streams a multipart file part into a spooled temporary file that
    rolls over to disk without an upper bound, so a route-level check runs only
    after the whole body has already been written.
    """
    created = (await bounded_client.post("/consumer/cases")).json()
    headers = {"X-Consumer-Case-Token": created["case_token"]}
    oversized = b"%PDF-1.4\n" + b"A" * (4 * 1024 * 1024)

    response = await bounded_client.post(
        f"/consumer/cases/{created['case_id']}/documents",
        headers=headers,
        files={"file": ("grande.pdf", oversized, "application/pdf")},
    )

    assert response.status_code == 413


async def test_declared_content_length_over_the_limit_is_refused(
    bounded_client: httpx.AsyncClient,
) -> None:
    response = await bounded_client.post(
        "/consumer/cases",
        headers={"Content-Length": str(64 * 1024 * 1024), "Content-Type": "application/json"},
        content=b"{}",
    )

    assert response.status_code == 413


@pytest.fixture
async def rate_limited_client(tmp_path: Path):
    app = create_app(
        _settings(
            tmp_path,
            case_rate_limit_per_minute=2,
            notice_rate_limit_per_minute=1,
            message_rate_limit_per_minute=2,
        )
    )
    async with app.router.lifespan_context(app), await _client(app) as client:
        yield client


async def test_case_creation_is_rate_limited(rate_limited_client: httpx.AsyncClient) -> None:
    codes = [(await rate_limited_client.post("/consumer/cases")).status_code for _ in range(4)]

    assert codes == [201, 201, 429, 429]


async def test_notice_generation_is_rate_limited(
    rate_limited_client: httpx.AsyncClient,
) -> None:
    """The most expensive route must not be the only unthrottled one."""
    created = (await rate_limited_client.post("/consumer/cases")).json()
    headers = {"X-Consumer-Case-Token": created["case_token"]}
    case_id = created["case_id"]

    # A case with no confirmed facts still consumes the limiter before the
    # handler decides it is not ready, which is the point of the guard.
    first = await rate_limited_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)
    second = await rate_limited_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)

    assert first.status_code == 409
    assert second.status_code == 429


async def test_message_route_is_rate_limited(rate_limited_client: httpx.AsyncClient) -> None:
    created = (await rate_limited_client.post("/consumer/cases")).json()
    headers = {"X-Consumer-Case-Token": created["case_token"]}
    body = {"text": "A loja cobrou um valor que nao reconheco."}

    codes = [
        (
            await rate_limited_client.post(
                f"/consumer/cases/{created['case_id']}/messages", headers=headers, json=body
            )
        ).status_code
        for _ in range(3)
    ]

    assert codes == [200, 200, 429]


async def test_full_case_store_refuses_new_cases_instead_of_evicting(tmp_path: Path) -> None:
    """Capacity must never be reclaimed by discarding a live case."""
    app = create_app(_settings(tmp_path, max_active_cases=2))
    async with app.router.lifespan_context(app), await _client(app) as client:
        codes = [(await client.post("/consumer/cases")).status_code for _ in range(4)]
        store = app.state.consumer_service._store

    assert codes == [201, 201, 503, 503]
    assert len(store._cases) == 2


def test_idle_cases_expire_and_release_their_identifiers() -> None:
    from app.consumer.store import ConsumerCaseStore

    now = [0.0]
    store = ConsumerCaseStore(idle_ttl_seconds=100.0, clock=lambda: now[0])
    record, _ = store.create()
    record.indexed_document_ids.add("doc-1")

    now[0] = 150.0
    expired = store.expire_idle_cases()

    assert [item.case_id for item in expired] == [record.case_id]
    assert expired[0].indexed_document_ids == {"doc-1"}
    assert store.indexed_document_ids() == set()


def test_active_cases_are_not_expired() -> None:
    from app.consumer.store import ConsumerCaseStore

    now = [0.0]
    store = ConsumerCaseStore(idle_ttl_seconds=100.0, clock=lambda: now[0])
    record, token = store.create()

    now[0] = 80.0
    store.get_authorized(record.case_id, token)  # refreshes the idle clock
    now[0] = 150.0

    assert store.expire_idle_cases() == []


class _StubRequest:
    def __init__(self, peer: str, headers: dict[str, str]) -> None:
        self.client = type("Client", (), {"host": peer})()
        self.headers = headers


def test_forwarded_for_is_ignored_without_configured_proxies() -> None:
    """An unproxied deployment must not trust a client-supplied header."""
    request = _StubRequest("203.0.113.9", {"x-forwarded-for": "1.1.1.1, 2.2.2.2"})

    assert client_identifier(request, trusted_proxy_hops=0) == "203.0.113.9"  # type: ignore[arg-type]


def test_forwarded_for_is_read_for_the_configured_hop_count() -> None:
    """Behind a proxy, every client must not collapse into one bucket.

    Each proxy appends the address it received the connection from, so with one
    trusted proxy the real client is the rightmost entry. Anything further left
    was supplied by the caller and is not trusted.
    """
    request = _StubRequest(
        "10.0.0.1", {"x-forwarded-for": "198.51.100.7, 203.0.113.5"}
    )

    assert client_identifier(request, trusted_proxy_hops=1) == "203.0.113.5"  # type: ignore[arg-type]


def test_forged_forwarded_entries_cannot_shift_the_identity() -> None:
    """A client prepending fake hops must not escape its own bucket."""
    honest = _StubRequest("10.0.0.1", {"x-forwarded-for": "203.0.113.5"})
    forged = _StubRequest(
        "10.0.0.1", {"x-forwarded-for": "9.9.9.9, 8.8.8.8, 203.0.113.5"}
    )

    assert client_identifier(honest, trusted_proxy_hops=1) == "203.0.113.5"  # type: ignore[arg-type]
    assert client_identifier(forged, trusted_proxy_hops=1) == "203.0.113.5"  # type: ignore[arg-type]


def test_two_trusted_proxies_skip_both_appended_hops() -> None:
    request = _StubRequest(
        "10.0.0.2", {"x-forwarded-for": "9.9.9.9, 203.0.113.5, 10.0.0.1"}
    )

    assert client_identifier(request, trusted_proxy_hops=2) == "203.0.113.5"  # type: ignore[arg-type]


def test_rate_limiter_prunes_buckets_of_inactive_clients() -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        5, window_seconds=60.0, clock=lambda: now[0], max_tracked_keys=3
    )

    for index in range(3):
        assert limiter.allow(f"client-{index}")
    now[0] = 120.0
    assert limiter.allow("client-late")

    assert len(limiter._events) <= 3
