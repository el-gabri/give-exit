"""Output schemas of the analysis agents.

These are the ``response_format`` contracts for classifier and legal
analysis. Field descriptions are prompt engineering: the LLM reads them.
"""

from pydantic import BaseModel, Field

from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.lawsuit import LawsuitType


class LawsuitClassification(BaseModel):
    """Which area of law this lawsuit belongs to."""

    lawsuit_type: LawsuitType
    conclusion: ConfidentConclusion = Field(
        description="Confidence and reasoning for the chosen type, citing "
        "the document passages that indicate it"
    )
    secondary_types: list[LawsuitType] = Field(
        default_factory=list,
        description="Other plausible types when the case crosses areas "
        "(e.g. banking + consumer)",
    )


class TimelineEvent(BaseModel):
    """A dated event reconstructed from the document."""

    date: str | None = Field(
        default=None, description="ISO date (YYYY-MM-DD) or null if not determinable"
    )
    description: str
    citation: Citation | None = None


class ClaimAnalysis(BaseModel):
    """One claim (pedido) and a preliminary document-support assessment."""

    claim: str = Field(description="What is being requested")
    legal_basis: str | None = Field(
        default=None,
        description=(
            "Statute/article/sumula alleged in the filing; not independently "
            "validated against an authoritative legal corpus"
        ),
    )
    assessment: ConfidentConclusion = Field(
        description=(
            "How completely the filing appears to support the claim, with reasoning "
            "and citations; never an outcome prediction"
        )
    )


class LegalAnalysis(BaseModel):
    """Structured legal reading of the lawsuit."""

    executive_summary: str = Field(
        description="5-8 sentence summary a partner could read in one minute"
    )
    timeline: list[TimelineEvent] = Field(
        default_factory=list, description="Chronology of relevant events"
    )
    claims: list[ClaimAnalysis] = Field(
        default_factory=list, description="Each pedido analyzed individually"
    )
    evidence_found: list[ConfidentConclusion] = Field(
        default_factory=list,
        description="Evidence mentioned or attached, with citations",
    )
