"""Risk assessment agent."""

from pydantic import BaseModel

from app.agents.base import BaseAgent, BuiltAgentPrompt
from app.agents.context import format_retrieval_bundle, retrieve_for_queries_with_trace
from app.llm.base import LLMClient
from app.prompts.risk import RISK_PROMPT
from app.rag.pipeline import RagPipeline
from app.schemas.risk import RiskAssessment

RISK_QUERIES = [
    "valor da causa condenacao indenizacao multa",
    "tutela de urgencia liminar antecipacao",
    "inversao do onus da prova hipossuficiencia",
    "danos morais materiais lucros cessantes",
]

MAX_JSON_CHARS = 6_000


class RiskAssessmentAgent(BaseAgent[RiskAssessment]):
    """Triages document-grounded risk allegations for professional review."""

    name = "risk_assessment"
    prompt = RISK_PROMPT
    output_schema = RiskAssessment

    def __init__(self, llm: LLMClient, rag: RagPipeline) -> None:
        super().__init__(llm)
        self._rag = rag

    async def build_user_prompt(self, state: object) -> BuiltAgentPrompt:
        document = state.document  # type: ignore[attr-defined]
        bundle = await retrieve_for_queries_with_trace(
            self._rag,
            doc_id=document.doc_id,
            queries=RISK_QUERIES,
            agent=self.name,
        )
        extraction = state.extraction  # type: ignore[attr-defined]
        analysis = state.legal_analysis  # type: ignore[attr-defined]
        context, retrievals = format_retrieval_bundle(
            document,
            bundle,
            security_assessment=getattr(state, "security_assessment", None),
        )
        return BuiltAgentPrompt(
            text=self.prompt.render_user(
                language=document.language,
                context=context,
                extraction_json=_dump(extraction),
                analysis_json=_dump(analysis),
            ),
            retrievals=retrievals,
        )


def _dump(model: BaseModel | None) -> str:
    if model is None:
        return "(indisponivel)"
    return model.model_dump_json()[:MAX_JSON_CHARS]
