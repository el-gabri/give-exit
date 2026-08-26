"""Persistent, version-aware legal-corpus index lifecycle."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.consumer.embedding_generation import (
    EmbeddingGenerationManager,
    validated_vectors_for_chunks,
)
from app.consumer.legal_corpus import LEGAL_CHUNKING_VERSION, LegalCorpus
from app.rag.pipeline import RagPipeline
from app.schemas.rag import Chunk


@dataclass(frozen=True, slots=True)
class LegalIndexResult:
    """Outcome of checking or materializing one legal-corpus index."""

    action: Literal["reused", "indexed"]
    doc_id: str
    chunks: int
    generation_id: str | None = None
    manifest_path: Path | None = None


async def legal_corpus_is_indexed(rag: RagPipeline, corpus: LegalCorpus) -> bool:
    """Check persistence and restore the corpus chunking version for audit traces."""

    doc_id = corpus.as_parsed_document().doc_id
    if doc_id not in await rag.list_document_ids():
        return False
    generation_id: str | None = None
    if rag.embedding_artifacts_dir is not None:
        try:
            manager = EmbeddingGenerationManager(rag, corpus)
            ready = await manager.is_ready()
            generation_id = manager.generation_id if ready else None
        except ValueError:
            return False
    else:
        try:
            entries = await rag.export_document(doc_id)
            validated_vectors_for_chunks(
                sorted(corpus.as_chunks(), key=lambda chunk: chunk.chunk_id),
                entries,
                expected_dimension=None,
            )
        except (RuntimeError, TypeError, ValueError):
            return False
        ready = True
    if ready:
        rag.register_indexed_document(
            doc_id,
            chunking_version=LEGAL_CHUNKING_VERSION,
            embedding_generation_id=generation_id,
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
        manager = (
            EmbeddingGenerationManager(rag, corpus)
            if rag.embedding_artifacts_dir is not None
            else None
        )
        return LegalIndexResult(
            action="reused",
            doc_id=doc_id,
            chunks=len(chunks),
            generation_id=manager.generation_id if manager is not None else None,
            manifest_path=manager.manifest_path if manager is not None else None,
        )

    if rag.embedding_artifacts_dir is not None:
        manager = EmbeddingGenerationManager(rag, corpus)
        manifest = await manager.build_and_activate(force=force)
        return LegalIndexResult(
            action="indexed",
            doc_id=doc_id,
            chunks=len(chunks),
            generation_id=manifest.generation_id,
            manifest_path=manager.manifest_path,
        )

    await rag.index_chunks(chunks)
    if not await legal_corpus_is_indexed(rag, corpus):
        raise RuntimeError("legal corpus indexing completed without a persisted document")
    return LegalIndexResult(action="indexed", doc_id=doc_id, chunks=len(chunks))


async def adopt_legal_corpus_index(
    rag: RagPipeline,
    corpus: LegalCorpus,
    entries: list[tuple[Chunk, list[float]]],
    *,
    source_index_name: str,
    attested_model_revision: str,
) -> LegalIndexResult:
    """Promote fully validated legacy vectors into the pinned active namespace."""

    manager = EmbeddingGenerationManager(rag, corpus)
    manifest = await manager.adopt_and_activate(
        entries,
        source_index_name=source_index_name,
        attested_model_revision=attested_model_revision,
    )
    return LegalIndexResult(
        action="indexed",
        doc_id=corpus.as_parsed_document().doc_id,
        chunks=len(corpus.as_chunks()),
        generation_id=manifest.generation_id,
        manifest_path=manager.manifest_path,
    )
