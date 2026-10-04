# Agreement Depth 10 and Alias Support Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover notice precision on the JUÁ stack after the lay alias chunks. The
agreement gate's depth goes from 13 to 10, and each query may contribute at most 3 alias
chunks to the supported set. Then measure, re-pin and document.

**Architecture:** The change is confined to the retrieval-agreement gate,
`strongly_supported_chunk_ids` in `app/consumer/legal_policy.py`. It gets a new depth
constant, an alias cap applied per query on the two-channel path, and a v6 policy version.
`app/consumer/aliases.py` gains two small helpers that write and recognise alias chunk
ids. Nothing else in retrieval, ground selection or the index changes.

**Tech Stack:** Python 3.14, pydantic, pytest (+ coverage 100% gate), ruff, mypy, import-linter, vulture; evaluation CLIs `app.evaluation.consumer_runner` and `app.evaluation.compare`.

**Spec:** `docs/superpowers/specs/2026-10-03-agreement-depth-and-alias-cap-design.md`

## Global Constraints

- Branch `feat/lay-aliases`, in this checkout. Commit per task. Never push.
- Python is `.venv/Scripts/python.exe`.
- Every configured (JUÁ) command runs with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2` and `--require-cached-queries`. The 4B model must never load.
- Baselines dir: `B=data/evaluation/baselines/2026-10-03-lay-aliases` (git-ignored). It holds `v4-configured-notice-{development,holdout}.json` and `v5-configured-notice-{development,holdout}.json` (v5 at depth 13).
- `AGREEMENT_MAX_RANK = 10`, `ALIAS_SUPPORT_CAP = 3`, `LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v6"`.
- Unchanged: the index (no re-embedding), the aliases, citations, the eligibility policy, the CDC anchor, ground selection, the precedence window, retrieval.
- The configured development acceptance run must reproduce the spike exactly:
  - precision 0.491;
  - article recall 0.592;
  - exact recall 0.400;
  - grounds 40 (16 labelled, 24 unlabelled);
  - known-bad 0;
  - failed 0.

  A difference stops execution for investigation; never re-tune.
- The holdout is measured once, after the acceptance run matches; it is reported, never tuned on.
- Suite runs deselect `tests/test_demo_manifest.py` (fails on main). Coverage stays 100%; ruff, mypy, lint-imports, vulture clean.
- Bash heredocs on this machine mangle backslashes: write content containing backslashes with the Edit/Write tools.

## Review Focus

1. **Reordered results.** A reranker reorders a trace's results, so list position is not rank. The cap must follow each item's `rank`, keeping the best-ranked agreeing aliases. Pinned in Task 1 (`test_the_cap_follows_rank_not_list_position`).
2. **A mixed batch.** One healthy two-channel trace sits beside one degraded single-channel trace. The cap applies only to the healthy trace, and corroboration counting is untouched. Pinned in Task 1 (`test_a_degraded_trace_beside_a_healthy_one_keeps_corroboration`).
3. **Cap bounds.** `alias_cap=0` supports no alias but every official chunk; a negative cap is a programming error. Pinned in Task 1 (`test_alias_cap_bounds`).
4. **The same alias in two queries.** An alias capped out in one query but within the cap in another is supported, because support is the union across queries. Pinned in Task 1 (`test_an_alias_capped_in_one_query_but_kept_in_another_is_supported`).
5. **The depth boundary.** An alias that agrees exactly at depth 10 takes a cap slot; one at 11 in either channel takes none. Pinned in Task 1 (`test_only_agreeing_aliases_take_cap_slots_at_the_depth_boundary`).

---

### Task 1: The gate — alias helpers, the cap, depth 10, policy v6

**Files:**
- Modify: `app/consumer/aliases.py` (new helpers after `MAX_ALIAS_CHARS` and the regexes)
- Modify: `app/consumer/legal_corpus.py:795` (`_alias_chunks` builds its id with `alias_chunk_id`)
- Modify: `app/consumer/legal_policy.py:50` (policy version), `:99-113` (depth comment, new cap), `:236-286` (gate)
- Create: `tests/test_alias_support_cap.py`
- Modify: `tests/test_consumer_api.py:168` (policy version pin)

