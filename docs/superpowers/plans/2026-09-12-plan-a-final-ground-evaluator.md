# Final-Ground Evaluator (Phase 0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the legal grounds a notice would actually cite, so that later corpus changes are judged on final citations and not only on ranked candidates.

**Architecture:** The selection logic of `ConsumerCaseService._legal_grounds` moves, unchanged, into `app/consumer/ground_selection.py` as the pure function `select_legal_grounds`, with the retrieval-agreement gate injectable; the service keeps a thin wrapper. A new `app/evaluation/consumer_notice.py` runs the production-shaped batch (the three production queries, k=8, hybrid) through that function and scores the final grounds. Evaluation summaries gain integer `totals` so CI can gate on counts such as known-bad citations.

**Tech Stack:** Python 3.10+ syntax, Pydantic v2, pytest with `asyncio_mode = "auto"`, ruff, strict mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-12-lgpd-codigo-civil-corpus-design.md`, section 6.0 (Phase 0). Evidence for the baseline: section 4.5.

**Delivery order:** this is the first of three plans. Plan A (this one), then Plan B (`2026-09-12-plan-b-vector-reuse.md`), then Plan C (`2026-09-12-plan-c-lgpd-cc-corpus.md`). Plan A changes neither the corpus nor the vector index.

## Global Constraints

- Notice behaviour must not change: `select_legal_grounds` returns exactly what `ConsumerCaseService._legal_grounds` returns today.
- Baseline to reproduce on the unchanged corpus (offline mock path, k = 8, 15 golden cases): **79** final grounds, **4** known-bad citations, macro exact recall **0.333**, 2 scope-gate abstentions.
- A known-bad citation is a cited ground whose `provision_id` equals a hard-negative article id, or whose `unit_id` equals a hard-negative unit id or descends from one. A prefix test on article ids is wrong: it treats art. 42-A as a subdivision of art. 42.
- `docs/rag-review/probes.py` must keep importing `ConsumerCaseService` and `_merge_results` from `app.consumer.service`, and its monkeypatch of `app.consumer.service.strongly_supported_chunk_ids` must keep affecting `_legal_grounds`. Do not run that script: it rewrites tracked evidence files.
- Every module listed in `[tool.coverage.run] source` must keep 100% branch coverage (`fail_under = 100`).
- Lint exactly as CI: `python -m ruff check app tests frontend`. Types: `python -m mypy app`.
- `python` below means the project virtualenv interpreter (`.venv\Scripts\python.exe` on Windows). Run every command from the repository root.
- Commit messages follow Conventional Commits and end with these two lines:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA`

## File Structure

| File | Responsibility |
|---|---|
| `app/consumer/ground_selection.py` (new) | Pure selection of the grounds a notice may cite, plus the merge and query-occurrence helpers it needs |
| `app/consumer/service.py` (modify) | Delegates `_legal_grounds` to the pure function; keeps the `_merge_results` name for callers |
| `app/schemas/evaluation.py` (modify) | `CaseResult.counts`, `EvaluationSummary.totals`, `EvaluationRunMetadata.ground_policy_version` |
| `app/evaluation/consumer_runner.py` (modify) | Gates read totals too; `query_hashes` becomes public; CLI gains the notice mode |
| `app/evaluation/consumer_retrievers.py` (modify) | Public pipeline factories and `prepare_evaluation_pipeline`, shared by both evaluators |
| `app/evaluation/consumer_notice.py` (new) | Notice-path evaluator and its metrics |
| `tests/test_ground_selection.py` (new) | Selector equivalence, injectable gate, every branch |
| `tests/test_consumer_notice_evaluation.py` (new) | Metrics, baseline, degraded and failing runs, CLI |
| `tests/test_consumer_grounds.py`, `tests/test_consumer_evaluation.py` (modify) | Import moved constants; totals and gates |
| `.github/workflows/ci.yml`, `pyproject.toml`, `README.md`, `README-pt.md` (modify) | CI gate, coverage list, documentation |

---

### Task 1: Extract the ground selector into a pure module

**Files:**
- Create: `app/consumer/ground_selection.py`
- Modify: `app/consumer/service.py` (imports at the top; constants block before `DEFAULT_MAX_DOCUMENTS_PER_CASE`; `_CATEGORY_LABEL`; method `_legal_grounds`; the `subject = _CATEGORY_LABEL[...]` line; functions `_merge_results` and `_chunk_query_occurrences`)
- Modify: `tests/test_consumer_grounds.py:8-12` (imports)
- Modify: `pyproject.toml` (`[tool.coverage.run] source`)
- Test: `tests/test_ground_selection.py`

**Interfaces:**
- Consumes: `LegalCorpus.provisions_for_chunk`, `unit_for_chunk`, `provision_for_chunk`, `authority_for_chunk` (existing); `provision_is_eligible`, `strongly_supported_chunk_ids` from `app.consumer.legal_policy` (existing); `is_consumer_scope` from `app.consumer.retrieval` (existing).
- Produces, in `app.consumer.ground_selection`:
  - `MAX_GROUND_CANDIDATES: int = 8`, `MIN_GROUND_SCORE_RATIO: float = 0.5`, `MAX_LEGAL_GROUNDS: int = 8`
  - `CATEGORY_LABELS: dict[str, str]`
  - `SupportGate = Callable[[list[RetrievalTrace]], frozenset[str]]`
  - `issue_label(facts: ConsumerCaseFacts) -> str`
  - `select_legal_grounds(corpus: LegalCorpus, facts: ConsumerCaseFacts, result_sets: list[list[RetrievedChunk]], traces: list[RetrievalTrace] | None = None, *, support: SupportGate = strongly_supported_chunk_ids) -> list[LegalGround]`
  - `merge_results(result_sets: list[list[RetrievedChunk]]) -> list[RetrievedChunk]`
  - `chunk_query_occurrences(traces: list[RetrievalTrace]) -> dict[str, int]`

- [ ] **Step 1: Create the working branch**

```bash
git switch -c feat/final-ground-evaluator
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ground_selection.py`:

