"""Transient-error retry behavior of the LLM port decorator."""

import pytest
from pydantic import BaseModel

from app.core.config import LLMProvider, Settings
from app.llm.base import LLMCallMetadata, ParsedResult, TextResult, TokenUsage
from app.llm.factory import create_llm_client
from app.llm.mock_client import MockLLMClient
from app.llm.retry import RetryingLLMClient, is_transient_llm_error


class RateLimitError(Exception):
    """Name-compatible stand-in for a provider rate-limit error."""


class _Verdict(BaseModel):
    ok: bool = True


class FlakyClient:
    """Fails a configurable number of times before succeeding."""

    def __init__(self, failures: int, error: Exception) -> None:
        self._failures = failures
        self._error = error
        self.calls = 0

    async def complete(self, **kwargs: object) -> TextResult:
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error
        return TextResult(text="ok", meta=self._meta())

    async def parse(self, *, schema: type[_Verdict], **kwargs: object) -> ParsedResult[_Verdict]:
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error
        return ParsedResult[schema](data=schema(), meta=self._meta())  # type: ignore[valid-type]

    @staticmethod
    def _meta() -> LLMCallMetadata:
        return LLMCallMetadata(
            provider="fake", model="fake", latency_ms=1.0, usage=TokenUsage()
        )


def _retrying(inner: FlakyClient, attempts: int = 3) -> tuple[RetryingLLMClient, list[float]]:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = RetryingLLMClient(
        inner, max_attempts=attempts, base_delay_seconds=0.01, sleep=record_sleep
    )
    return client, delays


async def test_transient_errors_are_retried_until_success() -> None:
    inner = FlakyClient(failures=2, error=RateLimitError("429"))
    client, delays = _retrying(inner)

    result = await client.parse(system="s", user="u", schema=_Verdict)

    assert result.data.ok
    assert inner.calls == 3
    assert len(delays) == 2


async def test_non_transient_errors_surface_immediately() -> None:
    inner = FlakyClient(failures=5, error=ValueError("schema refusal"))
    client, delays = _retrying(inner)

    with pytest.raises(ValueError):
        await client.complete(system="s", user="u")

    assert inner.calls == 1
    assert delays == []


async def test_exhausted_attempts_reraise_the_last_error() -> None:
    inner = FlakyClient(failures=5, error=RateLimitError("429"))
    client, delays = _retrying(inner, attempts=3)

    with pytest.raises(RateLimitError):
        await client.complete(system="s", user="u")

    assert inner.calls == 3
    assert len(delays) == 2


def test_transient_predicate_uses_status_codes_and_names() -> None:
    class APIStatusError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    assert is_transient_llm_error(APIStatusError(503))
    assert not is_transient_llm_error(APIStatusError(401))
    assert is_transient_llm_error(RateLimitError())
    assert not is_transient_llm_error(ValueError())


def test_factory_wraps_real_providers_but_not_the_mock() -> None:
    mock = create_llm_client(Settings(llm_provider=LLMProvider.MOCK, _env_file=None))
    assert isinstance(mock, MockLLMClient)

    openai = create_llm_client(
        Settings(llm_provider=LLMProvider.OPENAI, openai_api_key="sk-test", _env_file=None)
    )
    assert isinstance(openai, RetryingLLMClient)