**Interfaces:**
- Produces:
  - `app.consumer.aliases.alias_chunk_id(document_id: str, unit_key: str) -> str`;
  - `app.consumer.aliases.is_alias_chunk_id(chunk_id: str) -> bool`;
  - `app.consumer.legal_policy.ALIAS_SUPPORT_CAP: int = 3`;
  - `AGREEMENT_MAX_RANK: int = 10`;
  - `strongly_supported_chunk_ids(traces, *, max_rank=AGREEMENT_MAX_RANK, alias_cap=ALIAS_SUPPORT_CAP) -> frozenset[str]`, which raises `ValueError` on `alias_cap < 0`;
  - `LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v6"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alias_support_cap.py`:

```python
"""The alias support cap in the retrieval-agreement gate (ADR 0023)."""

from __future__ import annotations

import hashlib

import pytest

from app.consumer.aliases import alias_chunk_id, is_alias_chunk_id
from app.consumer.legal_policy import (
    AGREEMENT_MAX_RANK,
    ALIAS_SUPPORT_CAP,
    LEGAL_GROUND_POLICY_VERSION,
    strongly_supported_chunk_ids,
)
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace

DOC = "10f4ba20de24dbca"


def _alias(unit: str) -> str:
    return alias_chunk_id(DOC, unit)


def _official(unit: str) -> str:
    return f"{DOC}:legal:{unit}:part-01"


def _trace(
    chunk_ids: list[str],
    *,
    query: str = "consulta",
    query_index: int = 0,
    ranks: list[tuple[int, int]] | None = None,
    positions: list[int] | None = None,
    degraded: bool = False,
) -> RetrievalTrace:
    """One query's trace. Every chunk agrees at (rank, rank) unless ``ranks`` says otherwise.

    ``positions`` overrides each item's fused ``rank`` while keeping list order,
    as a reranker's reordering would.
    """
    count = len(chunk_ids)
    pairs = ranks or [(position, position) for position in range(1, count + 1)]
    fused = positions or list(range(1, count + 1))
    return RetrievalTrace(
        batch_id="batch",
        agent="consumer_legal_authorities",
        doc_id=DOC,
        query_index=query_index,
        query=query,
        query_sha256=hashlib.sha256(query.encode()).hexdigest(),
        requested_k=8,
        candidate_k=32,
        returned_count=count,
        retrieval_mode="hybrid",
        degraded_mode="lexical_only" if degraded else None,
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        chunking_version="test",
        score_type="rrf_score",
        rrf_constant=60,
        dense_weight=1.0,
        lexical_weight=1.0,
        results=[
            RetrievedItemTrace(
                rank=rank,
                chunk_id=chunk_id,
                doc_id=DOC,
                page_start=1,
                page_end=1,
                score=1.0 / (60 + rank),
                channel_ranks=(
                    {"lexical": rank} if degraded else {"dense": dense, "lexical": lexical}
                ),
                content_sha256=hashlib.sha256(chunk_id.encode()).hexdigest(),
            )
            for chunk_id, (dense, lexical), rank in zip(chunk_ids, pairs, fused, strict=True)
        ],
    )


def test_alias_chunk_ids_are_recognised_and_nothing_else() -> None:
    assert alias_chunk_id(DOC, "br-cdc-art-39-inciso-i") == (
        f"{DOC}:legal:br-cdc-art-39-inciso-i:alias-01"
    )
    assert is_alias_chunk_id(_alias("br-cdc-art-39-inciso-i"))
    assert is_alias_chunk_id(_alias("br-cf-art-5-xxxii"))
    assert not is_alias_chunk_id(_official("br-cdc-art-39-inciso-i"))
    assert not is_alias_chunk_id(f"{DOC}:legal:br-cdc-art-39:article:part-01")
    assert not is_alias_chunk_id("evidence-doc:0003")


def test_the_gate_depth_cap_and_policy_version() -> None:
    assert AGREEMENT_MAX_RANK == 10
    assert ALIAS_SUPPORT_CAP == 3
    assert LEGAL_GROUND_POLICY_VERSION == "consumer-notice-scope-eligibility-v6"


def test_a_query_contributes_at_most_three_alias_chunks() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20, 35, 49)]
    officials = [_official("br-cdc-art-6-inciso-iii"), _official("br-cdc-art-42-caput")]
    trace = _trace(
        [aliases[0], officials[0], aliases[1], aliases[2], aliases[3], officials[1], aliases[4]]
    )

    assert strongly_supported_chunk_ids([trace]) == {*aliases[:3], *officials}


def test_the_cap_counts_per_query() -> None:
    first = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20)]
    second = [_alias(f"br-cdc-art-{number}-caput") for number in (35, 49, 51)]

    supported = strongly_supported_chunk_ids(
        [_trace(first, query="primeira"), _trace(second, query="segunda", query_index=1)]
    )

    assert supported == {*first, *second}


def test_alias_cap_is_a_parameter() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20)]

    assert strongly_supported_chunk_ids([_trace(aliases)], alias_cap=1) == {aliases[0]}


def test_alias_cap_bounds() -> None:
    alias, official = _alias("br-cdc-art-18-caput"), _official("br-cdc-art-42-caput")

    assert strongly_supported_chunk_ids([_trace([alias, official])], alias_cap=0) == {official}
    with pytest.raises(ValueError, match="alias_cap"):
        strongly_supported_chunk_ids([_trace([alias])], alias_cap=-1)


def test_the_cap_follows_rank_not_list_position() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20, 35)]
    # A reranker listed them in reverse fused order.
    trace = _trace(aliases, positions=[4, 3, 2, 1])

    assert strongly_supported_chunk_ids([trace]) == set(aliases[1:])


def test_only_agreeing_aliases_take_cap_slots_at_the_depth_boundary() -> None:
    aliases = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20, 35, 49)]
    depth = AGREEMENT_MAX_RANK
    trace = _trace(
        aliases,
        ranks=[(1, depth + 1), (depth, depth), (depth + 1, 1), (2, 2), (3, 3)],
    )

    # The first and third do not agree, so they take no slot; the depth-10 one does.
    assert strongly_supported_chunk_ids([trace]) == {aliases[1], aliases[3], aliases[4]}


def test_an_alias_capped_in_one_query_but_kept_in_another_is_supported() -> None:
    shared = _alias("br-cdc-art-49-caput")
    others = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19, 20)]

    supported = strongly_supported_chunk_ids(
        [
            _trace([*others, shared], query="primeira"),
            _trace([shared], query="segunda", query_index=1),
        ]
    )

    assert supported == {*others, shared}


def test_a_degraded_trace_beside_a_healthy_one_keeps_corroboration() -> None:
    capped = [_alias(f"br-cdc-art-{number}-caput") for number in (18, 19)]
    corroborated = [_alias(f"br-cdc-art-{number}-caput") for number in (35, 49, 51)]
    healthy = _trace(capped, query="relato e pedido")
    # The complaint and the remedy searched separately corroborate the top three.
    complaint = _trace(corroborated, query="o produto quebrou", query_index=1, degraded=True)
    remedy = _trace(corroborated, query="quero a troca", query_index=2, degraded=True)

    supported = strongly_supported_chunk_ids([healthy, complaint, remedy], alias_cap=1)

    assert supported == {capped[0], *corroborated}
```

