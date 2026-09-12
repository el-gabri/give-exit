# ADR 0017: Reuse document vectors across embedding generations

## Status

Accepted; amends [ADR 0014](0014-resumable-embedding-generations.md).

## Context

A legal corpus release renames every chunk: `ParsedDocument.doc_id` hashes the
whole corpus text and chunk ids embed it. Under ADR 0014 a new release
therefore re-embeds every chunk, even when almost all chunk texts are
unchanged. The Consumer corpus is growing from 472 to about 2,460 chunks, and
the pinned 4B model runs on CPU; the last full generation took about two days
of wall-clock time. Every later amendment of an indexed statute would repeat
that cost.

A document vector depends only on the chunk text and on the document side of
the embedding contract: model repository and revision, dimension, dtype,
normalization and document formatter version.

## Decision

1. When building a generation, index the verified shards of earlier
   generations that are validated or active, have provenance `embedded`, and
   share the current document identity, including a pinned output dimension.
   Key their vectors by the SHA-256 of the chunk text.
2. Never reuse vectors from `adopted_existing_vectors` generations; their
   model revision is only attested.
3. Before using any reused vector, re-embed the two reused texts with the
   lowest hashes and require cosine ≥ 0.9999; otherwise fail the build and
   suggest `--no-reuse`. This catches a document-formatter change without a
   version bump and numerical drift across platforms.
4. Record provenance per vector. Reused JSONL lines carry `source`
   (generation id, chunk id, artifact SHA-256), shards count reused vectors,
   and the v2 manifest records `reused_chunk_count`, `reuse_sources` and the
   canary.
5. Reuse changes neither `provenance: embedded` nor the generation id.
   `--force` rebuilds without reuse; `--no-reuse` disables reuse and keeps
   resume.

## Consequences

- (+) A release that changes a few articles re-embeds only their chunks; the
  LGPD and Civil Code expansion reuses the 472 existing vectors.
- (+) Every reused vector remains traceable to the generation and artifact
  that produced it.
- (-) Reuse trusts that the document identity captures everything that moves
  vectors; the canary samples only two texts.
- (-) A generation must be kept for as long as it is a reuse source.
