"""Lawsuit type classifier agent."""

from app.agents.base import BaseAgent, BuiltAgentPrompt
from app.agents.context import format_retrieval_bundle, retrieve_for_queries_with_trace
from app.llm.base import LLMClient
from app.prompts.classifier import CLASSIFIER_PROMPT
from app.rag.pipeline import RagPipeline
from app.schemas.analysis import LawsuitClassification

CLASSIFICATION_QUERIES = [
    "natureza da acao classe processual assunto",
    "causa de pedir fatos relacao juridica",
    "pedidos fundamentos juridicos artigos leis codigo",
]
CLASSIFICATION_K = 3


class ClassifierAgent(BaseAgent[LawsuitClassification]):
    """Determines the area of law from bounded, auditable petition evidence."""

    name = "classifier"
    prompt = CLASSIFIER_PROMPT
    output_schema = LawsuitClassification

    def __init__(self, llm: LLMClient, rag: RagPipeline) -> None:
        super().__init__(llm)
        self._rag = rag

    async def build_user_prompt(self, state: object) -> BuiltAgentPrompt:
        document = state.document  # type: ignore[attr-defined]
        bundle = await retrieve_for_queries_with_trace(
            self._rag,
            doc_id=document.doc_id,
            queries=CLASSIFICATION_QUERIES,
            agent=self.name,
            k=CLASSIFICATION_K,
        )
        context, retrievals = format_retrieval_bundle(
            document,
            bundle,
            security_assessment=getattr(state, "security_assessment", None),
        )
        return BuiltAgentPrompt(
            text=self.prompt.render_user(language=document.language, context=context),
            retrievals=retrievals,
        )
