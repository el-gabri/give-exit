"""Stable ids for the LGPD and the Civil Code."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.consumer.schemas import LegalProvision, LegalSource, LegalTextUnit, LegalUnitKind
from app.schemas.evaluation import ConsumerLegalRetrievalHit

_LGPD_URL = "https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm"


def _provision(**overrides: object) -> LegalProvision:
    payload: dict[str, object] = {
        "provision_id": "br-lgpd-art-18",
        "source": LegalSource.DATA_PROTECTION_LAW,
        "source_name": "Lei Geral de Proteção de Dados Pessoais (Lei nº 13.709/2018)",
        "article": "art. 18",
        "citation_label": "LGPD, art. 18",
        "summary": "O titular dos dados pessoais tem direitos perante o controlador.",
        "official_url": _LGPD_URL,
        "corpus_release_id": "test-release",
        "verified_on": date(2026, 9, 12),
    }
    payload.update(overrides)
    return LegalProvision.model_validate(payload)


def test_lgpd_and_civil_code_provisions_get_their_own_law_ids() -> None:
    civil = _provision(
        provision_id="br-cc-art-1358-a",
        source=LegalSource.CIVIL_CODE,
        article="art. 1.358-A",
        citation_label="Código Civil, art. 1.358-A",
    )
    unit = LegalTextUnit(
        unit_id="br-cc-art-1358-a-paragrafo-1",
        kind=LegalUnitKind.PARAGRAPH,
        label="§ 1",
        text="§ 1º Texto de teste.",
    )

    assert _provision().law_id == "br-lgpd"
    assert civil.law_id == "br-cc"
    assert unit.unit_id == "br-cc-art-1358-a-paragrafo-1"


def test_a_provision_id_must_belong_to_its_law() -> None:
    with pytest.raises(ValidationError, match="must start with the law id"):
        _provision(provision_id="br-cdc-art-18")


def test_evaluation_hits_accept_the_new_laws_only() -> None:
    hit = ConsumerLegalRetrievalHit(
        provision_id="br-lgpd-art-18", unit_id="br-lgpd-art-18-inciso-vi"
    )

    assert hit.retrieval_id == "br-lgpd-art-18-inciso-vi"
    with pytest.raises(ValidationError, match="stable lowercase legal id"):
        ConsumerLegalRetrievalHit(provision_id="br-ctn-art-1")
