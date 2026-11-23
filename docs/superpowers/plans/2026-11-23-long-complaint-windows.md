# Long-Complaint Sentence Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A complaint longer than the narrative's 1,050 characters is searched
in sentence windows plus the remedy. A provision found only by windows needs
corroboration. Short complaints, and CI's numbers, stay unchanged.

**Architecture:**
- The support gate learns a window rule keyed on a new trace agent.
- The query builder returns a plan: today's two narrative queries for a short
  complaint, or the remedy plus budgeted windows for a long one.
- `retrieve_legal_candidates` runs the windows as their own batch.
- Everything downstream (`select_legal_grounds`, the service, the evaluators,
  the cache filler) keeps reading queries and traces as before.

**Tech Stack:** Python 3.10+, pytest, ruff (C90 ≤ 10), mypy strict.

**Spec:** `docs/superpowers/specs/2026-11-23-long-complaint-windows-design.md`

## Global Constraints

- Windows apply only when the normalized complaint exceeds `_NARRATIVE_COMPLAINT_CHARS` (1,050).
- `_WINDOW_CHARS = 600`, `_WINDOW_BUDGET_CHARS = 2_400`; sentences split on `(?<=[.!?])\s+`.
- Each window is bounded by `MAX_QUERY_CHARS`.
- `LEGAL_WINDOW_AGENT = "consumer_legal_window"`; policy `consumer-notice-scope-eligibility-v8`; query builder `consumer-legal-narrative-v8`.
- The four offline evaluations equal `main` except those two version strings.
- Never commit the real complaint; the long dataset holds only invented text.

## Review Focus

1. **A complaint of exactly 1,050 characters** keeps today's two queries; at 1,051 it gets windows. Test it in Task 2.
2. **A long complaint with no remedy** gets windows only, and in degraded mode no corroboration queries. Test it in Task 2.
3. **One sentence longer than 2,000 characters** becomes a single bounded window, and the refusal hint reports the cut. Test it in Tasks 2 and 3.
4. **A window trace whose items lack `source_provision_id`** (legacy store) falls back to the chunk id as the provision key, so it is never merged by accident. Test it in Task 1.
5. **Mixed health**, with the remedy batch healthy and the window batch degraded, follows each trace's own rule. Test it in Task 1.

---

### Task 1: The window rule in the support gate (policy v8)

**Files:** Modify `app/consumer/legal_policy.py`. Test in `tests/test_window_support.py` (new) and `tests/test_alias_support_cap.py:98`, `tests/test_consumer_api.py:168` (v8 pins).

**Interfaces:** Produces `LEGAL_WINDOW_AGENT: str` in `legal_policy`. `strongly_supported_chunk_ids` keeps its signature. Among healthy hybrid traces, a window trace's agreeing chunks count only for a provision that meets one of these:
- a non-window healthy trace agrees on it;
- two traces agree on it;
- one of its agreeing chunks is official, meaning `not is_alias_chunk_id`.

The provision key is `item.source_provision_id or item.chunk_id`.

- [ ] Step 1: failing tests:
  - `test_a_window_alone_does_not_support_an_alias_hit`;
  - `test_two_windows_support_the_same_provision` (the two can be different units of one article);
  - `test_the_remedy_search_vouches_for_a_window_hit`;
  - `test_one_window_supports_official_text`;
  - `test_items_without_a_provision_are_keyed_by_chunk`;
  - `test_degraded_windows_corroborate_like_any_independent_query`;
  - `test_mixed_health_follows_each_traces_rule`;
  - `test_without_windows_the_gate_is_unchanged` (the existing gate tests stay green);
  - the version pins become v8.
- [ ] Step 2: run them and see them fail.
- [ ] Step 3: implement with small helpers so C90 ≤ 10. Window traces are collected while the other traces are read, and decided after.
- [ ] Step 4: the tests pass. Commit `feat(consumer): window hits need corroboration (policy v8)`.

### Task 2: The query plan and window retrieval

**Files:** Modify `app/consumer/retrieval.py`. Test in `tests/test_consumer_retrieval.py`.