In `tests/test_consumer_api.py:168` change the pinned version:

```python
    assert notice["legal_ground_policy_version"] == "consumer-notice-scope-eligibility-v6"
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_alias_support_cap.py -q -p no:cacheprovider`
Expected: collection error, `ImportError: cannot import name 'alias_chunk_id' from 'app.consumer.aliases'`.

- [ ] **Step 3: Add the alias id helpers**

In `app/consumer/aliases.py`, after the `_LEGAL_REFERENCE` regex, add:

```python
# The id of a unit's alias chunk. The retrieval gate sees only trace chunk ids,
# so it recognises alias chunks by this shape (ADR 0023).
_ALIAS_CHUNK_ID = re.compile(r":legal:[a-z0-9-]+:alias-\d{2}$")


def alias_chunk_id(document_id: str, unit_key: str) -> str:
    """The chunk id of a unit's alias chunk; it is never quoted (ADR 0022)."""
    return f"{document_id}:legal:{unit_key}:alias-01"


def is_alias_chunk_id(chunk_id: str) -> bool:
    """Whether a retrieved chunk id names an alias chunk rather than statute text."""
    return _ALIAS_CHUNK_ID.search(chunk_id) is not None
```

This snippet contains backslashes (`\d`): write it with the Edit tool, not a bash heredoc.

