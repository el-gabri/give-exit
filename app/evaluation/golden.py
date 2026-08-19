"""Golden dataset loading.

A golden case is a JSON file with the document text (per page) and the
expected labels. Text-based (not PDF) so cases are reviewable in a diff and
cheap to author - the ingestion layer has its own tests.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.services.citations import quote_matches


@dataclass(frozen=True)
class RetrievalJudgment:
    """One query and stable relevance labels for offline ranking evaluation."""

    query: str
    relevant_page_ranges: tuple[tuple[int, int], ...]
    relevant_passages: tuple[str, ...]


@dataclass(frozen=True)
class GoldenCase:
    name: str
    document: ParsedDocument
    expected: dict[str, Any]
    retrieval_k: int = 5
    retrieval_judgments: tuple[RetrievalJudgment, ...] = ()


def _load_retrieval(payload: dict[str, Any]) -> tuple[int, tuple[RetrievalJudgment, ...]]:
    retrieval = payload.get("retrieval")
    if retrieval is None:
        return 5, ()

    k = retrieval.get("k", 5)
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("retrieval.k must be a positive integer")

    raw_queries = retrieval.get("queries", [])
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("retrieval.queries must contain at least one judgment")

    judgments: list[RetrievalJudgment] = []
    for index, item in enumerate(raw_queries):
        query = item.get("query", "").strip()
        if not query:
            raise ValueError(f"retrieval.queries[{index}].query must not be empty")

        page_ranges: list[tuple[int, int]] = []
        for page_range in item.get("relevant_page_ranges", []):
            start = page_range.get("start")
            end = page_range.get("end")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 1
                or end < start
            ):
                raise ValueError(
                    f"retrieval.queries[{index}] has an invalid relevant page range"
                )
            page_ranges.append((start, end))

        passages = tuple(
            passage.strip()
            for passage in item.get("relevant_passages", [])
            if passage.strip()
        )
        if not page_ranges and not passages:
            raise ValueError(
                f"retrieval.queries[{index}] needs a page range or passage label"
            )
        judgments.append(
            RetrievalJudgment(
                query=query,
                relevant_page_ranges=tuple(page_ranges),
                relevant_passages=passages,
            )
        )
    return k, tuple(judgments)


def load_case(path: Path) -> GoldenCase:
    payload = json.loads(path.read_text(encoding="utf-8"))
    retrieval_k, retrieval_judgments = _load_retrieval(payload)
    document = ParsedDocument(
        filename=f"{payload['name']}.pdf",
        pages=[
            DocumentPage(number=i + 1, text=text)
            for i, text in enumerate(payload["pages"])
        ],
        language=payload.get("language", "pt"),
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    for index, judgment in enumerate(retrieval_judgments):
        if any(end > document.page_count for _, end in judgment.relevant_page_ranges):
            raise ValueError(
                f"retrieval.queries[{index}] references a page outside the document"
            )
        judged_text = "\n".join(
            page.text
            for page in document.pages
            if not judgment.relevant_page_ranges
            or any(
                start <= page.number <= end
                for start, end in judgment.relevant_page_ranges
            )
        )
        for passage in judgment.relevant_passages:
            if not quote_matches(passage, judged_text):
                raise ValueError(
                    f"retrieval.queries[{index}] passage does not occur in its pages"
                )
    return GoldenCase(
        name=payload["name"],
        document=document,
        expected=payload["expected"],
        retrieval_k=retrieval_k,
        retrieval_judgments=retrieval_judgments,
    )


def load_dataset(directory: Path) -> list[GoldenCase]:
    cases = [load_case(p) for p in sorted(directory.glob("*.json"))]
    if not cases:
        raise FileNotFoundError(f"No golden cases (*.json) found in {directory}")
    return cases
