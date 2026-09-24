"""Evaluate the legal grounds a notice would actually cite.

The retrieval benchmark scores ranked candidates. A notice cites only what the
production selector keeps, so this evaluator runs the production-shaped batch
(the production queries, k=8, hybrid) through the same
``select_legal_grounds`` the service uses, and scores the final grounds against
the golden labels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from app.consumer.ground_selection import select_legal_grounds
from app.consumer.ground_verifier import GroundVerifier, NoGroundVerifier
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_policy import (
    AGREEMENT_MAX_RANK,
    LEGAL_GROUND_POLICY_VERSION,
    strongly_supported_chunk_ids,
)
from app.consumer.retrieval import (
    LEGAL_REQUESTED_K,
    build_legal_queries,
    is_consumer_scope,
    retrieve_legal_candidates,
)
from app.consumer.schemas import ConsumerCaseFacts, LegalGround
from app.core.config import GroundVerifierMode, RetrievalMode
from app.evaluation.consumer_golden import validate_consumer_legal_labels
from app.evaluation.consumer_retrievers import (
    configured_pipeline,
    offline_pipeline,
    prepare_evaluation_pipeline,
)
from app.evaluation.consumer_runner import QUERY_BUILDER_VERSION, query_hashes
from app.rag.pipeline import RagPipeline
from app.schemas.evaluation import (
    LEGAL_UNIT_MARKERS,
    CaseResult,
    ConsumerLegalGoldenCase,
    ConsumerLegalGoldenDataset,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricResult,
    RankedEvaluationRetrievalHit,
    RetrievalEvaluationConfiguration,
    max_queries_per_case,
)
from app.schemas.rag import RetrievedChunk
from app.schemas.trace import RetrievalTrace

NOTICE_REQUESTED_K = LEGAL_REQUESTED_K
# Sources that share the complementary ground budget (ADR 0016); their grounds
# are counted separately.
COMPLEMENTARY_LAW_IDS = frozenset({"br-lgpd", "br-cc"})

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
    return any(marker in stable_id for marker in LEGAL_UNIT_MARKERS)


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


@dataclass(frozen=True, slots=True)
class _CaseRetrieval:
    """One case's legal retrieval, reused by every gate depth measured."""

    result_sets: list[list[RetrievedChunk]] = field(default_factory=list)
    traces: list[RetrievalTrace] = field(default_factory=list)
    outcome: str = "scope_gate_abstained"


