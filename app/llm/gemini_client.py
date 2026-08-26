"""Google Gemini implementation of the provider-agnostic LLM port."""

import time
from typing import Any

from google import genai
from google.genai import types

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

PROVIDER_NAME = "gemini"


class GeminiClient:
    """LLMClient backed by the native asynchronous Google GenAI SDK."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = 8192,
        client: Any | None = None,
    ) -> None:
        self._client = client or genai.Client(api_key=api_key)
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
        # Gemini 3.6 deprecated sampling parameters, including temperature.
        del temperature
        start = time.perf_counter()
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=self._max_output_tokens,
            ),
        )
        text = _response_text(response)
        if not text:
            raise ValueError(f"Gemini returned no text content ({_failure_reason(response)})")

        meta = self._build_meta(response, start, prompt_version)
        logger.info(
            "llm_complete",
            response_id=getattr(response, "response_id", None),
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
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_output_tokens or self._max_output_tokens,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if parsed is None:
            text = _response_text(response)
            if not text:
                raise ValueError(
                    f"Gemini did not return valid {schema.__name__} output "
                    f"({_failure_reason(response)})"
                )
            data = schema.model_validate_json(text)
        else:
            data = parsed if isinstance(parsed, schema) else schema.model_validate(parsed)

        meta = self._build_meta(response, start, prompt_version)
        logger.info(
            "llm_parse",
            schema=schema.__name__,
            response_id=getattr(response, "response_id", None),
            **meta.model_dump(),
        )
        return ParsedResult[schema](data=data, meta=meta)  # type: ignore[valid-type]

    def _build_meta(
        self, response: Any, start: float, prompt_version: str | None
    ) -> LLMCallMetadata:
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        candidate_tokens = getattr(usage, "candidates_token_count", 0) or 0
        total_tokens = getattr(usage, "total_token_count", 0) or 0
        completion_tokens = max(candidate_tokens, max(0, total_tokens - prompt_tokens))
        token_usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        model = str(getattr(response, "model_version", None) or self._model)
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


def _response_text(response: Any) -> str:
    try:
        return str(getattr(response, "text", None) or "")
    except (AttributeError, IndexError, TypeError, ValueError):
        return ""


def _failure_reason(response: Any) -> str:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(prompt_feedback, "block_reason", None)
    if block_reason:
        return f"block_reason={block_reason}"
    candidates = getattr(response, "candidates", None) or []
    finish_reasons = [
        str(getattr(candidate, "finish_reason", "unknown")) for candidate in candidates
    ]
    return f"finish_reasons={','.join(finish_reasons) or 'none'}"
