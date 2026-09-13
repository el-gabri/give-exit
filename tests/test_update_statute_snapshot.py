"""The maintainer refresher: provenance, change detection and review gating."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.consumer.statutes import CDC, StatuteParseError, load_manifest, snapshot_path
from app.consumer.update_statute_snapshot import (
    BROWSER_USER_AGENT,
    main,
    refresh_snapshot,
)

CDC_TEXT_SHA256 = "ecc3fcb838ad85fb54aaae9b70b7f3cd851cdd66da14018eccaab32af402b5c9"
RETRIEVED_ON = date(2026, 9, 12)


def _cdc_bytes() -> bytes:
    return snapshot_path(CDC).read_bytes()


def _manifest(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / CDC.directory / "manifest.json").read_text(encoding="utf-8"))


def test_local_refresh_requires_a_note_and_writes_a_pending_manifest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="acquisition-note"):
        refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, source_file=snapshot_path(CDC))

    result = refresh_snapshot(
        CDC,
        RETRIEVED_ON,
        data_dir=tmp_path,
        source_file=snapshot_path(CDC),
        acquisition_note="Cópia baixada do endereço oficial e conferida manualmente.",
    )

    payload = _manifest(tmp_path)
    assert result.changed and result.article_count == 130
    assert payload["release_id"] == "br-cdc-official-2026-09-12-v1"
    assert payload["acquisition_method"] == "local_file"
    assert payload["http_user_agent"] is None
    assert payload["parsed_text_sha256"] == CDC_TEXT_SHA256
    assert payload["review_status"] == "pending_review"
    assert (tmp_path / CDC.directory / CDC.snapshot_file).read_bytes() == _cdc_bytes()
    with pytest.raises(ValueError, match="not been promoted"):
        load_manifest(CDC, data_dir=tmp_path)


def test_unchanged_text_writes_nothing_unless_forced(tmp_path: Path) -> None:
    def fetch(url: str, user_agent: str) -> tuple[bytes, str | None, str | None, str | None]:
        # Planalto's WAF appends a random script after </html> on every response.
        return _cdc_bytes() + b"<script>var token = 'random';</script>", url, None, None

    first = refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, fetch=fetch)
    before = (tmp_path / CDC.directory / "manifest.json").read_bytes()
    second = refresh_snapshot(CDC, date(2026, 9, 13), data_dir=tmp_path, fetch=fetch)
    forced = refresh_snapshot(CDC, date(2026, 9, 13), data_dir=tmp_path, fetch=fetch, force=True)

    assert first.changed and not second.changed and forced.changed
    assert second.parsed_text_sha256 == CDC_TEXT_SHA256
    assert before != (tmp_path / CDC.directory / "manifest.json").read_bytes()


def test_download_records_the_user_agent_and_http_metadata(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fetch(url: str, user_agent: str) -> tuple[bytes, str | None, str | None, str | None]:
        calls.append((url, user_agent))
        return _cdc_bytes(), url, '"etag-1"', "Thu, 23 Apr 2026 23:33:33 GMT"

    refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, fetch=fetch)

    payload = _manifest(tmp_path)
    assert calls == [(CDC.source_url, BROWSER_USER_AGENT)]
    assert payload["acquisition_method"] == "download_https"
    assert payload["http_user_agent"] == BROWSER_USER_AGENT
    assert payload["http_etag"] == '"etag-1"'
    assert payload["final_url"] == CDC.source_url


def test_empty_and_incomplete_documents_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="empty document"):
        refresh_snapshot(
            CDC,
            RETRIEVED_ON,
            data_dir=tmp_path,
            fetch=lambda url, ua: (b"", url, None, None),
        )
    truncated = _cdc_bytes()[: len(_cdc_bytes()) // 2]
    with pytest.raises(StatuteParseError):
        refresh_snapshot(
            CDC,
            RETRIEVED_ON,
            data_dir=tmp_path,
            fetch=lambda url, ua: (truncated, url, None, None),
        )
    assert not (tmp_path / CDC.directory / "manifest.json").exists()


def test_cli_refreshes_a_registered_law_into_a_chosen_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "--law", "cdc",
        "--source-file", str(snapshot_path(CDC)),
        "--acquisition-note", "Cópia local conferida.",
        "--retrieved-on", "2026-09-12",
        "--data-dir", str(tmp_path),
    ]

    assert main(arguments) == 0
    assert "pending_review" in capsys.readouterr().out
    assert main(arguments) == 0
    assert "Sem alteração textual" in capsys.readouterr().out
