# ADR 0006: Section-aware chunking for legal documents

Status: accepted · Date: 2026-07-23 · Amended by
[ADR 0019](0019-explicit-retrieval-agreement.md): uploaded evidence is the only
document the application chunks, so page-preserving mode is the default;
oversized paragraphs split at sentence ends or word breaks, not fixed offsets.

## Context

Naive fixed-size chunking (split every N chars) is the default in most RAG
tutorials, but Brazilian petitions have a strong canonical structure:
DOS FATOS (facts), DO DIREITO (legal grounds), DOS PEDIDOS (requests), plus
preliminares and tutela sections. A fixed-size chunk that straddles
DOS FATOS / DOS PEDIDOS pollutes retrieval for both "what happened?" and
"what do they want?" queries.

## Decision

1. Detect section headings with a typographic heuristic (short lines,
   >=90% uppercase letters) - robust across courts without needing an
   exhaustive heading list.
2. Chunk within sections only; prefix each chunk with its section title
   (cheap contextualization that improves both embedding quality and
   LLM grounding).
3. Greedy paragraph packing to ~1200 chars with 150-char overlap between
   consecutive chunks of the same section.
4. Chunks carry provenance (section, page span) -> user-facing citations.

## Consequences

- (+) Retrieval queries map cleanly onto legal questions per section.
- (+) Citations can show "DOS PEDIDOS, p. 12" instead of "chunk 37".
- (-) Heuristic can miss unconventional headings -> text still lands in the
  preceding section; degraded, not broken. Tunable constants, and the
  evaluation harness measures Precision@K, Recall@K, HitRate@K, MRR@K and
  NDCG@K to tune them against stable page/passage relevance judgments.

## Alternatives rejected

- **Fixed-size chunking**: simplest, but measurably worse on structured
  documents; kept as an implicit fallback (docs with no detected headings
  become one section packed greedily).
- **LLM-based semantic chunking**: highest quality, but adds an LLM call
  per document page - poor cost/latency trade-off for documents that are
  already explicitly structured.
