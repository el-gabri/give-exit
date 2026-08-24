# ADR 0011: Retrieval traceability and ranking evaluation

Status: accepted · Date: 2026-07-31 · Amended: 2026-08-24

## Context

Backend-reconstructed citations prove that a displayed excerpt comes from a
resolved source chunk/page, but they do not prove which query retrieved that
chunk, its original rank and score, whether prompt limits removed it, or whether
the excerpt entails the conclusion. Similarity scores alone also do not measure
retrieval quality; Recall@K and related metrics need reviewed relevance labels.
Raw natural-language queries and previews can themselves repeat allegations and
personal data, so live debugging and durable audit have different disclosure
boundaries.

## Decision

1. Store a typed retrieval trace under each agent execution trace. This reuses
   LangGraph's additive trace reducer and preserves attribution across parallel
   risk and strategy branches.
2. Record each query's effective `k`, raw ranks and scores, chunk/document IDs,
   section and page span, embedding/vector/index versions, latency, failure,
   consuming-agent outcome/prompt version, and SHA-256 of the exact indexed
   text. Preserve successful sibling lookups when one query in a batch fails.
3. Keep raw query text in the process-local job trace used by
   `/analyses/{job_id}/retrievals`, but replace it before Business JSONL writes
   with `[QUERY_REDACTED:<query-hash-prefix>]`. Retain the full SHA-256 for
   integrity/correlation. Never persist embeddings or full chunk text.
4. Before durable writes, pseudonymize filenames and reviewer labels with HMAC;
   omit reviewer comments, section labels, text previews and arbitrary source
   metadata; and reduce errors to bounded exception-type markers. The raw
   values remain available only in the process-local job during its configured
   retention window. This is minimization, not anonymization: hashes,
   pseudonyms and dedicated source fields can still be personal data.
5. Mark deduplication winners and the chunks that actually reached the prompt
   after the context character limit is applied.
6. Expose the raw current audit through the in-memory job endpoint and the
   minimized historical Business audit through `/runs/{run_id}/retrievals`.
   Keep the compact `/runs` listing free of trace data. Consumer traces stay on
   the in-memory case and are not written to `RunStore`.
7. Label golden retrieval queries using relevant page ranges and short passage
   anchors rather than chunk IDs. Resolve labels against the current chunker and
   report Precision@K, Recall@K, HitRate@K, MRR@K and NDCG@K.

## Consequences

- (+) While a job is live, an operator with API access can reconstruct raw query ->
  ranking -> merged context -> evidence ID. Historical Business records retain
  hash-linked query provenance without duplicating the natural-language query.
- (+) Chunk and query hashes make configuration or content drift detectable.
- (+) Relevance labels survive most chunk-size and overlap changes.
- (+) Old JSONL records remain readable because retrieval lists default empty.
- (+) A configured high-entropy telemetry pseudonym key permits stable
  cross-process references without storing the original identifier; without
  one, references intentionally change on restart to resist enumeration.
- (-) Run records grow with `queries × k`; the configured `k` bounds that
  increase. Durable records intentionally sacrifice human-readable previews
  and sections to reduce duplicated case text.
- (-) Runtime traces provide observability, not relevance truth. Formal ranking
  metrics remain an offline operation requiring reviewed labels.
- (-) Live job traces remain raw. Durable hashes, HMAC references and dedicated
  provenance fields are not anonymous, and the pseudonym key is itself a
  secret.
- (-) Durable retrieval history currently covers Business runs only. Consumer
  case/notice traces disappear on restart.
- (-) The mandatory first-page document-head excerpt is prompt provenance but
  not a retrieval result, so it is intentionally outside the retrieval audit;
  the classifier now also performs bounded traced retrieval for its citations.
