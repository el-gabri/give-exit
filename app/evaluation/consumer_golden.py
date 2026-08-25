"""Loading for the Consumer legal-retrieval golden dataset.

Its input is a lay complaint and its labels are stable CDC/CF article or
subdivision ids, so corpus re-chunking does not invalidate the judgments.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.schemas.evaluation import ConsumerLegalGoldenDataset

DEFAULT_DATASET_FILENAME = "dataset.json"


def load_consumer_legal_dataset(
    path: Path,
    *,
    corpus: LegalCorpus | None = None,
) -> ConsumerLegalGoldenDataset:
    """Load and validate a Consumer legal-retrieval dataset.

    ``path`` may point directly to the JSON file or to its dedicated directory.
    Validation rejects duplicate, overlapping or malformed stable ids before a
    model bake-off starts.
    """

    dataset_path = path / DEFAULT_DATASET_FILENAME if path.is_dir() else path
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Consumer legal golden not found: {dataset_path}")

    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Consumer legal golden root must be a JSON object")
    dataset = ConsumerLegalGoldenDataset.model_validate(payload)
    validate_consumer_legal_labels(dataset, corpus=corpus)
    return dataset


def validate_consumer_legal_labels(
    dataset: ConsumerLegalGoldenDataset,
    *,
    corpus: LegalCorpus | None = None,
) -> LegalCorpus:
    """Fail closed when a golden label does not resolve in the pinned corpus.

    Relevant labels must resolve to active articles/subdivisions. Hard negatives
    may intentionally point at inactive text, but still have to exist. Returning
    the corpus lets the runner reuse the exact release and hash that were checked.
    """

    effective_corpus = corpus or get_default_legal_corpus()
    if (
        dataset.target_corpus_release_id is not None
        and dataset.target_corpus_release_id != effective_corpus.release_id
    ):
        raise ValueError("golden target corpus release does not match the loaded corpus")
    if (
        dataset.target_corpus_sha256 is not None
        and dataset.target_corpus_sha256 != effective_corpus.corpus_sha256
    ):
        raise ValueError("golden target corpus hash does not match the loaded corpus")
    articles = {provision.provision_id: provision for provision in effective_corpus.provisions}
    units = {
        unit.unit_id: unit for provision in effective_corpus.provisions for unit in provision.units
    }
    known_ids = {*articles, *units}
    errors: list[str] = []

    for case in dataset.cases:
        for judgment in case.relevant:
            article = articles.get(judgment.article_id)
            if article is None:
                errors.append(f"{case.case_id}: unknown relevant article {judgment.article_id}")
                continue
            if _status_value(article.status) != "active":
                errors.append(
                    f"{case.case_id}: relevant article {judgment.article_id} is "
                    f"{_status_value(article.status)}"
                )
            if judgment.unit_id is None:
                continue
            unit = units.get(judgment.unit_id)
            if unit is None:
                errors.append(f"{case.case_id}: unknown relevant unit {judgment.unit_id}")
            elif _status_value(unit.status) != "active":
                errors.append(
                    f"{case.case_id}: relevant unit {judgment.unit_id} is "
                    f"{_status_value(unit.status)}"
                )
        for hard_negative in case.hard_negatives:
            if hard_negative not in known_ids:
                errors.append(f"{case.case_id}: unknown hard negative {hard_negative}")

    if errors:
        preview = "; ".join(errors[:10])
        remainder = len(errors) - 10
        suffix = f"; and {remainder} more" if remainder > 0 else ""
        raise ValueError(f"golden labels do not match corpus: {preview}{suffix}")
    return effective_corpus


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()
