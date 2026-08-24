"""Agent trace timestamps describe when work began, not trace construction."""

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from app.agents.base import AgentRunError, BaseAgent, BuiltAgentPrompt
from app.llm.mock_client import MockLLMClient
from app.prompts.base import PromptTemplate


class _Output(BaseModel):
    ok: bool = True


class _TimedLLM(MockLLMClient):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.called_at: datetime | None = None

    async def parse(self, **kwargs):  # type: ignore[override]
        self.called_at = datetime.now(timezone.utc)
        await asyncio.sleep(0.01)
        if self.fail:
            raise RuntimeError("provider failed")
        return await super().parse(**kwargs)


class _TimedAgent(BaseAgent[_Output]):
    name = "timed"
    prompt = PromptTemplate(
        name="timed",
        version="v1",
        system="system",
        user_template="{context}",
    )
    output_schema = _Output

    async def build_user_prompt(self, state: object) -> BuiltAgentPrompt:
        return BuiltAgentPrompt(text=self.prompt.render_user(context="context"))


async def test_success_trace_uses_the_actual_agent_start_time() -> None:
    llm = _TimedLLM()

    _, trace = await _TimedAgent(llm).run(object())

    assert llm.called_at is not None
    assert trace.started_at <= llm.called_at


async def test_failure_trace_preserves_the_actual_agent_start_time() -> None:
    llm = _TimedLLM(fail=True)

    with pytest.raises(AgentRunError) as raised:
        await _TimedAgent(llm).run(object())

    assert llm.called_at is not None
    assert raised.value.trace.started_at <= llm.called_at
