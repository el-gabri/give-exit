"""OpenAI implementation of the LLMClient protocol."""

import time
from typing import Any

from openai import AsyncOpenAI

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

PROVIDER_NAME = "openai"


class OpenAIClient:
    """LLMClient backed by the OpenAI API with native structured outputs."""

    def __init__(self, api_key: str, model: str, client: Any | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        prompt_version: str | None = None,
    ) -> TextResult:
        start = time.perf_counter()
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        meta = self._build_meta(response.usage, start, prompt_version)
        text = response.choices[0].message.content or ""
        logger.info("llm_complete", **meta.model_dump())
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
        if reasoning_effort is not None:
            return await self._parse_with_responses(
                system=system,
                user=user,
                schema=schema,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_output_tokens,
                prompt_version=prompt_version,
            )
        start = time.perf_counter()
        response = await self._client.beta.chat.completions.parse(
            model=self._model,
            temperature=temperature,
            response_format=schema,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        message = response.choices[0].message
        if message.parsed is None:  # refusal or parsing failure
            raise ValueError(
                f"Model did not return valid {schema.__name__}: "
                f"{message.refusal or 'unparseable output'}"
            )
        meta = self._build_meta(response.usage, start, prompt_version)
        logger.info("llm_parse", schema=schema.__name__, **meta.model_dump())
        return ParsedResult[schema](data=message.parsed, meta=meta)  # type: ignore[valid-type]

    async def _parse_with_responses(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        reasoning_effort: str,
        max_output_tokens: int | None,
        prompt_version: str | None,
    ) -> ParsedResult[SchemaT]:
        """Use Responses for reasoning controls and strict JSON Schema output."""
        start = time.perf_counter()
        # The SDK's generated overload narrows ``effort`` to a Literal while
        # the application validates the configuration dynamically. Keeping the
        # assembled request provider-typed at the boundary avoids a false
        # negative without weakening our public adapter types.
        request: Any = {
            "model": self._model,
            "instructions": system,
            "input": user,
            "reasoning": {"effort": reasoning_effort},
            "max_output_tokens": max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                }
            },
        }
        response = await self._client.responses.create(**request)
        raw_json = getattr(response, "output_text", None)
        if not raw_json:
            status = getattr(response, "status", "unknown")
            error = getattr(response, "error", None)
            raise ValueError(
                f"OpenAI response did not return valid {schema.__name__} output "
                f"(status={status}, error={error})"
            )
        data = schema.model_validate_json(raw_json)
        meta = self._build_meta(response.usage, start, prompt_version)
        logger.info(
            "llm_parse",
            schema=schema.__name__,
            response_api="responses",
            **meta.model_dump(),
        )
        return ParsedResult[schema](data=data, meta=meta)  # type: ignore[valid-type]

    def _build_meta(
        self, usage: object, start: float, prompt_version: str | None
    ) -> LLMCallMetadata:
        token_usage = TokenUsage(
            prompt_tokens=(
                getattr(usage, "prompt_tokens", None)
                or getattr(usage, "input_tokens", 0)
                or 0
            ),
            completion_tokens=(
                getattr(usage, "completion_tokens", None)
                or getattr(usage, "output_tokens", 0)
                or 0
            ),
        )
        return LLMCallMetadata(
            provider=PROVIDER_NAME,
            model=self._model,
            latency_ms=(time.perf_counter() - start) * 1000,
            usage=token_usage,
            cost_usd=estimate_cost_usd(self._model, token_usage),
            prompt_version=prompt_version,
        )