In `app/consumer/legal_corpus.py`, import `alias_chunk_id` from `app.consumer.aliases` (extend the existing `from app.consumer.aliases import (...)` block) and, in `_alias_chunks`, replace

```python
                    chunk_id=f"{document_id}:legal:{key}:alias-01",
```

with

```python
                    chunk_id=alias_chunk_id(document_id, key),
```

- [ ] **Step 4: Change the depth, add the cap and bump the policy version**

In `app/consumer/legal_policy.py`:

1. Line 50: `LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v6"`.
2. Add `from app.consumer.aliases import is_alias_chunk_id` to the imports. `aliases.py` imports neither `legal_policy` nor `legal_corpus`, so there is no cycle.
3. Replace the comment and constant block from `# Both channels must rank a chunk within this depth.` through `AGREEMENT_MAX_RANK = 13` with:

```python
# Both channels must rank a chunk within this depth. Appearing anywhere in the
# 32-deep candidate lists was near-vacuous (ADR 0019). Before the lay alias
# chunks, every exact hit on the configured stack ranked within 12 in both
# channels, so 13 kept one rank of margin. Alias chunks are short paraphrases
# that agree with themselves in both channels: at 13 the configured
# development split cited 67 grounds, 2 of them known-bad, and precision fell
# below v4. At 10 it keeps every exact hit (exact recall 0.400 at both depths)
# and drops 19 unlabelled grounds and one known-bad (ADR 0023). Precision moves
# sharply between 10 and 12 on 20 paired cases. Measure other depths with the
# notice evaluation's --agreement-max-rank sweep.
AGREEMENT_MAX_RANK = 10
# Each query may contribute at most this many alias chunks to the supported
# set, best rank first. An alias chunk's two channels score the same short
# paraphrase, so its agreement is weaker evidence than agreement on statute
# text. On the configured development split the cap kept every labelled
# ground, raised precision from 0.457 to 0.491 and removed the last known-bad
# citation (ADR 0023). Official chunks are never capped.
ALIAS_SUPPORT_CAP = 3
```

4. Replace `strongly_supported_chunk_ids` with the version below; its docstring gains one paragraph. Add the helper `_agreeing_chunk_ids` right after it:

```python
def strongly_supported_chunk_ids(
    traces: list[RetrievalTrace],
    *,
    max_rank: int = AGREEMENT_MAX_RANK,
    alias_cap: int = ALIAS_SUPPORT_CAP,
) -> frozenset[str]:
    """Return chunks that clear the retrieval-agreement safety gate.

    With the category allowlist gone this is the load-bearing precision
    control: an article reaches a notice because two independent signals
    agreed on it, not because one channel matched a shared word.

    Hybrid retrieval records the rank each chunk earned in the dense and the
    lexical channel. A chunk is supported when both channels ranked it within
    ``AGREEMENT_MAX_RANK``; the gate reads that from the trace rather than
    inferring it from a fused score, so it holds for any fusion weights and
    survives a reranker, which only reorders candidates. ``max_rank`` exists
    so the notice evaluation can measure other depths; production uses the
    default.

    An alias chunk's two channels score the same lay paraphrase, so each
    query contributes at most ``alias_cap`` of them, best rank first; an alias
    whose channels do not agree takes no slot, and official chunks are never
    capped (ADR 0023).

    A trace without both channels (lexical-only degraded mode, dense-only
    configuration) cannot show that agreement. There a chunk must rank in the
    top three for two independent queries, where independent means neither
    query's text contains the other's. The narrative ranking queries contain
    the complaint and the remedy, so they never corroborate each other or
    those two; the complaint and the remedy searched separately can.
    """

    if alias_cap < 0:
        raise ValueError("alias_cap must not be negative")
    supported: set[str] = set()
    corroborating_queries: dict[str, set[str]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        if trace.retrieval_mode == "hybrid" and trace.degraded_mode is None:
            supported.update(_agreeing_chunk_ids(trace, max_rank, alias_cap))
            continue
        for item in trace.results:
            if item.rank <= CORROBORATION_RANK:
                corroborating_queries.setdefault(item.chunk_id, set()).add(
                    _query_key(trace.query)
                )

    supported.update(
        chunk_id
        for chunk_id, queries in corroborating_queries.items()
        if _has_independent_pair(queries)
    )
    return frozenset(supported)


def _agreeing_chunk_ids(trace: RetrievalTrace, max_rank: int, alias_cap: int) -> list[str]:
    """Chunks both channels ranked within ``max_rank``, at most ``alias_cap`` of them aliases."""

    agreeing: list[str] = []
    aliases = 0
    for item in sorted(trace.results, key=lambda result: result.rank):
        if not _channels_agree(item.channel_ranks, max_rank):
            continue
        if is_alias_chunk_id(item.chunk_id):
            if aliases == alias_cap:
                continue
            aliases += 1
        agreeing.append(item.chunk_id)
    return agreeing
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_alias_support_cap.py tests/test_ground_selection.py tests/test_consumer_aliases.py tests/test_consumer_alias_chunks.py tests/test_consumer_citation_integrity.py -q -p no:cacheprovider`
Expected: all pass. `test_ground_selection.py` reads `AGREEMENT_MAX_RANK`, so it follows the constant. If a test there hard-codes 13 and fails, fix it to read the constant and ledger a ruling.

