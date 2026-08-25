"""CLI for materializing the versioned Consumer legal corpus before serving traffic."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.consumer.legal_index import legal_corpus_is_indexed, preindex_legal_corpus
from app.consumer.runtime import create_consumer_rag
from app.core.config import Settings
from app.core.logging import configure_logging


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
    return parser


async def _run(*, force: bool, check: bool) -> int:
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
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(force=args.force, check=args.check))
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
