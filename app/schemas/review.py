"""Human review decisions for analyses halted by the security gate."""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


class HumanReviewDecision(BaseModel):
    """A reviewer's verdict on a run halted as ``review_required``.

    Approval resumes automated analysis with the security findings still
    masked from every prompt; it never lifts a ``blocked`` outcome or an
    incomplete scan. Decisions are immutable and bound to the job, document,
    and exact security assessment that the reviewer saw.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    decision_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    job_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    security_assessment_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Digest of the exact security assessment reviewed",
    )
    approved: bool
    reviewer: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2_000)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
