"""End-to-end analysis use case: PDF path in, report out.

This is the single entry point the API (M5) and the UI (M6) call. It owns
nothing itself - ingestion, graph, and their dependencies are injected or
built by the factory at the composition root.
"""

from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.enrichment.datajud import DataJudClient
from app.ingestion.ocr import create_default_ocr_engine
from app.ingestion.service import DocumentIngestionService
from app.llm.factory import create_llm_client
from app.orchestration.graph import build_analysis_graph
from app.orchestration.state import AnalysisState
from app.rag.factory import create_rag_pipeline
from app.security import PromptInjectionDetector, TelemetryRedactor


def create_datajud_client(
    settings: Settings,
    telemetry_redactor: TelemetryRedactor | None = None,
) -> DataJudClient | None:
    """DataJud is optional: no key -> enrichment is skipped gracefully."""
    if not settings.datajud_api_key:
        return None
    redactor = telemetry_redactor or TelemetryRedactor(settings.telemetry_pseudonym_key)
    return DataJudClient(
        base_url=settings.datajud_base_url,
        api_key=settings.datajud_api_key,
        telemetry_redactor=redactor,
    )

logger = get_logger(__name__)


class LawsuitAnalysisService:
    """Analyze one lawsuit PDF end to end."""

    def __init__(self, ingestion: DocumentIngestionService, graph: object) -> None:
        self._ingestion = ingestion
        self._graph = graph

    async def analyze(self, pdf_path: Path) -> AnalysisState:
        document = await self._ingestion.ingest(pdf_path)
        result = await self._graph.ainvoke(AnalysisState(document=document))  # type: ignore[attr-defined]
        state = AnalysisState(**result)
        logger.info(
            "analysis_finished",
            doc_id=document.doc_id,
            errors=len(state.errors),
            has_report=state.report is not None,
        )
        return state


def create_analysis_service(settings: Settings) -> LawsuitAnalysisService:
    """Composition root for the full analysis pipeline."""
    telemetry_redactor = TelemetryRedactor(settings.telemetry_pseudonym_key)
    ingestion = DocumentIngestionService(
        ocr_engine=create_default_ocr_engine(),
        max_pages=settings.max_document_pages,
        telemetry_redactor=telemetry_redactor,
    )
    llm = create_llm_client(settings)
    rag = create_rag_pipeline(settings)
    datajud = create_datajud_client(settings, telemetry_redactor)
    return LawsuitAnalysisService(
        ingestion=ingestion,
        graph=build_analysis_graph(
            llm,
            rag,
            datajud,
            prompt_injection_detector=PromptInjectionDetector(
                llm,
                mode=settings.prompt_injection_scan_mode,
                strict_max_chars=settings.prompt_injection_strict_max_chars,
                strict_max_batches=settings.prompt_injection_strict_max_batches,
            ),
        ),
    )
