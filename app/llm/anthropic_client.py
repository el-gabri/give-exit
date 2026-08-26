"""Anthropic implementation of the provider-agnostic LLM port."""

import time
from typing import Any

from anthropic import AsyncAnthropic

from app.core.logging import get_logger
from app.llm.base import (
    LLMCallMetadata,
    ParsedResult,
    SchemaT,
    TextResult,
    TokenUsage,
)
from app.llm.pricing import estimate_cost_usd

logger = get_logger(__name__)

PROVIDER_NAME = "anthropic"
_FAILED_STOP_REASONS = {"refusal", "max_tokens", "model_context_window_exceeded"}


class AnthropicClient:
    """LLMClient backed by Claude's native Messages API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = 8192,
        client: Any | None = None,
    ) -> None:
        self._client = client or AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        prompt_version: str | None = None,
    ) -> TextResult:
        # Current Claude models use adaptive thinking and reject non-default
        # sampling controls. Keep the port parameter for compatibility but do
        # not forward it to the provider.
        del temperature
        start = time.perf_counter()
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        self._raise_for_incomplete_response(response)
        text_parts: list[str] = []
        for block in response.content:
            block_text = getattr(block, "text", None)
            if getattr(block, "type", None) == "text" and isinstance(block_text, str):
                text_parts.append(block_text)
        text = "\n".join(text_parts)
        if not text:
            raise ValueError("Claude returned no text content")

        meta = self._build_meta(response, start, prompt_version)
        logger.info(
            "llm_complete",
            request_id=getattr(response, "_request_id", None),
            stop_reason=getattr(response, "stop_reason", None),
            **meta.model_dump(),
        )
        return TextResult(text=text, meta=meta)

    async def parse(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        temperature: float = 0.0,
        prompt_version: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
    ) -> ParsedResult[SchemaT]:
        del temperature, reasoning_effort
        start = time.perf_counter()
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=max_output_tokens or self._max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        self._raise_for_incomplete_response(response)
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ValueError(f"Claude did not return valid {schema.__name__} output")
        data = parsed if isinstance(parsed, schema) else schema.model_validate(parsed)

        meta = self._build_meta(response, start, prompt_version)
        logger.info(
            "llm_parse",
            schema=schema.__name__,
            request_id=getattr(response, "_request_id", None),
            stop_reason=getattr(response, "stop_reason", None),
            **meta.model_dump(),
        )
        return ParsedResult[schema](data=data, meta=meta)  # type: ignore[valid-type]

    @staticmethod
    def _raise_for_incomplete_response(response: Any) -> None:
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason in _FAILED_STOP_REASONS:
            raise ValueError(f"Claude response was incomplete: stop_reason={stop_reason}")

    def _build_meta(
        self, response: Any, start: float, prompt_version: str | None
    ) -> LLMCallMetadata:
        usage = getattr(response, "usage", None)
        token_usage = TokenUsage(
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        model = str(getattr(response, "model", None) or self._model)
        cost_usd = estimate_cost_usd(model, token_usage)
        if cost_usd is None and model != self._model:
            cost_usd = estimate_cost_usd(self._model, token_usage)
        return LLMCallMetadata(
            provider=PROVIDER_NAME,
            model=model,
            latency_ms=(time.perf_counter() - start) * 1000,
            usage=token_usage,
            cost_usd=cost_usd,
            prompt_version=prompt_version,
        )
