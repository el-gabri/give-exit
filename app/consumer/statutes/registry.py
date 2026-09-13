"""The official statutes the Consumer legal corpus can be built from."""

from __future__ import annotations

from types import MappingProxyType

from app.consumer.schemas import LegalSource
from app.consumer.statutes.spec import DivisionSelector, StatuteSpec, TextCorrection

CDC = StatuteSpec(
    law_id="br-cdc",
    source=LegalSource.CONSUMER_DEFENSE_CODE,
    source_name="Código de Defesa do Consumidor (Lei nº 8.078/1990)",
    citation_prefix="CDC",
    source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
    directory="cdc",
    snapshot_file="l8078compilado.html",
    encoding="windows-1252",
    last_article=119,
    expected_article_count=130,
    required_articles=frozenset(
        {"42-a", *(f"54-{suffix}" for suffix in "abcdefg"), *(f"104-{suffix}" for suffix in "abc")}
    ),
)

LGPD = StatuteSpec(
    law_id="br-lgpd",
    source=LegalSource.DATA_PROTECTION_LAW,
    source_name="Lei Geral de Proteção de Dados Pessoais (Lei nº 13.709/2018)",
    citation_prefix="LGPD",
    source_url="https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm",
    directory="lgpd",
    snapshot_file="l13709compilado.html",
    encoding="windows-1252",
    last_article=65,
    expected_article_count=80,
    required_articles=frozenset(
        {*(f"55-{suffix}" for suffix in "abcdefghijklm"), "58-a", "58-b"}
    ),
    text_corrections=(
        TextCorrection(
            "Art. 5 7. (VETADO).",
            "Art. 57. (VETADO).",
            "espaço espúrio no número do art. 57 na página oficial",
        ),
    ),
)

CIVIL_CODE = StatuteSpec(
    law_id="br-cc",
    source=LegalSource.CIVIL_CODE,
    source_name="Código Civil (Lei nº 10.406/2002)",
    citation_prefix="Código Civil",
    source_url="https://www.planalto.gov.br/ccivil_03/leis/2002/l10406compilada.htm",
    directory="cc",
    snapshot_file="l10406compilada.html",
    encoding="windows-1252",
    last_article=2046,
    expected_article_count=2083,
    required_articles=frozenset(
        {
            "48-a", "49-a", "206-a", "421-a", "819-a", "853-a", "980-a", "1080-a",
            "1240-a", "1354-a", "1487-a", "1775-a", "1783-a", "1815-a",
            *(f"1358-{suffix}" for suffix in "abcdefghijklmnopqrstu"),
            *(f"1368-{suffix}" for suffix in "abcdef"),
            *(f"1510-{suffix}" for suffix in "abcde"),
        }
    ),
    known_absent=MappingProxyType(
        {
            number: "revogados em bloco no parágrafo 'Art. 1.620. a 1.629.' (Lei nº 12.010/2009)"
            for number in range(1621, 1630)
        }
    ),
    text_corrections=(
        TextCorrection("P A R T E G E R A L", "PARTE GERAL", "título com letras espaçadas"),
        TextCorrection("Art 1.636.", "Art. 1.636.", "falta o ponto depois de 'Art'"),
    ),
    # ADR 0016: the Civil Code is subsidiary to the CDC. Only the general part
    # and the law of obligations are indexed; the other books are audit-only.
    index_scope=(
        DivisionSelector((("parte", "geral"),)),
        DivisionSelector((("parte", "especial"), ("livro", "i"))),
    ),
)

STATUTES: tuple[StatuteSpec, ...] = (CDC, LGPD, CIVIL_CODE)
