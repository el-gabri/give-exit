# ADR 0014: Resumable and auditable embedding generations

## Status

Accepted

## Context

The Consumer legal corpus currently has 460 canonical chunks. Embedding all of
them with a 4B local model on CPU is a long batch operation. A single-process,
all-or-nothing call loses work on interruption and a document-ID-only readiness
check cannot prove that the active namespace contains the expected corpus or
embedding space.

The online path also needs bounded behavior when local inference is saturated
or unavailable. A slow semantic dependency must not make an API worker wait
without limit, and a fallback must not silently claim normal hybrid retrieval.

## Decision

1. Define an immutable embedding contract containing corpus and chunk hashes,
   exact model repository/revision, output dimension, L2 normalization,
   document/query formatter versions and query-instruction hash.
2. Split offline generation into deterministic chunk-ID-ordered shards. Persist
   every shard as gzip JSONL with checksums and update the manifest atomically.
3. On restart, reuse only shards whose artifact, chunk and vector checksums all
   validate. A failed generation remains resumable and records its last error.
4. Import precomputed vectors only after all shards validate. Verify the active
   store against the complete canonical chunk set before marking it `active`.
5. Permit promotion of legacy vectors only through an explicit CLI attestation
   of the exact model revision. Record provenance as
   `adopted_existing_vectors`; do not represent it as original run metadata.
6. Apply timeout, concurrency limiting, a query-hash TTL cache and circuit
   breaker around online query embeddings. When hybrid semantic retrieval is
   unavailable, permit a clearly traced lexical-only fallback. Legal-ground
   corroboration, source eligibility and citation reconstruction remain
   unchanged and may abstain.
7. Execute PostgreSQL lexical ranking in the database with a Portuguese
   `tsvector` GIN index instead of loading the whole document into Python.

## Consequences

- Interrupted CPU jobs resume from verified work rather than restarting all
  460 chunks.
- Readiness is fail-closed on missing/corrupt artifacts, wrong chunks, wrong
  dimensions or an incomplete active namespace.
- Every retrieval trace distinguishes normal hybrid results from degraded
  lexical-only results.
- Legacy promotion is fast but explicitly weaker evidence than a newly
  generated manifest, so audit consumers can distinguish the two.
- Full artifact and persistence validation adds startup/check latency; the
  service caches successful readiness for the process lifetime.
