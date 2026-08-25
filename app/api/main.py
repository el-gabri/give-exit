"""Consumer-only FastAPI application factory.

The lifespan is the composition root of the running service: every
dependency is built once from Settings and attached to app.state - routes
never construct infrastructure.

Run locally:
    uvicorn app.api.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.consumer_routes import router as consumer_router
from app.api.routes import router as system_router
from app.api.security import ApiKeyGuard, SlidingWindowRateLimiter
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.service import ConsumerCaseService
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.ingestion.ocr import create_default_ocr_engine
from app.ingestion.service import DocumentIngestionService
from app.llm.factory import create_llm_client
from app.rag.factory import create_embedding_client, create_rag_pipeline, create_reranker
from app.security import PromptInjectionDetector, TelemetryRedactor


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        llm = create_llm_client(settings)
        # The collection identity includes the exact legal-corpus digest and
        # embedding identity, so incompatible releases never share vectors.
        embedder = create_embedding_client(settings)
        reranker = create_reranker(settings)
        legal_corpus = get_default_legal_corpus()
        consumer_rag = create_rag_pipeline(
            settings,
            corpus_version=(f"{legal_corpus.release_id}-{legal_corpus.corpus_sha256[:12]}"),
            embedder=embedder,
            reranker=reranker,
        )
        telemetry_redactor = TelemetryRedactor(settings.telemetry_pseudonym_key)
        ingestion = DocumentIngestionService(
            ocr_engine=create_default_ocr_engine(),
            max_pages=settings.max_document_pages,
            telemetry_redactor=telemetry_redactor,
        )
        prompt_injection_detector = PromptInjectionDetector(
            llm,
            mode=settings.prompt_injection_scan_mode,
            strict_max_chars=settings.prompt_injection_strict_max_chars,
            strict_max_batches=settings.prompt_injection_strict_max_batches,
        )
        app.state.consumer_service = ConsumerCaseService(
            ingestion=ingestion,
            detector=prompt_injection_detector,
            rag=consumer_rag,
            legal_corpus=legal_corpus,
        )
        app.state.consumer_uploads_dir = settings.uploads_dir / "consumer"
        app.state.upload_rate_limiter = SlidingWindowRateLimiter(
            settings.upload_rate_limit_per_minute
        )
        # Case records are in-process, so vectors outside the canonical legal
        # corpus are orphaned whenever the service starts.
        await app.state.consumer_service.purge_orphaned_documents()
        yield

    app = FastAPI(
        title="Give Exit Consumer API",
        description=(
            "Assistente informativo para consumidores criarem rascunhos auditáveis "
            "de notificações extrajudiciais com evidências e fontes legais rastreáveis."
        ),
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(ApiKeyGuard(settings.api_auth_key))],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,  # local Streamlit by default
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(system_router)
    app.include_router(consumer_router)
    return app


app = create_app()
