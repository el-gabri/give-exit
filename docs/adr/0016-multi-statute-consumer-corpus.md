# ADR 0016: LGPD and a scoped Civil Code as complementary sources

## Status

Accepted · Date: 2026-09-13 · Amends: [ADR 0012](0012-bounded-consumer-extrajudicial-notice.md) §3
and [ADR 0013](0013-versioned-consumer-law-retrieval.md) §1

## Context

The consumer corpus held the complete CDC and seven constitutional provisions.
Consumer disputes about personal data (breaches, sharing without consent,
ignored deletion requests) are governed by the LGPD, and many consumer claims
also rest on general civil-law rules: undue payment, unjust enrichment,
late-payment interest, adhesion contracts, civil liability. Planalto publishes
both statutes as compiled HTML with their own quirks. The Civil Code has 2,083
articles; most concern companies, property, family and succession, which a
notice to a supplier never rests on. The ground selector already cites some
unrelated provisions because it relies on retrieval agreement (RAG review of
2026-09-07), and a five-times larger index widens that exposure.

## Decision

1. Parse every statute with one generic Planalto parser driven by a
   declarative specification per law: article range, declared gaps, exact text
   corrections and index scope. Parsing fails closed, and the CDC output is
   byte-identical to the former CDC parser.
2. Pin each snapshot with a manifest recording the raw SHA-256, the SHA-256 of
   the extracted text (Planalto's WAF randomizes raw bytes), the download user
   agent and a review status.
3. Index the whole LGPD. Index only the Civil Code's general part and law of
   obligations; keep the other books in the corpus as `audit_only`.
4. A notice may cite LGPD chapters I–III and V–VII and indexed Civil Code
   provisions, except Título VI of the law of obligations (the specific
   contract types). LGPD chapters IV, VIII, IX and X are never cited.
5. LGPD and Civil Code grounds are complementary: at most three per notice,
   only beside at least one CDC ground, and none when retrieval fell back to
   lexical-only search. The CDC remains the primary authority.
6. Data-protection complaints get a retrieval vocabulary without article
   numbers. The numeric anchors in the existing expansions stay unqualified:
   naming the CDC in them lowered the offline retrieval metrics on both the
   previous and the new corpus, so that change is deferred.
7. The consumer-scope gate is unchanged: the new sources are consulted only
   for consumer disputes.

## Consequences

- (+) Notices can cite data-protection duties and general civil-law rules
  with the same provenance and citation checks as the CDC.
- (+) A changed official page fails the parse or the text hash instead of
  silently changing the law.
- (+) On the configured JUÁ stack the original 15 cases kept their CDC
  retrieval (recall@5 0.564, article recall@5 0.756, nDCG@5 0.518 → 0.512,
  hard-negative rate 0.08), and no in-scope notice case was left without a
  ground.
- (-) The index grows from 472 to 2,455 chunks. The vector-reuse canary
  (ADR 0017) rejected the previous generation with a cosine of 0.999887
  against its 0.9999 threshold, on the same host and packages (a CPU
  batch-composition effect), so the first build ran with `--no-reuse` and
  embedded every chunk in about 23 hours on CPU. Calibrating the canary
  threshold is a follow-up.
- (-) The offline CI stack (a hashed 128-dimension embedder plus BM25) ranks
  far worse on the larger index. Its gates were re-baselined to the new
  measurement (article recall@5 0.48 instead of 0.75), so CI guards the
  offline baseline only; retrieval quality has to be measured on the
  configured stack.
- (-) Complementary precision needed a manual cut. On the configured stack
  all seven grounds drawn from Civil Code Título VI were off-topic for their
  complaint, which is why decision 4 excludes it; Título VII (unjust
  enrichment, undue payment) was on-topic. With the exclusion the evaluation
  notices cite 81 grounds, 21 of them complementary (from 87 and 27), with
  the same 3 known-bad citations, exact recall 0.228 and semantic success
  1.0. Some remaining complementary grounds are still doubtful.
- (-) Factual applicability is still not verified; the cap and the CDC anchor
  bound unsupported complementary grounds but do not remove them.
- (-) The Civil Code scope, the LGPD eligibility rules and the new golden
  labels require specialist legal review.
