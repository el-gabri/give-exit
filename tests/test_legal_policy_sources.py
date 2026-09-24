"""Eligibility and precedence for the LGPD and the Civil Code."""

from __future__ import annotations

from datetime import date

from app.consumer.legal_policy import (
    MAX_COMPLEMENTARY_GROUNDS,
    enforce_cdc_anchor,
    precedence_window,
    provision_is_eligible,
)
from app.consumer.schemas import LAW_ID_BY_SOURCE, LegalProvision, LegalSource


def _provision(source: LegalSource, number: int, **overrides: object) -> LegalProvision:
    payload: dict[str, object] = {
        "provision_id": f"{LAW_ID_BY_SOURCE[source]}-art-{number}",
        "source": source,
        "source_name": "Fonte de teste",
        "article": f"art. {number}",
        "citation_label": f"Teste, art. {number}",
        "summary": "Resumo de teste.",
        "official_url": "https://www.planalto.gov.br/teste.htm",
        "corpus_release_id": "test",
        "verified_on": date(2026, 9, 12),
        "title": "TÍTULO I Dos Direitos do Consumidor",
    }
    payload.update(overrides)
    return LegalProvision.model_validate(payload)


def _lgpd(chapter: str | None) -> LegalProvision:
    return _provision(LegalSource.DATA_PROTECTION_LAW, 18, chapter=chapter, title=None)


def test_lgpd_chapters_a_notice_cannot_rest_on() -> None:
    assert provision_is_eligible(_lgpd("CAPÍTULO III DOS DIREITOS DO TITULAR"))
    assert provision_is_eligible(_lgpd("CAPÍTULO VI DOS AGENTES DE TRATAMENTO DE DADOS PESSOAIS"))
    for excluded in (
        "CAPÍTULO IV DO TRATAMENTO DE DADOS PESSOAIS PELO PODER PÚBLICO",
        "CAPÍTULO VIII DA FISCALIZAÇÃO",
        "CAPÍTULO IX DA AGÊNCIA NACIONAL DE PROTEÇÃO DE DADOS",
        "CAPÍTULO X DISPOSIÇÕES FINAIS E TRANSITÓRIAS",
        None,
    ):
        assert not provision_is_eligible(_lgpd(excluded)), excluded


def test_civil_code_and_audit_only_provisions() -> None:
    assert provision_is_eligible(_provision(LegalSource.CIVIL_CODE, 927))
    assert not provision_is_eligible(
        _provision(LegalSource.CIVIL_CODE, 1511, index_scope="audit_only")
    )
    assert not provision_is_eligible(
        _provision(LegalSource.CONSUMER_DEFENSE_CODE, 42, index_scope="audit_only")
    )


def test_the_window_caps_complementary_sources_without_losing_slots() -> None:
    civil = [(_provision(LegalSource.CIVIL_CODE, n), f"cc-{n}") for n in range(1, 6)]
    cdc = [(_provision(LegalSource.CONSUMER_DEFENSE_CODE, n), f"cdc-{n}") for n in range(1, 7)]

    admitted = precedence_window([*civil, *cdc], window=8, lexical_only=False)

    assert MAX_COMPLEMENTARY_GROUNDS == 3
    assert [item for _, item in admitted] == [
        "cc-1", "cc-2", "cc-3", "cdc-1", "cdc-2", "cdc-3", "cdc-4", "cdc-5",
    ]


def test_lexical_only_retrieval_admits_no_complementary_source() -> None:
    mixed = [
        (_lgpd("CAPÍTULO VI DOS AGENTES DE TRATAMENTO DE DADOS PESSOAIS"), "lgpd"),
        (_provision(LegalSource.CONSUMER_DEFENSE_CODE, 14), "cdc"),
    ]

    assert [item for _, item in precedence_window(mixed, window=8, lexical_only=True)] == ["cdc"]


def test_civil_code_grounds_need_a_cdc_anchor() -> None:
    cdc = (_provision(LegalSource.CONSUMER_DEFENSE_CODE, 14), "cdc")
    cf = (_provision(LegalSource.FEDERAL_CONSTITUTION, 5), "cf")
    civil = (_provision(LegalSource.CIVIL_CODE, 927), "cc")

    assert [item for _, item in enforce_cdc_anchor([civil, cdc])] == ["cc", "cdc"]
    assert [item for _, item in enforce_cdc_anchor([civil, cf])] == ["cf"]


def test_the_lgpd_grounds_a_notice_without_the_cdc() -> None:
    """A data-protection complaint may have no CDC article that clears the gate."""
    lgpd = (_lgpd("CAPÍTULO III DOS DIREITOS DO TITULAR"), "lgpd")
    civil = (_provision(LegalSource.CIVIL_CODE, 927), "cc")

    assert [item for _, item in enforce_cdc_anchor([lgpd])] == ["lgpd"]
    # The LGPD does not anchor the Civil Code in the CDC's place.
    assert [item for _, item in enforce_cdc_anchor([lgpd, civil])] == ["lgpd"]


def test_civil_code_specific_contract_types_are_not_citable() -> None:
    especial = {"part": "PARTE ESPECIAL", "book": "LIVRO I DO DIREITO DAS OBRIGAÇÕES"}

    assert not provision_is_eligible(
        _provision(
            LegalSource.CIVIL_CODE,
            633,
            title="TÍTULO VI Das Várias Espécies de Contrato",
            **especial,
        )
    )
    assert provision_is_eligible(
        _provision(
            LegalSource.CIVIL_CODE,
            876,
            title="TÍTULO VII Dos Atos Unilaterais",
            **especial,
        )
    )
    assert provision_is_eligible(
        _provision(
            LegalSource.CIVIL_CODE,
            3,
            part="PARTE GERAL",
            book="LIVRO I DAS PESSOAS",
            title="TÍTULO I DAS PESSOAS NATURAIS",
        )
    )
