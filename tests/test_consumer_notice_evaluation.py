"""Notice-path evaluation: final grounds scored against the golden labels."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import LEGAL_GROUND_POLICY_VERSION
from app.consumer.schemas import ConsumerIssueCategory, LegalGround
from app.evaluation import consumer_runner
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.evaluation.consumer_notice import (
    ConsumerNoticeGroundEvaluator,
    is_known_bad_citation,
    notice_ground_metrics,
    run_notice_evaluation,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.evaluation import ConsumerLegalGoldenCase, ConsumerLegalRelevance
from app.schemas.rag import RetrievedChunk

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")


def _ground(unit_id: str) -> LegalGround:
    corpus = get_default_legal_corpus()
    chunk = next(
        item for item in corpus.as_chunks() if item.metadata.get("unit_id") == unit_id
    )
    return LegalGround(
        authority=corpus.authority_for_chunk(
            RetrievedChunk(chunk=chunk, score=0.03), retrieval_rank=1
        ),
        application_to_facts="fixture",
    )


def _case(**overrides: Any) -> ConsumerLegalGoldenCase:
    payload: dict[str, Any] = {
        "case_id": "notice_fixture",
        "category": "unauthorized_charge",
        "intake_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
        "slices": ("supplier:telecom",),
        "complaint": "A operadora cobrou um pacote que eu nunca contratei e já paguei.",
        "desired_resolution": "Quero meu dinheiro de volta.",
        "relevant": (
            ConsumerLegalRelevance(
                article_id="br-cdc-art-42",
                unit_id="br-cdc-art-42-paragrafo-unico",
                grade=3,
                rationale="repetição do indébito",
            ),
            ConsumerLegalRelevance(
                article_id="br-cdc-art-6",
                unit_id="br-cdc-art-6-inciso-iii",
                grade=1,
                rationale="informação clara",
            ),
        ),
        "hard_negatives": ("br-cdc-art-43",),
    }
    payload.update(overrides)
    return ConsumerLegalGoldenCase(**payload)


class _BrokenQueryEmbedder(MockEmbeddingClient):
    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("synthetic outage")


class _ExplodingPipeline:
    def retrieval_configuration(self, **_: object) -> dict[str, object]:
        return {"requested_k": 8, "retrieval_mode": "hybrid"}

    async def retrieve_many_with_traces(self, *_: object, **__: object) -> object:
        raise RuntimeError("store offline")


def test_known_bad_matching_is_exact_and_unit_aware() -> None:
    negatives = frozenset({"br-cdc-art-42", "br-cdc-art-18-paragrafo-1"})

    assert is_known_bad_citation("br-cdc-art-42", "br-cdc-art-42-paragrafo-unico", negatives)
    assert is_known_bad_citation(
        "br-cdc-art-18", "br-cdc-art-18-paragrafo-1-inciso-ii", negatives
    )
    assert not is_known_bad_citation("br-cdc-art-42-a", "br-cdc-art-42-a-caput", negatives)
    assert not is_known_bad_citation("br-cdc-art-18", "br-cdc-art-18-paragrafo-10", negatives)
    assert not is_known_bad_citation("br-cdc-art-18", None, negatives)


def test_final_grounds_are_scored_by_exact_unit_and_known_bad_citations() -> None:
    grounds = [
        _ground("br-cdc-art-42-paragrafo-unico"),
        _ground("br-cdc-art-43-paragrafo-2"),
    ]

    metrics, counts = notice_ground_metrics(grounds, _case(), degraded=False)

    assert counts == {
        "consumer_notice_grounds": 2,
        "consumer_notice_known_bad_citations": 1,
        "consumer_notice_complementary_grounds": 0,
    }
    assert {metric.name: metric.score for metric in metrics} == {
        "consumer_notice_semantic_success": 1.0,
        "consumer_notice_exact_recall": 0.5,
    }


def test_no_ground_cases_score_abstention_and_degradation() -> None:
    case = _case(
        category="no_consumer_relationship",
        relevant=(),
        hard_negatives=(),
        no_applicable_ground=True,
    )

    abstained, _ = notice_ground_metrics([], case, degraded=True)
    cited, _ = notice_ground_metrics(
        [_ground("br-cdc-art-42-paragrafo-unico")], case, degraded=False
    )

    assert {metric.name: metric.score for metric in abstained} == {
        "consumer_notice_semantic_success": 0.0,
        "consumer_notice_abstention": 1.0,
    }
    assert {metric.name: metric.score for metric in cited}["consumer_notice_abstention"] == 0.0


async def test_offline_notice_baseline_on_the_seed_dataset() -> None:
    """Baseline measured on 2026-09-07 (plan.md finding 4); change it deliberately."""

    summary = await run_notice_evaluation(load_consumer_legal_dataset(DATASET_PATH))

    assert summary.failed_case_count == 0
    assert summary.totals == {
        "consumer_notice_complementary_grounds": 0,
        "consumer_notice_grounds": 79,
        "consumer_notice_known_bad_citations": 4,
    }
    assert summary.averages["consumer_notice_exact_recall"] == 0.333
    assert summary.averages["consumer_notice_abstention"] == 1.0
    assert summary.averages["consumer_notice_semantic_success"] == 1.0
    assert summary.run is not None
    assert summary.run.cutoffs == (8,)
    assert summary.run.ground_policy_version == LEGAL_GROUND_POLICY_VERSION
    assert summary.run.retrieval.retriever_id == "offline_notice_path"


async def test_degraded_retrieval_is_reported_per_case() -> None:
    corpus = get_default_legal_corpus()
    store = InMemoryVectorStore()
    await RagPipeline(MockEmbeddingClient(), store).index_chunks(corpus.as_chunks())
    evaluator = ConsumerNoticeGroundEvaluator(
        RagPipeline(_BrokenQueryEmbedder(), store), corpus, retriever_id="degraded_test"
    )
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    single = dataset.model_copy(update={"cases": dataset.cases[:1]})

    summary = await evaluator.run(single)

    assert summary.cases[0].retrieval_outcome == "degraded"
    assert summary.averages["consumer_notice_semantic_success"] == 0.0


async def test_a_failing_case_is_recorded_without_stopping_the_run() -> None:
    corpus = get_default_legal_corpus()
    evaluator = ConsumerNoticeGroundEvaluator(
        _ExplodingPipeline(),  # type: ignore[arg-type]
        corpus,
        retriever_id="exploding",
    )

    summary = await evaluator.run(load_consumer_legal_dataset(DATASET_PATH))

    failed = [case for case in summary.cases if case.retrieval_outcome == "failed"]
    assert len(failed) == 13
    assert summary.failed_case_count == 13
    assert all("store offline" in case.errors[0] for case in failed)


def test_cli_writes_notice_results_and_gates_on_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "notice.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consumer_runner",
            str(DATASET_PATH),
            "--evaluate-notice",
            "--output",
            str(output),
            "--max",
            "consumer_notice_known_bad_citations=3",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["totals"]["consumer_notice_known_bad_citations"] == 4


def test_cli_rejects_require_semantic_without_notice_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["consumer_runner", "--require-semantic"])

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 2
