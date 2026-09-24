# ADR 0019: Explicit retrieval agreement, a citable-only index and verified grounds

## Status

Accepted · Date: 2026-09-24 · Amends: [ADR 0006](0006-section-aware-chunking.md),
[ADR 0013](0013-versioned-consumer-law-retrieval.md),
[ADR 0015](0015-bounded-notice-prose-composer.md)

## Context

A review of the RAG pipeline found that its main precision control and
several details around it did not do what they were described as doing:

- The support gate inferred "dense and lexical retrieval both found this
  chunk" from the fused RRF score. That held only for equal weights and a
  candidate depth of at most 61. With a reranker enabled the score type
  changed and the gate fell back to "top three in both queries", and the two
  queries are one framing of the narrative (the second is contained in the
  first). Their lexical top three were identical in 10 of 22 golden cases.
- The relative score floor (half the best score) could never bind on RRF
  scores: any chunk both channels returned within 32 candidates scores above
  it.
- 811 of 2,455 indexed legal chunks belonged to chapters a notice may never
  cite (CDC criminal offences, LGPD sanctions and authority, Civil Code
  specific contracts). They were filtered only after the top 8 were taken;
  on the golden queries 46% of the in-process lexical top 8 were such chunks.
- In PostgreSQL the lexical channel was `ts_rank_cd` over a stemmed
  `tsvector`, with no inverse document frequency. Its top 8 shared 19% of
  its chunks with the in-process BM25 top 8 that offline evaluation measures.
- Evidence pages were separated by a `CASO … EVIDENCIA … PAGINA n` marker
  that the page-preserving chunker indexed as body text; the evidence query
  began with "Evidência que comprova…", so every page matched lexically and
  passed the gate. A cake recipe uploaded beside an invoice did.
- PDF text arrived as one paragraph per page (`get_text("text")` never marks
  paragraphs), so evidence chunks were cut at fixed offsets, e.g. between
  `R$` and `1.250,00`. Citations quoted a chunk's first 700 characters,
  wherever the supporting sentence was.
- Every legal ground carried the same boilerplate explanation; nothing tied
  an article to the facts. Composer prose was checked only for links,
  brackets and hashes.

## Decision

1. **Record channel ranks and gate on them.** Every retrieved chunk carries
   `channel_ranks` (its rank in the dense and lexical lists) through fusion,
   reranking and the audit trace. A chunk is supported when both channels
   ranked it within `AGREEMENT_MAX_RANK = 20`. The gate holds for any fusion
   weights and with a reranker, which now orders candidates only within
   their agreement tier. The relative floor applies only to reranker and
   single-channel scores.
2. **Index only citable provisions** (`legal-hierarchy-v4`). Uncitable
   chapters stay in the corpus for audit. The eligibility check at selection
   time remains as defence in depth.
3. **Independent corroboration when a channel is missing.** When legal
   retrieval degrades to lexical-only, the complaint and the requested remedy
   are searched separately; a chunk needs a top-three rank for two queries
   neither of whose texts contains the other. Otherwise the notice abstains
   and asks for a retry.
4. **The same BM25 in every backend.** PostgreSQL stores the in-process
   tokenizer's tokens per row (backfilled for existing rows) and computes the
   same BM25 in SQL; on the golden queries the top 32 is identical in order
   and score to the in-process ranking. Connections are pooled.
5. **Evidence is indexed and searched on its own terms.** No marker is
   injected; the page-preserving chunker (now the default) keeps pages apart.
   Evidence queries are the complaint, the remedy and the identifiers
   documents print (protocols, date, amounts), with no scaffolding. PDF text
   is extracted per layout block; oversized paragraphs split at a sentence
   end or word break, never between a currency symbol and its amount. The
   quote is the passage of the chunk that best matches the confirmed facts.
6. **Optional span-verified ground check** (`LITIGATION_GROUND_VERIFIER=llm`,
   off by default). The configured LLM judges each selected ground and, to
   claim `applies`, quotes the consumer's account and the official text.
   Code accepts the verdict only if both quotes are verbatim substrings; the
   notice then shows them under the citation. Only `does_not_apply` removes a
   ground; failures keep the selection and add a warning.
7. **Composer prose may not cite or count.** Article references, `§`, `Lei
   nº`, currency and any number absent from the model's input are rejected,
   and the prompt says so (`consumer-notice-grounded-prose:v2`).

## Measurements (offline stack: hashed-BOW embeddings, in-process BM25)

| Metric | Before | After |
|---|---|---|
| retrieval recall@5 / article recall@5 | 0.142 / 0.225 | 0.142 / 0.225 |
| retrieval nDCG@5 | 0.069 | 0.075 |
| retrieval hard-negative rate@5 | 0.027 | 0.027 |
| notice grounds (22 cases) | 23 | 20 |
| notice complementary grounds | 5 | 3 |
| notice known-bad citations | 0 | 0 |
| notice exact recall | 0.017 | 0.017 |

`AGREEMENT_MAX_RANK` was chosen on this stack: with the citable-only index
and the former depth of 32 the notice evaluation cited 41 grounds, 2 of them
known bad; 24 gave 25 grounds, 20 gave 20, and 16 or less lost the only exact
hit. The seed has 22 cases and the offline dense channel is a hashed bag of
words, so this depth must be rechecked on the configured embedding stack
(`--evaluate-notice` with the configured pipeline).

## Alternatives tried and rejected

- **One query per complaint sentence** (alone, or on top of the two ranking
  queries, merged by max score, sum or RRF across queries). Offline recall@5
  fell from 0.142 to at most 0.100, and adding the facets raised cited grounds
  from 22 to 31–37 with no gain in exact recall. The ranking queries are
  unchanged; per-facet retrieval is used only for degraded-mode corroboration.
- **Dropping the framing words from the first legal query.** Narrative-only
  ranking lowered offline recall@5 to 0.067.
- **Excluding uncitable chunks without tightening agreement.** It exposed how
  weak "anywhere in both 32-deep lists" was: 41 grounds, 2 known bad.

## Consequences

- (+) The gate is explicit, auditable per chunk and independent of fusion
  weights and reranking.
- (+) Offline evaluation now measures the lexical ranker PostgreSQL ships.
- (+) Irrelevant uploads no longer pass the evidence gate through scaffolding.
- (+) Legal retrieval spends its slots on citable text only.
- (-) Existing legal indexes must be rebuilt once (`python -m
  app.consumer.preindex_legal`); remaining chunk texts are unchanged, so
  vectors are reused (ADR 0017) where an embedded generation exists.
- (-) In lexical-only mode notices abstain more often; the service asks for a
  retry rather than citing an uncorroborated keyword match.
- (-) Offline notice metrics are near their noise floor (one exact hit), so
  small threshold changes here cannot be validated offline.
