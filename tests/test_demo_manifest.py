"""Integrity and governance checks for committed demo PDFs."""

import hashlib
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
MANIFEST_PATH = DEMO_DIR / "manifest.json"
ALLOWED_RECORD_KINDS = {"public_judicial_record", "synthetic_fixture"}


def _demo_pdf_paths() -> set[str]:
    """Return all demo PDFs present in a CI checkout."""

    return {
        path.relative_to(ROOT).as_posix()
        for path in DEMO_DIR.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pdf"
    }


def test_demo_pdf_manifest_is_complete_current_and_reviewed() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

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
    assert set(manifest_paths) == _demo_pdf_paths(), (
        "Every demo PDF must be manifested, and stale entries must be removed"
    )

    for entry in entries:
        path = ROOT / entry["path"]
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
