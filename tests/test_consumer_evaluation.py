"""Consumer-mode legal retrieval golden and evaluator tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import ConsumerIssueCategory, ProvisionStatus
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.evaluation.consumer_retrievers import offline_hybrid_retriever
from app.evaluation.consumer_runner import (
    ConsumerLegalRetrievalEvaluator,
    check_consumer_gates,
    consumer_legal_metrics_at_k,
    normalize_consumer_retrieval_hit,
)
from app.schemas.evaluation import (
    ConsumerLegalGoldenCase,
    ConsumerLegalGoldenDataset,
    ConsumerLegalRelevance,
    ConsumerLegalRetrievalHit,
    EvaluationSummary,
)

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")


def test_consumer_regression_gates_check_floors_ceilings_and_missing_metrics() -> None:
    summary = EvaluationSummary(
        averages={"consumer_recall@5": 0.4, "consumer_hard_negative_rate@5": 0.1}
    )

    assert check_consumer_gates(
        summary,
        minimums=(("consumer_recall@5", 0.3),),
        maximums=(("consumer_hard_negative_rate@5", 0.2),),
    ) == []
    assert check_consumer_gates(
        summary,
        minimums=(("consumer_recall@5", 0.5), ("unknown", 0.1)),
        maximums=(("consumer_hard_negative_rate@5", 0.05),),
    ) == [
        "consumer_recall@5: 0.400 < required 0.500",
        "unknown: not produced by this run, cannot gate on it",
        "consumer_hard_negative_rate@5: 0.100 > allowed 0.050",
    ]


def _case(*, no_ground: bool = False) -> ConsumerLegalGoldenCase:
    return ConsumerLegalGoldenCase(
        case_id="metric_fixture",
        category="unauthorized_charge",
        intake_category=ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
        slices=("supplier:telecom",),
        complaint="A operadora cobrou um pacote que eu nunca contratei e já paguei.",
        desired_resolution="Quero meu dinheiro de volta.",
        relevant=(
            ()
            if no_ground
            else (
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
            )
        ),
        hard_negatives=("br-cdc-art-54-e",),
        no_applicable_ground=no_ground,
    )


def _hit(
    provision_id: str,
    *,
    unit_id: str | None = None,
    status: str = "active",
) -> ConsumerLegalRetrievalHit:
    return ConsumerLegalRetrievalHit(
        provision_id=provision_id,
        unit_id=unit_id,
        status=status,
    )


def test_seed_dataset_is_separate_versioned_and_explicitly_unreviewed() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)

    assert dataset.dataset_id == "consumer-legal-retrieval-seed"
    assert dataset.version == "1.1.0"
    assert dataset.authoring == "developer_authored_seed"
    assert dataset.review_status == "requires_legal_review"
    assert dataset.source_url.endswith("/l8078compilado.htm")
    assert len(dataset.cases) == 15
    assert len({case.category for case in dataset.cases}) >= 12
    assert sum(case.no_applicable_ground for case in dataset.cases) == 2
    assert all(case.slices for case in dataset.cases)
    salary_case = next(case for case in dataset.cases if case.case_id == "salario_atrasado")
    assert salary_case.category == "no_consumer_relationship"
    assert salary_case.intake_category is ConsumerIssueCategory.OTHER
    assert any(
        judgment.unit_id == "br-cdc-art-42-paragrafo-unico"
        for case in dataset.cases
        for judgment in case.relevant
    )


def test_seed_labels_resolve_to_active_units_in_versioned_cdc() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    corpus = get_default_legal_corpus()
    articles = {item.provision_id: item for item in corpus.provisions}
    units = {unit.unit_id: unit for article in corpus.provisions for unit in article.units}
    corpus_ids = {*articles, *units}

    for case in dataset.cases:
        for judgment in case.relevant:
            assert judgment.article_id in articles, (
                case.case_id,
                judgment.article_id,
            )
            assert articles[judgment.article_id].status is ProvisionStatus.ACTIVE
            if judgment.unit_id is not None:
                assert judgment.unit_id in units, (case.case_id, judgment.unit_id)
                assert units[judgment.unit_id].status is ProvisionStatus.ACTIVE
        for hard_negative in case.hard_negatives:
            assert hard_negative in corpus_ids, (case.case_id, hard_negative)


def test_loader_rejects_labels_that_are_both_relevant_and_negative(
    tmp_path: Path,
) -> None:
    payload = json.loads((DATASET_PATH / "dataset.json").read_text(encoding="utf-8"))
    first = payload["cases"][0]
    first["hard_negatives"] = [first["relevant"][0]["unit_id"]]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="both relevant and a hard negative"):
        load_consumer_legal_dataset(path)


def test_loader_rejects_labels_missing_from_the_pinned_corpus(tmp_path: Path) -> None:
    payload = json.loads((DATASET_PATH / "dataset.json").read_text(encoding="utf-8"))
    payload["cases"][0]["relevant"][0] = {
        "article_id": "br-cdc-art-999",
        "grade": 3,
        "rationale": "fixture deliberately outside the corpus",
    }
    path = tmp_path / "unknown-label.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown relevant article br-cdc-art-999"):
        load_consumer_legal_dataset(path)


def test_graded_metrics_include_exact_article_and_safety_signals() -> None:
    hits = [
        _hit("br-cdc-art-35"),
        _hit(
            "br-cdc-art-42",
            unit_id="br-cdc-art-42-paragrafo-unico",
        ),
        _hit("br-cdc-art-54-e", status="vetoed"),
        _hit(
            "br-cdc-art-6",
            unit_id="br-cdc-art-6-inciso-iii",
            status="unknown",
        ),
        _hit(
            "br-cdc-art-6",
            unit_id="br-cdc-art-6-inciso-iii",
            status="unknown",
        ),
    ]

    results = consumer_legal_metrics_at_k(hits, _case(), k=5)
    scores = {result.name: result.score for result in results}

    assert scores["consumer_recall@5"] == 1.0
    assert scores["consumer_article_recall@5"] == 1.0
    assert scores["consumer_mrr@5"] == 0.5
    assert scores["consumer_ndcg@5"] == 0.635
    assert scores["consumer_subdivision_precision@5"] == 0.4
    assert scores["consumer_article_precision@5"] == 0.4
    assert scores["consumer_hard_negative_rate@5"] == 0.25
    assert scores["consumer_inactive_provision_rate@5"] == 0.25
    assert scores["consumer_unknown_status_rate@5"] == 0.25


def test_no_ground_case_scores_explicit_abstention() -> None:
    empty = consumer_legal_metrics_at_k([], _case(no_ground=True), k=5)
    with_hit = consumer_legal_metrics_at_k([_hit("br-cdc-art-2")], _case(no_ground=True), k=5)

    assert {metric.name: metric.score for metric in empty}["consumer_abstention@5"] == 1.0
    assert {metric.name: metric.score for metric in with_hit}["consumer_abstention@5"] == 0.0
    assert all("consumer_recall" not in metric.name for metric in empty)


def test_hit_normalization_reads_retrieved_chunk_metadata() -> None:
    rag_hit = SimpleNamespace(
        score=0.87,
        chunk=SimpleNamespace(
            chunk_id="cdc:42:paragrafo-unico:0001",
            metadata={
                "provision_id": "br-cdc-art-42",
                "unit_id": "br-cdc-art-42-paragrafo-unico",
                "status": "active",
            },
        ),
    )

    normalized = normalize_consumer_retrieval_hit(rag_hit)

    assert normalized.provision_id == "br-cdc-art-42"
    assert normalized.unit_id == "br-cdc-art-42-paragrafo-unico"
    assert normalized.status == "active"
    assert normalized.score == 0.87


async def test_evaluator_accepts_sync_and_async_retriever_callables() -> None:
    dataset = ConsumerLegalGoldenDataset(
        dataset_id="fixture",
        version="1.0.0",
        description="Small deterministic evaluator fixture for callable tests.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(_case(),),
    )
    calls: list[tuple[str, int]] = []

    def sync_retriever(query: str, k: int) -> list[dict[str, object]]:
        calls.append((query, k))
        return [
            {
                "provision_id": "br-cdc-art-42",
                "unit_id": "br-cdc-art-42-paragrafo-unico",
                "status": "active",
                "score": 0.9,
            }
        ]

    async def async_retriever(query: str, k: int) -> list[str]:
        calls.append((query, k))
        return ["br-cdc-art-42-paragrafo-unico"]

    sync_summary = await ConsumerLegalRetrievalEvaluator(sync_retriever).run(dataset)
    async_summary = await ConsumerLegalRetrievalEvaluator(async_retriever).run(dataset)

    assert sync_summary.cases[0].score("consumer_recall@5") == 0.5
    assert async_summary.cases[0].score("consumer_recall@5") == 0.5
    assert len(sync_summary.cases[0].queries) == 3
    assert len(sync_summary.cases[0].query_sha256) == 3
    assert sync_summary.cases[0].retrieved_hits[0].retrieval_id == ("br-cdc-art-42-paragrafo-unico")
    assert len(calls) == 6
    assert all(k == 10 for _, k in calls)
    assert sum("cobrou um pacote" in query for query, _ in calls) == 4
    assert sum("artigo 42" in query for query, _ in calls) == 4
    assert sync_summary.run is not None
    assert sync_summary.run.dataset_sha256 == dataset.content_sha256
    assert sync_summary.run.corpus_sha256 == get_default_legal_corpus().corpus_sha256
    assert sync_summary.run.query_builder_version == "consumer-legal-three-query-v3"
    assert sync_summary.run.queries_per_case == 3
    assert sync_summary.by_category["unauthorized_charge"].case_count == 1
    assert sync_summary.by_slice["supplier:telecom"].case_count == 1
    assert sync_summary.metric_case_counts["consumer_recall@5"] == 1
    assert sync_summary.run.retrieval.configuration_complete is False
    assert sync_summary.metric_directions["consumer_hard_negative_rate@5"] == "lower_is_better"


async def test_evaluator_uses_production_intake_category_for_queries() -> None:
    case = _case().model_copy(
        update={
            # The fine category remains useful for reporting, but it is not a
            # category the guided production intake can submit.
            "category": "right_of_withdrawal",
            "intake_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
        }
    )
    dataset = ConsumerLegalGoldenDataset(
        dataset_id="intake-category-fixture",
        version="1.0.0",
        description="Fixture proving production-equivalent query construction.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(case,),
    )

    summary = await ConsumerLegalRetrievalEvaluator(lambda _query, _k: []).run(dataset)

    result = summary.cases[0]
    assert result.category == "right_of_withdrawal"
    assert summary.by_category["right_of_withdrawal"].case_count == 1
    assert any("artigo 42" in query for query in result.queries)
    assert all("artigo 49" not in query for query in result.queries)


def test_golden_case_rejects_non_production_intake_category() -> None:
    payload = _case().model_dump(mode="json")
    payload["intake_category"] = "right_of_withdrawal"

    with pytest.raises(ValidationError, match="intake_category"):
        ConsumerLegalGoldenCase.model_validate(payload)


async def test_evaluator_isolates_provider_failure_in_case_result() -> None:
    dataset = ConsumerLegalGoldenDataset(
        dataset_id="failure-fixture",
        version="1.0.0",
        description="Small deterministic evaluator fixture for failure isolation.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(_case(),),
    )

    def failing_retriever(_query: str, _k: int) -> list[str]:
        raise RuntimeError("backend unavailable")

    summary = await ConsumerLegalRetrievalEvaluator(failing_retriever).run(dataset)

    assert summary.cases[0].score("consumer_retrieval_success@5") == 0.0
    assert summary.cases[0].score("consumer_recall@5") == 0.0
    assert "backend unavailable" in summary.cases[0].errors[0]
    assert summary.failed_case_count == 1
    assert summary.failure_rate == 1.0
    assert summary.by_category["unauthorized_charge"].failure_rate == 1.0


async def test_no_consumer_scope_gate_abstains_without_calling_retriever() -> None:
    no_scope = _case(no_ground=True).model_copy(
        update={
            "case_id": "no_scope_fixture",
            "category": "no_consumer_relationship",
            "intake_category": ConsumerIssueCategory.OTHER,
            "slices": ("ground:none",),
            "complaint": "Meu empregador não pagou meu salário e minhas horas extras.",
            "desired_resolution": "Quero receber meu salário atrasado.",
        }
    )
    dataset = ConsumerLegalGoldenDataset(
        dataset_id="scope-fixture",
        version="1.0.0",
        description="Small deterministic evaluator fixture for scope-gate tests.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(no_scope,),
    )
    calls = 0

    def retriever(_query: str, _k: int) -> list[str]:
        nonlocal calls
        calls += 1
        return ["br-cdc-art-42"]

    summary = await ConsumerLegalRetrievalEvaluator(retriever).run(dataset)

    assert calls == 0
    assert summary.cases[0].score("consumer_abstention@5") == 1.0
    assert summary.cases[0].score("consumer_retrieval_success@5") == 1.0
    assert summary.cases[0].retrieval_outcome == "scope_gate_abstained"
    assert summary.cases[0].queries == ()
    assert summary.failure_rate == 0.0


async def test_builtin_offline_hybrid_retriever_returns_auditable_legal_hits() -> None:
    hits = await offline_hybrid_retriever(
        "Comprei pela internet há cinco dias e quero desistir da compra.",
        5,
    )

    assert len(hits) == 5
    assert all(hit.chunk.metadata.get("provision_id") for hit in hits)
    assert all(hit.chunk.metadata.get("corpus_release_id") for hit in hits)
    assert all(hit.chunk.metadata.get("content_sha256") for hit in hits)

    configuration = await offline_hybrid_retriever.evaluation_configuration(10)
    corpus = get_default_legal_corpus()
    assert configuration["retrieval_mode"] == "hybrid"
    assert configuration["candidate_k"] == 40
    assert configuration["rrf_constant"] == 60
    assert configuration["corpus_release_id"] == corpus.release_id
    assert configuration["corpus_sha256"] == corpus.corpus_sha256

    dataset = load_consumer_legal_dataset(DATASET_PATH)
    summary = await ConsumerLegalRetrievalEvaluator(
        offline_hybrid_retriever,
        cutoffs=(5,),
    ).run(dataset)
    assert summary.run is not None
    assert summary.run.retrieval.configuration_complete is True
    assert summary.run.retrieval.embedding_model == "mock-hashed-bow-v1:128"
