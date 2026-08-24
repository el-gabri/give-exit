# ADR 0008: Evidence IDs and deterministic source reconstruction

Status: accepted · Date: 2026-07-23 · Amended: 2026-08-24

## Context

Standard RAG evaluation (RAGAS-style) can use an LLM to judge whether answers
are faithful to context. That is useful for semantic assessment but expensive,
non-deterministic and still not legal ground truth. The earlier runtime contract
also asked the generating model for quote, page and optional chunk ID, so a
plausible-looking source field still originated at the untrusted generation
boundary.

## Decision

For Business reports, a generated `Citation` must select a non-empty
`chunk_id`. All conclusion-producing agents, including classification, receive
bounded retrieved context with those IDs. `quote` and `page` remain in the
public Business report/API shape for downstream clients, but the backend owns
them:

1. resolve the ID against the indexed chunks supplied for the current report;
2. reject unknown IDs, duplicate/ambiguous IDs and chunks from another document;
3. find a deterministic substantive verbatim token span shared by the chunk and
   a covered `ParsedDocument` page;
4. replace any model-supplied quote/page with the reconstructed values, or
   remove the citation if reconstruction fails.

The deterministic report composer exposes an `evidence_quality` result. It
counts verified and rejected source references, conclusions without a verified
citation, and citation-to-producing-agent-context coverage. A `passed` result
means those source-integrity checks passed; otherwise the report requires human
review. It never suppresses the underlying conclusion solely because the source
gate failed.

Offline evaluation still includes normalized quote-occurrence groundedness,
citation coverage, extraction accuracy/completeness and a secondary
LLM-as-judge response-quality metric. These are separate measurements, not a
runtime legal-correctness verdict.

## Consequences

- (+) Model-authored quote/page text cannot become a displayed Business citation;
  displayed fields are reconstructed from ingestion and index artifacts.
- (+) Resolution is deterministic, provider-independent and covered offline.
- (+) Unknown, cross-document, ambiguous and source-inconsistent references fail
  closed and remain visible in report gate counts/warnings.
- (+) Keeping quote/page in the output contract preserves readable exports while
  moving authority over those values to the backend.
- (-) Selecting a real chunk does not prove that it supports the conclusion.
  Source integrity is **not** semantic entailment, legal correctness, calibrated
  confidence, independent verification of law or fitness for filing.
- (-) The Business index contains the uploaded filing, not an authoritative
  statute or case-law corpus. Legal text quoted by a litigant remains party
  content. The versioned authoritative corpus is currently Consumer-only.
- (-) Conclusions without a resolvable source remain in the report with a
  `human_review_required` gate rather than being silently presented as grounded.