```python
"""The pure ground selector shared by the notice service and the evaluator."""

from __future__ import annotations

import hashlib

from app.consumer.ground_selection import (
    chunk_query_occurrences,
    issue_label,
    select_legal_grounds,
)
from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.schemas import ConsumerCaseFacts, ConsumerIssueCategory
from app.consumer.service import ConsumerCaseService
from app.schemas.rag import Chunk, RetrievedChunk
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace


def _facts(
    complaint: str = "A empresa cobrou duas vezes a mesma compra.",
) -> ConsumerCaseFacts:
    return ConsumerCaseFacts.model_validate(
        {
            "issue_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
            "complaint_summary": complaint,
            "desired_resolution": "Quero a devolução do valor pago em duplicidade.",
        }
    )


def _trace(
    results: list[RetrievedChunk],
    *,
    query_index: int = 0,
    error: str | None = None,
) -> RetrievalTrace:
    query = f"consulta {query_index}"
    return RetrievalTrace(
        batch_id="batch",
        agent="consumer_legal_authorities",
        doc_id=results[0].chunk.doc_id if results else "legal-doc",
        query_index=query_index,
        query=query,
        query_sha256=hashlib.sha256(query.encode()).hexdigest(),
        requested_k=max(1, len(results)),
        candidate_k=max(1, len(results)),
        returned_count=len(results),
        retrieval_mode="hybrid",
        embedding_model="test",
        vector_store="memory",
        index_version="test",
        chunking_version="test",
        score_type="rrf_score",
        rrf_constant=60,
        dense_weight=1.0,
        lexical_weight=1.0,
        error=error,
        results=[
            RetrievedItemTrace(
                rank=rank,
                chunk_id=item.chunk.chunk_id,
                doc_id=item.chunk.doc_id,
                page_start=item.chunk.page_start,
                page_end=item.chunk.page_end,
                score=item.score,
                content_sha256=hashlib.sha256(item.chunk.text.encode()).hexdigest(),
            )
            for rank, item in enumerate(results, start=1)
        ],
    )


def _chunk_for_unit(unit_id: str, *, include_inactive: bool = False) -> Chunk:
    return next(
        chunk
        for chunk in get_default_legal_corpus().as_chunks(include_inactive=include_inactive)
        if chunk.metadata.get("unit_id") == unit_id
    )


def _everything_supported(traces: list[RetrievalTrace]) -> frozenset[str]:
    return frozenset(item.chunk_id for trace in traces for item in trace.results)


def test_service_wrapper_returns_exactly_the_pure_selection() -> None:
    corpus = get_default_legal_corpus()
    service = object.__new__(ConsumerCaseService)
    service._legal_corpus = corpus
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.03)]]
    traces = [_trace(results[0])]

    via_service = service._legal_grounds(results, _facts(), traces)
    direct = select_legal_grounds(corpus, _facts(), results, traces)

    assert via_service == direct
    assert [ground.authority.unit_id for ground in direct] == [
        "br-cdc-art-42-paragrafo-unico"
    ]


def test_support_gate_is_injectable() -> None:
    corpus = get_default_legal_corpus()
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.0001)]]
    traces = [_trace(results[0])]

    assert select_legal_grounds(corpus, _facts(), results, traces) == []
    grounds = select_legal_grounds(
        corpus, _facts(), results, traces, support=_everything_supported
    )
    assert [ground.authority.unit_id for ground in grounds] == [
        "br-cdc-art-42-paragrafo-unico"
    ]


def test_out_of_scope_complaint_has_no_grounds() -> None:
    corpus = get_default_legal_corpus()
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.03)]]
    facts = _facts("Meu empregador não pagou meu salário nem o vale-transporte.")

    assert select_legal_grounds(corpus, facts, results, [_trace(results[0])]) == []


def test_supported_ids_without_results_yield_no_grounds() -> None:
    corpus = get_default_legal_corpus()

    grounds = select_legal_grounds(
        corpus,
        _facts(),
        [[]],
        [],
        support=lambda _traces: frozenset({"any-chunk"}),
    )

    assert grounds == []


def test_inactive_provisions_and_units_are_never_cited() -> None:
    corpus = get_default_legal_corpus()
    vetoed_article = next(
        chunk
        for chunk in corpus.as_chunks(include_inactive=True)
        if chunk.metadata.get("provision_id") == "br-cdc-art-11"
    )
    vetoed_unit = _chunk_for_unit("br-cdc-art-51-inciso-v", include_inactive=True)
    results = [
        [
            RetrievedChunk(chunk=vetoed_article, score=0.03),
            RetrievedChunk(chunk=vetoed_unit, score=0.03),
        ]
    ]

    grounds = select_legal_grounds(
        corpus, _facts(), results, [_trace(results[0])], support=_everything_supported
    )

    assert grounds == []


def test_failed_traces_do_not_count_as_query_support() -> None:
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    ok = _trace([RetrievedChunk(chunk=chunk, score=0.03)], query_index=0)
    failed = _trace([RetrievedChunk(chunk=chunk, score=0.03)], query_index=1, error="boom")

    assert chunk_query_occurrences([ok, failed]) == {chunk.chunk_id: 1}


def test_issue_label_defaults_to_a_generic_consumer_dispute() -> None:
    assert issue_label(ConsumerCaseFacts()) == "controvérsia de consumo"
    assert issue_label(_facts()) == "cobrança não reconhecida ou indevida"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ground_selection.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.consumer.ground_selection'`.

- [ ] **Step 4: Create the pure module**

Create `app/consumer/ground_selection.py`. The body of `select_legal_grounds` is the current `_legal_grounds` body with three substitutions: `self._legal_corpus` → `corpus`, `strongly_supported_chunk_ids(...)` → `support(...)`, and the label lookup → `issue_label(facts)`.

