# Long-complaint sentence windows — design

Date: 2026-11-23 · Status: approach approved in chat ("Yes, build it this way"); design awaiting approval

## Intent

Legal retrieval reads the first 1,050 characters of a complaint, so a long
complaint loses everything after that point. The first real long prompt
complaint (2,662 characters once its remedy is extracted) was affected this way:
- its forced-fulfilment sentence starts at character 1,109;
- without the consumer's own citation, the notice cited CDC art. 35, III (the
  refund the consumer refused) instead of art. 35, I.

The goal is to read the whole complaint, within today's embedding cost, without
letting one stray part of it add citations.

## Goals, non-goals, anti-goals

- **Goal.** A complaint longer than the narrative's 1,050 characters is
  searched in sentence windows, with the remedy as its own search.
- **Goal.** Results are combined so a citation from a single window needs
  corroboration.
- **Goal.** Keyword-only fallback works on long complaints, because the windows
  are independent searches.
- **Non-goal.** Short complaints, and every case in today's golden set (at most
  252 characters), keep exactly today's two queries. CI's gated numbers must not
  move.
- **Non-goal.** No LLM in retrieval, and no change to the agreement depth, the
  alias cap or eligibility.
- **Anti-goal.** Noise rising faster than recall. On the probe, keeping every
  window hit tripled the unlabelled citations and doubled the known-wrong ones.
- **Anti-goal.** A long complaint costing more JUÁ time than today. JUÁ runs at
  about 12 characters per second on CPU, inside a 300 s guard.

## Approaches (measured on JUÁ + PostgreSQL, 10 long versions of labelled development cases)

| Variant | Right | Known-wrong | Unlabelled | Refused |
|---|---|---|---|---|
| Today (first 1,050 characters + remedy, two queries) | 5 | 3 | 6 | 2 |
| Windows + remedy, every hit kept | 10 | 6 | 36 | 0 |
| Windows + remedy, a hit needs the remedy search or two searches | 9 | 3 | 13 | 0 |
| **Windows + remedy, as above, or one window matching official text (chosen)** | **10** | **3** | **13** | **0** |
| Today's two queries + windows + remedy, same rule | 10 | 3 | 17 | 0 |
| Windows + remedy, one provision per window | 9 | 5 | 24 | 0 |

- **Keyword-only fallback.** Today gives 1 right citation and refuses 9 cases.
  Windows + remedy give 4 right, 1 known-wrong, and refuse 4.
- **The real complaint, citation removed.** Today cites 35 III. The chosen
  variant cites 35 I, 18 § 4 and 20 II. In fallback mode, today refuses and the
  chosen variant cites 18 § 4 and 7 sole paragraph.

The chosen variant dominates the others on this sample:
- requiring two searches with no official-text exception loses art. 43 § 2;
- adding today's queries only adds noise and doubles the cost.

The probe is the cases in `scratchpad/fix3`, my writing, with labels inherited
from the development cases. It is a strong signal on a small sample.

## Decisions

1. **Query plan** (`app/consumer/retrieval.py`).
   - When the normalized complaint fits the narrative (1,050 characters or
     less), the ranking queries are today's two.
   - Otherwise the ranking queries are:
     - the remedy alone, cut at 500 characters as today, when there is one;
     - the complaint's sentence windows, which are whole sentences packed
       greedily into at most 600 characters, each bounded like any query.
   - `build_legal_queries` returns all of them, so the golden query-vector
     cache, the label-rank tool and the evaluation keep reading one list.
2. **Cost budget.** Windows total at most 2,400 characters, which with the
   remedy is about today's two-query cost.
   - Past the budget, the last window is kept, and windows are added from the
     start while they fit.
   - The start of a complaint holds what was bought and what went wrong; the
     end holds the latest state.
3. **Retrieval** (`retrieve_legal_candidates`).
   - The remedy runs under `consumer_legal_authorities`, as now.
   - Windows run as a separate batch under a new agent,
     `consumer_legal_window`, so they get their own embedding call and guard
     timeout.
   - Degraded mode adds no extra corroboration queries for a long complaint:
     windows and remedy are already independent formulations, and the existing
     top-3 corroboration rule applies to them unchanged.
4. **Support gate** (`legal_policy.strongly_supported_chunk_ids`, policy v8).
   - Healthy hybrid traces from the window agent no longer support a chunk on
     agreement alone.
   - A provision found by windows is supported when any of these holds:
     - a non-window trace also supports it;
     - two traces support it;
     - one of its agreeing chunks is official text, not an alias.
   - Provisions are read from the trace items' `source_provision_id`, so the
     gate still needs no corpus.
   - Non-window traces, the alias cap and degraded corroboration are unchanged.
5. **Refusal hint** (`truncated_narrative_fields`). The narrative cut is
   reported for what is actually searched: 1,050 characters for a short
   complaint, and the windows kept within the budget for a long one. The hint
   no longer says "only the start".
6. **Versions.** The query builder becomes `consumer-legal-narrative-v8`,
   ground policy v8, ADR 0026.
7. **Evaluation data.**
   - The 10 long cases join the repo as `eval_data/consumer_long_complaints/`,
     a development-only dataset separate from the golden set.
   - CI adds an ungated offline report of them.
   - Their JUÁ vectors go into the local query-vector cache (git-ignored), so
     configured runs use `--require-cached-queries`.
   - Promoting them into the gated golden set waits for a review (fix 6).

## Left to the builder

- Exact type of the query plan (a small frozen dataclass is enough).
- Test fixtures and names.
- How the long-complaint prompt test proves the late sentence is searched.
  Asserting that a window trace's query contains it is enough; the offline
  stack is not a quality signal.

## Evidence required before merge

- **CI gates.** The four offline evaluations are identical to `main` except
  the policy and query-builder versions.
- **The probe.** Rerun through the production code on JUÁ (cached vectors), it
  reproduces the chosen variant's numbers.
- **The real complaint.** It still gets 35 I without its citation, under the
  budget (one middle window dropped).
- **Tests.** Full suite, 100% coverage on the listed modules, ruff, mypy,
  lint-imports and vulture all pass; Python 3.10 too.
