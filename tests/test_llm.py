"""Tests for the LLM abstraction layer."""

from enum import Enum

import pytest
from pydantic import BaseModel

from app.core.config import LLMProvider, Settings
from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMClient, TokenUsage
from app.llm.factory import create_llm_client
from app.llm.gemini_client import GeminiClient
from app.llm.mock_client import MockLLMClient, synthesize_instance
from app.llm.pricing import estimate_cost_usd
from app.llm.retry import RetryingLLMClient


class RiskLevel(str, Enum):
    LOW = "low"
    HIGH = "high"


class _Party(BaseModel):
    name: str


class _Extraction(BaseModel):
    plaintiff: _Party
    claim_value: float | None
    risk: RiskLevel
    claims: list[str]


async def test_mock_parse_returns_valid_schema_and_metadata() -> None:
    client = MockLLMClient()
    result = await client.parse(system="s", user="u", schema=_Extraction)

    assert isinstance(result.data, _Extraction)
    assert result.meta.provider == "mock"
    assert result.meta.usage.total_tokens == 150
    assert result.meta.cost_usd == 0.0


async def test_mock_uses_canned_response_when_registered() -> None:
    canned = _Extraction(
        plaintiff=_Party(name="Maria Silva"),
        claim_value=50_000.0,
        risk=RiskLevel.HIGH,
        claims=["danos morais"],
    )
    client = MockLLMClient(responses={_Extraction: canned})
    result = await client.parse(system="s", user="u", schema=_Extraction)

    assert result.data.plaintiff.name == "Maria Silva"
    assert client.calls[0]["schema"] == "_Extraction"


def test_synthesizer_handles_nested_optional_enum_and_list_fields() -> None:
    instance = synthesize_instance(_Extraction)
    assert instance.plaintiff.name == "[mock]"
    assert instance.claim_value is None
    assert instance.risk is RiskLevel.LOW
    assert instance.claims == []


def test_factory_returns_mock_client() -> None:
    settings = Settings(llm_provider=LLMProvider.MOCK, _env_file=None)
    client = create_llm_client(settings)
    assert isinstance(client, MockLLMClient)
    assert isinstance(client, LLMClient)  # protocol conformance


def test_fresh_settings_run_offline_without_an_api_key() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_provider is LLMProvider.MOCK
    assert isinstance(create_llm_client(settings), MockLLMClient)


def test_factory_rejects_openai_without_key() -> None:
    settings = Settings(llm_provider=LLMProvider.OPENAI, openai_api_key=None, _env_file=None)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_llm_client(settings)


@pytest.mark.parametrize(
    ("provider", "error"),
    [
        (LLMProvider.ANTHROPIC, "ANTHROPIC_API_KEY"),
        (LLMProvider.GEMINI, "GEMINI_API_KEY"),
    ],
)
def test_factory_rejects_new_provider_without_its_key(provider: LLMProvider, error: str) -> None:
    settings = Settings(llm_provider=provider, _env_file=None)

    with pytest.raises(ValueError, match=error):
        create_llm_client(settings)


def test_factory_uses_provider_specific_default_models() -> None:
    anthropic = create_llm_client(
        Settings(
            llm_provider=LLMProvider.ANTHROPIC,
            anthropic_api_key="anthropic-test",
            _env_file=None,
        )
    )
    gemini = create_llm_client(
        Settings(
            llm_provider=LLMProvider.GEMINI,
            gemini_api_key="gemini-test",
            _env_file=None,
        )
    )

    assert isinstance(anthropic, RetryingLLMClient)
    assert isinstance(anthropic._inner, AnthropicClient)
    assert anthropic._inner._model == "claude-sonnet-5"
    assert isinstance(gemini, RetryingLLMClient)
    assert isinstance(gemini._inner, GeminiClient)
    assert gemini._inner._model == "gemini-3.6-flash"


def test_factory_honours_model_override_for_new_providers() -> None:
    client = create_llm_client(
        Settings(
            llm_provider=LLMProvider.ANTHROPIC,
            anthropic_api_key="anthropic-test",
            llm_model="claude-haiku-4-5",
            _env_file=None,
        )
    )

    assert isinstance(client, RetryingLLMClient)
    assert isinstance(client._inner, AnthropicClient)
    assert client._inner._model == "claude-haiku-4-5"


def test_pricing_known_and_unknown_models() -> None:
    usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert estimate_cost_usd("gpt-4o-mini", usage) == pytest.approx(0.75)
    assert estimate_cost_usd("claude-sonnet-5", usage) == pytest.approx(18.0)
    assert estimate_cost_usd("gemini-3.6-flash", usage) == pytest.approx(9.0)
    assert estimate_cost_usd("gpt-5.6-terra", usage) == pytest.approx(14.0)
    assert estimate_cost_usd("some-future-model", usage) is None
