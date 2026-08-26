"""Deterministic mock implementation of the LLMClient protocol.

Why a mock client is a first-class citizen and not a test hack:
- Tests run offline, fast and free (no API key in CI).
- Demos work without burning tokens.
- The pipeline can be developed end-to-end before prompts are tuned.

Usage: register canned responses per schema, or let the synthesizer build a
placeholder instance by filling required fields with type-appropriate values.
"""

import time
import types
import typing
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.llm.base import (
    LLMCallMetadata,
    ParsedResult,
    SchemaT,
    TextResult,
    TokenUsage,
)

PROVIDER_NAME = "mock"


class MockLLMClient:
    """LLMClient that returns canned or synthesized responses."""

    def __init__(
        self,
        responses: dict[type[BaseModel], BaseModel] | None = None,
        text_response: str = "[mock] resposta simulada",
        simulated_latency_ms: float = 5.0,
    ) -> None:
        self._responses = responses or {}
        self._text_response = text_response
        self._simulated_latency_ms = simulated_latency_ms
        self.calls: list[dict[str, Any]] = []  # inspectable call log for tests

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        prompt_version: str | None = None,
    ) -> TextResult:
        self.calls.append({"kind": "complete", "system": system, "user": user})
        return TextResult(text=self._text_response, meta=self._meta(prompt_version))

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
        del temperature, reasoning_effort, max_output_tokens
        self.calls.append(
            {"kind": "parse", "system": system, "user": user, "schema": schema.__name__}
        )
        canned = self._responses.get(schema)
        data = canned if canned is not None else synthesize_instance(schema)
        if not isinstance(data, schema):
            raise TypeError(
                f"Canned response for {schema.__name__} is a {type(data).__name__}"
            )
        return ParsedResult[schema](data=data, meta=self._meta(prompt_version))  # type: ignore[valid-type]

    def _meta(self, prompt_version: str | None) -> LLMCallMetadata:
        time.sleep(0)  # keep interface honest without slowing tests
        return LLMCallMetadata(
            provider=PROVIDER_NAME,
            model="mock-model",
            latency_ms=self._simulated_latency_ms,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
            cost_usd=0.0,
            prompt_version=prompt_version,
        )


def synthesize_instance(schema: type[SchemaT]) -> SchemaT:
    """Build a valid placeholder instance of any Pydantic model."""
    values = {
        name: _placeholder_for(field.annotation)
        for name, field in schema.model_fields.items()
        if field.is_required()
    }
    return schema(**values)


def _placeholder_for(annotation: Any) -> Any:  # noqa: PLR0911
    """Return a type-appropriate placeholder for a field annotation."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin in (typing.Union, types.UnionType):
        # Optional fields stay None - a mock must not invent values that the
        # schema allows to be absent (mirrors "missing information" semantics).
        if type(None) in args:
            return None
        return _placeholder_for(args[0])
    if origin in (list, set, tuple):
        return origin() if origin is not tuple else ()
    if origin is dict:
        return {}
    if annotation is str:
        return "[mock]"
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return next(iter(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return synthesize_instance(annotation)
    return None
