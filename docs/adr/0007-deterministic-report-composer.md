# ADR 0007: Deterministic report composer (no LLM at the last mile)

Status: accepted · Date: 2026-07-23 · Amended: 2026-08-24

## Context

The final report aggregates outputs of five agents. The obvious pattern is
a "composer agent" that receives everything and writes the report with an
LLM. That adds one more opportunity to hallucinate - at the exact step
where all conclusions converge and errors are least detectable.

## Decision

`compose_report` is plain Python. Sections map 1:1 to typed agent outputs;
aggregate confidence is an unweighted mean over every ConfidentConclusion
(transparent arithmetic over uncalibrated model scores, not an outcome
probability); "missing information" is computed from the
extraction schema plus the strategy agent's requests; the AI-reasoning
section is generated from the actual execution traces (models, prompt
versions, timings) - it describes what really happened, not what an LLM
says happened.

## Consequences

- (+) Assembly introduces no new generated claims; report structure is guaranteed.
- (+) Free and instant (no extra LLM call); partial reports on branch
  failure are trivial (None sections + warning).
- (+) The aggregate score is reproducible and explicitly labeled uncalibrated.
- (-) Deterministic assembly does not make upstream model claims correct; the
  evidence-quality gate and human legal review remain mandatory.
- (-) The report's prose polish is bounded by what agents produced;
  acceptable - polish belongs to the section content, not the assembly.