```python
"""Selection of the legal grounds a notice may cite.

Retrieval produces candidates; this module decides which of them become
``LegalGround`` objects. It is a pure function of the corpus, the confirmed
facts and the retrieval results, so the notice service and the notice-path
evaluator run exactly the same selection.
"""

from __future__ import annotations

from collections.abc import Callable

from app.consumer.legal_corpus import LegalCorpus
from app.consumer.legal_policy import provision_is_eligible, strongly_supported_chunk_ids
from app.consumer.retrieval import is_consumer_scope
from app.consumer.schemas import ConsumerCaseFacts, LegalGround, ProvisionStatus
from app.schemas.rag import RetrievedChunk
from app.schemas.trace import RetrievalTrace

# A notice cites law; a weakly ranked article is worse than a shorter notice.
# Only the strongest merged hits are eligible, each hit must stay within reach
# of the best one, and the score-type-aware gate in legal_policy must show
# dense/lexical agreement (or cross-query corroboration for non-RRF scores).
# Unlike the former relative-only floor, an arbitrary low-scoring top hit can
# no longer become authority merely because every other hit is even weaker.
MAX_GROUND_CANDIDATES = 8
MIN_GROUND_SCORE_RATIO = 0.5
MAX_LEGAL_GROUNDS = 8

CATEGORY_LABELS: dict[str, str] = {
    "unauthorized_charge": "cobrança não reconhecida ou indevida",
    "fraud": "fraude, golpe ou compra não reconhecida",
    "account_block": "bloqueio de conta, acesso ou valores",
    "negative_credit_record": "registro negativo de crédito",
    "loan_or_interest": "empréstimo, financiamento ou juros",
    "service_failure": "problema com produto ou serviço",
    "over_indebtedness": "superendividamento",
    "other": "controvérsia de consumo",
}

SupportGate = Callable[[list[RetrievalTrace]], frozenset[str]]


def issue_label(facts: ConsumerCaseFacts) -> str:
    """Lay description of the confirmed issue, used in notice prose."""

    return CATEGORY_LABELS[facts.issue_category.value if facts.issue_category else "other"]


def select_legal_grounds(
    corpus: LegalCorpus,
    facts: ConsumerCaseFacts,
    result_sets: list[list[RetrievedChunk]],
    traces: list[RetrievalTrace] | None = None,
    *,
    support: SupportGate = strongly_supported_chunk_ids,
) -> list[LegalGround]:
    """Return the legal grounds a notice would cite for these results.

    ``support`` is the retrieval-agreement gate. It is a parameter so the
    service can resolve it at call time and experiments can replace it.
    """

    category = facts.issue_category.value if facts.issue_category else "other"
    if not is_consumer_scope(
        category=category,
        complaint=facts.complaint_summary or "",
    ):
        return []
    # The issue category shapes the retrieval queries (see
    # build_legal_queries) but no longer decides which articles may be
    # cited: a consumer who picks the wrong type, or the catch-all
    # "other", must still be able to reach the authorities their own
    # report supports.
    strongly_supported = support(traces or [])
    if not strongly_supported:
        return []
    merged = merge_results(result_sets)
    if not merged:
        return []
    score_floor = merged[0].score * MIN_GROUND_SCORE_RATIO
    query_occurrences = chunk_query_occurrences(traces or [])
    candidates_by_provision: dict[str, list[tuple[int, RetrievedChunk]]] = {}
    for rank, result in enumerate(merged, start=1):
        if result.chunk.chunk_id not in strongly_supported:
            continue
        for provision in corpus.provisions_for_chunk(result):
            if provision.status is not ProvisionStatus.ACTIVE:
                continue
            if not provision_is_eligible(provision):
                continue
            unit = corpus.unit_for_chunk(result)
            if unit is not None and unit.status is not ProvisionStatus.ACTIVE:
                continue
            if provision.provision_id not in candidates_by_provision:
                candidates_by_provision[provision.provision_id] = []
            candidates_by_provision[provision.provision_id].append((rank, result))

    provision_candidates: list[tuple[int, float, int, RetrievedChunk]] = []
    for ranked_candidates in candidates_by_provision.values():
        selected_rank, selected = min(
            ranked_candidates,
            key=lambda item: (
                -query_occurrences.get(item[1].chunk.chunk_id, 0),
                -int(corpus.unit_for_chunk(item[1]) is not None),
                -item[1].score,
                item[1].chunk.chunk_id,
            ),
        )
        # A provision keeps the strongest position earned by any sibling,
        # while the quoted unit is the one corroborated across the most
        # independent formulations of the confirmed facts.
        provision_candidates.append(
            (
                min(rank for rank, _ in ranked_candidates),
                max(item.score for _, item in ranked_candidates),
                selected_rank,
                selected,
            )
        )

    grounds: list[LegalGround] = []
    issue = issue_label(facts)
    provision_candidates.sort(key=lambda item: (item[0], item[3].chunk.chunk_id))
    for _, provision_score, rank, result in provision_candidates[:MAX_GROUND_CANDIDATES]:
        if provision_score < score_floor:
            continue
        provision = corpus.provision_for_chunk(result)
        authority = corpus.authority_for_chunk(
            result,
            retrieval_rank=rank,
        )
        grounds.append(
            LegalGround(
                authority=authority,
                application_to_facts=(
                    f"O texto oficial em {provision.citation_label} foi localizado "
                    f"pela política de recuperação para {issue}. Sua aplicabilidade "
                    "ao caso não foi decidida pelo sistema e deve ser validada por "
                    "profissional habilitado contra os fatos e documentos citados."
                ),
            )
        )
        if len(grounds) >= MAX_LEGAL_GROUNDS:
            return grounds
    return grounds


def merge_results(result_sets: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    best: dict[str, RetrievedChunk] = {}
    for results in result_sets:
        for result in results:
            current = best.get(result.chunk.chunk_id)
            if current is None or result.score > current.score:
                best[result.chunk.chunk_id] = result
    return sorted(best.values(), key=lambda item: (-item.score, item.chunk.chunk_id))


def chunk_query_occurrences(traces: list[RetrievalTrace]) -> dict[str, int]:
    """Count independent queries that returned each chunk.

    Scores from separate RRF calls share a scale, but taking only their maximum
    erases the stronger signal that a statutory unit survived several distinct
    formulations of the confirmed facts.
    """

    occurrences: dict[str, set[tuple[str, int]]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        query_key = (trace.batch_id, trace.query_index)
        for item in trace.results:
            occurrences.setdefault(item.chunk_id, set()).add(query_key)
    return {chunk_id: len(queries) for chunk_id, queries in occurrences.items()}
```

- [ ] **Step 5: Make the service delegate to it**

In `app/consumer/service.py`:

1. After the `from app.consumer.composer import (...)` block, add:

```python
from app.consumer.ground_selection import issue_label, select_legal_grounds
from app.consumer.ground_selection import merge_results as _merge_results
```

2. In the `from app.consumer.legal_policy import (...)` block, delete the line `    provision_is_eligible,`. Keep `strongly_supported_chunk_ids`: the wrapper and the evidence code still use it.
3. In the `from app.consumer.schemas import (...)` block, delete the line `    ProvisionStatus,`.
4. Delete the six comment lines that start with `# A notice cites law; a weakly ranked article is worse than a shorter notice.` and the three constants `MAX_GROUND_CANDIDATES = 8`, `MIN_GROUND_SCORE_RATIO = 0.5`, `MAX_LEGAL_GROUNDS = 8`. Keep `DEFAULT_MAX_DOCUMENTS_PER_CASE = 20`.
5. Delete the whole `_CATEGORY_LABEL = {...}` dictionary.
6. Replace the whole method `_legal_grounds`, from `    def _legal_grounds(` through its final `        return grounds`, with:

```python
    def _legal_grounds(
        self,
        result_sets: list[list[RetrievedChunk]],
        facts: ConsumerCaseFacts,
        traces: list[RetrievalTrace] | None = None,
    ) -> list[LegalGround]:
        # The gate is looked up here, at call time, so replacing this module's
        # strongly_supported_chunk_ids (as docs/rag-review/probes.py does)
        # still changes what a notice cites.
        return select_legal_grounds(
            self._legal_corpus,
            facts,
            result_sets,
            traces,
            support=strongly_supported_chunk_ids,
        )
```

7. Replace the line
   `    subject = _CATEGORY_LABEL[facts.issue_category.value if facts.issue_category else "other"]`
   with
   `    subject = issue_label(facts)`.
8. Delete the module-level functions `def _merge_results(...)` and `def _chunk_query_occurrences(...)` (with their docstrings). Calls to `_merge_results(...)` elsewhere in the module keep working through the alias import.

- [ ] **Step 6: Point the existing tests at the moved constants**

In `tests/test_consumer_grounds.py`, replace

```python
from app.consumer.service import (
    MAX_GROUND_CANDIDATES,
    MIN_GROUND_SCORE_RATIO,
    ConsumerCaseService,
)
```

with

```python
from app.consumer.ground_selection import MAX_GROUND_CANDIDATES, MIN_GROUND_SCORE_RATIO
from app.consumer.service import ConsumerCaseService
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ground_selection.py tests/test_consumer_grounds.py tests/test_consumer_citation_integrity.py tests/test_consumer_api.py -q`
Expected: all pass.

Run: `python -c "from app.consumer.service import ConsumerCaseService, _merge_results; import app.consumer.service as s; print(s.strongly_supported_chunk_ids.__name__)"`
Expected: prints `strongly_supported_chunk_ids`. This is the import the review probes rely on.

- [ ] **Step 8: Hold the new module to 100% branch coverage**

In `pyproject.toml`, change `source = ["app.rag.reranking"]` to:

```toml
source = ["app.consumer.ground_selection", "app.rag.reranking"]
```

Run: `python -m pytest tests/test_ground_selection.py tests/test_consumer_grounds.py tests/test_reranking.py --cov --cov-report=term-missing -q`
Expected: `app/consumer/ground_selection.py` at 100%, no `Missing` entries, and the run passes `fail_under = 100`.

- [ ] **Step 9: Lint and type-check**

Run: `python -m ruff check app tests frontend`
If the only finding is import ordering (I001) in `app/consumer/service.py`, run `python -m ruff check --fix app/consumer/service.py` and re-run the check.
Run: `python -m mypy app`
Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 10: Commit**

```bash
git add app/consumer/ground_selection.py app/consumer/service.py tests/test_ground_selection.py tests/test_consumer_grounds.py pyproject.toml
git commit -F - <<'EOF'
refactor(consumer): extract the legal ground selector into a pure module

The notice service and the upcoming notice-path evaluator must run the same
selection. The retrieval-agreement gate becomes a parameter; the service
resolves it at call time so existing experiments still apply.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 2: Integer totals in evaluation summaries

**Files:**
- Modify: `app/schemas/evaluation.py` (`CaseResult`, `EvaluationRunMetadata`, `EvaluationSummary`)
- Modify: `app/evaluation/consumer_runner.py` (`check_consumer_gates`)
- Test: `tests/test_consumer_evaluation.py` (append two tests, extend one import)

**Interfaces:**
- Produces: `CaseResult.counts: dict[str, int]`; `EvaluationSummary.totals: dict[str, int]`, the per-name sum of case counts, sorted by name; `EvaluationRunMetadata.ground_policy_version: str | None`; `check_consumer_gates` resolves a metric name in `averages` first, then in `totals`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_evaluation.py`, add `CaseResult,` to the `from app.schemas.evaluation import (...)` block, then append:

```python
def test_summary_totals_sum_per_case_counts() -> None:
    summary = EvaluationSummary.from_cases(
        [
            CaseResult(case_name="a", counts={"consumer_notice_grounds": 3}),
            CaseResult(
                case_name="b",
                counts={
                    "consumer_notice_grounds": 2,
                    "consumer_notice_known_bad_citations": 1,
                },
            ),
        ]
    )

    assert summary.totals == {
        "consumer_notice_grounds": 5,
        "consumer_notice_known_bad_citations": 1,
    }


def test_gates_also_read_integer_totals() -> None:
    summary = EvaluationSummary(totals={"consumer_notice_known_bad_citations": 5})

    assert check_consumer_gates(
        summary, maximums=(("consumer_notice_known_bad_citations", 4.0),)
    ) == ["consumer_notice_known_bad_citations: 5.000 > allowed 4.000"]
    assert (
        check_consumer_gates(
            summary, maximums=(("consumer_notice_known_bad_citations", 5.0),)
        )
        == []
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_consumer_evaluation.py -q -k "totals"`
Expected: FAIL. `EvaluationSummary` has no attribute `totals`, and the gate reports `not produced by this run`.

- [ ] **Step 3: Implement the schema fields**

In `app/schemas/evaluation.py`:

1. In `class CaseResult`, after `errors: list[str] = Field(default_factory=list)`, add:

```python
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Integer counts for this case, summed into EvaluationSummary.totals",
    )
```

2. In `class EvaluationRunMetadata`, after `retrieval: RetrievalEvaluationConfiguration`, add:

```python
    ground_policy_version: str | None = None
```

3. In `class EvaluationSummary`, after `averages: dict[str, float] = Field(default_factory=dict)`, add:

```python
    totals: dict[str, int] = Field(default_factory=dict)
```

and in `from_cases`, add `totals=_sum_counts(cases),` to the `cls(...)` call, right after `averages=averages,`.

4. Add this function after `_aggregate_metrics`:

```python
def _sum_counts(cases: list[CaseResult]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for case in cases:
        for name, value in case.counts.items():
            totals[name] = totals.get(name, 0) + value
    return dict(sorted(totals.items()))
```

- [ ] **Step 4: Let the gates read totals**

In `app/evaluation/consumer_runner.py`, replace the body of `check_consumer_gates` with:

```python
    """Return deterministic regression-gate violations for a summary."""

    def lookup(name: str) -> float | None:
        if name in summary.averages:
            return summary.averages[name]
        if name in summary.totals:
            return float(summary.totals[name])
        return None

    violations: list[str] = []
    for name, floor in minimums:
        actual = lookup(name)
        if actual is None:
            violations.append(f"{name}: not produced by this run, cannot gate on it")
        elif actual < floor:
            violations.append(f"{name}: {actual:.3f} < required {floor:.3f}")
    for name, ceiling in maximums:
        actual = lookup(name)
        if actual is None:
            violations.append(f"{name}: not produced by this run, cannot gate on it")
        elif actual > ceiling:
            violations.append(f"{name}: {actual:.3f} > allowed {ceiling:.3f}")
    return violations
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_consumer_evaluation.py -q`
Expected: all pass, including the two new tests.

- [ ] **Step 6: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/schemas/evaluation.py app/evaluation/consumer_runner.py tests/test_consumer_evaluation.py
git commit -F - <<'EOF'
feat(evaluation): add integer totals to evaluation summaries

Metric scores are normalized to [0, 1], so counts such as cited grounds
need their own field. Regression gates now read totals as well.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 3: Notice-path evaluator

**Files:**
- Create: `app/evaluation/consumer_notice.py`
- Modify: `app/evaluation/consumer_retrievers.py` (rename the pipeline factories to public names, add `prepare_evaluation_pipeline`, use it in `_LazyConsumerRetriever._ready`)
- Modify: `app/evaluation/consumer_runner.py` (rename `_query_hashes` to `query_hashes` at its definition and its two call sites)
- Test: `tests/test_consumer_notice_evaluation.py`

**Interfaces:**
- Consumes: `select_legal_grounds` (Task 1); `CaseResult.counts`, `EvaluationRunMetadata.ground_policy_version` (Task 2).
- Produces, in `app.evaluation.consumer_retrievers`:
  - `offline_pipeline(corpus: LegalCorpus) -> RagPipeline`
  - `configured_pipeline(corpus: LegalCorpus) -> RagPipeline`
  - `async prepare_evaluation_pipeline(factory: PipelineFactory, corpus: LegalCorpus) -> RagPipeline`
