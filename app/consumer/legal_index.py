"""Persistent, version-aware legal-corpus index lifecycle."""

from dataclasses import dataclass
from typing import Literal

from app.consumer.legal_corpus import LEGAL_CHUNKING_VERSION, LegalCorpus
from app.rag.pipeline import RagPipeline


@dataclass(frozen=True, slots=True)
class LegalIndexResult:
    """Outcome of checking or materializing one legal-corpus index."""

    action: Literal["reused", "indexed"]
    doc_id: str
    chunks: int


async def legal_corpus_is_indexed(rag: RagPipeline, corpus: LegalCorpus) -> bool:
    """Check persistence and restore the corpus chunking version for audit traces."""

    doc_id = corpus.as_parsed_document().doc_id
    ready = doc_id in await rag.list_document_ids()
    if ready:
        rag.register_indexed_document(
            doc_id,
            chunking_version=LEGAL_CHUNKING_VERSION,
        )
    return ready


async def preindex_legal_corpus(
    rag: RagPipeline,
    corpus: LegalCorpus,
    *,
    force: bool = False,
) -> LegalIndexResult:
    """Materialize the immutable corpus once and verify the persisted document."""

    chunks = corpus.as_chunks()
    doc_id = corpus.as_parsed_document().doc_id
    if not force and await legal_corpus_is_indexed(rag, corpus):
        return LegalIndexResult(action="reused", doc_id=doc_id, chunks=len(chunks))

    await rag.index_chunks(chunks)
    if not await legal_corpus_is_indexed(rag, corpus):
        raise RuntimeError("legal corpus indexing completed without a persisted document")
    return LegalIndexResult(action="indexed", doc_id=doc_id, chunks=len(chunks))
