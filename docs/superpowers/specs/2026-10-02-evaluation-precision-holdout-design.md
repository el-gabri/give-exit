# Evaluation that sees precision, a holdout and uncertainty — design

- Date: 2026-10-02
- Status: approved section by section in brainstorming; pending review of this written spec
- Classification: architectural — changes the evaluation schemas, the golden dataset, the CI
  gates and adds an evaluation-only embedding cache
- Branch: `feat/evaluation-precision-holdout`

## 1. Goal

Make the Consumer retrieval and notice evaluation trustworthy enough to steer the next retrieval
changes (document-side aliases, light stemming, the gate structure, sibling collapse). Today it
cannot: it does not see precision, it scores only exact units, it has no held-out cases, it gives
no sense of uncertainty, and the configured JUÁ stack cannot complete an aggregate run on the
development machine.

Success means:

1. The notice evaluation reports labelled, unlabelled and known-bad grounds, notice precision and
   notice article recall.
2. CI fails a system that cites nothing.
3. Every summary carries a development/holdout breakdown and bootstrap intervals, and a compare
   tool reports paired differences between two runs.
4. The configured evaluation runs from cached golden query vectors without loading JUÁ; the cache
   fills resumably, one case at a time.
5. A configured retrieval evaluation that degrades to lexical-only fails instead of being scored
   as hybrid.

No production behaviour changes.

## 2. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | bf16 dtype setting | Out of scope. The cache makes it unnecessary for evaluation, and a dtype would have to enter the document identity of embedding generations. |
| D2 | Abstention evidence | Author six new no-ground cases as holdout, written from categories of non-consumer disputes, without checking them against the scope keyword list and without changing the scope gate in this work. |
| D3 | Recall credit | Add notice article recall beside exact recall. No partial or hierarchical credit. |
| D4 | Cache placement | An evaluation-only wrapper around the configured embedder. Never in `QueryEmbeddingGuard`, which serves real complaints. |
| D5 | Split representation | A first-class, validated `split` field on golden cases, not a free-form slice. |
| D6 | Gating | CI gates run on the development split. The holdout is reported and uploaded, never gated. |

## 3. Non-goals

- Any change to retrieval, ground selection, the scope gate, the corpus, the chunking or the
  embedding model. This work measures; it does not tune.
- The retrieval benchmark's `consumer_article_recall@k`, which counts a hard-negative unit of a
  labelled article as an article hit. It stays as it is; only the notice metrics are new.
- Renaming `consumer_notice_semantic_success` (it means "not degraded", which is a validity check,
  not a quality score). Its behaviour and `--require-semantic` are unchanged.
- Caching corroboration queries. They run only when retrieval degrades, which the cache prevents.
- Legal validation. New golden cases remain `requires_legal_review`.

## 4. Evidence (from the 2026-10-02 review)

- The CI notice gates are `--max consumer_notice_known_bad_citations=0` and
  `--min consumer_notice_abstention=1.0`. A run that cites nothing satisfies both.
- At agreement depth 20 the LLM verifier removed 34 of 57 grounds without removing a labelled hit
  (ADR 0019). No metric reports grounds that are neither labelled nor known-bad.
- `_cites` in `app/evaluation/consumer_notice.py` scores exact units only. In
  `produto_duravel_com_vicio` the notice cites CDC art. 18 § 1 while the label is art. 18 § 1 II,
  which scores 0 although the article is right.
- `_NON_CONSUMER_SIGNALS` contains one phrase per golden no-ground case ("meu vizinho",
  "salário"/"vale-transporte", "multa de trânsito", "emprestei dinheiro"). The last two were added
  in `d3e0f93`, immediately before the cases themselves (`9fd3590`). Abstention = 1.0 has no
  held-out evidence.
- The 13 in-scope cases added in dataset 2.1.0 postdate the agreement-depth decision (ADR 0019,
  measured on the 22 cases of 2.0.0) and were never used to tune a parameter.
