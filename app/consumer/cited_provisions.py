"""Statutory provisions the consumer cites in their own account (ADR 0025).

A consumer who writes "nos termos dos artigos 30 e 35, inciso I, do CDC" has
made a legal claim, and retrieval can still miss it. The first real long
prompt complaint was refused for that reason:
- the narrative was cut before that sentence;
- neither retrieval channel ranked CDC art. 30 in its top ten.

The agreement gate decides when the system's own search may cite an article.
A provision the consumer names is the consumer's claim whether or not a
search finds it. This module only resolves such citations against the
corpus. The scope gate, the eligibility policy and the notice's caps still
apply where grounds are selected.

Only explicit citations are read: an article number with its law named after
it, by acronym, name or law number. Examples: "art. 49 do CDC", "§ 1º do art.
18 da Lei 8.078/90", "art. 18 da LGPD", "art. 186 do Código Civil".

These forms are not read, and a citation that does not resolve leaves the
notice as retrieval made it:
- an article without a law;
- a range ("arts. 18 a 25");
- the law named first ("o CDC, em seu art. 35");
- the Constitution.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

from app.consumer.legal_corpus import LegalCorpus
from app.consumer.legal_policy import alias_exclusion_reason
from app.consumer.schemas import (
    ConsumerCaseFacts,
    LegalProvision,
    LegalTextUnit,
    LegalUnitKind,
    ProvisionStatus,
)
from app.schemas.rag import Chunk

# The patterns read text folded to lowercase without accents, so "Código",
# "parágrafo único" and "6º" arrive as "codigo", "paragrafo unico" and "6o".
_NUMBER = r"\d{1,4}(?:\.\d{3})?(?:\s*-\s*[a-z](?![a-z]))?"
_ORDINAL = r"(?:[o°](?![a-z]))?"
_ROMAN = r"(?<![a-z])[ivxlc]+(?![a-z])"
_LETTER = r"[\"'“”‘’]?[a-z](?![a-z])[\"'“”‘’]?"
_LIST = r"(?:\s*,\s*|\s+e\s+)"
# Every repetition is bounded: an unbounded one made each start position scan
# the rest of a long run ("§ 1º § 1º § 1º ..."), seconds of CPU on 20,000
# characters. Real clauses list a handful of items.
_MAX_ITEMS = 12
_MORE = rf"{{0,{_MAX_ITEMS}}}"
_PARAGRAPHS = rf"(?:§§|paragrafos)\s*{_NUMBER}{_ORDINAL}(?:{_LIST}{_NUMBER}{_ORDINAL}){_MORE}"
_PARAGRAPH = rf"(?:§|paragrafo|par\.)\s*(?:unico|{_NUMBER}{_ORDINAL})"
_INCISOS = rf"incisos?\s+{_ROMAN}(?:{_LIST}{_ROMAN}){_MORE}"
_ALINEAS = rf"alineas?\s+{_LETTER}(?:{_LIST}{_LETTER}){_MORE}"
_SUBDIVISION = rf"(?:{_PARAGRAPHS}|{_PARAGRAPH}|{_INCISOS}|{_ALINEAS}|caput)"
_ARTICLE_WORD = r"(?<![a-z])(?:arts?\.?|artigos?)"
# "o art. 30 e o art. 35 do CDC": a determiner may precede a repeated article word.
_DETERMINER = r"(?:o|os|a|as|ao|aos|no|nos|na|nas|pelo|pelos|pela|pelas)\s+"
_ARTICLE_NUMBER = rf"(?:(?:{_DETERMINER})?{_ARTICLE_WORD}\s*)?{_NUMBER}{_ORDINAL}"
_PIECE = rf"(?:{_SUBDIVISION}|{_ROMAN}|{_ARTICLE_NUMBER})"
_SEPARATOR = rf"(?:{_LIST}|\s+)"
_OF = r"\s+d[oa]\s+"
_LAW_NUMBER = r"lei\s*(?:n[o°.]?\s*)?"
# Regex group name -> law id. Each law is matched by acronym, name or number.
_LAW_IDS = {"cdc": "br-cdc", "lgpd": "br-lgpd", "cc": "br-cc"}
_LAWS = {
    "cdc": (
        r"cdc|codigo\s+(?:de\s+defesa\s+)?do\s+consumidor"
        rf"|{_LAW_NUMBER}8\.?078(?:\s*/\s*(?:19)?90)?"
    ),
    "lgpd": (
        r"lgpd|lei\s+geral\s+de\s+protecao\s+de\s+dados(?:\s+pessoais)?"
        rf"|{_LAW_NUMBER}13\.?709(?:\s*/\s*(?:20)?18)?"
    ),
    "cc": (
        r"codigo\s+civil|cc(?:\s*/\s*(?:20)?02)?"
        rf"|{_LAW_NUMBER}10\.?406(?:\s*/\s*(?:20)?02)?"
    ),
}
_LAW_ALTERNATIVES = "|".join(f"(?P<{name}>{pattern})" for name, pattern in _LAWS.items())
_CLAUSE_RE = re.compile(
    # "o § 1º do art. 18 do CDC": subdivisions may come first, outermost last.
    rf"(?:(?P<prefix>{_SUBDIVISION}(?:(?:{_SEPARATOR}|{_OF})(?:{_SUBDIVISION}|{_ROMAN})){_MORE})"
    rf"{_OF})?"
    rf"{_ARTICLE_WORD}\s*(?P<body>{_NUMBER}{_ORDINAL}(?:{_SEPARATOR}{_PIECE}){_MORE})"
    rf"(?:\s*,\s*|\s+)(?:d[oa]s?\s+)?(?:{_LAW_ALTERNATIVES})(?![a-z])"
)
_TOKEN_RE = re.compile(
    rf"(?P<paragraphs>{_PARAGRAPHS})|(?P<paragraph>{_PARAGRAPH})|(?P<incisos>{_INCISOS})"
    rf"|(?P<alineas>{_ALINEAS})|(?P<caput>caput)|(?P<roman>{_ROMAN})|(?P<article>{_NUMBER})"
)
_KEYWORD_RE = re.compile(r"^(?:§§|§|paragrafos|paragrafo|par\.|incisos?|alineas?)\s*")
_NUMBER_KEY_RE = re.compile(r"(\d{1,4}(?:\.\d{3})?)(?:\s*-\s*([a-z]))?")
_OF_RE = re.compile(_OF)
# The hierarchy level each token fills, outermost first.
_LEVELS = ("paragraph", "inciso", "alinea")
_LEVEL_OF_TOKEN = {
    "paragraphs": "paragraph",
    "paragraph": "paragraph",
    "caput": "paragraph",
    "incisos": "inciso",
    "roman": "inciso",
    "alineas": "alinea",
}


@dataclass(frozen=True, slots=True)
class ConsumerCitation:
    """A provision the consumer cited, resolved to the official text a notice quotes."""

    provision: LegalProvision
    unit: LegalTextUnit
    # The indexed official chunk of ``unit``: what the citation quotes.
    chunk: Chunk
    # The article was cited without a subdivision; ``unit`` is its caput.
    whole_article: bool


@dataclass(frozen=True, slots=True)
class _Reference:
    article: str
    # "caput" when named explicitly ("art. 35, caput").
    paragraph: str | None = None
    inciso: str | None = None
    alinea: str | None = None

    @property
    def whole(self) -> bool:
        return self.paragraph is None and self.inciso is None and self.alinea is None


def consumer_cited_provisions(
    corpus: LegalCorpus, facts: ConsumerCaseFacts
) -> list[ConsumerCitation]:
    """Citable units the consumer named, in order of first mention.

    The complaint is read before the remedy. An article cited whole yields to
    a unit of the same article cited elsewhere, and repeats collapse.
    """

    articles = {
        (provision.law_id, provision.article_key): provision
        for provision in corpus.retrievable_provisions()
        if provision.status is ProvisionStatus.ACTIVE
    }
    citations: list[ConsumerCitation] = []
    for law_id, reference in _cited_references(facts):
        provision = articles.get((law_id, reference.article))
        citation = _resolve(corpus, provision, reference) if provision is not None else None
        if citation is not None:
            citations.append(citation)
    return _deduplicated(citations)


def _cited_references(facts: ConsumerCaseFacts) -> list[tuple[str, _Reference]]:
    found: list[tuple[str, _Reference]] = []
    for text in (facts.complaint_summary, facts.desired_resolution):
        for match in _CLAUSE_RE.finditer(_fold(text or "")):
            law_id = next(_LAW_IDS[name] for name in _LAW_IDS if match.group(name))
            references = _references(match.group("body"), match.group("prefix"))
            found.extend((law_id, reference) for reference in references)
    return found


def _references(body: str, prefix: str | None) -> list[_Reference]:
    """Every unit one clause names, each refining the article before it."""

    references: list[_Reference] = []
    for kind, text in _tokens(body):
        if kind == "article":
            references.append(_Reference(_number_key(text)))
        else:  # the body opens with its article number, so one precedes this
            _attach_all(references, kind, text)
    if prefix:
        # "inciso II do § 3º do art. 54-F": the last "do" segment is outermost.
        scoped = references[:1]
        for segment in reversed(_OF_RE.split(prefix)):
            for kind, text in _tokens(segment):
                _attach_all(scoped, kind, text)
        references[:1] = scoped
    return references


def _tokens(text: str) -> list[tuple[str, str]]:
    return [(str(match.lastgroup), match.group()) for match in _TOKEN_RE.finditer(text)]


def _attach_all(references: list[_Reference], kind: str, text: str) -> None:
    level = _LEVEL_OF_TOKEN[kind]
    for value in _token_values(kind, text):
        _attach(references, level, value)


def _token_values(kind: str, text: str) -> list[str]:
    if kind == "caput":
        return ["caput"]
    items = re.split(_LIST, _KEYWORD_RE.sub("", text))
    if kind in ("paragraphs", "paragraph"):
        return [item if item == "unico" else _number_key(item) for item in items]
    return [item.strip("\"'“”‘’") for item in items]


def _attach(references: list[_Reference], level: str, value: str) -> None:
    """Fill ``level`` of the last reference, or start a sibling that shares its parents."""

    last = references[-1]
    depth = _LEVELS.index(level)
    if all(getattr(last, name) is None for name in _LEVELS[depth:]):
        references[-1] = replace(last, **{level: value})
        return
    parents = {name: getattr(last, name) for name in _LEVELS[:depth]}
    references.append(replace(_Reference(last.article), **parents, **{level: value}))


def _resolve(
    corpus: LegalCorpus, provision: LegalProvision, reference: _Reference
) -> ConsumerCitation | None:
    unit = _cited_unit(provision, reference)
    if unit is None:
        return None
    chunk = corpus.citation_chunk(provision, unit)
    if chunk is None:  # inactive, an amendment, or otherwise never indexed
        return None
    # ADR 0022: a unit that states the supplier's defense is never quoted on
    # the consumer's behalf, even when the consumer names it.
    if alias_exclusion_reason(unit.unit_id, corpus.alias_source_block(provision, unit)):
        return None
    return ConsumerCitation(
        provision=provision, unit=unit, chunk=chunk, whole_article=reference.whole
    )


def _cited_unit(provision: LegalProvision, reference: _Reference) -> LegalTextUnit | None:
    """The unit at the deepest level the reference names; the caput when it names none."""

    paragraph = None if reference.paragraph == "caput" else reference.paragraph
    if reference.alinea is not None:
        kind = LegalUnitKind.ALINEA
    elif reference.inciso is not None:
        kind = LegalUnitKind.INCISO
    elif paragraph is not None:
        kind = LegalUnitKind.PARAGRAPH
    else:
        kind = LegalUnitKind.CAPUT
    return next(
        (
            unit
            for unit in provision.units
            if unit.kind is kind
            and unit.paragraph == paragraph
            and unit.inciso == reference.inciso
            and unit.alinea == reference.alinea
        ),
        None,
    )


def _deduplicated(citations: list[ConsumerCitation]) -> list[ConsumerCitation]:
    by_unit = {
        citation.provision.provision_id for citation in citations if not citation.whole_article
    }
    seen: set[str] = set()
    kept: list[ConsumerCitation] = []
    for citation in citations:
        if citation.whole_article and citation.provision.provision_id in by_unit:
            continue
        if citation.unit.unit_id in seen:
            continue
        seen.add(citation.unit.unit_id)
        kept.append(citation)
    return kept


def _number_key(text: str) -> str:
    """``1.022`` -> ``1022``; ``54 - F`` -> ``54-f``; ``06`` -> ``6``."""

    match = _NUMBER_KEY_RE.match(text)
    if match is None:  # pragma: no cover - callers pass text the token regex matched
        raise ValueError(f"not an article or paragraph number: {text!r}")
    digits = str(int(match.group(1).replace(".", "")))
    return f"{digits}-{match.group(2)}" if match.group(2) else digits


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())
