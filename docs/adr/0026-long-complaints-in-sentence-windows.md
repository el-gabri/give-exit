# ADR 0026: Long complaints in sentence windows

## Status

Accepted · Date: 2026-11-23 · Amends: [ADR 0018](0018-narrative-only-consumer-intake.md) (ranking queries), [ADR 0023](0023-agreement-depth-and-alias-cap.md) (the agreement gate) · Follows: [ADR 0025](0025-consumer-cited-provisions-and-actionable-refusals.md)

## Context

Legal retrieval built two ranking queries from the narrative: the first
1,050 characters of the complaint plus the remedy, once framed and once bare.
A long complaint lost everything after that cut. The first real long prompt
complaint (2,662 characters once its remedy was extracted) had its
forced-fulfilment sentence at character 1,109. Without the consumer's own
citation (ADR 0025), its notice cited CDC art. 35, III, the refund the
consumer had refused.

The golden set cannot measure this: its complaints run 60 to 252 characters.
So ten labelled development cases were rewritten as long complaints, 1,750
to 2,320 characters:
- invented detail;
- the key facts at the start, straddling the cut, or entirely past it;
- the source case's remedy, labels and hard negatives;
- no article cited.

They are now `eval_data/consumer_long_complaints` (development only). Every
variant was then measured on the configured stack (JUÁ 4B, PostgreSQL BM25),
through the production retrieval, selection and scoring code. The variants
differed only in their queries and their support rule:

| Variant | Right | Known-wrong | Unlabelled | Refused |
|---|---|---|---|---|
| Today: first 1,050 characters + remedy, two queries | 5 | 3 | 6 | 2 |
| Windows + remedy, every agreeing hit kept | 10 | 6 | 36 | 0 |
| Windows + remedy, the remedy or two searches needed | 9 | 3 | 13 | 0 |
| **Windows + remedy, the remedy, two searches, or one window on official text** | **10** | **3** | **13** | **0** |
| Today's two queries + windows + remedy, same rule | 10 | 3 | 17 | 0 |
| Windows + remedy, one provision per window | 9 | 5 | 24 | 0 |

- **Keyword-only fallback.** Today refused 9 of the 10 and cited 1 right
  article. Windows + remedy refused 4 and cited 4 right articles and 1
  known-wrong one (CDC art. 18 in the withdrawal case).
- **The motivating complaint, citation removed.** Today cites 35 III. The
  chosen variant cites 35 I, 18 § 4 and 20 II.

## Decision

1. **Ranking queries for a long complaint.** A complaint that fits the
   narrative (at most 1,050 characters, every golden case) keeps its two
   queries unchanged. A longer one is searched as:
   - the remedy alone, cut at 500 characters as before;
   - whole-sentence windows of at most 600 characters, each bounded like any
     query.

   The windows total at most 2,400 characters, which with the remedy is about
   the cost of the two narrative queries. Past the budget, the last window
   stays (the latest state of the dispute) and windows join from the start
   (what was bought and what went wrong) while they fit. A repeated window is
   searched once. `build_legal_queries` still returns every ranking query.
2. **Windows run as their own batch** under the agent `consumer_legal_window`,
   so they get their own embedding call and timeout. A long complaint gets no
   extra degraded-mode corroboration queries: its windows and remedy already
   are independent formulations.
3. **The window rule in the support gate (policy v8).** A provision that only
   window traces agree on is supported when any of these holds:
   - the remedy search or a second window agrees on it;
   - one of its agreeing chunks is official text rather than an alias.

   Provisions come from the trace items' `source_provision_id`; without it,
   each chunk is its own key. Everything else is unchanged: other traces, the
   alias cap, the depth and degraded corroboration.
4. **The refusal hint** reports the characters actually searched, and no
   longer says that only the start is read.
5. **Versions.** The query builder becomes `consumer-legal-narrative-v8`, and
   the ground policy becomes `consumer-notice-scope-eligibility-v8`. CI reports
   the long set ungated.

## Consequences

- (+) **On the long set, through the production evaluation runner on the
  configured stack** (`--require-cached-queries --require-semantic`):
  - right citations go from 5 to 10, and refusals from 2 to 0;
  - known-wrong citations stay at 3;
  - exact recall goes from 0.25 to 0.65, and article recall from 0.40 to 0.85;
  - precision goes from 0.35 to 0.45.

  The run reproduces the probe row exactly, case by case.
- (+) **The motivating complaint, citation removed,** now cites CDC art. 35, I
  (with 18 § 4 and 20 II). Under the budget, its middle window is dropped. In
  keyword-only fallback it cites 18 § 4, where it was refused.
- (+) **CI is unaffected.** The four gated offline evaluations are identical
  to the previous version, except the two version strings: every golden
  complaint fits the narrative.
- (-) **Unlabelled citations double on the long set** (6 to 13), so notices
  get longer. Recurring ones are CDC art. 20, II (service defect) in two
  product cases and the motivating one, art. 35, I in two cases without an
  offer, and art. 51, I. Every notice stays `requires_legal_review`.
- (-) **The three known-wrong citations remain:**
  - CDC art. 18 § 2 and art. 49 in the repair case;
  - CDC art. 18 in the withdrawal case.

  Today's queries made the same mistakes in different places.
- (-) **The sample is small.** Ten cases, written by the developer, with
  labels inherited from developer-authored seeds. The combining rule was
  chosen on them, so they are not a held-out measurement.
- (-) **A complaint longer than about 2,400 characters still loses its
  middle.** The budget trades coverage for the embedding timeout: on this CPU
  JUÁ embeds about 12 characters per second.

## Follow-ups

- **Review the long set and promote it.** Once reviewed, move it into the
  gated golden set, and add an in-scope refusal-rate metric there.
- **Measure the budget on the production host.** Its CPU sets how much of a
  long complaint fits the 300 s guard.
- **Reduce the recurring unlabelled grounds.** Review the aliases that carry
  them (CDC 20 II, 35 I, 51 I).
