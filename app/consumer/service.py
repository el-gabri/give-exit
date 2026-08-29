"""Consumer case application service.

Facts remain user allegations, evidence is scanned before RAG, legal
authorities come only from the reviewed corpus, and the final artifact is
assembled deterministically.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

from app.consumer.composer import (
    DeterministicNoticeComposer,
    NoticeComposition,
    NoticeDraftComposer,
    NoticeProse,
)
from app.consumer.intake import (
    extract_explicit_facts,
    merge_explicit_facts,
    next_assistant_message,
    recommended_documents,
)
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_index import (
    LegalIndexResult,
    legal_corpus_is_indexed,
    preindex_legal_corpus,
)
from app.consumer.legal_policy import (
    LEGAL_GROUND_POLICY_REVIEW_STATUS,
    LEGAL_GROUND_POLICY_VERSION,
    provision_is_eligible,
    strongly_supported_chunk_ids,
)
from app.consumer.monetary import extract_brl_mentions
from app.consumer.retrieval import (
    build_evidence_queries,
    build_legal_queries,
    infer_retrieval_category,
    is_consumer_scope,
)
from app.consumer.schemas import (
    ConsumerCaseFacts,
    ConsumerCaseSnapshot,
    ConsumerCaseStatus,
    ConsumerEvidence,
    ConsumerMessage,
    ConsumerMessageRole,
    ConsumerNotice,
    EvidenceCitation,
    EvidenceMonetaryReference,
    EvidenceStatus,
    LegalGround,
    MonetarySourceType,
    NoticeGenerationTiming,
    ProvisionStatus,
    SettlementComponentSource,
    SettlementInputs,
)
from app.consumer.settlement import SettlementCalculator
from app.consumer.store import (
    ConsumerCaseRecord,
    ConsumerCaseStore,
    StoredEvidence,
)
from app.core.logging import get_logger
from app.ingestion.service import DocumentIngestionService
from app.rag.pipeline import RagPipeline
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.schemas.rag import RetrievedChunk
from app.schemas.security import SecurityAction
from app.schemas.trace import AgentStatus, RetrievalTrace
from app.security.prompt_injection import PromptInjectionDetector, failed_scan_assessment
from app.security.sanitization import sanitized_document
from app.security.telemetry import redact_sensitive_text

logger = get_logger(__name__)

# A notice cites law; a weakly ranked article is worse than a shorter notice.
# Only the strongest merged hits are eligible, each hit must stay within reach
# of the best one, and the score-type-aware gate in legal_policy must show
# dense/lexical agreement (or cross-query corroboration for non-RRF scores).
# Unlike the former relative-only floor, an arbitrary low-scoring top hit can
# no longer become authority merely because every other hit is even weaker.
MAX_GROUND_CANDIDATES = 8
MIN_GROUND_SCORE_RATIO = 0.5
MAX_LEGAL_GROUNDS = 8
DEFAULT_MAX_DOCUMENTS_PER_CASE = 20

# Scaffolding this service injects between evidence pages so the generic
# chunker keeps them in separate sections. It identifies the case, so it must
# never survive into a citation, and it is matched here in upper case because
# that is the only form ``_combined_evidence`` emits.
_EVIDENCE_PAGE_MARKER_RE = re.compile(
    r"\[?\s*CASO\s+[0-9A-F]{8}\s+EVIDENCIA\s+[0-9A-F]{8}\s+PAGINA\s+\d+\s*\]?",
    re.IGNORECASE,
)
# Characters through which untrusted excerpt text could inject a link, raw
# HTML or a code span. An uploaded PDF is attacker-controlled input in the
# common fraud scenario, and the notice is rendered as markdown and exported
# from it. Emphasis markers (* and _) are deliberately NOT escaped: they cannot
# create a link, and escaping them turned every masked "CPF 12.***.***/0001-00"
# in real evidence into "12.\*\*\*.\*\*\*/0001-00" in the delivered document.
_MARKDOWN_INLINE_ESCAPE_RE = re.compile(r"([\\`\[\]<>])")

_CATEGORY_LABEL = {
    "unauthorized_charge": "cobrança não reconhecida ou indevida",
    "fraud": "fraude, golpe ou compra não reconhecida",
    "account_block": "bloqueio de conta, acesso ou valores",
    "negative_credit_record": "registro negativo de crédito",
    "loan_or_interest": "empréstimo, financiamento ou juros",
    "service_failure": "problema com produto ou serviço",
    "over_indebtedness": "superendividamento",
    "other": "controvérsia de consumo",
}


class ConsumerCaseNotReadyError(ValueError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("consumer case is not ready for a notice")


class ConsumerRetrievalError(RuntimeError):
    """Required evidence or authority retrieval failed."""


class ConsumerLegalCorpusNotReadyError(ConsumerRetrievalError):
    """Notice generation requires a pre-indexed versioned legal corpus."""


class ConsumerEvidenceLimitError(ValueError):
    """The case already holds as many evidence documents as it may."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"a consumer case accepts at most {limit} evidence documents")


