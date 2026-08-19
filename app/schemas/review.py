"""Human review decisions for analyses halted by the security gate."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class HumanReviewDecision(BaseModel):
    """A reviewer's verdict on a run halted as ``review_required``.

    Approval resumes automated analysis with the security findings still
    masked from every prompt; it never lifts a ``blocked`` outcome or an
    incomplete scan.
    """

    approved: bool
    reviewer: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2_000)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