Then run `.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app`. Expected: clean. If ruff flags long lines in the new test file (E501), wrap them; that is formatting only, so ledger a ruling.

- [ ] **Step 6: Commit**

The offline measurement pins in `tests/test_consumer_notice_evaluation.py`, and any other test that pins offline totals, are expected to fail until Task 2 re-measures them. Do not touch them here.

```bash
git add app/consumer/aliases.py app/consumer/legal_corpus.py app/consumer/legal_policy.py tests/test_alias_support_cap.py tests/test_consumer_api.py
git commit -m "feat(consumer): agreement depth 10 and at most three alias chunks per query

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Acceptance on JUÁ, holdout, offline re-measurement and pins

**Files:**
- Modify: `tests/test_consumer_notice_evaluation.py` (baseline totals and averages, sweep default depth and totals, verifier removals, CLI sweep rows)
- Modify: any other test whose failure in Step 6 is an offline measurement pin
- Modify: `.github/workflows/ci.yml` (the two offline notice gates and their comment)

**Interfaces:**
- Consumes: Task 1's gate (`AGREEMENT_MAX_RANK = 10`, `ALIAS_SUPPORT_CAP = 3`, policy v6).
- Produces: in `$B`, the files `step4-configured-notice-{development,holdout}.json`, `compare-step4-v4-{development,holdout}.txt`, `compare-step4-v5-development.txt` and `step4-offline-notice-{development,holdout}.json`. Task 3's ADR reads them.

- [ ] **Step 1: Run the configured development acceptance run**

```bash
B=data/evaluation/baselines/2026-10-03-lay-aliases
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2
.venv/Scripts/python.exe -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured --split development --require-cached-queries --output "$B/step4-configured-notice-development.json" > "$B/step4-configured-notice-development.log" 2>&1; echo "exit $?"
.venv/Scripts/python.exe -c "
import json
d = json.load(open('$B/step4-configured-notice-development.json', encoding='utf-8'))
a, t = d['averages'], d['totals']
print(round(a['consumer_notice_precision'], 3), round(a['consumer_notice_article_recall'], 3), round(a['consumer_notice_exact_recall'], 3), t['consumer_notice_grounds'], t['consumer_notice_labelled_grounds'], t['consumer_notice_unlabelled_grounds'], t['consumer_notice_known_bad_citations'], d['failed_case_count'], d['run']['agreement_max_rank'], d['run']['ground_policy_version'])
"
```

Expected: `exit 0` and `0.491 0.592 0.4 40 16 24 0 0 10 consumer-notice-scope-eligibility-v6`.

**STOP if any number differs.** That means the production gate does not do what the spike did. Compare per-case grounds with `$B/spike-alias-cap-3-depth10-development.json`, find the cause with superpowers:systematic-debugging, and report to the user. Do not re-tune.

- [ ] **Step 2: Paired comparisons on development**

```bash
.venv/Scripts/python.exe -m app.evaluation.compare "$B/v4-configured-notice-development.json" "$B/step4-configured-notice-development.json" > "$B/compare-step4-v4-development.txt"
.venv/Scripts/python.exe -m app.evaluation.compare "$B/v5-configured-notice-development.json" "$B/step4-configured-notice-development.json" > "$B/compare-step4-v5-development.txt"
grep -E "article_recall|exact_recall|precision|known_bad|labelled|_grounds  " "$B/compare-step4-v4-development.txt"
```

Expected against v4: article recall `0.421 → 0.592, +0.171 [-0.004, +0.371]`, known-bad `1 → 0`.

- [ ] **Step 3: Measure the holdout once**

```bash
.venv/Scripts/python.exe -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured --split holdout --require-cached-queries --output "$B/step4-configured-notice-holdout.json" > "$B/step4-configured-notice-holdout.log" 2>&1; echo "exit $?"
.venv/Scripts/python.exe -m app.evaluation.compare "$B/v4-configured-notice-holdout.json" "$B/step4-configured-notice-holdout.json" > "$B/compare-step4-v4-holdout.txt"
cat "$B/compare-step4-v4-holdout.txt"
```

Expected: `exit 0` and no failed case. Record the numbers in the ledger; they are reported, not gated.

- [ ] **Step 4: Re-measure the offline stack**

```bash
for split in development holdout; do
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --split $split --output "$B/step4-offline-notice-$split.json" > "$B/step4-offline-notice-$split.log" 2>&1; echo "offline $split exit $?"
done
.venv/Scripts/python.exe -c "
import json
for s in ('development', 'holdout'):
    d = json.load(open(f'$B/step4-offline-notice-{s}.json', encoding='utf-8'))
    a, t = d['averages'], d['totals']
    print(s, {k.replace('consumer_notice_', ''): round(v, 3) for k, v in a.items() if k.startswith('consumer_notice')}, {k.replace('consumer_notice_', ''): v for k, v in t.items()})
