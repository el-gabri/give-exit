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
"""

from dataclasses import dataclass, field

from app.schemas.document import ParsedDocument
from app.schemas.rag import Chunk

HEADING_MAX_CHARS = 80
HEADING_MIN_LETTERS = 3
HEADING_UPPER_RATIO = 0.9


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


def _split_sections(document: ParsedDocument) -> list[_Section]:
    sections: list[_Section] = [_Section(title=None)]
    for page in document.pages:
        for raw_paragraph in page.text.split("\n\n"):
            paragraph = " ".join(raw_paragraph.split())
            if not paragraph:
                continue
            if is_heading(paragraph):
                sections.append(_Section(title=paragraph))
            else:
                sections[-1].paragraphs.append((page.number, paragraph))
    return [s for s in sections if s.paragraphs]


class SectionAwareChunker:
    """Splits a ParsedDocument into retrieval-ready chunks."""

    VERSION = "section-aware-v1"

    def __init__(self, target_chars: int = 1200, overlap_chars: int = 150) -> None:
        if overlap_chars >= target_chars:
            raise ValueError("overlap_chars must be smaller than target_chars")
        self._target = target_chars
        self._overlap = overlap_chars

    @property
    def index_version(self) -> str:
        """Configuration fingerprint recorded in retrieval audit traces."""
        return f"{self.VERSION}:target={self._target}:overlap={self._overlap}"

    def chunk(
        self, document: ParsedDocument, *, doc_id: str | None = None
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in _split_sections(document):
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
            tail = last_paragraph[-self._overlap :]
            buffer = [(last_page, tail)] if len(last_paragraph) > self._overlap else []
            size = sum(len(p) for _, p in buffer)

        for page, paragraph in section.paragraphs:
            for piece in _hard_split(paragraph, self._target):
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


def _hard_split(paragraph: str, max_chars: int) -> list[str]:
    """Split a pathological paragraph that alone exceeds the target size."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    return [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)]
