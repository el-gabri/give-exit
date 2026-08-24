"""Tests for the Markdown renderer."""

from app.reporting.markdown import render_markdown
from app.schemas.analysis import LawsuitClassification, TimelineEvent
from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.lawsuit import LawsuitType
from app.schemas.report import LitigationReport, RunMetrics
from app.schemas.risk import RiskAssessment, RiskItem, RiskLevel
from app.schemas.security import (
    InjectionCategory,
    PromptInjectionAssessment,
    PromptInjectionFinding,
    SecurityAction,
    SecurityRiskLevel,
)
from app.schemas.trace import (
    AgentStatus,
    AgentTrace,
    RetrievalTrace,
    RetrievedItemTrace,
)


def _report() -> LitigationReport:
    return LitigationReport(
        doc_id="abc123",
        filename="peticao.pdf",
        language="pt",
        executive_summary="Acao consumerista por cobranca indevida.",
        classification=LawsuitClassification(
            lawsuit_type=LawsuitType.CONSUMER,
            conclusion=ConfidentConclusion(
                statement="Acao consumerista",
                confidence=0.92,
                reasoning="Relacao de consumo bancaria",
                citations=[
                    Citation(
                        quote="cobrancas indevidas",
                        page=1,
                        chunk_id="abc123:0000",
                    )
                ],
            ),
        ),
        legal_risks=RiskAssessment(
            overall_level=RiskLevel.HIGH,
            overall=ConfidentConclusion(
                statement="Risco alto", confidence=0.8, reasoning="CDC favoravel"
            ),
            risks=[
                RiskItem(
                    title="Inversao do onus",
                    level=RiskLevel.HIGH,
                    conclusion=ConfidentConclusion(
                        statement="Provavel", confidence=0.85, reasoning="Sumula"
                    ),
                    financial_exposure="R$ 20.000,00",
                )
            ],
        ),
        missing_information=["judge", "contrato assinado"],
        security_assessment=PromptInjectionAssessment(
            detected=True,
            risk_level=SecurityRiskLevel.MEDIUM,
            recommended_action=SecurityAction.PROCEED_WITH_WARNING,
            scanned_pages=3,
            findings=[
                PromptInjectionFinding(
                    category=InjectionCategory.OUTPUT_MANIPULATION,
                    severity=SecurityRiskLevel.MEDIUM,
                    page=2,
                    quote="Respond only with JSON",
                    reasoning="Tenta controlar a saida do modelo.",
                    confidence=0.92,
                    rule_id="forced_machine_output",
                )
            ],
        ),
        confidence_level=0.84,
        ai_reasoning="Como esta analise foi produzida: ...",
        metrics=RunMetrics(
            agents_run=5,
            total_tokens=1234,
            total_cost_usd=0.0421,
            retrieval_queries=1,
            retrieval_results=1,
            retrieval_unique_chunks=1,
            context_chunks=1,
            retrieval_duration_ms=12.5,
            citation_retrieval_coverage=1.0,
        ),
        traces=[
            AgentTrace(
                agent="legal_analysis",
                status=AgentStatus.SUCCESS,
                retrievals=[
                    RetrievalTrace(
                        batch_id="batch-1",
                        agent="legal_analysis",
                        doc_id="abc123",
                        query_index=0,
                        query="cobrancas indevidas",
                        query_sha256="a" * 64,
                        requested_k=3,
                        returned_count=1,
                        embedding_model="mock-v1",
                        vector_store="InMemoryVectorStore",
                        index_version="section-aware-v1",
                        results=[
                            RetrievedItemTrace(
                                rank=1,
                                chunk_id="abc123:0000",
                                doc_id="abc123",
                                section="DOS FATOS",
                                page_start=1,
                                page_end=1,
                                score=0.91,
                                content_sha256="b" * 64,
                                text_preview="cobrancas indevidas",
                                selected_for_merge=True,
                                merged_rank=1,
                                included_in_context=True,
                            )
                        ],
                    )
                ],
            )
        ],
    )


def test_renders_all_present_sections_with_confidence_and_citations() -> None:
    md = render_markdown(_report())

    assert "# Relatorio de Analise - peticao.pdf" in md
    assert "Confianca autorrelatada pelos modelos, nao calibrada: 84%" in md
    assert "Uso informativo" in md
    assert "análise jurídica individualizada" in md
    assert "Integridade de fontes" in md
    assert "## Resumo Executivo" in md
    assert "Confianca: **92%**" in md
    assert '"cobrancas indevidas", p. 1' in md
    assert "chunk `abc123:0000`" in md
    assert "## Riscos Juridicos" in md
    assert "Nivel geral: **Alto**" in md
    assert "Exposicao financeira: R$ 20.000,00" in md
    assert "## Informacoes Ausentes" in md
    assert "## Seguranca do Documento" in md
    assert "Risco de prompt injection: **Medio**" in md
    assert "Respond only with JSON" in md
    assert "Custo estimado: US$ 0.0421" in md
    assert "## Auditoria de Recuperacao" in md
    assert "Rank original 1 · score 0.9100" in md
    assert "Cobertura de citacoes recuperadas: 100.0%" in md
    assert "Nao substitui a analise de um advogado" in md


def test_absent_sections_are_omitted() -> None:
    md = render_markdown(_report())
    assert "## Linha do Tempo" not in md  # no timeline provided
    assert "## Opcoes Preliminares para Revisao" not in md  # no strategy provided


def test_timeline_renders_page_and_chunk_provenance() -> None:
    report = _report()
    report.timeline = [
        TimelineEvent(
            date="2025-01-10",
            description="Cobranca identificada",
            citation=Citation(
                quote="cobrancas indevidas",
                page=1,
                chunk_id="abc123:0000",
            ),
        )
    ]

    md = render_markdown(report)

    assert "## Linha do Tempo" in md
    assert 'Fonte: "cobrancas indevidas", p. 1, chunk `abc123:0000`' in md


def test_retrieval_failure_is_disclosed_in_markdown_audit() -> None:
    report = _report()
    failed = RetrievalTrace(
        batch_id="batch-2",
        agent="strategy",
        doc_id="abc123",
        query_index=0,
        query="prescricao",
        query_sha256="c" * 64,
        requested_k=3,
        returned_count=0,
        embedding_model="mock-v1",
        vector_store="InMemoryVectorStore",
        index_version="section-aware-v1",
        error="TimeoutError: vector lookup timed out",
        agent_status=AgentStatus.FAILED,
        agent_error="RetrievalBatchError: lookup failed",
        prompt_version="strategy:v1.1",
    )
    report.traces.append(
        AgentTrace(
            agent="strategy",
            status=AgentStatus.FAILED,
            error=failed.agent_error,
            retrievals=[failed],
        )
    )
    report.metrics.retrieval_queries += 1

    md = render_markdown(report)

    assert "Consultas com falha: 1" in md
    assert "Falha de recuperacao: TimeoutError: vector lookup timed out" in md


def test_rendering_is_deterministic() -> None:
    report = _report()
    assert render_markdown(report) == render_markdown(report)