"
```

Expected: both exit 0. Record the totals in the ledger.

- [ ] **Step 5: Re-pin the offline measurement tests**

In `tests/test_consumer_notice_evaluation.py`:
- **`test_offline_notice_baseline_on_the_seed_dataset`:**
  - set `summary.totals`, `consumer_notice_exact_recall`, `consumer_notice_abstention` and the per-split precision, article recall and abstention to the values the test now produces (read them from the failure diff, or from a full-dataset run of the offline notice evaluation);
  - add `assert summary.run.agreement_max_rank == 10`;
  - append a docstring paragraph: "Re-measured on 2026-10-03 for agreement depth 10 and the alias support cap (ADR 0023): …" with the new totals.
- **`test_an_agreement_sweep_retrieves_each_case_once`:** `default.run.agreement_max_rank == 10`. Re-pin the shallow (8) and default totals, and keep the comment that a shallower gate can only drop grounds. That holds only if shallow ≤ default; if it doesn't, stop and investigate.
- **`test_verifier_removals_are_counted_per_case`:** removed count = the new default grounds total.
- **`test_cli_prints_an_agreement_sweep`:** the rows for depths 16 and 20 at their new grounds counts.

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_notice_evaluation.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Full suite, then classify every failure**

Run: `.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q -p no:cacheprovider --deselect tests/test_demo_manifest.py > "$B/step4-suite.log" 2>&1; grep -aE "^(FAILED|ERROR) |[0-9]+ (passed|failed)|Required test coverage" "$B/step4-suite.log"`

For each failure, decide which kind it is:
- **An offline measurement pin** (a total, rank or fixture tied to the old gate): re-pin it with the measured value and ledger `Task 2: Ruling: re-pinned <test> — <old> -> <new>, gate change (ADR 0023) — cost if wrong: the pin follows a behaviour change it should have caught`.
- **A behaviour regression:** stop and debug.

Repeat until the suite is green with coverage 100%.

- [ ] **Step 7: Re-baseline the offline CI notice gates**

In `.github/workflows/ci.yml`, step "Consumer notice final-ground gates":
- set `--max consumer_notice_known_bad_citations=` to the offline development known-bad total from Step 4;
- set `--min consumer_notice_labelled_grounds=` to its labelled total;
- keep `--min consumer_notice_abstention=1.0`;
- replace the "Re-measured on 2026-10-03 for corpus v5" comment block with one that gives the new development totals and cites ADR 0023, keeping the reason for the known-bad ceiling if it is still 1.

The retrieval gate step does not change. Then run both CI commands exactly as `ci.yml` writes them (with `.venv/Scripts/python.exe -m`), outputs into `$B`:

```bash
.venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --split development --output "$B/step4-ci-r.json" --min consumer_retrieval_success@5=1.0 --min consumer_recall@5=0.24 --min consumer_article_recall@5=0.54 --min consumer_ndcg@5=0.18 --min consumer_abstention@5=1.0 --max consumer_hard_negative_rate@5=0.03 --max consumer_inactive_provision_rate@5=0.0 --max consumer_unknown_status_rate@5=0.0 > "$B/step4-ci-r.log" 2>&1; echo "retrieval gate exit $?"
```

For the notice gate, use the command with the new values from `ci.yml`, writing `--output "$B/step4-ci-n.json"`, then `echo "notice gate exit $?"`.
Expected: both exit 0.

- [ ] **Step 8: Static checks and commit**

Run: `.venv/Scripts/python.exe -m ruff check app tests frontend && .venv/Scripts/python.exe -m mypy app && .venv/Scripts/lint-imports.exe && .venv/Scripts/vulture.exe app --min-confidence 90`
Expected: all clean (lint-imports: `Contracts: 1 kept, 0 broken`).

```bash
git add tests .github/workflows/ci.yml
git commit -m "test(consumer): re-measure the notice gates for depth 10 and the alias cap

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: ADR 0023 and the docs

