"""Maintainer CLI: refresh the pinned snapshot of one official statute.

Runtime code never calls this module. A refresh is an explicit, reviewable
operation: download (or read a local copy), parse and validate completeness,
hash, then atomically replace the snapshot and its manifest. New snapshots are
written as ``pending_review``; promote them to ``engineering_validated`` only
after comparing a sample of articles with the official page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.consumer.statutes import (
    DATA_DIR,
    MANIFEST_SCHEMA_VERSION,
    STATUTE_PARSER_VERSION,
    STATUTES,
    StatuteSpec,
    extract_paragraphs,
    load_manifest,
    manifest_path,
    parse_statute,
    parsed_text_sha256,
    snapshot_path,
)

REFRESH_TOOL_VERSION = "statute-snapshot-refresh-v1"
# Planalto's WAF resets connections that do not look like a browser. The exact
# string is recorded in the manifest, so the acquisition stays transparent.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

Fetcher = Callable[[str, str], tuple[bytes, str | None, str | None, str | None]]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    changed: bool
    snapshot_path: Path
    manifest_path: Path
    article_count: int
    parsed_text_sha256: str


def refresh_snapshot(
    spec: StatuteSpec,
    retrieved_on: date,
    *,
    data_dir: Path = DATA_DIR,
    source_file: Path | None = None,
    acquisition_note: str | None = None,
    user_agent: str = BROWSER_USER_AGENT,
    force: bool = False,
    fetch: Fetcher | None = None,
) -> RefreshResult:
    note: str | None
    agent: str | None
    if source_file is None:
        source, final_url, etag, last_modified = (fetch or _download)(spec.source_url, user_agent)
        method, note, agent = "download_https", None, user_agent
    else:
        if not acquisition_note or not acquisition_note.strip():
            raise ValueError("--source-file requires a non-empty --acquisition-note")
        source = source_file.read_bytes()
        final_url = etag = last_modified = None
        method, note, agent = "local_file", acquisition_note.strip(), None
    if not source:
        raise RuntimeError(f"{spec.law_id}: Planalto returned an empty document")

    paragraphs = extract_paragraphs(source.decode(spec.encoding))
    articles = parse_statute(spec, paragraphs)
    text_hash = parsed_text_sha256(paragraphs)
    target_manifest = manifest_path(spec, data_dir)
    target_snapshot = snapshot_path(spec, data_dir)
    if not force and target_manifest.is_file():
        current = load_manifest(spec, data_dir, allow_pending=True)
        if current.parsed_text_sha256 == text_hash:
            return RefreshResult(False, target_snapshot, target_manifest, len(articles), text_hash)

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_id": f"{spec.law_id}-official-{retrieved_on.isoformat()}-v1",
        "law_id": spec.law_id,
        "source_url": spec.source_url,
        "retrieved_on": retrieved_on.isoformat(),
        "encoding": spec.encoding,
        "snapshot_file": spec.snapshot_file,
        "snapshot_sha256": hashlib.sha256(source).hexdigest(),
        "parsed_text_sha256": text_hash,
        "parser_version": STATUTE_PARSER_VERSION,
        "acquisition_method": method,
        "acquisition_note": note,
        "final_url": final_url,
        "http_etag": etag,
        "http_last_modified": last_modified,
        "http_user_agent": agent,
        "refresh_tool_version": REFRESH_TOOL_VERSION,
        "review_status": "pending_review",
    }
    target_snapshot.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target_snapshot, source)
    _atomic_write(
        target_manifest,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return RefreshResult(True, target_snapshot, target_manifest, len(articles), text_hash)


def _download(
    url: str, user_agent: str
) -> tuple[bytes, str | None, str | None, str | None]:  # pragma: no cover - network
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return (
            response.read(),
            response.geturl(),
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
        )


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    laws = {spec.law_id.removeprefix("br-"): spec for spec in STATUTES}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--law", required=True, choices=sorted(laws))
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=date.today())
    parser.add_argument("--source-file", type=Path, help="pin an already downloaded copy")
    parser.add_argument("--acquisition-note", help="required provenance note for --source-file")
    parser.add_argument("--user-agent", default=BROWSER_USER_AGENT)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--force", action="store_true", help="write even when the extracted text is unchanged"
    )
    args = parser.parse_args(argv)
    spec = laws[args.law]
    result = refresh_snapshot(
        spec,
        args.retrieved_on,
        data_dir=args.data_dir,
        source_file=args.source_file,
        acquisition_note=args.acquisition_note,
        user_agent=args.user_agent,
        force=args.force,
    )
    if not result.changed:
        print(
            f"Sem alteração textual em {spec.law_id} ({result.parsed_text_sha256}); "
            "nada gravado."
        )
        return 0
    print(f"{spec.law_id}: {result.article_count} artigos · texto {result.parsed_text_sha256}")
    print(result.snapshot_path)
    print(result.manifest_path)
    print(
        "Manifesto gravado como pending_review: confira artigos por amostragem "
        "antes de promover."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
