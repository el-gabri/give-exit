"""Risk assessment schemas."""

from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.common import ConfidentConclusion


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskItem(BaseModel):
    """One identified legal or financial risk."""

    title: str = Field(description="Short label, e.g. 'Inversao do onus da prova'")
    level: RiskLevel
    conclusion: ConfidentConclusion = Field(
        description="Why this is a risk, with confidence and citations"
    )
    financial_exposure: str | None = Field(
        default=None,
        description="Estimated exposure when derivable from the document "
        "(e.g. 'R$ 20.000,00 + honorarios'), else null",
    )


class RiskAssessment(BaseModel):
    """Preliminary triage of risks alleged or observable in the filing."""

    overall_level: RiskLevel
    overall: ConfidentConclusion = Field(
        description=(
            "Overall document-based triage with explicit reasoning; level and "
            "confidence are not probabilities of a legal outcome"
        )
    )
    risks: list[RiskItem] = Field(default_factory=list)
