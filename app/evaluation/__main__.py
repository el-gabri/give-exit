"""CLI entry point: ``python -m app.evaluation [golden_dir] [gates]``.

Runs the evaluation suite with the configured provider and prints a summary
table. With LITIGATION_LLM_PROVIDER=mock it exercises the full pipeline
offline; with a real provider it measures actual quality, including the LLM
judge.

Gates turn the report into a build check. Offline, retrieval ranking and
pipeline health are the meaningful signals: the mock provider writes
placeholder text, so groundedness and extraction accuracy are structurally
zero and must not be gated in CI.

    python -m app.evaluation eval_data --require-no-errors \
        --min retrieval_recall@3=1.0
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.config import LLMProvider, get_settings
from app.core.logging import configure_logging
from app.evaluation.golden import load_dataset
from app.evaluation.runner import EvaluationRunner
from app.llm.factory import create_llm_client
from app.orchestration.graph import build_analysis_graph
from app.rag.factory import create_rag_pipeline
from app.schemas.evaluation import EvaluationSummary
from app.security import PromptInjectionDetector


def _threshold(raw: str) -> tuple[str, float]:
    name, separator, value = raw.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"expected metric=value, got {raw!r}")
    try:
        return name.strip(), float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation")
    parser.add_argument(
        "golden_dir", nargs="?", default="eval_data", type=Path, help="golden case directory"
    )
    parser.add_argument(
        "--min",
        dest="minimums",
        action="append",
        default=[],
        type=_threshold,
        metavar="METRIC=VALUE",
        help="fail if the metric average falls below VALUE (repeatable)",
    )
    parser.add_argument(
        "--max",
        dest="maximums",
        action="append",
        default=[],
        type=_threshold,
        metavar="METRIC=VALUE",
        help="fail if the metric average rises above VALUE (repeatable)",
    )
    parser.add_argument(
        "--require-no-errors",
        action="store_true",
        help="fail if any golden case reported a pipeline error",
    )
    return parser.parse_args(argv)


def check_gates(summary: EvaluationSummary, args: argparse.Namespace) -> list[str]:
    """Return one message per violated gate; empty means the run passed."""
    violations: list[str] = []
    if args.require_no_errors and summary.failed_case_count:
        failing = [case.case_name for case in summary.cases if case.errors]
        violations.append(f"{summary.failed_case_count} case(s) reported errors: {failing}")
    for name, floor in args.minimums:
        actual = summary.averages.get(name)
        if actual is None:
            violations.append(f"{name}: not produced by this run, cannot gate on it")
        elif actual < floor:
            violations.append(f"{name}: {actual:.3f} < required {floor:.3f}")
    for name, ceiling in args.maximums:
        actual = summary.averages.get(name)
        if actual is None:
            violations.append(f"{name}: not produced by this run, cannot gate on it")
        elif actual > ceiling:
            violations.append(f"{name}: {actual:.3f} > allowed {ceiling:.3f}")
    return violations


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    llm = create_llm_client(settings)
    rag = create_rag_pipeline(settings)
    graph = build_analysis_graph(
        llm,
        rag,
        prompt_injection_detector=PromptInjectionDetector(
            llm,
            mode=settings.prompt_injection_scan_mode,
            strict_max_chars=settings.prompt_injection_strict_max_chars,
            strict_max_batches=settings.prompt_injection_strict_max_batches,
        ),
    )
    judge_llm = llm if settings.llm_provider is not LLMProvider.MOCK else None

    runner = EvaluationRunner(graph, judge_llm=judge_llm, rag=rag)
    summary = await runner.run(load_dataset(args.golden_dir))

    print("\n=== Evaluation summary ===")
    for name, score in sorted(summary.averages.items()):
        print(f"{name:>24}: {score:.3f}")
    print(f"{'cases':>24}: {len(summary.cases)}")
    for case in summary.cases:
        status = "ERRORS" if case.errors else "ok"
        print(f"\n[{status}] {case.case_name}")
        for metric in case.metrics:
            print(f"    {metric.name:>24}: {metric.score:.3f}  {metric.details}")

    violations = check_gates(summary, args)
    if violations:
        print("\n=== Gate failures ===")
        for violation in violations:
            print(f"  FAIL {violation}")
        return 1
    if args.minimums or args.maximums or args.require_no_errors:
        print("\nAll evaluation gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
