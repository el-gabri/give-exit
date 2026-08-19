"""API DTOs - the HTTP contract, separate from domain schemas."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.review import HumanReviewDecision


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    FAILED = "failed"


class StageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageStatus(BaseModel):
    name: str
    state: StageState


class JobStatus(BaseModel):
    """Progress snapshot returned by GET /analyses/{job_id}."""

    job_id: str
    filename: str
    state: JobState
    stages: list[StageStatus] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None = None
    review: HumanReviewDecision | None = None


class JobCreated(BaseModel):
    job_id: str
    status_url: str


class ReviewRequest(BaseModel):
    """Reviewer decision for a job halted as review_required."""

    approved: bool
    reviewer: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2_000)
