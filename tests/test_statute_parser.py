"""The generic Planalto statute parser, rule by rule, on minimal pages."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from app.consumer.schemas import LegalSource, LegalUnitKind, ProvisionStatus
from app.consumer.statutes import (
    DivisionSelector,
    ParsedArticle,
    StatuteParseError,
    StatuteSpec,
    TextCorrection,
    division_numeral,
    extract_paragraphs,
    in_index_scope,
    match_article_heading,
    parse_statute,
    parsed_text_sha256,
)
from app.consumer.statutes.parser import _OfficialHtmlTextParser

_SIGNATURE = "Brasília, 1 de janeiro de 2020; 199º da Independência."
_BODY = ("Art. 1º Um.", "Art. 2º Dois.", "Art. 3º Três.")


def _spec(**overrides: object) -> StatuteSpec:
    values: dict[str, object] = {
        "law_id": "br-cc",
        "source": LegalSource.CIVIL_CODE,
        "source_name": "Lei de teste",
        "citation_prefix": "Teste",
        "source_url": "https://www.planalto.gov.br/teste.htm",
        "directory": "teste",
        "snapshot_file": "teste.html",
        "encoding": "utf-8",
        "last_article": 3,
        "expected_article_count": 3,
    }
    values.update(overrides)
    return StatuteSpec(**values)  # type: ignore[arg-type]


def _parse(*paragraphs: str, **overrides: object) -> tuple[ParsedArticle, ...]:
    return parse_statute(_spec(**overrides), list(paragraphs))


def test_headings_nest_reset_and_take_their_names() -> None:
    articles = _parse(
        "Presidência da República",
        "PARTE GERAL",
        "LIVRO I",
        "DAS PESSOAS",
        "TÍTULO I Das Coisas",
        "CAPÍTULO I Disposições Gerais",
        "Art. 1 o Texto um.",
        "SUBTÍTULO I Do Casamento",
        "Seção I",
        "(Incluído pela Lei nº 1, de 2020)",
        "Da Seção Nomeada",
        "Art. 2º Texto dois.",
        "Art. 2-A. Texto dois A.",
        "Art. 2-B. Texto dois B.",
        "CAPITULO II DO FIM",
        "Art. 3º Texto três.",
        _SIGNATURE,
        "Texto depois da assinatura é ignorado.",
        expected_article_count=5,
        required_articles=frozenset({"2-a", "2-b"}),
    )

    first, second, second_a, _, third = articles
    assert (first.part, first.book, first.title, first.chapter) == (
        "PARTE GERAL",
        "LIVRO I DAS PESSOAS",
        "TÍTULO I Das Coisas",
        "CAPÍTULO I Disposições Gerais",
    )
    assert (second.subtitle, second.chapter, second.section) == (
        "SUBTÍTULO I Do Casamento",
        None,
        "Seção I Da Seção Nomeada",
    )
    assert (second_a.provision_id, second_a.article_key, second_a.article_label) == (
        "br-cc-art-2-a",
        "2-a",
        "art. 2-A",
    )
    assert (third.chapter, third.section) == ("CAPITULO II DO FIM", None)
    assert third.official_text == "Art. 3º Texto três."


def test_article_headings_accept_thousands_ordinals_and_a_parenthesis() -> None:
    assert match_article_heading("Art. 1.358-A. Texto") == (1358, "A")
    assert match_article_heading("Art. 3 o São absolutamente") == (3, None)
    assert match_article_heading("Art. 1º O presente") == (1, None)
    assert match_article_heading("Art. 759.(Revogado pela Lei nº 1, de 2020)") == (759, None)
    assert match_article_heading("Art 1.636. Sem ponto") is None
    assert match_article_heading("“Art. 7º citado") is None


def test_thousands_are_kept_in_labels_but_not_in_ids() -> None:
    paragraphs = [f"Art. {number:,}. Texto.".replace(",", ".") for number in range(1, 1002)]

    articles = parse_statute(
        _spec(last_article=1001, expected_article_count=1001), [*paragraphs, _SIGNATURE]
    )

    assert (articles[-1].provision_id, articles[-1].article_label) == (
        "br-cc-art-1001",
        "art. 1.001",
    )


def test_known_absences_bridge_the_sequence() -> None:
    articles = _parse(
        "Art. 1º Um.",
        "Art. 3º Três.",
        _SIGNATURE,
        expected_article_count=2,
        known_absent=MappingProxyType({2: "revogado em bloco"}),
    )

    assert [article.number for article in articles] == [1, 3]


def test_an_article_out_of_sequence_is_an_error() -> None:
    with pytest.raises(StatuteParseError, match="out of sequence after art. 1"):
        _parse("Art. 1º Um.", "Art. 3º Três.", _SIGNATURE)
    with pytest.raises(StatuteParseError, match="out of sequence after art. 1"):
        _parse("Art. 1º Um.", "Art. 2-A. Dois A.", _SIGNATURE)
    with pytest.raises(StatuteParseError, match="out of sequence after the start"):
        _parse("Art. 2º Dois.", _SIGNATURE)


def test_unexpected_text_after_a_heading_is_an_error() -> None:
    with pytest.raises(StatuteParseError, match="unexpected paragraph after heading"):
        _parse("CAPÍTULO I Disposições", "texto solto sem artigo", *_BODY, _SIGNATURE)


def test_corrections_must_apply_exactly_once() -> None:
    fix = TextCorrection("P A R T E G E R A L", "PARTE GERAL", "letras espaçadas")

    articles = _parse("P A R T E G E R A L", *_BODY, _SIGNATURE, text_corrections=(fix,))

    assert articles[0].part == "PARTE GERAL"
    with pytest.raises(StatuteParseError, match="matched 0 paragraphs"):
        _parse(*_BODY, _SIGNATURE, text_corrections=(fix,))
    with pytest.raises(StatuteParseError, match="matched 2 paragraphs"):
        _parse(
            "P A R T E G E R A L",
            "P A R T E G E R A L",
            *_BODY,
            _SIGNATURE,
            text_corrections=(fix,),
        )


def test_the_signature_is_required_and_may_have_a_space_before_the_comma() -> None:
    assert len(_parse(*_BODY, "Brasília , 14 de agosto de 2018.")) == 3
    with pytest.raises(StatuteParseError, match="signature"):
        _parse(*_BODY)


def test_completeness_checks_required_suffixes_and_the_count() -> None:
    with pytest.raises(StatuteParseError, match="missing suffixed articles"):
        _parse(*_BODY, _SIGNATURE, required_articles=frozenset({"2-a"}))
    with pytest.raises(StatuteParseError, match="3 articles, expected 4"):
        _parse(*_BODY, _SIGNATURE, expected_article_count=4)


def test_units_statuses_and_every_block_type() -> None:
    articles = _parse(
        "Art. 1º Caput do primeiro:",
        "I - primeiro inciso;",
        "a) primeira alínea;",
        "I - inciso repetido;",
        "§ 1º Primeiro parágrafo:",
        "II - inciso do parágrafo:",
        "b) alínea do inciso do parágrafo;",
        "Parágrafo único. Parágrafo único.",
        "Pena - detenção.",
        "“§ 4º Parágrafo de outra lei.",
        "“IV - Inciso de outra lei.",
        "“Art. 5º Artigo de outra lei.",
        "(Vide Lei nº 1, de 2020)",
        "Texto sem marcador.",
        "Art. 2º (VETADO).",
        "Art. 3º (Revogado pela Lei nº 2, de 2021).",
        _SIGNATURE,
    )

    first, vetoed, revoked = articles
    assert [unit.kind for unit in first.units] == [
        LegalUnitKind.CAPUT,
        LegalUnitKind.INCISO,
        LegalUnitKind.ALINEA,
        LegalUnitKind.INCISO,
        LegalUnitKind.PARAGRAPH,
        LegalUnitKind.INCISO,
        LegalUnitKind.ALINEA,
        LegalUnitKind.PARAGRAPH,
        LegalUnitKind.PENALTY,
        LegalUnitKind.QUOTED_AMENDMENT,
        LegalUnitKind.QUOTED_AMENDMENT,
        LegalUnitKind.QUOTED_AMENDMENT,
        LegalUnitKind.NOTE,
        LegalUnitKind.NORMATIVE_OTHER,
    ]
    ids = {unit.unit_id for unit in first.units}
    assert {"br-cc-art-1-inciso-i-2", "br-cc-art-1-paragrafo-1-inciso-ii-alinea-b"} <= ids
    assert (first.status, vetoed.status, revoked.status) == (
        ProvisionStatus.ACTIVE,
        ProvisionStatus.VETOED,
        ProvisionStatus.REVOKED,
    )


def test_division_numerals_and_index_scope() -> None:
    assert division_numeral("PARTE ESPECIAL", "parte") == "especial"
    assert division_numeral("LIVRO I DO DIREITO DAS OBRIGAÇÕES", "livro") == "i"
    assert division_numeral("TÍTULO I-A (Incluído pela Lei nº 1) DA EMPRESA", "titulo") == "i-a"
    assert division_numeral(None, "parte") == ""
    assert division_numeral("Sem numeral", "livro") == ""
    scoped = _spec(
        index_scope=(
            DivisionSelector((("parte", "geral"),)),
            DivisionSelector((("parte", "especial"), ("livro", "i"))),
        )
    )
    business = _parse("PARTE ESPECIAL", "LIVRO II Do Direito de Empresa", *_BODY, _SIGNATURE)
    obligations = _parse(
        "PARTE ESPECIAL", "LIVRO I DO DIREITO DAS OBRIGAÇÕES", *_BODY, _SIGNATURE
    )

    assert not in_index_scope(scoped, business[0])
    assert in_index_scope(scoped, obligations[0])
    assert in_index_scope(_spec(), business[0])


def test_paragraph_extraction_ignores_scripts_and_joins_line_breaks() -> None:
    html = (
        "<html><head><style>p { color: red }</style>"
        "<script>var x = '<p>oculto</p>';</script></head><body>"
        "texto fora de parágrafo</p><br><p>Art. 1º&nbsp;Um<br>linha dois</p><p> </p>"
        "<div><p>Art. 2º Dois.</p></div></body></html>"
    )

    paragraphs = extract_paragraphs(html)

    assert paragraphs == ["Art. 1º Um linha dois", "Art. 2º Dois."]
    assert len(parsed_text_sha256(paragraphs)) == 64


def test_tags_inside_ignored_blocks_are_skipped() -> None:
    parser = _OfficialHtmlTextParser()
    parser.handle_starttag("script", [])
    parser.handle_starttag("p", [])
    parser.handle_data("oculto")
    parser.handle_endtag("p")
    parser.handle_endtag("script")

    assert parser.paragraphs == []
