"""The service runs the optional scope check once per complaint (spec 2026-10-04 §4.3)."""

from typing import Any

import pytest

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import ConsumerCaseFacts, NoticeGenerationMode, ScopeVerification
from app.consumer.service import (
    ConsumerCaseNotReadyError,
    ConsumerCaseService,
    ConsumerPromptNoticeError,
)
from app.core.config import ScopeVerifierMode
from app.core.hashing import sha256_hex
from app.ingestion.service import DocumentIngestionService
from app.llm.mock_client import MockLLMClient
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.security.prompt_injection import PromptInjectionDetector

IN_SCOPE = "A loja não entregou a geladeira que comprei e não devolve o dinheiro."
OUT_OF_SCOPE = "Meu vizinho bloqueia a garagem."


class _CountingVerifier:
    def __init__(self, *, remove: bool) -> None:
        self.calls: list[str] = []
        self._remove = remove

    async def verify(self, complaint: str) -> ScopeVerification:
        self.calls.append(complaint)
        return ScopeVerification(
            mode=ScopeVerifierMode.LLM,
            complaint_sha256=sha256_hex(complaint),
            verdict="not_consumer" if self._remove else "consumer",
            quote=complaint[:20] if self._remove else None,
            removed=self._remove,
        )


def _service(verifier: Any) -> ConsumerCaseService:
    return ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=RagPipeline(MockEmbeddingClient(), InMemoryVectorStore()),
        legal_corpus=get_default_legal_corpus(),
        scope_verifier=verifier,
    )


def _record(service: ConsumerCaseService, complaint: str) -> Any:
    record, _ = service._store.create()
    record.facts = ConsumerCaseFacts(
        complaint_summary=complaint, desired_resolution="Quero resolver."
    )
    return record


async def test_the_verifier_runs_only_for_deterministically_in_scope_cases() -> None:
    verifier = _CountingVerifier(remove=False)
    service = _service(verifier)

    with pytest.raises(ConsumerCaseNotReadyError):
        await service._check_scope(_record(service, OUT_OF_SCOPE), NoticeGenerationMode.CASE)
    await service._check_scope(_record(service, IN_SCOPE), NoticeGenerationMode.CASE)

    assert verifier.calls == [IN_SCOPE]


async def test_a_prompt_complaint_the_gate_rejects_is_refused_at_generation() -> None:
    # The prompt path's start check reads the whole message, generation reads
    # the account without the "Quero..." sentences; when only the account is
    # out of scope, generation must refuse with the out-of-scope message.
    verifier = _CountingVerifier(remove=False)
    service = _service(verifier)

    with pytest.raises(ConsumerPromptNoticeError, match="relação de consumo"):
        await service._check_scope(_record(service, OUT_OF_SCOPE), NoticeGenerationMode.PROMPT)
    assert verifier.calls == []


async def test_a_removal_refuses_the_case_and_is_remembered_by_readiness() -> None:
    verifier = _CountingVerifier(remove=True)
    service = _service(verifier)
    record = _record(service, IN_SCOPE)

    with pytest.raises(ConsumerCaseNotReadyError):
        await service._check_scope(record, NoticeGenerationMode.CASE)
    with pytest.raises(ConsumerCaseNotReadyError):
        await service._check_scope(record, NoticeGenerationMode.CASE)

    assert verifier.calls == [IN_SCOPE]  # stored, not asked twice
    assert record.scope_verification is not None and record.scope_verification.removed
    assert "consumer_relationship" in service._readiness_missing(record)


async def test_a_removal_in_the_prompt_path_raises_the_prompt_error() -> None:
    service = _service(_CountingVerifier(remove=True))

    with pytest.raises(ConsumerPromptNoticeError, match="relação de consumo"):
        await service._check_scope(_record(service, IN_SCOPE), NoticeGenerationMode.PROMPT)


async def test_an_edited_complaint_is_verified_again() -> None:
    verifier = _CountingVerifier(remove=True)
    service = _service(verifier)
    record = _record(service, IN_SCOPE)
    with pytest.raises(ConsumerCaseNotReadyError):
        await service._check_scope(record, NoticeGenerationMode.CASE)

    edited = "A loja não entregou o fogão que comprei e não devolve o dinheiro."
    record.facts = record.facts.model_copy(update={"complaint_summary": edited})

    assert "consumer_relationship" not in service._readiness_missing(record)
    with pytest.raises(ConsumerCaseNotReadyError):
        await service._check_scope(record, NoticeGenerationMode.CASE)
    assert verifier.calls == [IN_SCOPE, edited]