- A full configured run (JUÁ 4B, 2560 dimensions) has never completed on the development machine;
  a single case does.
- `_LazyConsumerRetriever.__call__` calls `RagPipeline.retrieve`, which discards the trace. A query
  embedding timeout makes `QueryEmbeddingGuard` fall back to lexical-only and the case is scored as
  hybrid.

## 5. Design

### 5.1 Notice metrics

`notice_ground_metrics` classifies every cited ground into exactly one class, in this precedence:

1. **known-bad** — matches a hard negative (`is_known_bad_citation`, unchanged);
2. **labelled** — its `provision_id` is the `article_id` of one of the case's relevant judgments;
3. **unlabelled** — everything else.

A ground citing CDC art. 39 III in a case labelled art. 39 I with art. 39 III as hard negative is
known-bad, not labelled.

New counts, summed into `totals`:

| Count | Meaning |
|---|---|
| `consumer_notice_labelled_grounds` | grounds in class 2 |
| `consumer_notice_unlabelled_grounds` | grounds in class 3 |

`consumer_notice_grounds`, `consumer_notice_known_bad_citations` and
`consumer_notice_complementary_grounds` are unchanged, so grounds = known-bad + labelled +
unlabelled.

New per-case metrics, averaged:

| Metric | Cases | Score |
|---|---|---|
| `consumer_notice_article_recall` | in-scope (has relevant judgments) | distinct labelled articles cited by a labelled ground ÷ distinct labelled articles |
| `consumer_notice_precision` | any case that cited at least one ground | labelled grounds ÷ grounds |

A case that cites nothing produces no `consumer_notice_precision` entry, so a run that cites
nothing produces no precision at all and a `--min` gate on it fails with "not produced by this
run". A no-ground case that cites something scores precision 0. A failed case scores
`consumer_notice_article_recall` 0 when in scope and produces no precision entry.

`consumer_notice_exact_recall` and `consumer_notice_abstention` are unchanged.

### 5.2 Split

`ConsumerLegalGoldenCase` gains `split: Literal["development", "holdout"] = "development"`. The
default keeps every existing test fixture valid; the dataset file states the split of every case
explicitly.

Dataset 2.2.0:

- holdout: the 13 in-scope cases added in 2.1.0 (`cartao_de_credito_nao_solicitado` through
  `loja_nega_garantia_e_manda_ao_fabricante`) and the new no-ground cases (5.3);
- development: everything else, including `multa_de_transito` and `emprestimo_entre_amigos`.

`CaseResult` gains `split: str | None = None`, filled by both evaluators.
`EvaluationSummary` gains `by_split: dict[str, EvaluationGroupSummary]`, grouped like `by_slice`.
Top-level `averages` and `totals` still cover every evaluated case.

`consumer_runner` gains `--split {all,development,holdout}` (default `all`). It evaluates only the
cases of that split; `EvaluationRunMetadata` gains `case_split: str | None` recording the choice,
and `dataset_sha256` remains the hash of the cases actually evaluated. `label_ranks` gains the same
option.

Policy, stated in the dataset description and ADR 0021: parameters are tuned on development only.
A holdout case whose result informed a decision moves to development in the next dataset version.

### 5.3 New holdout no-ground cases

Six cases, lay Portuguese narratives in the style of the existing seed, each with
`no_applicable_ground: true`, `category: "no_consumer_relationship"`, slices `ground:none`, a
`domain:*` slice and `wording:lay`, two or three plausible CDC hard negatives, and
`split: "holdout"`. Categories:

- a small business unpaid by its corporate client (B2B);
- a residential lease with a private landlord, without an agency;
- an INSS benefit denial;
- an income-tax dispute with the Receita Federal;
- a car collision with a private driver;
- a condominium-fee dispute with the condominium.

They are written without checking them against `_NON_CONSUMER_SIGNALS`, and the scope gate is not
changed in this work. Whatever the offline and configured stacks do on them is recorded as found.

### 5.4 Bootstrap intervals

