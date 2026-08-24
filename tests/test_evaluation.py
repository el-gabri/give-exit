"""Tests for metrics, golden loading and the evaluation runner."""

import json
from pathlib import Path

import pytest

from app.evaluation import metrics
from app.evaluation.golden import load_case, load_dataset
from app.evaluation.runner import EvaluationRunner
from app.llm.mock_client import MockLLMClient
from app.orchestration.graph import build_analysis_graph
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.analysis import LawsuitClassification
from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.lawsuit import (
    LawsuitExtraction,
    LawsuitType,
    MonetaryValue,
    Party,
    PartyRole,
)
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.report import LitigationReport

DOC_TEXT = (
    "A autora e correntista do banco reu. Passou a identificar cobrancas "
    "mensais indevidas de tarifa, no valor de R$ 89,90."
)


def _report(quotes: list[str]) -> LitigationReport:
    return LitigationReport(
        doc_id="d1",
        filename="x.pdf",
        language="pt",
        executive_summary="resumo",
        classification=LawsuitClassification(
            lawsuit_type=LawsuitType.CONSUMER,
            conclusion=ConfidentConclusion(
                statement="consumerista",
                confidence=0.9,
                reasoning="r",
                citations=[
                    Citation(chunk_id=f"evaluation:{index}", quote=quote)
                    for index, quote in enumerate(quotes)
                ],
            ),
        ),
    )


def test_citation_verification_normalizes_accents_and_case() -> None:
    assert metrics.citation_supported("COBRANCAS mensais INDEVIDAS", DOC_TEXT)
    assert metrics.citation_supported("cobranças mensais indevidas", DOC_TEXT)
    assert not metrics.citation_supported("clausula de arbitragem", DOC_TEXT)


def test_groundedness_and_hallucination_are_complements() -> None:
    report = _report(["cobrancas mensais indevidas", "clausula inexistente"])
    grounded = metrics.groundedness(report, DOC_TEXT)
    hallucinated = metrics.hallucination_rate(report, DOC_TEXT)
    assert grounded.score == 0.5
    assert hallucinated.score == 0.5


def test_no_citations_means_zero_groundedness_full_hallucination_risk() -> None:
    report = _report([])
    assert metrics.groundedness(report, DOC_TEXT).score == 0.0
    assert metrics.hallucination_rate(report, DOC_TEXT).score == 1.0


def test_extraction_accuracy_and_completeness() -> None:
    extraction = LawsuitExtraction(
        claim_value=MonetaryValue(amount=16978.0),
        parties=[
            Party(name="Maria Silva", role=PartyRole.PLAINTIFF),
            Party(name="Banco Exemplo S.A.", role=PartyRole.DEFENDANT),
        ],
        main_requests=["indenizacao por danos morais"],
    )
    expected = {
        "claim_value_amount": 16978.0,
        "plaintiff": "Maria Silva",
        "defendant": "Banco Exemplo",
        "main_requests_contains": ["danos morais"],
    }
    assert metrics.extraction_accuracy(extraction, expected).score == 1.0
    assert metrics.completeness(extraction, expected).score == 1.0

    wrong = {"claim_value_amount": 99.0, "plaintiff": "Outra Pessoa"}
    assert metrics.extraction_accuracy(extraction, wrong).score == 0.0


def test_relevance_judgments_resolve_against_current_chunks() -> None:
    chunks = [
        Chunk(
            chunk_id="new-chunk-id",
            doc_id="d1",
            text="[DOS FATOS]\nHouve cobrancas mensais indevidas de tarifa.",
            section="DOS FATOS",
            page_start=1,
            page_end=1,
        ),
        Chunk(
            chunk_id="another-id",
            doc_id="d1",
            text="[DOS PEDIDOS]\nRequer indenizacao por danos morais.",
            section="DOS PEDIDOS",
            page_start=2,
            page_end=2,
        ),
        Chunk(
            chunk_id="overlapping-id",
            doc_id="d1",
            text="[DOS FATOS]\nCobrancas mensais indevidas continuaram.",
            section="DOS FATOS",
            page_start=1,
            page_end=1,
        ),
    ]

    relevant = metrics.relevant_chunk_ids(
        chunks,
        page_ranges=[(1, 1)],
        passages=["COBRANÇAS mensais indevidas"],
    )

    assert relevant == {"new-chunk-id", "overlapping-id"}
    groups = metrics.relevant_chunk_groups(
        chunks,
        page_ranges=[(1, 1)],
        passages=["COBRANÇAS mensais indevidas"],
    )
    assert groups == [{"new-chunk-id", "overlapping-id"}]


