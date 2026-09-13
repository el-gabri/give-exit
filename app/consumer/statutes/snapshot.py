"""Pinned official snapshots and the manifests that make them auditable.

Production never downloads law: it verifies a local copy. The raw bytes are
pinned by SHA-256, and so is the extracted text, because Planalto's WAF adds a
random script after ``</html>`` to every response and makes raw hashes differ
between two downloads of an unchanged law.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.consumer.statutes.parser import (
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    extract_paragraphs,
    parse_statute,
    parsed_text_sha256,
)
from app.consumer.statutes.spec import StatuteSpec

MANIFEST_SCHEMA_VERSION = 3
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ACQUISITION_METHODS = frozenset({"download_https", "local_file"})
_REVIEW_STATUSES = frozenset({"pending_review", "engineering_validated", "legal_reviewed"})


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Provenance of one pinned statute snapshot."""

    schema_version: int
    release_id: str
    law_id: str
    source_url: str
    retrieved_on: date
    encoding: str
    snapshot_file: str
    snapshot_sha256: str
    parsed_text_sha256: str
    parser_version: str
    acquisition_method: str
    acquisition_note: str | None
    final_url: str | None
    http_etag: str | None
    http_last_modified: str | None
    http_user_agent: str | None
    refresh_tool_version: str
    review_status: str

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        spec: StatuteSpec,
        *,
        allow_pending: bool = False,
    ) -> SnapshotManifest:
        missing = sorted({item.name for item in dataclasses.fields(cls)} - value.keys())
        if missing:
            raise ValueError(f"{spec.law_id} manifest is missing fields: {missing}")

        def optional(name: str) -> str | None:
            item = value[name]
            return None if item is None else str(item)

        manifest = cls(
            schema_version=int(value["schema_version"]),
            release_id=str(value["release_id"]),
            law_id=str(value["law_id"]),
            source_url=str(value["source_url"]),
            retrieved_on=date.fromisoformat(str(value["retrieved_on"])),
            encoding=str(value["encoding"]),
            snapshot_file=str(value["snapshot_file"]),
            snapshot_sha256=str(value["snapshot_sha256"]),
            parsed_text_sha256=str(value["parsed_text_sha256"]),
            parser_version=str(value["parser_version"]),
            acquisition_method=str(value["acquisition_method"]),
            acquisition_note=optional("acquisition_note"),
            final_url=optional("final_url"),
            http_etag=optional("http_etag"),
            http_last_modified=optional("http_last_modified"),
            http_user_agent=optional("http_user_agent"),
            refresh_tool_version=str(value["refresh_tool_version"]),
            review_status=str(value["review_status"]),
        )
        manifest._validate(spec, allow_pending=allow_pending)
        return manifest

    def to_mapping(self) -> dict[str, Any]:
        mapping = dataclasses.asdict(self)
        mapping["retrieved_on"] = self.retrieved_on.isoformat()
        return mapping

    def _validate(self, spec: StatuteSpec, *, allow_pending: bool) -> None:
        problems = (
            (self.schema_version != MANIFEST_SCHEMA_VERSION, "unsupported manifest schema"),
            (self.law_id != spec.law_id, "manifest law_id does not match the statute"),
            (self.source_url != spec.source_url, "manifest does not point to the official URL"),
            (self.snapshot_file != spec.snapshot_file, "manifest names another snapshot file"),
            (self.encoding != spec.encoding, "manifest encoding does not match the statute"),
            (not _SHA256_RE.fullmatch(self.snapshot_sha256), "invalid snapshot SHA-256"),
            (not _SHA256_RE.fullmatch(self.parsed_text_sha256), "invalid parsed-text SHA-256"),
            (
                self.parser_version != STATUTE_PARSER_VERSION,
                "parser version does not match runtime",
            ),
            (self.acquisition_method not in _ACQUISITION_METHODS, "invalid acquisition method"),
            (
                self.acquisition_method == "local_file" and not self.acquisition_note,
                "local snapshots require an acquisition note",
            ),
            (self.review_status not in _REVIEW_STATUSES, "invalid review status"),
            (
                self.review_status == "pending_review" and not allow_pending,
                "snapshot has not been promoted after review",
            ),
        )
        for failed, message in problems:
            if failed:
                raise ValueError(f"{spec.law_id}: {message}")


@dataclass(frozen=True, slots=True)
class LoadedStatute:
    """A verified snapshot and its parsed articles."""

    spec: StatuteSpec
    manifest: SnapshotManifest
    articles: tuple[ParsedArticle, ...]


def manifest_path(spec: StatuteSpec, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / spec.directory / "manifest.json"


def snapshot_path(spec: StatuteSpec, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / spec.directory / spec.snapshot_file


def load_manifest(
    spec: StatuteSpec,
    data_dir: Path = DATA_DIR,
    *,
    allow_pending: bool = False,
) -> SnapshotManifest:
    path = manifest_path(spec, data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{spec.law_id} snapshot manifest not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{spec.law_id} manifest root must be an object")
    return SnapshotManifest.from_mapping(raw, spec, allow_pending=allow_pending)


def load_statute(spec: StatuteSpec, data_dir: Path = DATA_DIR) -> LoadedStatute:
    """Verify the pinned bytes and extracted text, then parse every article."""

    manifest = load_manifest(spec, data_dir)
    path = snapshot_path(spec, data_dir)
    try:
        source = path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{spec.law_id} snapshot not found: {path}") from exc
    actual = hashlib.sha256(source).hexdigest()
    if actual != manifest.snapshot_sha256:
        raise ValueError(
            f"{spec.law_id} snapshot integrity check failed: "
            f"expected {manifest.snapshot_sha256}, got {actual}"
        )
    paragraphs = extract_paragraphs(source.decode(manifest.encoding))
    if parsed_text_sha256(paragraphs) != manifest.parsed_text_sha256:
        raise ValueError(f"{spec.law_id} extracted text does not match the manifest")
    return LoadedStatute(spec=spec, manifest=manifest, articles=parse_statute(spec, paragraphs))
