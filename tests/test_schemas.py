"""Tests for domain schema behavior (not just field presence)."""

import pytest

from app.schemas.common import ConfidentConclusion
from app.schemas.lawsuit import LawsuitExtraction, Party, PartyRole


def test_confidence_outside_documented_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="confidence must be between"):
        ConfidentConclusion(statement="s", confidence=1.7, reasoning="r")
    with pytest.raises(ValueError, match="confidence must be between"):
        ConfidentConclusion(statement="s", confidence=-0.2, reasoning="r")


def test_valid_confidence_retains_percentage_view() -> None:
    conclusion = ConfidentConclusion(statement="s", confidence=0.87, reasoning="r")
    assert conclusion.confidence_pct == 87


def test_missing_fields_reports_absent_information() -> None:
    empty = LawsuitExtraction()
    assert "case_number" in empty.missing_fields()
    assert "parties" in empty.missing_fields()

    partial = LawsuitExtraction(
        case_number="0001234-56.2026.8.26.0100",
        parties=[Party(name="Maria Silva", role=PartyRole.PLAINTIFF)],
        main_requests=["indenizacao por danos morais"],
    )
    missing = partial.missing_fields()
    assert "case_number" not in missing
    assert "parties" not in missing
    assert "judge" in missing
