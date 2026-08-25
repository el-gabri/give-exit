# ADR 0008: Reconstruct citations from selected evidence IDs

**Status:** accepted

## Context

Model-authored quotes, page numbers or statute references are not trustworthy
enough for consumer legal assistance. Retrieval rank alone also does not prove
that a provision is active, applicable or sufficiently supported.

## Decision

Evidence citations are created from selected retrieved chunks and the original
filename/page mapping. Legal citations are created from canonical corpus
metadata after deterministic eligibility checks.

Every citation carries stable IDs and source/content hashes. The notice
composer accepts only these reconstructed citation objects. Retrieved content
is treated as untrusted data and never as instructions.

## Consequences

- Displayed citations can be deterministically reproduced.
- The application can distinguish retrieval candidates from used authorities.
- Corpus and chunking changes remain visible through release IDs and hashes.
- Correct citation reconstruction does not prove legal applicability; corpus,
  policies and evaluation labels still require independent legal review.
