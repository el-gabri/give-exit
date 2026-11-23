"""A refused notice names its reasons, says whether a retry helps and keeps its audit."""

from __future__ import annotations

import hashlib

from app.consumer.refusals import GroundingOutcome, notice_refusal
from app.consumer.retrieval import truncated_narrative_fields
from app.consumer.schemas import (
    ConsumerCaseFacts,
    NoticeGenerationMode,
    NoticeRefusal,
    NoticeRefusalReason,
)
from app.schemas.trace import RetrievalTrace

REMEDY = "Quero a troca do produto."
ACCOUNT = "A loja entregou um modelo diferente do anunciado."


def _trace(*, degraded_mode: str | None = None, agent: str = "consumer_legal_authorities") -> (
    RetrievalTrace
):
    query = "consulta"
    return RetrievalTrace(
        batch_id="batch",
        agent=agent,
        doc_id="legal-doc",
        query_index=0,
        query=query,
        query_sha256=hashlib.sha256(query.encode()).hexdigest(),
        requested_k=8,
        candidate_k=32,
        returned_count=0,
        retrieval_mode="hybrid",
        degraded_mode=degraded_mode,
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        chunking_version="test",
        score_type="rrf_score",
    )


def _outcome(**overrides: object) -> GroundingOutcome:
    values: dict[str, object] = {
        "supported_legal_chunks": 0,
        "selected_grounds": 0,
        "verified_grounds": 0,
        "evidence_required": False,
        "evidence_cited": 0,
    }
    values.update(overrides)
    return GroundingOutcome(**values)  # type: ignore[arg-type]


def _refusal(
    outcome: GroundingOutcome,
    *,
    facts: ConsumerCaseFacts | None = None,
    legal: list[RetrievalTrace] | None = None,
    evidence: list[RetrievalTrace] | None = None,
    mode: NoticeGenerationMode = NoticeGenerationMode.PROMPT,
) -> NoticeRefusal | None:
    return notice_refusal(
        case_id="case-1",
        mode=mode,
        facts=facts or ConsumerCaseFacts(complaint_summary=ACCOUNT, desired_resolution=REMEDY),
        legal_traces=[_trace()] if legal is None else legal,
        evidence_traces=evidence or [],
        outcome=outcome,
        corpus_release_id="release-1",
        corpus_sha256="a" * 64,
    )


def test_a_notice_with_the_support_it_needs_is_not_refused() -> None:
    assert _refusal(_outcome(verified_grounds=1)) is None
    assert (
        _refusal(
            _outcome(verified_grounds=1, evidence_required=True, evidence_cited=1),
            mode=NoticeGenerationMode.CASE,
        )
        is None
    )


def test_no_channel_agreement_on_a_long_complaint_without_a_remedy() -> None:
    long_account = " ".join([ACCOUNT] * 30)  # about 1,500 characters
    refusal = _refusal(_outcome(), facts=ConsumerCaseFacts(complaint_summary=long_account))

    assert refusal is not None
    assert refusal.reasons == [
        NoticeRefusalReason.NO_REQUESTED_RESOLUTION,
        NoticeRefusalReason.NARRATIVE_TRUNCATED,
        NoticeRefusalReason.NO_RETRIEVAL_AGREEMENT,
    ]
    assert refusal.retryable is False
    assert len(refusal.hints) == 3
    assert "Quero" in refusal.hints[0]
    assert f"1.050 dos {len(long_account):,}".replace(",", ".") in refusal.hints[1]
    assert "art. 35, inciso I, do CDC" in refusal.hints[2]
    assert refusal.message.startswith(
        "Não foi possível gerar o rascunho com segurança porque a recuperação não "
        "encontrou suporte verificável em fundamentos jurídicos."
    )
    assert "mesmo resultado" in refusal.message
    assert "Tente novamente" not in refusal.message


