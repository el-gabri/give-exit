"""CLI for materializing the versioned Consumer legal corpus before serving traffic."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.consumer.legal_index import (
    adopt_legal_corpus_index,
    legal_corpus_is_indexed,
    preindex_legal_corpus,
)
from app.consumer.runtime import create_consumer_rag
from app.core.config import Settings
from app.core.logging import configure_logging
from app.rag.factory import create_vector_store_for_index
from app.rag.vector_store import DocumentExportingVectorStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Pré-indexa CDC e Constituição no índice versionado do modelo de embedding "
            "configurado."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recalcula todos os embeddings mesmo quando o corpus já está presente",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="apenas verifica a prontidão; retorna código 2 quando o índice está ausente",
    )
    parser.add_argument(
        "--adopt-source-index",
        help=(
            "copia vetores de um namespace legado validado para o namespace atual, "
            "sem executar o modelo"
        ),
    )
    parser.add_argument(
        "--attest-source-revision",
        help=(
            "revisão exata que o operador verificou ter produzido o índice legado; "
            "obrigatória com --adopt-source-index"
        ),
    )
    return parser


async def _run(
    *,
    force: bool,
    check: bool,
    adopt_source_index: str | None,
    attested_source_revision: str | None,
) -> int:
    if adopt_source_index and (force or check):
        raise ValueError("--adopt-source-index não pode ser combinado com --force ou --check")
    if bool(adopt_source_index) != bool(attested_source_revision):
        raise ValueError(
            "--adopt-source-index e --attest-source-revision devem ser informados juntos"
        )
    settings = Settings(embedding_show_progress_bar=not check)
    configure_logging(settings.log_level)
    print("Preparando a configuração do índice legal...", flush=True)
    corpus, rag = create_consumer_rag(settings)
    configuration = rag.retrieval_configuration(requested_k=1)
    model = configuration["embedding_model"]
    index_name = configuration["index_name"]

    if check:
        ready = await legal_corpus_is_indexed(rag, corpus)
        state = "pronto" if ready else "ausente"
        print(f"Índice legal {state}: {index_name}")
        print(f"Modelo: {model}")
        print(f"Corpus: {corpus.release_id} ({corpus.corpus_sha256})")
        return 0 if ready else 2

    if adopt_source_index is not None and attested_source_revision is not None:
        source = create_vector_store_for_index(settings, index_name=adopt_source_index)
        if not isinstance(source, DocumentExportingVectorStore):
            raise TypeError("o backend configurado não permite exportar o índice legado")
        entries = await source.export_document(corpus.as_parsed_document().doc_id)
        if not entries:
            raise ValueError(f"o índice legado está vazio: {adopt_source_index}")
        result = await adopt_legal_corpus_index(
            rag,
            corpus,
            entries,
            source_index_name=adopt_source_index,
            attested_model_revision=attested_source_revision,
        )
        print(f"Índice legado promovido sem re-embedding: {index_name}")
        print(f"Documento: {result.doc_id} · chunks: {result.chunks}")
        print(f"Geração: {result.generation_id} · manifesto: {result.manifest_path}")
        return 0

    print(
        f"Corpus: {corpus.release_id} · {len(corpus.as_chunks())} chunks · modelo: {model}",
        flush=True,
    )
    print(
        "A primeira execução com um modelo local grande pode levar dezenas de "
        "minutos ou horas em CPU. "
        "Interromper o processo preserva qualquer índice anterior.",
        flush=True,
    )
    started = time.perf_counter()
    result = await preindex_legal_corpus(rag, corpus, force=force)
    elapsed = time.perf_counter() - started
    verb = "reutilizado" if result.action == "reused" else "indexado"
    print(f"Índice legal {verb} com sucesso em {elapsed:.1f}s: {index_name}")
    print(f"Documento: {result.doc_id} · chunks: {result.chunks}")
    if result.generation_id is not None:
        print(f"Geração: {result.generation_id} · manifesto: {result.manifest_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(
            _run(
                force=args.force,
                check=args.check,
                adopt_source_index=args.adopt_source_index,
                attested_source_revision=args.attest_source_revision,
            )
        )
    except KeyboardInterrupt:
        print("Pré-indexação interrompida.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Falha ao preparar o índice legal ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
