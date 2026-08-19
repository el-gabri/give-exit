"""Regression thresholds for the document security gate.

Thresholds are set at the measured values, so a rule change that quietly
weakens detection - or starts flagging ordinary legal language - fails the
build instead of going unnoticed.
"""

import pytest

from app.evaluation.security_benchmark import (
    BenchmarkReport,
    load_cases,
    run_benchmark,
)

# Measured on the deterministic rules, 2026-08-19. Raise these when the gate
# genuinely improves; never lower one to make a failing build pass.
MIN_ATTACK_RECALL = 0.75
MAX_FALSE_POSITIVE_RATE = 0.0
# Obfuscation handling is fully deterministic, so anything below perfect here
# is a real regression rather than sampling noise.
PERFECT_TECHNIQUES = frozenset(
    {
        "base64",
        "direct_english",
        "direct_portuguese",
        "external_transfer",
        "fake_system_marker",
        "homoglyph",
        "html_comment",
        "page_boundary_split",
        "zero_width",
    }
)


@pytest.fixture(scope="module")
def report() -> BenchmarkReport:
    return run_benchmark(load_cases())


def test_dataset_covers_attacks_and_benign_controls() -> None:
    cases = load_cases()
    assert sum(case.is_attack for case in cases) >= 20
    assert sum(not case.is_attack for case in cases) >= 10


def test_attack_recall_meets_the_measured_floor(report: BenchmarkReport) -> None:
    assert report.recall >= MIN_ATTACK_RECALL, (
        f"recall dropped to {report.recall:.3f}; missed {report.attacks.missed}"
    )


def test_ordinary_legal_language_is_never_flagged(report: BenchmarkReport) -> None:
    assert report.false_positive_rate <= MAX_FALSE_POSITIVE_RATE, (
        f"benign passages flagged: {report.benign.missed}"
    )


def test_obfuscation_and_direct_attacks_stay_fully_detected(
    report: BenchmarkReport,
) -> None:
    for technique in sorted(PERFECT_TECHNIQUES):
        group = report.by_technique.get(technique)
        assert group is not None, f"benchmark lost coverage for {technique}"
        assert group.rate == 1.0, f"{technique} regressed: missed {group.missed}"


def test_paraphrase_evasion_is_tracked_as_a_known_gap(
    report: BenchmarkReport,
) -> None:
    """Lexical rules catch no paraphrased attack.

    If this ever starts passing, the rules improved and the recorded gap in
    the README should be updated.
    """
    paraphrase = report.by_technique["paraphrase_evasion"]
    assert paraphrase.total >= 5
    assert paraphrase.rate < MIN_ATTACK_RECALL


def test_rules_missed_attacks_still_reach_the_semantic_reviewer(
    report: BenchmarkReport,
) -> None:
    """This is what actually makes `balanced` stronger than `rules`.

    Balanced mode reviews only forwarded candidates, so an attack the rules
    miss is invisible to the reviewer unless the candidate selector escalates
    it. Without that, choosing `balanced` would buy nothing on paraphrases.
    """
    assert report.escalated_misses.rate >= 0.8, (
        f"attacks invisible to both stages: {report.escalated_misses.missed}"
    )


def test_escalation_does_not_flood_the_reviewer_with_ordinary_text(
    report: BenchmarkReport,
) -> None:
    """Escalation costs tokens, so it must stay rare on benign documents."""
    assert report.benign_escalated.rate <= 0.15, (
        f"too much benign text escalated: {report.benign_escalated.missed}"
    )
