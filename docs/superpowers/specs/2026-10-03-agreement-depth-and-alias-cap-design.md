# Agreement depth 10 and an alias support cap — design

Date: 2026-10-03 · Step 4 of the RAG review, on branch `feat/lay-aliases` · Builds on
[ADR 0019](../../adr/0019-explicit-retrieval-agreement.md) and
[ADR 0022](../../adr/0022-lay-language-alias-chunks.md)

## 1. Problem

The lay alias chunks of ADR 0022 lift retrieval on the configured stack (JUÁ, development:
article recall@5 0.438 → 0.733), but the notice cites twice as many grounds. Development
notice precision falls 0.390 → 0.312 and the known-bad count rises 1 → 2, so the stop
rules of the alias spec fired. The cause is in the agreement gate: an alias chunk is a few
lay sentences, and both channels score the same paraphrase. Its two-channel "agreement"
is not two independent signals, and 66 of the 67 development grounds pass the gate
through an alias.

## 2. Intent and success criteria

The user's goal is to recover the notice's safety on JUÁ while keeping the alias recall
gain, and to merge steps 2 and 4 together only if the numbers recover.

On the configured development split, paired against the v4 configured baseline:

- notice precision ≥ 0.390 (v4);
- known-bad citations ≤ 1 (v4);
- notice article recall clearly above v4. The user accepted the chosen configuration's
  +0.171, whose 95% interval [−0.004, +0.371] touches zero, as meeting this.

The holdout is measured once, after the configuration is fixed, and reported only.

## 3. Evidence behind the decision (spikes, 2026-10-03)

All spikes ran on JUÁ with the golden query cache, the v5 index and the development split.
They replaced the gate or the retrieval inside a throwaway script, and nothing was
tuned on the holdout.

| configuration | precision | article recall (interval vs v4) | unlabelled | known-bad |
|---|---|---|---|---|
| depth 13 (today) | 0.312 | 0.608 [+0.013, +0.375] | 48 | 2 |
| depth 12 | 0.308 | 0.592 [−0.004, +0.371] | 47 | 1 |
| depth 10 | 0.457 | 0.592 [−0.004, +0.371] | 29 | 1 |
| **depth 10, at most 3 alias chunks per query** | **0.491** | **0.592 [−0.004, +0.371]** | **24** | **0** |
| depth 10, at most 2 alias chunks per query | 0.546 | 0.517 [−0.117, +0.325] | 19 | 0 |
| depth 8 | 0.430 | 0.475 [−0.150, +0.258] | 26 | 0 |
| depth 10, alias passes only beside its official text in the dense pool | 0.583 | 0.367 | 10 | 1 |
| grounds per notice capped at 4 or 5 | within 0.02 of uncapped | unchanged | | |

What the spikes ruled out:

- **Sibling collapse before the k = 8 cut**, which counts provisions instead of chunks in
  each query's top 8, changed no ground in any of the 20 in-scope development cases. It
  admitted one extra gate-passing chunk in total. A chunk both channels rank within the
  gate depth already sits inside its query's fused top 8, and ground selection already
  works per provision. It is dropped.
- **An alias-specific depth** gave the same numbers as the global depth, because almost
  every ground comes through an alias.
- **Requiring the official text to back an alias** removes the recall the aliases exist for.

Exact recall stays 0.400 at depth 10, so no exact hit is lost against depth 13. Precision
moves sharply between depths 10 and 12 (0.457 against 0.308); on 20 paired cases the
choice is sensitive, and ADR 0023 says so.

## 4. Design

### 4.1 Agreement depth

`AGREEMENT_MAX_RANK` in `app/consumer/legal_policy.py` goes from 13 to 10. The comment is
rewritten:

- the history: depth 13 was chosen before aliases, because every exact hit then ranked
  within 12;
- the alias evidence above;
- a pointer to ADR 0023.

### 4.2 Alias support cap

A new constant, `ALIAS_SUPPORT_CAP = 3`, sits in `legal_policy.py`.
`strongly_supported_chunk_ids(traces, *, max_rank=AGREEMENT_MAX_RANK,
alias_cap=ALIAS_SUPPORT_CAP)` applies it on the two-channel path only:

- within each trace, in result (fused) order, an alias chunk whose channels agree counts as
  supported only while fewer than `alias_cap` alias chunks of that trace have already
  counted;
- an alias chunk that does not agree takes no slot;
- official chunks are never capped;
- the cap is per query (per trace), not per notice.

The degraded single-channel path, top `CORROBORATION_RANK` (3) in two independent
queries, is unchanged: a cap of 3 can never bind there. The keyword parameter exists so
the evaluation can measure other caps, as `max_rank` does for depths. There is no new
CLI flag.

### 4.3 Recognising alias chunks

