"""Legal strategy schemas."""

from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.common import ConfidentConclusion


class ActionPriority(str, Enum):
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DefenseOption(BaseModel):
    """A possible line of defense."""

    argument: str = Field(description="The defense argument")
    legal_basis: str | None = Field(
        default=None, description="Statute/article/precedent supporting it"
    )
    assessment: ConfidentConclusion = Field(
        description="Viability of this defense, with reasoning"
    )


class RecommendedAction(BaseModel):
    """A preliminary next step for a legal professional to review."""

    action: str
    priority: ActionPriority
    rationale: str = Field(description="Why this action, why this priority")


class StrategyPlan(BaseModel):
    """Initial hypotheses for professional review, never legal advice or a decision."""

    overall_approach: ConfidentConclusion = Field(
        description=(
            "Preliminary options to consider (contest / negotiate / hybrid) with "
            "reasoning; never a final instruction"
        )
    )
    defenses: list[DefenseOption] = Field(default_factory=list)
    settlement: ConfidentConclusion = Field(
        description="Settlement considerations and a range only when directly "
        "derivable from document values; never an outcome prediction"
    )
    next_actions: list[RecommendedAction] = Field(default_factory=list)
    missing_information: list[str] = Field(
        default_factory=list,
        description="Information not in the document that the legal team "
        "should obtain before finalizing the strategy",
    )
