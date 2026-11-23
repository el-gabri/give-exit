"""The long-complaint probe set (ADR 0026): long versions of labelled development cases."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.retrieval import build_legal_query_plan
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.scope import is_consumer_scope
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.schemas.evaluation import ConsumerLegalGoldenCase

GOLDEN = Path("eval_data/consumer_legal_retrieval")
LONG = Path("eval_data/consumer_long_complaints")
_ARTICLE_RE = re.compile(r"\b(?:arts?\.|artigos?)\s*\d", re.IGNORECASE)


@pytest.fixture(scope="module")
def long_cases() -> tuple[ConsumerLegalGoldenCase, ...]:
    return load_consumer_legal_dataset(LONG, corpus=get_default_legal_corpus()).cases


def test_long_cases_derive_from_development_cases(
    long_cases: tuple[ConsumerLegalGoldenCase, ...],
) -> None:
    """Same labels and remedy as a development case; never a holdout one."""
    golden = {case.case_id: case for case in load_consumer_legal_dataset(GOLDEN).cases}

    assert len(long_cases) == 10
    for case in long_cases:
        original = golden[case.case_id.removesuffix("_longo")]
        assert case.case_id.endswith("_longo")
        assert original.split == case.split == "development"
        assert case.relevant == original.relevant
        assert case.hard_negatives == original.hard_negatives
        assert case.desired_resolution == original.desired_resolution
        assert case.category == original.category


def test_long_cases_are_long_and_in_scope(
    long_cases: tuple[ConsumerLegalGoldenCase, ...],
) -> None:
    for case in long_cases:
        facts = ConsumerCaseFacts(
            complaint_summary=case.complaint, desired_resolution=case.desired_resolution
        )
        assert build_legal_query_plan(facts).windows, case.case_id
        assert is_consumer_scope(complaint=case.complaint), case.case_id


def test_long_cases_cite_no_article(long_cases: tuple[ConsumerLegalGoldenCase, ...]) -> None:
    """A citation would reach the notice without retrieval and hide what retrieval does."""
    assert not [case.case_id for case in long_cases if _ARTICLE_RE.search(case.complaint)]
