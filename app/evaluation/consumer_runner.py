"""Provider-neutral evaluation for Consumer legal retrieval.

The evaluator intentionally depends on a small callable boundary instead of a
specific vector store.  Dense, hybrid and reranked implementations can all be
compared with the same input text and stable legal labels, offline.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

from app.consumer.legal_corpus import LegalCorpus
from app.consumer.retrieval import build_legal_queries_for_case, is_consumer_scope
from app.evaluation.consumer_golden import (
    load_consumer_legal_dataset,
    validate_consumer_legal_labels,
)
from app.schemas.evaluation import (
    CaseResult,
    ConsumerLegalGoldenCase,
    ConsumerLegalGoldenDataset,
    ConsumerLegalRetrievalHit,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricResult,
    RankedEvaluationRetrievalHit,
    RetrievalEvaluationConfiguration,
)

ConsumerRetriever = Callable[[str, int], object]

_INACTIVE_STATUSES = {
    "inactive",
    "inativo",
    "repealed",
    "revoked",
    "revogado",
    "vetado",
    "vetoed",
}
_UNKNOWN_STATUSES = {"", "desconhecido", "unknown"}
_UNIT_MARKERS = ("-caput", "-paragrafo-", "-inciso-", "-alinea-")
QUERY_BUILDER_VERSION = "consumer-legal-three-query-v2"


def _threshold(raw: str) -> tuple[str, float]:
    name, separator, value = raw.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"expected metric=value, got {raw!r}")
    try:
        return name.strip(), float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc


def check_consumer_gates(
    summary: EvaluationSummary,
    *,
    minimums: Sequence[tuple[str, float]] = (),
    maximums: Sequence[tuple[str, float]] = (),
) -> list[str]:
    """Return deterministic regression-gate violations for a summary."""

    violations: list[str] = []
    for name, floor in minimums:
        actual = summary.averages.get(name)
        if actual is None:
            violations.append(f"{name}: not produced by this run, cannot gate on it")
        elif actual < floor:
            violations.append(f"{name}: {actual:.3f} < required {floor:.3f}")
    for name, ceiling in maximums:
        actual = summary.averages.get(name)
        if actual is None:
            violations.append(f"{name}: not produced by this run, cannot gate on it")
        elif actual > ceiling:
            violations.append(f"{name}: {actual:.3f} > allowed {ceiling:.3f}")
    return violations


def _article_id_from_stable_id(stable_id: str) -> str:
    marker_positions = [
        position for marker in _UNIT_MARKERS if (position := stable_id.find(marker)) >= 0
    ]
    return stable_id[: min(marker_positions)] if marker_positions else stable_id


def _string_value(value: object, default: str = "") -> str:
    enum_value = getattr(value, "value", value)
    if enum_value is None:
        return default
    return str(enum_value)


def _mapping_from(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def normalize_consumer_retrieval_hit(value: object) -> ConsumerLegalRetrievalHit:
    """Normalize strings, mappings, or ``RetrievedChunk``-like objects.

    Production RAG hits are expected to expose legal fields through
    ``hit.chunk.metadata``.  Direct mappings are convenient for deterministic
    fixtures and external bake-off scripts.
    """

    if isinstance(value, ConsumerLegalRetrievalHit):
        return value
    if isinstance(value, str):
        stable_id = value.strip().lower()
        article_id = _article_id_from_stable_id(stable_id)
        unit_id = stable_id if stable_id != article_id else None
        return ConsumerLegalRetrievalHit(
            provision_id=article_id,
            unit_id=unit_id,
            status="unknown",
        )

    direct = _mapping_from(value)
    chunk_value = direct.get("chunk") if direct else getattr(value, "chunk", None)
    chunk = _mapping_from(chunk_value)
    metadata_value = (
        direct.get("metadata") or chunk.get("metadata") or getattr(chunk_value, "metadata", None)
    )
    metadata = _mapping_from(metadata_value)

    def pick(name: str) -> object | None:
        if name in direct:
            return direct[name]
        if name in metadata:
            return metadata[name]
        if hasattr(value, name):
            return cast(object, getattr(value, name))
        if hasattr(chunk_value, name):
            return cast(object, getattr(chunk_value, name))
        return None

    provision_value = pick("provision_id")
    unit_value = pick("unit_id")
    if provision_value is None:
        chunk_id = pick("chunk_id")
        if chunk_id is not None and _string_value(chunk_id).startswith("br-"):
            unit_value = chunk_id
            provision_value = _article_id_from_stable_id(_string_value(chunk_id))
    if provision_value is None:
        raise ValueError("retrieval hit needs provision_id in the hit or chunk metadata")

    score_value = pick("score")
    status_value = pick("status")
    return ConsumerLegalRetrievalHit(
        provision_id=_string_value(provision_value),
        unit_id=_string_value(unit_value) if unit_value is not None else None,
        score=float(_string_value(score_value)) if score_value is not None else 0.0,
        status=_string_value(status_value, "unknown"),
    )


def _normalize_hits(value: object) -> list[ConsumerLegalRetrievalHit]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise TypeError("retriever must return an iterable of legal retrieval hits")
    return [normalize_consumer_retrieval_hit(hit) for hit in value]


def _matching_judgment_indexes(
    hit: ConsumerLegalRetrievalHit,
    case: ConsumerLegalGoldenCase,
) -> set[int]:
    matches: set[int] = set()
    for index, judgment in enumerate(case.relevant):
        if judgment.unit_id is not None:
            if hit.retrieval_id == judgment.unit_id:
                matches.add(index)
        elif hit.provision_id == judgment.article_id:
            matches.add(index)
    return matches


def _is_hard_negative(hit: ConsumerLegalRetrievalHit, ids: set[str]) -> bool:
    return any(
        hit.retrieval_id == negative_id
        or hit.provision_id == negative_id
        or hit.retrieval_id.startswith(f"{negative_id}-")
        for negative_id in ids
    )


def consumer_legal_metrics_at_k(
    hits: Sequence[ConsumerLegalRetrievalHit],
    case: ConsumerLegalGoldenCase,
    *,
    k: int,
) -> list[MetricResult]:
    """Score one complaint with exact, article-level and safety metrics."""

    if k < 1:
        raise ValueError("k must be at least 1")

    top_hits = list(hits[:k])
    ranked_unique: list[tuple[int, ConsumerLegalRetrievalHit]] = []
    seen_units: set[str] = set()
    for rank, hit in enumerate(top_hits, start=1):
        if hit.retrieval_id in seen_units:
            continue
        seen_units.add(hit.retrieval_id)
        ranked_unique.append((rank, hit))

    hard_negative_ids = set(case.hard_negatives)
    hard_negative_count = sum(_is_hard_negative(hit, hard_negative_ids) for _, hit in ranked_unique)
    inactive_count = sum(hit.status in _INACTIVE_STATUSES for _, hit in ranked_unique)
    unknown_status_count = sum(hit.status in _UNKNOWN_STATUSES for _, hit in ranked_unique)
    returned_count = len(ranked_unique)
    safety_metrics = [
        MetricResult(
            name=f"consumer_retrieval_success@{k}",
            score=1.0,
            details="retrieval completed",
        ),
        MetricResult(
            name=f"consumer_hard_negative_rate@{k}",
            score=round(hard_negative_count / returned_count, 3) if returned_count else 0.0,
            direction="lower_is_better",
            details=f"{hard_negative_count}/{returned_count} unique hits are hard negatives",
        ),
        MetricResult(
            name=f"consumer_inactive_provision_rate@{k}",
            score=round(inactive_count / returned_count, 3) if returned_count else 0.0,
            direction="lower_is_better",
            details=f"{inactive_count}/{returned_count} unique hits are inactive",
        ),
        MetricResult(
            name=f"consumer_unknown_status_rate@{k}",
            score=round(unknown_status_count / returned_count, 3) if returned_count else 0.0,
            direction="lower_is_better",
            details=f"{unknown_status_count}/{returned_count} unique hits lack status",
        ),
    ]

    if case.no_applicable_ground:
        return [
            MetricResult(
                name=f"consumer_abstention@{k}",
                score=1.0 if not top_hits else 0.0,
                details=(
                    "retriever abstained on a no-ground complaint"
                    if not top_hits
                    else f"retriever returned {len(top_hits)} hits for a no-ground complaint"
                ),
            ),
            *safety_metrics,
        ]

    judgments_hit: set[int] = set()
    relevant_ranks: list[int] = []
    gains_by_rank: list[tuple[int, float]] = []
    relevant_result_count = 0
    relevant_article_ids = {judgment.article_id for judgment in case.relevant}
    relevant_articles_seen: set[str] = set()
    article_hit_count = 0

    for rank, hit in ranked_unique:
        matched = _matching_judgment_indexes(hit, case)
        new_matches = matched - judgments_hit
        if new_matches:
            relevant_result_count += 1
            relevant_ranks.append(rank)
            grade = max(case.relevant[index].grade for index in new_matches)
            gains_by_rank.append((rank, float(2**grade - 1)))
            judgments_hit.update(new_matches)
        if (
            hit.provision_id in relevant_article_ids
            and hit.provision_id not in relevant_articles_seen
        ):
            relevant_articles_seen.add(hit.provision_id)
            article_hit_count += 1

    recall = len(judgments_hit) / len(case.relevant)
    reciprocal_rank = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in gains_by_rank)
    ideal_gains = sorted((2**item.grade - 1 for item in case.relevant), reverse=True)[:k]
    idcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal_gains, start=1))

    ranking_metrics = [
        MetricResult(
            name=f"consumer_recall@{k}",
            score=round(recall, 3),
            details=f"{len(judgments_hit)}/{len(case.relevant)} graded judgments retrieved",
        ),
        MetricResult(
            name=f"consumer_article_recall@{k}",
            score=round(len(relevant_articles_seen) / len(relevant_article_ids), 3),
            details=(
                f"{len(relevant_articles_seen)}/{len(relevant_article_ids)} "
                "relevant articles retrieved"
            ),
        ),
        MetricResult(
            name=f"consumer_mrr@{k}",
            score=round(reciprocal_rank, 3),
            details=(
                f"first exact relevant result at rank {relevant_ranks[0]}"
                if relevant_ranks
                else "no exact relevant result"
            ),
        ),
        MetricResult(
            name=f"consumer_ndcg@{k}",
            score=round(dcg / idcg, 3) if idcg else 0.0,
            details="graded gain (essential=3, relevant=2, useful=1)",
        ),
        MetricResult(
            name=f"consumer_subdivision_precision@{k}",
            score=round(relevant_result_count / k, 3),
            details=f"{relevant_result_count}/{k} ranks add an exact relevant unit",
        ),
        MetricResult(
            name=f"consumer_article_precision@{k}",
            score=round(article_hit_count / k, 3),
            details=f"{article_hit_count}/{k} ranks add a relevant article",
        ),
    ]
    return [*ranking_metrics, *safety_metrics]


class ConsumerLegalRetrievalEvaluator:
    """Run the same Consumer golden against any sync or async retriever.

    The callable contract is ``retriever(query: str, k: int) -> iterable``.
    Each item may be a :class:`ConsumerLegalRetrievalHit`, a stable-id string,
    a mapping, or a ``RetrievedChunk``-like object with legal chunk metadata.
    """

    def __init__(
        self,
        retriever: ConsumerRetriever,
        *,
        cutoffs: Sequence[int] = (5, 10),
        corpus: LegalCorpus | None = None,
    ) -> None:
        normalized_cutoffs = tuple(sorted(set(cutoffs)))
        if not normalized_cutoffs or normalized_cutoffs[0] < 1:
            raise ValueError("cutoffs must contain positive integers")
        self._retriever = retriever
        self._cutoffs = normalized_cutoffs
        self._corpus = corpus

    async def run(
        self,
        dataset: ConsumerLegalGoldenDataset,
    ) -> EvaluationSummary:
        corpus = validate_consumer_legal_labels(dataset, corpus=self._corpus)
        results = [await self._run_case(case) for case in dataset.cases]
        retrieval = await self._retrieval_configuration(corpus)
        run = EvaluationRunMetadata(
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_sha256=dataset.content_sha256,
            dataset_review_status=dataset.review_status,
            corpus_release_id=corpus.release_id,
            corpus_sha256=corpus.corpus_sha256,
            query_builder_version=QUERY_BUILDER_VERSION,
            queries_per_case=3,
            cutoffs=self._cutoffs,
            retrieval=retrieval,
        )
        return EvaluationSummary.from_cases(results, run=run)

    async def _run_case(self, case: ConsumerLegalGoldenCase) -> CaseResult:
        queries: list[str] = []
        hits: list[ConsumerLegalRetrievalHit] = []
        retrieval_outcome = "completed"
        try:
            if not is_consumer_scope(
                category=case.category,
                complaint=case.complaint,
            ):
                retrieval_outcome = "scope_gate_abstained"
            else:
                result_sets = []
                queries = build_legal_queries_for_case(
                    category=case.category,
                    complaint=case.complaint,
                    desired_resolution=case.desired_resolution,
                )
                for query in queries:
                    raw_result = self._retriever(query, self._cutoffs[-1])
                    if inspect.isawaitable(raw_result):
                        raw_result = await cast(Awaitable[object], raw_result)
                    result_sets.append(_normalize_hits(raw_result))
                hits = _merge_normalized_hits(result_sets)
        except Exception as exc:  # one provider failure must not erase other cases
            return CaseResult(
                case_name=case.case_id,
                category=case.category,
                slices=case.slices,
                queries=tuple(queries),
                query_sha256=_query_hashes(queries),
                retrieval_outcome="failed",
                metrics=_failed_case_metrics(case, self._cutoffs),
                errors=[f"retrieval failed: {type(exc).__name__}: {exc}"],
            )

        case_metrics = [
            metric
            for cutoff in self._cutoffs
            for metric in consumer_legal_metrics_at_k(hits, case, k=cutoff)
        ]
        return CaseResult(
            case_name=case.case_id,
            category=case.category,
            slices=case.slices,
            queries=tuple(queries),
            query_sha256=_query_hashes(queries),
            retrieved_hits=tuple(
                RankedEvaluationRetrievalHit(
                    rank=rank,
                    retrieval_id=hit.retrieval_id,
                    provision_id=hit.provision_id,
                    unit_id=hit.unit_id,
                    score=hit.score,
                    status=hit.status,
                )
                for rank, hit in enumerate(hits, start=1)
            ),
            retrieval_outcome=retrieval_outcome,
            metrics=case_metrics,
        )

    async def _retrieval_configuration(
        self,
        corpus: LegalCorpus,
    ) -> RetrievalEvaluationConfiguration:
        retriever_type = type(self._retriever)
        retriever_id = (
            f"{getattr(self._retriever, '__module__', retriever_type.__module__)}:"
            f"{getattr(self._retriever, '__qualname__', retriever_type.__qualname__)}"
        )
        payload: dict[str, object] = {
            "retriever_id": retriever_id,
            "requested_k": self._cutoffs[-1],
            "configuration_complete": False,
        }
        describe = getattr(self._retriever, "evaluation_configuration", None)
        if callable(describe):
            try:
                described = describe(self._cutoffs[-1])
                if inspect.isawaitable(described):
                    described = await cast(Awaitable[object], described)
                if not isinstance(described, Mapping):
                    raise TypeError("retriever evaluation_configuration must return a mapping")
                payload.update(described)
                reported_release = described.get("corpus_release_id")
                reported_hash = described.get("corpus_sha256")
                if reported_release is not None and str(reported_release) != corpus.release_id:
                    raise ValueError("retriever corpus release does not match the evaluated corpus")
                if reported_hash is not None and str(reported_hash) != corpus.corpus_sha256:
                    raise ValueError("retriever corpus hash does not match the evaluated corpus")
                payload["configuration_complete"] = True
            except Exception as exc:
                payload["configuration_error"] = f"{type(exc).__name__}: {exc}"
        else:
            payload["configuration_error"] = (
                "retriever does not expose evaluation_configuration(requested_k)"
            )
        return RetrievalEvaluationConfiguration.model_validate(payload)


def _merge_normalized_hits(
    result_sets: Sequence[Sequence[ConsumerLegalRetrievalHit]],
) -> list[ConsumerLegalRetrievalHit]:
    best: dict[str, ConsumerLegalRetrievalHit] = {}
    for hits in result_sets:
        for hit in hits:
            current = best.get(hit.retrieval_id)
            if current is None or hit.score > current.score:
                best[hit.retrieval_id] = hit
    return sorted(best.values(), key=lambda hit: (-hit.score, hit.retrieval_id))


def _query_hashes(queries: Sequence[str]) -> tuple[str, ...]:
    return tuple(hashlib.sha256(query.encode("utf-8")).hexdigest() for query in queries)


def _failed_case_metrics(
    case: ConsumerLegalGoldenCase,
    cutoffs: Sequence[int],
) -> list[MetricResult]:
    metrics: list[MetricResult] = []
    for k in cutoffs:
        metrics.append(
            MetricResult(
                name=f"consumer_retrieval_success@{k}",
                score=0.0,
                details="retrieval failed",
            )
        )
        for name in (
            "consumer_hard_negative_rate",
            "consumer_inactive_provision_rate",
            "consumer_unknown_status_rate",
        ):
            metrics.append(
                MetricResult(
                    name=f"{name}@{k}",
                    score=1.0,
                    direction="lower_is_better",
                    details="retrieval failed; safety quality is fail-closed",
                )
            )
        if case.no_applicable_ground:
            metrics.append(
                MetricResult(
                    name=f"consumer_abstention@{k}",
                    score=0.0,
                    details="retrieval failure is not a valid abstention",
                )
            )
            continue
        for name in (
            "consumer_recall",
            "consumer_article_recall",
            "consumer_mrr",
            "consumer_ndcg",
            "consumer_subdivision_precision",
            "consumer_article_precision",
        ):
            metrics.append(
                MetricResult(
                    name=f"{name}@{k}",
                    score=0.0,
                    details="retrieval failed",
                )
            )
    return metrics


def _empty_retriever(_query: str, _k: int) -> list[ConsumerLegalRetrievalHit]:
    """Deterministic abstaining baseline selected explicitly from the CLI."""
    return []


def _import_retriever(spec: str) -> ConsumerRetriever:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("retriever must use the format 'python.module:callable'")
    candidate = getattr(importlib.import_module(module_name), attribute)
    if not callable(candidate):
        raise TypeError(f"{spec} is not callable")
    return cast(ConsumerRetriever, candidate)


async def _cli() -> None:
    from app.core.logging import configure_logging

    configure_logging(level="WARNING")
    parser = argparse.ArgumentParser(
        description="Evaluate any callable against the Consumer legal-retrieval seed."
    )
    parser.add_argument(
        "dataset",
        nargs="?",
        default="eval_data/consumer_legal_retrieval",
        help="dataset.json or its parent directory",
    )
    parser.add_argument(
        "--retriever",
        help=(
            "import path 'python.module:callable'; callable receives (query, k). "
            "The default is the built-in offline mock+BM25 hybrid baseline."
        ),
    )
    parser.add_argument(
        "--empty-baseline",
        action="store_true",
        help="evaluate explicit abstention instead of running retrieval",
    )
    parser.add_argument("--output", help="optional JSON output path")
    parser.add_argument(
        "--min",
        dest="minimums",
        action="append",
        default=[],
        type=_threshold,
        metavar="METRIC=VALUE",
        help="fail if a metric average falls below VALUE (repeatable)",
    )
    parser.add_argument(
        "--max",
        dest="maximums",
        action="append",
        default=[],
        type=_threshold,
        metavar="METRIC=VALUE",
        help="fail if a metric average rises above VALUE (repeatable)",
    )
    args = parser.parse_args()

    if args.retriever and args.empty_baseline:
        parser.error("--retriever and --empty-baseline are mutually exclusive")
    if args.retriever:
        retriever = _import_retriever(args.retriever)
    elif args.empty_baseline:
        retriever = _empty_retriever
    else:
        from app.evaluation.consumer_retrievers import offline_hybrid_retriever

        retriever = offline_hybrid_retriever
    dataset = load_consumer_legal_dataset(Path(args.dataset))
    summary = await ConsumerLegalRetrievalEvaluator(retriever).run(dataset)
    rendered = summary.model_dump_json(indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    if summary.failed_case_count:
        raise SystemExit(2)
    violations = check_consumer_gates(
        summary,
        minimums=args.minimums,
        maximums=args.maximums,
    )
    if violations:
        print("Consumer evaluation gate failures:")
        for violation in violations:
            print(f"  FAIL {violation}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_cli())
