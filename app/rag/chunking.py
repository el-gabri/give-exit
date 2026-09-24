"""Section-aware chunking for Brazilian legal documents.

Strategy (see ADR 0006):
1. Detect section headings - petitions are strongly structured
   ("DOS FATOS", "DO DIREITO", "DOS PEDIDOS", "I - PRELIMINARMENTE"...).
   Heuristic: short lines whose letters are (almost) all uppercase.
2. Chunk WITHIN sections, never across them - a chunk mixing facts with
   requests pollutes retrieval across otherwise independent questions.
3. Pack whole paragraphs greedily up to a target size, with a character
   overlap between consecutive chunks of the same section.
4. Every chunk keeps provenance: section title + page span, powering the
   citations shown to the user.

Evidence mode (``page_preserving=True``) skips heading detection: each page is
one untitled section and every nonempty block is body text. Uploaded evidence
is not a petition. A receipt or bank statement read by OCR is often nothing but
short uppercase lines, which the heuristic turns into section titles, and a
title with no body text after it is discarded - with the receipt's only content.
Evidence is the only document the application chunks, so this is the default.

A paragraph longer than the target is split at a sentence end or, failing
that, a word break, never inside a word or an amount, and the overlap carried
into the next chunk starts on a word boundary.
"""

from dataclasses import dataclass, field

from app.schemas.document import ParsedDocument
from app.schemas.rag import Chunk

HEADING_MAX_CHARS = 80
HEADING_MIN_LETTERS = 3
HEADING_UPPER_RATIO = 0.9
# A word break right after one of these would split an amount from its value.
_CURRENCY_SYMBOLS = ("R$", "US$", "$", "€")


def is_heading(line: str) -> bool:
    """True if the line looks like a legal section heading."""
    stripped = line.strip()
    if not stripped or len(stripped) > HEADING_MAX_CHARS:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < HEADING_MIN_LETTERS:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= HEADING_UPPER_RATIO


@dataclass
class _Section:
    title: str | None
    paragraphs: list[tuple[int, str]] = field(default_factory=list)  # (page, text)


def _paragraphs(text: str) -> list[str]:
    """The page's nonempty blocks, each collapsed onto one line."""
    return [
        paragraph
        for paragraph in (" ".join(block.split()) for block in text.split("\n\n"))
        if paragraph
    ]


def _split_sections(document: ParsedDocument) -> list[_Section]:
    sections: list[_Section] = [_Section(title=None)]
    for page in document.pages:
        for paragraph in _paragraphs(page.text):
            if is_heading(paragraph):
                sections.append(_Section(title=paragraph))
            else:
                sections[-1].paragraphs.append((page.number, paragraph))
    return [s for s in sections if s.paragraphs]


def _page_sections(document: ParsedDocument) -> list[_Section]:
    sections = [
        _Section(
            title=None,
            paragraphs=[(page.number, paragraph) for paragraph in _paragraphs(page.text)],
        )
        for page in document.pages
    ]
    return [s for s in sections if s.paragraphs]


class SectionAwareChunker:
    """Splits a ParsedDocument into retrieval-ready chunks."""

    VERSION = "section-aware-v2"

    def __init__(
        self,
        target_chars: int = 1200,
        overlap_chars: int = 150,
        *,
        page_preserving: bool = True,
    ) -> None:
        if overlap_chars >= target_chars:
            raise ValueError("overlap_chars must be smaller than target_chars")
        self._target = target_chars
        self._overlap = overlap_chars
        self._page_preserving = page_preserving

    @property
    def index_version(self) -> str:
        """Configuration fingerprint recorded in retrieval audit traces."""
        version = f"{self.VERSION}:target={self._target}:overlap={self._overlap}"
        return f"{version}:page-preserving" if self._page_preserving else version

    def chunk(
        self, document: ParsedDocument, *, doc_id: str | None = None
    ) -> list[Chunk]:
        sections = (
            _page_sections(document) if self._page_preserving else _split_sections(document)
        )
        chunks: list[Chunk] = []
        for section in sections:
            chunks.extend(
                self._chunk_section(doc_id or document.doc_id, section, len(chunks))
            )
        return chunks

    def _chunk_section(
        self, doc_id: str, section: _Section, start_index: int
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        buffer: list[tuple[int, str]] = []
        size = 0

        def flush() -> None:
            nonlocal buffer, size
            if not buffer:
                return
            index = start_index + len(chunks)
            text = "\n".join(p for _, p in buffer)
            pages = [pg for pg, _ in buffer]
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:{index:04d}",
                    doc_id=doc_id,
                    text=text if section.title is None else f"[{section.title}]\n{text}",
                    section=section.title,
                    page_start=min(pages),
                    page_end=max(pages),
                )
            )
            # keep tail of the last paragraph as overlap for continuity
            last_page, last_paragraph = buffer[-1]
            tail = _word_aligned_tail(last_paragraph, self._overlap)
            buffer = [(last_page, tail)] if len(last_paragraph) > self._overlap else []
            size = sum(len(p) for _, p in buffer)

        for page, paragraph in section.paragraphs:
            # Leave room for the overlap so a split piece plus the carried
            # tail still fits the target.
            pieces = (
                [paragraph]
                if len(paragraph) <= self._target
                else split_at_boundaries(paragraph, self._target - self._overlap)
            )
            for piece in pieces:
                if size + len(piece) > self._target and buffer:
                    flush()
                buffer.append((page, piece))
                size += len(piece)
        # final flush without seeding overlap
        if buffer:
            index = start_index + len(chunks)
            text = "\n".join(p for _, p in buffer)
            pages = [pg for pg, _ in buffer]
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:{index:04d}",
                    doc_id=doc_id,
                    text=text if section.title is None else f"[{section.title}]\n{text}",
                    section=section.title,
                    page_start=min(pages),
                    page_end=max(pages),
                )
            )
        return chunks


def split_at_boundaries(text: str, max_chars: int) -> list[str]:
    """Split text into pieces of at most ``max_chars`` at natural boundaries.

    The last sentence or clause end in reach wins when it keeps at least half
    the budget, then the last word break that does not separate a currency
    symbol from its amount; only a single word longer than half the budget is
    cut where it stands.
    """
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        boundary = max(remaining.rfind(". ", 0, max_chars), remaining.rfind("; ", 0, max_chars))
        if boundary < max_chars // 2:
            boundary = remaining.rfind(" ", 0, max_chars)
            while boundary > 0 and remaining[:boundary].endswith(_CURRENCY_SYMBOLS):
                boundary = remaining.rfind(" ", 0, boundary)
        if boundary < max_chars // 2:
            boundary = max_chars
        else:
            boundary += 1
        pieces.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _word_aligned_tail(paragraph: str, overlap: int) -> str:
    """The paragraph's last ``overlap`` characters, starting at a word."""
    tail = paragraph[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail
