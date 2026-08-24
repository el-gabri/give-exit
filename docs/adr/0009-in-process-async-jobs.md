# ADR 0009: In-process async jobs (broker-ready interface)

Status: accepted · Date: 2026-07-23 · Amended: 2026-08-24

## Context

A full analysis takes 30-90s of LLM calls; blocking an HTTP request that
long is unacceptable. Standard options: task queue (Celery/RQ + Redis),
cloud queue, or in-process asyncio tasks.

## Decision

`POST /analyses` returns 202 + job id; an asyncio task streams LangGraph
execution and updates stage progress for polling. A semaphore caps executing
analyses (`LITIGATION_MAX_CONCURRENT_JOBS`, default 4), and reservations cap
accepted work waiting for a slot (`LITIGATION_MAX_QUEUED_JOBS`, default 16).
The same capacity boundary applies to an approved human-review resume. Once the
combined capacity is reserved, new submissions fail explicitly with `503` and
`Retry-After` instead of creating an unbounded task backlog. A per-job wall-clock
deadline provides a separate bound for stalled providers.

Jobs, full reports and pending review state live in the process-local registry.
The registry retains a bounded number of finished jobs. Completed Business run
metadata and minimized retrieval traces are appended to `RunStore` JSONL, but
that ledger is history, not executable job state. Consumer cases and their
traces are separate and in memory only.

Pending `review_required` jobs have a separate count limit and TTL because
each one retains a parsed filing for possible resume. Eviction happens only
after the halted run has been durably recorded. If that append fails, the job
fails closed and releases the parsed document instead of retaining unbounded
case data in memory.

The `AnalysisJobManager` surface remains the replacement boundary for a future
broker/shared-state implementation, but the current behavior must not be
described as a durable or horizontally coordinated queue.

## Consequences

- (+) Zero queue infrastructure for a bounded single-node demo (fits Docker
  Compose, which binds to localhost by default).
- (+) Progress comes from LangGraph value-streaming - no bespoke callback
  plumbing inside agents.
- (+) Explicit concurrency and queue limits provide backpressure rather than
  accepting unlimited OCR/provider work.
- (+) Separate review-pending count/age limits bound retained filing data even
  when no reviewer responds.
- (-) Queued, running and `review_required` jobs are lost on process restart;
  persisted run/review records cannot resume them.
- (-) Horizontal replicas maintain independent semaphores, registries and job
  IDs. A request routed to another replica cannot read or resume the first
  replica's job, and local JSONL is not a distributed coordination mechanism.
- (-) Moving to Redis/cloud queues still requires shared idempotent job state,
  cross-worker cancellation/retry policy, distributed retention and tenant
  isolation; replacing one class is the seam, not proof the migration is
  otherwise mechanical.
