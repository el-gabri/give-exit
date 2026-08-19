# ADR 0014: Human review as a state input, not a route override

Status: accepted · Date: 2026-08-19

## Context

ADR 0010 halts high-risk documents with the job outcome `review_required` and
records the page-attributed findings a reviewer needs. It stops there: the
outcome was terminal, so the only way to analyze a false-positive filing was to
re-upload it and hope the scan classified it differently. "Halt for human
review" named a state without providing the review.

Two properties must survive adding that path. The routing policy has to stay
deterministic and auditable — a reviewer resolves one specific halt, and never
gains the ability to talk the pipeline past a `blocked` verdict or an
incomplete scan. And the decision itself is evidence: who allowed a flagged
document into automated legal analysis is part of the audit trail, not a log
line.

## Decision

Model the decision as data on the graph state. `AnalysisState.human_review`
holds a `HumanReviewDecision` (approved, reviewer, comment, timestamp), and
`_after_security_scan` admits a halted run to indexing only when the scan
completed, the action is exactly `HUMAN_REVIEW`, and an approving decision is
present. The router keeps deciding; the decision is one more input it reads.

`POST /analyses/{job_id}/review` applies a decision to a job in
`review_required`, and is rejected with 409 in any other state, so a decision
is made once. Approval re-runs the pipeline from the same parsed document with
the decision in the initial state — findings stay masked exactly as in
`medium`, because approval means "this document may be analyzed", not "this
text is trustworthy". Rejection ends the run as `rejected`, preserving the
halted report for audit. Each decision appends a record to the run ledger under
the same run id, and the composer writes the reviewer's name into the report
warnings.

## Consequences

- (+) The security gate's central safety promise is operational: a halt has a
  resolution, and both outcomes are recorded.
- (+) The approval path cannot widen: `blocked` and incomplete scans are
  excluded in the router, which is covered by tests rather than convention.
- (+) Approved runs stay honest — masked excerpts remain masked, and the report
  states that a named human authorized the analysis.
- (+) Re-running from the parsed document reuses the existing graph unchanged;
  no checkpointing or resumable-node machinery is introduced.
- (-) Approval pays for a full re-run, including a second security scan.
  Acceptable while these halts are rare; a checkpointer is the answer if they
  are not.
- (-) The reviewer identity is self-declared. It is an audit record, not
  authentication, until per-user AuthN lands.
- (-) Decisions live in the in-process job registry, so a restart before the
  decision loses the pending job along with every other in-flight one.
