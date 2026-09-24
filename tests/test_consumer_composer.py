"""Offline contracts for the grounded final-notice composer."""

from decimal import Decimal

import pytest

from app.consumer.composer import NoticeProse, OpenAINoticeComposer, _validate_prose
from app.consumer.schemas import ConsumerCaseFacts, EvidenceCitation
from app.consumer.service import ConsumerCaseService
from app.core.config import NoticeComposer, Settings
from app.ingestion.service import DocumentIngestionService
from app.llm.base import LLMCallMetadata, ParsedResult, TokenUsage
from app.llm.mock_client import MockLLMClient
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.security.prompt_injection import PromptInjectionDetector


class _FakeStructuredClient:
    def __init__(self, prose: NoticeProse) -> None:
        self.prose = prose
        self.call: dict[str, object] | None = None

    async def parse(self, **kwargs: object) -> ParsedResult[NoticeProse]:
        self.call = kwargs
        return ParsedResult(
            data=self.prose,
            meta=LLMCallMetadata(
                provider="openai",
                model="gpt-5.6-terra",
                latency_ms=12,
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            ),
        )


def _prose(**overrides: str) -> NoticeProse:
    values = {
        "purpose": "Busca-se uma solução consensual para a controvérsia relatada.",
        "facts_framing": "O relato é apresentado como alegação da consumidora.",
        "legal_transition": "Os fundamentos selecionados são apresentados para análise.",
        "requests_transition": "Solicitam-se as providências abaixo.",
        "closing": "Permanece aberta a possibilidade de composição amigável.",
    }
    values.update(overrides)
    return NoticeProse(**values)


async def test_openai_composer_requests_low_reasoning_structured_prose() -> None:
    fake = _FakeStructuredClient(_prose())
    composer = OpenAINoticeComposer(fake, reasoning_effort="low", max_output_tokens=1200)
    facts = ConsumerCaseFacts(
        complaint_summary="Houve cobrança não reconhecida.",
        desired_resolution="Cancelar a cobrança.",
    )
    evidence = [
        EvidenceCitation(
            evidence_id="evidence-1",
            filename="fatura.pdf",
            page=1,
            quote="Lançamento de R$ 50,00.",
            chunk_id="chunk-1",
            content_sha256="a" * 64,
        )
    ]

    result = await composer.compose(
        facts=facts,
        evidence=evidence,
        legal_grounds=[],
        requests=["Cancelar a cobrança."],
        public_proposal=Decimal("50.00"),
    )

    assert result.mode is NoticeComposer.OPENAI
    assert result.prose == _prose()
    assert fake.call is not None
    assert fake.call["schema"] is NoticeProse
    assert fake.call["reasoning_effort"] == "low"
    assert fake.call["max_output_tokens"] == 1200
    assert "Lançamento de R$ 50,00." in str(fake.call["user"])


def test_composer_rejects_source_bearing_model_prose() -> None:
    with pytest.raises(ValueError, match="source-bearing"):
        _validate_prose(_prose(legal_transition="Veja https://exemplo.test."))


@pytest.mark.parametrize(
    "legal_transition",
    [
        "Conforme o art. 42 do código, cabe a devolução.",
        "Nos termos do artigo 6º, a informação é direito básico.",
        "O inciso III garante informação adequada.",
        "A Lei nº 8.078 disciplina a matéria.",
        "O § 1º do dispositivo se aplica.",
    ],
)
def test_composer_rejects_article_level_citations(legal_transition: str) -> None:
    with pytest.raises(ValueError, match="source-bearing|legal provision"):
        _validate_prose(_prose(legal_transition=legal_transition))


def test_composer_rejects_amounts_and_numbers_it_invented() -> None:
    packet = '{"relato":"Cobrança de 250 reais em 10/03/2026","protocolos":["ABC-123"]}'

    with pytest.raises(ValueError, match="source-bearing"):
        _validate_prose(_prose(closing="Propõe-se o pagamento de R$ 500,00."), source_packet=packet)
    with pytest.raises(ValueError, match="absent from its input"):
        _validate_prose(
            _prose(requests_transition="Aguarda-se resposta em 5 dias úteis."),
            source_packet=packet,
        )
    # Numbers taken from the packet are the consumer's own facts.
    _validate_prose(
        _prose(facts_framing="Relata-se cobrança de 250 reais em 10/03/2026, protocolo ABC-123."),
        source_packet=packet,
    )


def test_notice_composer_is_offline_by_default_and_requires_openai_key() -> None:
    assert Settings(_env_file=None).notice_composer is NoticeComposer.DETERMINISTIC
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings(notice_composer=NoticeComposer.OPENAI, _env_file=None)


class _FailingComposer:
    async def compose(self, **kwargs: object) -> object:
        del kwargs
        raise RuntimeError("provider unavailable")


async def test_service_marks_deterministic_fallback_when_composer_fails() -> None:
    service = ConsumerCaseService(
        ingestion=DocumentIngestionService(),
        detector=PromptInjectionDetector(MockLLMClient()),
        rag=RagPipeline(MockEmbeddingClient(), InMemoryVectorStore()),
        notice_composer=_FailingComposer(),  # type: ignore[arg-type]
    )

    composition, warnings = await service._compose_notice_prose(
        facts=ConsumerCaseFacts(complaint_summary="Cobrança não reconhecida."),
        evidence=[],
        legal_grounds=[],
        requests=["Cancelar a cobrança."],
        public_proposal=None,
    )

    assert composition.mode is NoticeComposer.DETERMINISTIC
    assert warnings == [
        "A composição por IA não esteve disponível; o rascunho foi montado "
        "deterministicamente."
    ]
