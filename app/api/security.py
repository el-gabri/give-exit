"""API access controls: shared-key auth, body bounds and request rate limiting.

Every guard defaults to a permissive local-demo posture and hardens through
settings. The auth guard is an app-level dependency so a newly added route can
never be exposed unauthenticated by omission, and the body limit is ASGI-level
middleware because a route dependency runs only after the server has already
buffered the whole request.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import HTTPException, Request

PUBLIC_PATHS = frozenset({"/health"})

# Multipart framing (boundary, part headers, trailing CRLFs) sits on top of the
# file itself. The early ASGI limit therefore allows a small envelope so a
# legitimate maximum-size file is rejected by the endpoint's precise check,
# with its localized message, instead of by the transport-level guard.
MULTIPART_ENVELOPE_MARGIN_BYTES = 64 * 1024

# Idle rate-limit buckets are pruned as they are observed, but a burst of
# single-request clients would still grow the map without an explicit cap.
MAX_TRACKED_RATE_LIMIT_KEYS = 10_000

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class ApiKeyGuard:
    """Require ``X-API-Key`` on every non-public route when a key is configured."""

    def __init__(self, expected_key: str | None) -> None:
        self._expected = (expected_key or "").strip() or None

    async def __call__(self, request: Request) -> None:
        if self._expected is None or request.url.path in PUBLIC_PATHS:
            return
        supplied = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied.encode(), self._expected.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")


class RequestBodyTooLargeError(Exception):
    """Internal signal that a request body exceeded the configured ceiling."""


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before the server buffers them.

    ``UploadFile`` size checks inside a route run only after Starlette has
    already streamed the whole multipart body into a spooled temporary file,
    which rolls over to disk without an upper bound. This middleware caps the
    declared ``Content-Length`` and also counts the bytes actually received, so
    a chunked upload cannot bypass the declared-size check either.
    """

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self._max_body_bytes:
            await _send_payload_too_large(send)
            return

        received = 0
        response_started = False

        async def counted_receive() -> MutableMapping[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise RequestBodyTooLargeError
            return message

        async def tracking_send(message: MutableMapping[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, counted_receive, tracking_send)
        except RequestBodyTooLargeError:
            if not response_started:
                await _send_payload_too_large(send)


def _declared_content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_payload_too_large(send: Send) -> None:
    body = b'{"detail":"O arquivo excede o limite aceito pela API."}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SlidingWindowRateLimiter:
    """In-process per-client sliding window; a limit below 1 disables it.

    Exhausted buckets are dropped as they are observed and the map is capped,
    so a stream of one-request clients cannot turn the limiter itself into the
    memory leak it exists to prevent.
    """

    def __init__(
        self,
        limit_per_minute: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_keys: int = MAX_TRACKED_RATE_LIMIT_KEYS,
    ) -> None:
        self._limit = limit_per_minute
        self._window = window_seconds
        self._clock = clock
        self._max_tracked_keys = max_tracked_keys
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self._limit < 1:
            return True
        now = self._clock()
        self._evict_expired(now)
        events = self._events[key]
        while events and now - events[0] >= self._window:
            events.popleft()
        if len(events) >= self._limit:
            return False
        events.append(now)
        return True

    def _evict_expired(self, now: float) -> None:
        """Drop buckets whose whole window has passed, oldest activity first."""
        if len(self._events) < self._max_tracked_keys:
            return
        stale = [
            key
            for key, events in self._events.items()
            if not events or now - events[-1] >= self._window
        ]
        for key in stale:
            del self._events[key]
        if len(self._events) < self._max_tracked_keys:
            return
        # Every tracked client is still inside its window. Drop the least
        # recently active buckets so the map stays bounded; the worst case is
        # that a quiet client gets a fresh allowance.
        ordered = sorted(self._events.items(), key=lambda item: item[1][-1])
        for key, _ in ordered[: len(self._events) - self._max_tracked_keys + 1]:
            del self._events[key]


class RateLimiterRegistry:
    """Named per-route-class limiters resolved from settings at startup."""

    def __init__(self, limiters: dict[str, SlidingWindowRateLimiter]) -> None:
        self._limiters = dict(limiters)

    def get(self, name: str) -> SlidingWindowRateLimiter:
        try:
            return self._limiters[name]
        except KeyError as exc:  # pragma: no cover - wiring mistake, not input
            raise KeyError(f"unknown rate limiter: {name}") from exc


def client_identifier(request: Request, *, trusted_proxy_hops: int = 0) -> str:
    """Return the rate-limiting identity for a request.

    Behind a reverse proxy every socket peer is the proxy, which would collapse
    all callers into one bucket and let a single client throttle everyone. The
    forwarded chain is only consulted for the number of hops the operator says
    it actually runs, because any client can append entries to that header.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_hops < 1:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [item.strip() for item in forwarded.split(",") if item.strip()]
    if not hops:
        return peer
    # The rightmost entries were appended by our own trusted proxies; the entry
    # immediately left of them is the closest address we did not let the client
    # forge outright.
    index = max(0, len(hops) - trusted_proxy_hops)
    return hops[index] if index < len(hops) else hops[0]


def enforce_rate_limit(name: str) -> Callable[[Request], None]:
    """Build a dependency throttling one class of expensive endpoints."""

    def dependency(request: Request) -> None:
        registry: RateLimiterRegistry = request.app.state.rate_limiters
        identity = client_identifier(
            request, trusted_proxy_hops=request.app.state.trusted_proxy_hops
        )
        if not registry.get(name).allow(f"{name}:{identity}"):
            raise HTTPException(
                status_code=429,
                detail="Limite de requisições atingido; aguarde um minuto e tente novamente.",
            )

    return dependency


enforce_case_rate_limit = enforce_rate_limit("cases")
enforce_message_rate_limit = enforce_rate_limit("messages")
enforce_upload_rate_limit = enforce_rate_limit("uploads")
enforce_notice_rate_limit = enforce_rate_limit("notice")


__all__ = [
    "MULTIPART_ENVELOPE_MARGIN_BYTES",
    "ApiKeyGuard",
    "BodySizeLimitMiddleware",
    "RateLimiterRegistry",
    "RequestBodyTooLargeError",
    "SlidingWindowRateLimiter",
    "client_identifier",
    "enforce_case_rate_limit",
    "enforce_message_rate_limit",
    "enforce_notice_rate_limit",
    "enforce_rate_limit",
    "enforce_upload_rate_limit",
]
