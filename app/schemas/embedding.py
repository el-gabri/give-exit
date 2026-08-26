"""Versioned contracts for reproducible embedding generations."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmbeddingGenerationStatus(str, Enum):
    """Lifecycle of one immutable document-vector generation."""

    BUILDING = "building"
    VALIDATED = "validated"
    ACTIVE = "active"
    FAILED = "failed"


class EmbeddingContract(BaseModel):
    """Everything that can change the meaning or shape of an embedding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["embedding-contract-v1"] = "embedding-contract-v1"
    model_repository: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    output_dimension: int | None = Field(default=None, ge=1)
    vector_dtype: Literal["float32"] = "float32"
    normalization: Literal["l2"] = "l2"
    document_formatter_version: str = Field(min_length=1)
    query_formatter_version: str = Field(min_length=1)
    query_instruction_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EmbeddingShardManifest(BaseModel):
    """Checksummed output for one independently resumable chunk shard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shard_index: int = Field(ge=0)
    artifact_file: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk_count: int = Field(ge=1)
    chunk_ids_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunks_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    vectors_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_dimension: int = Field(ge=1)


class EmbeddingGenerationManifest(BaseModel):
    """Durable handoff between batch embedding and the active vector index."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["embedding-generation-manifest-v1"] = (
        "embedding-generation-manifest-v1"
    )
    generation_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    status: EmbeddingGenerationStatus = EmbeddingGenerationStatus.BUILDING
    index_name: str = Field(min_length=1)
    source_index_name: str | None = None
    attested_source_model_revision: str | None = None
    provenance: Literal["embedded", "adopted_existing_vectors"] = "embedded"
    corpus_release_id: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: str = Field(min_length=1)
    chunking_version: str = Field(min_length=1)
    expected_chunk_count: int = Field(ge=1)
    expected_chunk_ids_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_chunks_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract: EmbeddingContract
    shard_size: int = Field(ge=1)
    expected_shard_count: int = Field(ge=1)
    completed_chunk_count: int = Field(default=0, ge=0)
    package_versions: dict[str, str] = Field(default_factory=dict)
    hardware: dict[str, str] = Field(default_factory=dict)
    shards: list[EmbeddingShardManifest] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validated_at: datetime | None = None
    activated_at: datetime | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_provenance_fields(self) -> EmbeddingGenerationManifest:
        if self.provenance == "adopted_existing_vectors":
            if not (self.source_index_name or "").strip():
                raise ValueError("adopted vectors require source_index_name")
            if self.attested_source_model_revision != self.contract.model_revision:
                raise ValueError(
                    "adopted vectors require an attested revision matching the contract"
                )
        elif self.source_index_name is not None or self.attested_source_model_revision is not None:
            raise ValueError("embedded generations cannot claim legacy-source attestation")
        return self
