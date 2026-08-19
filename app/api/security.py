"""API access controls: optional shared-key auth and upload rate limiting.

Both guards default to a permissive local-demo posture and harden through
settings. The auth guard is an app-level dependency so a newly added route
can never be exposed unauthenticated by omission.
"""

import hmac
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request

PUBLIC_PATHS = frozenset({"/health"})


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


class SlidingWindowRateLimiter:
    """In-process per-client sliding window; a limit below 1 disables it."""

    def __init__(
        self,
        limit_per_minute: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit_per_minute
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self._limit < 1:
            return True
        now = self._clock()
        events = self._events[key]
        while events and now - events[0] >= self._window:
            events.popleft()
        if len(events) >= self._limit:
            return False
        events.append(now)
        return True


def enforce_upload_rate_limit(request: Request) -> None:
    """Dependency for endpoints whose handling is expensive (OCR, scan, LLM)."""
    limiter: SlidingWindowRateLimiter = request.app.state.upload_rate_limiter
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        raise HTTPException(
            status_code=429,
            detail="Limite de envios atingido; aguarde um minuto e tente novamente.",
        )
