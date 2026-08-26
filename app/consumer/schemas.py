"""Typed contracts for the consumer-side extrajudicial-notice workflow.

Evidence citations and legal-authority citations deliberately use different
models. A document excerpt can establish a fact; it cannot become a legal
authority merely because it was retrieved by the same RAG pipeline.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import NoticeComposer
from app.llm.base import LLMCallMetadata
from app.schemas.document import ExtractionMethod
from app.schemas.security import PromptInjectionAssessment
from app.schemas.trace import RetrievalTrace


class ConsumerIssueCategory(str, Enum):
    """Consumer complaint categories supported by the guided intake."""

    UNAUTHORIZED_CHARGE = "unauthorized_charge"
    FRAUD = "fraud"
    ACCOUNT_BLOCK = "account_block"
    NEGATIVE_CREDIT_RECORD = "negative_credit_record"
    LOAN_OR_INTEREST = "loan_or_interest"
    SERVICE_FAILURE = "service_failure"
    OVER_INDEBTEDNESS = "over_indebtedness"
    OTHER = "other"


class ConsumerMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class EvidenceStatus(str, Enum):
    ACCEPTED = "accepted"
    ACCEPTED_WITH_WARNING = "accepted_with_warning"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class MonetarySourceType(str, Enum):
    CONSUMER_CONFIRMED = "consumer_confirmed"
    EVIDENCE = "evidence"


class ConsumerCaseStatus(str, Enum):
    COLLECTING_FACTS = "collecting_facts"
    COLLECTING_EVIDENCE = "collecting_evidence"
    READY_FOR_NOTICE = "ready_for_notice"
    NOTICE_GENERATED = "notice_generated"


class LegalSource(str, Enum):
    FEDERAL_CONSTITUTION = "federal_constitution"
    CONSUMER_DEFENSE_CODE = "consumer_defense_code"


class ProvisionStatus(str, Enum):
    ACTIVE = "active"
    VETOED = "vetoed"
    REVOKED = "revoked"


class LegalUnitKind(str, Enum):
    """Normative hierarchy level within one statutory article."""

    CAPUT = "caput"
    PARAGRAPH = "paragraph"
    INCISO = "inciso"
    ALINEA = "alinea"
    NORMATIVE_OTHER = "normative_other"
    PENALTY = "penalty"
    QUOTED_AMENDMENT = "quoted_amendment"
    NOTE = "note"


class LegalContentKind(str, Enum):
    """Whether cited content is an official transcription or editorial aid."""

    OFFICIAL = "official"
    EDITORIAL = "editorial"


class ConsumerMessage(BaseModel):
    role: ConsumerMessageRole
    content: str = Field(min_length=1, max_length=20_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConsumerCaseFacts(BaseModel):
    """Facts supplied or explicitly confirmed by the consumer.

    Monetary fields have different meanings and must not be silently merged.
    The primary UI collects losses and optional costs, while the negotiation
    calculator defines the proposal shown in the generated notice.
    """

    consumer_name: str | None = Field(default=None, max_length=200)
    bank_name: str | None = Field(
        default=None,
        max_length=200,
        description="Legacy API name for the supplier, company or institution",
    )
    issue_category: ConsumerIssueCategory | None = None
    complaint_summary: str | None = Field(default=None, max_length=10_000)
    incident_date_or_period: str | None = Field(default=None, max_length=500)
    prior_protocols: list[str] = Field(default_factory=list)
    direct_loss_amount: Decimal | None = Field(default=None, ge=0)
    direct_loss_reference_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=64,
        description="Optional evidence-backed candidate explicitly selected by the consumer",
    )
    improper_payment_amount: Decimal | None = Field(
        default=None,
        ge=0,
        description=(
            "Part of direct_loss_amount actually paid under an allegedly improper "
            "charge; used only for the conditional CDC art. 42 scenario"
        ),
    )
    article_42_double_repayment_requested: bool = False
    unsuccessful_scenario_cost_amount: Decimal | None = Field(
        default=None,
        ge=0,
        description=("Consumer-supplied estimate of explicit cost if no agreement is reached"),
    )
    desired_resolution: str | None = Field(default=None, max_length=2_000)
    response_deadline_business_days: int = Field(default=10, ge=1, le=60)

    @field_validator("prior_protocols")
    @classmethod
    def _clean_protocols(cls, values: list[str]) -> list[str]:
        return [value.strip()[:200] for value in values if value.strip()]

    @model_validator(mode="after")
    def _validate_improper_payment_subset(self) -> ConsumerCaseFacts:
        if (
            self.improper_payment_amount is not None
            and self.direct_loss_amount is not None
            and self.improper_payment_amount > self.direct_loss_amount
        ):
            raise ValueError("improper_payment_amount cannot exceed direct_loss_amount")
        return self

    def missing_fields(self) -> list[str]:
        """Return information needed before drafting a useful notice."""
        required = {
            "bank_name": self.bank_name,
            "consumer_name": self.consumer_name,
            "issue_category": self.issue_category,
            "complaint_summary": self.complaint_summary,
            "incident_date_or_period": self.incident_date_or_period,
            "desired_resolution": self.desired_resolution,
        }
        return [name for name, value in required.items() if value is None or value == ""]


class ConsumerIntakeExtraction(BaseModel):
    """Schema-constrained extraction result; every field is optional."""

    consumer_name: str | None = None
    bank_name: str | None = None
    issue_category: ConsumerIssueCategory | None = None
    complaint_summary: str | None = None
    incident_date_or_period: str | None = None
    prior_protocols: list[str] | None = None
    direct_loss_amount: Decimal | None = Field(default=None, ge=0)
    direct_loss_reference_id: str | None = None
    improper_payment_amount: Decimal | None = Field(default=None, ge=0)
    article_42_double_repayment_requested: bool | None = None
    unsuccessful_scenario_cost_amount: Decimal | None = Field(default=None, ge=0)
    desired_resolution: str | None = None


class EvidenceMonetaryReference(BaseModel):
    """A monetary candidate extracted from evidence, never an automatic loss."""

    reference_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    amount: Decimal = Field(gt=0)
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=300)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConsumerEvidence(BaseModel):
    evidence_id: str
    filename: str
    page_count: int = Field(ge=0)
    status: EvidenceStatus
    media_type: str = Field(pattern=r"^(application/pdf|image/(png|jpeg))$")
    extraction_method: ExtractionMethod
    ocr_applied: bool = False
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    monetary_references: list[EvidenceMonetaryReference] = Field(default_factory=list)
    security_assessment: PromptInjectionAssessment | None = None
    warnings: list[str] = Field(default_factory=list)


class EvidenceCitation(BaseModel):
    """A source-verifiable fact citation from consumer-supplied evidence."""

    evidence_id: str
    filename: str
    page: int = Field(ge=1)
    page_end: int | None = Field(default=None, ge=1)
    quote: str = Field(min_length=1, max_length=1_000)
    chunk_id: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_page_range(self) -> EvidenceCitation:
        if self.page_end is not None and self.page_end < self.page:
            raise ValueError("page_end cannot precede page")
        return self


class LegalTextUnit(BaseModel):
    """Smallest addressable normative unit kept in the official-law snapshot."""

    model_config = ConfigDict(frozen=True)

    unit_id: str = Field(pattern=r"^br-(cf|cdc)-[a-z0-9-]+$")
    kind: LegalUnitKind
    label: str = Field(min_length=1)
    text: str = Field(min_length=1)
    paragraph: str | None = None
    inciso: str | None = None
    alinea: str | None = None
    status: ProvisionStatus = ProvisionStatus.ACTIVE
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _set_and_validate_hash(self) -> LegalTextUnit:
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.content_sha256 is None:
            object.__setattr__(self, "content_sha256", expected)
        elif self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match legal unit text")
        return self


class LegalProvision(BaseModel):
    """Versioned provision with compatible summary and optional official text."""

    model_config = ConfigDict(frozen=True)

    provision_id: str = Field(pattern=r"^br-(cf|cdc)-[a-z0-9-]+$")
    source: LegalSource
    source_name: str
    article: str
    citation_label: str
    summary: str = Field(min_length=1)
    official_url: str = Field(pattern=r"^https://www\.planalto\.gov\.br/")
    tags: tuple[str, ...] = Field(default_factory=tuple)
    corpus_release_id: str
    verified_on: date
    status: ProvisionStatus = ProvisionStatus.ACTIVE
    law_id: str | None = Field(default=None, pattern=r"^br-(cf|cdc)$")
    article_key: str | None = Field(default=None, pattern=r"^[0-9]+(?:-[a-z])?$")
    title: str | None = None
    chapter: str | None = None
    section: str | None = None
    official_text: str | None = None
    official_text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    units: tuple[LegalTextUnit, ...] = Field(default_factory=tuple)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _set_and_validate_hash(self) -> LegalProvision:
        expected = hashlib.sha256(self.summary.encode("utf-8")).hexdigest()
        if self.content_sha256 is None:
            object.__setattr__(self, "content_sha256", expected)
        elif self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match summary")
        if self.law_id is None:
            object.__setattr__(
                self,
                "law_id",
                ("br-cf" if self.source is LegalSource.FEDERAL_CONSTITUTION else "br-cdc"),
            )
        if self.official_text is None:
            if self.official_text_sha256 is not None:
                raise ValueError("official_text_sha256 requires official_text")
        else:
            official_hash = hashlib.sha256(self.official_text.encode("utf-8")).hexdigest()
            if self.official_text_sha256 is None:
                object.__setattr__(self, "official_text_sha256", official_hash)
            elif self.official_text_sha256 != official_hash:
                raise ValueError("official_text_sha256 does not match official_text")
        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("legal provision contains duplicate unit ids")
        return self


class LegalAuthorityCitation(BaseModel):
    """Citation to reviewed law metadata, kept separate from case evidence."""

    model_config = ConfigDict(frozen=True)

    provision_id: str
    source_name: str
    article: str
    citation_label: str
    summary: str
    official_url: str
    corpus_release_id: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_kind: LegalContentKind
    status: ProvisionStatus = ProvisionStatus.ACTIVE
    law_id: str | None = Field(default=None, pattern=r"^br-(cf|cdc)$")
    article_key: str | None = Field(default=None, pattern=r"^[0-9]+(?:-[a-z])?$")
    title: str | None = None
    chapter: str | None = None
    section: str | None = None
    official_text: str | None = None
    official_text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    unit_id: str | None = Field(default=None, pattern=r"^br-(cf|cdc)-[a-z0-9-]+$")
    unit_label: str | None = None
    official_excerpt: str | None = None
    official_excerpt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    chunk_id: str | None = None
    retrieval_rank: int | None = Field(default=None, ge=1)
    retrieval_score: float | None = None

    @model_validator(mode="after")
    def _validate_content_hashes(self) -> LegalAuthorityCitation:
        summary_hash = hashlib.sha256(self.summary.encode("utf-8")).hexdigest()
        if self.content_sha256 != summary_hash:
            raise ValueError("content_sha256 does not match cited legal summary")

        if self.official_text is None:
            if self.official_text_sha256 is not None:
                raise ValueError("official_text_sha256 requires official_text")
        else:
            official_hash = hashlib.sha256(self.official_text.encode("utf-8")).hexdigest()
            if self.official_text_sha256 != official_hash:
                raise ValueError("official_text_sha256 does not match official_text")

        if self.official_excerpt is None:
            if self.official_excerpt_sha256 is not None:
                raise ValueError("official_excerpt_sha256 requires official_excerpt")
        else:
            excerpt_hash = hashlib.sha256(self.official_excerpt.encode("utf-8")).hexdigest()
            if self.official_excerpt_sha256 != excerpt_hash:
                raise ValueError("official_excerpt_sha256 does not match official_excerpt")

        if self.unit_id is not None and self.official_excerpt is None:
            raise ValueError("unit_id requires an official_excerpt")
        if self.content_kind is LegalContentKind.EDITORIAL and (
            self.official_text is not None or self.official_excerpt is not None
        ):
            raise ValueError("editorial citations cannot contain official text")
        if self.content_kind is LegalContentKind.OFFICIAL and (
            self.official_text is None and self.official_excerpt is None
        ):
            raise ValueError("official citations require official text")
        return self

    @classmethod
    def from_provision(
        cls,
        provision: LegalProvision,
        *,
        unit: LegalTextUnit | None = None,
        chunk_id: str | None = None,
        retrieval_rank: int | None = None,
        retrieval_score: float | None = None,
    ) -> LegalAuthorityCitation:
        if provision.content_sha256 is None:  # pragma: no cover - validator guarantees it
            raise ValueError("provision has no content hash")
        return cls(
            provision_id=provision.provision_id,
            source_name=provision.source_name,
            article=provision.article,
            citation_label=provision.citation_label,
            summary=provision.summary,
            official_url=provision.official_url,
            corpus_release_id=provision.corpus_release_id,
            content_sha256=provision.content_sha256,
            content_kind=(
                LegalContentKind.OFFICIAL
                if provision.official_text is not None or unit is not None
                else LegalContentKind.EDITORIAL
            ),
            status=unit.status if unit is not None else provision.status,
            law_id=provision.law_id,
            article_key=provision.article_key,
            title=provision.title,
            chapter=provision.chapter,
            section=provision.section,
            official_text=provision.official_text,
            official_text_sha256=provision.official_text_sha256,
            source_snapshot_sha256=provision.source_snapshot_sha256,
            unit_id=unit.unit_id if unit is not None else None,
            unit_label=unit.label if unit is not None else None,
            official_excerpt=unit.text if unit is not None else None,
            official_excerpt_sha256=(unit.content_sha256 if unit is not None else None),
            chunk_id=chunk_id,
            retrieval_rank=retrieval_rank,
            retrieval_score=retrieval_score,
        )


class LegalGround(BaseModel):
    authority: LegalAuthorityCitation
    application_to_facts: str = Field(min_length=1, max_length=4_000)


class SettlementComponentSource(BaseModel):
    """Traceable source used by one monetary calculation component."""

    source_type: MonetarySourceType
    evidence_id: str | None = None
    filename: str | None = None
    page: int | None = Field(default=None, ge=1)
    quote: str = Field(min_length=1, max_length=1_000)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    extraction_method: ExtractionMethod | None = None
    ocr_applied: bool = False


class SettlementComponent(BaseModel):
    kind: str = Field(pattern=r"^(direct_loss|conditional_article_42|downside_cost)$")
    amount: Decimal = Field(ge=0)
    included_in_public_proposal: bool
    formula: str = Field(min_length=1)
    sources: list[SettlementComponentSource] = Field(default_factory=list)


class SettlementInputs(BaseModel):
    """Explicit inputs to a deterministic, non-predictive negotiation scenario."""

    model_config = ConfigDict(extra="forbid")

    direct_loss_amount: Decimal = Field(default=Decimal("0"), ge=0)
    improper_payment_amount: Decimal = Field(default=Decimal("0"), ge=0)
    downside_cost_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Explicit cost assigned to the unsuccessful scenario",
    )
    article_42_double_repayment_supported: bool = False
    direct_loss_sources: list[SettlementComponentSource] = Field(default_factory=list)
    improper_payment_sources: list[SettlementComponentSource] = Field(default_factory=list)
    downside_cost_sources: list[SettlementComponentSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_inputs(self) -> SettlementInputs:
        if self.improper_payment_amount > self.direct_loss_amount:
            raise ValueError("improper_payment_amount cannot exceed direct_loss_amount")
        return self


class SettlementScenario(BaseModel):
    """Traceable negotiation amounts without predicted legal odds."""

    methodology_version: str
    calibrated: bool = False
    is_legal_outcome_prediction: bool = False
    direct_loss_amount: Decimal = Field(ge=0)
    improper_payment_amount: Decimal = Field(ge=0)
    downside_cost_amount: Decimal = Field(ge=0)
    unsuccessful_outcome_value: Decimal
    conditional_article_42_increment_amount: Decimal = Field(ge=0)
    low_outcome_value: Decimal = Field(ge=0)
    high_outcome_value: Decimal = Field(ge=0)
    public_proposal_amount: Decimal | None = Field(default=None, ge=0)
    private_reservation_amount: Decimal | None = Field(default=None, ge=0)
    article_42_assumption: str
    components: list[SettlementComponent] = Field(default_factory=list)
    calculation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    methodology: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class ConsumerCaseSnapshot(BaseModel):
    case_id: str
    status: ConsumerCaseStatus = ConsumerCaseStatus.COLLECTING_FACTS
    messages: list[ConsumerMessage] = Field(default_factory=list)
    facts: ConsumerCaseFacts = Field(default_factory=ConsumerCaseFacts)
    missing_fields: list[str] = Field(default_factory=list)
    recommended_documents: list[str] = Field(default_factory=list)
    documents: list[ConsumerEvidence] = Field(default_factory=list)
    ready_for_notice: bool = False
    facts_confirmed: bool = False
    notice_available: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NoticeGenerationTiming(BaseModel):
    """Latency ledger for one generation, safe to expose in its audit trail."""

    evidence_index_ms: float = Field(ge=0)
    evidence_index_reused: bool
    retrieval_ms: float = Field(ge=0)
    composition_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class ConsumerNotice(BaseModel):
    """Auditable extrajudicial notice draft, not a filed lawsuit."""

    notice_id: str
    case_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    title: str = "Notificação extrajudicial com proposta de acordo"
    addressee: str
    facts_summary: str
    evidence_references: list[EvidenceCitation] = Field(default_factory=list)
    legal_grounds: list[LegalGround] = Field(default_factory=list)
    requests: list[str] = Field(default_factory=list)
    response_deadline_business_days: int = Field(ge=1, le=60)
    settlement: SettlementScenario
    full_text: str
    corpus_release_id: str
    corpus_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    legal_ground_policy_version: str = Field(min_length=1)
    legal_ground_policy_review_status: str = Field(
        pattern=r"^(requires_legal_review|legally_reviewed)$"
    )
    composition_mode: NoticeComposer = NoticeComposer.DETERMINISTIC
    composition_metadata: LLMCallMetadata | None = None
    generation_timing: NoticeGenerationTiming
    retrievals: list[RetrievalTrace] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
