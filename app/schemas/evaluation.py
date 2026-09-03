"""Evaluation schemas."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.consumer.schemas import ConsumerIssueCategory

_LEGAL_ID_PATTERN = re.compile(r"^br-(?:cdc|cf)-art-[a-z0-9]+(?:-[a-z0-9]+)*$")

MetricDirection = Literal["higher_is_better", "lower_is_better"]


def _validate_legal_id(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if not _LEGAL_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must be a stable lowercase CDC/CF id such as "
            "'br-cdc-art-42-paragrafo-unico'"
        )
    return normalized


class MetricResult(BaseModel):
    """One metric computed for one case."""

    name: str
    score: float = Field(ge=0.0, le=1.0, description="Normalized value from 0.0 to 1.0")
    direction: MetricDirection = Field(
        default="higher_is_better",
        description="Whether a larger or smaller value represents better quality",
    )
    details: str = Field(default="", description="Human-readable explanation")


class RankedEvaluationRetrievalHit(BaseModel):
    """Minimal ranked output retained so a metric can be independently audited."""

    rank: int = Field(ge=1)
    retrieval_id: str
    provision_id: str
    unit_id: str | None = None
    score: float
    status: str


class CaseResult(BaseModel):
    """All metrics for one golden case."""

    case_name: str
    category: str | None = None
    slices: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    query_sha256: tuple[str, ...] = ()
    retrieved_hits: tuple[RankedEvaluationRetrievalHit, ...] = ()
    retrieval_outcome: str | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def score(self, metric_name: str) -> float | None:
        for metric in self.metrics:
            if metric.name == metric_name:
                return metric.score
        return None


class EvaluationGroupSummary(BaseModel):
    """Metric aggregates and failure coverage for one case segment."""

    case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    averages: dict[str, float] = Field(default_factory=dict)
    metric_case_counts: dict[str, int] = Field(default_factory=dict)


class RetrievalEvaluationConfiguration(BaseModel):
    """Effective retrieval settings captured for a reproducible bake-off."""

    model_config = ConfigDict(frozen=True)

    retriever_id: str
    retrieval_mode: str = "unknown"
    requested_k: int = Field(ge=1)
    candidate_k: int | None = Field(default=None, ge=1)
    candidate_multiplier: int | None = Field(default=None, ge=1)
    embedding_model: str = "unknown"
    embedding_model_revision: str | None = None
    embedding_query_instruction: str | None = None
    embedding_query_instruction_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    vector_store: str = "unknown"
    index_name: str | None = None
    index_version: str = "unknown"
    chunking_version: str | None = None
    rrf_constant: int | None = Field(default=None, ge=1)
    dense_weight: float | None = Field(default=None, gt=0)
    lexical_weight: float | None = Field(default=None, gt=0)
    reranker_model: str | None = None
    reranker_model_revision: str | None = None
    configuration_complete: bool = False
    configuration_error: str | None = None


class EvaluationRunMetadata(BaseModel):
    """Immutable dataset, corpus and model identity for one evaluation run."""

    model_config = ConfigDict(frozen=True)

    dataset_id: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_review_status: str
    corpus_release_id: str
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    query_builder_version: str
    queries_per_case: int = Field(ge=1)
    cutoffs: tuple[int, ...] = Field(min_length=1)
    retrieval: RetrievalEvaluationConfiguration


class EvaluationSummary(BaseModel):
    """Aggregate over all cases in a run."""

    cases: list[CaseResult] = Field(default_factory=list)
    averages: dict[str, float] = Field(default_factory=dict)
    metric_case_counts: dict[str, int] = Field(default_factory=dict)
    metric_directions: dict[str, MetricDirection] = Field(default_factory=dict)
    failed_case_count: int = Field(default=0, ge=0)
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    by_category: dict[str, EvaluationGroupSummary] = Field(default_factory=dict)
    by_slice: dict[str, EvaluationGroupSummary] = Field(default_factory=dict)
    run: EvaluationRunMetadata | None = None

    @classmethod
    def from_cases(
        cls,
        cases: list[CaseResult],
        *,
        run: EvaluationRunMetadata | None = None,
    ) -> EvaluationSummary:
        averages, directions, metric_case_counts = _aggregate_metrics(cases)
        failed_case_count = sum(bool(case.errors) for case in cases)
        return cls(
            cases=cases,
            averages=averages,
            metric_case_counts=metric_case_counts,
            metric_directions=directions,
            failed_case_count=failed_case_count,
            failure_rate=round(failed_case_count / len(cases), 3) if cases else 0.0,
            by_category=_group_cases(
                cases,
                lambda case: (case.category,) if case.category is not None else (),
            ),
            by_slice=_group_cases(cases, lambda case: case.slices),
            run=run,
        )


def _aggregate_metrics(
    cases: list[CaseResult],
) -> tuple[dict[str, float], dict[str, MetricDirection], dict[str, int]]:
    totals: dict[str, list[float]] = {}
    directions: dict[str, MetricDirection] = {}
    for case in cases:
        for metric in case.metrics:
            known_direction = directions.setdefault(metric.name, metric.direction)
            if known_direction != metric.direction:
                raise ValueError(f"metric {metric.name!r} has inconsistent optimization directions")
            totals.setdefault(metric.name, []).append(metric.score)
    averages = {name: round(sum(scores) / len(scores), 3) for name, scores in totals.items()}
    return averages, directions, {name: len(scores) for name, scores in totals.items()}


def _group_cases(
    cases: list[CaseResult],
    keys_for_case: Callable[[CaseResult], tuple[str, ...]],
) -> dict[str, EvaluationGroupSummary]:
    # A tiny callable boundary keeps the generic summary independent of Consumer schemas.
    grouped: dict[str, list[CaseResult]] = {}
    for case in cases:
        keys = keys_for_case(case)
        for key in keys:
            grouped.setdefault(key, []).append(case)
    output: dict[str, EvaluationGroupSummary] = {}
    for key, group_cases in sorted(grouped.items()):
        averages, _, metric_case_counts = _aggregate_metrics(group_cases)
        failed = sum(bool(case.errors) for case in group_cases)
        output[key] = EvaluationGroupSummary(
            case_count=len(group_cases),
            failed_case_count=failed,
            failure_rate=round(failed / len(group_cases), 3),
            averages=averages,
            metric_case_counts=metric_case_counts,
        )
    return output


class ConsumerLegalRelevance(BaseModel):
    """A graded article or subdivision judgment for one lay complaint."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    article_id: str = Field(description="Stable article id, e.g. br-cdc-art-42")
    unit_id: str | None = Field(
        default=None,
        description=("Optional exact subdivision id. Omit when the whole article is the judgment."),
    )
    grade: int = Field(ge=1, le=3, description="1=useful, 2=relevant, 3=essential")
    rationale: str = Field(min_length=1)

    @field_validator("article_id")
    @classmethod
    def _article_id_is_stable(cls, value: str) -> str:
        normalized = _validate_legal_id(value, field_name="article_id")
        if any(
            marker in normalized for marker in ("-caput", "-paragrafo-", "-inciso-", "-alinea-")
        ):
            raise ValueError("article_id must identify the article, not a subdivision")
        return normalized

    @field_validator("unit_id")
    @classmethod
    def _unit_id_is_stable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_legal_id(value, field_name="unit_id")

    @model_validator(mode="after")
    def _unit_belongs_to_article(self) -> ConsumerLegalRelevance:
        if self.unit_id is not None and not self.unit_id.startswith(f"{self.article_id}-"):
            raise ValueError("unit_id must be a subdivision of article_id")
        return self

    @property
    def relevance_id(self) -> str:
        """Most precise id used by exact/subdivision metrics."""
        return self.unit_id or self.article_id


