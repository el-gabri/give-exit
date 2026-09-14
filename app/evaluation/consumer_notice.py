"""Evaluate the legal grounds a notice would actually cite.

The retrieval benchmark scores ranked candidates. A notice cites only what the
production selector keeps, so this evaluator runs the production-shaped batch
(the three production queries, k=8, hybrid) through the same
``select_legal_grounds`` the service uses, and scores the final grounds against
the golden labels.
"""

from __future__ import annotations

from typing import Literal

from app.consumer.ground_selection import select_legal_grounds
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_policy import LEGAL_GROUND_POLICY_VERSION
from app.consumer.retrieval import build_legal_queries, is_consumer_scope
from app.consumer.schemas import ConsumerCaseFacts, LegalGround
from app.core.config import RetrievalMode
from app.evaluation.consumer_golden import validate_consumer_legal_labels
from app.evaluation.consumer_retrievers import (
    configured_pipeline,
    offline_pipeline,
    prepare_evaluation_pipeline,
)
from app.evaluation.consumer_runner import QUERY_BUILDER_VERSION, query_hashes
from app.rag.pipeline import RagPipeline
from app.schemas.evaluation import (
    CaseResult,
    ConsumerLegalGoldenCase,
    ConsumerLegalGoldenDataset,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricResult,
    RankedEvaluationRetrievalHit,
    RetrievalEvaluationConfiguration,
)

NOTICE_REQUESTED_K = 8
# Sources that may only complement the CDC in a notice. None exist until the
# LGPD and the Civil Code enter the corpus; the count is reported from now on.
COMPLEMENTARY_LAW_IDS = frozenset({"br-lgpd", "br-cc"})
_UNIT_MARKERS = ("-caput", "-paragrafo-", "-inciso-", "-alinea-")

NoticePipelineName = Literal["offline", "configured"]


def is_known_bad_citation(
    provision_id: str,
    unit_id: str | None,
    hard_negatives: frozenset[str],
) -> bool:
    """Whether a cited ground is one of the case's labelled hard negatives.

    Article-level labels match by provision id only: a prefix test would treat
    art. 42-A as a subdivision of art. 42. Unit-level labels also match their
    own descendants, because a labelled paragraph covers its incisos.
    """

    if provision_id in hard_negatives:
        return True
    if unit_id is None:
        return False
    return any(
        unit_id == negative or (_is_unit_id(negative) and unit_id.startswith(f"{negative}-"))
        for negative in hard_negatives
    )


def _is_unit_id(stable_id: str) -> bool:
    return any(marker in stable_id for marker in _UNIT_MARKERS)


def notice_ground_metrics(
    grounds: list[LegalGround],
    case: ConsumerLegalGoldenCase,
    *,
    degraded: bool,
) -> tuple[list[MetricResult], dict[str, int]]:
    """Score the grounds of one case; counts are summed across the run."""

    hard_negatives = frozenset(case.hard_negatives)
    counts = {
        "consumer_notice_grounds": len(grounds),
        "consumer_notice_known_bad_citations": sum(
            is_known_bad_citation(
                ground.authority.provision_id, ground.authority.unit_id, hard_negatives
            )
            for ground in grounds
        ),
        "consumer_notice_complementary_grounds": sum(
            ground.authority.law_id in COMPLEMENTARY_LAW_IDS for ground in grounds
        ),
    }
    metrics = [
        MetricResult(
            name="consumer_notice_semantic_success",
            score=0.0 if degraded else 1.0,
            details=(
                "lexical-only fallback was used" if degraded else "hybrid retrieval completed"
            ),
        )
    ]
    if case.no_applicable_ground:
        metrics.append(
            MetricResult(
                name="consumer_notice_abstention",
                score=1.0 if not grounds else 0.0,
                details=f"{len(grounds)} grounds cited for a no-ground complaint",
            )
        )
        return metrics, counts
    cited = sum(
        any(_cites(ground, judgment.article_id, judgment.unit_id) for ground in grounds)
        for judgment in case.relevant
    )
    metrics.append(
        MetricResult(
            name="consumer_notice_exact_recall",
            score=round(cited / len(case.relevant), 3),
            details=f"{cited}/{len(case.relevant)} labelled judgments cited",
        )
    )
    return metrics, counts


def _cites(ground: LegalGround, article_id: str, unit_id: str | None) -> bool:
    if unit_id is not None:
        return ground.authority.unit_id == unit_id
    return ground.authority.provision_id == article_id


