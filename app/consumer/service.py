"""Consumer case application service.

Facts remain user allegations, evidence is scanned before RAG, legal
authorities come only from the reviewed corpus, and the final artifact is
assembled deterministically.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

from app.consumer.intake import (
    extract_explicit_facts,
    merge_explicit_facts,
    next_assistant_message,
    recommended_documents,
)
from app.consumer.composer import (
    DeterministicNoticeComposer,
    NoticeComposition,
    NoticeDraftComposer,
    NoticeProse,
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
from app.consumer.store import ConsumerCaseRecord, ConsumerCaseStore, StoredEvidence
from app.core.logging import get_logger
from app.ingestion.service import DocumentIngestionService
from app.rag.pipeline import RagPipeline
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.schemas.rag import RetrievedChunk
from app.schemas.security import SecurityAction
from app.schemas.trace import AgentStatus, RetrievalTrace
from app.security.prompt_injection import PromptInjectionDetector
from app.security.sanitization import sanitized_document

logger = get_logger(__name__)

CONSUMER_NOTICE_WARNING = (
    "Rascunho informativo para revisão humana. Não é petição judicial nem substitui "
    "orientação jurídica individualizada. Confira fatos, documentos, destinatário e prazos."
)

# A notice cites law; a weakly ranked article is worse than a shorter notice.
# Only the strongest merged hits are eligible, each hit must stay within reach
# of the best one, and the score-type-aware gate in legal_policy must show
# dense/lexical agreement (or cross-query corroboration for non-RRF scores).
# Unlike the former relative-only floor, an arbitrary low-scoring top hit can
# no longer become authority merely because every other hit is even weaker.
MAX_GROUND_CANDIDATES = 8
MIN_GROUND_SCORE_RATIO = 0.5
MAX_LEGAL_GROUNDS = 8

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
    ) -> None:
        self._ingestion = ingestion
        self._detector = detector
        self._rag = rag
        self._store = store or ConsumerCaseStore()
        self._legal_corpus = legal_corpus or get_default_legal_corpus()
        self._settlement = settlement_calculator or SettlementCalculator()
        self._notice_composer = notice_composer or DeterministicNoticeComposer()
        self._legal_index_lock = asyncio.Lock()
        self._legal_indexed = False

    def create_case(self) -> tuple[ConsumerCaseSnapshot, str, str]:
        record, token = self._store.create()
        greeting = (
            "Conte o que aconteceu com a empresa, fornecedor ou instituição, incluindo "
            "datas, valores e tentativas anteriores de solução. Suas mensagens serão "
            "tratadas como alegações até você confirmar os fatos e enviar documentos."
        )
        record.messages.append(
            ConsumerMessage(role=ConsumerMessageRole.ASSISTANT, content=greeting)
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
        record.messages.append(user_message)
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
        record.messages.append(
            ConsumerMessage(role=ConsumerMessageRole.ASSISTANT, content=assistant)
        )
        if client_message_id:
            record.idempotent_messages[client_message_id] = assistant
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
        source_sha256 = _sha256_file(path)
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
        document = await self._ingestion.ingest(path, require_text=True)
        content_sha256 = hashlib.sha256(document.full_text.encode()).hexdigest()
        assessment, _ = await self._detector.scan(document)
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
        record.messages.append(
            ConsumerMessage(role=ConsumerMessageRole.ASSISTANT, content=assistant)
        )
        return self._snapshot(record), public

    async def generate_notice(self, case_id: str, token: str) -> ConsumerNotice:
        generation_started = perf_counter()
        record = self._store.get_authorized(case_id, token)
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
            (legal_results, legal_traces), (evidence_results, evidence_traces) = (
                await asyncio.gather(
                    self._rag.retrieve_many_with_traces(
                        legal_queries,
                        doc_id=self._legal_corpus.as_parsed_document().doc_id,
                        agent="consumer_legal_authorities",
                        k=8,
                        mode="hybrid",
                    ),
                    self._rag.retrieve_many_with_traces(
                        evidence_queries,
                        doc_id=evidence_document.doc_id,
                        agent="consumer_case_evidence",
                        k=6,
                        mode="hybrid",
                    ),
                )
            )
        except Exception as exc:
            raise ConsumerRetrievalError(
                "required retrieval failed; no unsupported notice was generated"
            ) from exc
        retrieval_ms = (perf_counter() - retrieval_started) * 1000

        legal_grounds = self._legal_grounds(
            legal_results,
            record.facts,
            legal_traces,
        )
        evidence_references = self._evidence_references(evidence_results, page_sources)
        if not legal_grounds or not evidence_references:
            raise ConsumerRetrievalError(
                "retrieval returned insufficient grounded support for a notice"
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
            warnings=[CONSUMER_NOTICE_WARNING, *composition_warnings],
        )
        record.notice = notice
        record.touch()
        logger.info(
            "consumer_notice_generated",
            case_id=record.case_id,
            **notice.generation_timing.model_dump(),
        )
        return notice

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
            logger.warning(
                "consumer_notice_composer_fallback",
                error_type=type(exc).__name__,
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

    async def purge_orphaned_documents(self) -> int:
        """Delete evidence vectors whose owning case no longer exists.

        Case records live in process memory while the vector store persists,
        so a restart strands previously indexed evidence. Run at startup.
        """
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
                heading = (
                    f"CASO {record.case_id[:8]} EVIDENCIA "
                    f"{evidence.public.evidence_id[:8]} PAGINA {original.number}"
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
            source = page_sources.get(result.chunk.page_start)
            if source is None:
                continue
            evidence, original_page = source
            quote = _clean_chunk_quote(result.chunk.text)
            if not quote:
                continue
            citations.append(
                EvidenceCitation(
                    evidence_id=evidence.public.evidence_id,
                    filename=evidence.public.filename,
                    page=original_page,
                    quote=quote,
                    chunk_id=result.chunk.chunk_id,
                    content_sha256=hashlib.sha256(result.chunk.text.encode("utf-8")).hexdigest(),
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
    lines = text.splitlines()
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        lines = lines[1:]
    return " ".join(" ".join(lines).split())[:700]


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
    protocols = ", ".join(facts.prior_protocols) or "nenhum protocolo informado"
    lines = [
        "# NOTIFICAÇÃO EXTRAJUDICIAL COM PROPOSTA DE ACORDO",
        "",
        f"**Notificante:** {name}",
        f"**Notificada:** {supplier}",
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
    for item in evidence:
        lines.append(
            f"- **{item.filename}, p. {item.page}** — {item.quote} (chunk `{item.chunk_id}`)"
        )
    lines.extend(["", "## 4. Fundamentos jurídicos", ""])
    if prose is not None:
        lines.extend([prose.legal_transition, ""])
    for ground in legal_grounds:
        authority = ground.authority
        official_excerpt = _bounded_legal_excerpt(
            authority.official_excerpt or authority.official_text
        )
        unit_suffix = f", {authority.unit_label}" if authority.unit_label else ""
        source_hash = (
            authority.official_excerpt_sha256
            or authority.official_text_sha256
            or authority.content_sha256
        )
        lines.append(
            f"- **[{authority.citation_label}{unit_suffix}]({authority.official_url})** — "
            f"{official_excerpt or authority.summary} Aplicação: "
            f"{ground.application_to_facts} "
            f"(corpus `{authority.corpus_release_id}`, SHA-256 `{source_hash}`)"
        )
    lines.extend(["", "## 5. Providências solicitadas", ""])
    if prose is not None:
        lines.extend([prose.requests_transition, ""])
    lines.extend(f"- {request}" for request in requests)
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
            "",
            "---",
            "",
            f"> {CONSUMER_NOTICE_WARNING}",
        ]
    )
    markdown = "\n".join(lines)
    # The private reservation value is intentionally unavailable to this
    # renderer, so it cannot leak into an exported notice by accident.
    return markdown


def _brl(value: Decimal) -> str:
    rendered = f"{value:,.2f}"
    return rendered.replace(",", "_").replace(".", ",").replace("_", ".")


def _bounded_legal_excerpt(text: str | None, limit: int = 600) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rsplit(" ", 1)[0] + "…"
