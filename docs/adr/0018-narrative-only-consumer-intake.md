# ADR 0018: Narrative-only consumer intake

## Status

Accepted · Date: 2026-09-14 · Amends: [ADR 0012](0012-bounded-consumer-extrajudicial-notice.md)

## Context

The Consumer intake asked the consumer to self-classify the complaint into
one of seventeen fixed issue categories (`ConsumerIssueCategory`). That label
drove two things: it selected one of seventeen keyword expansions appended to
the retrieval queries, and it gated a branch of the consumer-scope check.
Both uses were weaker than they looked. The category was a lay
self-classification of a narrative that often spans several problems, so a
wrong pick could steer retrieval toward the wrong provisions without the
consumer ever knowing. And the scope-gate branch that read the category
(`_OUT_OF_SCOPE_CATEGORIES`) held strings — `"employment"`, `"labor"`,
`"neighbor_dispute"`, `"no_consumer_relationship"` — that were never members
of `ConsumerIssueCategory`; every call site passed the enum's own value, so
that branch was unreachable for the system's entire life. The gate's real
work was always done by narrative signals alone.

A specific case motivated re-examining the whole mechanism: a consumer
charged for a service they never requested was classified under
`unauthorized_charge`, whose keyword expansion pulled in the Civil Code's
undue-payment chapter (arts. 878, 880, 881) — a chapter premised on a payment
that was made and should not have been. Nothing was paid. Article 880 of
that chapter argues the *supplier's* side of an undue-payment dispute. The
category label, not the consumer's actual words, put those articles in the
notice.

The first fix tried was narrower than removing the category outright: keep
two queries, drop the seventeen per-category expansions, and replace them
with one fixed legal-vocabulary string appended to every query
(`LEGAL_LEXICON`). Measured on the 22-case offline stack (a hashed
128-dimension embedder, effectively BM25 — this is what CI runs), that
single lexicon scored worse than appending nothing at all:
`article_recall@5` 0.100, `recall@5` 0.000, 3 known-bad citations, against
0.225 / 0.142 / 0 known-bad with no injected vocabulary. A dose-response
test confirmed the direction: the more generic legal vocabulary appended to
every query, the worse retrieval got — 11 concepts scored 0.100, 3 concepts
0.175, no injected terms 0.225. The mechanism is dilution: identical tokens
appended to every query drown the case-specific signal that distinguishes
one complaint from another, regardless of whether those tokens are routed by
a category label or fixed for everyone.

## Decision

1. The Consumer intake collects no issue category. Retrieval queries are
   built from the complaint and the requested remedy alone.
2. The query builder (`consumer-legal-narrative-v6`) returns two queries and
   injects no vocabulary into either: query 1 frames the same narrative as a
   consumer situation and asks to locate directly applicable provisions;
   query 2 is the bare narrative. A fixed legal lexicon was tried between the
   category expansions and this design and was removed after measurement —
   it diluted retrieval on every offline test and added no benefit on the
   configured stack (see Consequences).
3. The consumer-scope gate decides from the narrative alone. The category
   branches it used to read were already dead: `_OUT_OF_SCOPE_CATEGORIES`
   held strings that were never members of `ConsumerIssueCategory`, so that
   branch was unreachable for the system's entire life. Removing it changes
   no observed behavior.
4. `ConsumerFactsPatch` forbids extra fields (`extra="forbid"`), so a client
   that still sends `issue_category` gets HTTP 422 instead of the field being
   silently ignored. The break is intentional and loud.
5. The notice has no subject line, and every ground's rationale credits the
   consumer's account rather than an intake label.

## Consequences

This change trades breadth for precision, and the trade is not free.

- (+) On the target complaint — a service the consumer never requested — the
  configured JUÁ stack (single-case measurement; see below) stopped citing
  Civil Code arts. 878, 880 and 881, the undue-payment chapter premised on a
  payment that never happened, with art. 880 arguing the supplier's side. CDC
  art. 52 started being cited instead.
- (+) A wrong or absent self-classification can no longer misdirect
  retrieval, because there is nothing left for a consumer to misclassify.
- (+) The `_OUT_OF_SCOPE_CATEGORIES` dead branch and the `_CATEGORY_EXPANSIONS`
  and `LEGAL_LEXICON` routing tables are gone; the retrieval query path has
  one fewer layer between the consumer's own words and the corpus.
- (-) **Article recall dropped by more than half on the stack CI runs.** On
  the offline stack (hashed 128-dimension embedder, effectively BM25),
  `consumer_article_recall@5` fell from 0.482 to 0.225, `consumer_recall@5`
  from 0.175 to 0.142, and `consumer_ndcg@5` from 0.164 to 0.069. The "before"
  figures were measured on the 21-case dataset; the "after" figures on 22
  (Task 9 added one case). `consumer_hard_negative_rate@5` held essentially
  flat, 0.029 to 0.027. Evaluation notices now cite roughly half as many
  grounds — 23 vs. 46 — and zero known-bad citations instead of 1. A reader
  of this ADR six months from now should not be surprised that recall is
  lower than it used to be: it is, measurably, and that is the cost of this
  design, not an oversight.
- (-) **The largest measured cost: `consumer_notice_exact_recall` fell about
  ninefold, 0.149 → 0.017, on the offline stack.**
  (`tests/test_consumer_notice_evaluation.py:165` now;
  `8387881:tests/test_consumer_notice_evaluation.py:155` before.) This is the
  notice-level metric closest to "did the notice cite a provision the golden
  set labelled relevant" — at 0.017
  the offline notices almost never do. Put plainly: notices got shorter
  *and* their hit rate on labelled-relevant provisions collapsed by roughly
  nine times. That combination is consistent with retrieval being weaker
  overall on this stack, not merely more selective about what it cites. This
  is the cost most likely to matter to a reader six months out, and it is
  not softened here: on the metric that most directly checks whether a
  notice cites what it should, this design measurably underperforms what it
  replaced.
- (-) **A fixed lexicon was tried and made things worse, not better.** With
  one lexicon string appended to every query, offline `article_recall@5` was
  0.100 and `recall@5` was 0.000 — worse than appending nothing — with 3
  known-bad citations. The dose-response test (11 concepts → 3 → none: 0.100
  → 0.175 → 0.225) showed the effect is monotonic dilution, not noise. There
  is currently no substitute mechanism bridging lay vocabulary to legal
  terms; a future design would need to earn its keep against this same
  offline instrument before being reintroduced.
- (-) **The configured-stack evidence is a single case, not an aggregate.**
  The full 22-case run on the configured JUÁ embedder could not complete on
  this machine (four attempts, including two segfaults and one OOM-kill,
  even after a bf16 monkeypatch cut resident memory roughly in half). Only
  the one case that motivated the change — the unrequested-service
  complaint — was measured end to end on that stack. The fix to that case's
  specific defect is confirmed; the aggregate effect on the configured stack
  is not.
- (-) CDC art. 39 III, the literal statutory hypothesis for an unrequested
  service, is still not retrieved on the configured stack for the case that
  motivated this change, even though both queries carry the complaint's own
  words about the unrequested service. That is a retrieval/embedding gap
  that narrative-only queries do not close by themselves; see follow-ups.
- (-) Any lay-vocabulary-to-legal-term bridge that a future design wants to
  reintroduce needs a different mechanism than "append fixed text to every
  query" — that specific mechanism is now a measured, rejected approach, not
  an untried one.

## Follow-ups (measured, out of scope here)

- `MIN_GROUND_SCORE_RATIO = 0.5` in `app/consumer/ground_selection.py` is
  calibrated for reciprocal-rank fusion's compressed score distribution.
  With the already-implemented cross-encoder reranker enabled, whose scores
  are far more spread, the same ratio discards correct grounds: on the
  target case, CDC art. 52 ranked 2nd and 3rd and CDC art. 6 ranked 4th, and
  the floor cut art. 6 by three hundredths, collapsing the notice to a
  single ground. Making that floor score-type aware is the change that would
  let the reranker be enabled.
- CDC art. 39 III never reaches the candidate set on the configured stack,
  even though both queries carry the complaint's own words about an
  unrequested service. That is a retrieval/embedding gap, not a ranking one,
  and no reranker or ground-selection change can fix it.
- The contested debt amount is booked as `direct_loss_amount`, so an amount
  the supplier demands becomes the consumer's "prejuízo direto" and then the
  public settlement proposal. This predates and is independent of the
  category removal, but it compounds the unrequested-service scenario this
  ADR discusses.
- A fact-to-ground coherence gate: with no payment confirmed in the facts,
  CDC art. 42 sole paragraph and the Civil Code's undue-payment chapter
  should be ineligible regardless of retrieval rank, instead of relying on
  retrieval to avoid surfacing them.
