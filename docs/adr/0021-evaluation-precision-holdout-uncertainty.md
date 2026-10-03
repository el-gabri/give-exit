# ADR 0021: Evaluation that sees precision, a holdout and uncertainty

## Status

Accepted · Date: 2026-10-02 · Amends: [ADR 0011](0011-retrieval-traceability-and-evaluation.md)

## Context

A review of the RAG found that the evaluation steering it could not see what
it most needed to:

- The notice gates (`known_bad_citations <= 0`, `abstention = 1.0`) were both
  satisfied by a selector that cites nothing, and no metric counted grounds
  that were neither labelled nor known-bad. At agreement depth 20 the LLM
  verifier removed 34 of 57 grounds without touching a labelled hit.
- Exact recall scored units only: citing CDC art. 18 § 1 where the label is
  art. 18 § 1 II scored 0.
- The scope gate's keyword list holds one phrase per golden no-ground case,
  two of them added just before their cases. Abstention 1.0 had no held-out
  evidence, and no case was held out from tuning at all.
- Averages over a few dozen cases came with no sense of their uncertainty,
  and depth and stemming decisions turned on a single citation.
- The configured JUÁ stack has never completed an aggregate run on the
  development machine, and the configured retrieval evaluation scored a
  lexical-only fallback as hybrid.

## Decision

1. **Ground classes.** Every cited ground is exactly one of known-bad (matches
   a hard negative), labelled (its article is labelled for the case) or
   unlabelled, in that precedence. The notice evaluation counts each class,
   reports `consumer_notice_precision` (labelled ÷ cited, only for a case that
   cited something, so an empty run produces no precision) and
   `consumer_notice_article_recall` beside exact recall. No partial credit.
2. **Splits.** Golden cases carry `split: development | holdout` (dataset
   2.2.0). The holdout is the 13 in-scope cases added in 2.1.0 and six new
   disputes with no consumer relationship, written without consulting the
   scope keywords. Parameters are tuned on development only; a holdout case
   whose result informs a decision moves to development in the next version.
3. **Gates.** CI gates the development split and adds
   `--min consumer_notice_labelled_grounds=1`. The holdout is reported and
   uploaded on every build, never gated.
4. **Uncertainty.** Every summary and every split carries 95% case-bootstrap
   intervals (2,000 resamples, seed 0). `python -m app.evaluation.compare`
   pairs two runs by case and reports each metric's and count's difference
   with a paired-bootstrap interval.
5. **Query-vector cache.** Configured evaluations read golden query vectors
   from `data/evaluation/query_vectors/<contract_id>/`, keyed by the embedding
   contract (model, revision, dimension, normalization, formatter versions,
   instruction hash) and the SHA-256 of each query; no query text is stored.
   `python -m app.evaluation.query_vectors` fills it one case at a time and
   resumes after a crash; `--require-cached-queries` forbids loading the model
   and checks coverage before any query runs, because a miss inside a run
   would surface below the query guard as a silent lexical-only fallback.
   Unpinned models are never cached. Production never persists a query.
6. **Degraded runs fail.** A configured retrieval case that falls back to
   lexical-only is a failed case (exit 2), not a hybrid result.
7. **No bf16 setting.** With the cache, evaluation no longer needs it, and a
   dtype would have to enter the generation identity.

## Measurements (offline stack, agreement depth 13, 2026-10-02)

| Split | Grounds | Labelled | Unlabelled | Known-bad | Precision | Article recall | Abstention |
|---|---|---|---|---|---|---|---|
| development (24) | 17 | 1 | 16 | 0 | 0.042 | 0.025 | 1.0 |
| holdout (19) | 9 | 1 | 8 | 0 | 0.167 | 0.077 | 0.667 |

All six new no-ground cases pass the scope gate; the agreement gate keeps
four of them from citing anything, and two still cite CDC articles. Held-out
abstention at the scope gate is therefore 0/6, and the 1.0 measured on
development reflects the keyword list rather than the rule. The offline dense
channel is a hashed bag of words, so these numbers are a tripwire, not
evidence about the configured stack.

## Consequences

- (+) A selector that cites nothing, or cites mostly unlabelled articles, now
  shows up in the gates and the reports.
- (+) Retrieval changes can be compared case by case with intervals, on cases
  no parameter was tuned on.
- (+) The configured stack becomes measurable once its golden query vectors
  are cached, without loading the 4B model again.
- (−) The holdout is small; its intervals are wide.
- (−) The scope gate's overfit is now visible and not fixed here.
- Follow-ups: document-side aliases, light stemming measured on the
  configured stack, the gate structure and sibling collapse, each decided with
  `compare` on the development split.
