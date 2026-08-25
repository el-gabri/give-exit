# ADR 0013: Versioned statutory retrieval for the consumer journey

## Status

Accepted

## Context

The first consumer prototype indexed a small set of editorial summaries from
the Federal Constitution and Consumer Defense Code (CDC).  That was useful for
proving provenance and notice composition, but it could not support broad
consumer complaints or evaluate the actual task of mapping a lay account to an
applicable statutory provision.

Legal retrieval has two complementary signals.  Exact references and legal
terms benefit from lexical search, while a consumer's description often uses
different words from the statute and benefits from semantic search.  Model
leaderboards alone cannot establish which combination works for this product.

## Decision

1. Treat the compiled CDC published by Planalto as the primary source.  Build
   immutable corpus releases offline; runtime requests never scrape Planalto.
   Keep selected constitutional grounds as exact official transcriptions, with
   editorial summaries explicitly identified as non-normative aids.
2. Preserve the official text and hierarchy (article, paragraph, item and
   letter), source URL, status and content hashes.  Editorial summaries may be
   auxiliary metadata but are never represented as statutory quotations.
3. Chunk at legal boundaries.  A chunk never crosses an article; long articles
   split at subdivisions and repeat enough hierarchy to remain interpretable.
4. Keep uploaded evidence and the canonical legal corpus under distinct,
   stable document IDs, and require every retrieval to be scoped by `doc_id`.
   The collection identity binds the canonical corpus hash, embedding model
   and optional model revision; any change creates a new index and requires
   reindexing.
5. Retrieve legal candidates with a deterministic hybrid of lexical and dense
   rankings, with an optional bounded reranker.  The confirmed complaint and
   desired resolution must appear in the legal retrieval queries.
6. Keep vetoed or repealed material available for audit, but exclude it from
   authorities used in a generated notice.
7. Evaluate this path with a separate, human-reviewable Consumer golden dataset
   containing lay complaints, graded provision relevance, hard negatives and
   no-applicable-provision cases.  Report retrieval quality by complaint type,
   along with latency, cost and inactive-authority error rate.
8. Apply a conservative deterministic scope gate before legal retrieval. Clear
   labor and neighbor disputes abstain instead of receiving arbitrary CDC
   grounds; ambiguous cases remain eligible for human review.

## Consequences

- (+) A notice can be traced to exact official statutory text and a reproducible
  corpus/index release.
- (+) Retrieval supports exact legal references and semantic paraphrases.
- (+) Embedding changes become evidence-based rather than configuration guesses.
- (+) Historical notices retain the source and model versions needed for audit.
- (-) Corpus updates require a reviewed ingestion and reindexing process.
- (-) Local legal embedding models and rerankers may require significant memory
  or GPU capacity; hosted embeddings remain a supported baseline.
- (-) The golden dataset needs qualified legal review before its thresholds can
  be treated as a release gate.
