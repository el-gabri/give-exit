"""Parse official compiled statutes published by Planalto.

The pages are legacy HTML: each rendered paragraph is a ``<p>``, and headings
and article headings must be recognized from text. The rules here are the same
for every statute; what differs between laws (last article, known gaps, page
quirks, index scope) is declared in a ``StatuteSpec``. Parsing fails closed:
a paragraph the rules cannot place is an error, never silently dropped or glued
to the wrong article.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.consumer.schemas import LegalTextUnit, LegalUnitKind, ProvisionStatus
from app.consumer.statutes.spec import StatuteSpec

STATUTE_PARSER_VERSION = "planalto-statute-parser-v1"
HIERARCHY_LEVELS = ("part", "book", "title", "subtitle", "chapter", "section", "subsection")

_LEVEL_BY_KEYWORD = {
    "PARTE": "part",
    "LIVRO": "book",
    "TITULO": "title",
    "SUBTITULO": "subtitle",
    "CAPITULO": "chapter",
    "SECAO": "section",
    "SUBSECAO": "subsection",
}
# Applied to accent-stripped text, so "TITULO" also matches the page's "TÍTULO".
_HEADING_RE = re.compile(
    r"^(?P<keyword>PARTE|LIVRO|TITULO|SUBTITULO|CAPITULO|SECAO|SUBSECAO)\s+"
    r"(?P<numeral>[IVXLCDM]+(?:-[A-Z])?|UNIC[AO]|GERAL|ESPECIAL|COMPLEMENTAR)(?=\s|$)",
    re.IGNORECASE,
)
# "Art. 1º", "Art. 3 o", "Art. 1.358-A." and "Art. 759.(Revogado..." (CC).
_ARTICLE_RE = re.compile(
    r"^Art\.\s*(?P<number>\d{1,3}(?:\.\d{3})+|\d+)(?:\s?[º°o](?=[\s.(]))?"
    r"(?:-(?P<suffix>[A-Z]))?\.?(?=[\s(]|$)",
    re.IGNORECASE,
)
_INLINE_NOTE_RE = re.compile(
    r"\((?:reda[cç][aã]o\s+dada|inclu[ií]d[oa]|acrescentad[oa]|revogad[oa]|vide|"
    r"vig[eê]ncia|produ[cç][aã]o\s+de\s+efeito|regulamentad[oa])[^)]*\)|\bVig[eê]ncia\b",
    re.IGNORECASE,
)
_TERMINATOR_RE = re.compile(r"^Bras[ií]lia\s*,", re.IGNORECASE)
_DIVISION_NUMERAL = r"([ivxlcdm]+(?:-[a-z])?|unic[ao]|geral|especial|complementar)\b"

_PARAGRAPH_RE = re.compile(
    r"^(?:(?P<unique>Parágrafo\s+único)|§\s*(?P<number>\d+(?:-[A-Z])?)"
    r"(?:\s*(?:[º°oª]\.?|\.\s*[º°oª]?))?)\.?(?=\s)",
    re.IGNORECASE,
)
_INCISO_RE = re.compile(r"^(?P<label>[IVXLCDM]+)\s*[-–—](?=\s)")
_ALINEA_RE = re.compile(r"^(?P<label>[a-z])\s*[)](?=\s)", re.IGNORECASE)
_PENALTY_RE = re.compile(r"^Pena\b", re.IGNORECASE)
_QUOTED_AMENDMENT_RE = re.compile(
    r'^["“”«]\s*(?:Art\.\s*\d+|§\s*\d+|[IVXLCDM]+\s*[-–—])',
    re.IGNORECASE,
)
_EDITORIAL_NOTE_RE = re.compile(
    r"^\((?:redação\s+dada|incluíd[oa]|acrescentad[oa]|revogad[oa]|vide|vigência|"
    r"produção\s+de\s+efeito|regulamentad[oa])\b",
    re.IGNORECASE,
)
_VETOED_RE = re.compile(r"\(\s*VETAD[OA]\s*\)", re.IGNORECASE)
_REVOKED_RE = re.compile(r"\(\s*REVOGAD[OA][^)]*\)", re.IGNORECASE)


class StatuteParseError(ValueError):
    """A snapshot does not match its statute specification."""


@dataclass(frozen=True, slots=True)
class ParsedArticle:
    """One top-level article, its hierarchy and its addressable units."""

    number: int
    suffix: str | None
    article_key: str
    article_label: str
    provision_id: str
    part: str | None
    book: str | None
    title: str | None
    subtitle: str | None
    chapter: str | None
    section: str | None
    subsection: str | None
    official_text: str
    status: ProvisionStatus
    units: tuple[LegalTextUnit, ...]


@dataclass(slots=True)
class _Draft:
    number: int
    suffix: str | None
    hierarchy: dict[str, str | None]
    blocks: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _ParagraphBuilder:
    fragments: list[str] = field(default_factory=list)


class _OfficialHtmlTextParser(HTMLParser):
    """Extract rendered paragraphs from the legacy Planalto markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._stack: list[_ParagraphBuilder] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if lowered == "p":
            self._stack.append(_ParagraphBuilder())
        elif lowered == "br" and self._stack:
            self._stack[-1].fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth or lowered != "p" or not self._stack:
            return
        builder = self._stack.pop()
        paragraph = _normalize_text("".join(builder.fragments))
        if paragraph:
            self.paragraphs.append(paragraph)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and self._stack:
            self._stack[-1].fragments.append(data)


def extract_paragraphs(html: str) -> list[str]:
    parser = _OfficialHtmlTextParser()
    parser.feed(html)
    parser.close()
    return parser.paragraphs


def parsed_text_sha256(paragraphs: Sequence[str]) -> str:
    """Hash of the extracted text; stable across downloads of the same law."""

    return hashlib.sha256("\n".join(paragraphs).encode("utf-8")).hexdigest()


def match_article_heading(paragraph: str) -> tuple[int, str | None] | None:
    match = _ARTICLE_RE.match(paragraph)
    if match is None:
        return None
    suffix = match.group("suffix")
    return int(match.group("number").replace(".", "")), suffix.upper() if suffix else None


def strip_article_heading(text: str) -> str:
    """The text of a caput without its ``Art. N`` heading."""

    match = _ARTICLE_RE.match(text)
    return text[match.end() :].lstrip() if match else text


def parse_statute(spec: StatuteSpec, paragraphs: Sequence[str]) -> tuple[ParsedArticle, ...]:
    """Parse and validate every top-level article of one statute."""

    hierarchy: dict[str, str | None] = dict.fromkeys(HIERARCHY_LEVELS)
    drafts: list[_Draft] = []
    current: _Draft | None = None
    open_level: str | None = None
    named = True
    for paragraph in _apply_corrections(spec, paragraphs):
        heading = _HEADING_RE.match(_strip_accents(paragraph))
        if heading:
            level = _LEVEL_BY_KEYWORD[_strip_accents(heading.group("keyword")).upper()]
            _set_level(hierarchy, level, paragraph)
            current, open_level, named = None, level, not _is_bare_heading(paragraph)
            continue
        article = match_article_heading(paragraph)
        if article is not None:
            previous = (drafts[-1].number, drafts[-1].suffix) if drafts else None
            if not _is_next_article(article[0], article[1], previous, spec.known_absent):
                raise StatuteParseError(
                    f"{spec.law_id}: article heading out of sequence after "
                    f"{_describe(previous)}: {paragraph[:80]!r}"
                )
            current = _Draft(article[0], article[1], dict(hierarchy), [paragraph])
            drafts.append(current)
            open_level = None
            continue
        if (
            current is not None
            and current.number == spec.last_article
            and _TERMINATOR_RE.match(paragraph)
        ):
            break
        if open_level is not None:
            # Between a heading and the first article of its division.
            named = _extend_heading(spec, hierarchy, open_level, paragraph, named=named)
            continue
        if current is not None:
            current.blocks.append(paragraph)
    else:
        raise StatuteParseError(f"{spec.law_id}: the signature after the last article is missing")
    articles = tuple(_build_article(spec, draft) for draft in drafts)
    _validate_completeness(spec, articles)
    return articles


def division_numeral(label: str | None, keyword: str) -> str:
    """Extract ``i-a`` from ``TÍTULO I-A (Incluído…) DA EMPRESA…`` for ``titulo``."""

    if not label:
        return ""
    match = re.match(rf"{keyword}\s+{_DIVISION_NUMERAL}", _strip_accents(label).casefold())
    return match.group(1) if match else ""


def in_index_scope(spec: StatuteSpec, article: ParsedArticle) -> bool:
    if spec.index_scope is None:
        return True
    divisions = {
        "parte": division_numeral(article.part, "parte"),
        "livro": division_numeral(article.book, "livro"),
        "titulo": division_numeral(article.title, "titulo"),
        "capitulo": division_numeral(article.chapter, "capitulo"),
    }
    return any(
        all(divisions.get(level) == value for level, value in selector.path)
        for selector in spec.index_scope
    )


def _apply_corrections(spec: StatuteSpec, paragraphs: Sequence[str]) -> list[str]:
    corrected = list(paragraphs)
    for correction in spec.text_corrections:
        positions = [i for i, text in enumerate(corrected) if text.startswith(correction.find)]
        if len(positions) != 1:
            raise StatuteParseError(
                f"{spec.law_id}: correction {correction.find!r} matched "
                f"{len(positions)} paragraphs; expected exactly one"
            )
        index = positions[0]
        corrected[index] = correction.replace + corrected[index][len(correction.find) :]
    return corrected


def _is_next_article(
    number: int,
    suffix: str | None,
    previous: tuple[int, str | None] | None,
    known_absent: Mapping[int, str],
) -> bool:
    if previous is None:
        return number == 1 and suffix is None
    previous_number, previous_suffix = previous
    if suffix is None:
        expected = previous_number + 1
        while expected in known_absent:
            expected += 1
        return number == expected
    if number != previous_number:
        return False
    if previous_suffix is None:
        return suffix == "A"
    return ord(suffix) == ord(previous_suffix) + 1


def _describe(previous: tuple[int, str | None] | None) -> str:
    if previous is None:
        return "the start"
    number, suffix = previous
    return f"art. {number}" if suffix is None else f"art. {number}-{suffix}"


def _set_level(hierarchy: dict[str, str | None], level: str, label: str) -> None:
    index = HIERARCHY_LEVELS.index(level)
    hierarchy[level] = label
    for lower in HIERARCHY_LEVELS[index + 1 :]:
        hierarchy[lower] = None


def _extend_heading(
    spec: StatuteSpec,
    hierarchy: dict[str, str | None],
    level: str,
    paragraph: str,
    *,
    named: bool,
) -> bool:
    """Attach the name of an open heading; return whether it is named now.

    Editorial notes between a heading and its first article are skipped. A
    bare heading ("Seção I") takes the next paragraph as its name; any heading
    also absorbs an uppercase continuation ("LIVRO I" + "DAS PESSOAS").
    """

    if _EDITORIAL_NOTE_RE.match(paragraph):
        return named
    if _is_uppercase(paragraph) or not named:
        label = hierarchy[level]
        hierarchy[level] = f"{label} {paragraph}" if label else paragraph
        return True
    raise StatuteParseError(
        f"{spec.law_id}: unexpected paragraph after heading "
        f"{hierarchy[level]!r}: {paragraph[:80]!r}"
    )


def _is_bare_heading(paragraph: str) -> bool:
    without_notes = " ".join(_INLINE_NOTE_RE.sub(" ", _strip_accents(paragraph)).split())
    match = _HEADING_RE.match(without_notes)
    return match is not None and match.end() == len(without_notes)


def _is_uppercase(paragraph: str) -> bool:
    letters = [character for character in paragraph if character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


def _build_article(spec: StatuteSpec, draft: _Draft) -> ParsedArticle:
    number, suffix = draft.number, draft.suffix
    article_key = str(number) if suffix is None else f"{number}-{suffix.lower()}"
    label_number = f"{number:,}".replace(",", ".")
    article_label = f"art. {label_number}" if suffix is None else f"art. {label_number}-{suffix}"
    provision_id = f"{spec.law_id}-art-{article_key}"
    units = _build_units(provision_id, draft.blocks)
    statuses = {unit.status for unit in units if unit.kind is not LegalUnitKind.NOTE}
    if statuses == {ProvisionStatus.VETOED}:
        status = ProvisionStatus.VETOED
    elif statuses == {ProvisionStatus.REVOKED}:
        status = ProvisionStatus.REVOKED
    else:
        status = ProvisionStatus.ACTIVE
    return ParsedArticle(
        number=number,
        suffix=suffix,
        article_key=article_key,
        article_label=article_label,
        provision_id=provision_id,
        part=draft.hierarchy["part"],
        book=draft.hierarchy["book"],
        title=draft.hierarchy["title"],
        subtitle=draft.hierarchy["subtitle"],
        chapter=draft.hierarchy["chapter"],
        section=draft.hierarchy["section"],
        subsection=draft.hierarchy["subsection"],
        official_text="\n\n".join(draft.blocks),
        status=status,
        units=units,
    )


def _validate_completeness(spec: StatuteSpec, articles: tuple[ParsedArticle, ...]) -> None:
    # Strict sequencing plus the signature check already prove numbers 1..last
    # minus the declared absences; what remains are suffixes and the total.
    keys = {article.article_key for article in articles}
    if not spec.required_articles <= keys:
        raise StatuteParseError(
            f"{spec.law_id}: missing suffixed articles {sorted(spec.required_articles - keys)}"
        )
    if len(articles) != spec.expected_article_count:
        raise StatuteParseError(
            f"{spec.law_id}: {len(articles)} articles, expected {spec.expected_article_count}"
        )


def _build_units(provision_id: str, blocks: list[str]) -> tuple[LegalTextUnit, ...]:
    units: list[LegalTextUnit] = []
    used_ids: set[str] = set()
    current_paragraph: str | None = None
    current_inciso: str | None = None
    unstructured_counts: dict[LegalUnitKind, int] = {}

    for position, text in enumerate(blocks):
        paragraph_match = _PARAGRAPH_RE.match(text)
        inciso_match = _INCISO_RE.match(text)
        alinea_match = _ALINEA_RE.match(text)
        paragraph: str | None = current_paragraph
        inciso: str | None = current_inciso
        alinea: str | None = None

        if position == 0:
            kind = LegalUnitKind.CAPUT
            label = "caput"
            fragment = "caput"
            current_paragraph = None
            current_inciso = None
            paragraph = None
            inciso = None
        elif paragraph_match:
            kind = LegalUnitKind.PARAGRAPH
            number = paragraph_match.group("number")
            paragraph = "unico" if paragraph_match.group("unique") else str(number).lower()
            label = "parágrafo único" if paragraph == "unico" else f"§ {number}"
            fragment = f"paragrafo-{paragraph}"
            current_paragraph = paragraph
            current_inciso = None
            inciso = None
        elif inciso_match:
            kind = LegalUnitKind.INCISO
            inciso = inciso_match.group("label").lower()
            label = f"inciso {inciso.upper()}"
            prefix = f"paragrafo-{paragraph}-" if paragraph else ""
            fragment = f"{prefix}inciso-{inciso}"
            current_inciso = inciso
        elif alinea_match:
            kind = LegalUnitKind.ALINEA
            alinea = alinea_match.group("label").lower()
            label = f"alínea {alinea}"
            paragraph_prefix = f"paragrafo-{paragraph}-" if paragraph else ""
            inciso_prefix = f"inciso-{inciso}-" if inciso else ""
            fragment = f"{paragraph_prefix}{inciso_prefix}alinea-{alinea}"
        else:
            kind = _unstructured_unit_kind(text)
            unstructured_counts[kind] = unstructured_counts.get(kind, 0) + 1
            ordinal = unstructured_counts[kind]
            label_prefix, fragment_prefix = {
                LegalUnitKind.PENALTY: ("pena", "pena"),
                LegalUnitKind.QUOTED_AMENDMENT: (
                    "alteração legal transcrita",
                    "alteracao-citada",
                ),
                LegalUnitKind.NOTE: ("nota editorial", "nota"),
                LegalUnitKind.NORMATIVE_OTHER: (
                    "bloco normativo",
                    "normativo",
                ),
            }[kind]
            label = f"{label_prefix} {ordinal}"
            fragment = f"{fragment_prefix}-{ordinal:03d}"
            if kind is LegalUnitKind.QUOTED_AMENDMENT:
                paragraph, inciso = _quoted_unit_hierarchy(text)

        base_unit_id = f"{provision_id}-{fragment}"
        unit_id = base_unit_id
        duplicate = 2
        while unit_id in used_ids:
            unit_id = f"{base_unit_id}-{duplicate}"
            duplicate += 1
        used_ids.add(unit_id)
        units.append(
            LegalTextUnit(
                unit_id=unit_id,
                kind=kind,
                label=label,
                text=text,
                paragraph=paragraph,
                inciso=inciso,
                alinea=alinea,
                status=_status_for_text(text),
            )
        )
    return tuple(units)


def _unstructured_unit_kind(text: str) -> LegalUnitKind:
    """Classify official normative blocks before falling back to editorial notes."""

    if _PENALTY_RE.match(text):
        return LegalUnitKind.PENALTY
    if _QUOTED_AMENDMENT_RE.match(text):
        return LegalUnitKind.QUOTED_AMENDMENT
    if _EDITORIAL_NOTE_RE.match(text):
        return LegalUnitKind.NOTE
    return LegalUnitKind.NORMATIVE_OTHER


def _quoted_unit_hierarchy(text: str) -> tuple[str | None, str | None]:
    unquoted = text.lstrip('"“”« ').strip()
    paragraph_match = _PARAGRAPH_RE.match(unquoted)
    if paragraph_match:
        number = paragraph_match.group("number")
        paragraph = "unico" if paragraph_match.group("unique") else str(number).lower()
        return paragraph, None
    inciso_match = _INCISO_RE.match(unquoted)
    if inciso_match:
        return None, inciso_match.group("label").lower()
    return None, None


def _status_for_text(text: str) -> ProvisionStatus:
    if _REVOKED_RE.search(text):
        return ProvisionStatus.REVOKED
    if _VETOED_RE.search(text):
        return ProvisionStatus.VETOED
    return ProvisionStatus.ACTIVE


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\xa0", " "))
    return " ".join(normalized.split())


def _strip_accents(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", value)
        if unicodedata.category(character) != "Mn"
    )