def test_binary_retrieval_metrics_at_k() -> None:
    def retrieved(chunk_id: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk=Chunk(
                chunk_id=chunk_id,
                doc_id="d1",
                text=chunk_id,
                page_start=1,
                page_end=1,
            ),
            score=1.0,
        )

    results = metrics.retrieval_metrics_at_k(
        [[retrieved("relevant-a"), retrieved("irrelevant"), retrieved("relevant-b")]],
        [{"relevant-a", "relevant-b", "relevant-c"}],
        k=3,
    )
    scores = {metric.name: metric.score for metric in results}

    assert scores == {
        "retrieval_precision@3": 0.667,
        "retrieval_recall@3": 0.667,
        "retrieval_hit_rate@3": 1.0,
        "retrieval_mrr@3": 1.0,
        "retrieval_ndcg@3": 0.704,
    }


def test_retrieval_metrics_deduplicate_ids_and_collapse_overlap_groups() -> None:
    def retrieved(chunk_id: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk=Chunk(
                chunk_id=chunk_id,
                doc_id="d1",
                text=chunk_id,
                page_start=1,
                page_end=1,
            ),
            score=1.0,
        )

    results = metrics.retrieval_metrics_at_k(
        [[retrieved("overlap-a"), retrieved("overlap-a"), retrieved("irrelevant")]],
        [[{"overlap-a", "overlap-b"}]],
        k=3,
    )
    scores = {metric.name: metric.score for metric in results}

    assert scores["retrieval_precision@3"] == 0.333
    assert scores["retrieval_recall@3"] == 1.0
    assert scores["retrieval_ndcg@3"] == 1.0
    assert all(0.0 <= score <= 1.0 for score in scores.values())


def test_unresolved_retrieval_judgment_fails_loudly() -> None:
    with pytest.raises(ValueError, match="resolved to no current chunk"):
        metrics.retrieval_metrics_at_k([[]], [[set()]], k=3)


@pytest.mark.parametrize(
    ("judgment", "message"),
    [
        (
            {
                "query": "cobranca",
                "relevant_page_ranges": [{"start": 2, "end": 2}],
            },
            "outside the document",
        ),
        (
            {
                "query": "cobranca",
                "relevant_page_ranges": [{"start": 1, "end": 1}],
                "relevant_passages": ["texto que nao existe"],
            },
            "passage does not occur",
        ),
    ],
)
def test_golden_loader_rejects_unresolvable_retrieval_labels(
    tmp_path: Path, judgment: dict, message: str
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "name": "invalid",
                "pages": ["A autora relata cobranca indevida."],
                "expected": {},
                "retrieval": {"k": 3, "queries": [judgment]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_case(path)


async def test_runner_over_golden_dataset_offline() -> None:
    cases = load_dataset(Path("eval_data"))
    assert {c.name for c in cases} == {"consumer_billing", "labor_overtime"}
    assert all(case.retrieval_k == 3 for case in cases)
    assert all(len(case.retrieval_judgments) == 3 for case in cases)

    rag = RagPipeline(embedder=MockEmbeddingClient(), store=InMemoryVectorStore())
    graph = build_analysis_graph(MockLLMClient(), rag)
    summary = await EvaluationRunner(graph, rag=rag).run(cases)

    assert len(summary.cases) == 2
    # every metric present for every case
    for case in summary.cases:
        names = {m.name for m in case.metrics}
        assert {
            "groundedness",
            "hallucination_rate",
            "citation_coverage",
            "extraction_accuracy",
            "completeness",
            "classification_accuracy",
            "retrieval_precision@3",
            "retrieval_recall@3",
            "retrieval_hit_rate@3",
            "retrieval_mrr@3",
            "retrieval_ndcg@3",
        } <= names
    assert "groundedness" in summary.averages
    assert 0.0 <= summary.averages["retrieval_recall@3"] <= 1.0
