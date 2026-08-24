# ADR 0014: Human review as a state input, not a route override

Status: accepted · Date: 2026-08-19 · Amended: 2026-08-24

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
holds an immutable `HumanReviewDecision` (decision/job/document IDs, exact
security-assessment SHA-256, approved, reviewer, comment and timestamp), and
`_after_security_scan` admits a halted run to indexing only when the scan
completed, the action is exactly `HUMAN_REVIEW`, and an approving decision is
present. The router keeps deciding; the decision is one more input it reads.

`POST /analyses/{job_id}/review` applies a decision to a job in
`review_required`, and is rejected with 409 in any other state, so a decision
is made once. The decision is appended to the Business run ledger **before**
any state transition or approved resume is scheduled; a persistence failure
rejects the operation. Durable telemetry replaces the supplied reviewer label
with an HMAC reference and omits the optional free-text comment.

Approval reserves the same bounded in-process execution capacity as a new
analysis, then re-runs the pipeline from the same parsed document with the
decision in the initial state — findings stay masked exactly as in `medium`,
because approval means "this document may be analyzed", not "this text is
trustworthy". Rejection ends the run as `rejected`, preserving the halted
report for audit. The in-memory report warning uses the reviewer label supplied
by the caller.

## Consequences

- (+) The security gate's central safety promise is operational: a halt has a
  resolution, and both outcomes are recorded.
- (+) Binding decision, job, document and security-assessment digest makes the
  reviewed artifact explicit and prevents reusing a decision for another halt.
- (+) Persist-before-resume avoids starting approved work without first writing
  its decision record.
- (+) The approval path cannot widen: `blocked` and incomplete scans are
  excluded in the router, which is covered by tests rather than convention.
- (+) Approved runs stay honest — masked excerpts remain masked, and the report
  records the self-declared reviewer label that authorized the analysis.
- (+) Re-running from the parsed document reuses the existing graph unchanged;
  no checkpointing or resumable-node machinery is introduced.
- (-) Approval pays for a full re-run, including a second security scan.
  Acceptable while these halts are rare; a checkpointer is the answer if they
  are not.
- (-) The reviewer identity is self-declared input. The deployment-wide API key
  authenticates a caller only when configured (and is mandatory in production
  mode); it does not establish the human's identity, role, bar status or tenant.
  The reviewer field is an audit label, not verified attribution, until
  per-user AuthN/RBAC lands.
- (-) Decisions live in the in-process job registry, so a restart before the
  decision loses the pending job along with every other in-flight one. A
  persisted decision/history record cannot resume a job after restart, and
  another replica has no shared registry in which to find it.
- (-) Pending review jobs retain parsed filing data. Separate count and TTL
  limits evict the oldest or expired entries only after durable audit; if audit
  persistence is unavailable, the rich state is discarded and the job fails
  closed.
- (-) An approved resume consumes the bounded local queue. Capacity exhaustion
  returns a retryable failure; no durable broker holds the decision for later
  execution.
