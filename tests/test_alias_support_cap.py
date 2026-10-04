"""The alias support cap in the retrieval-agreement gate (ADR 0023)."""

from __future__ import annotations

import hashlib

import pytest

from app.consumer.aliases import alias_chunk_id, is_alias_chunk_id
from app.consumer.legal_policy import (
    AGREEMENT_MAX_RANK,
    ALIAS_SUPPORT_CAP,
    LEGAL_GROUND_POLICY_VERSION,
    strongly_supported_chunk_ids,
)
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace

DOC = "10f4ba20de24dbca"


def _alias(unit: str) -> str:
    return alias_chunk_id(DOC, unit)


def _official(unit: str) -> str:
    return f"{DOC}:legal:{unit}:part-01"


def _trace(
    chunk_ids: list[str],
    *,
    query: str = "consulta",
    query_index: int = 0,
    ranks: list[tuple[int, int]] | None = None,
    positions: list[int] | None = None,
    degraded: bool = False,
) -> RetrievalTrace:
    """One query's trace. Every chunk agrees at (rank, rank) unless ``ranks`` says otherwise.

    ``positions`` overrides each item's fused ``rank`` while keeping list order,
    as a reranker's reordering would.
    """
    count = len(chunk_ids)
    pairs = ranks or [(position, position) for position in range(1, count + 1)]
    fused = positions or list(range(1, count + 1))
    return RetrievalTrace(
        batch_id="batch",
        agent="consumer_legal_authorities",
        doc_id=DOC,
        query_index=query_index,
        query=query,
        query_sha256=hashlib.sha256(query.encode()).hexdigest(),
        requested_k=8,
        candidate_k=32,
        returned_count=count,
        retrieval_mode="hybrid",
        degraded_mode="lexical_only" if degraded else None,
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        chunking_version="test",
        score_type="rrf_score",
        rrf_constant=60,
        dense_weight=1.0,
        lexical_weight=1.0,
        results=[
            RetrievedItemTrace(
                rank=rank,
                chunk_id=chunk_id,
                doc_id=DOC,
                page_start=1,
                page_end=1,
                score=1.0 / (60 + rank),
                channel_ranks=(
                    {"lexical": rank} if degraded else {"dense": dense, "lexical": lexical}
                ),
                content_sha256=hashlib.sha256(chunk_id.encode()).hexdigest(),
            )
            for chunk_id, (dense, lexical), rank in zip(chunk_ids, pairs, fused, strict=True)
        ],
    )


def test_alias_chunk_ids_are_recognised_and_nothing_else() -> None:
    assert alias_chunk_id(DOC, "br-cdc-art-39-inciso-i") == (
        f"{DOC}:legal:br-cdc-art-39-inciso-i:alias-01"
    )
    assert is_alias_chunk_id(_alias("br-cdc-art-39-inciso-i"))
    assert is_alias_chunk_id(_alias("br-cf-art-5-xxxii"))
    assert not is_alias_chunk_id(_official("br-cdc-art-39-inciso-i"))
    assert not is_alias_chunk_id(f"{DOC}:legal:br-cdc-art-39:article:part-01")
    assert not is_alias_chunk_id("evidence-doc:0003")


def test_the_gate_depth_cap_and_policy_version() -> None:
    assert AGREEMENT_MAX_RANK == 10
    assert ALIAS_SUPPORT_CAP == 3
    assert LEGAL_GROUND_POLICY_VERSION == "consumer-notice-scope-eligibility-v6"


def test_a_query_contributes_at_most_three_alias_chunks() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20, 35, 49)]
    officials = [_official("br-cdc-art-6-inciso-iii"), _official("br-cdc-art-42-caput")]
    trace = _trace(
        [aliases[0], officials[0], aliases[1], aliases[2], aliases[3], officials[1], aliases[4]]
    )

    assert strongly_supported_chunk_ids([trace]) == {*aliases[:3], *officials}


def test_the_cap_counts_per_query() -> None:
    first = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20)]
    second = [_alias(f"br-cdc-art-{number}-caput") for number in (35, 49, 51)]

    supported = strongly_supported_chunk_ids(
        [_trace(first, query="primeira"), _trace(second, query="segunda", query_index=1)]
    )

    assert supported == {*first, *second}


def test_alias_cap_is_a_parameter() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20)]

    assert strongly_supported_chunk_ids([_trace(aliases)], alias_cap=1) == {aliases[0]}


def test_alias_cap_bounds() -> None:
    alias, official = _alias("br-cdc-art-18-caput"), _official("br-cdc-art-42-caput")

    assert strongly_supported_chunk_ids([_trace([alias, official])], alias_cap=0) == {official}
    with pytest.raises(ValueError, match="alias_cap"):
        strongly_supported_chunk_ids([_trace([alias])], alias_cap=-1)


def test_the_cap_follows_rank_not_list_position() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20, 35)]
    # A reranker listed them in reverse fused order.
    trace = _trace(aliases, positions=[4, 3, 2, 1])

    assert strongly_supported_chunk_ids([trace]) == set(aliases[1:])


def test_only_agreeing_aliases_take_cap_slots_at_the_depth_boundary() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20, 35, 49)]
    depth = AGREEMENT_MAX_RANK
    trace = _trace(
        aliases,
        ranks=[(1, depth + 1), (depth, depth), (depth + 1, 1), (2, 2), (3, 3)],
    )

    # The first and third do not agree, so they take no slot; the depth-10 one does.
    assert strongly_supported_chunk_ids([trace]) == {aliases[1], aliases[3], aliases[4]}


def test_an_alias_capped_in_one_query_but_kept_in_another_is_supported() -> None:
    shared = _alias("br-cdc-art-49-caput")
    others = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20)]

    supported = strongly_supported_chunk_ids(
        [
            _trace([*others, shared], query="primeira"),
            _trace([shared], query="segunda", query_index=1),
        ]
    )

    assert supported == {*others, shared}


def test_a_degraded_trace_beside_a_healthy_one_keeps_corroboration() -> None:
    capped = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19)]
    corroborated = [_alias(f"br-cdc-art-{number}-caput") for number in (35, 49, 51)]
    healthy = _trace(capped, query="relato e pedido")
    # The complaint and the remedy searched separately corroborate the top three.
    complaint = _trace(corroborated, query="o produto quebrou", query_index=1, degraded=True)
    remedy = _trace(corroborated, query="quero a troca", query_index=2, degraded=True)

    supported = strongly_supported_chunk_ids([healthy, complaint, remedy], alias_cap=1)

    assert supported == {capped[0], *corroborated}
