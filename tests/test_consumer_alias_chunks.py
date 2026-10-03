"""Alias chunks: one per aliased unit, validated against the statute."""

from __future__ import annotations

from functools import lru_cache

import pytest

from app.consumer.aliases import AliasEntry, AliasSet
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_policy import provision_is_eligible
from app.consumer.schemas import ProvisionStatus
from app.core.hashing import sha256_hex
from app.evaluation import label_ranks
from app.evaluation.consumer_runner import normalize_consumer_retrieval_hit
from app.schemas.evaluation import ConsumerLegalRelevance
from app.schemas.rag import Chunk, RetrievedChunk

FIRST = "A loja só me vendeu o celular se eu levasse a capinha junto."
SECOND = "O banco disse que só liberava o empréstimo se eu fizesse um seguro."


@lru_cache(maxsize=1)
def _base() -> LegalCorpus:
    return LegalCorpus(get_default_legal_corpus().provisions)


def _entry(
    provision_id: str,
    unit_key: str,
    *,
    status: str = "generated",
    source_sha256: str | None = None,
) -> AliasEntry:
    base = _base()
    provision = base.get(provision_id)
    unit = next((item for item in provision.units if item.unit_id == unit_key), None)
    return AliasEntry(
        unit_key=unit_key,
        provision_id=provision_id,
        source_sha256=source_sha256 or sha256_hex(base.alias_source_block(provision, unit)),
        aliases=(FIRST, SECOND),
        status=status,
    )


def _aliased(*entries: AliasEntry) -> LegalCorpus:
    return LegalCorpus(
        _base().provisions, aliases=AliasSet(prompt_version="test-prompt", entries=entries)
    )


def _aliases_of(corpus: LegalCorpus, **options: bool) -> list[Chunk]:
    return [
        chunk
        for chunk in corpus.as_chunks(**options)
        if chunk.metadata.get("chunk_level") == "alias"
    ]


def test_an_aliased_unit_gets_one_alias_chunk() -> None:
    corpus = _aliased(
        _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"),
        _entry("br-cf-art-5-xxxii", "br-cf-art-5-xxxii"),
    )

    # Sorted by id: the CDC unit's alias before the Constitution provision's.
    [unit_alias, provision_alias] = sorted(_aliases_of(corpus), key=lambda chunk: chunk.chunk_id)

    assert len(corpus.as_chunks()) == len(_base().as_chunks()) + 2
    assert unit_alias.chunk_id == f"{corpus.document_id}:legal:br-cdc-art-39-inciso-i:alias-01"
    assert unit_alias.text == f"{FIRST}\n{SECOND}"
    assert unit_alias.metadata["unit_id"] == "br-cdc-art-39-inciso-i"
    assert unit_alias.metadata["provision_id"] == "br-cdc-art-39"
    assert unit_alias.metadata["content_kind"] == "lay_alias"
    page = [item.provision_id for item in corpus.provisions].index("br-cdc-art-39") + 1
    assert (unit_alias.page_start, unit_alias.page_end) == (page, page)
    assert corpus.unit_for_chunk(unit_alias).unit_id == "br-cdc-art-39-inciso-i"  # type: ignore[union-attr]
    assert provision_alias.metadata["unit_id"] is None
    assert provision_alias.chunk_id.endswith(":legal:br-cf-art-5-xxxii:alias-01")


def test_rejected_inactive_and_uncitable_entries_make_no_chunk() -> None:
    base = _base()
    inactive_provision, inactive_unit = next(
        (provision, unit)
        for provision in base.provisions
        if provision.law_id == "br-cdc" and provision_is_eligible(provision)
        for unit in provision.units
        if unit.status is not ProvisionStatus.ACTIVE
    )
    uncitable = next(
        provision
        for provision in base.provisions
        if provision.law_id == "br-cdc" and provision.units and not provision_is_eligible(provision)
    )
    corpus = _aliased(
        _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i", status="rejected"),
        _entry(inactive_provision.provision_id, inactive_unit.unit_id),
        _entry(uncitable.provision_id, uncitable.units[0].unit_id),
    )

    assert _aliases_of(corpus) == []
    assert _aliases_of(corpus, include_inactive=True, include_uncitable=True) == []


def test_entries_outside_coverage_or_the_statute_are_refused() -> None:
    civil = next(
        provision
        for provision in _base().provisions
        if provision.law_id == "br-cc" and provision.units
    )
    with pytest.raises(ValueError, match="br-cc is outside alias coverage"):
        _aliased(_entry(civil.provision_id, civil.units[0].unit_id))
    unknown = _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i").model_copy(
        update={"unit_key": "br-cdc-art-39-inciso-xcix"}
    )
    with pytest.raises(ValueError, match="br-cdc-art-39-inciso-xcix: not a unit of br-cdc-art-39"):
        _aliased(unknown)


def test_a_stale_entry_fails_corpus_load_naming_it_and_the_fix() -> None:
    stale = _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i", source_sha256="0" * 64)

    with pytest.raises(ValueError, match=r"br-cdc-art-39-inciso-i.*generate_aliases"):
        _aliased(stale)


def test_the_corpus_hash_covers_aliases_but_an_empty_set_changes_nothing() -> None:
    base = _base()
    empty = LegalCorpus(base.provisions, aliases=AliasSet(prompt_version="test-prompt"))
    generated = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"))
    reviewed = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i", status="reviewed"))

    other_prompt = LegalCorpus(
        base.provisions,
        aliases=AliasSet(
            prompt_version="other-prompt",
            entries=(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"),),
        ),
    )

    assert empty.corpus_sha256 == base.corpus_sha256
    assert generated.corpus_sha256 != base.corpus_sha256
    assert reviewed.corpus_sha256 != generated.corpus_sha256
    assert other_prompt.corpus_sha256 != generated.corpus_sha256
    assert get_default_legal_corpus().aliases is not None


def test_evaluators_count_an_alias_hit_as_its_units_hit() -> None:
    corpus = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"))
    [alias] = _aliases_of(corpus)
    hit = RetrievedChunk(chunk=alias, score=0.03)
    label = ConsumerLegalRelevance(
        article_id="br-cdc-art-39", unit_id="br-cdc-art-39-inciso-i", grade=3, rationale="r"
    )

    normalized = normalize_consumer_retrieval_hit(hit)

    assert (normalized.provision_id, normalized.unit_id) == (
        "br-cdc-art-39",
        "br-cdc-art-39-inciso-i",
    )
    assert label_ranks._matches(hit, label)
