"""Tests for deterministic report composition and retrieval attribution."""

from datetime import datetime, timedelta, timezone

from app.orchestration.state import AnalysisState
from app.schemas.analysis import LegalAnalysis
from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.schemas.rag import Chunk
from app.schemas.report import EvidenceQualityStatus
from app.schemas.trace import (
    AgentStatus,
    AgentTrace,
    RetrievalTrace,
    RetrievedItemTrace,
)
from app.services.composer import _build_metrics, compose_report


def _state_with_retrieval(*, retrieval_agent: str) -> AnalysisState:
    document = ParsedDocument(
        filename="peticao.pdf",
        pages=[DocumentPage(number=1, text="A autora relata cobrancas indevidas.")],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    chunk = Chunk(
        chunk_id=f"{document.doc_id}:0000",
        doc_id=document.doc_id,
        text=document.pages[0].text,
        page_start=1,
        page_end=1,
    )
    retrieval = RetrievalTrace(
        batch_id="batch-1",
        agent=retrieval_agent,
        doc_id=document.doc_id,
        query_index=0,
        query="cobrancas",
        query_sha256="a" * 64,
        requested_k=1,
        returned_count=1,
        embedding_model="mock",
        vector_store="memory",
        index_version="test",
        agent_status=AgentStatus.SUCCESS,
        prompt_version=f"{retrieval_agent}:v1",
        results=[
            RetrievedItemTrace(
                rank=1,
                chunk_id=chunk.chunk_id,
                doc_id=document.doc_id,
                page_start=1,
                page_end=1,
                score=0.9,
                content_sha256="b" * 64,
                selected_for_merge=True,
                merged_rank=1,
                included_in_context=True,
            )
        ],
    )
    return AnalysisState(
        document=document,
        chunks=[chunk],
        legal_analysis=LegalAnalysis(
            executive_summary="Resumo.",
            evidence_found=[
                ConfidentConclusion(
                    statement="Ha cobranca indevida.",
                    confidence=0.8,
                    reasoning="Consta do documento.",
                    citations=[
                        Citation(
                            quote="cobrancas indevidas",
                            page=1,
                            chunk_id=chunk.chunk_id,
                        )
                    ],
                )
            ],
        ),
        traces=[
            AgentTrace(
                agent=retrieval_agent,
                status=AgentStatus.SUCCESS,
                retrievals=[retrieval],
            )
        ],
    )


def test_citation_retrieval_coverage_requires_the_producing_agent_context() -> None:
    report = compose_report(_state_with_retrieval(retrieval_agent="risk_assessment"))

    assert report.metrics.citation_retrieval_coverage == 0.0
    assert report.evidence_quality.status is EvidenceQualityStatus.HUMAN_REVIEW_REQUIRED
    assert any("rastreabilidade" in warning for warning in report.warnings)


def test_citation_retrieval_coverage_accepts_the_producing_agent_context() -> None:
    report = compose_report(_state_with_retrieval(retrieval_agent="legal_analysis"))

    assert report.metrics.citation_retrieval_coverage == 1.0
    assert report.evidence_quality.status is EvidenceQualityStatus.PASSED
    assert report.evidence_quality.semantic_entailment_checked is False
    assert report.evidence_quality.legal_correctness_checked is False
    assert not any("rastreabilidade" in warning for warning in report.warnings)


def test_total_duration_is_wall_clock_span_for_overlapping_agents() -> None:
    started_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    metrics = _build_metrics(
        [
            AgentTrace(
                agent="legal_analysis",
                status=AgentStatus.SUCCESS,
                started_at=started_at,
                duration_ms=300.0,
            ),
            AgentTrace(
                agent="risk_assessment",
                status=AgentStatus.SUCCESS,
                started_at=started_at + timedelta(milliseconds=100),
                duration_ms=400.0,
            ),
        ]
    )

    assert metrics.total_duration_ms == 500.0
