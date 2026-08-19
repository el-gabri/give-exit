"""Deterministic report composer (see ADR 0007).

The composer is intentionally NOT an LLM agent. Agents produce typed,
cited conclusions; this module assembles them into the final report with
plain code. The last mile of a legal product must not be able to
hallucinate: aggregate confidence is computed, the AI-reasoning section is
built from real traces, and missing information comes from the extraction
schema itself.
"""

from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.report import LitigationReport, RunMetrics
from app.schemas.security import PromptInjectionAssessment, SecurityRiskLevel
from app.schemas.trace import AgentStatus, AgentTrace
from app.services.citations import validate_report_citations


def compose_report(state: object) -> LitigationReport:
    """Assemble the LitigationReport from a completed AnalysisState."""
    document = state.document  # type: ignore[attr-defined]
    classification = state.classification  # type: ignore[attr-defined]
    extraction = state.extraction  # type: ignore[attr-defined]
    analysis = state.legal_analysis  # type: ignore[attr-defined]
    risk = state.risk  # type: ignore[attr-defined]
    strategy = state.strategy  # type: ignore[attr-defined]
    security_assessment = getattr(state, "security_assessment", None)
    traces: list[AgentTrace] = state.traces  # type: ignore[attr-defined]

    conclusions = _collect_conclusions(classification, analysis, risk, strategy)
    missing = _missing_information(extraction, strategy)

    report = LitigationReport(
        doc_id=document.doc_id,
        filename=document.filename,
        language=document.language,
        executive_summary=analysis.executive_summary if analysis else "",
        classification=classification,
        parties=extraction,
        timeline=analysis.timeline if analysis else [],
        main_claims=analysis.claims if analysis else [],
        evidence_found=analysis.evidence_found if analysis else [],
        missing_information=missing,
        legal_risks=risk,
        suggested_strategy=strategy,
        possible_settlement=strategy.settlement if strategy else None,
        security_assessment=security_assessment,
        datajud=getattr(state, "enrichment", None),
        confidence_level=_aggregate_confidence(conclusions),
        ai_reasoning=_build_ai_reasoning(state, traces),
        warnings=[
            *document.warnings,
            *_security_warnings(security_assessment),
            *_review_warnings(state),
            *_error_warnings(state),
        ],
        metrics=_build_metrics(traces),
        traces=traces,
    )
    citation_result = validate_report_citations(
        report, document, state.chunks  # type: ignore[attr-defined]
    )
    included_by_agent: dict[str, set[str]] = {}
    for trace in traces:
        for retrieval in trace.retrievals:
            included_by_agent.setdefault(retrieval.agent, set()).update(
                item.chunk_id
                for item in retrieval.results
                if item.included_in_context
            )
    chunk_citations = [
        (agent, citation)
        for agent, citation in _citations_by_agent(report)
        if citation.chunk_id is not None
    ]
    if chunk_citations:
        retrieved_citations = sum(
            citation.chunk_id in included_by_agent.get(agent, set())
            for agent, citation in chunk_citations
        )
        report.metrics.citation_retrieval_coverage = round(
            retrieved_citations / len(chunk_citations), 3
        )
        if retrieved_citations < len(chunk_citations):
            report.warnings.append(
                "Revisao de rastreabilidade: "
                f"{len(chunk_citations) - retrieved_citations} citacao(oes) apontam "
                "para trechos sem registro de inclusao no contexto dos agentes."
            )
    if citation_result.rejected_citations:
        report.warnings.append(
            f"{citation_result.rejected_citations} citacao(oes) com localizacao de "
            "fonte invalida foram removidas do relatorio."
        )
    if citation_result.conclusions_without_verified_citation:
        report.warnings.append(
            "Revisao humana obrigatoria: "
            f"{citation_result.conclusions_without_verified_citation}/"
            f"{citation_result.total_conclusions} conclusao(oes) nao possuem citacao verificada."
        )
    return report


