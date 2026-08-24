"""Strategy agent."""

from app.agents.base import BaseAgent, BuiltAgentPrompt
from app.agents.context import format_retrieval_bundle, retrieve_for_queries_with_trace
from app.agents.risk import _dump
from app.llm.base import LLMClient
from app.prompts.strategy import STRATEGY_PROMPT
from app.rag.pipeline import RagPipeline
from app.schemas.strategy import StrategyPlan

STRATEGY_QUERIES = [
    "pedidos requerimentos condenacao",
    "fundamentos juridicos artigos leis",
    "provas documentos anexos",
    "prazos audiencia citacao contestacao",
]


class StrategyAgent(BaseAgent[StrategyPlan]):
    """Organizes preliminary defense options for professional review."""

    name = "strategy"
    prompt = STRATEGY_PROMPT
    output_schema = StrategyPlan

    def __init__(self, llm: LLMClient, rag: RagPipeline) -> None:
        super().__init__(llm)
        self._rag = rag

    async def build_user_prompt(self, state: object) -> BuiltAgentPrompt:
        document = state.document  # type: ignore[attr-defined]
        bundle = await retrieve_for_queries_with_trace(
            self._rag,
            doc_id=document.doc_id,
            queries=STRATEGY_QUERIES,
            agent=self.name,
        )
        context, retrievals = format_retrieval_bundle(
            document,
            bundle,
            security_assessment=getattr(state, "security_assessment", None),
        )
        return BuiltAgentPrompt(
            text=self.prompt.render_user(
                language=document.language,
                context=context,
                extraction_json=_dump(state.extraction),  # type: ignore[attr-defined]
                analysis_json=_dump(state.legal_analysis),  # type: ignore[attr-defined]
            ),
            retrievals=retrievals,
        )
