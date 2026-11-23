"""Statutory citations the consumer writes resolve to citable corpus units."""

from __future__ import annotations

import time

import pytest

from app.consumer.cited_provisions import consumer_cited_provisions
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import ConsumerCaseFacts


def _facts(complaint: str, remedy: str | None = None) -> ConsumerCaseFacts:
    return ConsumerCaseFacts(complaint_summary=complaint, desired_resolution=remedy)


def _unit_ids(complaint: str, remedy: str | None = None) -> list[str | None]:
    citations = consumer_cited_provisions(get_default_legal_corpus(), _facts(complaint, remedy))
    return [citation.unit.unit_id if citation.unit else None for citation in citations]


def test_the_long_complaint_citation_resolves_to_both_articles() -> None:
    # The shape of the first real long prompt complaint, in invented words.
    complaint = (
        "A loja entregou um notebook diferente do anunciado. Meu pedido inicial era o "
        "cumprimento forçado da oferta, nos termos dos artigos 30 e 35, inciso I, do "
        "Código de Defesa do Consumidor (CDC), mas a empresa ofereceu só a devolução."
    )
    corpus = get_default_legal_corpus()

    citations = consumer_cited_provisions(corpus, _facts(complaint))

    assert [citation.unit.unit_id for citation in citations if citation.unit] == [
        "br-cdc-art-30-caput",
        "br-cdc-art-35-inciso-i",
    ]
    assert [citation.whole_article for citation in citations] == [True, False]
    assert citations[0].chunk.chunk_id == (
        f"{corpus.document_id}:legal:br-cdc-art-30-caput:part-01"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Conforme o art. 18, § 1º, II, do CDC.", ["br-cdc-art-18-paragrafo-1-inciso-ii"]),
        ("Vale o § 1º do art. 18 do CDC.", ["br-cdc-art-18-paragrafo-1"]),
        (
            "Peço a devolução em dobro, como diz o parágrafo único do artigo 42 do CDC.",
            ["br-cdc-art-42-paragrafo-unico"],
        ),
        ("Tenho direito pelo art. 6º, inciso III, da Lei 8.078/90.", ["br-cdc-art-6-inciso-iii"]),
        ("Nos termos do art. 49 da Lei nº 8.078/1990.", ["br-cdc-art-49-caput"]),
        ("ART. 49 DO CÓDIGO DE DEFESA DO CONSUMIDOR.", ["br-cdc-art-49-caput"]),
        ("O art. 54-F do CDC se aplica.", ["br-cdc-art-54-f-caput"]),
        ("O inciso II do § 3º do art. 54-F do CDC.", ["br-cdc-art-54-f-paragrafo-3-inciso-ii"]),
        ("Pelo art. 18 da LGPD pedi meus dados.", ["br-lgpd-art-18-caput"]),
        ("Art. 18, II, da Lei 13.709/2018.", ["br-lgpd-art-18-inciso-ii"]),
        ("O dano está no art. 186 do Código Civil.", ["br-cc-art-186-caput"]),
        (
            "Os incisos I e II do art. 35 do CDC.",
            ["br-cdc-art-35-inciso-i", "br-cdc-art-35-inciso-ii"],
        ),
        (
            "Os arts. 18, 19 e 20 do CDC.",
            ["br-cdc-art-18-caput", "br-cdc-art-19-caput", "br-cdc-art-20-caput"],
        ),
        (
            "O art. 30 e o art. 35, caput, do CDC.",
            ["br-cdc-art-30-caput", "br-cdc-art-35-caput"],
        ),
        ("O art. 4º, II, alínea a, do CDC.", ["br-cdc-art-4-inciso-ii-alinea-a"]),
    ],
)
def test_paragraph_inciso_alinea_and_reverse_order_forms(
    text: str, expected: list[str]
) -> None:
    assert _unit_ids(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # No law is named: "art. 35" of what?
        "O art. 35 garante a troca.",
        # The CDC has no art. 999, and its art. 35 no inciso IX.
        "Conforme o art. 999 do CDC.",
        "Conforme o art. 35, inciso IX, do CDC.",
        # A criminal offence (Título II) is never a ground of a consumer's notice.
        "A loja cometeu o crime do art. 61 do CDC.",
        # Vetoed.
        "O art. 6º, IX, do CDC.",
        # The supplier's defense, which a notice must not quote for the consumer.
        "A empresa alegou o art. 14, § 3º, do CDC.",
        # Ranges are too broad to be one claim.
        "Os arts. 18 a 25 do CDC.",
        # The law named first is not read.
        "O CDC, em seu art. 35, garante a troca.",
        # The Constitution is not read.
        "O art. 5º, XXXII, da Constituição Federal.",
        # A stray word between the article and the law is no citation.
        "O art. 35 e a LGPD protegem o consumidor.",
    ],
)
def test_citations_that_resolve_to_nothing(text: str) -> None:
    assert _unit_ids(text) == []


def test_a_whole_article_citation_yields_to_a_unit_citation_of_the_same_article() -> None:
    assert _unit_ids(
        "O art. 35 do CDC me protege.", "Quero o que diz o art. 35, inciso I, do CDC."
    ) == ["br-cdc-art-35-inciso-i"]


def test_repeated_citations_collapse_in_order_of_first_mention() -> None:
    assert _unit_ids(
        "Pelo art. 18 do CDC e pelo art. 49 do CDC.", "Repito: art. 18 do CDC."
    ) == ["br-cdc-art-18-caput", "br-cdc-art-49-caput"]


@pytest.mark.parametrize(
    ("piece", "ending"),
    [("§ 1º ", "do art. 1"), ("art. 35 e ", ""), ("inciso I e ", "do art. 5")],
    ids=["paragraphs", "articles", "incisos"],
)
def test_a_long_run_of_citation_words_is_scanned_in_bounded_time(
    piece: str, ending: str
) -> None:
    """Unbounded repetition took seconds of event-loop time on such a run."""
    limit = ConsumerCaseFacts.model_fields["complaint_summary"].metadata[0].max_length
    text = (piece * (limit // len(piece)))[: limit - len(ending)] + ending
    started = time.perf_counter()

    assert _unit_ids(text) == []
    assert time.perf_counter() - started < 1.5


def test_no_text_resolves_to_nothing() -> None:
    assert consumer_cited_provisions(get_default_legal_corpus(), ConsumerCaseFacts()) == []


def test_the_citation_chunk_is_the_indexed_official_text() -> None:
    corpus = get_default_legal_corpus()
    provision = corpus.get("br-cdc-art-6")
    vetoed = next(unit for unit in provision.units if unit.unit_id == "br-cdc-art-6-inciso-ix")
    caput = next(unit for unit in provision.units if unit.unit_id == "br-cdc-art-6-caput")

    assert corpus.citation_chunk(provision, vetoed) is None
    chunk = corpus.citation_chunk(provision, caput)
    assert chunk is not None
    assert chunk.metadata["unit_id"] == "br-cdc-art-6-caput"
    assert corpus.citation_chunk(corpus.get("br-cf-art-5-xxxii"), None) is not None
