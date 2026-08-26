"""Execution and retrieval traces for one pipeline run."""

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from app.llm.base import LLMCallMetadata
from app.schemas.rag import MetadataValue


class AgentStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class RetrievedItemTrace(BaseModel):
    """One ranked retrieval result without retaining the full legal text."""

    rank: int = Field(ge=1)
    chunk_id: str
    doc_id: str
    section: str | None = None
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    score: float
    content_sha256: str
    source_metadata: dict[str, MetadataValue] = Field(
        default_factory=dict,
        description="Structured chunk provenance, excluding the full source text",
    )
    source_url: str | None = None
    source_release_id: str | None = None
    source_snapshot_sha256: str | None = None
    source_content_sha256: str | None = None
    source_provision_id: str | None = None
    source_unit_id: str | None = None
    text_preview: str | None = Field(
        default=None, description="Bounded, whitespace-normalized preview of the indexed chunk"
    )
    selected_for_merge: bool = Field(
        default=False,
        description="Whether this query result won deduplication for the chunk",
    )
    merged_rank: int | None = Field(
        default=None,
        ge=1,
        description="Rank after results from an agent's queries are deduplicated",
    )
    included_in_context: bool = Field(
        default=False,
        description="Whether this chunk was actually included in the LLM prompt",
    )


class RetrievalTrace(BaseModel):
    """Auditable query-to-ranked-results event for one agent."""

    retrieval_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    batch_id: str = Field(
        description="Shared identifier for queries embedded and searched together"
    )
    agent: str
    doc_id: str
    query_index: int = Field(ge=0)
    query: str
    query_sha256: str
    requested_k: int = Field(ge=1)
    candidate_k: int | None = Field(
        default=None,
        ge=1,
        description="Candidates fetched before fusion/reranking",
    )
    candidate_multiplier: int | None = Field(default=None, ge=1)
    returned_count: int = Field(ge=0)
    retrieval_mode: str = "hybrid"
    embedding_model: str
    embedding_model_revision: str | None = None
    embedding_generation_id: str | None = None
    embedding_query_instruction: str | None = None
    embedding_query_instruction_sha256: str | None = None
    vector_store: str
    index_version: str
    chunking_version: str = "unknown"
    score_type: str = "cosine_similarity"
    rrf_constant: int | None = Field(default=None, ge=1)
    dense_weight: float | None = Field(default=None, gt=0)
    lexical_weight: float | None = Field(default=None, gt=0)
    reranker_model: str | None = None
    reranker_model_revision: str | None = None
    embedding_duration_ms: float = Field(default=0.0, ge=0)
    embedding_cache_hits: int = Field(default=0, ge=0)
    search_duration_ms: float = Field(default=0.0, ge=0)
    total_duration_ms: float = Field(default=0.0, ge=0)
    batch_duration_ms: float = Field(default=0.0, ge=0)
    context_truncated: bool = Field(
        default=False,
        description="Whether the assembled context omitted merged retrieval results",
    )
    degraded_mode: str | None = Field(
        default=None,
        description="Explicit fallback mode used when the configured retrieval path degraded",
    )
    degraded_reason: str | None = Field(
        default=None,
        description="Bounded dependency failure class; never contains the raw query",
    )
    error: str | None = None
    agent_status: AgentStatus | None = None
    agent_error: str | None = None
    prompt_version: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    results: list[RetrievedItemTrace] = Field(default_factory=list)


class AgentTrace(BaseModel):
    """What one agent did during a run: timing, cost, outcome."""

    agent: str
    status: AgentStatus
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Wall-clock time when the agent started work",
    )
    duration_ms: float = 0.0
    llm_meta: LLMCallMetadata | None = None
    error: str | None = None
    retrievals: list[RetrievalTrace] = Field(default_factory=list)