def _citations_by_agent(report: LitigationReport) -> list[tuple[str, Citation]]:
    """Associate every report citation with the agent that produced it."""
    attributed: list[tuple[str, Citation]] = []

    def add(agent: str, conclusions: list[ConfidentConclusion]) -> None:
        attributed.extend(
            (agent, citation)
            for conclusion in conclusions
            for citation in conclusion.citations
        )

    if report.classification:
        add("classifier", [report.classification.conclusion])
    attributed.extend(
        ("legal_analysis", event.citation)
        for event in report.timeline
        if event.citation is not None
    )
    add("legal_analysis", [claim.assessment for claim in report.main_claims])
    add("legal_analysis", report.evidence_found)
    if report.legal_risks:
        add(
            "risk_assessment",
            [
                report.legal_risks.overall,
                *(item.conclusion for item in report.legal_risks.risks),
            ],
        )
    if report.suggested_strategy:
        add(
            "strategy",
            [
                report.suggested_strategy.overall_approach,
                report.suggested_strategy.settlement,
                *(
                    defense.assessment
                    for defense in report.suggested_strategy.defenses
                ),
            ],
        )
    return attributed


def _collect_conclusions(
    classification: object, analysis: object, risk: object, strategy: object
) -> list[ConfidentConclusion]:
    conclusions: list[ConfidentConclusion] = []
    if classification is not None:
        conclusions.append(classification.conclusion)  # type: ignore[attr-defined]
    if analysis is not None:
        conclusions.extend(c.assessment for c in analysis.claims)  # type: ignore[attr-defined]
        conclusions.extend(analysis.evidence_found)  # type: ignore[attr-defined]
    if risk is not None:
        conclusions.append(risk.overall)  # type: ignore[attr-defined]
        conclusions.extend(r.conclusion for r in risk.risks)  # type: ignore[attr-defined]
    if strategy is not None:
        conclusions.append(strategy.overall_approach)  # type: ignore[attr-defined]
        conclusions.append(strategy.settlement)  # type: ignore[attr-defined]
        conclusions.extend(d.assessment for d in strategy.defenses)  # type: ignore[attr-defined]
    return conclusions


def _aggregate_confidence(conclusions: list[ConfidentConclusion]) -> float:
    """Unweighted mean of all conclusion confidences.

    Deliberately simple and transparent: easy to explain, easy to audit.
    A weighted scheme would imply precision we do not have.
    """
    if not conclusions:
        return 0.0
    return round(sum(c.confidence for c in conclusions) / len(conclusions), 3)


def _missing_information(extraction: object, strategy: object) -> list[str]:
    missing: list[str] = []
    if extraction is not None:
        missing.extend(extraction.missing_fields())  # type: ignore[attr-defined]
    if strategy is not None:
        missing.extend(strategy.missing_information)  # type: ignore[attr-defined]
    # dedupe, preserve order
    return list(dict.fromkeys(missing))


def _build_metrics(traces: list[AgentTrace]) -> RunMetrics:
    metered = [t.llm_meta for t in traces if t.llm_meta is not None]
    retrievals = [retrieval for trace in traces for retrieval in trace.retrievals]
    result_chunk_ids = {
        item.chunk_id for retrieval in retrievals for item in retrieval.results
    }
    context_chunk_ids = {
        item.chunk_id
        for retrieval in retrievals
        for item in retrieval.results
        if item.included_in_context
    }
    batch_durations = {
        retrieval.batch_id: retrieval.batch_duration_ms for retrieval in retrievals
    }
    return RunMetrics(
        total_duration_ms=round(sum(t.duration_ms for t in traces), 1),
        total_tokens=sum(m.usage.total_tokens for m in metered),
        total_cost_usd=round(sum(m.cost_usd or 0.0 for m in metered), 6),
        models_used=sorted({m.model for m in metered}),
        prompt_versions=sorted(
            {m.prompt_version for m in metered if m.prompt_version}
        ),
        agents_run=len(traces),
        retrieval_queries=len(retrievals),
        retrieval_results=sum(retrieval.returned_count for retrieval in retrievals),
        retrieval_unique_chunks=len(result_chunk_ids),
        context_chunks=len(context_chunk_ids),
        retrieval_duration_ms=round(sum(batch_durations.values()), 1),
    )


def _review_warnings(state: object) -> list[str]:
    review = getattr(state, "human_review", None)
    if review is None or not review.approved:
        return []
    return [
        "Analise automatizada retomada apos aprovacao em revisao humana por "
        f"'{review.reviewer}'. Os trechos sinalizados permanecem mascarados."
    ]


def _error_warnings(state: object) -> list[str]:
    errors: list[str] = getattr(state, "errors", [])
    if not errors:
        return []
    return [
        "Este e um relatorio parcial. Etapas do pipeline que falharam: "
        + "; ".join(errors)
    ]


