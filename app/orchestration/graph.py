"""Analysis graph: wires agents into a LangGraph state machine.

Topology:

    START -> security_scan -+-> index -> classify -> extract -> analyze -+-> risk ---+
                            |                         |          +-> strategy --+-> compose
                            |                         +-> enrich ---------------+
                            +-> security_compose -> END

Risk and strategy are independent given the legal analysis, so they run in
PARALLEL branches (the additive reducers on traces/errors make concurrent
writes safe). Compose is deterministic (no LLM, see ADR 0007) and runs for
all agent failures, producing a visibly partial report. High-risk prompt
injection, an incomplete scan, or an indexing failure skips the dependent
analysis nodes and goes straight to composition.

Every agent node is wrapped so that failures are recorded in state
(errors + a failed AgentTrace) instead of crashing the run.
"""

import time
from collections.abc import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from app.agents.base import AgentRunError, BaseAgent
from app.agents.classifier import ClassifierAgent
from app.agents.extraction import EntityExtractionAgent
from app.agents.legal_analysis import LegalAnalysisAgent
from app.agents.risk import RiskAssessmentAgent
from app.agents.strategy import StrategyAgent
from app.core.logging import get_logger
from app.enrichment.datajud import DataJudClient
from app.enrichment.node import make_enrich_node
from app.llm.base import LLMClient
from app.orchestration.state import AnalysisState
from app.rag.pipeline import RagPipeline
from app.schemas.security import SecurityAction
from app.schemas.trace import AgentStatus, AgentTrace
from app.security.prompt_injection import (
    PromptInjectionDetector,
    failed_scan_assessment,
)
from app.services.composer import compose_report

logger = get_logger(__name__)

NodeFn = Callable[[AnalysisState], Awaitable[dict]]


def _agent_node(agent: BaseAgent, state_field: str) -> NodeFn:
    """Wrap an agent as a graph node with uniform error handling."""

    async def node(state: AnalysisState) -> dict:
        start = time.perf_counter()
        try:
            output, trace = await agent.run(state)
            return {state_field: output, "traces": [trace]}
        except AgentRunError as exc:
            logger.exception("agent_failed", agent=agent.name)
            cause = exc.cause
            return {
                "errors": [f"{agent.name}: {type(cause).__name__}: {cause}"],
                "traces": [exc.trace],
            }
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception("agent_failed", agent=agent.name)
            return {
                "errors": [f"{agent.name}: {type(exc).__name__}: {exc}"],
                "traces": [agent.failure_trace(exc, duration_ms)],
            }

    return node


def _after_index(state: AnalysisState) -> str:
    return "compose" if state.errors else "classify"


def _after_security_scan(state: AnalysisState) -> str:
    assessment = state.security_assessment
    if assessment is None:
        return "security_compose"
    if assessment.recommended_action.allows_automated_analysis:
        return "index"
    # An approved human review resumes a review_required halt. Findings stay
    # masked downstream. Blocked outcomes and incomplete scans are never
    # overridden.
    if (
        assessment.scan_complete
        and assessment.recommended_action is SecurityAction.HUMAN_REVIEW
        and state.human_review is not None
        and state.human_review.approved
    ):
        return "index"
    return "security_compose"


def build_analysis_graph(
    llm: LLMClient,
    rag: RagPipeline,
    datajud: DataJudClient | None = None,
    prompt_injection_detector: PromptInjectionDetector | None = None,
):
    """Compose the analysis pipeline. Dependencies injected at the root."""

    detector = prompt_injection_detector or PromptInjectionDetector(llm)

    async def security_scan_node(state: AnalysisState) -> dict:
        start = time.perf_counter()
        try:
            assessment, trace = await detector.scan(state.document)
            result: dict = {
                "security_assessment": assessment,
                "traces": [trace],
            }
            if not assessment.scan_complete:
                result["errors"] = [
                    "security_scan: required security review was not completed"
                ]
            return result
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception("prompt_injection_scan_failed", doc_id=state.document.doc_id)
            assessment = failed_scan_assessment(
                document=state.document,
                scan_mode=detector.mode,
                error=exc,
            )
            return {
                "security_assessment": assessment,
                "errors": [f"security_scan: {type(exc).__name__}: {exc}"],
                "traces": [
                    AgentTrace(
                        agent=detector.name,
                        status=AgentStatus.FAILED,
                        duration_ms=duration_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                ],
            }

    async def index_node(state: AnalysisState) -> dict:
        try:
            chunks = await rag.index_document(
                state.document, state.security_assessment
            )
            return {"chunks": chunks}
        except Exception as exc:
            logger.exception("indexing_failed", doc_id=state.document.doc_id)
            return {"errors": [f"index: {type(exc).__name__}: {exc}"]}

    async def compose_node(state: AnalysisState) -> dict:
        # Deterministic assembly - no LLM call, cannot hallucinate.
        return {"report": compose_report(state)}

    classifier = ClassifierAgent(llm)
    extractor = EntityExtractionAgent(llm, rag)
    analyst = LegalAnalysisAgent(llm, rag)
    risk_assessor = RiskAssessmentAgent(llm, rag)
    strategist = StrategyAgent(llm, rag)

    builder = StateGraph(AnalysisState)
    builder.add_node("security_scan", security_scan_node)
    builder.add_node("index", index_node)
    builder.add_node("classify", _agent_node(classifier, "classification"))
    builder.add_node("extract", _agent_node(extractor, "extraction"))
    builder.add_node("analyze", _agent_node(analyst, "legal_analysis"))
    builder.add_node("risk", _agent_node(risk_assessor, "risk"))
    builder.add_node("strategy", _agent_node(strategist, "strategy"))
    builder.add_node("enrich", make_enrich_node(datajud))
    builder.add_node("compose", compose_node)
    builder.add_node("security_compose", compose_node)

    builder.add_edge(START, "security_scan")
    builder.add_conditional_edges(
        "security_scan",
        _after_security_scan,
        {"index": "index", "security_compose": "security_compose"},
    )
    builder.add_edge("security_compose", END)
    builder.add_conditional_edges(
        "index", _after_index, {"classify": "classify", "compose": "compose"}
    )
    builder.add_edge("classify", "extract")
    # fan-out 1: legal analysis and DataJud enrichment are independent
    builder.add_edge("extract", "analyze")
    builder.add_edge("extract", "enrich")
    # fan-out 2: risk and strategy run in parallel after the legal analysis
    builder.add_edge("analyze", "risk")
    builder.add_edge("analyze", "strategy")
    # fan-in: compose waits for all branches; runs even on partial failure
    builder.add_edge(["risk", "strategy", "enrich"], "compose")
    builder.add_edge("compose", END)

    return builder.compile()
