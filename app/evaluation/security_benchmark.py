"""Offline recall/false-positive measurement for the document security gate.

The gate is the boundary the rest of the trust story rests on, so its
effectiveness should be a measured number rather than an assumption. This
scores the deterministic rules against labeled adversarial and benign
passages: no LLM, no network, fully reproducible.

Deterministic rules are the floor. ``balanced`` and ``strict`` modes add a
semantic reviewer that can only add findings, never remove a rule finding, so
their recall is at least what this reports.

Run it with::

    python -m app.evaluation.security_benchmark
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.security.prompt_injection import _semantic_candidates, scan_prompt_injection_rules

DEFAULT_DATASET = Path("eval_data/security/injection_benchmark.json")


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    kind: str
    pages: tuple[str, ...]
    category: str | None = None
    technique: str | None = None

    @property
    def is_attack(self) -> bool:
        return self.kind == "attack"


@dataclass
class GroupResult:
    """Detection outcome for one slice of the dataset."""

    total: int = 0
    detected: int = 0
    missed: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.detected / self.total if self.total else 0.0

    def record_hit(self, case_id: str, *, detected: bool) -> None:
        """Count a case that should be detected; list it when it is not."""
        self.total += 1
        if detected:
            self.detected += 1
        else:
            self.missed.append(case_id)

    def record_false_alarm(self, case_id: str, *, detected: bool) -> None:
        """Count a case that should pass; list it when it is detected anyway."""
        self.total += 1
        if detected:
            self.detected += 1
            self.missed.append(case_id)


@dataclass
class BenchmarkReport:
    attacks: GroupResult
    benign: GroupResult
    by_category: dict[str, GroupResult]
    by_technique: dict[str, GroupResult]
    # Balanced mode only reviews what the candidate selector forwards, so an
    # attack the rules miss is invisible to the reviewer unless it lands here.
    escalated_misses: GroupResult = field(default_factory=GroupResult)
    benign_escalated: GroupResult = field(default_factory=GroupResult)

    @property
    def recall(self) -> float:
        return self.attacks.rate

    @property
    def false_positive_rate(self) -> float:
        """Fraction of benign passages the rules incorrectly flagged."""
        return self.benign.rate

    def render(self) -> str:
        lines = [
            "Prompt-injection gate - deterministic rules only",
            "",
            f"  attack recall        : {self.recall:.3f} "
            f"({self.attacks.detected}/{self.attacks.total} detected)",
            f"  false-positive rate  : {self.false_positive_rate:.3f} "
            f"({self.benign.detected}/{self.benign.total} benign passages flagged)",
            "",
            "  Balanced mode - what the semantic reviewer gets to see",
            f"    rules-missed attacks escalated : {self.escalated_misses.rate:.3f} "
            f"({self.escalated_misses.detected}/{self.escalated_misses.total})",
            f"    benign text escalated (cost)   : {self.benign_escalated.rate:.3f} "
            f"({self.benign_escalated.detected}/{self.benign_escalated.total})",
            "",
            "  Recall by category",
        ]
        for name, group in sorted(self.by_category.items()):
            lines.append(f"    {name:<22} {group.rate:.3f}  ({group.detected}/{group.total})")
        lines.append("")
        lines.append("  Recall by evasion technique")
        for name, group in sorted(self.by_technique.items()):
            lines.append(f"    {name:<22} {group.rate:.3f}  ({group.detected}/{group.total})")
        if self.attacks.missed:
            lines.extend(
                [
                    "",
                    "  Known gaps (attacks the rules do not catch):",
                    *(f"    - {case_id}" for case_id in self.attacks.missed),
                ]
            )
        if self.benign.missed:
            lines.extend(
                [
                    "",
                    "  False positives:",
                    *(f"    - {case_id}" for case_id in self.benign.missed),
                ]
            )
        return "\n".join(lines)


def load_cases(path: Path = DEFAULT_DATASET) -> list[BenchmarkCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        BenchmarkCase(
            case_id=item["id"],
            kind=item["kind"],
            pages=tuple(item["pages"]),
            category=item.get("category"),
            technique=item.get("technique"),
        )
        for item in payload["cases"]
    ]
    if not cases:
        raise ValueError(f"no benchmark cases found in {path}")
    seen = Counter(case.case_id for case in cases)
    duplicates = sorted(case_id for case_id, count in seen.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate benchmark case ids: {duplicates}")
    return cases


def _document(case: BenchmarkCase) -> ParsedDocument:
    return ParsedDocument(
        filename=f"{case.case_id}.pdf",
        pages=[
            DocumentPage(number=index, text=text)
            for index, text in enumerate(case.pages, start=1)
        ],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )


def run_benchmark(cases: list[BenchmarkCase]) -> BenchmarkReport:
    attacks = GroupResult()
    benign = GroupResult()
    escalated_misses = GroupResult()
    benign_escalated = GroupResult()
    by_category: dict[str, GroupResult] = defaultdict(GroupResult)
    by_technique: dict[str, GroupResult] = defaultdict(GroupResult)

    for case in cases:
        document = _document(case)
        findings = scan_prompt_injection_rules(document)
        flagged = bool(findings)
        if not case.is_attack:
            # For benign passages "detected" counts false positives, and an
            # escalation is review cost spent on ordinary text.
            benign.record_false_alarm(case.case_id, detected=flagged)
            benign_escalated.record_false_alarm(
                case.case_id, detected=bool(_semantic_candidates(document, findings))
            )
            continue
        attacks.record_hit(case.case_id, detected=flagged)
        if not flagged:
            escalated_misses.record_hit(
                case.case_id, detected=bool(_semantic_candidates(document, findings))
            )
        for bucket, key in ((by_category, case.category), (by_technique, case.technique)):
            if key is not None:
                bucket[key].record_hit(case.case_id, detected=flagged)

    return BenchmarkReport(
        attacks=attacks,
        benign=benign,
        by_category=dict(by_category),
        by_technique=dict(by_technique),
        escalated_misses=escalated_misses,
        benign_escalated=benign_escalated,
    )


def main() -> None:
    report = run_benchmark(load_cases())
    print(report.render())


if __name__ == "__main__":
    main()
