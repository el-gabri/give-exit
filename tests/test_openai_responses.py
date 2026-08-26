"""Offline contract for the OpenAI Responses structured-output path."""

from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from app.llm.openai_client import OpenAIClient


class _Answer(BaseModel):
    text: str


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text='{"text":"resposta validada"}',
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            status="completed",
            error=None,
        )


async def test_responses_parse_uses_low_reasoning_and_strict_json_schema() -> None:
    responses = _FakeResponses()
    client = OpenAIClient(
        "test-key",
        "gpt-5.6-terra",
        client=SimpleNamespace(responses=responses),
    )

    result = await client.parse(
        system="policy",
        user="case packet",
        schema=_Answer,
        reasoning_effort="low",
        max_output_tokens=2500,
        prompt_version="notice-v1",
    )

    assert result.data == _Answer(text="resposta validada")
    assert result.meta.usage.prompt_tokens == 11
    assert result.meta.usage.completion_tokens == 7
    [call] = responses.calls
    assert call["model"] == "gpt-5.6-terra"
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == 2500
    assert call["store"] is False
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