class ConsumerCaseService:
    def __init__(
        self,
        *,
        ingestion: DocumentIngestionService,
        detector: PromptInjectionDetector,
        rag: RagPipeline,
        store: ConsumerCaseStore | None = None,
        legal_corpus: LegalCorpus | None = None,
        settlement_calculator: SettlementCalculator | None = None,
        notice_composer: NoticeDraftComposer | None = None,
        max_documents_per_case: int = DEFAULT_MAX_DOCUMENTS_PER_CASE,
        purge_orphaned_evidence: bool = True,
    ) -> None:
        self._ingestion = ingestion
        self._detector = detector
        self._rag = rag
        self._store = store or ConsumerCaseStore()
        self._legal_corpus = legal_corpus or get_default_legal_corpus()
        self._settlement = settlement_calculator or SettlementCalculator()
        self._notice_composer = notice_composer or DeterministicNoticeComposer()
        self._max_documents_per_case = max_documents_per_case
        self._purge_orphaned_evidence = purge_orphaned_evidence
        self._legal_index_lock = asyncio.Lock()
        self._legal_indexed = False

    def create_case(self) -> tuple[ConsumerCaseSnapshot, str, str]:
        record, token = self._store.create()
        greeting = (
            "Conte o que aconteceu com a empresa, fornecedor ou instituição, incluindo "
            "datas, valores e tentativas anteriores de solução. Suas mensagens serão "
            "tratadas como alegações até você confirmar os fatos e enviar documentos."
        )
        record.append_message(
            ConsumerMessage(role=ConsumerMessageRole.ASSISTANT, content=greeting),
            max_messages=self._store.max_messages_per_case,
        )
        record.touch()
        return self._snapshot(record), token, greeting

    def get_case(self, case_id: str, token: str) -> ConsumerCaseSnapshot:
        return self._snapshot(self._store.get_authorized(case_id, token))

    def add_message(
        self,
        case_id: str,
        token: str,
        text: str,
        *,
        client_message_id: str | None = None,
    ) -> tuple[ConsumerCaseSnapshot, str]:
        record = self._store.get_authorized(case_id, token)
        if client_message_id and client_message_id in record.idempotent_messages:
            return self._snapshot(record), record.idempotent_messages[client_message_id]

        user_message = ConsumerMessage(role=ConsumerMessageRole.USER, content=text.strip())
        record.append_message(user_message, max_messages=self._store.max_messages_per_case)
        extraction = extract_explicit_facts(user_message.content, record.facts)
        merged = merge_explicit_facts(record.facts, extraction)
        if merged != record.facts:
            record.facts = merged
        # Every new allegation requires a fresh human review, even when the
        # conservative extractor cannot map it into one structured field.
        record.facts_confirmed = False
        record.notice = None

        assistant = next_assistant_message(
            record.facts, has_evidence=self._has_accepted_evidence(record)
        )
        record.append_message(
            ConsumerMessage(role=ConsumerMessageRole.ASSISTANT, content=assistant),
            max_messages=self._store.max_messages_per_case,
        )
        if client_message_id:
            record.remember_assistant_reply(
                client_message_id,
                assistant,
                max_keys=self._store.max_idempotency_keys_per_case,
            )
        record.touch()
        return self._snapshot(record), assistant

    def update_facts(
        self,
        case_id: str,
        token: str,
        updates: dict[str, Any],
        *,
        facts_confirmed: bool | None = None,
    ) -> ConsumerCaseSnapshot:
        record = self._store.get_authorized(case_id, token)
        allowed = set(ConsumerCaseFacts.model_fields)
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported fact fields: {', '.join(sorted(unknown))}")
        updates = dict(updates)
        if "direct_loss_amount" in updates and "direct_loss_reference_id" not in updates:
            updates["direct_loss_reference_id"] = None
        reference_id = updates.get(
            "direct_loss_reference_id", record.facts.direct_loss_reference_id
        )
        if reference_id is not None:
            source = _find_monetary_reference(record, str(reference_id))
            amount = updates.get("direct_loss_amount", record.facts.direct_loss_amount)
            if source is None or amount is None or Decimal(str(amount)) != source[1].amount:
                raise ValueError(
                    "direct_loss_reference_id must identify a matching accepted evidence value"
                )

        payload = record.facts.model_dump()
        payload.update(updates)
        updated = ConsumerCaseFacts.model_validate(payload)
        if updated != record.facts:
            record.facts = updated
            record.notice = None
            record.facts_confirmed = False
        if facts_confirmed is not None:
            record.facts_confirmed = facts_confirmed
        record.touch()
        return self._snapshot(record)

    async def add_document(
        self, case_id: str, token: str, *, filename: str, path: Path, media_type: str
    ) -> tuple[ConsumerCaseSnapshot, ConsumerEvidence]:
        record = self._store.get_authorized(case_id, token)
        source_sha256 = await asyncio.to_thread(_sha256_file, path)
        # Deduplication, the per-case ceiling and the append are one critical
        # section: checking them outside the lock let two concurrent uploads of
        # the same file both pass and be indexed twice.
        async with record.mutation_lock:
            return await self._ingest_document(
                record,
                filename=filename,
                path=path,
                media_type=media_type,
                source_sha256=source_sha256,
            )

    async def _ingest_document(
        self,
        record: ConsumerCaseRecord,
        *,
        filename: str,
        path: Path,
        media_type: str,
        source_sha256: str,
    ) -> tuple[ConsumerCaseSnapshot, ConsumerEvidence]:
        duplicate = next(
            (
                item.public
                for item in record.documents
                if item.public.source_sha256 == source_sha256
            ),
            None,
        )
        if duplicate is not None:
            return self._snapshot(record), duplicate
        if len(record.documents) >= self._max_documents_per_case:
            raise ConsumerEvidenceLimitError(self._max_documents_per_case)
        document = await self._ingestion.ingest(path, require_text=True)
        content_sha256 = hashlib.sha256(document.full_text.encode()).hexdigest()
        try:
            assessment, _ = await self._detector.scan(document)
        except Exception as exc:
            # The deterministic scan is mandatory: evidence that could not be
            # screened must not become RAG context just because the screening
            # itself broke. Fail closed with an assessment that says so.
            logger.warning(
                "consumer_document_scan_failed",
                case_id=record.case_id,
                error_type=type(exc).__name__,
            )
            assessment = failed_scan_assessment(
                document=document,
                scan_mode=self._detector.mode,
                error=exc,
            )
        status = _evidence_status(assessment.recommended_action)
        safe = (
            sanitized_document(document, assessment)
            if assessment.recommended_action.allows_automated_analysis
            else None
        )
        warnings = [*document.warnings, *assessment.warnings]
        if assessment.detected:
            warnings.append(
                "O controle de segurança sinalizou instruções dirigidas à IA neste arquivo."
            )
        if not assessment.recommended_action.allows_automated_analysis:
            warnings.append(
                "O conteúdo não foi disponibilizado ao RAG; revise o arquivo manualmente."
            )
        evidence_id = uuid.uuid4().hex
        monetary_references = (
            _extract_monetary_references(safe, source_sha256) if safe is not None else []
        )
        public = ConsumerEvidence(
            evidence_id=evidence_id,
            filename=Path(filename).name[:255] or "evidencia",
            page_count=document.page_count,
            media_type=media_type,
            extraction_method=document.extraction_method,
            ocr_applied=document.ocr_applied,
            status=status,
            source_sha256=source_sha256,
            content_sha256=content_sha256,
            monetary_references=monetary_references,
            security_assessment=assessment,
            warnings=warnings,
        )
        record.documents.append(StoredEvidence(public=public, safe_document=safe))
        record.notice = None
        record.facts_confirmed = False
        record.touch()
        assistant = (
            f"Documento {public.filename} aceito e vinculado ao caso."
            if safe is not None
            else f"Documento {public.filename} requer revisão e não será usado automaticamente."
        )
        record.append_message(
            ConsumerMessage(role=ConsumerMessageRole.ASSISTANT, content=assistant),
            max_messages=self._store.max_messages_per_case,
        )
        return self._snapshot(record), public

    async def generate_notice(self, case_id: str, token: str) -> ConsumerNotice:
        record = self._store.get_authorized(case_id, token)
        # One notice per case at a time. Without this, two concurrent requests
        # interleave readiness checks, evidence indexing and the final write, so
        # the stored notice can mix one run's grounds with another's evidence.
        async with record.mutation_lock:
            return await self._generate_notice(record)

    async def _generate_notice(self, record: ConsumerCaseRecord) -> ConsumerNotice:
        generation_started = perf_counter()
        snapshot = self._snapshot(record)
        if not snapshot.ready_for_notice:
            raise ConsumerCaseNotReadyError(self._readiness_missing(record))

        await self._ensure_legal_corpus_indexed()
        evidence_document, page_sources = self._combined_evidence(record)
        index_started = perf_counter()
        evidence_index_reused = await self._ensure_evidence_indexed(record, evidence_document)
        evidence_index_ms = (perf_counter() - index_started) * 1000

        legal_queries = build_legal_queries(record.facts)
        evidence_queries = build_evidence_queries(record.facts)
        retrieval_started = perf_counter()
        try:
            legal_results, legal_traces, evidence_results, evidence_traces = (
                await self._retrieve_notice_support(
                    legal_queries=legal_queries,
                    evidence_queries=evidence_queries,
                    evidence_doc_id=evidence_document.doc_id,
                )
            )
        except Exception as exc:
            raise ConsumerRetrievalError(
                "required retrieval failed; no unsupported notice was generated"
            ) from exc
        retrieval_ms = (perf_counter() - retrieval_started) * 1000
        degraded_modes = sorted(
            {
                trace.degraded_mode
                for trace in (*legal_traces, *evidence_traces)
                if trace.degraded_mode
            }
        )

        legal_grounds = self._legal_grounds(
            legal_results,
            record.facts,
            legal_traces,
        )
        evidence_references = self._evidence_references(evidence_results, page_sources)
        if not legal_grounds or not evidence_references:
            missing_support: list[str] = []
            if not legal_grounds:
                missing_support.append("fundamentos jurídicos")
            if not evidence_references:
                missing_support.append("trechos dos documentos enviados")
            logger.warning(
                "consumer_grounding_insufficient",
                case_id=record.case_id,
                legal_grounds=len(legal_grounds),
                evidence_references=len(evidence_references),
                legal_retrieval_degraded=any(trace.degraded_mode for trace in legal_traces),
                evidence_retrieval_degraded=any(
                    trace.degraded_mode for trace in evidence_traces
                ),
            )
            raise ConsumerRetrievalError(
                "Não foi possível gerar o rascunho com segurança porque a recuperação "
                f"não encontrou suporte verificável em {' e '.join(missing_support)}. "
                "Tente novamente; se o problema persistir, revise a categoria e os "
                "documentos enviados."
            )
        legal_merged = _merge_results(legal_results)
        evidence_merged = _merge_results(evidence_results)
        legal_traces = _annotate_composer_selection(
            legal_traces,
            legal_merged,
            {
                ground.authority.chunk_id
                for ground in legal_grounds
                if ground.authority.chunk_id is not None
            },
        )
        evidence_traces = _annotate_composer_selection(
            evidence_traces,
            evidence_merged,
            {citation.chunk_id for citation in evidence_references},
        )

        direct_loss_sources = _direct_loss_sources(record)
        improper_payment_sources = (
            direct_loss_sources
            if (
                record.facts.direct_loss_reference_id is not None
                and record.facts.improper_payment_amount == record.facts.direct_loss_amount
            )
            else _confirmed_fact_sources(
                "valor indevidamente pago",
                record.facts.improper_payment_amount,
                record.facts.complaint_summary,
            )
        )
        downside_cost_sources = _confirmed_fact_sources(
            "custo do cenário sem acordo",
            record.facts.unsuccessful_scenario_cost_amount,
            record.facts.complaint_summary,
        )
        settlement = self._settlement.calculate(
            SettlementInputs(
                direct_loss_amount=record.facts.direct_loss_amount or Decimal("0"),
                improper_payment_amount=(record.facts.improper_payment_amount or Decimal("0")),
                downside_cost_amount=(
                    record.facts.unsuccessful_scenario_cost_amount or Decimal("0")
                ),
                article_42_double_repayment_supported=(
                    record.facts.article_42_double_repayment_requested
                    and (record.facts.improper_payment_amount or Decimal("0")) > 0
                ),
                direct_loss_sources=direct_loss_sources,
                improper_payment_sources=improper_payment_sources,
                downside_cost_sources=downside_cost_sources,
            )
        )
        requests = _requests(record.facts)
        composition_started = perf_counter()
        composition, composition_warnings = await self._compose_notice_prose(
            facts=record.facts,
            evidence=evidence_references,
            legal_grounds=legal_grounds,
            requests=requests,
            public_proposal=settlement.public_proposal_amount,
        )
        composition_ms = (perf_counter() - composition_started) * 1000
        full_text = _render_notice_markdown(
            facts=record.facts,
            evidence=evidence_references,
            legal_grounds=legal_grounds,
            requests=requests,
            public_proposal=settlement.public_proposal_amount,
            prose=composition.prose,
        )
        notice = ConsumerNotice(
            notice_id=uuid.uuid4().hex,
            case_id=record.case_id,
            addressee=record.facts.bank_name or "[EMPRESA, FORNECEDOR OU INSTITUIÇÃO]",
            facts_summary=record.facts.complaint_summary or "",
            evidence_references=evidence_references,
            legal_grounds=legal_grounds,
            requests=requests,
            response_deadline_business_days=record.facts.response_deadline_business_days,
            settlement=settlement,
            full_text=full_text,
            corpus_release_id=self._legal_corpus.release_id,
            corpus_sha256=self._legal_corpus.corpus_sha256,
            legal_ground_policy_version=LEGAL_GROUND_POLICY_VERSION,
            legal_ground_policy_review_status=LEGAL_GROUND_POLICY_REVIEW_STATUS,
            composition_mode=composition.mode,
            composition_metadata=composition.metadata,
            generation_timing=NoticeGenerationTiming(
                evidence_index_ms=evidence_index_ms,
                evidence_index_reused=evidence_index_reused,
                retrieval_ms=retrieval_ms,
                composition_ms=composition_ms,
                total_ms=(perf_counter() - generation_started) * 1000,
            ),
            retrievals=[*legal_traces, *evidence_traces],
            retrieval_degraded_modes=degraded_modes,
            warnings=[*composition_warnings, *_degraded_retrieval_warnings(degraded_modes)],
        )
        record.notice = notice
        record.touch()
        logger.info(
            "consumer_notice_generated",
            case_id=record.case_id,
            **notice.generation_timing.model_dump(),
        )
        return notice

    async def _retrieve_notice_support(
        self,
        *,
        legal_queries: list[str],
        evidence_queries: list[str],
        evidence_doc_id: str,
    ) -> tuple[
        list[list[RetrievedChunk]],
        list[RetrievalTrace],
        list[list[RetrievedChunk]],
        list[RetrievalTrace],
    ]:
        """Retrieve both source sets without making them compete for one model slot."""

        legal_results, legal_traces = await self._rag.retrieve_many_with_traces(
            legal_queries,
            doc_id=self._legal_corpus.as_parsed_document().doc_id,
            agent="consumer_legal_authorities",
            k=8,
            mode="hybrid",
        )
        evidence_results, evidence_traces = await self._rag.retrieve_many_with_traces(
            evidence_queries,
            doc_id=evidence_doc_id,
            agent="consumer_case_evidence",
            k=6,
            mode="hybrid",
        )
        return legal_results, legal_traces, evidence_results, evidence_traces

    async def _ensure_evidence_indexed(
        self, record: ConsumerCaseRecord, evidence_document: ParsedDocument
    ) -> bool:
        """Index a changed evidence set once; return whether a cached index was reused."""
        async with record.evidence_index_lock:
            current_id = evidence_document.doc_id
            if (
                record.active_evidence_document_id == current_id
                and current_id in record.indexed_document_ids
            ):
                logger.info(
                    "consumer_evidence_index_reused",
                    case_id=record.case_id,
                    doc_id=current_id,
                )
                return True

            chunks = await self._rag.index_document(evidence_document)
            if not chunks:
                raise ConsumerRetrievalError("accepted evidence produced no retrievable text")

            previous_id = record.active_evidence_document_id
            record.active_evidence_document_id = current_id
            record.indexed_document_ids.add(current_id)
            if previous_id is not None and previous_id != current_id:
                await self._rag.delete_document(previous_id)
                record.indexed_document_ids.discard(previous_id)
            logger.info(
                "consumer_evidence_indexed",
                case_id=record.case_id,
                doc_id=current_id,
                chunks=len(chunks),
            )
            return False

    async def _compose_notice_prose(
        self,
        *,
        facts: ConsumerCaseFacts,
        evidence: list[EvidenceCitation],
        legal_grounds: list[LegalGround],
        requests: list[str],
        public_proposal: Decimal | None,
    ) -> tuple[NoticeComposition, list[str]]:
        """Degrade safely when the optional prose provider is unavailable."""
        try:
            composition = await self._notice_composer.compose(
                facts=facts,
                evidence=evidence,
                legal_grounds=legal_grounds,
                requests=requests,
                public_proposal=public_proposal,
            )
            return composition, []
        except Exception as exc:
            # The type alone cannot distinguish a rejected schema from an
            # unavailable model or an output-token ceiling consumed by
            # reasoning, and all three surface to the consumer as the same
            # sentence. Keep the provider's message, redacted, so a
            # misconfiguration is diagnosable without reproducing it.
            logger.warning(
                "consumer_notice_composer_fallback",
                error_type=type(exc).__name__,
                error=redact_sensitive_text(exc)[:500],
            )
            fallback = await DeterministicNoticeComposer().compose(
                facts=facts,
                evidence=evidence,
                legal_grounds=legal_grounds,
                requests=requests,
                public_proposal=public_proposal,
            )
            return fallback, [
                "A composição por IA não esteve disponível; o rascunho foi montado "
                "deterministicamente."
            ]

    def get_notice(self, case_id: str, token: str) -> ConsumerNotice:
        record = self._store.get_authorized(case_id, token)
        if record.notice is None:
            raise ConsumerCaseNotReadyError(["notice_not_generated"])
        return record.notice

    async def delete_case(self, case_id: str, token: str) -> None:
        record = self._store.delete_authorized(case_id, token)
        for doc_id in record.indexed_document_ids:
            await self._rag.delete_document(doc_id)

    async def purge_expired_cases(self) -> int:
        """Drop idle cases and release the evidence vectors they still own.

        Cases are in-process, so nothing else reclaims their memory or their
        rows in the shared vector store once a consumer walks away.
        """
        expired = self._store.expire_idle_cases()
        for record in expired:
            for doc_id in record.indexed_document_ids:
                await self._rag.delete_document(doc_id)
        if expired:
            logger.info("consumer_expired_cases_purged", cases=len(expired))
        return len(expired)

    async def purge_orphaned_documents(self) -> int:
        """Delete evidence vectors whose owning case no longer exists.

        Case records live in process memory while the vector store persists,
        so a restart strands previously indexed evidence. Run at startup.

        This is only correct when this process owns the vector-store namespace
        outright. With several workers, replicas or a shared PostgreSQL
        namespace, every startup would delete the other processes' live case
        evidence, so those deployments disable it through settings.
        """
        if not self._purge_orphaned_evidence:
            logger.info("consumer_orphan_purge_disabled")
            return 0
        try:
            indexed = await self._rag.list_document_ids()
        except TypeError:
            return 0
        keep = {self._legal_corpus.as_parsed_document().doc_id}
        keep |= self._store.indexed_document_ids()
        orphans = sorted(indexed - keep)
        for doc_id in orphans:
            await self._rag.delete_document(doc_id)
        if orphans:
            logger.info("consumer_orphan_documents_purged", documents=len(orphans))
        return len(orphans)

    async def legal_corpus_ready(self) -> bool:
        """Refresh readiness without loading or invoking the embedding model."""

        if self._legal_indexed:
            return True
        async with self._legal_index_lock:
            if not self._legal_indexed:
                self._legal_indexed = await legal_corpus_is_indexed(
                    self._rag,
                    self._legal_corpus,
                )
            return self._legal_indexed

    async def prepare_legal_corpus(self, *, force: bool = False) -> LegalIndexResult:
        """Materialize the legal index outside a notice-generation request."""

        async with self._legal_index_lock:
            result = await preindex_legal_corpus(
                self._rag,
                self._legal_corpus,
                force=force,
            )
            self._legal_indexed = True
            return result

    async def _ensure_legal_corpus_indexed(self) -> None:
        if await self.legal_corpus_ready():
            return
        raise ConsumerLegalCorpusNotReadyError(
            "A base legal do modelo configurado ainda não foi pré-indexada. "
            "Execute `python -m app.consumer.preindex_legal` e reinicie a API."
        )

    def _snapshot(self, record: ConsumerCaseRecord) -> ConsumerCaseSnapshot:
        missing = record.facts.missing_fields()
        readiness_missing = self._readiness_missing(record)
        ready = not readiness_missing
        if "consumer_relationship" in readiness_missing:
            missing.append("consumer_relationship")
        if record.notice is not None:
            status = ConsumerCaseStatus.NOTICE_GENERATED
        elif ready:
            status = ConsumerCaseStatus.READY_FOR_NOTICE
        elif missing:
            status = ConsumerCaseStatus.COLLECTING_FACTS
        else:
            status = ConsumerCaseStatus.COLLECTING_EVIDENCE
        return ConsumerCaseSnapshot(
            case_id=record.case_id,
            status=status,
            messages=record.messages,
            facts=record.facts,
            missing_fields=missing,
            recommended_documents=recommended_documents(record.facts.issue_category),
            documents=[item.public for item in record.documents],
            ready_for_notice=ready,
            facts_confirmed=record.facts_confirmed,
            notice_available=record.notice is not None,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _readiness_missing(self, record: ConsumerCaseRecord) -> list[str]:
        missing = list(record.facts.missing_fields())
        category = (
            record.facts.issue_category.value
            if record.facts.issue_category is not None
            else "other"
        )
        if not is_consumer_scope(
            category=category,
            complaint=record.facts.complaint_summary or "",
        ):
            missing.append("consumer_relationship")
        if not self._has_accepted_evidence(record):
            missing.append("accepted_evidence")
        if not record.facts_confirmed:
            missing.append("facts_confirmation")
        return missing

    @staticmethod
    def _accepted_documents(record: ConsumerCaseRecord) -> list[StoredEvidence]:
        return [item for item in record.documents if item.safe_document is not None]

    def _has_accepted_evidence(self, record: ConsumerCaseRecord) -> bool:
        return bool(self._accepted_documents(record))

    def _combined_evidence(
        self, record: ConsumerCaseRecord
    ) -> tuple[ParsedDocument, dict[int, tuple[StoredEvidence, int]]]:
        pages: list[DocumentPage] = []
        sources: dict[int, tuple[StoredEvidence, int]] = {}
        for evidence in self._accepted_documents(record):
            assert evidence.safe_document is not None
            for original in evidence.safe_document.pages:
                global_page = len(pages) + 1
                # The marker must satisfy SectionAwareChunker.is_heading, which
                # requires ~all letters to be upper case. uuid4 hex carries
                # lower-case a-f, so an unmodified id made this a heading only
                # ~3% of the time; the rest of the time every evidence page fell
                # into one section and chunks packed text across two different
                # uploaded files under a single page number. Upper case makes
                # detection deterministic, and _evidence_references still
                # refuses to cite any chunk that spans pages.
                heading = (
                    f"CASO {record.case_id[:8].upper()} EVIDENCIA "
                    f"{evidence.public.evidence_id[:8].upper()} PAGINA {original.number}"
                )
                pages.append(DocumentPage(number=global_page, text=f"{heading}\n\n{original.text}"))
                sources[global_page] = (evidence, original.number)
        return (
            ParsedDocument(
                filename=f"consumer_case_{record.case_id}_evidence.pdf",
                pages=pages,
                language="pt",
                extraction_method=ExtractionMethod.NATIVE_TEXT,
                warnings=[],
            ),
            sources,
        )

    def _legal_grounds(
        self,
        result_sets: list[list[RetrievedChunk]],
        facts: ConsumerCaseFacts,
        traces: list[RetrievalTrace] | None = None,
    ) -> list[LegalGround]:
        category = facts.issue_category.value if facts.issue_category else "other"
        if not is_consumer_scope(
            category=category,
            complaint=facts.complaint_summary or "",
        ):
            return []
        inferred_category = infer_retrieval_category(
            category,
            facts.complaint_summary or "",
        )
        strongly_supported = strongly_supported_chunk_ids(traces or [])
        if not strongly_supported:
            return []
        merged = _merge_results(result_sets)
        if not merged:
            return []
        score_floor = merged[0].score * MIN_GROUND_SCORE_RATIO
        candidates = [
            item
            for item in merged[:MAX_GROUND_CANDIDATES]
            if item.score >= score_floor
        ]
        grounds: list[LegalGround] = []
        seen: set[str] = set()
        issue = _CATEGORY_LABEL[facts.issue_category.value if facts.issue_category else "other"]
        for rank, result in enumerate(candidates, start=1):
            for provision in self._legal_corpus.provisions_for_chunk(result):
                if provision.status is not ProvisionStatus.ACTIVE:
                    continue
                if result.chunk.chunk_id not in strongly_supported:
                    continue
                if not provision_is_eligible(inferred_category, provision.provision_id):
                    continue
                unit = self._legal_corpus.unit_for_chunk(result)
                if unit is not None and unit.status is not ProvisionStatus.ACTIVE:
                    continue
                if provision.provision_id in seen:
                    continue
                seen.add(provision.provision_id)
                authority = self._legal_corpus.authority_for_chunk(
                    result,
                    retrieval_rank=rank,
                )
                grounds.append(
                    LegalGround(
                        authority=authority,
                        application_to_facts=(
                            f"O texto oficial em {provision.citation_label} foi localizado "
                            f"pela política de recuperação para {issue}. Sua aplicabilidade "
                            "ao caso não foi decidida pelo sistema e deve ser validada por "
                            "profissional habilitado contra os fatos e documentos citados."
                        ),
                    )
                )
                if len(grounds) >= MAX_LEGAL_GROUNDS:
                    return grounds
        return grounds

    @staticmethod
    def _evidence_references(
        result_sets: list[list[RetrievedChunk]],
        page_sources: dict[int, tuple[StoredEvidence, int]],
    ) -> list[EvidenceCitation]:
        citations: list[EvidenceCitation] = []
        for result in _merge_results(result_sets):
            chunk = result.chunk
            # A citation names exactly one file and one page. A chunk that packed
            # text from several evidence pages cannot be attributed truthfully to
            # any of them, so it is dropped rather than quoted under the first
            # page's filename. A shorter notice is recoverable; a notice that
            # puts one document's words under another document's name is not.
            if chunk.page_start != chunk.page_end:
                logger.warning(
                    "consumer_evidence_citation_spans_pages",
                    chunk_id=chunk.chunk_id,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                )
                continue
            source = page_sources.get(chunk.page_start)
            if source is None:
                continue
            evidence, original_page = source
            quote = _clean_chunk_quote(chunk.text)
            if not quote:
                continue
            citations.append(
                EvidenceCitation(
                    evidence_id=evidence.public.evidence_id,
                    filename=evidence.public.filename,
                    page=original_page,
                    quote=quote,
                    chunk_id=chunk.chunk_id,
                    content_sha256=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                )
            )
            if len(citations) >= 8:
                break
        return citations


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_monetary_references(
    document: ParsedDocument,
    source_sha256: str,
) -> list[EvidenceMonetaryReference]:
    references: list[EvidenceMonetaryReference] = []
    for page in document.pages:
        for mention in extract_brl_mentions(page.text):
            material = f"{source_sha256}:{page.number}:{mention.amount}:{mention.quote_sha256}"
            references.append(
                EvidenceMonetaryReference(
                    reference_id=hashlib.sha256(material.encode("utf-8")).hexdigest()[:24],
                    amount=mention.amount,
                    page=page.number,
                    quote=mention.quote,
                    quote_sha256=mention.quote_sha256,
                )
            )
            if len(references) >= 50:
                return references
    return references


def _find_monetary_reference(
    record: ConsumerCaseRecord,
    reference_id: str,
) -> tuple[StoredEvidence, EvidenceMonetaryReference] | None:
    for evidence in record.documents:
        if evidence.safe_document is None:
            continue
        for reference in evidence.public.monetary_references:
            if reference.reference_id == reference_id:
                return evidence, reference
    return None


def _direct_loss_sources(record: ConsumerCaseRecord) -> list[SettlementComponentSource]:
    reference_id = record.facts.direct_loss_reference_id
    if reference_id is None:
        return _confirmed_fact_sources(
            "prejuízo direto",
            record.facts.direct_loss_amount,
            record.facts.complaint_summary,
        )
    found = _find_monetary_reference(record, reference_id)
    if found is None:
        raise ValueError("confirmed direct-loss evidence reference is no longer available")
    evidence, reference = found
    return [
        SettlementComponentSource(
            source_type=MonetarySourceType.EVIDENCE,
            evidence_id=evidence.public.evidence_id,
            filename=evidence.public.filename,
            page=reference.page,
            quote=reference.quote,
            quote_sha256=reference.quote_sha256,
            source_sha256=evidence.public.source_sha256,
            content_sha256=evidence.public.content_sha256,
            extraction_method=evidence.public.extraction_method,
            ocr_applied=evidence.public.ocr_applied,
        )
    ]


def _confirmed_fact_sources(
    label: str,
    amount: Decimal | None,
    complaint_summary: str | None,
) -> list[SettlementComponentSource]:
    if amount is None or amount <= 0:
        return []
    summary = " ".join((complaint_summary or "").split())[:500]
    source_excerpt = f"{label}: R$ {amount}; relato confirmado: {summary or 'não informado'}"
    return [
        SettlementComponentSource(
            source_type=MonetarySourceType.CONSUMER_CONFIRMED,
            quote=source_excerpt,
            quote_sha256=hashlib.sha256(source_excerpt.encode("utf-8")).hexdigest(),
        )
    ]


def _evidence_status(action: SecurityAction) -> EvidenceStatus:
    return {
        SecurityAction.PROCEED: EvidenceStatus.ACCEPTED,
        SecurityAction.PROCEED_WITH_WARNING: EvidenceStatus.ACCEPTED_WITH_WARNING,
        SecurityAction.HUMAN_REVIEW: EvidenceStatus.REVIEW_REQUIRED,
        SecurityAction.BLOCK: EvidenceStatus.BLOCKED,
    }[action]


def _merge_results(result_sets: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    best: dict[str, RetrievedChunk] = {}
    for results in result_sets:
        for result in results:
            current = best.get(result.chunk.chunk_id)
            if current is None or result.score > current.score:
                best[result.chunk.chunk_id] = result
    return sorted(best.values(), key=lambda item: (-item.score, item.chunk.chunk_id))


def _annotate_composer_selection(
    traces: list[RetrievalTrace],
    merged: list[RetrievedChunk],
    included_chunk_ids: set[str],
) -> list[RetrievalTrace]:
    """Record which ranked hits became grounded composer inputs."""
    merged_ranks = {item.chunk.chunk_id: rank for rank, item in enumerate(merged, start=1)}
    winners: dict[str, tuple[int, int, float]] = {}
    for trace_index, trace in enumerate(traces):
        for item in trace.results:
            current = winners.get(item.chunk_id)
            if current is None or item.score > current[2]:
                winners[item.chunk_id] = (trace_index, item.rank, item.score)

    annotated: list[RetrievalTrace] = []
    truncated = any(item.chunk.chunk_id not in included_chunk_ids for item in merged)
    for trace_index, trace in enumerate(traces):
        results = []
        for item in trace.results:
            winner = winners.get(item.chunk_id)
            selected = winner is not None and winner[:2] == (trace_index, item.rank)
            results.append(
                item.model_copy(
                    update={
                        "selected_for_merge": selected,
                        "merged_rank": merged_ranks.get(item.chunk_id),
                        "included_in_context": (selected and item.chunk_id in included_chunk_ids),
                    }
                )
            )
        annotated.append(
            trace.model_copy(
                update={
                    "context_truncated": truncated,
                    "agent_status": AgentStatus.SUCCESS,
                    "prompt_version": "consumer-grounded-composer:v1",
                    "results": results,
                }
            )
        )
    return annotated


def _clean_chunk_quote(text: str) -> str:
    """Return the document's own words, without this service's page marker.

    The ``CASO ... EVIDENCIA ... PAGINA n`` marker is scaffolding injected to
    keep evidence pages in separate chunker sections. It names the case, so it
    must never reach an exported notice, and it is removed wherever it appears
    rather than only as a leading section title.
    """
    without_marker = _EVIDENCE_PAGE_MARKER_RE.sub(" ", text)
    lines = without_marker.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("[") and first.endswith("]"):
        lines = lines[1:]
    return " ".join(" ".join(lines).split())[:700]


def _markdown_inline(text: str) -> str:
    """Escape untrusted text before it is embedded in the notice markdown.

    Evidence excerpts and filenames come from files someone else sent the
    consumer. Left raw, ``[clique aqui](https://...)`` inside a PDF becomes a
    live hyperlink in the notice the consumer reads, exports and forwards.
    """
    return _MARKDOWN_INLINE_ESCAPE_RE.sub(r"\\\1", text)


def _single_line(text: str) -> str:
    """Collapse whitespace so a value can safely become one Markdown line."""
    return " ".join(text.split())


def _degraded_retrieval_warnings(degraded_modes: list[str]) -> list[str]:
    """Tell the reader when a draft was built without semantic retrieval."""
    if not degraded_modes:
        return []
    return [
        "A busca semântica não estava disponível e a recuperação usou apenas "
        f"correspondência léxica ({', '.join(degraded_modes)}). Os mesmos filtros "
        "determinísticos de fundamentos e citações continuam valendo, mas revise "
        "este rascunho com atenção redobrada ou gere novamente mais tarde."
    ]


def _requests(facts: ConsumerCaseFacts) -> list[str]:
    requests = [facts.desired_resolution or "solução integral do problema relatado"]
    if facts.direct_loss_amount and facts.direct_loss_amount > 0:
        requests.append("restituição do prejuízo direto alegado, após conferência dos comprovantes")
    if facts.prior_protocols:
        requests.append("resposta escrita e fundamentada aos protocolos já registrados")
    requests.append("confirmação escrita das providências adotadas dentro do prazo indicado")
    return requests


def _render_notice_markdown(
    *,
    facts: ConsumerCaseFacts,
    evidence: list[EvidenceCitation],
    legal_grounds: list[LegalGround],
    requests: list[str],
    public_proposal: Decimal | None,
    prose: NoticeProse | None = None,
) -> str:
    name = facts.consumer_name or "[PREENCHER NOME DO(A) CONSUMIDOR(A)]"
    supplier = facts.bank_name or "[PREENCHER EMPRESA, FORNECEDOR OU INSTITUIÇÃO]"
    subject = _CATEGORY_LABEL[facts.issue_category.value if facts.issue_category else "other"]
    protocols = (
        ", ".join(_single_line(item) for item in facts.prior_protocols)
        or "nenhum protocolo informado"
    )
    lines = [
        "# NOTIFICAÇÃO EXTRAJUDICIAL COM PROPOSTA DE ACORDO",
        "",
        "**À**",
        "",
        f"{_single_line(supplier)}",
        "[PREENCHER ENDEREÇO DA NOTIFICADA]",
        "",
        f"**Notificante:** {_single_line(name)}",
        "[PREENCHER CPF E ENDEREÇO DO(A) NOTIFICANTE]",
        "",
        f"**Assunto:** {subject}",
        "",
        "## 1. Finalidade",
        "",
        (
            prose.purpose
            if prose is not None
            else "Esta notificação busca solução consensual de uma controvérsia de consumo. "
            "Não se trata de ação judicial nem de reconhecimento definitivo de responsabilidade."
        ),
        "",
        "## 2. Fatos declarados pelo(a) consumidor(a)",
        "",
        facts.complaint_summary or "[PREENCHER RELATO]",
        *(["", prose.facts_framing] if prose is not None else []),
        "",
        f"**Data ou período:** {facts.incident_date_or_period or '[PREENCHER]'}",
        f"**Protocolos anteriores:** {protocols}",
        "",
        "## 3. Documentos de suporte",
        "",
    ]
    # Retrieval identifiers (chunk id, corpus release, content hashes) stay out
    # of the delivered document. They are provenance for the consumer's own
    # review and for /consumer/cases/{id}/notice/retrievals, and every one of
    # them survives unchanged on EvidenceCitation and LegalAuthorityCitation.
    for item in evidence:
        lines.append(
            f"- **{_markdown_inline(item.filename)}, p. {item.page}** — "
            f"{_markdown_inline(item.quote)}"
        )
    lines.extend(["", "## 4. Fundamentos jurídicos", ""])
    if prose is not None:
        lines.extend([prose.legal_transition, ""])
    for ground in legal_grounds:
        authority = ground.authority
        # The official text is quoted verbatim, including its own legislative
        # annotations: trimming a statute to look tidier would make the quote
        # something other than what the source says.
        official_excerpt = _bounded_legal_excerpt(
            authority.official_excerpt or authority.official_text
        )
        unit_suffix = f", {authority.unit_label}" if authority.unit_label else ""
        # ``application_to_facts`` explains how the retrieval policy selected
        # this provision. That is reviewer-facing commentary about our own
        # software; addressed to the supplier it only undercuts the notice.
        lines.append(
            f"- **[{authority.citation_label}{unit_suffix}]({authority.official_url})** — "
            f"{official_excerpt or authority.summary}"
        )
    lines.extend(["", "## 5. Providências solicitadas", ""])
    if prose is not None:
        lines.extend([prose.requests_transition, ""])
    # A confirmed fact may span several lines; a raw newline inside a list item
    # silently drops the bullet for every line after the first.
    lines.extend(f"- {_single_line(request)}" for request in requests)
    lines.extend(["", "## 6. Proposta para composição", ""])
    if public_proposal is None:
        lines.append(
            "Neste momento, propõe-se solução não monetária nos termos dos pedidos acima, "
            "sem atribuição automática de indenização."
        )
    else:
        lines.append(
            f"Para tentativa de composição, propõe-se o valor de **R$ {_brl(public_proposal)}**, "
            "sujeito à conferência dos comprovantes e à revisão humana. O valor é uma âncora "
            "de negociação calculada por cenário, não uma previsão de decisão judicial."
        )
    lines.extend(
        [
            "",
            "## 7. Prazo e encerramento",
            "",
            *([prose.closing, ""] if prose is not None else []),
            "Solicita-se resposta escrita em até "
            f"**{facts.response_deadline_business_days} dias úteis**. "
            "A ausência de acordo não altera direitos, defesas ou prazos legais de qualquer parte.",
        ]
    )
    # Place, date and signature are left as fields to fill rather than
    # generated: the notice is sent on a day the renderer cannot know, and a
    # plausible-looking wrong date on an extrajudicial notice is worse than a
    # blank one.
    lines.extend(
        [
            "",
            "---",
            "",
            "[PREENCHER LOCAL], [PREENCHER DATA].",
            "",
            "___________________________________________",
            "",
            f"{_single_line(name)}",
            "[PREENCHER CPF DO(A) NOTIFICANTE]",
        ]
    )
    # The private reservation value is intentionally unavailable to this
    # renderer, so it cannot leak into an exported notice by accident.
    return "\n".join(lines)


def _brl(value: Decimal) -> str:
    rendered = f"{value:,.2f}"
    return rendered.replace(",", "_").replace(".", ",").replace("_", ".")


def _bounded_legal_excerpt(text: str | None, limit: int = 600) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rsplit(" ", 1)[0] + "…"
