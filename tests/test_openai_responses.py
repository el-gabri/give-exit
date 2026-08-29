"""Offline contract for the OpenAI Responses structured-output path."""

from types import SimpleNamespace
from typing import Any

import pytest
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
        max_output_tokens=1200,
        prompt_version="notice-v1",
    )

    assert result.data == _Answer(text="resposta validada")
    assert result.meta.usage.prompt_tokens == 11
    assert result.meta.usage.completion_tokens == 7
    [call] = responses.calls
    assert call["model"] == "gpt-5.6-terra"
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == 1200
    assert call["store"] is False
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True


def test_strict_schema_forbids_additional_properties() -> None:
    """Strict mode rejects a schema without additionalProperties: false.

    Sending Pydantic's schema verbatim made every Responses call fail, and the
    notice composer reported that only as a silent fallback to deterministic
    prose.
    """
    from app.llm.openai_client import strict_json_schema

    schema = strict_json_schema(_Answer)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "additionalProperties" not in _Answer.model_json_schema()


def test_strict_schema_matches_the_sdk_builder() -> None:
    """Pin our transform against the SDK's own notion of a strict schema.

    Using the SDK as an oracle in the test rather than at runtime keeps us off
    its private API while still failing loudly if it changes what strict means.
    """
    pydantic_lib = pytest.importorskip("openai.lib._pydantic")
    from app.consumer.composer import NoticeProse
    from app.llm.openai_client import strict_json_schema

    for model in (_Answer, NoticeProse):
        assert strict_json_schema(model) == pydantic_lib.to_strict_json_schema(model)


async def test_responses_path_sends_the_strict_schema() -> None:
    responses = _FakeResponses()
    client = OpenAIClient("test-key", "gpt-5.6-terra", client=SimpleNamespace(responses=responses))

    await client.parse(
        system="policy", user="packet", schema=_Answer, reasoning_effort="low"
    )

    [call] = responses.calls
    assert call["text"]["format"]["schema"]["additionalProperties"] is False
