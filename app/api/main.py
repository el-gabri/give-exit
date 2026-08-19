"""FastAPI application factory.

The lifespan is the composition root of the running service: every
dependency is built once from Settings and attached to app.state - routes
never construct infrastructure.

Run locally:
    uvicorn app.api.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.consumer_routes import router as consumer_router
from app.api.jobs import AnalysisJobManager
from app.api.routes import router
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.service import ConsumerCaseService
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.ingestion.ocr import create_default_ocr_engine
from app.ingestion.service import DocumentIngestionService
from app.llm.factory import create_llm_client
from app.observability.store import RunStore
from app.orchestration.graph import build_analysis_graph
from app.rag.factory import create_embedding_client, create_rag_pipeline, create_reranker
from app.security import PromptInjectionDetector
from app.services.analysis import create_datajud_client


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        llm = create_llm_client(settings)
        # Business and Consumer use isolated indexes.  The legal collection is
        # tied to the exact corpus digest, so a snapshot/parser change cannot
        # silently reuse stale vectors or change the established Business rank.
        embedder = create_embedding_client(settings)
        reranker = create_reranker(settings)
        rag = create_rag_pipeline(settings, embedder=embedder, reranker=reranker)
        legal_corpus = get_default_legal_corpus()
        consumer_rag = create_rag_pipeline(
            settings,
            corpus_version=(f"{legal_corpus.release_id}-{legal_corpus.corpus_sha256[:12]}"),
            embedder=embedder,
            reranker=reranker,
        )
        ingestion = DocumentIngestionService(
            ocr_engine=create_default_ocr_engine(), max_pages=settings.max_document_pages
        )
        prompt_injection_detector = PromptInjectionDetector(
            llm,
            mode=settings.prompt_injection_scan_mode,
            strict_max_chars=settings.prompt_injection_strict_max_chars,
            strict_max_batches=settings.prompt_injection_strict_max_batches,
        )
        run_store = RunStore(settings.data_dir / "runs.jsonl")
        app.state.run_store = run_store
        app.state.job_manager = AnalysisJobManager(
            ingestion=ingestion,
            graph=build_analysis_graph(
                llm,
                rag,
                create_datajud_client(settings),
                prompt_injection_detector=prompt_injection_detector,
            ),
            run_store=run_store,
            uploads_dir=settings.uploads_dir,
            retain_uploads=settings.retain_uploads,
            rag=rag,
            retain_index=settings.retain_index,
        )
        app.state.consumer_service = ConsumerCaseService(
            ingestion=ingestion,
            detector=prompt_injection_detector,
            rag=consumer_rag,
            legal_corpus=legal_corpus,
        )
        app.state.consumer_uploads_dir = settings.uploads_dir / "consumer"
        # Job and case records are in-process, so at startup every document in
        # either index is an orphan from a previous run.
        await app.state.consumer_service.purge_orphaned_documents()
        if not settings.retain_index:
            for doc_id in sorted(await rag.list_document_ids()):
                await rag.delete_document(doc_id)
        yield

    app = FastAPI(
        title="AI Litigation Copilot API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,  # local Streamlit by default
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(consumer_router)
    return app


app = create_app()
