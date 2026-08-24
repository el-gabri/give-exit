"""Shared explainability primitives.

Every important conclusion in the system is a ``ConfidentConclusion``:
statement + confidence + reasoning + citations. This is the mechanism that
makes the product auditable instead of a black box.

Note on validation strategy: OpenAI structured outputs accepts only a subset
of JSON Schema, and numeric ``minimum``/``maximum`` keywords may be rejected
server-side. We therefore keep the generated schema plain and enforce the
0..1 confidence range with a client-side Pydantic validator. Invalid values
are rejected rather than clamped because coercing an uncalibrated model score
to 100% would create a misleading safety signal.
"""

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    """Stable evidence pointer enriched by the backend before report delivery.

    The model is responsible only for selecting a ``chunk_id`` that appeared in
    its context. ``quote`` and ``page`` remain on the public report contract,
    but they are derived from the indexed chunk and parsed document rather than
    trusted from model output.
    """

    chunk_id: str = Field(
        min_length=1,
        description="Required RAG chunk id selected from the supplied context",
    )
    quote: str = Field(
        default="",
        description="Backend-reconstructed verbatim excerpt; model input is ignored",
    )
    page: int | None = Field(
        default=None,
        ge=1,
        description="Backend-reconstructed 1-based page number",
    )


class ConfidentConclusion(BaseModel):
    """A model conclusion with explicit, auditable support fields."""

    statement: str = Field(description="The conclusion itself")
    confidence: float = Field(
        description=(
            "Uncalibrated model self-assessment of textual support between 0.0 "
            "and 1.0; not an outcome probability"
        )
    )
    reasoning: str = Field(
        description="WHY this conclusion follows from the evidence"
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="Document excerpts supporting the conclusion",
    )

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: float) -> float:
        """Reject values outside the documented, uncalibrated score range."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v

    @property
    def confidence_pct(self) -> int:
        return round(self.confidence * 100)