- Produces, in `app.evaluation.consumer_runner`: `query_hashes(queries: Sequence[str]) -> tuple[str, ...]`
- Produces, in `app.evaluation.consumer_notice`:
  - `NOTICE_REQUESTED_K = 8`
  - `COMPLEMENTARY_LAW_IDS: frozenset[str]` = `{"br-lgpd", "br-cc"}`
  - `NoticePipelineName = Literal["offline", "configured"]`
  - `is_known_bad_citation(provision_id: str, unit_id: str | None, hard_negatives: frozenset[str]) -> bool`
  - `notice_ground_metrics(grounds: list[LegalGround], case: ConsumerLegalGoldenCase, *, degraded: bool) -> tuple[list[MetricResult], dict[str, int]]`
  - `class ConsumerNoticeGroundEvaluator(pipeline: RagPipeline, corpus: LegalCorpus, *, retriever_id: str)` with `async run(dataset: ConsumerLegalGoldenDataset) -> EvaluationSummary`
  - `async run_notice_evaluation(dataset: ConsumerLegalGoldenDataset, *, pipeline_name: NoticePipelineName = "offline") -> EvaluationSummary`
- Count names: `consumer_notice_grounds`, `consumer_notice_known_bad_citations`, `consumer_notice_complementary_grounds`. Metric names: `consumer_notice_exact_recall`, `consumer_notice_abstention`, `consumer_notice_semantic_success`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_consumer_notice_evaluation.py`:

```python
"""Notice-path evaluation: final grounds scored against the golden labels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import LEGAL_GROUND_POLICY_VERSION
from app.consumer.schemas import ConsumerIssueCategory, LegalGround
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.evaluation.consumer_notice import (
    ConsumerNoticeGroundEvaluator,
    is_known_bad_citation,
    notice_ground_metrics,
    run_notice_evaluation,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.evaluation import ConsumerLegalGoldenCase, ConsumerLegalRelevance
from app.schemas.rag import RetrievedChunk

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")


def _ground(unit_id: str) -> LegalGround:
    corpus = get_default_legal_corpus()
    chunk = next(
        item for item in corpus.as_chunks() if item.metadata.get("unit_id") == unit_id
    )
    return LegalGround(
        authority=corpus.authority_for_chunk(
            RetrievedChunk(chunk=chunk, score=0.03), retrieval_rank=1
        ),
        application_to_facts="fixture",
    )


def _case(**overrides: Any) -> ConsumerLegalGoldenCase:
    payload: dict[str, Any] = {
        "case_id": "notice_fixture",
        "category": "unauthorized_charge",
        "intake_category": ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
        "slices": ("supplier:telecom",),
        "complaint": "A operadora cobrou um pacote que eu nunca contratei e já paguei.",
        "desired_resolution": "Quero meu dinheiro de volta.",
        "relevant": (
            ConsumerLegalRelevance(
                article_id="br-cdc-art-42",
                unit_id="br-cdc-art-42-paragrafo-unico",
                grade=3,
                rationale="repetição do indébito",
            ),
            ConsumerLegalRelevance(
                article_id="br-cdc-art-6",
                unit_id="br-cdc-art-6-inciso-iii",
                grade=1,
                rationale="informação clara",
            ),
        ),
        "hard_negatives": ("br-cdc-art-43",),
    }
    payload.update(overrides)
    return ConsumerLegalGoldenCase(**payload)


class _BrokenQueryEmbedder(MockEmbeddingClient):
    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("synthetic outage")


class _ExplodingPipeline:
    def retrieval_configuration(self, **_: object) -> dict[str, object]:
        return {"requested_k": 8, "retrieval_mode": "hybrid"}

    async def retrieve_many_with_traces(self, *_: object, **__: object) -> object:
        raise RuntimeError("store offline")


def test_known_bad_matching_is_exact_and_unit_aware() -> None:
    negatives = frozenset({"br-cdc-art-42", "br-cdc-art-18-paragrafo-1"})

    assert is_known_bad_citation("br-cdc-art-42", "br-cdc-art-42-paragrafo-unico", negatives)
    assert is_known_bad_citation(
        "br-cdc-art-18", "br-cdc-art-18-paragrafo-1-inciso-ii", negatives
    )
    assert not is_known_bad_citation("br-cdc-art-42-a", "br-cdc-art-42-a-caput", negatives)
    assert not is_known_bad_citation("br-cdc-art-18", "br-cdc-art-18-paragrafo-10", negatives)
    assert not is_known_bad_citation("br-cdc-art-18", None, negatives)


def test_final_grounds_are_scored_by_exact_unit_and_known_bad_citations() -> None:
    grounds = [
        _ground("br-cdc-art-42-paragrafo-unico"),
        _ground("br-cdc-art-43-paragrafo-2"),
    ]

    metrics, counts = notice_ground_metrics(grounds, _case(), degraded=False)

    assert counts == {
        "consumer_notice_grounds": 2,
        "consumer_notice_known_bad_citations": 1,
        "consumer_notice_complementary_grounds": 0,
    }
    assert {metric.name: metric.score for metric in metrics} == {
        "consumer_notice_semantic_success": 1.0,
        "consumer_notice_exact_recall": 0.5,
    }


def test_no_ground_cases_score_abstention_and_degradation() -> None:
    case = _case(
        category="no_consumer_relationship",
        relevant=(),
        hard_negatives=(),
        no_applicable_ground=True,
    )

    abstained, _ = notice_ground_metrics([], case, degraded=True)
    cited, _ = notice_ground_metrics(
        [_ground("br-cdc-art-42-paragrafo-unico")], case, degraded=False
    )

    assert {metric.name: metric.score for metric in abstained} == {
        "consumer_notice_semantic_success": 0.0,
        "consumer_notice_abstention": 1.0,
    }
    assert {metric.name: metric.score for metric in cited}["consumer_notice_abstention"] == 0.0


async def test_offline_notice_baseline_on_the_seed_dataset() -> None:
    """Baseline measured on 2026-09-07 (plan.md finding 4); change it deliberately."""

    summary = await run_notice_evaluation(load_consumer_legal_dataset(DATASET_PATH))

    assert summary.failed_case_count == 0
    assert summary.totals == {
        "consumer_notice_complementary_grounds": 0,
        "consumer_notice_grounds": 79,
        "consumer_notice_known_bad_citations": 4,
    }
    assert summary.averages["consumer_notice_exact_recall"] == 0.333
    assert summary.averages["consumer_notice_abstention"] == 1.0
    assert summary.averages["consumer_notice_semantic_success"] == 1.0
    assert summary.run is not None
    assert summary.run.cutoffs == (8,)
    assert summary.run.ground_policy_version == LEGAL_GROUND_POLICY_VERSION
    assert summary.run.retrieval.retriever_id == "offline_notice_path"


async def test_degraded_retrieval_is_reported_per_case() -> None:
    corpus = get_default_legal_corpus()
    store = InMemoryVectorStore()
    await RagPipeline(MockEmbeddingClient(), store).index_chunks(corpus.as_chunks())
    evaluator = ConsumerNoticeGroundEvaluator(
        RagPipeline(_BrokenQueryEmbedder(), store), corpus, retriever_id="degraded_test"
    )
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    single = dataset.model_copy(update={"cases": dataset.cases[:1]})

    summary = await evaluator.run(single)

    assert summary.cases[0].retrieval_outcome == "degraded"
    assert summary.averages["consumer_notice_semantic_success"] == 0.0


async def test_a_failing_case_is_recorded_without_stopping_the_run() -> None:
    corpus = get_default_legal_corpus()
    evaluator = ConsumerNoticeGroundEvaluator(
        _ExplodingPipeline(),  # type: ignore[arg-type]
        corpus,
        retriever_id="exploding",
    )

    summary = await evaluator.run(load_consumer_legal_dataset(DATASET_PATH))

    failed = [case for case in summary.cases if case.retrieval_outcome == "failed"]
    assert len(failed) == 13
    assert summary.failed_case_count == 13
    assert all("store offline" in case.errors[0] for case in failed)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_consumer_notice_evaluation.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.evaluation.consumer_notice'`.

- [ ] **Step 3: Share the evaluation pipelines**

In `app/evaluation/consumer_retrievers.py`:

1. Rename `def _offline_pipeline(` to `def offline_pipeline(` and `def _configured_pipeline(` to `def configured_pipeline(`. Update the two module-level instances at the bottom to pass `offline_pipeline` and `configured_pipeline`.
2. Add after `configured_pipeline`:

```python
async def prepare_evaluation_pipeline(
    factory: PipelineFactory,
    corpus: LegalCorpus,
) -> RagPipeline:
    """Build one evaluation stack whose legal corpus is ready to search.

    Offline stacks index the corpus in memory. A configured stack must already
    hold the active embedding generation; evaluation never re-embeds it.
    """

    pipeline = factory(corpus)
    if pipeline.embedding_artifacts_dir is None:
        await pipeline.index_chunks(corpus.as_chunks())
    elif not await legal_corpus_is_indexed(pipeline, corpus):
        raise RuntimeError(
            "the configured legal embedding generation is not active; "
            "run `python -m app.consumer.preindex_legal` first"
        )
    return pipeline
```

3. In `_LazyConsumerRetriever._ready`, replace the body of the inner `if self._pipeline is None or self._doc_id is None:` block with:

```python
                corpus = get_default_legal_corpus()
                self._pipeline = await prepare_evaluation_pipeline(self._factory, corpus)
                self._doc_id = corpus.as_parsed_document().doc_id
                self._corpus = corpus
```

In `app/evaluation/consumer_runner.py`, rename `def _query_hashes(` to `def query_hashes(` and update its two call sites (`query_sha256=_query_hashes(queries)` → `query_sha256=query_hashes(queries)`).

- [ ] **Step 4: Create the notice-path evaluator**

Create `app/evaluation/consumer_notice.py`:

```python
"""Evaluate the legal grounds a notice would actually cite.

