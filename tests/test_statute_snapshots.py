"""Pinned official snapshots: integrity, manifests and CDC identity."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.consumer.statutes import (
    CDC,
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    load_manifest,
    load_statute,
    manifest_path,
    snapshot_path,
)

# Computed with the former app/consumer/cdc_snapshot.py parser before its removal.
CDC_LEGACY_DIGEST = "bf4b5660a266627354364867f9b7fbd590f7b679ad2ec21266da03a95c452ccb"


def _legacy_digest(articles: Sequence[ParsedArticle]) -> str:
    payload = [
        {
            "provision_id": article.provision_id,
            "article_key": article.article_key,
            "article_label": article.article_label,
            "title": article.title,
            "chapter": article.chapter,
            "section": article.section,
            "official_text": article.official_text,
            "status": article.status.value,
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "kind": unit.kind.value,
                    "label": unit.label,
                    "text": unit.text,
                    "paragraph": unit.paragraph,
                    "inciso": unit.inciso,
                    "alinea": unit.alinea,
                    "status": unit.status.value,
                }
                for unit in article.units
            ],
        }
        for article in articles
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _copy_cdc(tmp_path: Path) -> Path:
    target = tmp_path / CDC.directory
    target.mkdir()
    shutil.copy(manifest_path(CDC), target / "manifest.json")
    shutil.copy(snapshot_path(CDC), target / CDC.snapshot_file)
    return target


def _rewrite_manifest(directory: Path, **changes: object) -> None:
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_cdc_manifest_is_versioned_and_integrity_checked() -> None:
    manifest = load_manifest(CDC)

    assert manifest.schema_version == 3
    assert manifest.release_id == "br-cdc-official-2026-08-04-v1"
    assert manifest.retrieved_on.isoformat() == "2026-08-04"
    assert manifest.parser_version == STATUTE_PARSER_VERSION
    assert manifest.acquisition_method == "download_https"
    assert manifest.http_user_agent is None
    assert manifest.review_status == "engineering_validated"
    assert manifest.parsed_text_sha256 == (
        "ecc3fcb838ad85fb54aaae9b70b7f3cd851cdd66da14018eccaab32af402b5c9"
    )
    assert snapshot_path(CDC).stat().st_size == 169_132
    assert hashlib.sha256(snapshot_path(CDC).read_bytes()).hexdigest() == (
        manifest.snapshot_sha256
    )
    assert manifest.snapshot_sha256 == (
        "bbfa64a79067ad3edd6b4dfff46cf905c85a44b2d5d8b2ac058a6a8855f13ef8"
    )


def test_generic_parser_reproduces_the_cdc_byte_for_byte() -> None:
    articles = load_statute(CDC).articles

    assert len(articles) == 130
    assert _legacy_digest(articles) == CDC_LEGACY_DIGEST
    assert all(
        article.part is None
        and article.book is None
        and article.subtitle is None
        and article.subsection is None
        for article in articles
    )


def test_tampered_snapshot_bytes_are_rejected(tmp_path: Path) -> None:
    directory = _copy_cdc(tmp_path)
    (directory / CDC.snapshot_file).write_bytes(snapshot_path(CDC).read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="integrity check failed"):
        load_statute(CDC, data_dir=tmp_path)


def test_extracted_text_must_match_the_manifest(tmp_path: Path) -> None:
    directory = _copy_cdc(tmp_path)
    _rewrite_manifest(directory, parsed_text_sha256="0" * 64)

    with pytest.raises(ValueError, match="extracted text does not match"):
        load_statute(CDC, data_dir=tmp_path)


def test_manifests_must_match_their_statute_and_be_promoted(tmp_path: Path) -> None:
    directory = _copy_cdc(tmp_path)
    _rewrite_manifest(directory, review_status="pending_review")

    with pytest.raises(ValueError, match="not been promoted"):
        load_manifest(CDC, data_dir=tmp_path)
    assert load_manifest(CDC, data_dir=tmp_path, allow_pending=True).review_status == (
        "pending_review"
    )
    _rewrite_manifest(directory, review_status="engineering_validated", law_id="br-lgpd")
    with pytest.raises(ValueError, match="law_id does not match"):
        load_manifest(CDC, data_dir=tmp_path)


def test_missing_files_and_malformed_manifests_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="manifest not found"):
        load_manifest(CDC, data_dir=tmp_path)
    directory = _copy_cdc(tmp_path)
    (directory / CDC.snapshot_file).unlink()
    with pytest.raises(RuntimeError, match="snapshot not found"):
        load_statute(CDC, data_dir=tmp_path)
    (directory / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an object"):
        load_manifest(CDC, data_dir=tmp_path)
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        load_manifest(CDC, data_dir=tmp_path)
