"""Graph node for DataJud enrichment.

Enrichment NEVER fails the pipeline: no case number, no API key, network
error - all produce a DataJudEnrichment explaining why, plus a trace for
observability. Official-record validation is a bonus, not a dependency.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from app.enrichment.datajud import DataJudClient
from app.orchestration.state import AnalysisState
from app.schemas.enrichment import DataJudEnrichment
from app.schemas.trace import AgentStatus, AgentTrace

logger = get_logger(__name__)


def make_enrich_node(
    datajud: DataJudClient | None,
) -> Callable[[AnalysisState], Awaitable[dict[str, Any]]]:
    # LangGraph reads the first parameter's annotation to decide whether a node
    # receives the state model or a raw dict, so this must name the real state
    # type - an abstract stand-in silently downgrades it to a dict.
    async def enrich_node(state: AnalysisState) -> dict[str, Any]:
        start = time.perf_counter()
        extraction = state.extraction
        case_number = extraction.case_number if extraction else None

        if datajud is None:
            enrichment = DataJudEnrichment(
                attempted=False,
                notes=["DataJud desabilitado (LITIGATION_DATAJUD_API_KEY ausente)."],
            )
        elif not case_number:
            enrichment = DataJudEnrichment(
                attempted=False,
                notes=["Numero de processo nao encontrado no documento."],
            )
        else:
            try:
                alias, info = await datajud.lookup(case_number)
                if alias is None:
                    enrichment = DataJudEnrichment(
                        attempted=True,
                        notes=[f"Numero CNJ nao reconhecido: {case_number}"],
                    )
                elif info is None:
                    enrichment = DataJudEnrichment(
                        attempted=True,
                        tribunal_alias=alias,
                        notes=["Processo nao localizado no DataJud."],
                    )
                else:
                    enrichment = DataJudEnrichment(
                        attempted=True,
                        found=True,
                        tribunal_alias=alias,
                        info=info,
                        notes=["Dados validados contra a base oficial DataJud/CNJ."],
                    )
            except Exception as exc:
                logger.warning("datajud_lookup_failed", error=str(exc))
                enrichment = DataJudEnrichment(
                    attempted=True,
                    notes=[f"Falha na consulta ao DataJud: {type(exc).__name__}"],
                )

        trace = AgentTrace(
            agent="datajud_enrichment",
            status=AgentStatus.SUCCESS,
            duration_ms=(time.perf_counter() - start) * 1000,
        )
        return {"enrichment": enrichment, "traces": [trace]}

    return enrich_node
