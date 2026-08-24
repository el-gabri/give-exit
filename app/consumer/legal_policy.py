"""Conservative, deterministic eligibility policy for cited consumer law.

Retrieval is a candidate generator, not a legal merits decision.  A chunk may
be highly ranked because it shares generic words such as ``consumidor`` or
``reparação`` while addressing a different issue.  Before a retrieved chunk
can become a legal ground, this module requires its article to be among a
small category-specific set.

The mapping is an engineering safety control derived from the repository's
versioned query taxonomy and legal-evaluation seed.  It is deliberately
narrow and is still marked as requiring Brazilian-lawyer review; absence from
the mapping means abstention, never permission to improvise an authority.
"""

from __future__ import annotations

from types import MappingProxyType

from app.schemas.trace import RetrievalTrace

LEGAL_GROUND_POLICY_VERSION = "consumer-ground-eligibility-v1"
LEGAL_GROUND_POLICY_REVIEW_STATUS = "requires_legal_review"


_ELIGIBLE_PROVISIONS = MappingProxyType(
    {
        "unauthorized_charge": frozenset({"br-cdc-art-42", "br-cdc-art-42-a"}),
        "fraud": frozenset({"br-cdc-art-6", "br-cdc-art-14"}),
        "account_block": frozenset(
            {"br-cdc-art-6", "br-cdc-art-14", "br-cdc-art-20", "br-cdc-art-22"}
        ),
        "negative_credit_record": frozenset({"br-cdc-art-42-a", "br-cdc-art-43"}),
        "loan_or_interest": frozenset(
            {
                "br-cdc-art-6",
                "br-cdc-art-46",
                "br-cdc-art-51",
                "br-cdc-art-52",
                "br-cdc-art-54-a",
                "br-cdc-art-54-b",
                "br-cdc-art-54-c",
                "br-cdc-art-54-d",
                "br-cdc-art-54-f",
                "br-cdc-art-54-g",
            }
        ),
        "service_failure": frozenset({"br-cdc-art-14", "br-cdc-art-20"}),
        "product_defect": frozenset({"br-cdc-art-18", "br-cdc-art-26"}),
        "non_delivery": frozenset({"br-cdc-art-30", "br-cdc-art-35"}),
        "right_of_withdrawal": frozenset({"br-cdc-art-49"}),
        "misleading_advertising": frozenset(
            {"br-cdc-art-30", "br-cdc-art-36", "br-cdc-art-37", "br-cdc-art-38"}
        ),
        "abusive_practice": frozenset({"br-cdc-art-39"}),
        "abusive_collection": frozenset({"br-cdc-art-42", "br-cdc-art-42-a"}),
        "public_utility": frozenset({"br-cdc-art-22"}),
        "consumer_safety": frozenset(
            {
                "br-cdc-art-8",
                "br-cdc-art-9",
                "br-cdc-art-10",
                "br-cdc-art-12",
                "br-cdc-art-14",
            }
        ),
        "contract_terms": frozenset(
            {
                "br-cdc-art-46",
                "br-cdc-art-47",
                "br-cdc-art-51",
                "br-cdc-art-54",
                "br-cdc-art-54-a",
                "br-cdc-art-54-b",
                "br-cdc-art-54-c",
                "br-cdc-art-54-d",
                "br-cdc-art-54-f",
                "br-cdc-art-54-g",
            }
        ),
        "over_indebtedness": frozenset(
            {
                "br-cdc-art-54-a",
                "br-cdc-art-54-b",
                "br-cdc-art-54-c",
                "br-cdc-art-54-d",
                "br-cdc-art-54-f",
                "br-cdc-art-54-g",
                "br-cdc-art-104-a",
                "br-cdc-art-104-b",
                "br-cdc-art-104-c",
            }
        ),
        # ``other`` is intentionally absent.  A broad catch-all category does
        # not provide enough information to select a legal authority safely.
    }
)


def eligible_provision_ids(category: str) -> frozenset[str]:
    """Return the reviewed-policy candidates for one inferred category."""

    return _ELIGIBLE_PROVISIONS.get(category, frozenset())


def provision_is_eligible(category: str, provision_id: str) -> bool:
    """Whether a retrieved article may be considered for this category."""

    return provision_id in eligible_provision_ids(category)


def strongly_supported_chunk_ids(traces: list[RetrievalTrace]) -> frozenset[str]:
    """Return chunks that clear a score-type-aware retrieval safety gate.

    For reciprocal-rank fusion, a score above the best possible contribution
    from either single channel proves that both dense and lexical retrieval
    contributed.  Reranker and other score scales have no portable absolute
    threshold, so they require the same chunk to rank in the top three for at
    least two independently constructed queries.
    """

    supported: set[str] = set()
    corroboration: dict[str, set[int]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        if trace.score_type == "rrf_score" and trace.rrf_constant is not None:
            weights = (trace.dense_weight or 1.0, trace.lexical_weight or 1.0)
            single_channel_ceiling = max(weights) / (trace.rrf_constant + 1)
            supported.update(
                item.chunk_id
                for item in trace.results
                if item.score > single_channel_ceiling + 1e-12
            )
            continue
        for item in trace.results:
            if item.rank <= 3:
                corroboration.setdefault(item.chunk_id, set()).add(trace.query_index)

    supported.update(
        chunk_id for chunk_id, query_indexes in corroboration.items() if len(query_indexes) >= 2
    )
    return frozenset(supported)
