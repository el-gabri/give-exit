"""The optional, drop-only LLM scope check (spec 2026-10-04 §4)."""

from typing import Any

import pytest
from pydantic import BaseModel

from app.consumer.scope_verifier import (
    PROMPT_VERSION,
    LLMScopeVerifier,
    NoScopeVerifier,
    create_scope_verifier,
)
from app.core.config import ScopeVerifierMode, Settings
from app.core.hashing import sha256_hex
from app.llm.base import LLMCallMetadata, ParsedResult
from app.llm.mock_client import MockLLMClient

COMPLAINT = (
    "Aluguei a sala comercial do meu tio e ele quer aumentar o valor no meio do contrato."
)


class _FakeLLM:
    def __init__(
        self, answer: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self._answer = answer
        self._error = error

    async def complete(self, **_: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the verifier must use parse")

    async def parse(self, *, schema: type[BaseModel], **_: Any) -> ParsedResult[Any]:
        if self._error is not None:
            raise self._error
        return ParsedResult(
            data=schema.model_validate(self._answer),
            meta=LLMCallMetadata(provider="fake", model="fake", latency_ms=0.0),
        )


async def _verify(answer: dict[str, Any] | None = None, error: Exception | None = None) -> Any:
    verifier = LLMScopeVerifier(_FakeLLM(answer, error), max_output_tokens=1_000)  # type: ignore[arg-type]
    return await verifier.verify(COMPLAINT)


async def test_not_consumer_with_a_verbatim_quote_removes_the_case() -> None:
    result = await _verify(
        {
            "verdict": "not_consumer",
            "relationship": "tenancy",
            "quote": "Aluguei a sala comercial do meu tio",
        }
    )

    assert result.removed and result.verdict == "not_consumer"
    assert result.quote == "Aluguei a sala comercial do meu tio"
    assert result.relationship == "tenancy"
    assert result.complaint_sha256 == sha256_hex(COMPLAINT)
    assert result.prompt_version == PROMPT_VERSION


@pytest.mark.parametrize(
    "answer",
    [
        {
            "verdict": "not_consumer",
            "relationship": "tenancy",
            "quote": "meu tio alugou uma sala para mim",
        },
        {"verdict": "not_consumer", "relationship": "tenancy", "quote": "meu tio"},
        {"verdict": "not_consumer", "relationship": "tenancy", "quote": ""},
        {"verdict": "uncertain", "relationship": "other", "quote": ""},
        {"verdict": "consumer", "relationship": "other", "quote": ""},
    ],
)
async def test_anything_short_of_a_verified_removal_keeps_the_case(answer: dict[str, Any]) -> None:
    result = await _verify(answer)

    assert not result.removed
    assert result.quote is None


async def test_a_provider_error_keeps_the_case_and_records_why() -> None:
    result = await _verify(error=RuntimeError("provider down"))

    assert not result.removed
    assert result.verdict == "uncertain"
    assert result.error == "RuntimeError"


async def test_a_malformed_answer_keeps_the_case() -> None:
    result = await _verify({"verdict": "maybe"})

    assert not result.removed and result.error is not None


async def test_no_scope_verifier_never_removes() -> None:
    result = await NoScopeVerifier().verify(COMPLAINT)

    assert result.mode is ScopeVerifierMode.NONE and not result.removed


async def test_the_mock_provider_never_removes_a_case() -> None:
    verifier = LLMScopeVerifier(MockLLMClient(), max_output_tokens=1_000)

    result = await verifier.verify(COMPLAINT)

    assert not result.removed


def test_the_factory_honours_the_setting() -> None:
    assert isinstance(
        create_scope_verifier(Settings(scope_verifier=ScopeVerifierMode.NONE)), NoScopeVerifier
    )
    assert isinstance(
        create_scope_verifier(Settings(scope_verifier=ScopeVerifierMode.LLM)), LLMScopeVerifier
    )
