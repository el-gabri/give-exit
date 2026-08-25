# ADR 0010: Prompt-injection security gate before indexing

Status: accepted · Date: 2026-07-31

## Context

PDF text is external, untrusted input. A malicious or contaminated filing can
contain instructions aimed at the model, such as requests to ignore the system
prompt, reveal secrets, call tools, or force a particular answer. If that text
is indexed and later retrieved as ordinary context, prompt delimiters and a
system warning help but do not provide a sufficient security boundary.

The detector must also avoid treating normal legal imperatives such as
"requer", "determine-se" or "intime-se" as attacks. Findings need source-page
provenance so a reviewer can inspect the evidence without losing the original
document.

## Decision

Run `PromptInjectionDetector` before any uploaded document reaches the RAG
index. It scans every page with conservative Portuguese and English rules that target
model-directed instructions, role overrides, secret extraction, tool
manipulation, forced output and obfuscated payloads.

`LITIGATION_PROMPT_INJECTION_SCAN_MODE` controls the detector:

- `rules` runs only the deterministic page scan;
- `balanced` is the default and adds a bounded, structured semantic review of
  excerpts already identified as suspicious;
- `strict` semantically reviews all page text in bounded batches and fails
  closed if any required review batch fails or if the configured character or
  batch budget would be exceeded.

The semantic classifier receives no tools or secrets and cannot decide the
pipeline route. Code validates and merges its structured findings, then applies
this deterministic policy:

- `none` and `low`: continue;
- `medium`: continue with a visible warning and mask the flagged excerpts from
  downstream prompts;
- `high`: quarantine the document with outcome `review_required`;
- `critical`: reject the document with outcome `blocked`.

Neither security outcome is recorded as successful document processing; the
API exposes it separately from provider and ingestion failures.

Masking changes only the text eligible for retrieval and drafting. The source
document is preserved according to the configured retention policy, and each
finding records category, severity, page, verbatim excerpt, reasoning and
confidence in the structured audit data.

## Consequences

- (+) Every document receives a cheap, deterministic check before its content
  reaches embeddings, retrieval or analysis prompts.
- (+) The same explicit policy governs mock, offline and real-provider modes;
  an LLM cannot silently downgrade a block decision.
- (+) Reviewers can audit page-attributed findings while the original evidence
  remains intact.
- (+) Balanced mode limits additional LLM cost to suspicious excerpts.
- (+) Strict mode is available for higher-sensitivity workflows and preserves
  aggregate token, latency and cost accounting across review batches.
- (-) Heuristics can produce false positives and a semantic classifier can
  still make mistakes; this is risk reduction, not proof that a document is
  safe.
- (-) High-risk documents require an explicit human review path before the
  evidence can be used in a notice.
- (-) Strict mode may add substantial latency and LLM cost on long filings.
