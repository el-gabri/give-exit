"""Integrity and governance checks for committed demo PDFs."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_RECORD_KINDS = {"public_judicial_record", "synthetic_fixture"}


def _demo_pdf_paths(root: Path) -> set[str]:
    """Return all demo PDFs present in a checkout."""

    demo_dir = root / "demo"
    if not demo_dir.is_dir():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in demo_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pdf"
    }


def test_demo_pdf_manifest_is_complete_current_and_reviewed() -> None:
    _check_demo_manifest(ROOT)


def _check_demo_manifest(root: Path) -> None:
    """Every committed demo PDF is manifested, current and reviewed.

    With no demo PDF committed there is nothing to govern, and no manifest is
    needed; a PDF added back without one fails.
    """

    pdfs = _demo_pdf_paths(root)
    manifest_path = root / "demo" / "manifest.json"
    if not manifest_path.exists():
        assert not pdfs, f"demo PDFs without demo/manifest.json: {sorted(pdfs)}"
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert (
        manifest["policy"][
            "public_access_establishes_unrestricted_redistribution_rights"
        ]
        is False
    )

    entries = manifest["files"]
    manifest_paths = [entry["path"] for entry in entries]
    assert len(manifest_paths) == len(set(manifest_paths)), "duplicate manifest path"
    assert set(manifest_paths) == pdfs, (
        "Every demo PDF must be manifested, and stale entries must be removed"
    )

    for entry in entries:
        path = root / entry["path"]
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["sha256"] == actual_hash, f"hash drift for {entry['path']}"

        assert entry["record_kind"] in ALLOWED_RECORD_KINDS
        assert entry["synthetic"] is (
            entry["record_kind"] == "synthetic_fixture"
        )
        assert isinstance(entry["contains_personal_data"], bool)
        assert isinstance(entry["contains_personal_data_like_content"], bool)

        provenance = entry["provenance"]
        assert "source_url" in provenance
        assert provenance["access_status"].strip()
        assert provenance["basis"].strip()
        assert provenance["notes"].strip()

        review = entry["review"]
        assert review["status"].strip()
        date.fromisoformat(review["reviewed_on"])
        assert review["review_basis"].strip()
        assert review["redistribution_status"].strip()
        assert review["notes"].strip()

        if entry["contains_personal_data"]:
            assert review["status"] == "reviewed_with_restrictions"
            assert review["redistribution_status"] in {
                "not_established",
                "restricted",
            }
            assert provenance["source_url"] is None or provenance[
                "source_url"
            ].strip()


def test_no_demo_pdfs_need_no_manifest(tmp_path: Path) -> None:
    # The demo directory was deleted (ed1c004); with no committed PDF there is
    # nothing to govern.
    _check_demo_manifest(tmp_path)


def test_a_demo_pdf_without_a_manifest_still_fails(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "peticao.pdf").write_bytes(b"%PDF-1.4")

    with pytest.raises(AssertionError, match="demo/peticao.pdf"):
        _check_demo_manifest(tmp_path)