The retrieval benchmark scores ranked candidates. A notice cites only what the
production selector keeps, so this evaluator runs the production-shaped batch
(the three production queries, k=8, hybrid) through the same
``select_legal_grounds`` the service uses, and scores the final grounds against
the golden labels.
"""

from __future__ import annotations

from typing import Literal

from app.consumer.ground_selection import select_legal_grounds
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_policy import LEGAL_GROUND_POLICY_VERSION
from app.consumer.retrieval import build_legal_queries, is_consumer_scope
from app.consumer.schemas import ConsumerCaseFacts, LegalGround
from app.core.config import RetrievalMode
from app.evaluation.consumer_golden import validate_consumer_legal_labels
from app.evaluation.consumer_retrievers import (
    configured_pipeline,
    offline_pipeline,
    prepare_evaluation_pipeline,
)
from app.evaluation.consumer_runner import QUERY_BUILDER_VERSION, query_hashes
from app.rag.pipeline import RagPipeline
from app.schemas.evaluation import (
    CaseResult,
    ConsumerLegalGoldenCase,
    ConsumerLegalGoldenDataset,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricResult,
    RankedEvaluationRetrievalHit,
    RetrievalEvaluationConfiguration,
)

NOTICE_REQUESTED_K = 8
# Sources that may only complement the CDC in a notice. None exist until the
# LGPD and the Civil Code enter the corpus; the count is reported from now on.
COMPLEMENTARY_LAW_IDS = frozenset({"br-lgpd", "br-cc"})
_UNIT_MARKERS = ("-caput", "-paragrafo-", "-inciso-", "-alinea-")

NoticePipelineName = Literal["offline", "configured"]


def is_known_bad_citation(
    provision_id: str,
    unit_id: str | None,
    hard_negatives: frozenset[str],
) -> bool:
    """Whether a cited ground is one of the case's labelled hard negatives.

    Article-level labels match by provision id only: a prefix test would treat
    art. 42-A as a subdivision of art. 42. Unit-level labels also match their
    own descendants, because a labelled paragraph covers its incisos.
    """

    if provision_id in hard_negatives:
        return True
    if unit_id is None:
        return False
    return any(
        unit_id == negative or (_is_unit_id(negative) and unit_id.startswith(f"{negative}-"))
        for negative in hard_negatives
    )


def _is_unit_id(stable_id: str) -> bool:
    return any(marker in stable_id for marker in _UNIT_MARKERS)


def notice_ground_metrics(
    grounds: list[LegalGround],
    case: ConsumerLegalGoldenCase,
    *,
    degraded: bool,
) -> tuple[list[MetricResult], dict[str, int]]:
    """Score the grounds of one case; counts are summed across the run."""

    hard_negatives = frozenset(case.hard_negatives)
    counts = {
        "consumer_notice_grounds": len(grounds),
        "consumer_notice_known_bad_citations": sum(
            is_known_bad_citation(
                ground.authority.provision_id, ground.authority.unit_id, hard_negatives
            )
            for ground in grounds
        ),
        "consumer_notice_complementary_grounds": sum(
            ground.authority.law_id in COMPLEMENTARY_LAW_IDS for ground in grounds
        ),
    }
    metrics = [
        MetricResult(
            name="consumer_notice_semantic_success",
            score=0.0 if degraded else 1.0,
            details=(
                "lexical-only fallback was used" if degraded else "hybrid retrieval completed"
            ),
        )
    ]
    if case.no_applicable_ground:
        metrics.append(
            MetricResult(
                name="consumer_notice_abstention",
                score=1.0 if not grounds else 0.0,
                details=f"{len(grounds)} grounds cited for a no-ground complaint",
            )
        )
        return metrics, counts
    cited = sum(
        any(_cites(ground, judgment.article_id, judgment.unit_id) for ground in grounds)
        for judgment in case.relevant
    )
    metrics.append(
        MetricResult(
            name="consumer_notice_exact_recall",
            score=round(cited / len(case.relevant), 3),
            details=f"{cited}/{len(case.relevant)} labelled judgments cited",
        )
    )
    return metrics, counts


def _cites(ground: LegalGround, article_id: str, unit_id: str | None) -> bool:
    if unit_id is not None:
        return ground.authority.unit_id == unit_id
    return ground.authority.provision_id == article_id


def _failed_notice_metrics(case: ConsumerLegalGoldenCase) -> list[MetricResult]:
    name = (
        "consumer_notice_abstention"
        if case.no_applicable_ground
        else "consumer_notice_exact_recall"
    )
    return [
        MetricResult(
            name="consumer_notice_semantic_success",
            score=0.0,
            details="retrieval failed",
        ),
        MetricResult(
            name=name,
            score=0.0,
            details="retrieval failure is not a valid result",
        ),
    ]


class ConsumerNoticeGroundEvaluator:
    """Run golden cases through production retrieval and ground selection."""

    def __init__(
        self,
        pipeline: RagPipeline,
        corpus: LegalCorpus,
        *,
        retriever_id: str,
    ) -> None:
        self._pipeline = pipeline
        self._corpus = corpus
        self._retriever_id = retriever_id

    async def run(self, dataset: ConsumerLegalGoldenDataset) -> EvaluationSummary:
        corpus = validate_consumer_legal_labels(dataset, corpus=self._corpus)
        doc_id = corpus.as_parsed_document().doc_id
        cases = [await self._run_case(case, doc_id) for case in dataset.cases]
        configuration = self._pipeline.retrieval_configuration(
            requested_k=NOTICE_REQUESTED_K,
            mode=RetrievalMode.HYBRID,
            doc_id=doc_id,
        )
        retrieval = RetrievalEvaluationConfiguration.model_validate(
            {
                **configuration,
                "retriever_id": self._retriever_id,
                "configuration_complete": True,
            }
        )
        run = EvaluationRunMetadata(
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_sha256=dataset.content_sha256,
            dataset_review_status=dataset.review_status,
            corpus_release_id=corpus.release_id,
            corpus_sha256=corpus.corpus_sha256,
            query_builder_version=QUERY_BUILDER_VERSION,
            queries_per_case=3,
            cutoffs=(NOTICE_REQUESTED_K,),
            retrieval=retrieval,
            ground_policy_version=LEGAL_GROUND_POLICY_VERSION,
        )
        return EvaluationSummary.from_cases(cases, run=run)

    async def _run_case(self, case: ConsumerLegalGoldenCase, doc_id: str) -> CaseResult:
        facts = ConsumerCaseFacts(
            issue_category=case.intake_category,
            complaint_summary=case.complaint,
            desired_resolution=case.desired_resolution,
        )
        queries: list[str] = []
        grounds: list[LegalGround] = []
        try:
            if not is_consumer_scope(
                category=case.intake_category.value,
                complaint=case.complaint,
            ):
                outcome = "scope_gate_abstained"
            else:
                queries = build_legal_queries(facts)
                result_sets, traces = await self._pipeline.retrieve_many_with_traces(
                    queries,
                    doc_id=doc_id,
                    agent="consumer_legal_authorities",
                    k=NOTICE_REQUESTED_K,
                    mode=RetrievalMode.HYBRID,
                )
                grounds = select_legal_grounds(self._corpus, facts, result_sets, traces)
                outcome = (
                    "degraded" if any(trace.degraded_mode for trace in traces) else "completed"
                )
        except Exception as exc:  # one provider failure must not erase other cases
            return CaseResult(
                case_name=case.case_id,
                category=case.category,
                slices=case.slices,
                queries=tuple(queries),
                query_sha256=query_hashes(queries),
                retrieval_outcome="failed",
                metrics=_failed_notice_metrics(case),
                errors=[f"notice retrieval failed: {type(exc).__name__}: {exc}"],
            )
        metrics, counts = notice_ground_metrics(grounds, case, degraded=outcome == "degraded")
        return CaseResult(
            case_name=case.case_id,
            category=case.category,
            slices=case.slices,
            queries=tuple(queries),
            query_sha256=query_hashes(queries),
            retrieved_hits=tuple(
                RankedEvaluationRetrievalHit(
                    rank=position,
                    retrieval_id=ground.authority.unit_id or ground.authority.provision_id,
                    provision_id=ground.authority.provision_id,
                    unit_id=ground.authority.unit_id,
                    score=ground.authority.retrieval_score or 0.0,
                    status=ground.authority.status.value,
                )
                for position, ground in enumerate(grounds, start=1)
            ),
            retrieval_outcome=outcome,
            metrics=metrics,
            counts=counts,
        )


async def run_notice_evaluation(
    dataset: ConsumerLegalGoldenDataset,
    *,
    pipeline_name: NoticePipelineName = "offline",
) -> EvaluationSummary:
    """Evaluate final grounds with the offline mock stack or the configured one."""

    corpus = get_default_legal_corpus()
    factory = offline_pipeline if pipeline_name == "offline" else configured_pipeline
    pipeline = await prepare_evaluation_pipeline(factory, corpus)
    evaluator = ConsumerNoticeGroundEvaluator(
        pipeline, corpus, retriever_id=f"{pipeline_name}_notice_path"
    )
    return await evaluator.run(dataset)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_consumer_notice_evaluation.py tests/test_consumer_evaluation.py -q`
Expected: all pass. The baseline test must report exactly 79 / 4 / 0.333. If it does not, stop: do not edit the expected numbers. Compare with a direct run of `ConsumerCaseService._legal_grounds` on the same cases, and report the difference before continuing.

- [ ] **Step 6: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/evaluation/consumer_notice.py app/evaluation/consumer_retrievers.py app/evaluation/consumer_runner.py tests/test_consumer_notice_evaluation.py
git commit -F - <<'EOF'
feat(evaluation): score the grounds a notice would actually cite

Runs the production-shaped batch (three queries, k=8, hybrid) through the
shared ground selector and reports cited grounds, known-bad citations, exact
recall, abstention and semantic success. Baseline on the seed dataset:
79 grounds, 4 known-bad citations, exact recall 0.333.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 4: CLI mode, CI gate and documentation

**Files:**
- Modify: `app/evaluation/consumer_runner.py` (`_cli`)
- Modify: `.github/workflows/ci.yml` (new step after "Consumer legal-retrieval regression gates"; artifact paths)
- Modify: `README.md` (after the configured-benchmark code block that ends with `--output consumer-retrieval-results.json`)
- Modify: `README-pt.md` (same place in "Testes e avaliação")
- Test: `tests/test_consumer_notice_evaluation.py` (append two tests)

**Interfaces:**
- Consumes: `run_notice_evaluation` (Task 3); `check_consumer_gates` reading totals (Task 2).
- Produces: CLI flags `--evaluate-notice`, `--notice-pipeline {offline,configured}` (default `offline`) and `--require-semantic`. The last one adds the gate `consumer_notice_semantic_success >= 1.0` and requires `--evaluate-notice`. `--evaluate-notice` cannot be combined with `--retriever` or `--empty-baseline`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_consumer_notice_evaluation.py`:

```python
import asyncio
import json
import sys

import pytest

from app.evaluation import consumer_runner


def test_cli_writes_notice_results_and_gates_on_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "notice.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consumer_runner",
            str(DATASET_PATH),
            "--evaluate-notice",
            "--output",
            str(output),
            "--max",
            "consumer_notice_known_bad_citations=3",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["totals"]["consumer_notice_known_bad_citations"] == 4


def test_cli_rejects_require_semantic_without_notice_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["consumer_runner", "--require-semantic"])

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 2
```

Move the new `import` lines to the top of the file with the other imports, keeping ruff's import order.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_consumer_notice_evaluation.py -q -k cli`
Expected: FAIL, because argparse rejects `--evaluate-notice` and `--require-semantic` as unrecognized arguments (exit code 2 in both cases, so the first test fails on the exit-code assertion).

- [ ] **Step 3: Add the notice mode to the CLI**

In `app/evaluation/consumer_runner.py`, inside `_cli`, add these arguments right after the `--max` argument:

```python
    parser.add_argument(
        "--evaluate-notice",
        action="store_true",
        help="score the grounds the production selector would cite (k=8, three queries)",
    )
    parser.add_argument(
        "--notice-pipeline",
        choices=("offline", "configured"),
        default="offline",
        help="stack used by --evaluate-notice; 'configured' needs an active legal index",
    )
    parser.add_argument(
        "--require-semantic",
        action="store_true",
        help="with --evaluate-notice, fail when any case fell back to lexical-only retrieval",
    )
```

Then replace everything from `args = parser.parse_args()` through the line `summary = await ConsumerLegalRetrievalEvaluator(retriever).run(dataset)` with:

```python
    args = parser.parse_args()

    if args.evaluate_notice and (args.retriever or args.empty_baseline):
        parser.error("--evaluate-notice cannot be combined with --retriever or --empty-baseline")
    if args.require_semantic and not args.evaluate_notice:
        parser.error("--require-semantic requires --evaluate-notice")
    if args.retriever and args.empty_baseline:
        parser.error("--retriever and --empty-baseline are mutually exclusive")
    dataset = load_consumer_legal_dataset(Path(args.dataset))
    minimums = list(args.minimums)
    if args.evaluate_notice:
        from app.evaluation.consumer_notice import run_notice_evaluation

        summary = await run_notice_evaluation(dataset, pipeline_name=args.notice_pipeline)
        if args.require_semantic:
            minimums.append(("consumer_notice_semantic_success", 1.0))
    else:
        if args.retriever:
            retriever = _import_retriever(args.retriever)
        elif args.empty_baseline:
            retriever = _empty_retriever
        else:
            from app.evaluation.consumer_retrievers import offline_hybrid_retriever

            retriever = offline_hybrid_retriever
        summary = await ConsumerLegalRetrievalEvaluator(retriever).run(dataset)
```

Finally, change the gate call from `minimums=args.minimums,` to `minimums=minimums,`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_consumer_notice_evaluation.py tests/test_consumer_evaluation.py -q`
Expected: all pass.

- [ ] **Step 5: Add the CI gate**

In `.github/workflows/ci.yml`, insert after the "Consumer legal-retrieval regression gates" step:

```yaml
      - name: Consumer notice final-ground gates
        # Scores the grounds the production selector would cite, not ranked
        # candidates. The known-bad ceiling is the measured baseline; it may
        # only go down.
        run: |
          python -m app.evaluation.consumer_runner \
            eval_data/consumer_legal_retrieval \
            --evaluate-notice \
            --output consumer-notice-results.json \
            --max consumer_notice_known_bad_citations=4 \
            --min consumer_notice_abstention=1.0
```

In the "Upload consumer evaluation evidence" step, replace `path: consumer-legal-retrieval-results.json` with:

```yaml
          path: |
            consumer-legal-retrieval-results.json
            consumer-notice-results.json
```

- [ ] **Step 6: Document the mode**

In `README.md`, after the code block that ends with `--output consumer-retrieval-results.json`, add:

````markdown
The notice-path evaluation scores the grounds the production selector would
actually cite (the three production queries, k=8, and the same
`select_legal_grounds` the service uses), rather than ranked candidates:

```powershell
python -m app.evaluation.consumer_runner --evaluate-notice --output notice-results.json
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured `
  --require-semantic --output configured-notice-results.json
```

It reports cited grounds, known-bad citations (labelled hard negatives that
were cited), exact recall over cited units, abstention and semantic success.
````

In `README-pt.md`, after the code block that ends with `--output consumer-retrieval-results.json`, add:

````markdown
A avaliação pelo caminho da notificação mede os fundamentos que o seletor de
produção realmente citaria (as três consultas de produção, k=8, e o mesmo
`select_legal_grounds` usado pelo serviço), e não candidatos ranqueados:

```powershell
python -m app.evaluation.consumer_runner --evaluate-notice --output notice-results.json
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured `
  --require-semantic --output configured-notice-results.json
```

O relatório traz fundamentos citados, citações ruins conhecidas (hard negatives
rotulados que foram citados), recall exato das unidades citadas, abstenção e
sucesso da recuperação semântica.
````

- [ ] **Step 7: Run the CI commands locally**

Run: `python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --output consumer-notice-results.json --max consumer_notice_known_bad_citations=4 --min consumer_notice_abstention=1.0`
Expected: exit code 0.
Then delete the generated `consumer-notice-results.json`; it is a CI artifact, not a tracked file.

Run: `python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --output consumer-legal-retrieval-results.json --min consumer_retrieval_success@5=1.0 --min consumer_recall@5=0.29 --min consumer_article_recall@5=0.75 --min consumer_ndcg@5=0.25 --min consumer_abstention@5=1.0 --max consumer_hard_negative_rate@5=0.07 --max consumer_inactive_provision_rate@5=0.0 --max consumer_unknown_status_rate@5=0.0`
Expected: exit code 0. The retrieval gates are unaffected by this plan. Delete the generated JSON.

- [ ] **Step 8: Full verification**

Run: `python -m pytest -q`
Expected: every test passes. That is the 303 existing tests plus the new ones from Tasks 1–4.
Run: `python -m pytest --cov --cov-report=term-missing -q`
Expected: 100% for `app/consumer/ground_selection.py` and `app/rag/reranking.py`.
Run: `python -m ruff check app tests frontend`, `python -m mypy app`, `lint-imports`, `vulture app --min-confidence 90`
Expected: all clean.

- [ ] **Step 9: Commit**

```bash
git add app/evaluation/consumer_runner.py tests/test_consumer_notice_evaluation.py .github/workflows/ci.yml README.md README-pt.md
git commit -F - <<'EOF'
feat(evaluation): add --evaluate-notice mode and a CI final-ground gate

CI now fails if the production selector cites more labelled hard negatives
than the measured baseline (4) or cites anything for a no-ground case.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

## Notes for the executor

- The retrieval evaluator's `_is_hard_negative` in `app/evaluation/consumer_runner.py` still uses the prefix test described in the Global Constraints (it counts 5 instead of 4 on the same grounds). Fixing it changes `consumer_hard_negative_rate@5` and is deliberately out of scope here. It is recorded as a follow-up in Plan C.
- Plan C updates the baseline test in `tests/test_consumer_notice_evaluation.py` when the LGPD and the Civil Code enter the corpus. Its acceptance rule is that known-bad citations on the original 15 cases stay at 4 or fewer.