class ConsumerNoticeGroundEvaluator:
    """Run golden cases through production retrieval and ground selection."""

    def __init__(
        self,
        pipeline: RagPipeline,
        corpus: LegalCorpus,
        *,
        retriever_id: str,
        ground_verifier: GroundVerifier | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._corpus = corpus
        self._retriever_id = retriever_id
        self._ground_verifier = ground_verifier or NoGroundVerifier()
        self._verifier_mode = GroundVerifierMode.NONE
        # Retrieval does not depend on the gate depth, so a sweep over depths
        # embeds and searches each case once.
        self._retrievals: dict[str, _CaseRetrieval] = {}

    async def run(
        self,
        dataset: ConsumerLegalGoldenDataset,
        *,
        agreement_max_rank: int = AGREEMENT_MAX_RANK,
    ) -> EvaluationSummary:
        corpus = validate_consumer_legal_labels(dataset, corpus=self._corpus)
        doc_id = corpus.document_id
        cases = [
            await self._run_case(case, doc_id, agreement_max_rank) for case in dataset.cases
        ]
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
            queries_per_case=max_queries_per_case(cases),
            cutoffs=(NOTICE_REQUESTED_K,),
            retrieval=retrieval,
            ground_policy_version=LEGAL_GROUND_POLICY_VERSION,
            agreement_max_rank=agreement_max_rank,
            ground_verifier=self._verifier_mode.value,
        )
        return EvaluationSummary.from_cases(cases, run=run)

    async def _run_case(
        self, case: ConsumerLegalGoldenCase, doc_id: str, agreement_max_rank: int
    ) -> CaseResult:
        facts = ConsumerCaseFacts(
            complaint_summary=case.complaint,
            desired_resolution=case.desired_resolution,
        )
        in_scope = is_consumer_scope(complaint=case.complaint)
        queries = build_legal_queries(facts) if in_scope else []
        try:
            retrieval = await self._retrieve(case.case_id, facts, queries, doc_id)
            grounds = select_legal_grounds(
                self._corpus,
                facts,
                retrieval.result_sets,
                retrieval.traces,
                support=partial(strongly_supported_chunk_ids, max_rank=agreement_max_rank),
            )
            grounds, verifier_counts = await self._verify(facts, grounds)
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
        metrics, counts = notice_ground_metrics(
            grounds, case, degraded=retrieval.outcome == "degraded"
        )
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
            retrieval_outcome=retrieval.outcome,
            metrics=metrics,
            counts={**counts, **verifier_counts},
        )

    async def _retrieve(
        self, case_id: str, facts: ConsumerCaseFacts, queries: list[str], doc_id: str
    ) -> _CaseRetrieval:
        cached = self._retrievals.get(case_id)
        if cached is not None:
            return cached
        if not queries:  # the scope gate abstained before retrieval
            retrieval = _CaseRetrieval()
        else:
            result_sets, traces = await retrieve_legal_candidates(
                self._pipeline, facts, doc_id=doc_id, k=NOTICE_REQUESTED_K
            )
            retrieval = _CaseRetrieval(
                result_sets=result_sets,
                traces=traces,
                outcome=(
                    "degraded" if any(trace.degraded_mode for trace in traces) else "completed"
                ),
            )
        self._retrievals[case_id] = retrieval
        return retrieval

    async def _verify(
        self, facts: ConsumerCaseFacts, grounds: list[LegalGround]
    ) -> tuple[list[LegalGround], dict[str, int]]:
        """Apply the configured verifier as the service does; count what it did."""
        if not grounds:
            return grounds, {}
        result = await self._ground_verifier.verify(facts, grounds)
        self._verifier_mode = result.summary.mode
        if result.summary.mode is GroundVerifierMode.NONE:
            return result.grounds, {}
        return result.grounds, {
            "consumer_notice_verifier_removed": result.summary.removed,
            "consumer_notice_verifier_failures": int(result.summary.error is not None),
        }


async def run_notice_evaluation(
    dataset: ConsumerLegalGoldenDataset,
    *,
    pipeline_name: NoticePipelineName = "offline",
    agreement_max_rank: int = AGREEMENT_MAX_RANK,
    ground_verifier: GroundVerifier | None = None,
) -> EvaluationSummary:
    """Evaluate final grounds with the offline mock stack or the configured one."""

    sweep = await run_notice_agreement_sweep(
        dataset,
        pipeline_name=pipeline_name,
        agreement_max_ranks=(agreement_max_rank,),
        ground_verifier=ground_verifier,
    )
    return sweep[agreement_max_rank]


async def run_notice_agreement_sweep(
    dataset: ConsumerLegalGoldenDataset,
    *,
    pipeline_name: NoticePipelineName = "offline",
    agreement_max_ranks: Sequence[int],
    ground_verifier: GroundVerifier | None = None,
) -> dict[int, EvaluationSummary]:
    """Evaluate the notice grounds at several agreement-gate depths.

    Each case is retrieved once; only ground selection (and the verifier, when
    one is configured) runs again per depth.
    """

    corpus = get_default_legal_corpus()
    factory = offline_pipeline if pipeline_name == "offline" else configured_pipeline
    pipeline = await prepare_evaluation_pipeline(factory, corpus)
    evaluator = ConsumerNoticeGroundEvaluator(
        pipeline,
        corpus,
        retriever_id=f"{pipeline_name}_notice_path",
        ground_verifier=ground_verifier,
    )
    try:
        return {
            rank: await evaluator.run(dataset, agreement_max_rank=rank)
            for rank in agreement_max_ranks
        }
    finally:
        pipeline.close()
