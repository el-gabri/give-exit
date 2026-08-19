"""The final litigation report - the product's deliverable.

Assembled deterministically by the composer (no LLM call): every section
is either a typed agent output or computed from one. See ADR 0007.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.schemas.analysis import ClaimAnalysis, LawsuitClassification, TimelineEvent
from app.schemas.common import ConfidentConclusion
from app.schemas.enrichment import DataJudEnrichment
from app.schemas.lawsuit import LawsuitExtraction
from app.schemas.risk import RiskAssessment
from app.schemas.security import PromptInjectionAssessment
from app.schemas.strategy import StrategyPlan
from app.schemas.trace import AgentTrace


class RunMetrics(BaseModel):
    """Aggregated observability for one pipeline run."""

    total_duration_ms: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    unpriced_calls: int = Field(
        default=0,
        description=(
            "LLM calls whose model has no price entry. Their spend is missing "
            "from total_cost_usd, which is therefore a lower bound."
        ),
    )
    unpriced_models: list[str] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    agents_run: int = 0
    retrieval_queries: int = 0
    retrieval_results: int = 0
    retrieval_unique_chunks: int = 0
    context_chunks: int = 0
    retrieval_duration_ms: float = 0.0
    citation_retrieval_coverage: float | None = Field(
        default=None,
        description="Fraction of chunk citations present in their producing agent's context",
    )


class LitigationReport(BaseModel):
    """Complete analysis report for one lawsuit."""

    # Provenance
    doc_id: str
    filename: str
    language: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Sections
    executive_summary: str
    classification: LawsuitClassification | None = None
    parties: LawsuitExtraction | None = Field(
        default=None, description="Parties, court, values - full extraction"
    )
    timeline: list[TimelineEvent] = Field(default_factory=list)
    main_claims: list[ClaimAnalysis] = Field(default_factory=list)
    evidence_found: list[ConfidentConclusion] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    legal_risks: RiskAssessment | None = None
    suggested_strategy: StrategyPlan | None = None
    possible_settlement: ConfidentConclusion | None = None
    security_assessment: PromptInjectionAssessment | None = Field(
        default=None,
        description="Pre-analysis prompt-injection security assessment",
    )
    datajud: DataJudEnrichment | None = Field(
        default=None, description="Validation against official CNJ court records"
    )

    # Explainability & observability
    confidence_level: float = Field(
        default=0.0,
        description="Aggregate confidence over all conclusions (mean, 0-1)",
    )
    ai_reasoning: str = Field(
        default="",
        description="How the system reached its conclusions: pipeline, "
        "models, prompt versions, retrieval statistics",
    )
    warnings: list[str] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    traces: list[AgentTrace] = Field(default_factory=list)
