"""Runtime validation for citations shown in legal reports.

Evaluation metrics are useful after the fact, but a legal report must not
display a citation that cannot be traced to the uploaded document. This
module validates the claim at report-composition time and removes invalid
citations before they reach an API client or export.
"""

import re
import unicodedata
from dataclasses import dataclass

from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.document import ParsedDocument
from app.schemas.rag import Chunk
from app.schemas.report import LitigationReport

# A quote is evidence only if locating it in the source is non-trivial. Common
# short words occur on nearly every page of a petition, so matching one proves
# nothing about the conclusion it supposedly supports. The floor is deliberately
# low: real legal citations are often two words ("danos morais", "cobrancas
# indevidas"), and stripping those would cost more than the trivial matches it
# prevents.
MIN_QUOTE_CHARS = 12
MIN_QUOTE_WORDS = 2


def normalize_text(text: str) -> str:
    """Normalize text for resilient verbatim matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def is_substantive_quote(quote: str) -> bool:
    """Whether a quote is long enough for a match to carry evidentiary weight."""
    normalized = normalize_text(quote)
    return (
        len(normalized) >= MIN_QUOTE_CHARS
        and len(normalized.split()) >= MIN_QUOTE_WORDS
    )


def quote_matches(quote: str, source_text: str) -> bool:
    """Whether a quoted passage occurs in a source text after normalization."""
    normalized_quote = normalize_text(quote)
    return bool(normalized_quote) and normalized_quote in normalize_text(source_text)


def quote_verifies(quote: str, source_text: str) -> bool:
    """Whether a quote is both substantive and present in the source."""
    return is_substantive_quote(quote) and quote_matches(quote, source_text)


@dataclass(frozen=True)
class CitationValidationResult:
    total_citations: int
    verified_citations: int
    rejected_citations: int
    conclusions_without_verified_citation: int
    total_conclusions: int


def validate_report_citations(
    report: LitigationReport, document: ParsedDocument, chunks: list[Chunk]
) -> CitationValidationResult:
    """Remove citations that do not match their declared source location.

    A citation must carry a substantive quote, identify a valid page and quote
    that page. When it also names a chunk, the chunk must belong to this
    document, contain the quote, and cover the cited page. This avoids treating
    a true quote from a different page as valid evidence for a claimed
    location.
    """
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    conclusions = list(_report_conclusions(report))
    total = 0
    verified = 0
    rejected = 0
    without_verified = 0

    for event in report.timeline:
        if event.citation is None:
            continue
        total += 1
        if _citation_is_valid(event.citation, document, chunks_by_id):
            verified += 1
        else:
            rejected += 1
            event.citation = None

    for conclusion in conclusions:
        valid_citations: list[Citation] = []
        for citation in conclusion.citations:
            total += 1
            if _citation_is_valid(citation, document, chunks_by_id):
                valid_citations.append(citation)
                verified += 1
            else:
                rejected += 1
        conclusion.citations = valid_citations
        if not valid_citations:
            without_verified += 1

    return CitationValidationResult(
        total_citations=total,
        verified_citations=verified,
        rejected_citations=rejected,
        conclusions_without_verified_citation=without_verified,
        total_conclusions=len(conclusions),
    )


def _citation_is_valid(
    citation: Citation, document: ParsedDocument, chunks_by_id: dict[str, Chunk]
) -> bool:
    if not is_substantive_quote(citation.quote):
        return False
    if citation.page is None or citation.page > document.page_count:
        return False
    if not quote_matches(citation.quote, document.pages[citation.page - 1].text):
        return False
    if citation.chunk_id is None:
        return True

    chunk = chunks_by_id.get(citation.chunk_id)
    return bool(
        chunk
        and chunk.doc_id == document.doc_id
        and chunk.page_start <= citation.page <= chunk.page_end
        and quote_matches(citation.quote, chunk.text)
    )


def _report_conclusions(report: LitigationReport) -> list[ConfidentConclusion]:
    conclusions: list[ConfidentConclusion] = []
    if report.classification:
        conclusions.append(report.classification.conclusion)
    conclusions.extend(claim.assessment for claim in report.main_claims)
    conclusions.extend(report.evidence_found)
    if report.legal_risks:
        conclusions.append(report.legal_risks.overall)
        conclusions.extend(risk.conclusion for risk in report.legal_risks.risks)
    if report.suggested_strategy:
        conclusions.append(report.suggested_strategy.overall_approach)
        conclusions.append(report.suggested_strategy.settlement)
        conclusions.extend(defense.assessment for defense in report.suggested_strategy.defenses)
    return conclusions
