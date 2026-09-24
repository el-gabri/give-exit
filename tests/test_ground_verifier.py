"""The optional ground verifier may only act on quotes that check out verbatim."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import fitz
import httpx
import pytest

from app.api.main import create_app
from app.consumer.ground_verifier import (
    GroundVerificationResult,
    LLMGroundVerifier,
    NoGroundVerifier,
    _ModelVerdict,
    _ModelVerification,
    apply_verdicts,
    create_ground_verifier,
)
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.notice_markdown import render_notice_markdown
from app.consumer.schemas import (
    ConsumerCaseFacts,
    GroundVerification,
    GroundVerificationSummary,
    LegalGround,
)
from app.core.config import GroundVerifierMode, LLMProvider, Settings, VectorStoreBackend
from app.llm.mock_client import MockLLMClient
from app.schemas.rag import RetrievedChunk

FACTS = ConsumerCaseFacts(
    complaint_summary="A loja cobrou duas vezes a mesma compra no cartão de crédito.",
    desired_resolution="Quero a devolução em dobro do valor pago indevidamente.",
)


def _ground(unit_id: str) -> LegalGround:
    corpus = get_default_legal_corpus()
    chunk = next(
        chunk for chunk in corpus.as_chunks() if chunk.metadata.get("unit_id") == unit_id
    )
    return LegalGround(
        authority=corpus.authority_for_chunk(RetrievedChunk(chunk=chunk, score=0.03)),
        application_to_facts="Selecionado pela política de recuperação.",
    )


def _official(ground: LegalGround) -> str:
    return " ".join((ground.authority.official_excerpt or "").split())


def _applies(index: int, ground: LegalGround, fact_quote: str) -> _ModelVerdict:
    return _ModelVerdict(
        ground_index=index,
        verdict="applies",
        fact_quote=fact_quote,
        provision_quote=_official(ground)[:80],
    )


def test_verbatim_quotes_verify_a_ground_and_explain_it() -> None:
    ground = _ground("br-cdc-art-42-paragrafo-unico")
    # Case and spacing may differ; the source's own spelling is what is kept.
    verdict = _applies(0, ground, "A  LOJA cobrou duas vezes a mesma compra")

    [verified] = apply_verdicts(FACTS, [ground], [verdict])

    assert verified.verification == GroundVerification(
        verdict="applies",
        verified=True,
        fact_quote="A loja cobrou duas vezes a mesma compra",
        provision_quote=_official(ground)[:80],
    )
    assert "A loja cobrou duas vezes a mesma compra" in verified.application_to_facts
    assert "profissional habilitado" in verified.application_to_facts


@pytest.mark.parametrize(
    "fact_quote",
    [
        "A loja cobrou três vezes a mesma compra",  # not what the consumer said
        "duas vezes",  # too short to prove anything
    ],
)
def test_unverifiable_quotes_leave_the_ground_uncertain(fact_quote: str) -> None:
    ground = _ground("br-cdc-art-42-paragrafo-unico")

    [kept] = apply_verdicts(FACTS, [ground], [_applies(0, ground, fact_quote)])

    assert kept.verification == GroundVerification(verdict="uncertain", verified=False)
    assert kept.application_to_facts == ground.application_to_facts


def test_a_quote_from_the_wrong_provision_is_not_verified() -> None:
    ground = _ground("br-cdc-art-42-paragrafo-unico")
    other = _ground("br-cdc-art-43-paragrafo-2")
    verdict = _applies(0, other, "A loja cobrou duas vezes a mesma compra")

    [kept] = apply_verdicts(FACTS, [ground], [verdict])

    assert kept.verification is not None and not kept.verification.verified


def test_rejected_grounds_are_removed_and_missing_verdicts_stay_uncertain() -> None:
    cdc_42 = _ground("br-cdc-art-42-paragrafo-unico")
    cdc_43 = _ground("br-cdc-art-43-paragrafo-2")

    kept = apply_verdicts(
        FACTS,
        [cdc_42, cdc_43],
        [_ModelVerdict(ground_index=1, verdict="does_not_apply")],
    )

    assert [ground.authority.unit_id for ground in kept] == ["br-cdc-art-42-paragrafo-unico"]
    assert kept[0].verification == GroundVerification(verdict="uncertain", verified=False)


def test_removing_the_last_cdc_ground_drops_complementary_sources() -> None:
    cdc_42 = _ground("br-cdc-art-42-paragrafo-unico")
    civil_code = _ground("br-cc-art-876-caput")

    kept = apply_verdicts(
        FACTS,
        [cdc_42, civil_code],
        [_ModelVerdict(ground_index=0, verdict="does_not_apply")],
    )

    assert kept == []


def test_removing_the_last_cdc_ground_keeps_lgpd_grounds() -> None:
    cdc_42 = _ground("br-cdc-art-42-paragrafo-unico")
    lgpd = _ground("br-lgpd-art-18-inciso-vi")
    civil_code = _ground("br-cc-art-876-caput")

    kept = apply_verdicts(
        FACTS,
        [cdc_42, lgpd, civil_code],
        [_ModelVerdict(ground_index=0, verdict="does_not_apply")],
    )

    assert [ground.authority.unit_id for ground in kept] == ["br-lgpd-art-18-inciso-vi"]


async def test_default_verifier_passes_grounds_through() -> None:
    grounds = [_ground("br-cdc-art-42-paragrafo-unico")]

    result = await NoGroundVerifier().verify(FACTS, grounds)

    assert result.grounds == grounds
    assert result.summary == GroundVerificationSummary(mode=GroundVerifierMode.NONE)


async def test_llm_verifier_records_metadata_and_removals() -> None:
    cdc_42 = _ground("br-cdc-art-42-paragrafo-unico")
    cdc_43 = _ground("br-cdc-art-43-paragrafo-2")
    client = MockLLMClient(
        responses={
            _ModelVerification: _ModelVerification(
                verdicts=[
                    _applies(0, cdc_42, "cobrou duas vezes a mesma compra"),
                    _ModelVerdict(ground_index=1, verdict="does_not_apply"),
                ]
            )
        }
    )

    result = await LLMGroundVerifier(client, max_output_tokens=512).verify(
        FACTS, [cdc_42, cdc_43]
    )

    assert [ground.authority.unit_id for ground in result.grounds] == [
        "br-cdc-art-42-paragrafo-unico"
    ]
    assert result.summary.mode is GroundVerifierMode.LLM
    assert result.summary.removed == 1
    assert result.summary.metadata is not None
    assert result.warnings == []
    [call] = client.calls
    # The packet carries the facts and each ground's official text, nothing else.
    assert "cobrou duas vezes" in call["user"]
    assert cdc_42.authority.citation_label in call["user"]
    assert "chunk" not in call["user"]


async def test_llm_verifier_failure_keeps_the_selected_grounds() -> None:
    class FailingClient(MockLLMClient):
        async def parse(self, **kwargs):  # noqa: ANN003, ANN201
            raise TimeoutError("provider timed out")

    grounds = [_ground("br-cdc-art-42-paragrafo-unico")]

    result = await LLMGroundVerifier(FailingClient(), max_output_tokens=512).verify(
        FACTS, grounds
    )

    assert result.grounds == grounds
    assert result.summary.error == "TimeoutError"
    assert result.warnings


async def test_llm_verifier_skips_the_call_without_grounds() -> None:
    client = MockLLMClient()

    result = await LLMGroundVerifier(client, max_output_tokens=512).verify(FACTS, [])

    assert result.grounds == []
    assert client.calls == []


def test_factory_is_off_by_default() -> None:
    assert isinstance(create_ground_verifier(Settings(_env_file=None)), NoGroundVerifier)
    enabled = Settings(
        ground_verifier=GroundVerifierMode.LLM, llm_provider=LLMProvider.MOCK, _env_file=None
    )
    assert isinstance(create_ground_verifier(enabled), LLMGroundVerifier)


def test_only_verified_grounds_render_their_relation_to_the_facts() -> None:
    [verified] = apply_verdicts(
        FACTS,
        [_ground("br-cdc-art-42-paragrafo-unico")],
        [_applies(0, _ground("br-cdc-art-42-paragrafo-unico"), "cobrou duas vezes a mesma compra")],
    )
    uncertain = _ground("br-cdc-art-43-paragrafo-2")

    markdown = render_notice_markdown(
        facts=FACTS,
        evidence=[],
        legal_grounds=[verified, uncertain],
        requests=["Devolução em dobro."],
        public_proposal=Decimal("10.00"),
    )

    assert markdown.count("Relação com os fatos") == 1
    assert 'relata que "cobrou duas vezes a mesma compra"' in markdown


class _RejectAll:
    async def verify(
        self, facts: ConsumerCaseFacts, grounds: list[LegalGround]
    ) -> GroundVerificationResult:
        del facts
        return GroundVerificationResult(
            grounds=[],
            summary=GroundVerificationSummary(mode=GroundVerifierMode.LLM, removed=len(grounds)),
        )


async def test_notice_abstains_when_the_verifier_rejects_every_ground(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.main.create_ground_verifier", lambda _settings: _RejectAll())
    settings = Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.consumer_service.prepare_legal_corpus()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = (await client.post("/consumer/cases")).json()
            case_id = created["case_id"]
            headers = {"X-Consumer-Case-Token": created["case_token"]}
            document = fitz.open()
            document.new_page().insert_text(
                fitz.Point(72, 72), "Fatura: cobranca em duplicidade da compra, protocolo ABC-123."
            )
            await client.post(
                f"/consumer/cases/{case_id}/documents",
                headers=headers,
                files={"file": ("fatura.pdf", document.tobytes(), "application/pdf")},
            )
            await client.patch(
                f"/consumer/cases/{case_id}/facts",
                headers=headers,
                json={
                    "consumer_name": "Maria Souza",
                    "bank_name": "Loja Exemplo",
                    "complaint_summary": "A loja fez cobranca em duplicidade da compra.",
                    "incident_date_or_period": "10/03/2026",
                    "prior_protocols": ["ABC-123"],
                    "desired_resolution": "Quero a devolucao da cobranca em duplicidade.",
                    "facts_confirmed": True,
                },
            )

            response = await client.post(f"/consumer/cases/{case_id}/notice", headers=headers)

    assert response.status_code == 503
    assert "fundamentos jurídicos" in response.json()["detail"]
