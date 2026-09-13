"""Official statute snapshots: specification, parsing and integrity."""

from app.consumer.statutes.parser import (
    HIERARCHY_LEVELS,
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    StatuteParseError,
    division_numeral,
    extract_paragraphs,
    in_index_scope,
    match_article_heading,
    parse_statute,
    parsed_text_sha256,
)
from app.consumer.statutes.spec import DivisionSelector, StatuteSpec, TextCorrection

__all__ = [
    "HIERARCHY_LEVELS",
    "STATUTE_PARSER_VERSION",
    "DivisionSelector",
    "ParsedArticle",
    "StatuteParseError",
    "StatuteSpec",
    "TextCorrection",
    "division_numeral",
    "extract_paragraphs",
    "in_index_scope",
    "match_article_heading",
    "parse_statute",
    "parsed_text_sha256",
]