def _failed_notice_metrics(case: ConsumerLegalGoldenCase) -> list[MetricResult]:
    name = (
        "consumer_notice_abstention"
        if case.no_applicable_ground
        else "consumer_notice_exact_recall"
    )
    return [
        MetricResult(
            name="consumer_notice_semantic_success",
            score=0.0,
            details="retrieval failed",
        ),
        MetricResult(
            name=name,
            score=0.0,
            details="retrieval failure is not a valid result",
        ),
    ]


class ConsumerNoticeGroundEvaluator:
    """Run golden cases through production retrieval and ground selection."""

    def __init__(
        self,
        pipeline: RagPipeline,
        corpus: LegalCorpus,
        *,
        retriever_id: str,
    ) -> None:
        self._pipeline = pipeline
        self._corpus = corpus
        self._retriever_id = retriever_id

    async def run(self, dataset: ConsumerLegalGoldenDataset) -> EvaluationSummary:
        corpus = validate_consumer_legal_labels(dataset, corpus=self._corpus)
        doc_id = corpus.as_parsed_document().doc_id
        cases = [await self._run_case(case, doc_id) for case in dataset.cases]
        configuration = self._pipeline.retrieval_configuration(
            requested_k=NOTICE_REQUESTED_K,
            mode=RetrievalMode.HYBRID,
            doc_id=doc_id,
        )
        retrieval = RetrievalEvaluationConfiguration.model_validate(
            {
                **configuration,
                "retriever_id": self._retriever_id,
                "configuration_complete": True,
            }
        )
        run = EvaluationRunMetadata(
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_sha256=dataset.content_sha256,
            dataset_review_status=dataset.review_status,
            corpus_release_id=corpus.release_id,
            corpus_sha256=corpus.corpus_sha256,
            query_builder_version=QUERY_BUILDER_VERSION,
            queries_per_case=2,
            cutoffs=(NOTICE_REQUESTED_K,),
            retrieval=retrieval,
            ground_policy_version=LEGAL_GROUND_POLICY_VERSION,
        )
        return EvaluationSummary.from_cases(cases, run=run)

    async def _run_case(self, case: ConsumerLegalGoldenCase, doc_id: str) -> CaseResult:
        facts = ConsumerCaseFacts(
            complaint_summary=case.complaint,
            desired_resolution=case.desired_resolution,
        )
        queries: list[str] = []
        grounds: list[LegalGround] = []
        try:
            if not is_consumer_scope(
                complaint=case.complaint,
            ):
                outcome = "scope_gate_abstained"
            else:
                queries = build_legal_queries(facts)
                result_sets, traces = await self._pipeline.retrieve_many_with_traces(
                    queries,
                    doc_id=doc_id,
                    agent="consumer_legal_authorities",
                    k=NOTICE_REQUESTED_K,
                    mode=RetrievalMode.HYBRID,
                )
                grounds = select_legal_grounds(self._corpus, facts, result_sets, traces)
                outcome = (
                    "degraded" if any(trace.degraded_mode for trace in traces) else "completed"
                )
        except Exception as exc:  # one provider failure must not erase other cases
            return CaseResult(
                case_name=case.case_id,
                category=case.category,
                slices=case.slices,
                queries=tuple(queries),
                query_sha256=query_hashes(queries),
                retrieval_outcome="failed",
                metrics=_failed_notice_metrics(case),
                errors=[f"notice retrieval failed: {type(exc).__name__}: {exc}"],
            )
        metrics, counts = notice_ground_metrics(grounds, case, degraded=outcome == "degraded")
        return CaseResult(
            case_name=case.case_id,
            category=case.category,
            slices=case.slices,
            queries=tuple(queries),
            query_sha256=query_hashes(queries),
            retrieved_hits=tuple(
                RankedEvaluationRetrievalHit(
                    rank=position,
                    retrieval_id=ground.authority.unit_id or ground.authority.provision_id,
                    provision_id=ground.authority.provision_id,
                    unit_id=ground.authority.unit_id,
                    score=ground.authority.retrieval_score or 0.0,
                    status=ground.authority.status.value,
                )
                for position, ground in enumerate(grounds, start=1)
            ),
            retrieval_outcome=outcome,
            metrics=metrics,
            counts=counts,
        )


async def run_notice_evaluation(
    dataset: ConsumerLegalGoldenDataset,
    *,
    pipeline_name: NoticePipelineName = "offline",
) -> EvaluationSummary:
    """Evaluate final grounds with the offline mock stack or the configured one."""

    corpus = get_default_legal_corpus()
    factory = offline_pipeline if pipeline_name == "offline" else configured_pipeline
    pipeline = await prepare_evaluation_pipeline(factory, corpus)
    evaluator = ConsumerNoticeGroundEvaluator(
        pipeline, corpus, retriever_id=f"{pipeline_name}_notice_path"
    )
    return await evaluator.run(dataset)