**Interfaces:**
- `LegalQueryPlan` (frozen dataclass): `ranking: tuple[str, ...]`, `windows: tuple[str, ...]`, and the property `queries -> list[str]`.
- `build_legal_query_plan(facts)`;
- `build_legal_query_plan_for_case(*, complaint, desired_resolution)`;
- `build_legal_queries` and `build_legal_queries_for_case` return `plan.queries`;
- `legal_query_windows(complaint: str) -> list[str]`;
- `truncated_narrative_fields(facts)` returns `(length, searched_chars)` for the complaint and `(length, 500)` for the remedy.

For a long complaint, `ranking` is `(remedy,)` when there is a remedy, cut at 500 characters, and `()` otherwise.

- [ ] Step 1: failing tests:
  - `test_a_complaint_that_fits_keeps_the_two_narrative_queries` (lengths 1,050 and 1,051);
  - `test_a_long_complaint_is_read_in_sentence_windows` (the late sentence lands in a window; each window is 600 characters or less unless one sentence is longer);
  - `test_windows_keep_the_start_and_the_end_within_the_budget`;
  - `test_one_overlong_sentence_is_one_bounded_window`;
  - `test_a_long_complaint_keeps_the_remedy_as_its_own_query` (replaces `test_long_complaint_does_not_remove_resolution`);
  - `test_long_complaint_retrieval_runs_windows_as_their_own_batch`;
  - `test_degraded_long_complaint_adds_no_corroboration_queries`;
  - `test_long_complaint_without_remedy_searches_windows_only`;
  - the rewritten degraded-independence test: windows corroborate each other, and one window alone does not;
  - `test_truncated_fields_report_what_is_searched`.
- [ ] Step 2: run them and see them fail.
- [ ] Step 3: implement. `retrieve_legal_candidates` runs the ranking batch, then the windows batch under `LEGAL_WINDOW_AGENT`. The narrative corroboration fallback stays for short complaints only. Update the module docstring's sentence-splitting paragraph.
- [ ] Step 4: the retrieval, ground-selection, notice-evaluation and prompt-notice tests pass. Commit `feat(consumer): read long complaints in sentence windows`.

### Task 3: The refusal hint reports what was searched

**Files:** Modify `app/consumer/refusals.py`. Test in `tests/test_consumer_refusals.py`.

- [ ] Step 1: update the tests:
  - the truncation cases use an account beyond the window budget;
  - the hint says "lê só parte de cada texto (do relato, {searched} dos {length} caracteres…)";
  - a 1,500-character account no longer yields `narrative_truncated`.
- [ ] Step 2: run them and see them fail. Step 3: implement. Step 4: they pass, with 100% coverage. Commit `fix(consumer): the truncation hint names what was searched`.

### Task 4: Long-complaint dataset, CI report, query-builder v8

**Files:**
- Create: `eval_data/consumer_long_complaints/dataset.json` (the 10 probe cases), `tests/test_long_complaint_dataset.py`.
- Modify: `app/evaluation/consumer_runner.py` (`QUERY_BUILDER_VERSION`), `.github/workflows/ci.yml` (ungated report and artifact).

- [ ] Step 1: failing tests:
  - `test_long_cases_derive_from_development_cases` (identical `relevant`, `hard_negatives`, `desired_resolution` and split; none from the holdout);
  - `test_long_cases_are_long_and_in_scope`;
  - `test_long_cases_cite_no_article` (no "art." anywhere);
  - the query-builder version pin.
- [ ] Step 2: run them and see them fail. Step 3: add the dataset, the version and the CI step. Step 4: the tests pass. Commit `test(evaluation): long-complaint probe dataset and report`.

### Task 5: ADR 0026 and docs

- [ ] ADR 0026: context, the measured table, decisions, consequences and follow-ups.
- [ ] Update the architecture index and the API guide (long complaints read in windows).
- [ ] Commit `docs: ADR 0026 — long complaints in sentence windows`.

### Task 6: Verification

- [ ] Run the full suite with coverage, plus ruff, mypy, lint-imports and vulture; then the Python 3.10 suite.
- [ ] Run the four offline evaluations from a clean export and diff them against `main`: only the two version strings may differ.
- [ ] Run the probe through the production code on the configured stack (cached JUÁ vectors) and compare with the spec's chosen row. Check the window queries hash-match the probe's.
- [ ] Run the real complaint, citation removed, through the service on JUÁ: it must get 35 I under the budget.
- [ ] Import the probe vectors into the local query-vector cache (git-ignored).