def _security_warnings(
    assessment: PromptInjectionAssessment | None,
) -> list[str]:
    if assessment is None:
        return []
    warnings = list(assessment.warnings)
    if not assessment.detected:
        return warnings
    count = len(assessment.findings)
    if assessment.risk_level is SecurityRiskLevel.MEDIUM:
        warnings.append(
            f"Seguranca: {count} trecho(s) suspeito(s) foram removidos do contexto "
            "dos agentes; revise os achados."
        )
    elif assessment.risk_level is SecurityRiskLevel.HIGH:
        warnings.append(
            f"Seguranca: {count} tentativa(s) de prompt injection de alto risco "
            "foram detectadas. A analise juridica automatizada foi interrompida "
            "para revisao humana."
        )
    elif assessment.risk_level is SecurityRiskLevel.CRITICAL:
        warnings.append(
            f"Seguranca: {count} tentativa(s) critica(s) de prompt injection foram "
            "detectadas. A analise juridica automatizada foi bloqueada."
        )
    else:
        warnings.append(
            f"Seguranca: {count} trecho(s) potencialmente suspeito(s) foram sinalizados."
        )
    return warnings


def _build_ai_reasoning(state: object, traces: list[AgentTrace]) -> str:
    """Human-readable account of HOW the analysis was produced."""
    document = state.document  # type: ignore[attr-defined]
    chunks = state.chunks  # type: ignore[attr-defined]
    security_assessment = getattr(state, "security_assessment", None)
    lines = [
        "Como esta analise foi produzida:",
        f"1. O documento '{document.filename}' ({document.page_count} paginas, "
        f"idioma '{document.language}', extracao "
        f"{document.extraction_method.value}) foi recebido para analise.",
    ]
    next_step = 2
    if security_assessment is not None:
        lines.append(
            f"{next_step}. A varredura de prompt injection examinou "
            f"{security_assessment.scanned_pages} pagina(s): risco "
            f"'{security_assessment.risk_level.value}', acao "
            f"'{security_assessment.recommended_action.value}'."
        )
        next_step += 1
    if chunks:
        lines.extend(
            [
                f"{next_step}. O documento foi dividido em {len(chunks)} trechos "
                "indexados por secao.",
                f"{next_step + 1}. Agentes especializados analisaram o documento, "
                "cada um recuperando apenas os trechos relevantes para sua tarefa "
                "(RAG), com os achados de seguranca removidos do contexto.",
            ]
        )
        next_step += 2
        retrievals = [
            retrieval for trace in traces for retrieval in trace.retrievals
        ]
        if retrievals:
            included = {
                item.chunk_id
                for retrieval in retrievals
                for item in retrieval.results
                if item.included_in_context
            }
            lines.append(
                f"{next_step}. A auditoria registrou {len(retrievals)} consultas, "
                f"{sum(item.returned_count for item in retrievals)} resultados "
                f"ranqueados e {len(included)} trechos efetivamente enviados aos modelos."
            )
            next_step += 1
    elif security_assessment is not None and not (
        security_assessment.recommended_action.allows_automated_analysis
    ):
        lines.append(
            f"{next_step}. A analise juridica automatizada foi interrompida antes "
            "da indexacao; o documento original foi preservado para revisao humana."
        )
        next_step += 1
    for i, trace in enumerate(traces, start=next_step):
        if trace.llm_meta is None:
            lines.append(
                f"{i}. Agente '{trace.agent}': {trace.status.value}"
                + (f" ({trace.error})" if trace.error else "")
            )
        else:
            lines.append(
                f"{i}. Agente '{trace.agent}' "
                f"(prompt {trace.llm_meta.prompt_version}, modelo "
                f"{trace.llm_meta.model}): {trace.status.value} em "
                f"{trace.duration_ms:.0f}ms, "
                f"{trace.llm_meta.usage.total_tokens} tokens."
            )
    failed = [t for t in traces if t.status is AgentStatus.FAILED]
    lines.append(
        "Cada conclusao inclui nivel de confianca, justificativa e citacoes "
        "do documento original. Conclusoes sem citacao devem ser verificadas "
        "com atencao redobrada."
    )
    if failed:
        lines.append(
            f"ATENCAO: {len(failed)} agente(s) falharam nesta execucao; "
            "o relatorio pode estar incompleto."
        )
    return "\n".join(lines)
