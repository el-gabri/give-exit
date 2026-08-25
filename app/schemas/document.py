"""Schemas describing an ingested document (pre-analysis)."""

import hashlib
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class ExtractionMethod(str, Enum):
    NATIVE_TEXT = "native_text"
    OCR = "ocr"
    HYBRID = "hybrid"


class DocumentPage(BaseModel):
    """A single page of extracted text."""

    number: int = Field(ge=1, description="1-based page number")
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


class ParsedDocument(BaseModel):
    """Result of the ingestion pipeline; input to chunking and agents."""

    filename: str
    pages: list[DocumentPage]
    language: str = Field(description="ISO 639-1 code, e.g. 'pt'")
    extraction_method: ExtractionMethod
    ocr_applied: bool = False
    warnings: list[str] = Field(
        default_factory=list,
        description="Quality issues a human should know about (e.g. scanned "
        "pages without OCR available)",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def doc_id(self) -> str:
        """Content-addressed id: same file -> same id.

        Makes vector-store upserts idempotent and supports reproducible indexing.
        """
        digest = hashlib.sha256(self.full_text.encode("utf-8")).hexdigest()
        return digest[:16]

    @property
    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)