New module `app/evaluation/intervals.py`:

- `bootstrap_interval(scores, *, resamples=2000, level=0.95, seed=0) -> MetricInterval`:
  percentile bootstrap of the mean. Resample indices are drawn as `int(rng.random() * n)` from
  `random.Random(seed)`, so results are identical on Python 3.10 and 3.12. One score yields a
  zero-width interval; no scores raise `ValueError`.
- `paired_difference_interval(before, after, ...)`: the same, over per-pair differences
  (after − before).

`MetricInterval` (in `app/schemas/evaluation.py`) holds `low`, `high`, `resamples`, `level` and
`seed`, rounded to three decimals like the averages.

`EvaluationSummary` and each `by_split` group gain `intervals: dict[str, MetricInterval]`, one per
averaged metric, computed from the same per-case scores as the average. `by_slice` and
`by_category` groups get none (most hold one or two cases).

### 5.5 Compare tool

New CLI `python -m app.evaluation.compare BEFORE.json AFTER.json [--split NAME]`, reading two
summaries as written by `consumer_runner --output` (sweep files are not supported).

- Cases are paired by `case_name`. Cases present in one file only are listed and excluded. With no
  shared case it exits 2.
- `--split` keeps only pairs whose `split` matches in both files.
- For every metric both cases of a pair report: pairs, mean before, mean after, mean difference
  and its 95% paired-bootstrap interval.
- For every count: total before, total after, total difference and the interval of the total
  difference (pairs × the interval of the mean per-case difference).
- It prints the run-metadata fields that differ (dataset hash, case split, corpus release, query
  builder, retrieval mode, embedding model and revision, reranker, agreement depth, ground policy,
  verifier).
- Output is a text table; exit code 0 when it ran. It makes no decision.

### 5.6 Query-vector cache

New module `app/evaluation/query_vectors.py`.

**Contract key.** `contract_id` is the first 16 hex characters of the canonical-JSON SHA-256 of
`RagPipeline.embedding_contract_configuration()` without `require_model_revision` (which does not
affect vectors). Any change of model, revision, dimension, normalization, formatter version or
instruction hash yields another cache. A contract without a model revision is never cached:
`configured_pipeline` then returns the plain pipeline, and `--require-cached-queries` fails.

**Storage.** `<settings.data_dir>/evaluation/query_vectors/<contract_id>/` holding `contract.json`
(the keyed contract) and an append-only `vectors.jsonl`, one
`{"query_sha256": ..., "vector": [...]}` per line. No query text is stored. `data/evaluation/` is
gitignored.

- On load, every vector is validated: finite values, the contract's dimension when it declares one,
  L2 norm, all through `validate_embedding_vectors`. A final line that does not parse is a crash artifact:
  it is dropped and the file is rewritten atomically without it before anything is appended. A
  malformed line anywhere else, or a `contract.json` that does not match the key, raises.
- Duplicate hashes keep the first vector.
- Appends write, flush and `fsync` once per batch. One writer at a time is assumed.

**Wrapper.** `CachedQueryEmbedder(inner, cache, *, require_cached=False)` implements the embedding
protocol:

- `embed_queries` serves cache hits; misses go to `inner.embed_queries` in one call, are validated,
  appended and returned in query order. With `require_cached=True` a miss raises
  `QueryVectorCacheMiss` naming how many queries are missing, and the model is never loaded.
- `embed_query` delegates to `embed_queries`; `embed_documents` and `embed` pass through.
- Every other attribute is forwarded to `inner` (`__getattr__`), so `RagPipeline` reads the real
  model name, revision, instruction and formatter versions, and traces and index versions are
  identical to an uncached run.

**Wiring.** `configured_pipeline` builds the real embedder with `create_embedding_client`, wraps it
and passes it as `embedder=` to `create_rag_pipeline`, then binds the cache opened for
`pipeline.embedding_contract_configuration()`. Read-through is the default for every configured
evaluation (`consumer_runner`, the notice evaluation, `label_ranks`).
`consumer_retrievers.configure_query_vectors(require_cached: bool)` sets the policy; the
`--require-cached-queries` option of `consumer_runner` and `label_ranks` calls it with `True`.

