"""Provider-agnostic LLM contract.

Design goals:
1. The application never imports a vendor SDK outside ``app/llm``.
2. Every call returns rich metadata (tokens, latency, cost, model, prompt
   version) so observability is built into the type system, not bolted on.
3. Structured output is first-class: ``parse`` takes a Pydantic schema and
   returns a validated instance of it.
"""

from typing import Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class TokenUsage(BaseModel):
    """Token accounting for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMCallMetadata(BaseModel):
    """Observability envelope attached to every LLM response."""

    provider: str
    model: str
    latency_ms: float = Field(ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = None
    prompt_version: str | None = None


class TextResult(BaseModel):
    """Free-text completion plus metadata."""

    text: str
    meta: LLMCallMetadata


class ParsedResult(BaseModel, Generic[SchemaT]):
    """Schema-validated completion plus metadata."""

    data: SchemaT
    meta: LLMCallMetadata


@runtime_checkable
class LLMClient(Protocol):
    """What every LLM backend must implement.

    Swapping OpenAI for Anthropic/Gemini means writing one new class that
    satisfies this protocol - no changes anywhere else in the codebase.
    """

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        prompt_version: str | None = None,
    ) -> TextResult:
        """Return a free-text completion."""
        ...

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
        """Return a completion validated against ``schema``."""
        ...
