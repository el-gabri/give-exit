"""Copy an existing embedded Chroma legal index to PostgreSQL/pgvector.

The command never calls the embedding model. It copies the already-materialized
vectors and provenance payloads from the versioned Chroma collection, then the
normal runtime can switch to the Postgres backend.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from app.consumer.legal_corpus import get_default_legal_corpus
from app.core.config import Settings
from app.core.logging import configure_logging
from app.rag.factory import configured_embedding_identity, vector_store_index_name
from app.rag.vector_store import ChromaVectorStore, PostgresVectorStore
from app.schemas.rag import Chunk


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copia o índice legal Chroma existente para PostgreSQL/pgvector "
            "sem re-embedding."
        )
    )
    parser.add_argument(
        "--source-index",
        help="nome da coleção Chroma de origem; por padrão deriva-o do corpus e embedding atuais",
    )
    return parser


async def _run(*, source_index: str | None) -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    dsn = (settings.postgres_dsn or "").strip()
    if not dsn:
        raise ValueError("Defina LITIGATION_POSTGRES_DSN antes da migração")

    corpus = get_default_legal_corpus()
    corpus_version = f"{corpus.release_id}-{corpus.corpus_sha256[:12]}"
    index_name = source_index or vector_store_index_name(
        settings,
        corpus_version=corpus_version,
        embedding_model=configured_embedding_identity(settings),
    )
    source = ChromaVectorStore(settings.chroma_dir, collection_name=index_name)
    destination = PostgresVectorStore(dsn=dsn, index_name=index_name)
    entries = await source.export_entries()
    if not entries:
        print(f"A coleção Chroma de origem está vazia ou ausente: {index_name}", file=sys.stderr)
        return 2

    documents: dict[str, list[tuple[Chunk, list[float]]]] = defaultdict(list)
    for chunk, vector in entries:
        documents[chunk.doc_id].append((chunk, vector))
    for doc_id in sorted(documents):
        rows = documents[doc_id]
        await destination.replace_document(
            [chunk for chunk, _ in rows],
            [vector for _, vector in rows],
        )

    migrated_ids = await destination.list_document_ids()
    expected_ids = set(documents)
    if not expected_ids.issubset(migrated_ids):
        raise RuntimeError("a migração terminou sem todos os documentos no PostgreSQL")
    dimensions = len(entries[0][1])
    print(f"Índice migrado para PostgreSQL: {index_name}")
    print(f"Documentos: {len(documents)} · chunks: {len(entries)} · dimensões: {dimensions}")
    print("Os embeddings foram copiados do Chroma; o modelo não foi executado.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(source_index=args.source_index))
    except Exception as exc:
        print(f"Falha ao migrar o índice legal ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
