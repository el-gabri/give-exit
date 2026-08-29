"""Consumer-only FastAPI application factory.

The lifespan is the composition root of the running service: every
dependency is built once from Settings and attached to app.state - routes
never construct infrastructure.

Run locally:
    uvicorn app.api.main:app --reload
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.consumer_routes import router as consumer_router
from app.api.routes import router as system_router
from app.api.security import (
    MULTIPART_ENVELOPE_MARGIN_BYTES,
    ApiKeyGuard,
    BodySizeLimitMiddleware,
    RateLimiterRegistry,
    SlidingWindowRateLimiter,
)
from app.consumer.composer import create_notice_composer
from app.consumer.runtime import create_consumer_rag
from app.consumer.service import ConsumerCaseService
from app.consumer.store import ConsumerCaseStore
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.ingestion.ocr import create_default_ocr_engine
from app.ingestion.service import DocumentIngestionService
from app.llm.factory import create_llm_client
from app.security import PromptInjectionDetector, TelemetryRedactor

logger = get_logger(__name__)


async def _expire_idle_cases_forever(
    service: ConsumerCaseService, *, interval_seconds: float
) -> None:
    """Reclaim idle cases and their evidence vectors while the app runs.

    Without this, a case abandoned mid-flow keeps its extracted text in memory
    and its vectors in the store until the process restarts.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await service.purge_expired_cases()
        except Exception:  # pragma: no cover - a sweep failure must not kill the loop
            logger.warning("consumer_case_sweep_failed", exc_info=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        llm = create_llm_client(settings)
        # The collection identity includes the exact legal-corpus digest and
        # embedding identity, so incompatible releases never share vectors.
        legal_corpus, consumer_rag = create_consumer_rag(settings)
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
            notice_composer=create_notice_composer(settings),
            store=ConsumerCaseStore(
                max_active_cases=settings.max_active_cases,
                idle_ttl_seconds=settings.case_idle_ttl_seconds,
                max_messages_per_case=settings.max_messages_per_case,
                max_idempotency_keys_per_case=settings.max_idempotency_keys_per_case,
            ),
            max_documents_per_case=settings.max_documents_per_case,
            purge_orphaned_evidence=settings.purge_orphaned_evidence_on_startup,
        )
        app.state.consumer_uploads_dir = settings.uploads_dir / "consumer"
        app.state.max_upload_bytes = settings.max_upload_bytes
        app.state.trusted_proxy_hops = settings.trusted_proxy_hops
        app.state.rate_limiters = RateLimiterRegistry(
            {
                "cases": SlidingWindowRateLimiter(settings.case_rate_limit_per_minute),
                "messages": SlidingWindowRateLimiter(settings.message_rate_limit_per_minute),
                "uploads": SlidingWindowRateLimiter(settings.upload_rate_limit_per_minute),
                "notice": SlidingWindowRateLimiter(settings.notice_rate_limit_per_minute),
            }
        )
        # Case records are in-process, so vectors outside the canonical legal
        # corpus are orphaned whenever the service starts. Only safe when this
        # process owns the vector-store namespace; see the setting's docstring.
        await app.state.consumer_service.purge_orphaned_documents()
        await app.state.consumer_service.legal_corpus_ready()
        sweeper = asyncio.create_task(
            _expire_idle_cases_forever(
                app.state.consumer_service,
                interval_seconds=settings.case_sweep_interval_seconds,
            )
        )
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

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
    # Outermost guard: a route-level size check runs only after the server has
    # already buffered the whole multipart body to disk.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_ENVELOPE_MARGIN_BYTES,
    )
    app.include_router(system_router)
    app.include_router(consumer_router)
    return app


app = create_app()
