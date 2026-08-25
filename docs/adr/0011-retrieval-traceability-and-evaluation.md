# ADR 0011: Preserve Consumer retrieval traces and evaluate rankings

**Status:** accepted

## Context

A final notice alone cannot explain which query retrieved which legal or
evidence chunk, how candidates were fused, or why one chunk was selected.
Retrieval changes therefore need query-level audit and repeatable regression
metrics.

## Decision

Every Consumer retrieval records a typed trace with query/hash, component,
embedding identity, index/chunking version, retrieval mode, candidate depth,
fusion/reranker settings, latency, ranked chunk metadata and final selection
flags.

The in-memory notice retains these traces and exposes them at
`/consumer/cases/{case_id}/notice/retrievals`. Text previews are opt-in.

The Consumer golden dataset pins its own hash, corpus release/hash and query
builder. It scores exact and article Recall, MRR, NDCG, subdivision precision,
hard negatives, authority status and out-of-scope abstention.

## Consequences

- A reviewer can reconstruct query to chunk to citation.
- Embedding and ranking bake-offs share one provider-neutral evaluator.
- Sensitive previews are not retained by default.
- Traces currently disappear with in-memory case state; production requires a
  tenant-scoped, append-only audit store and retention policy.
