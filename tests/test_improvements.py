"""Tests covering Tracks 1-5 improvements across scope, verifiers, security, RAG and intake."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.api.security import SlidingWindowRateLimiter
from app.consumer.composer import LLMNoticeComposer, create_notice_composer
from app.consumer.ground_selection import select_legal_grounds
from app.consumer.ground_verifier import LLMGroundVerifier, create_ground_verifier
from app.consumer.intake import extract_explicit_facts
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.notice_markdown import render_notice_markdown
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.scope import NonConsumerRelationship, assess_scope
from app.consumer.scope_verifier import LLMScopeVerifier, create_scope_verifier
from app.core.config import (
    GroundVerifierMode,
    LLMProvider,
    NoticeComposer,
    ScopeVerifierMode,
    Settings,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.resilience import EmbeddingUnavailableError, QueryEmbeddingGuard
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace


def test_verifier_settings_validate_reasoning_effort_and_wire_model_overrides() -> None:
    with pytest.raises(ValidationError, match="LITIGATION_GROUND_VERIFIER_REASONING_EFFORT"):
        Settings(ground_verifier_reasoning_effort="invalid", _env_file=None)

    with pytest.raises(ValidationError, match="LITIGATION_SCOPE_VERIFIER_REASONING_EFFORT"):
        Settings(scope_verifier_reasoning_effort="invalid", _env_file=None)

    settings = Settings(
        llm_provider=LLMProvider.ANTHROPIC,
        anthropic_api_key="sk-ant-test",
        ground_verifier=GroundVerifierMode.LLM,
        ground_verifier_model="claude-haiku-4-5",
        ground_verifier_reasoning_effort="medium",
        scope_verifier=ScopeVerifierMode.LLM,
        scope_verifier_model="claude-haiku-4-5",
        scope_verifier_reasoning_effort="high",
        _env_file=None,
    )
    gv = create_ground_verifier(settings)
    sv = create_scope_verifier(settings)

    assert isinstance(gv, LLMGroundVerifier)
    assert gv._reasoning_effort == "medium"
    assert gv._client._inner._model == "claude-haiku-4-5"  # type: ignore[attr-defined]
    assert isinstance(sv, LLMScopeVerifier)
    assert sv._reasoning_effort == "high"
    assert sv._client._inner._model == "claude-haiku-4-5"  # type: ignore[attr-defined]


async def test_verifiers_forward_reasoning_effort_when_configured() -> None:
    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks()
    caput_chunk = next(
        c for c in chunks if c.metadata.get("unit_id") == "br-cdc-art-42-caput"
    )
    results = [[RetrievedChunk(chunk=caput_chunk, score=0.030)]]
    facts = ConsumerCaseFacts(
        complaint_summary="Cobrança com ameaça de exposição no trabalho.",
        desired_resolution="Cessar ameaças.",
    )
    grounds = select_legal_grounds(
        corpus, facts, results, [], support=lambda _traces: frozenset({caput_chunk.chunk_id})
    )
    mock_settings = Settings(
        llm_provider=LLMProvider.MOCK,
        ground_verifier=GroundVerifierMode.LLM,
        ground_verifier_reasoning_effort="low",
        scope_verifier=ScopeVerifierMode.LLM,
        scope_verifier_reasoning_effort="low",
        _env_file=None,
    )
    gv = create_ground_verifier(mock_settings)
    sv = create_scope_verifier(mock_settings)

    gv_result = await gv.verify(facts, grounds)
    sv_result = await sv.verify("Comprei uma geladeira com defeito.")

    assert gv_result.summary.mode is GroundVerifierMode.LLM
    assert sv_result.mode is ScopeVerifierMode.LLM


def test_scope_gate_abstains_on_b2b_invoice_issuer_and_residential_security_deposit() -> None:
    b2b = assess_scope(
        complaint=(
            "Tenho uma pequena gráfica e imprimi cinco mil folhetos para uma rede de "
            "academias. Entreguei tudo no prazo, emiti a nota fiscal e a empresa não paga."
        )
    )
    assert not b2b.in_scope
    assert b2b.relationship is NonConsumerRelationship.COMPLAINANT_SUPPLIER

    tenancy = assess_scope(
        complaint=(
            "Aluguei um apartamento direto com o dono, sem imobiliária. Saí do imóvel "
            "e ele ainda não devolveu o depósito de caução de três aluguéis."
        )
    )
    assert not tenancy.in_scope
    assert tenancy.relationship is NonConsumerRelationship.TENANCY


def test_cdc_art_42_disambiguates_caput_from_sole_paragraph_on_harassment() -> None:
    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks()
    sole_chunk = next(
        c for c in chunks if c.metadata.get("unit_id") == "br-cdc-art-42-paragrafo-unico"
    )
    caput_chunk = next(
        c for c in chunks if c.metadata.get("unit_id") == "br-cdc-art-42-caput"
    )
    results = [
        [
            RetrievedChunk(chunk=sole_chunk, score=0.030),
            RetrievedChunk(chunk=caput_chunk, score=0.016),
        ]
    ]
    trace = RetrievalTrace(
        batch_id="b1",
        agent="consumer_legal_authorities",
        doc_id=sole_chunk.doc_id,
        query_index=0,
        query="ameaças de cobrança",
        query_sha256="a" * 64,
        requested_k=8,
        candidate_k=8,
        returned_count=2,
        retrieval_mode="hybrid",
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        chunking_version="test",
        score_type="rrf_score",
        rrf_constant=60,
        dense_weight=1.0,
        lexical_weight=1.0,
        results=[
            RetrievedItemTrace(
                rank=1,
                chunk_id=sole_chunk.chunk_id,
                doc_id=sole_chunk.doc_id,
                page_start=1,
                page_end=1,
                score=0.030,
                channel_ranks={"dense": 4, "lexical": 9},
                content_sha256="b" * 64,
            ),
            RetrievedItemTrace(
                rank=2,
                chunk_id=caput_chunk.chunk_id,
                doc_id=caput_chunk.doc_id,
                page_start=1,
                page_end=1,
                score=0.016,
                channel_ranks={"lexical": 1},
                content_sha256="c" * 64,
            ),
        ],
    )
    facts = ConsumerCaseFacts(
        complaint_summary=(
            "Estou atrasado com uma parcela e a empresa de cobrança liga para o meu "
            "trabalho e ameaça me expor nas redes sociais."
        ),
        desired_resolution="Quero que parem as ameaças.",
    )

    grounds = select_legal_grounds(corpus, facts, results, [trace])

    assert [g.authority.unit_id for g in grounds] == ["br-cdc-art-42-caput"]

    # When both sole paragraph and caput are already in candidates, caput is kept directly.
    both_supported = select_legal_grounds(
        corpus,
        facts,
        results,
        [trace],
        support=lambda _traces: frozenset({sole_chunk.chunk_id, caput_chunk.chunk_id}),
    )
    assert [g.authority.unit_id for g in both_supported] == ["br-cdc-art-42-caput"]

    # When only sole paragraph was retrieved and caput is absent from merged, art. 42 is dropped.
    sole_only = select_legal_grounds(
        corpus,
        facts,
        [[RetrievedChunk(chunk=sole_chunk, score=0.030)]],
        [trace],
        support=lambda _traces: frozenset({sole_chunk.chunk_id}),
    )
    assert sole_only == []

    # When improper_payment_amount > 0, sole paragraph is preserved.
    paid_facts = ConsumerCaseFacts(
        complaint_summary=facts.complaint_summary,
        desired_resolution=facts.desired_resolution,
        disputed_amount=Decimal("100.00"),
        improper_payment_amount=Decimal("50.00"),
    )
    paid_grounds = select_legal_grounds(
        corpus,
        paid_facts,
        [[RetrievedChunk(chunk=sole_chunk, score=0.030)]],
        [trace],
        support=lambda _traces: frozenset({sole_chunk.chunk_id}),
    )
    assert [g.authority.unit_id for g in paid_grounds] == ["br-cdc-art-42-paragrafo-unico"]


def test_rate_limiter_preserves_throttled_keys_against_single_request_floods() -> None:
    now = 100.0
    limiter = SlidingWindowRateLimiter(
        2, window_seconds=60.0, clock=lambda: now, max_tracked_keys=3
    )
    assert limiter.allow("attacker")
    assert limiter.allow("attacker")
    assert not limiter.allow("attacker")

    # Flood with single-request keys; "attacker" is at limit (2) and must not be evicted.
    now = 101.0
    assert limiter.allow("ephemeral-1")
    now = 102.0
    assert limiter.allow("ephemeral-2")
    now = 103.0
    assert limiter.allow("ephemeral-3")

    assert not limiter.allow("attacker")


async def test_query_embedding_guard_bounds_background_tasks() -> None:
    gate = asyncio.Event()

    class SlowEmbedder(MockEmbeddingClient):
        async def embed_queries(self, texts: list[str]) -> list[list[float]]:
            await gate.wait()
            return await super().embed_queries(texts)

    guard = QueryEmbeddingGuard(
        SlowEmbedder(),
        timeout_seconds=0.01,
        max_concurrency=2,
        queue_timeout_seconds=0.05,
        circuit_breaker_failures=5,
        circuit_breaker_reset_seconds=60.0,
        cache_ttl_seconds=60.0,
        cache_max_entries=16,
        expected_dimension=128,
        max_background_tasks=1,
    )

    with pytest.raises(EmbeddingUnavailableError):
        await guard.embed(["consulta 1"])
    with pytest.raises(EmbeddingUnavailableError):
        await guard.embed(["consulta 2"])

    await asyncio.sleep(0.02)
    assert len(guard._background_tasks) <= 1
    gate.set()
    await asyncio.sleep(0.02)


async def test_lexical_token_cache_reuses_term_frequencies() -> None:
    store = InMemoryVectorStore()
    chunk = Chunk(
        chunk_id="doc-1:0",
        doc_id="doc-1",
        text="Cobrança indevida na fatura do cartão de crédito.",
        page_start=1,
        page_end=1,
    )
    await store.upsert([chunk], [[1.0, 0.0]])

    first = await store.lexical_query("cobrança indevida fatura", "doc-1", 5)
    second = await store.lexical_query("cartão crédito fatura", "doc-1", 5)

    assert len(first) == 1
    assert len(second) == 1
    cached = store._lexical_tokens._documents.get("doc-1")
    assert cached is not None
    tokens, counts = cached[chunk.text]
    assert "cobranca" in tokens
    assert counts["cobranca"] == 1


def test_provider_agnostic_llm_notice_composer_factory() -> None:
    settings = Settings(
        llm_provider=LLMProvider.MOCK,
        notice_composer=NoticeComposer.LLM,
        notice_composer_model="claude-sonnet-5",
        _env_file=None,
    )
    composer = create_notice_composer(settings)
    assert isinstance(composer, LLMNoticeComposer)
    assert composer._mode is NoticeComposer.LLM


def test_intake_extracts_corporate_suffix_suppliers_and_protocols() -> None:
    extraction = extract_explicit_facts(
        "Comprei uma passagem da Gol Linhas Aéreas em 12/04/2026 e abri o protocolo "
        "GOL-2026-9988 sem resposta. Quero o reembolso integral.",
        ConsumerCaseFacts(),
    )

    assert extraction.bank_name == "Gol Linhas Aéreas"
    assert extraction.incident_date_or_period == "12/04/2026"
    assert extraction.prior_protocols == ["GOL-2026-9988"]
    assert extraction.desired_resolution == "Quero o reembolso integral."


def test_notice_markdown_renders_optional_party_details_when_supplied() -> None:
    facts = ConsumerCaseFacts(
        consumer_name="Maria Silva",
        consumer_document="123.456.789-00",
        consumer_address="Rua das Flores, 100, Belo Horizonte/MG",
        bank_name="Empresa Exemplo S.A.",
        supplier_address="Av. Paulista, 1000, São Paulo/SP",
        complaint_summary="Cobrança em duplicidade na fatura.",
        incident_date_or_period="10/03/2026",
        prior_protocols=["PROT-999"],
        desired_resolution="Estorno do valor cobrado a mais.",
        notice_location_date="Belo Horizonte/MG, 05 de novembro de 2026.",
    )

    rendered = render_notice_markdown(
        facts=facts,
        evidence=[],
        legal_grounds=[],
        requests=["Estorno do valor cobrado a mais."],
        public_proposal=Decimal("150.00"),
    )

    assert "Av. Paulista, 1000, São Paulo/SP" in rendered
    assert "CPF: 123.456.789-00 — Endereço: Rua das Flores, 100, Belo Horizonte/MG" in rendered
    assert "Belo Horizonte/MG, 05 de novembro de 2026." in rendered
    assert "[PREENCHER ENDEREÇO DA NOTIFICADA]" not in rendered
    assert "[PREENCHER CPF DO(A) NOTIFICANTE]" not in rendered
    assert "[PREENCHER LOCAL]" not in rendered
