"""Schemas for the retrieval layer."""

from pydantic import BaseModel, Field

MetadataValue = str | int | float | bool | None

# Retrieval channels recorded in ``RetrievedChunk.channel_ranks``.
DENSE_CHANNEL = "dense"
LEXICAL_CHANNEL = "lexical"


class Chunk(BaseModel):
    """A retrievable slice of a document, with provenance."""

    chunk_id: str = Field(description="Stable id: '{doc_id}:{index:04d}'")
    doc_id: str
    text: str
    section: str | None = Field(
        default=None, description="Heading of the section this chunk belongs to"
    )
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    metadata: dict[str, MetadataValue] = Field(
        default_factory=dict,
        description=(
            "Structured source provenance such as law/article, corpus release, "
            "official URL and content hash"
        ),
    )


class RetrievedChunk(BaseModel):
    """A chunk returned by similarity search."""

    chunk: Chunk
    score: float = Field(description="Similarity score (higher = more relevant)")
    channel_ranks: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "1-based rank of this chunk in each retrieval channel that returned it, "
            "such as {'dense': 3, 'lexical': 7}. A channel that did not return the "
            "chunk is absent."
        ),
    )
