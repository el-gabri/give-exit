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
from app.consumer.statutes.registry import CDC, CIVIL_CODE, LGPD, STATUTES
from app.consumer.statutes.snapshot import (
    DATA_DIR,
    MANIFEST_SCHEMA_VERSION,
    LoadedStatute,
    SnapshotManifest,
    load_manifest,
    load_statute,
    manifest_path,
    snapshot_path,
)
from app.consumer.statutes.spec import DivisionSelector, StatuteSpec, TextCorrection

__all__ = [
    "CDC",
    "CIVIL_CODE",
    "DATA_DIR",
    "HIERARCHY_LEVELS",
    "LGPD",
    "MANIFEST_SCHEMA_VERSION",
    "STATUTE_PARSER_VERSION",
    "STATUTES",
    "DivisionSelector",
    "LoadedStatute",
    "ParsedArticle",
    "SnapshotManifest",
    "StatuteParseError",
    "StatuteSpec",
    "TextCorrection",
    "division_numeral",
    "extract_paragraphs",
    "in_index_scope",
    "load_manifest",
    "load_statute",
    "manifest_path",
    "match_article_heading",
    "parse_statute",
    "parsed_text_sha256",
    "snapshot_path",
]
