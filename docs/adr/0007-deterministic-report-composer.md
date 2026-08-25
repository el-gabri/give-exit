# ADR 0007: Compose consumer notices deterministically

**Status:** accepted

## Context

An extrajudicial notice combines confirmed allegations, evidence references,
legal authorities, requests, deadlines and a transparent settlement scenario.
Allowing a generative model to rewrite those sections would weaken provenance
and make the same confirmed case produce different legal language.

## Decision

The notice composer is plain Python. It accepts typed Consumer facts, evidence
citations, eligible legal grounds and settlement components. It renders one
canonical Markdown artifact; PDF and DOCX are derived from that Markdown.

The LLM does not write, revise or polish the notice.

## Consequences

- Identical inputs and versions produce identical notice content.
- Legal citations and monetary values can be traced to typed sources.
- Unsupported sections fail before an artifact is returned.
- Language is more constrained than free-form generation, by design.
