# ADR 0015: Optional bounded prose for consumer notices

## Status

Accepted; supersedes ADR 0007 while retaining its deterministic default.

## Context

Purely fixed prose is reproducible but can make a consumer notice read like a
template. Allowing a model to regenerate the entire document would weaken the
source, legal-ground and monetary guarantees. The system therefore needs a
boundary that can improve phrasing without transferring authoritative choices
to the model.

## Decision

1. Keep the deterministic composer as the default and fallback.
2. Configure the optional OpenAI notice composer independently from the LLM
   used for document-security review.
3. Send only the confirmed case packet: explicit allegations, selected evidence
   excerpts, already-filtered legal-ground summaries, deterministic requests
   and whether a monetary proposal exists.
4. Require strict structured output with exactly five length-bounded prose
   fields: purpose, facts framing, legal transition, requests transition and
   closing.
5. Treat every value inside the packet as untrusted data and instruct the model
   never to follow embedded instructions or introduce facts, sources, requests,
   deadlines, legal consequences or individualized legal advice.
6. Reject prose containing links, chunk/hash markers or citation syntax. The
   renderer alone inserts facts, evidence references, legal citations, requests,
   deadlines and monetary values.
7. Use `store=False`, bounded output and retry policy at the provider adapter.
   A refusal, timeout, schema violation or post-validation failure is recorded
   and falls back to deterministic prose.

## Consequences

- Source selection, citation reconstruction and monetary calculations remain
  deterministic and auditable.
- Generated language is explicitly non-authoritative and limited to phrasing.
- The final wording may vary when the option is enabled, so model, prompt and
  call metadata are retained with the notice.
- The bounded packet can still contain personal data; production deployment
  requires a documented processor basis, minimization, retention controls and
  vendor terms appropriate to LGPD obligations.