**Builder CLI.** `python -m app.evaluation.query_vectors [dataset] [--case ID]... [--check]`:

- builds the configured pipeline without requiring the legal index (query vectors do not depend on
  it);
- for each in-scope case (scope gate passes) of the dataset, or only the named cases, embeds the
  missing `build_legal_queries` vectors in one model call through the wrapper, not through
  `QueryEmbeddingGuard`, so there is no timeout and no lexical fallback; each case is persisted
  before the next starts, so a crash loses at most one case;
- prints, per case, cached/embedded counts and a final coverage line;
- `--check` embeds nothing, prints coverage and exits 1 if any query is missing.

### 5.7 Degraded configured retrieval

`_LazyConsumerRetriever.__call__` calls `retrieve_with_trace` and raises
`DegradedRetrievalError("retrieval degraded: <mode>")` when the trace has a `degraded_mode`. The
runner already records an exception as a failed case, and a run with failed cases exits 2. The
notice evaluation keeps reporting degradation through `consumer_notice_semantic_success`.

### 5.8 CI

- Retrieval gates: `--split development`, thresholds re-measured on the offline stack after
  implementation and set at the measured values in the existing manner.
- Notice gates: `--split development`, existing gates kept, plus
  `--min consumer_notice_labelled_grounds=<offline development baseline>`.
- A new step runs the retrieval and notice evaluations with `--split holdout --output ...`,
  without gates, and its outputs join the uploaded evaluation evidence.

## 6. Testing

Test-first, per unit:

- notice metrics: the three-way classification and its precedence, precision omitted for a
  zero-ground case, precision 0 for a no-ground case with grounds, article recall with repeated
  units of one article, failed-case metrics;
- split: validation of the literal, the default, `CaseResult.split`, `by_split`, `--split`
  filtering and `case_split` in the run metadata;
- intervals: determinism, a hand-checked small case, the zero-width and empty cases, the paired
  variant;
- compare: pairing, excluded unpaired cases, `--split`, count totals, metadata differences, the
  no-shared-case exit;
- cache: round trip, a truncated final line recovered and rewritten, a malformed inner line
  rejected, contracts isolated, a revision-less contract not cached, the strict miss, vector
  validation, attribute forwarding observed in a pipeline trace;
- builder CLI: resume (a second run embeds nothing), `--case`, `--check` exit codes;
- degraded retriever: a forced lexical fallback fails the case.

`app.evaluation.intervals`, `app.evaluation.query_vectors` and `app.evaluation.compare` join the
100% coverage scope in `pyproject.toml`. Existing tests pinning dataset version 2.1.0 and its case
counts move to 2.2.0.

## 7. Documentation

- ADR 0021, "Evaluation that sees precision, a holdout and uncertainty", recording D1–D6, the new
  metrics and the tuning policy.
- README "Evaluation and verification": the new metrics, `--split`, the compare tool and the cache
  commands.
- The dataset description names the split policy.

## 8. Risks and follow-ups

- The holdout is small (13 in scope plus six no-ground cases), so its intervals will be
  wide. That is the honest state of the evidence; the remedy is more reviewed cases.
- The new no-ground cases will likely lower holdout abstention. That is expected and is not fixed
  here; any scope-gate change belongs to later work, measured on development, after which those
  cases move to development.
- The cache trusts that a pinned revision plus the formatter versions determine query vectors.
  CPU batch numerics differ slightly by batch composition (the reuse canary measured 0.999887
  similarity); cached vectors are therefore a reproducible measurement, not bit-identical to a
  live query.
- After this work: document-side aliases, light stemming measured on the configured stack, the
  gate structure and sibling collapse (steps 2–4 of the review), each decided with `compare` on the
  development split.
