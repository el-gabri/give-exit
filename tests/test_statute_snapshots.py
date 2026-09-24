"""Pinned official snapshots: integrity, manifests and CDC identity."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.consumer.schemas import ProvisionStatus
from app.consumer.statutes import (
    CDC,
    CIVIL_CODE,
    LGPD,
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    in_index_scope,
    load_manifest,
    load_statute,
    manifest_path,
    snapshot_path,
)

# Computed with the former app/consumer/cdc_snapshot.py parser before its removal.
CDC_LEGACY_DIGEST = "bf4b5660a266627354364867f9b7fbd590f7b679ad2ec21266da03a95c452ccb"
LGPD_TEXT_SHA256 = "8232caaef569007b54fa8f38678d5087d2e1f916af6298e2a3654f0416498b27"
CC_TEXT_SHA256 = "cb60b801d600a771f539cf94be7d7990bafb5829924f1e753e4b5224db276802"


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


def test_lgpd_snapshot_parses_completely() -> None:
    loaded = load_statute(LGPD)
    statuses = Counter(article.status for article in loaded.articles)
    by_key = {article.article_key: article for article in loaded.articles}

    assert loaded.manifest.parsed_text_sha256 == LGPD_TEXT_SHA256
    assert loaded.manifest.review_status == "engineering_validated"
    assert len(loaded.articles) == 80
    assert sum(len(article.units) for article in loaded.articles) == 464
    assert statuses == {
        ProvisionStatus.ACTIVE: 73,
        ProvisionStatus.VETOED: 6,
        ProvisionStatus.REVOKED: 1,
    }
    assert {
        article.provision_id
        for article in loaded.articles
        if article.status is ProvisionStatus.VETOED
    } == {f"br-lgpd-art-{number}" for number in ("28", "55", "56", "57", "58", "59")}
    assert by_key["55-b"].status is ProvisionStatus.REVOKED
    assert (by_key["55-j"].chapter or "").startswith(
        "CAPÍTULO IX DA AGÊNCIA NACIONAL DE PROTEÇÃO DE DADOS"
    )


def test_civil_code_snapshot_parses_completely_and_scopes_the_index() -> None:
    loaded = load_statute(CIVIL_CODE)
    by_key = {article.article_key: article for article in loaded.articles}
    statuses = Counter(article.status for article in loaded.articles)
    in_scope = [article for article in loaded.articles if in_index_scope(CIVIL_CODE, article)]

    assert loaded.manifest.parsed_text_sha256 == CC_TEXT_SHA256
    assert loaded.manifest.review_status == "engineering_validated"
    assert len(loaded.articles) == 2083
    assert sum(len(article.units) for article in loaded.articles) == 3857
    assert statuses == {
        ProvisionStatus.ACTIVE: 2017,
        ProvisionStatus.REVOKED: 65,
        ProvisionStatus.VETOED: 1,
    }
    assert not set(range(1621, 1630)) & {article.number for article in loaded.articles}
    assert by_key["1620"].status is ProvisionStatus.REVOKED
    assert by_key["1636"].status is ProvisionStatus.ACTIVE
    assert by_key["759"].status is ProvisionStatus.REVOKED
    assert by_key["819-a"].status is ProvisionStatus.VETOED
    assert (by_key["1358-a"].provision_id, by_key["1358-a"].article_label) == (
        "br-cc-art-1358-a",
        "art. 1.358-A",
    )
    assert by_key["1"].part == "PARTE GERAL"
    assert by_key["927"].book == "LIVRO I DO DIREITO DAS OBRIGAÇÕES"
    assert (len(in_scope), sum(a.status is ProvisionStatus.ACTIVE for a in in_scope)) == (
        971,
        919,
    )
