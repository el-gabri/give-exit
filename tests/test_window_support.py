"""A provision only sentence windows found needs corroboration (ADR 0026)."""

from __future__ import annotations

import hashlib

from app.consumer.legal_policy import LEGAL_WINDOW_AGENT, strongly_supported_chunk_ids
from app.consumer.retrieval import LEGAL_RETRIEVAL_AGENT
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace

DOC = "legal-doc"


def _official(unit: str) -> str:
    return f"{DOC}:legal:{unit}:part-01"


def _alias(unit: str) -> str:
    return f"{DOC}:legal:{unit}:alias-01"


def _item(
    rank: int, chunk_id: str, provision: str | None, *, lexical_only: bool = False
) -> RetrievedItemTrace:
    ranks = {"lexical": rank} if lexical_only else {"dense": rank, "lexical": rank}
    return RetrievedItemTrace(
        rank=rank,
        chunk_id=chunk_id,
        doc_id=DOC,
        page_start=1,
        page_end=1,
        score=1.0 / rank,
        channel_ranks=ranks,
        content_sha256="0" * 64,
        source_provision_id=provision,
    )


def _trace(
    index: int,
    hits: list[tuple[str, str | None]],
    *,
    agent: str = LEGAL_WINDOW_AGENT,
    degraded: bool = False,
) -> RetrievalTrace:
    query = f"janela {index} do relato"
    return RetrievalTrace(
        batch_id=agent,
        agent=agent,
        doc_id=DOC,
        query_index=index,
        query=query,
        query_sha256=hashlib.sha256(query.encode()).hexdigest(),
        requested_k=8,
        returned_count=len(hits),
        retrieval_mode="hybrid",
        degraded_mode="lexical_only" if degraded else None,
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        results=[
            _item(rank, chunk_id, provision, lexical_only=degraded)
            for rank, (chunk_id, provision) in enumerate(hits, start=1)
        ],
    )


ART_29 = "br-cdc-art-29"
ART_35 = "br-cdc-art-35"


def test_a_window_alone_does_not_support_an_alias_hit() -> None:
    window = _trace(0, [(_alias("br-cdc-art-29-caput"), ART_29)])

    assert strongly_supported_chunk_ids([window]) == frozenset()


def test_two_windows_support_the_same_provision() -> None:
    first = _trace(0, [(_alias("br-cdc-art-35-inciso-i"), ART_35)])
    second = _trace(1, [(_alias("br-cdc-art-35-caput"), ART_35)])

    assert strongly_supported_chunk_ids([first, second]) == {
        _alias("br-cdc-art-35-inciso-i"),
        _alias("br-cdc-art-35-caput"),
    }


def test_the_remedy_search_vouches_for_a_window_hit() -> None:
    remedy = _trace(0, [(_official("br-cdc-art-35-inciso-i"), ART_35)], agent=LEGAL_RETRIEVAL_AGENT)
    window = _trace(
        1, [(_alias("br-cdc-art-35-inciso-iii"), ART_35), (_alias("br-cdc-art-29-caput"), ART_29)]
    )

    assert strongly_supported_chunk_ids([remedy, window]) == {
        _official("br-cdc-art-35-inciso-i"),
        _alias("br-cdc-art-35-inciso-iii"),
    }


def test_one_window_supports_official_text() -> None:
    window = _trace(0, [(_official("br-cdc-art-43-paragrafo-2"), "br-cdc-art-43")])

    assert strongly_supported_chunk_ids([window]) == {_official("br-cdc-art-43-paragrafo-2")}


def test_items_without_a_provision_are_keyed_by_chunk() -> None:
    """A legacy store without provision metadata never merges two articles by accident."""
    different = [
        _trace(0, [(_alias("br-cdc-art-29-caput"), None)]),
        _trace(1, [(_alias("br-cdc-art-41-caput"), None)]),
    ]
    same = [
        _trace(0, [(_alias("br-cdc-art-29-caput"), None)]),
        _trace(1, [(_alias("br-cdc-art-29-caput"), None)]),
    ]

    assert strongly_supported_chunk_ids(different) == frozenset()
    assert strongly_supported_chunk_ids(same) == {_alias("br-cdc-art-29-caput")}


def test_degraded_windows_corroborate_like_any_independent_query() -> None:
    both = _alias("br-cdc-art-35-inciso-i")
    once = _alias("br-cdc-art-29-caput")
    first = _trace(0, [(both, ART_35), (once, ART_29)], degraded=True)
    second = _trace(1, [(both, ART_35)], degraded=True)

    assert strongly_supported_chunk_ids([first, second]) == {both}


def test_mixed_health_follows_each_traces_rule() -> None:
    remedy = _trace(
        0, [(_alias("br-cdc-art-49-caput"), "br-cdc-art-49")], agent=LEGAL_RETRIEVAL_AGENT
    )
    corroborated = _alias("br-cdc-art-35-inciso-i")
    first = _trace(
        1, [(corroborated, ART_35), (_alias("br-cdc-art-29-caput"), ART_29)], degraded=True
    )
    second = _trace(2, [(corroborated, ART_35)], degraded=True)

    assert strongly_supported_chunk_ids([remedy, first, second]) == {
        _alias("br-cdc-art-49-caput"),
        corroborated,
    }


def test_without_windows_the_gate_is_unchanged() -> None:
    narrative = _trace(0, [(_alias("br-cdc-art-29-caput"), ART_29)], agent=LEGAL_RETRIEVAL_AGENT)

    assert strongly_supported_chunk_ids([narrative]) == {_alias("br-cdc-art-29-caput")}


def test_the_alias_cap_applies_inside_each_window() -> None:
    """Corroboration across windows never lifts the per-query alias cap."""
    hits = [
        (_alias(f"br-cdc-art-{number}-caput"), f"br-cdc-art-{number}") for number in (6, 7, 8, 9)
    ]
    windows = [_trace(0, hits), _trace(1, hits)]

    assert strongly_supported_chunk_ids(windows) == {chunk for chunk, _ in hits[:3]}
