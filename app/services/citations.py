"""Runtime reconstruction and validation for citations shown in legal reports.

Evaluation metrics are useful after the fact, but a legal report must not
display a citation that cannot be traced to the uploaded document. This
module resolves model-selected chunk ids at report-composition time, derives
the page and verbatim quote from trusted ingestion artifacts, and removes
unresolvable citations before they reach an API client or export.
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
MAX_RECONSTRUCTED_QUOTE_WORDS = 40


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
    """Resolve citation ids and replace model text with source-derived values.

    The model supplies only a chunk id. A citation is verified only when that
    id uniquely identifies a chunk belonging to this document and a
    substantive verbatim excerpt can be reconstructed from both the chunk and
    one of the source pages it covers. Any model-supplied quote or page is
    ignored. Duplicate ids are treated as ambiguous and therefore unverifiable.
    """
    chunks_by_id = _unique_chunks_by_id(chunks)
    conclusions = list(_report_conclusions(report))
    total = 0
    verified = 0
    rejected = 0
    without_verified = 0

    for event in report.timeline:
        if event.citation is None:
            continue
        total += 1
        reconstructed = _reconstruct_citation(event.citation, document, chunks_by_id)
        if reconstructed is not None:
            event.citation = reconstructed
            verified += 1
        else:
            rejected += 1
            event.citation = None

    for conclusion in conclusions:
        valid_citations: list[Citation] = []
        for citation in conclusion.citations:
            total += 1
            reconstructed = _reconstruct_citation(citation, document, chunks_by_id)
            if reconstructed is not None:
                valid_citations.append(reconstructed)
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


def _unique_chunks_by_id(chunks: list[Chunk]) -> dict[str, Chunk]:
    """Return only ids that resolve to exactly one supplied chunk."""
    unique: dict[str, Chunk] = {}
    ambiguous: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in unique:
            ambiguous.add(chunk.chunk_id)
        else:
            unique[chunk.chunk_id] = chunk
    for chunk_id in ambiguous:
        unique.pop(chunk_id, None)
    return unique


def _reconstruct_citation(
    citation: Citation, document: ParsedDocument, chunks_by_id: dict[str, Chunk]
) -> Citation | None:
    chunk = chunks_by_id.get(citation.chunk_id)
    if chunk is None or chunk.doc_id != document.doc_id:
        return None

    source = _source_excerpt(document, chunk)
    if source is None:
        return None
    page, quote = source
    return Citation(chunk_id=chunk.chunk_id, quote=quote, page=page)


def _source_excerpt(document: ParsedDocument, chunk: Chunk) -> tuple[int, str] | None:
    """Find a deterministic verbatim page span also present in ``chunk``.

    Chunks can span pages and normalize paragraph whitespace, so direct string
    slicing is not reliable. We instead find the longest contiguous sequence of
    normalized word tokens shared by each covered source page and the chunk,
    then return the corresponding characters from the original page. Ties are
    resolved by page order and earliest source position.
    """
    chunk_tokens = _tokens(chunk.text)
    if not chunk_tokens:
        return None

    best: tuple[int, int, int, str] | None = None
    for page in document.pages:
        if not chunk.page_start <= page.number <= chunk.page_end:
            continue
        match = _longest_common_token_span(page.text, chunk_tokens)
        if match is None:
            continue
        token_count, start, end = match
        quote = page.text[start:end].strip()
        if not is_substantive_quote(quote):
            continue
        candidate = (token_count, -page.number, -start, quote)
        if best is None or candidate[:3] > best[:3]:
            best = candidate

    if best is None:
        return None
    return -best[1], best[3]


@dataclass(frozen=True)
class _Token:
    canonical: str
    start: int
    end: int


def _tokens(text: str) -> list[_Token]:
    return [
        _Token(normalize_text(match.group()), match.start(), match.end())
        for match in re.finditer(r"\w+", text, flags=re.UNICODE)
    ]


def _longest_common_token_span(
    page_text: str, chunk_tokens: list[_Token]
) -> tuple[int, int, int] | None:
    """Return ``(word_count, start, end)`` for the best shared token run."""
    page_tokens = _tokens(page_text)
    if not page_tokens:
        return None

    previous = [0] * (len(chunk_tokens) + 1)
    best_length = 0
    best_end = 0
    for page_index, page_token in enumerate(page_tokens, start=1):
        current = [0] * (len(chunk_tokens) + 1)
        for chunk_index, chunk_token in enumerate(chunk_tokens, start=1):
            if page_token.canonical == chunk_token.canonical:
                current[chunk_index] = previous[chunk_index - 1] + 1
                if current[chunk_index] > best_length:
                    best_length = current[chunk_index]
                    best_end = page_index
        previous = current

    if best_length == 0:
        return None
    selected_length = min(best_length, MAX_RECONSTRUCTED_QUOTE_WORDS)
    start_index = best_end - best_length
    end_index = start_index + selected_length - 1
    return (
        selected_length,
        page_tokens[start_index].start,
        page_tokens[end_index].end,
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