class ConsumerLegalGoldenCase(BaseModel):
    """One realistic Consumer-mode legal-retrieval judgment."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    case_id: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    category: str = Field(
        pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$",
        description="Fine-grained evaluation category used for reporting and slices",
    )
    intake_category: ConsumerIssueCategory = Field(
        description="Production-valid guided-intake category used to construct queries",
    )
    slices: tuple[str, ...] = Field(
        min_length=1,
        description="Evaluation slices such as supplier:retail or wording:lay",
    )
    complaint: str = Field(min_length=20)
    desired_resolution: str = Field(min_length=3)
    relevant: tuple[ConsumerLegalRelevance, ...] = ()
    hard_negatives: tuple[str, ...] = ()
    no_applicable_ground: bool = False

    @field_validator("slices")
    @classmethod
    def _slices_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().lower() for value in values)
        if any(not value or ":" not in value for value in normalized):
            raise ValueError("slices must use non-empty 'dimension:value' labels")
        if len(set(normalized)) != len(normalized):
            raise ValueError("slices must not contain duplicate labels")
        return normalized

    @field_validator("hard_negatives")
    @classmethod
    def _hard_negative_ids_are_stable(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _validate_legal_id(value, field_name="hard_negatives") for value in values
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("hard_negatives must not contain duplicate ids")
        return normalized

    @model_validator(mode="after")
    def _judgments_are_consistent(self) -> ConsumerLegalGoldenCase:
        relevance_ids = [item.relevance_id for item in self.relevant]
        if len(set(relevance_ids)) != len(relevance_ids):
            raise ValueError("relevant judgments must not contain duplicate ids")
        overlap = set(relevance_ids) & set(self.hard_negatives)
        ancestor_overlap = {
            hard_negative
            for judgment in self.relevant
            for hard_negative in self.hard_negatives
            if hard_negative == judgment.article_id
            or (judgment.unit_id is None and hard_negative.startswith(f"{judgment.article_id}-"))
        }
        overlap.update(ancestor_overlap)
        if overlap:
            raise ValueError(
                "an id cannot be both relevant and a hard negative: " + ", ".join(sorted(overlap))
            )
        if self.no_applicable_ground and self.relevant:
            raise ValueError("no-ground cases must not have relevant judgments")
        if not self.no_applicable_ground and not self.relevant:
            raise ValueError("a case needs relevant judgments or no_applicable_ground=true")
        return self

    @property
    def retrieval_text(self) -> str:
        """Canonical lay narrative; the runner derives production-equivalent queries."""
        return f"Relato: {self.complaint}\nSolução esperada: {self.desired_resolution}"


class ConsumerLegalGoldenDataset(BaseModel):
    """Versioned Consumer-mode retrieval dataset."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    dataset_id: str = Field(pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=20)
    source_url: str
    authoring: Literal["developer_authored_seed"]
    review_status: Literal["requires_legal_review", "legally_reviewed"]
    target_corpus_release_id: str | None = None
    target_corpus_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    cases: tuple[ConsumerLegalGoldenCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> ConsumerLegalGoldenDataset:
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case_id values must be unique")
        return self

    @property
    def content_sha256(self) -> str:
        """Hash the canonical semantic payload, independent of JSON formatting."""
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConsumerLegalRetrievalHit(BaseModel):
    """Provider-neutral legal hit consumed by the Consumer evaluator."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    provision_id: str = Field(description="Stable article id")
    unit_id: str | None = Field(default=None, description="Exact subdivision id, if known")
    score: float = 0.0
    status: str = Field(
        default="unknown",
        description="active, vetoed, repealed or unknown; used for safety metrics",
    )

    @field_validator("provision_id")
    @classmethod
    def _provision_id_is_stable(cls, value: str) -> str:
        return _validate_legal_id(value, field_name="provision_id")

    @field_validator("unit_id")
    @classmethod
    def _hit_unit_id_is_stable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_legal_id(value, field_name="unit_id")

    @field_validator("status")
    @classmethod
    def _status_is_normalized(cls, value: str) -> str:
        return value.strip().lower() or "unknown"

    @model_validator(mode="after")
    def _hit_unit_belongs_to_article(self) -> ConsumerLegalRetrievalHit:
        if self.unit_id is not None and not self.unit_id.startswith(f"{self.provision_id}-"):
            raise ValueError("unit_id must be a subdivision of provision_id")
        return self

    @property
    def retrieval_id(self) -> str:
        return self.unit_id or self.provision_id