**Files:**
- Create: `docs/adr/0023-agreement-depth-and-alias-cap.md`
- Modify: `docs/adr/0019-explicit-retrieval-agreement.md` (status line)
- Modify: `docs/adr/0022-lay-language-alias-chunks.md` (end of the "Configured stack" section)
- Modify: `docs/architecture.md` (rule 9 at lines 68-71; ADR index after the 0022 line)
- Modify: `README.md:111-113`, `README-pt.md:87-88` (the agreement paragraph)

**Interfaces:**
- Consumes: Task 2's `$B` files and ledger numbers.

- [ ] **Step 1: Write ADR 0023**

Create `docs/adr/0023-agreement-depth-and-alias-cap.md` with these sections. Fill the holdout and offline rows from Task 2's `$B` files and ledger lines; development rows below are the acceptance values.

```markdown
# ADR 0023: Agreement depth 10 and an alias support cap

## Status

Accepted · Date: 2026-10-03 · Amends: [ADR 0019](0019-explicit-retrieval-agreement.md),
[ADR 0022](0022-lay-language-alias-chunks.md)

## Context

<Two short paragraphs: on JUÁ the lay alias chunks lifted retrieval (development article
recall@5 0.438 → 0.733) but the notice cited 67 grounds against 34, precision fell 0.390 →
0.312 and known-bad rose 1 → 2 (ADR 0022). 66 of the 67 grounds passed the gate through an
alias, whose two channels score the same paraphrase.>

## Decision

1. `AGREEMENT_MAX_RANK` goes from 13 to 10.
2. On the two-channel path each query contributes at most `ALIAS_SUPPORT_CAP` = 3 alias
   chunks to the supported set, best rank first; an alias that does not agree takes no slot
   and official chunks are never capped. The degraded single-channel path is unchanged.
3. The ground policy version becomes `consumer-notice-scope-eligibility-v6`.

## Evidence (spikes, JUÁ, development, 2026-10-03)

<The table of spec §3, verbatim, and the three "ruled out" bullets: sibling collapse
changed no ground in 20 in-scope cases, an alias-specific depth equals the global depth,
requiring official corroboration loses the recall the aliases exist for.>

## Measurements (configured stack, against the v4 baseline)

| Development (24 cases) | v4 | v5, depth 13 | **v5, depth 10 + cap 3** | release − v4, 95% interval |
|---|---|---|---|---|
| notice article recall | 0.421 | 0.608 | **0.592** | +0.171 [−0.004, +0.371] |
| notice exact recall | 0.225 | 0.400 | **0.400** | <from compare-step4-v4-development.txt> |
| notice precision | 0.390 | 0.312 | **0.491** | <from compare-step4-v4-development.txt> |
| grounds / labelled / unlabelled | 34 / 13 / 20 | 67 / 17 / 48 | **40 / 16 / 24** | <labelled and unlabelled rows> |
| known-bad citations | 1 | 2 | **0** | −1 <interval> |

| Holdout (19 cases, reported only) | v4 | v5, depth 13 | **v5, depth 10 + cap 3** |
|---|---|---|---|
| notice article recall / exact / precision / abstention / known-bad | <v4-configured-notice-holdout.json> | 0.590 / 0.346 / 0.229 / 0.167 / 2 | <step4-configured-notice-holdout.json> |

Offline (regression tripwire): <development and holdout grounds / labelled / unlabelled /
known-bad from step4-offline-notice-*.json, and the CI gate values set in Task 2>.

## Consequences

- (+) Development precision 0.491 against v4's 0.390, no known-bad citation, and every
  exact hit kept.
- (+) Article recall +0.171 over v4, of the +0.187 the aliases brought at depth 13.
- (−) The recall gain's 95% interval touches zero (lower bound −0.004); the user accepted
  it as meeting the bar.
- (−) Precision is sensitive to the depth on 20 paired cases (0.457 at 10, 0.308 at 12);
  re-measure the depth whenever the aliases or the corpus change.
- (−) The alias crowding of ADR 0022 is reduced in the notice, not in retrieval; hub
  aliases still fill retrieval slots.
- <(−/+) the holdout outcome, in one line.>
```

Replace every `<…>` instruction with the measured numbers and prose before committing. The `<…>` lines are fill-in instructions for this step, not text to keep. Write the file with the Write tool.

- [ ] **Step 2: Amend ADR 0019 and ADR 0022, update architecture and READMEs**

1. `docs/adr/0019-explicit-retrieval-agreement.md`: append ` · Amended by: [ADR 0023](0023-agreement-depth-and-alias-cap.md)` to the status line.
2. `docs/adr/0022-lay-language-alias-chunks.md`: at the end of the "Configured stack" section add the paragraph: "Step 4 of the review answers these numbers: agreement depth 10 and at most three alias chunks per query restore development precision to 0.491 with no known-bad citation ([ADR 0023](0023-agreement-depth-and-alias-cap.md))."
3. `docs/architecture.md` rule 9: change "both channels must rank a chunk within their top 13." to "both channels must rank a chunk within their top 10, and each query counts at most three alias chunks (ADR 0023)." After the 0022 index line add `- [0023](adr/0023-agreement-depth-and-alias-cap.md) — agreement depth 10 and an alias support cap`.
4. `README.md`: in the agreement bullet change "both ranked it within their top 13." to "both ranked it within their top 10; each query counts at most three alias chunks (ADR 0023)."
5. `README-pt.md`: change "o colocam entre os 13 primeiros." to "o colocam entre os 10 primeiros, e cada consulta conta no máximo três chunks de aliases (ADR 0023)."

- [ ] **Step 3: Check and commit**

Run: `grep -rn "top 13\|13 primeiros\|within their top 13" README.md README-pt.md docs/architecture.md` → expected: no output.
Run: `.venv/Scripts/python.exe -m pytest tests/test_alias_support_cap.py -q -p no:cacheprovider` → expected: pass (docs-only task; this confirms the tree is intact).

```bash
git add docs/adr/0023-agreement-depth-and-alias-cap.md docs/adr/0019-explicit-retrieval-agreement.md docs/adr/0022-lay-language-alias-chunks.md docs/architecture.md README.md README-pt.md
git commit -m "docs(adr-0023): agreement depth 10 and an alias support cap

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
