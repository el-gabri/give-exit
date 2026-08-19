"""Composition root for LLM clients.

The ONLY module allowed to know which concrete providers exist. Everything
else depends on the ``LLMClient`` protocol.
"""

from app.core.config import LLMProvider, Settings
from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMClient
from app.llm.gemini_client import GeminiClient
from app.llm.mock_client import MockLLMClient
from app.llm.openai_client import OpenAIClient
from app.llm.retry import RetryingLLMClient

_DEFAULT_MODELS: dict[LLMProvider, str] = {
    LLMProvider.OPENAI: "gpt-4o-mini",
    LLMProvider.ANTHROPIC: "claude-sonnet-5",
    LLMProvider.GEMINI: "gemini-3.6-flash",
}


def create_llm_client(settings: Settings) -> LLMClient:
    """Build the configured LLM client.

    Raises:
        ValueError: if the provider needs credentials that are missing.
    """
    if settings.llm_provider is LLMProvider.MOCK:
        return MockLLMClient()
    return RetryingLLMClient(
        _create_provider_client(settings),
        max_attempts=settings.llm_retry_max_attempts,
    )


def _create_provider_client(settings: Settings) -> LLMClient:
    model = _model_for(settings)
    if settings.llm_provider is LLMProvider.OPENAI:
        api_key = _required_key(settings.openai_api_key, provider="openai", variable="OPENAI")
        return OpenAIClient(api_key=api_key, model=model)

    if settings.llm_provider is LLMProvider.ANTHROPIC:
        api_key = _required_key(
            settings.anthropic_api_key, provider="anthropic", variable="ANTHROPIC"
        )
        return AnthropicClient(
            api_key=api_key,
            model=model,
            max_output_tokens=settings.llm_max_output_tokens,
        )

    if settings.llm_provider is LLMProvider.GEMINI:
        api_key = _required_key(settings.gemini_api_key, provider="gemini", variable="GEMINI")
        return GeminiClient(
            api_key=api_key,
            model=model,
            max_output_tokens=settings.llm_max_output_tokens,
        )

    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _model_for(settings: Settings) -> str:
    override = (settings.llm_model or "").strip()
    if override:
        return override
    try:
        return _DEFAULT_MODELS[settings.llm_provider]
    except KeyError as exc:  # pragma: no cover - guarded by the factory branches
        raise ValueError(f"No default model for LLM provider: {settings.llm_provider}") from exc


def _required_key(value: str | None, *, provider: str, variable: str) -> str:
    key = (value or "").strip()
    if not key:
        raise ValueError(
            f"LITIGATION_{variable}_API_KEY is required when "
            f"LITIGATION_LLM_PROVIDER={provider}. Set it in .env or explicitly "
            "use LITIGATION_LLM_PROVIDER=mock for the offline demo."
        )
    return key
