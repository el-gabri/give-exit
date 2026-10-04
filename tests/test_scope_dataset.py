"""Dataset 2.4.0: the development cases the scope gate is measured on."""

from pathlib import Path

from app.evaluation.consumer_golden import load_consumer_legal_dataset

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")
NEW_OUT_OF_SCOPE_DOMAINS = {
    "state": 3,
    "tenancy": 4,
    "employment": 3,
    "family": 3,
    "private_parties": 3,
    "complainant_supplier": 2,
    "criminal": 1,
    "partnership": 1,
}


def test_new_out_of_scope_cases_cover_every_relationship_class() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    domains: dict[str, int] = {}
    for case in dataset.cases:
        domain = next((s.split(":", 1)[1] for s in case.slices if s.startswith("domain:")), None)
        if domain in NEW_OUT_OF_SCOPE_DOMAINS:
            assert case.split == "development"
            assert case.no_applicable_ground and not case.relevant
            domains[domain] = domains.get(domain, 0) + 1

    assert domains == NEW_OUT_OF_SCOPE_DOMAINS


def test_eight_in_scope_look_alikes_are_labelled_development_cases() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    look_alikes = [case for case in dataset.cases if "scope:lookalike" in case.slices]

    assert len(look_alikes) == 8
    assert all(case.split == "development" and case.relevant for case in look_alikes)