The gate sees only trace chunk ids. `app/consumer/aliases.py` gains
`alias_chunk_id(document_id, unit_key) -> str`, the one place that writes
`…:legal:<unit_key>:alias-01`, and `is_alias_chunk_id(chunk_id) -> bool`. They live there,
not in `legal_corpus.py`, because `legal_corpus.py` imports `legal_policy.py`, so the
gate importing from it would create a cycle; `aliases.py` imports neither.
`LegalCorpus._alias_chunks` builds its ids with `alias_chunk_id`, and the gate uses
`is_alias_chunk_id`. `LegalCorpus.unit_for_chunk` keeps its unit-specific match.

### 4.4 Policy version

`LEGAL_GROUND_POLICY_VERSION` goes from `consumer-notice-scope-eligibility-v5` to `-v6`, so
evaluation runs record the new gate.

### 4.5 Unchanged

The index (no re-embedding), the aliases, citations and the citation swap, the eligibility
policy, the CDC anchor, ground selection, the precedence window and retrieval itself stay as
they are. Retrieval metrics do not pass through the gate and are not re-measured.

## 5. Verification and measurement

### 5.1 Acceptance on the configured stack

With the gate implemented, the configured development notice evaluation
(`--notice-pipeline configured --require-cached-queries`) must reproduce the spike exactly:

- precision 0.491;
- article recall 0.592;
- exact recall 0.400;
- 40 grounds: 16 labelled, 24 unlabelled;
- known-bad 0;
- no failed or degraded case.

A difference means the production gate does not do what the spike did. Execution stops and
investigates; it does not re-tune.

Two paired comparisons on development:

- against `v4-configured-notice-development.json`, the decision against the bar;
- against `v5-configured-notice-development.json` (depth 13), the effect of step 4 alone.

The baselines are in `data/evaluation/baselines/2026-10-03-lay-aliases/`.

### 5.2 Holdout

Once the acceptance run matches, the configured holdout notice evaluation runs once and is
compared with `v4-configured-notice-holdout.json`. It is reported in ADR 0023 and to the
user for the merge decision; nothing is tuned on it.

### 5.3 Offline stack and CI

The offline development and holdout notice evaluations are re-measured with the new gate.

- Tests that pin offline measurements are re-pinned: the notice baseline totals and
  averages, the agreement sweep rows, the verifier-removal count and any label-rank verdict
  that flips. Each re-pin gets a ledger ruling naming its reason.
- The two offline CI notice gates are re-baselined at the new development measurement,
  with dated comments: `consumer_notice_known_bad_citations` max and
  `consumer_notice_labelled_grounds` min. `consumer_notice_abstention` stays at 1.0.
- The retrieval gates do not change.

### 5.4 Records

- **New ADR 0023**, "Agreement depth 10 and an alias support cap". Status Accepted; it
  amends ADR 0019 and ADR 0022. It records the problem, the decision, the spike tables
  (§3), the dropped sibling collapse, the acceptance numbers, the holdout and offline
  results, and the consequences: the 0.004 recall-interval miss and the depth sensitivity.
- **ADR 0019** status gains "Amended by ADR 0023"; ADR 0022's configured-stack section
  points to it. `docs/architecture.md` lists 0023.
- **Docs that state "within their top 13"** are updated: `README.md` (gate paragraph),
  `README-pt.md` (same paragraph) and `docs/architecture.md` (rule 9).

## 6. Tests

Each test is written first and seen to fail.

- A query with 5 agreeing alias chunks and 2 agreeing official chunks supports the first 3
  aliases in result order and both official chunks.
- The cap counts per query: two traces with 3 different agreeing aliases each support all 6.
- An alias whose channels do not agree takes no slot: a non-agreeing alias at rank 1 and 3
  agreeing aliases below it support those 3.
- `alias_cap` is a parameter: with `alias_cap=1` only the first agreeing alias is supported.
- The degraded corroboration path is unchanged by the cap.
- `is_alias_chunk_id` is true for `…:legal:<unit>:alias-01` and false for `…:part-01`
  chunks, article-level chunks and evidence chunks.
- A notice evaluation run records policy version v6 and `agreement_max_rank` 10.

Existing tests that hard-code depth 13 change, each with a ruling:

- the sweep's default-depth assertion and the corresponding run metadata;
- `tests/test_consumer_api.py`'s comment;
- `tests/test_evaluation_compare.py` keeps its 13 → 12 fixture: it is plain metadata for
  the comparison, not the production depth.

Tests that read `AGREEMENT_MAX_RANK` follow the constant. Coverage stays at 100%, and
ruff, mypy, import-linter and vulture stay clean. The full suite runs with the existing
deselection of `tests/test_demo_manifest.py`, which fails on main.

## 7. Non-goals

- Sibling collapse (measured: no effect, §3).
- Rewriting or reviewing hub aliases.
- Stemming (review step 2b).
- The recall/precision gate split (review step 3).
- The out-of-scope holdout cases that now get grounds: reported, not addressed here.
- Pushing, or merging without the user.
