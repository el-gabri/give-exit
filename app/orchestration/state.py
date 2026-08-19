"""Shared state of the analysis graph.

Lists that multiple nodes append to (traces, errors) use an additive
reducer, so nodes can run in parallel later (M3b) without write conflicts.
"""

import operator
from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.analysis import LawsuitClassification, LegalAnalysis
from app.schemas.document import ParsedDocument
from app.schemas.enrichment import DataJudEnrichment
from app.schemas.lawsuit import LawsuitExtraction
from app.schemas.rag import Chunk
from app.schemas.report import LitigationReport
from app.schemas.review import HumanReviewDecision
from app.schemas.risk import RiskAssessment
from app.schemas.security import PromptInjectionAssessment
from app.schemas.strategy import StrategyPlan
from app.schemas.trace import AgentTrace


class AnalysisState(BaseModel):
    """Everything the pipeline knows about one lawsuit analysis run."""

    document: ParsedDocument
    # Present only when a reviewer resumed a run halted as review_required.
    human_review: HumanReviewDecision | None = None

    # Filled by the graph as it advances
    security_assessment: PromptInjectionAssessment | None = None
    chunks: list[Chunk] = Field(default_factory=list)
    classification: LawsuitClassification | None = None
    extraction: LawsuitExtraction | None = None
    legal_analysis: LegalAnalysis | None = None
    risk: RiskAssessment | None = None
    strategy: StrategyPlan | None = None
    enrichment: DataJudEnrichment | None = None
    report: LitigationReport | None = None

    # Observability (append-only, parallel-safe)
    traces: Annotated[list[AgentTrace], operator.add] = Field(default_factory=list)
    errors: Annotated[list[str], operator.add] = Field(default_factory=list)
