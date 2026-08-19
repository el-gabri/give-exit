"""Bounded retry decorator for the LLM port.

A single 429/5xx or dropped connection should not degrade a whole analysis
into a partial report. Only transient provider failures are retried; schema
refusals, auth errors and other deterministic failures surface immediately.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.logging import get_logger
from app.llm.base import LLMClient, ParsedResult, SchemaT, TextResult

logger = get_logger(__name__)

ResultT = TypeVar("ResultT")

# Matched by exception type name so one predicate covers OpenAI, Anthropic,
# Gemini and the underlying httpx stack without importing any of them.
_TRANSIENT_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "InternalServerError",
        "OverloadedError",
        "PoolTimeout",
        "RateLimitError",
        "ReadTimeout",
        "ServerError",
        "ServiceUnavailableError",
        "TimeoutError",
        "TimeoutException",
        "WriteTimeout",
    }
)
_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})


def is_transient_llm_error(error: Exception) -> bool:
    """Whether retrying the same call can plausibly succeed."""
    for attribute in ("status_code", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value in _TRANSIENT_STATUS_CODES
    return type(error).__name__ in _TRANSIENT_ERROR_NAMES


class RetryingLLMClient:
    """LLMClient decorator adding exponential backoff with jitter."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 8.0,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._inner = inner
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._rng = rng or random.Random()
        self._sleep = sleep

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        prompt_version: str | None = None,
    ) -> TextResult:
        return await self._call(
            lambda: self._inner.complete(
                system=system,
                user=user,
                temperature=temperature,
                prompt_version=prompt_version,
            )
        )

    async def parse(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        temperature: float = 0.0,
        prompt_version: str | None = None,
    ) -> ParsedResult[SchemaT]:
        return await self._call(
            lambda: self._inner.parse(
                system=system,
                user=user,
                schema=schema,
                temperature=temperature,
                prompt_version=prompt_version,
            )
        )

    async def _call(self, operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if attempt >= self._max_attempts or not is_transient_llm_error(exc):
                    raise
                delay = min(self._max_delay, self._base_delay * 2 ** (attempt - 1))
                delay *= 0.5 + self._rng.random() / 2  # jitter against thundering herds
                logger.warning(
                    "llm_call_retried",
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    delay_s=round(delay, 2),
                    error=f"{type(exc).__name__}: {exc}",
                )
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover
