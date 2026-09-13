"""Declarative description of one official statute snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.consumer.schemas import LegalSource


@dataclass(frozen=True, slots=True)
class TextCorrection:
    """An exact fix for a typographic quirk of an official page.

    ``find`` must prefix exactly one extracted paragraph. A correction that no
    longer applies fails the parse, so a page Planalto has fixed is noticed and
    reviewed instead of silently changing the text.
    """

    find: str
    replace: str
    reason: str


@dataclass(frozen=True, slots=True)
class DivisionSelector:
    """A path of (level, numeral) pairs, e.g. (("parte", "especial"), ("livro", "i"))."""

    path: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class StatuteSpec:
    """What a snapshot of one statute must contain, and how to read it."""

    law_id: str
    source: LegalSource
    source_name: str
    citation_prefix: str
    source_url: str
    directory: str
    snapshot_file: str
    encoding: str
    last_article: int
    expected_article_count: int
    required_articles: frozenset[str] = frozenset()
    known_absent: Mapping[int, str] = field(default_factory=lambda: MappingProxyType({}))
    text_corrections: tuple[TextCorrection, ...] = ()
    # ``None`` indexes the whole statute; otherwise only matching divisions.
    index_scope: tuple[DivisionSelector, ...] | None = None