def test_a_degraded_refusal_is_retryable() -> None:
    refusal = _refusal(_outcome(), legal=[_trace(degraded_mode="lexical_only")])

    assert refusal is not None
    assert refusal.reasons == [
        NoticeRefusalReason.SEMANTIC_RETRIEVAL_UNAVAILABLE,
        NoticeRefusalReason.NO_RETRIEVAL_AGREEMENT,
    ]
    assert refusal.retryable is True
    assert "tente novamente em alguns minutos" in refusal.message
    assert refusal.retrieval_degraded_modes == ["lexical_only"]


def test_supported_chunks_that_are_not_citable_are_named_as_such() -> None:
    refusal = _refusal(_outcome(supported_legal_chunks=2))

    assert refusal is not None
    assert refusal.reasons == [NoticeRefusalReason.NO_ELIGIBLE_GROUND]


def test_grounds_the_verifier_removed_are_named_as_such() -> None:
    refusal = _refusal(_outcome(supported_legal_chunks=2, selected_grounds=2))

    assert refusal is not None
    assert refusal.reasons == [NoticeRefusalReason.GROUNDS_REMOVED_BY_VERIFIER]


def test_a_case_notice_without_an_evidence_excerpt() -> None:
    refusal = _refusal(
        _outcome(verified_grounds=2, evidence_required=True),
        mode=NoticeGenerationMode.CASE,
        evidence=[_trace(agent="consumer_case_evidence")],
    )

    assert refusal is not None
    assert refusal.reasons == [NoticeRefusalReason.NO_EVIDENCE_SUPPORT]
    assert refusal.retryable is False
    assert "trechos dos documentos enviados" in refusal.message
    assert "fundamentos jurídicos" not in refusal.message


def test_a_degraded_evidence_search_makes_an_evidence_refusal_retryable() -> None:
    refusal = _refusal(
        _outcome(evidence_required=True),
        mode=NoticeGenerationMode.CASE,
        legal=[_trace(degraded_mode="lexical_only")],
        evidence=[_trace(agent="consumer_case_evidence", degraded_mode="lexical_only")],
    )

    assert refusal is not None
    assert refusal.reasons == [
        NoticeRefusalReason.SEMANTIC_RETRIEVAL_UNAVAILABLE,
        NoticeRefusalReason.NO_RETRIEVAL_AGREEMENT,
        NoticeRefusalReason.NO_EVIDENCE_SUPPORT,
    ]
    assert len(refusal.hints) == 3
    assert refusal.retryable is True
    assert "fundamentos jurídicos e trechos dos documentos enviados" in refusal.message


def test_the_refusal_keeps_the_audit_and_the_corpus_identity() -> None:
    legal = [_trace()]
    refusal = _refusal(_outcome(), legal=legal)

    assert refusal is not None
    assert refusal.case_id == "case-1"
    assert refusal.generation_mode is NoticeGenerationMode.PROMPT
    assert refusal.retrievals == legal
    assert refusal.corpus_release_id == "release-1"
    assert refusal.legal_ground_policy_version.startswith("consumer-notice-scope-eligibility-")


def test_truncated_narrative_fields_name_what_the_narrative_cuts() -> None:
    facts = ConsumerCaseFacts(complaint_summary="a" * 1_200, desired_resolution="b " * 300)

    assert truncated_narrative_fields(facts) == {
        "complaint_summary": (1_200, 1_050),
        "desired_resolution": (599, 500),
    }
    assert truncated_narrative_fields(ConsumerCaseFacts(complaint_summary=ACCOUNT)) == {}


def test_a_long_remedy_is_named_in_the_truncation_hint() -> None:
    refusal = _refusal(
        _outcome(),
        facts=ConsumerCaseFacts(complaint_summary=ACCOUNT, desired_resolution="Quero " * 120),
    )

    assert refusal is not None
    assert refusal.reasons[0] is NoticeRefusalReason.NARRATIVE_TRUNCATED
    assert "do pedido, 500 dos 719" in refusal.hints[0]
