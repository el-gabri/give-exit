# Evaluation That Sees Precision, a Holdout and Uncertainty — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Consumer retrieval and notice evaluation report precision, article recall, a development/holdout split, bootstrap intervals and paired comparisons, and let the configured JUÁ stack run from cached golden query vectors.

**Architecture:** Metrics, splits and intervals extend the existing evaluation schemas and evaluators in `app/evaluation`. A new `compare` CLI pairs two result files. A new `query_vectors` module holds an evaluation-only cache wrapped around the configured embedder, wired through `configured_pipeline`; production code is untouched.

**Tech Stack:** Python 3.10+/3.12, pydantic v2, pytest + pytest-asyncio (auto mode), ruff, mypy strict, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-02-evaluation-precision-holdout-design.md`

## Global Constraints

- No production behaviour change: nothing under `app/consumer`, `app/rag` or `app/api` changes.
- No JUÁ re-index: the embedding-generation identity is untouched.
- No query text is persisted; the cache lives under `<data_dir>/evaluation/query_vectors/` and `data/evaluation/` is gitignored.
- Ruff: line length 100, rules `E,F,I,UP,B,SIM,C90`, max complexity 10. Mypy: strict. Python floor: 3.10.
- New modules `app.evaluation.intervals`, `app.evaluation.compare` and `app.evaluation.query_vectors` join the 100% coverage scope in `pyproject.toml`; mark only `if __name__ == "__main__":` lines `# pragma: no cover`.
- Tests that build `Settings` pass `_env_file=None`, so a developer's `.env` (which configures JUÁ) never leaks in.
- On this machine run Python as `.venv/Scripts/python.exe`. CI uses `python`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Measured offline baselines (agreement depth 13, dataset 2.2.0), used by Tasks 3 and 9:

  | Run | Grounds | Labelled | Unlabelled | Known-bad | Precision | Article recall | Abstention |
  |---|---|---|---|---|---|---|---|
  | all 43 cases | 26 | 2 | 24 | 0 | 0.083 | 0.045 | 0.8 |
  | development (24) | 17 | 1 | 16 | 0 | 0.042 | 0.025 | 1.0 |
  | holdout (19) | 9 | 1 | 8 | 0 | 0.167 | 0.077 | 0.667 |

  Retrieval, development split: recall@5 0.142, article recall@5 0.225, nDCG@5 0.075,
  hard-negative rate@5 0.025, abstention@5 1.0. All six new no-ground cases pass the scope gate.

## Review Focus

1. A result file written before this change (no `split`, `intervals`, `case_split` or new metrics) given to `compare` must load, pair and name the metrics only one run reports — Task 5.
2. The same query text twice in one embedding batch must be embedded once and returned in order — Task 7.
3. `--split NAME` on a dataset with no case in that split must exit 2 with a message, not a traceback or an empty summary — Task 2.
4. A case whose split changed between two compared runs, under `--split`, must be excluded and listed — Task 5.
5. A cache file with Windows line endings or blank lines (copied between machines) must load — Task 7.

---

### Task 1: Notice ground classes, precision and article recall

**Files:**
- Modify: `app/evaluation/consumer_notice.py` (`notice_ground_metrics`, `_failed_notice_metrics`; add `_ground_class`)
- Modify: `app/evaluation/consumer_runner.py` (`render_agreement_sweep` columns)
- Test: `tests/test_consumer_notice_evaluation.py`

**Interfaces:**
- Produces: counts `consumer_notice_labelled_grounds`, `consumer_notice_unlabelled_grounds`; metrics `consumer_notice_precision` (only when a case cited ≥1 ground), `consumer_notice_article_recall` (cases with relevant judgments). Later tasks pin these names.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_notice_evaluation.py`, replace `test_final_grounds_are_scored_by_exact_unit_and_known_bad_citations` with:

```python
def test_final_grounds_are_scored_by_exact_unit_and_known_bad_citations() -> None:
    grounds = [
        _ground("br-cdc-art-42-paragrafo-unico"),
        _ground("br-cdc-art-43-paragrafo-2"),
    ]

    metrics, counts = notice_ground_metrics(grounds, _case(), degraded=False)

    assert counts == {
        "consumer_notice_grounds": 2,
        "consumer_notice_known_bad_citations": 1,
        "consumer_notice_labelled_grounds": 1,
        "consumer_notice_unlabelled_grounds": 0,
        "consumer_notice_complementary_grounds": 0,
    }
    assert {metric.name: metric.score for metric in metrics} == {
        "consumer_notice_semantic_success": 1.0,
        "consumer_notice_precision": 0.5,
        "consumer_notice_exact_recall": 0.5,
        "consumer_notice_article_recall": 0.5,
    }


def test_a_known_bad_unit_of_a_labelled_article_is_not_counted_as_labelled() -> None:
    case = _case(
        relevant=(
            ConsumerLegalRelevance(
                article_id="br-cdc-art-39",
                unit_id="br-cdc-art-39-inciso-i",
                grade=3,
                rationale="venda casada",
            ),
        ),
        hard_negatives=("br-cdc-art-39-inciso-iii",),
    )

    metrics, counts = notice_ground_metrics(
        [_ground("br-cdc-art-39-inciso-iii")], case, degraded=False
    )

    assert counts["consumer_notice_known_bad_citations"] == 1
    assert counts["consumer_notice_labelled_grounds"] == 0
    assert counts["consumer_notice_unlabelled_grounds"] == 0
    scores = {metric.name: metric.score for metric in metrics}
    assert scores["consumer_notice_precision"] == 0.0
    assert scores["consumer_notice_article_recall"] == 0.0


def test_article_recall_counts_distinct_labelled_articles() -> None:
    grounds = [
        _ground("br-cdc-art-42-paragrafo-unico"),
        _ground("br-cdc-art-42-caput"),
        _ground("br-cdc-art-51-inciso-iv"),
    ]

    metrics, counts = notice_ground_metrics(grounds, _case(), degraded=False)

    assert counts["consumer_notice_labelled_grounds"] == 2
    assert counts["consumer_notice_unlabelled_grounds"] == 1
    scores = {metric.name: metric.score for metric in metrics}
    assert scores["consumer_notice_precision"] == 0.667
    # Two grounds cite art. 42 and none cites art. 6: one of two labelled articles.
    assert scores["consumer_notice_article_recall"] == 0.5
    assert scores["consumer_notice_exact_recall"] == 0.5


def test_a_case_that_cites_nothing_has_no_precision() -> None:
    metrics, counts = notice_ground_metrics([], _case(), degraded=False)

    assert counts["consumer_notice_grounds"] == 0
    assert {metric.name: metric.score for metric in metrics} == {
        "consumer_notice_semantic_success": 1.0,
        "consumer_notice_exact_recall": 0.0,
        "consumer_notice_article_recall": 0.0,
    }
```

In `test_no_ground_cases_score_abstention_and_degradation`, replace the last assertion with:

```python
    cited_scores = {metric.name: metric.score for metric in cited}
    assert cited_scores["consumer_notice_abstention"] == 0.0
    assert cited_scores["consumer_notice_precision"] == 0.0
```

In `test_a_failing_case_is_recorded_without_stopping_the_run`, append:

```python
    assert failed[0].score("consumer_notice_article_recall") == 0.0
    assert failed[0].score("consumer_notice_precision") is None
```

In `test_offline_notice_baseline_on_the_seed_dataset`, append this paragraph to the docstring and replace the `summary.totals` assertion:

```python
    Re-measured on 2026-10-02 for the ground classes (precision and article
    recall, spec 2026-10-02): of the 22 grounds, 2 cite an article the case
    labels and 20 cite one it does not.
```

```python
    assert summary.totals == {
        "consumer_notice_complementary_grounds": 7,
        "consumer_notice_grounds": 22,
        "consumer_notice_known_bad_citations": 0,
        "consumer_notice_labelled_grounds": 2,
        "consumer_notice_unlabelled_grounds": 20,
    }
```

In `test_cli_prints_an_agreement_sweep`, after the `table[0]` assertion, add:

```python
    assert "labelled" in table[0].split()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_notice_evaluation.py -q`
Expected: FAIL — the new count and metric keys are missing.

- [ ] **Step 3: Implement**

In `app/evaluation/consumer_notice.py`, add below `_is_unit_id`:

```python
GroundClass = Literal["known_bad", "labelled", "unlabelled"]


def _ground_class(
    ground: LegalGround,
    hard_negatives: frozenset[str],
    labelled_articles: frozenset[str],
) -> GroundClass:
    """Classify one cited ground. A known-bad citation is never labelled.

    The precedence matters when a hard negative is a unit of a labelled
    article: citing CDC art. 39 III in a case labelled art. 39 I, with 39 III
    as its hard negative, is a wrong citation, not a right article.
    """

    authority = ground.authority
    if is_known_bad_citation(authority.provision_id, authority.unit_id, hard_negatives):
        return "known_bad"
    if authority.provision_id in labelled_articles:
        return "labelled"
    return "unlabelled"
```

Replace `notice_ground_metrics` with:

```python
def notice_ground_metrics(
    grounds: list[LegalGround],
    case: ConsumerLegalGoldenCase,
    *,
    degraded: bool,
) -> tuple[list[MetricResult], dict[str, int]]:
    """Score the grounds of one case; counts are summed across the run."""

    labelled_articles = frozenset(judgment.article_id for judgment in case.relevant)
    hard_negatives = frozenset(case.hard_negatives)
    classes = [_ground_class(ground, hard_negatives, labelled_articles) for ground in grounds]
    labelled = classes.count("labelled")
    counts = {
        "consumer_notice_grounds": len(grounds),
        "consumer_notice_known_bad_citations": classes.count("known_bad"),
        "consumer_notice_labelled_grounds": labelled,
        "consumer_notice_unlabelled_grounds": classes.count("unlabelled"),
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
    if grounds:
        # A case that cites nothing has no precision, so a run that cites
        # nothing produces none and a gate on it fails instead of passing.
        metrics.append(
            MetricResult(
                name="consumer_notice_precision",
                score=round(labelled / len(grounds), 3),
                details=f"{labelled}/{len(grounds)} cited grounds are labelled for the case",
            )
        )
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
    cited_articles = {
        ground.authority.provision_id
        for ground, kind in zip(grounds, classes, strict=True)
        if kind == "labelled"
    }
    metrics.extend(
        [
            MetricResult(
                name="consumer_notice_exact_recall",
                score=round(cited / len(case.relevant), 3),
                details=f"{cited}/{len(case.relevant)} labelled judgments cited",
            ),
            MetricResult(
                name="consumer_notice_article_recall",
                score=round(len(cited_articles) / len(labelled_articles), 3),
                details=(
                    f"{len(cited_articles)}/{len(labelled_articles)} labelled articles cited"
                ),
            ),
        ]
    )
    return metrics, counts
```

Replace `_failed_notice_metrics` with:

```python
def _failed_notice_metrics(case: ConsumerLegalGoldenCase) -> list[MetricResult]:
    invalid = "retrieval failure is not a valid result"
    semantic = MetricResult(
        name="consumer_notice_semantic_success", score=0.0, details="retrieval failed"
    )
    if case.no_applicable_ground:
        return [
            semantic,
            MetricResult(name="consumer_notice_abstention", score=0.0, details=invalid),
        ]
    return [
        semantic,
        MetricResult(name="consumer_notice_exact_recall", score=0.0, details=invalid),
        MetricResult(name="consumer_notice_article_recall", score=0.0, details=invalid),
    ]
```

In `app/evaluation/consumer_runner.py`, `render_agreement_sweep`, add the labelled column after `known_bad`:

```python
    columns = (
        ("grounds", "consumer_notice_grounds"),
        ("complementary", "consumer_notice_complementary_grounds"),
        ("known_bad", "consumer_notice_known_bad_citations"),
        ("labelled", "consumer_notice_labelled_grounds"),
        ("verifier_removed", "consumer_notice_verifier_removed"),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_notice_evaluation.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/evaluation/consumer_notice.py app/evaluation/consumer_runner.py tests/test_consumer_notice_evaluation.py
git commit -m "feat(evaluation): score notice precision and article recall

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: A development/holdout split through schemas, evaluators and CLIs

**Files:**
- Modify: `app/schemas/evaluation.py`
- Modify: `app/evaluation/consumer_golden.py` (add `dataset_split`)
- Modify: `app/evaluation/consumer_runner.py` (case split, run metadata, `--split`, `_prepare`)
- Modify: `app/evaluation/consumer_notice.py` (case split, run metadata)
- Modify: `app/evaluation/label_ranks.py` (`--split`)
- Test: `tests/test_consumer_evaluation.py`, `tests/test_consumer_notice_evaluation.py`, `tests/test_label_ranks.py`

**Interfaces:**
- Produces: `CaseSplit = Literal["development", "holdout"]`, `CASE_SPLITS: tuple[CaseSplit, ...]` in `app.schemas.evaluation`; `ConsumerLegalGoldenCase.split: CaseSplit = "development"`; `ConsumerLegalGoldenDataset.case_split -> str` (the single split, or `"all"`); `CaseResult.split: str | None`; `EvaluationSummary.by_split: dict[str, EvaluationGroupSummary]`; `EvaluationRunMetadata.case_split: str | None`; `dataset_split(dataset, split: str) -> ConsumerLegalGoldenDataset` in `app.evaluation.consumer_golden`; `consumer_runner._prepare(parser, args) -> ConsumerLegalGoldenDataset`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_evaluation.py`, add imports `asyncio`, `sys`, `from app.evaluation import consumer_runner` and change the golden import to `from app.evaluation.consumer_golden import dataset_split, load_consumer_legal_dataset`. Add:

```python
def _split_dataset() -> ConsumerLegalGoldenDataset:
    holdout = ConsumerLegalGoldenCase.model_validate(
        {**_case().model_dump(mode="json"), "case_id": "holdout_fixture", "split": "holdout"}
    )
    return ConsumerLegalGoldenDataset(
        dataset_id="split-fixture",
        version="1.0.0",
        description="Small deterministic evaluator fixture for golden splits.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(_case(), holdout),
    )


def _write_dataset(path: Path, dataset: ConsumerLegalGoldenDataset) -> Path:
    path.write_text(dataset.model_dump_json(), encoding="utf-8")
    return path


def test_golden_cases_default_to_development_and_reject_unknown_splits() -> None:
    payload = _case().model_dump(mode="json")

    assert _case().split == "development"
    assert ConsumerLegalGoldenCase.model_validate({**payload, "split": "holdout"}).split == (
        "holdout"
    )
    with pytest.raises(ValidationError, match="split"):
        ConsumerLegalGoldenCase.model_validate({**payload, "split": "test"})


def test_dataset_split_keeps_only_the_requested_cases() -> None:
    dataset = _split_dataset()
    development = dataset_split(dataset, "development")

    assert dataset_split(dataset, "all") is dataset
    assert [case.case_id for case in dataset_split(dataset, "holdout").cases] == [
        "holdout_fixture"
    ]
    assert dataset.case_split == "all"
    assert development.case_split == "development"
    with pytest.raises(ValueError, match="unknown split"):
        dataset_split(dataset, "test")
    with pytest.raises(ValueError, match="no holdout cases"):
        dataset_split(development, "holdout")


async def test_case_results_carry_their_split_and_the_summary_groups_by_it() -> None:
    def retriever(_query: str, _k: int) -> list[str]:
        return ["br-cdc-art-42-paragrafo-unico"]

    summary = await ConsumerLegalRetrievalEvaluator(retriever).run(_split_dataset())

    assert [case.split for case in summary.cases] == ["development", "holdout"]
    assert set(summary.by_split) == {"development", "holdout"}
    assert summary.by_split["holdout"].case_count == 1
    assert summary.run is not None and summary.run.case_split == "all"


def test_cli_evaluates_one_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dataset_path = _write_dataset(tmp_path / "dataset.json", _split_dataset())
    output = tmp_path / "out.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consumer_runner",
            str(dataset_path),
            "--empty-baseline",
            "--split",
            "holdout",
            "--output",
            str(output),
        ],
    )

    asyncio.run(consumer_runner._cli())

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [case["case_name"] for case in payload["cases"]] == ["holdout_fixture"]
    assert payload["run"]["case_split"] == "holdout"


def test_cli_rejects_a_split_the_dataset_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    development = dataset_split(_split_dataset(), "development")
    dataset_path = _write_dataset(tmp_path / "dataset.json", development)
    monkeypatch.setattr(
        sys,
        "argv",
        ["consumer_runner", str(dataset_path), "--empty-baseline", "--split", "holdout"],
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 2
    assert "no holdout cases" in capsys.readouterr().err
```

In `tests/test_consumer_notice_evaluation.py`, `test_degraded_retrieval_is_reported_per_case`, append:

```python
    assert summary.cases[0].split == "development"
    assert summary.run is not None and summary.run.case_split == "development"
```

In `tests/test_label_ranks.py`, add `ConsumerLegalGoldenCase` to the existing `from app.schemas.evaluation import ...` line and:

```python
async def test_cli_ranks_one_split(tmp_path: Path) -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    first = dataset.cases[0]
    holdout = ConsumerLegalGoldenCase.model_validate(
        {**first.model_dump(mode="json"), "case_id": "holdout_fixture", "split": "holdout"}
    )
    subset = dataset.model_copy(update={"cases": (first, holdout)})
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(subset.model_dump_json(), encoding="utf-8")
    output = tmp_path / "ranks.json"

    await label_ranks._cli([str(dataset_path), "--split", "holdout", "--output", str(output)])

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert {label["case_id"] for label in payload["labels"]} == {"holdout_fixture"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py tests/test_label_ranks.py -q`
Expected: FAIL — `split` is an unknown field, `dataset_split` does not exist.

- [ ] **Step 3: Implement the schemas**

In `app/schemas/evaluation.py`, below `MetricDirection`:

```python
CaseSplit = Literal["development", "holdout"]
CASE_SPLITS: tuple[CaseSplit, ...] = ("development", "holdout")
```

In `CaseResult`, after `slices`:

```python
    split: str | None = None
```

In `EvaluationRunMetadata`, after `ground_verifier`:

```python
    case_split: str | None = Field(
        default=None, description="Golden split evaluated: development, holdout or all"
    )
```

In `EvaluationSummary`, after `by_slice`:

```python
    by_split: dict[str, EvaluationGroupSummary] = Field(default_factory=dict)
```

and in `from_cases`, after `by_slice=...`:

```python
            by_split=_group_cases(
                cases, lambda case: (case.split,) if case.split is not None else ()
            ),
```

In `ConsumerLegalGoldenCase`, after `no_applicable_ground`:

```python
    split: CaseSplit = Field(
        default="development",
        description=(
            "development cases may inform parameter choices; holdout cases are only "
            "reported, and move to development once a decision has used them"
        ),
    )
```

In `ConsumerLegalGoldenDataset`, after `content_sha256`:

```python
    @property
    def case_split(self) -> str:
        """The split every case belongs to, or ``all`` when the cases are mixed."""
        splits = {case.split for case in self.cases}
        return next(iter(splits)) if len(splits) == 1 else "all"
```

- [ ] **Step 4: Implement `dataset_split`**

In `app/evaluation/consumer_golden.py`, import `CASE_SPLITS` from `app.schemas.evaluation` and add:

```python
def dataset_split(
    dataset: ConsumerLegalGoldenDataset, split: str
) -> ConsumerLegalGoldenDataset:
    """The dataset restricted to one split; ``all`` returns it unchanged."""

    if split == "all":
        return dataset
    if split not in CASE_SPLITS:
        raise ValueError(f"unknown split {split!r}; expected all, development or holdout")
    cases = tuple(case for case in dataset.cases if case.split == split)
    if not cases:
        raise ValueError(f"the dataset has no {split} cases")
    return dataset.model_copy(update={"cases": cases})
```

- [ ] **Step 5: Carry the split through both evaluators**

In `app/evaluation/consumer_runner.py`, `ConsumerLegalRetrievalEvaluator._run_case`, add `split=case.split,` after `slices=case.slices,` in both `CaseResult(...)` constructions, and in `run` add `case_split=dataset.case_split,` to `EvaluationRunMetadata(...)`.

In `app/evaluation/consumer_notice.py`, `ConsumerNoticeGroundEvaluator._run_case`, add `split=case.split,` after `slices=case.slices,` in both `CaseResult(...)` constructions, and in `run` add `case_split=dataset.case_split,` to `EvaluationRunMetadata(...)`.

- [ ] **Step 6: Add `--split` to both CLIs**

In `app/evaluation/consumer_runner.py`, import `CASE_SPLITS` from `app.schemas.evaluation`, `ConsumerLegalGoldenDataset` is already imported, and import `dataset_split` from `app.evaluation.consumer_golden`. Add:

```python
def _prepare(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> ConsumerLegalGoldenDataset:
    """Load the dataset and keep the split the flags select."""
    dataset = load_consumer_legal_dataset(Path(args.dataset))
    try:
        return dataset_split(dataset, args.split)
    except ValueError as exc:
        parser.error(str(exc))
```

In `_cli`, add the argument before `args = parser.parse_args()`:

```python
    parser.add_argument(
        "--split",
        choices=("all", *CASE_SPLITS),
        default="all",
        help="evaluate only the development or the holdout cases (default: all)",
    )
```

and replace `dataset = load_consumer_legal_dataset(Path(args.dataset))` with `dataset = _prepare(parser, args)`.

In `app/evaluation/label_ranks.py`, import `dataset_split` alongside `load_consumer_legal_dataset` and `CASE_SPLITS` from `app.schemas.evaluation`. In `_cli`, add the same `--split` argument, then replace the dataset line with:

```python
    try:
        dataset = dataset_split(load_consumer_legal_dataset(Path(args.dataset)), args.split)
    except ValueError as exc:
        parser.error(str(exc))
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py tests/test_label_ranks.py -q`
Expected: PASS.

- [ ] **Step 8: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/schemas/evaluation.py app/evaluation/consumer_golden.py app/evaluation/consumer_runner.py app/evaluation/consumer_notice.py app/evaluation/label_ranks.py tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py tests/test_label_ranks.py
git commit -m "feat(evaluation): carry a development/holdout split through golden runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Golden dataset 2.2.0 with an explicit holdout

**Files:**
- Modify: `eval_data/consumer_legal_retrieval/dataset.json`
- Test: `tests/test_consumer_evaluation.py`, `tests/test_consumer_notice_evaluation.py`

**Interfaces:**
- Consumes: `ConsumerLegalGoldenCase.split` (Task 2), the notice counts and metrics (Task 1).
- Produces: dataset 2.2.0 — 43 cases, 19 holdout (the set `HOLDOUT_CASES` below), 39 that pass the scope gate.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_evaluation.py`, add near the top:

```python
HOLDOUT_CASES = frozenset(
    {
        "cartao_de_credito_nao_solicitado",
        "titulo_de_capitalizacao_exigido_no_emprestimo",
        "preco_da_prateleira_diferente_no_caixa",
        "conserto_sem_orcamento",
        "preco_abusivo_durante_enchente",
        "recusa_de_venda_a_vista",
        "nome_mantido_no_cadastro_apos_quitacao",
        "quitacao_antecipada_do_financiamento",
        "multa_de_cancelamento_da_academia",
        "pedido_de_acesso_aos_dados_ignorado",
        "carregador_pegou_fogo",
        "voo_cancelado_sem_assistencia",
        "loja_nega_garantia_e_manda_ao_fabricante",
        "cliente_empresarial_nao_paga",
        "caucao_retida_pelo_proprietario",
        "auxilio_doenca_negado_pelo_inss",
        "restituicao_do_imposto_retida",
        "batida_de_carro_com_particular",
        "taxa_extra_do_condominio",
    }
)
```

In `test_seed_dataset_is_separate_versioned_and_explicitly_unreviewed`, change `"2.1.0"` to `"2.2.0"`, `== 37` to `== 43` and the no-ground count `== 4` to `== 10`.

Replace `test_dataset_version_is_two_one_zero` with:

```python
def test_dataset_version_is_two_two_zero_with_an_explicit_holdout() -> None:
    """2.2.0 states every case's split. The holdout is the 13 in-scope cases
    2.1.0 added, on which no parameter was tuned, and six new disputes with no
    consumer relationship written without consulting the scope gate's keywords.
    The traffic-fine and private-loan cases stay in development: the scope
    keywords were written against them."""
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    raw = json.loads((DATASET_PATH / "dataset.json").read_text(encoding="utf-8"))

    assert dataset.version == "2.2.0"
    assert all("split" in case for case in raw["cases"])
    assert {case.case_id for case in dataset.cases if case.split == "holdout"} == HOLDOUT_CASES
    new_no_ground = [
        case for case in dataset.cases if case.no_applicable_ground and case.split == "holdout"
    ]
    assert len(new_no_ground) == 6
    assert all("ground:none" in case.slices for case in new_no_ground)
```

In `tests/test_consumer_notice_evaluation.py`:

`test_offline_notice_baseline_on_the_seed_dataset` — append to the docstring:

```python
    Re-measured on 2026-10-02 for dataset 2.2.0 (six holdout disputes with no
    consumer relationship; every earlier case unchanged): 26 grounds, 2
    labelled, 24 unlabelled, still no known-bad citation. All six new cases pass
    the scope gate and two still get grounds (cliente_empresarial_nao_paga: CDC
    arts. 18, 104-B § 4 and 20 II; batida_de_carro_com_particular: CDC art. 18),
    so holdout abstention is 0.667 while development stays 1.0.
```

and replace its assertions from `summary.totals` through `semantic_success` with:

```python
    assert summary.totals == {
        "consumer_notice_complementary_grounds": 7,
        "consumer_notice_grounds": 26,
        "consumer_notice_known_bad_citations": 0,
        "consumer_notice_labelled_grounds": 2,
        "consumer_notice_unlabelled_grounds": 24,
    }
    assert summary.averages["consumer_notice_exact_recall"] == 0.0
    assert summary.averages["consumer_notice_abstention"] == 0.8
    assert summary.averages["consumer_notice_semantic_success"] == 1.0
    development = summary.by_split["development"].averages
    holdout = summary.by_split["holdout"].averages
    assert development["consumer_notice_abstention"] == 1.0
    assert development["consumer_notice_precision"] == 0.042
    assert development["consumer_notice_article_recall"] == 0.025
    assert holdout["consumer_notice_abstention"] == 0.667
    assert holdout["consumer_notice_precision"] == 0.167
    assert holdout["consumer_notice_article_recall"] == 0.077
```

`test_a_failing_case_is_recorded_without_stopping_the_run` — replace the comment and counts:

```python
    # Every case the scope gate lets through fails: 43 cases in dataset 2.2.0
    # minus the four development no-ground cases the gate stops first. The six
    # holdout no-ground cases pass the gate.
    failed = [case for case in summary.cases if case.retrieval_outcome == "failed"]
    assert len(failed) == 39
    assert summary.failed_case_count == 39
```

`test_an_agreement_sweep_retrieves_each_case_once` — replace the tail:

```python
    # 43 cases, four of them stopped by the scope gate before retrieval.
    assert len(calls) == 39
    assert shallow.run is not None and shallow.run.agreement_max_rank == 8
    assert default.run is not None and default.run.agreement_max_rank == 13
    assert default.run.ground_verifier == "none"
    # A shallower gate can only drop grounds.
    assert shallow.totals["consumer_notice_grounds"] == 14
    assert default.totals["consumer_notice_grounds"] == 26
```

`test_verifier_removals_are_counted_per_case` — `== 22` becomes `== 26`.

`test_cli_prints_an_agreement_sweep` — the row assertion becomes:

```python
    assert [line.split()[:2] for line in table[1:]] == [["16", "31"], ["20", "40"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py -q`
Expected: FAIL — the dataset is still 2.1.0 with 37 cases.

- [ ] **Step 3: Rewrite the dataset**

Run once (not committed):

```bash
.venv/Scripts/python.exe - <<'EOF'
import json
from pathlib import Path

path = Path("eval_data/consumer_legal_retrieval/dataset.json")
data = json.loads(path.read_text(encoding="utf-8"))
assert data["version"] == "2.1.0" and len(data["cases"]) == 37
holdout = {case["case_id"] for case in data["cases"][22:35]}
assert holdout == {
    "cartao_de_credito_nao_solicitado", "titulo_de_capitalizacao_exigido_no_emprestimo",
    "preco_da_prateleira_diferente_no_caixa", "conserto_sem_orcamento",
    "preco_abusivo_durante_enchente", "recusa_de_venda_a_vista",
    "nome_mantido_no_cadastro_apos_quitacao", "quitacao_antecipada_do_financiamento",
    "multa_de_cancelamento_da_academia", "pedido_de_acesso_aos_dados_ignorado",
    "carregador_pegou_fogo", "voo_cancelado_sem_assistencia",
    "loja_nega_garantia_e_manda_ao_fabricante",
}
for case in data["cases"]:
    case["split"] = "holdout" if case["case_id"] in holdout else "development"


def no_ground(case_id, domain, complaint, desired, negatives):
    return {
        "case_id": case_id,
        "category": "no_consumer_relationship",
        "slices": ["ground:none", f"domain:{domain}", "wording:lay"],
        "complaint": complaint,
        "desired_resolution": desired,
        "relevant": [],
        "hard_negatives": negatives,
        "no_applicable_ground": True,
        "split": "holdout",
    }


data["cases"].extend([
    no_ground(
        "cliente_empresarial_nao_paga", "b2b",
        "Tenho uma pequena gráfica e imprimi cinco mil folhetos para uma rede de academias. "
        "Entreguei tudo no prazo, emiti a nota fiscal e a empresa não paga há três meses.",
        "Quero receber o valor da nota fiscal com correção.",
        ["br-cdc-art-2", "br-cdc-art-35", "br-cdc-art-42"],
    ),
    no_ground(
        "caucao_retida_pelo_proprietario", "private_lease",
        "Aluguei um apartamento direto com o dono, sem imobiliária. Saí do imóvel há dois "
        "meses, devolvi as chaves e ele ainda não devolveu o depósito de caução de três "
        "aluguéis.",
        "Quero a devolução do depósito de caução.",
        ["br-cdc-art-42", "br-cdc-art-51"],
    ),
    no_ground(
        "auxilio_doenca_negado_pelo_inss", "social_security",
        "Pedi auxílio-doença ao INSS depois de uma cirurgia no joelho e o pedido foi negado, "
        "mesmo com os laudos do médico dizendo que não posso trabalhar.",
        "Quero que o benefício seja concedido desde a data do pedido.",
        ["br-cdc-art-22", "br-cdc-art-6-inciso-x"],
    ),
    no_ground(
        "restituicao_do_imposto_retida", "tax",
        "Minha declaração do imposto de renda caiu na malha fina por causa de um recibo "
        "médico e a restituição está retida há um ano, mesmo eu tendo mandado os documentos "
        "pedidos pela Receita Federal.",
        "Quero liberar a restituição com a correção.",
        ["br-cdc-art-22", "br-cdc-art-42"],
    ),
    no_ground(
        "batida_de_carro_com_particular", "traffic_accident",
        "Um motorista bateu na traseira do meu carro no semáforo, admitiu a culpa na hora, "
        "mas agora não atende minhas ligações nem paga o conserto do para-choque.",
        "Quero que ele pague o orçamento do conserto.",
        ["br-cdc-art-12", "br-cdc-art-14"],
    ),
    no_ground(
        "taxa_extra_do_condominio", "condominium",
        "O condomínio aprovou uma taxa extra para reformar a fachada numa assembleia da qual "
        "não fui avisado, e agora a administração cobra a taxa junto com a mensalidade.",
        "Quero suspender a cobrança da taxa extra até uma nova assembleia.",
        ["br-cdc-art-39-inciso-iii", "br-cdc-art-42"],
    ),
])
data["version"] = "2.2.0"
data["description"] = (
    "Developer-authored seed for offline retrieval evaluation from lay Brazilian consumer "
    "complaints to CDC provisions, with LGPD and Civil Code complements. It is not a "
    "production legal golden until specialist review. Development cases may inform "
    "parameter choices; holdout cases are only reported, and a holdout case whose result "
    "informed a decision moves to development in the next version."
)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(len(data["cases"]), "cases")
EOF
```

Expected output: `43 cases`. These six case texts were fixed before measurement and must not be edited to change how the system scores them.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py tests/test_label_ranks.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add eval_data/consumer_legal_retrieval/dataset.json tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py
git commit -m "test(evaluation): golden dataset 2.2.0 with an explicit holdout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Bootstrap intervals for every averaged metric

**Files:**
- Create: `app/evaluation/intervals.py`
- Modify: `app/schemas/evaluation.py` (`MetricInterval`; `intervals` on `EvaluationGroupSummary` and `EvaluationSummary`)
- Modify: `app/evaluation/consumer_runner.py`, `app/evaluation/consumer_notice.py` (wrap summaries with `with_intervals`)
- Modify: `pyproject.toml` (coverage source)
- Test: `tests/test_evaluation_intervals.py`, `tests/test_consumer_notice_evaluation.py`, `tests/test_consumer_evaluation.py`

**Interfaces:**
- Consumes: `CaseResult.split`, `EvaluationSummary.by_split` (Task 2).
- Produces: `MetricInterval(low: float, high: float, level: float, resamples: int, seed: int)` in `app.schemas.evaluation`; in `app.evaluation.intervals`: `bootstrap_interval(scores, *, resamples=2000, level=0.95, seed=0) -> MetricInterval`, `paired_difference_interval(before, after, *, resamples=2000, level=0.95, seed=0) -> MetricInterval`, `metric_intervals(cases) -> dict[str, MetricInterval]`, `with_intervals(summary) -> EvaluationSummary`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evaluation_intervals.py`:

```python
"""Bootstrap intervals for evaluation averages and paired differences."""

from __future__ import annotations

from typing import Any

import pytest

from app.evaluation.intervals import (
    bootstrap_interval,
    metric_intervals,
    paired_difference_interval,
    with_intervals,
)
from app.schemas.evaluation import CaseResult, EvaluationSummary, MetricResult


def test_a_hand_checked_interval() -> None:
    # random.Random(0) draws 0.844, 0.758, 0.421, 0.259, 0.511, 0.405, 0.784,
    # 0.303. With two cases the resampled index pairs are (1, 1), (0, 0),
    # (1, 0) and (1, 0), so the means of [0, 1] are 1, 0, 0.5 and 0.5. At level
    # 0.5 the bounds are the 1st and 3rd of the four sorted means (0-based).
    interval = bootstrap_interval([0.0, 1.0], resamples=4, level=0.5)

    assert (interval.low, interval.high) == (0.5, 1.0)
    assert (interval.resamples, interval.level, interval.seed) == (4, 0.5, 0)


def test_identical_scores_give_a_zero_width_interval() -> None:
    constant = bootstrap_interval([0.25, 0.25, 0.25])
    single = bootstrap_interval([0.7])

    assert (constant.low, constant.high) == (0.25, 0.25)
    assert (single.low, single.high) == (0.7, 0.7)


def test_intervals_are_deterministic_and_bracket_the_mean() -> None:
    scores = [0.0, 0.0, 1.0, 0.5, 1.0, 0.0, 0.25]

    interval = bootstrap_interval(scores)

    assert interval == bootstrap_interval(scores)
    assert interval.low <= sum(scores) / len(scores) <= interval.high
    assert 0.0 <= interval.low < interval.high <= 1.0


@pytest.mark.parametrize(
    ("scores", "options", "message"),
    [
        ([], {}, "at least one score"),
        ([0.5], {"resamples": 0}, "resamples"),
        ([0.5], {"level": 1.0}, "level"),
    ],
)
def test_invalid_requests_are_rejected(
    scores: list[float], options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        bootstrap_interval(scores, **options)


def test_paired_differences() -> None:
    before = [0.0, 0.5, 1.0]

    shifted = paired_difference_interval(before, [0.1, 0.6, 1.1])
    same = paired_difference_interval(before, before)

    assert (shifted.low, shifted.high) == (0.1, 0.1)
    assert (same.low, same.high) == (0.0, 0.0)
    with pytest.raises(ValueError, match="same number"):
        paired_difference_interval([0.1], [0.1, 0.2])


def _result(name: str, split: str, score: float, *slices: str) -> CaseResult:
    return CaseResult(
        case_name=name,
        split=split,
        slices=slices,
        metrics=[MetricResult(name="m", score=score)],
    )


def test_summaries_get_intervals_overall_and_per_split_only() -> None:
    summary = with_intervals(
        EvaluationSummary.from_cases(
            [
                _result("a", "development", 1.0, "supplier:bank"),
                _result("b", "development", 1.0),
                _result("c", "holdout", 0.0),
            ]
        )
    )

    assert set(summary.intervals) == {"m"}
    assert summary.by_split["development"].intervals["m"].low == 1.0
    assert summary.by_split["holdout"].intervals["m"].high == 0.0
    assert summary.by_slice["supplier:bank"].intervals == {}
    assert metric_intervals([]) == {}
```

In `tests/test_consumer_notice_evaluation.py`, `test_offline_notice_baseline_on_the_seed_dataset`, append:

```python
    assert set(summary.intervals) == set(summary.averages)
    assert set(summary.by_split["holdout"].intervals) == set(holdout)
```

In `tests/test_consumer_evaluation.py`, `test_builtin_offline_hybrid_retriever_returns_auditable_legal_hits`, append:

```python
    assert set(summary.intervals) == set(summary.averages)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluation_intervals.py -q`
Expected: FAIL — `app.evaluation.intervals` does not exist.

- [ ] **Step 3: Add `MetricInterval` and the summary fields**

In `app/schemas/evaluation.py`, above `EvaluationGroupSummary`:

```python
class MetricInterval(BaseModel):
    """A bootstrap confidence interval for one averaged metric or difference."""

    model_config = ConfigDict(frozen=True)

    low: float
    high: float
    level: float = Field(gt=0.0, lt=1.0)
    resamples: int = Field(ge=1)
    seed: int
```

Add to `EvaluationGroupSummary` and to `EvaluationSummary` (after `averages`):

```python
    intervals: dict[str, MetricInterval] = Field(default_factory=dict)
```

- [ ] **Step 4: Create `app/evaluation/intervals.py`**

```python
"""Case-bootstrap confidence intervals for evaluation averages.

A golden run averages per-case scores over a few dozen cases, so one case
flipping moves an average by several hundredths. These intervals resample the
cases with replacement and report the percentile range of the resampled means:
how far an average can be trusted to separate two runs.

Indices are drawn as ``int(rng.random() * n)`` from ``random.Random(seed)``
rather than with ``randrange`` or ``choices``, whose algorithms are not
promised to stay fixed across Python releases, so the same scores and seed give
the same interval on every supported interpreter.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from functools import lru_cache

from app.schemas.evaluation import CaseResult, EvaluationSummary, MetricInterval

DEFAULT_RESAMPLES = 2_000
DEFAULT_LEVEL = 0.95
DEFAULT_SEED = 0


def bootstrap_interval(
    scores: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    level: float = DEFAULT_LEVEL,
    seed: int = DEFAULT_SEED,
) -> MetricInterval:
    """Percentile bootstrap interval of the mean of ``scores``."""

    if not scores:
        raise ValueError("a bootstrap interval needs at least one score")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie strictly between 0 and 1")
    values = [float(score) for score in scores]
    count = len(values)
    means = sorted(
        sum(values[index] for index in sample) / count
        for sample in _resample_indices(count, resamples, seed)
    )
    tail = (1.0 - level) / 2.0
    return MetricInterval(
        low=round(means[_order_statistic(tail, resamples)], 3),
        high=round(means[_order_statistic(1.0 - tail, resamples)], 3),
        level=level,
        resamples=resamples,
        seed=seed,
    )


def paired_difference_interval(
    before: Sequence[float],
    after: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    level: float = DEFAULT_LEVEL,
    seed: int = DEFAULT_SEED,
) -> MetricInterval:
    """Bootstrap interval of the mean per-pair difference ``after - before``."""

    if len(before) != len(after):
        raise ValueError("paired scores need the same number of cases")
    return bootstrap_interval(
        [later - earlier for earlier, later in zip(before, after, strict=True)],
        resamples=resamples,
        level=level,
        seed=seed,
    )


def metric_intervals(cases: Iterable[CaseResult]) -> dict[str, MetricInterval]:
    """One interval per metric, over the same per-case scores its average uses."""

    scores: dict[str, list[float]] = {}
    for case in cases:
        for metric in case.metrics:
            scores.setdefault(metric.name, []).append(metric.score)
    return {name: bootstrap_interval(values) for name, values in sorted(scores.items())}


def with_intervals(summary: EvaluationSummary) -> EvaluationSummary:
    """The summary with intervals for its averages and for each split's.

    Slice and category groups get none: most of them hold one or two cases.
    """

    by_split = {
        name: group.model_copy(
            update={
                "intervals": metric_intervals(
                    case for case in summary.cases if case.split == name
                )
            }
        )
        for name, group in summary.by_split.items()
    }
    return summary.model_copy(
        update={"intervals": metric_intervals(summary.cases), "by_split": by_split}
    )


def _order_statistic(quantile: float, count: int) -> int:
    """The 0-based index of the ``quantile`` order statistic among ``count`` means."""
    # Rounding first keeps 0.975 * 2000 from landing on 1949.9999999.
    return min(count - 1, math.floor(round(quantile * count, 9)))


@lru_cache(maxsize=32)
def _resample_indices(count: int, resamples: int, seed: int) -> tuple[tuple[int, ...], ...]:
    rng = random.Random(seed)
    return tuple(
        tuple(int(rng.random() * count) for _ in range(count)) for _ in range(resamples)
    )
```

- [ ] **Step 5: Attach intervals in both evaluators and the coverage scope**

In `app/evaluation/consumer_runner.py`, import `with_intervals` from `app.evaluation.intervals` and in `ConsumerLegalRetrievalEvaluator.run` replace `return EvaluationSummary.from_cases(results, run=run)` with:

```python
        return with_intervals(EvaluationSummary.from_cases(results, run=run))
```

In `app/evaluation/consumer_notice.py`, import `with_intervals` and in `ConsumerNoticeGroundEvaluator.run` replace `return EvaluationSummary.from_cases(cases, run=run)` with:

```python
        return with_intervals(EvaluationSummary.from_cases(cases, run=run))
```

In `pyproject.toml`, `[tool.coverage.run] source`, add `"app.evaluation.intervals",` in alphabetical position.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluation_intervals.py tests/test_consumer_notice_evaluation.py tests/test_consumer_evaluation.py -q`
Expected: PASS.

Run: `.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q`
Expected: PASS with `app/evaluation/intervals.py` at 100%.

- [ ] **Step 7: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/evaluation/intervals.py app/schemas/evaluation.py app/evaluation/consumer_runner.py app/evaluation/consumer_notice.py pyproject.toml tests/test_evaluation_intervals.py tests/test_consumer_notice_evaluation.py tests/test_consumer_evaluation.py
git commit -m "feat(evaluation): bootstrap intervals for every averaged metric

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Compare two runs case by case

**Files:**
- Create: `app/evaluation/compare.py`
- Modify: `pyproject.toml` (coverage source)
- Test: `tests/test_evaluation_compare.py`

**Interfaces:**
- Consumes: `paired_difference_interval` (Task 4), `CaseResult.split`, `CASE_SPLITS` (Task 2).
- Produces: `compare_summaries(before, after, *, split=None) -> Comparison`, `render_comparison(comparison) -> str`, `load_summary(path) -> EvaluationSummary`, `main(argv) -> int`; CLI `python -m app.evaluation.compare BEFORE AFTER [--split NAME]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evaluation_compare.py`:

```python
"""Paired comparison of two evaluation runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.compare import compare_summaries, load_summary, main, render_comparison
from app.schemas.evaluation import (
    CaseResult,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricResult,
    RetrievalEvaluationConfiguration,
)

_SHA = "0" * 64


def _run(**overrides: object) -> EvaluationRunMetadata:
    payload: dict[str, object] = {
        "dataset_id": "seed",
        "dataset_version": "2.2.0",
        "dataset_sha256": _SHA,
        "dataset_review_status": "requires_legal_review",
        "corpus_release_id": "v4",
        "corpus_sha256": _SHA,
        "query_builder_version": "q7",
        "queries_per_case": 2,
        "cutoffs": (8,),
        "retrieval": RetrievalEvaluationConfiguration(retriever_id="r", requested_k=8),
        "agreement_max_rank": 13,
    }
    payload.update(overrides)
    return EvaluationRunMetadata.model_validate(payload)


def _case(name: str, split: str, recall: float, grounds: int, **extra: float) -> CaseResult:
    metrics = [MetricResult(name="recall", score=recall)]
    metrics += [MetricResult(name=metric, score=value) for metric, value in extra.items()]
    return CaseResult(case_name=name, split=split, metrics=metrics, counts={"grounds": grounds})


def _summary(cases: list[CaseResult], **run: object) -> EvaluationSummary:
    return EvaluationSummary.from_cases(cases, run=_run(**run))


def _write(path: Path, summary: EvaluationSummary) -> Path:
    path.write_text(summary.model_dump_json(), encoding="utf-8")
    return path


def test_runs_are_paired_case_by_case() -> None:
    before = _summary(
        [
            _case("a", "development", 0.0, 1),
            _case("b", "development", 0.5, 2),
            _case("gone", "holdout", 1.0, 0),
        ]
    )
    after = _summary(
        [
            _case("a", "development", 1.0, 2),
            _case("b", "development", 0.5, 3),
            _case("new", "holdout", 0.0, 0),
        ],
        agreement_max_rank=12,
    )

    comparison = compare_summaries(before, after)

    assert comparison.pairs == 2
    assert comparison.only_before == ("gone",)
    assert comparison.only_after == ("new",)
    [recall] = comparison.metrics
    assert (recall.name, recall.pairs, recall.before, recall.after) == ("recall", 2, 0.25, 0.75)
    assert recall.difference == 0.5
    assert recall.interval.low <= 0.5 <= recall.interval.high
    [grounds] = comparison.counts
    assert (grounds.before, grounds.after, grounds.difference) == (3, 5, 2)
    assert (grounds.low, grounds.high) == (2.0, 2.0)
    assert comparison.metadata_differences == (("agreement_max_rank", "13", "12"),)
    rendered = render_comparison(comparison)
    assert "only in BEFORE (excluded): gone" in rendered
    assert "only in AFTER (excluded): new" in rendered


def test_a_split_keeps_only_cases_in_that_split_in_both_runs() -> None:
    before = _summary([_case("a", "development", 0.0, 0), _case("moved", "development", 0.0, 0)])
    after = _summary([_case("a", "development", 1.0, 0), _case("moved", "holdout", 1.0, 0)])

    comparison = compare_summaries(before, after, split="development")

    assert comparison.pairs == 1
    assert comparison.only_before == ("moved",)


def test_metrics_one_run_never_reports_are_named() -> None:
    before = _summary([_case("a", "development", 0.0, 0)])
    after = _summary([_case("a", "development", 0.0, 0, precision=0.5)])

    comparison = compare_summaries(before, after)

    assert [item.name for item in comparison.metrics] == ["recall"]
    assert comparison.one_sided_metrics == ("precision",)
    assert "reported by one run only: precision" in render_comparison(comparison)


def test_runs_without_metadata_or_counts_compare_too() -> None:
    plain = EvaluationSummary.from_cases(
        [CaseResult(case_name="a", metrics=[MetricResult(name="recall", score=1.0)])]
    )

    comparison = compare_summaries(plain, plain)

    assert comparison.metadata_differences == ()
    assert comparison.counts == ()
    assert "count" not in render_comparison(comparison).split()
    with pytest.raises(ValueError, match="in the holdout split"):
        compare_summaries(plain, plain, split="holdout")


def test_runs_without_a_shared_case_cannot_be_compared() -> None:
    with pytest.raises(ValueError, match="share no case"):
        compare_summaries(
            _summary([_case("a", "development", 0.0, 0)]),
            _summary([_case("b", "development", 0.0, 0)]),
        )


def test_a_result_written_before_splits_existed_still_loads(tmp_path: Path) -> None:
    legacy = _summary([_case("a", "development", 0.5, 1)]).model_dump(mode="json")
    for case in legacy["cases"]:
        del case["split"]
    del legacy["by_split"], legacy["intervals"]
    legacy["run"].pop("case_split")
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    summary = load_summary(path)

    assert summary.cases[0].split is None
    assert compare_summaries(summary, summary).pairs == 1


def test_cli_prints_the_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _write(tmp_path / "before.json", _summary([_case("a", "development", 0.0, 1)]))
    after = _write(
        tmp_path / "after.json",
        _summary([_case("a", "development", 1.0, 2)], agreement_max_rank=12),
    )

    assert main([str(before), str(after)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "pairs: 1"
    assert "changed agreement_max_rank: 13 -> 12" in lines
    assert any(line.startswith("recall") and "+1.000" in line for line in lines)
    assert any(line.startswith("grounds") and "+1" in line for line in lines)


def test_cli_refuses_sweep_files_and_disjoint_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sweep = tmp_path / "sweep.json"
    sweep.write_text(json.dumps({"agreement_sweep": {}}), encoding="utf-8")
    one = _write(tmp_path / "one.json", _summary([_case("a", "development", 0.0, 0)]))
    other = _write(tmp_path / "other.json", _summary([_case("b", "development", 0.0, 0)]))

    assert main([str(sweep), str(one)]) == 2
    assert "sweep files are not supported" in capsys.readouterr().err
    assert main([str(one), str(other)]) == 2
    assert "share no case" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluation_compare.py -q`
Expected: FAIL — `app.evaluation.compare` does not exist.

- [ ] **Step 3: Create `app/evaluation/compare.py`**

```python
"""Paired comparison of two evaluation runs.

Two golden runs differ by a handful of cases, so their averages alone cannot
say whether a change helped. This tool pairs the runs case by case and reports,
for every metric and count, the mean difference with a paired-bootstrap
interval. It decides nothing: an interval that straddles zero is the honest
answer that the golden set cannot tell the runs apart.

    python -m app.evaluation.compare before.json after.json [--split development]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.evaluation.intervals import paired_difference_interval
from app.schemas.evaluation import (
    CASE_SPLITS,
    CaseResult,
    EvaluationRunMetadata,
    EvaluationSummary,
    MetricInterval,
)

_RUN_FIELDS = (
    "dataset_sha256",
    "case_split",
    "corpus_release_id",
    "query_builder_version",
    "ground_policy_version",
    "agreement_max_rank",
    "ground_verifier",
)
_RETRIEVAL_FIELDS = (
    "retrieval_mode",
    "embedding_model",
    "embedding_model_revision",
    "reranker_model",
)

_Pair = tuple[CaseResult, CaseResult]


@dataclass(frozen=True, slots=True)
class MetricComparison:
    name: str
    pairs: int
    before: float
    after: float
    interval: MetricInterval

    @property
    def difference(self) -> float:
        return round(self.after - self.before, 3)


@dataclass(frozen=True, slots=True)
class CountComparison:
    name: str
    pairs: int
    before: int
    after: int
    low: float
    high: float

    @property
    def difference(self) -> int:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class Comparison:
    pairs: int
    only_before: tuple[str, ...]
    only_after: tuple[str, ...]
    metrics: tuple[MetricComparison, ...]
    counts: tuple[CountComparison, ...]
    one_sided_metrics: tuple[str, ...]
    metadata_differences: tuple[tuple[str, str, str], ...]


def compare_summaries(
    before: EvaluationSummary, after: EvaluationSummary, *, split: str | None = None
) -> Comparison:
    """Pair two runs by case name, optionally within one split of both."""

    earlier = _cases(before, split)
    later = _cases(after, split)
    shared = sorted(earlier.keys() & later.keys())
    if not shared:
        scope = f" in the {split} split" if split else ""
        raise ValueError(f"the two runs share no case{scope}")
    pairs = [(earlier[name], later[name]) for name in shared]
    return Comparison(
        pairs=len(pairs),
        only_before=tuple(sorted(earlier.keys() - later.keys())),
        only_after=tuple(sorted(later.keys() - earlier.keys())),
        metrics=_metric_comparisons(pairs),
        counts=_count_comparisons(pairs),
        one_sided_metrics=_one_sided_metrics(pairs),
        metadata_differences=_metadata_differences(before.run, after.run),
    )


def render_comparison(comparison: Comparison) -> str:
    """A text report: pairing, changed run settings, then metric and count tables."""

    lines = [f"pairs: {comparison.pairs}"]
    if comparison.only_before:
        lines.append("only in BEFORE (excluded): " + ", ".join(comparison.only_before))
    if comparison.only_after:
        lines.append("only in AFTER (excluded): " + ", ".join(comparison.only_after))
    if comparison.one_sided_metrics:
        lines.append("reported by one run only: " + ", ".join(comparison.one_sided_metrics))
    lines.extend(
        f"changed {name}: {earlier} -> {later}"
        for name, earlier, later in comparison.metadata_differences
    )
    lines += ["", _header("metric")]
    lines.extend(
        f"{item.name[:44]:44} {item.pairs:>5} {item.before:>7.3f} {item.after:>7.3f} "
        f"{item.difference:>+7.3f}  [{item.interval.low:+.3f}, {item.interval.high:+.3f}]"
        for item in comparison.metrics
    )
    if comparison.counts:
        lines += ["", _header("count")]
        lines.extend(
            f"{item.name[:44]:44} {item.pairs:>5} {item.before:>7} {item.after:>7} "
            f"{item.difference:>+7}  [{item.low:+.1f}, {item.high:+.1f}]"
            for item in comparison.counts
        )
    return "\n".join(lines)


def load_summary(path: Path) -> EvaluationSummary:
    """Read one summary written by ``consumer_runner --output``."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "agreement_sweep" in payload:
        raise ValueError(f"{path} is not one evaluation summary; sweep files are not supported")
    return EvaluationSummary.model_validate(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two Consumer evaluation runs case by case."
    )
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--split", choices=CASE_SPLITS, help="compare only one split's cases")
    args = parser.parse_args(argv)
    try:
        comparison = compare_summaries(
            load_summary(args.before), load_summary(args.after), split=args.split
        )
    except ValueError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 2
    print(render_comparison(comparison))
    return 0


def _header(kind: str) -> str:
    return f"{kind:44} {'pairs':>5} {'before':>7} {'after':>7} {'diff':>7}  95% interval"


def _cases(summary: EvaluationSummary, split: str | None) -> dict[str, CaseResult]:
    return {
        case.case_name: case for case in summary.cases if split is None or case.split == split
    }


def _scores(case: CaseResult) -> dict[str, float]:
    return {metric.name: metric.score for metric in case.metrics}


def _metric_comparisons(pairs: list[_Pair]) -> tuple[MetricComparison, ...]:
    paired: dict[str, tuple[list[float], list[float]]] = {}
    for earlier, later in pairs:
        before_scores, after_scores = _scores(earlier), _scores(later)
        for name in before_scores.keys() & after_scores.keys():
            values = paired.setdefault(name, ([], []))
            values[0].append(before_scores[name])
            values[1].append(after_scores[name])
    return tuple(
        MetricComparison(
            name=name,
            pairs=len(before),
            before=round(sum(before) / len(before), 3),
            after=round(sum(after) / len(after), 3),
            interval=paired_difference_interval(before, after),
        )
        for name, (before, after) in sorted(paired.items())
    )


def _count_comparisons(pairs: list[_Pair]) -> tuple[CountComparison, ...]:
    names = sorted({name for pair in pairs for case in pair for name in case.counts})
    comparisons: list[CountComparison] = []
    for name in names:
        before = [earlier.counts.get(name, 0) for earlier, _ in pairs]
        after = [later.counts.get(name, 0) for _, later in pairs]
        # The interval of a total is the pair count times that of the mean.
        mean = paired_difference_interval(before, after)
        comparisons.append(
            CountComparison(
                name=name,
                pairs=len(pairs),
                before=sum(before),
                after=sum(after),
                low=round(mean.low * len(pairs), 1),
                high=round(mean.high * len(pairs), 1),
            )
        )
    return tuple(comparisons)


def _one_sided_metrics(pairs: list[_Pair]) -> tuple[str, ...]:
    """Metrics one run reports for the shared cases and the other never does."""
    before = {name for earlier, _ in pairs for name in _scores(earlier)}
    after = {name for _, later in pairs for name in _scores(later)}
    return tuple(sorted(before ^ after))


def _metadata_differences(
    before: EvaluationRunMetadata | None, after: EvaluationRunMetadata | None
) -> tuple[tuple[str, str, str], ...]:
    earlier, later = _metadata(before), _metadata(after)
    return tuple(
        (name, earlier[name], later[name]) for name in earlier if earlier[name] != later[name]
    )


def _metadata(run: EvaluationRunMetadata | None) -> dict[str, str]:
    if run is None:
        return {name: "-" for name in (*_RUN_FIELDS, *_RETRIEVAL_FIELDS)}
    values: dict[str, object] = {name: getattr(run, name) for name in _RUN_FIELDS}
    values.update({name: getattr(run.retrieval, name) for name in _RETRIEVAL_FIELDS})
    return {name: "-" if value is None else str(value) for name, value in values.items()}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

In `pyproject.toml`, add `"app.evaluation.compare",` to the coverage source.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluation_compare.py -q`
Expected: PASS.

Run: `.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q`
Expected: PASS with `app/evaluation/compare.py` at 100%.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/evaluation/compare.py pyproject.toml tests/test_evaluation_compare.py
git commit -m "feat(evaluation): compare two runs case by case with paired intervals

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: A degraded configured retrieval fails the case

**Files:**
- Modify: `app/evaluation/consumer_retrievers.py` (`DegradedRetrievalError`, `_LazyConsumerRetriever.__call__`)
- Test: `tests/test_consumer_evaluation.py`

**Interfaces:**
- Produces: `DegradedRetrievalError(RuntimeError)` in `app.evaluation.consumer_retrievers`.

- [ ] **Step 1: Write the failing test**

In `tests/test_consumer_evaluation.py`, add imports `from app.evaluation.consumer_retrievers import _LazyConsumerRetriever, offline_hybrid_retriever`, `from app.rag.embeddings import MockEmbeddingClient`, `from app.rag.pipeline import RagPipeline`, `from app.rag.vector_store import InMemoryVectorStore`, and:

```python
class _BrokenQueryEmbedder(MockEmbeddingClient):
    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("synthetic outage")


async def test_a_degraded_retrieval_fails_the_case_instead_of_scoring_lexical_only() -> None:
    retriever = _LazyConsumerRetriever(
        lambda _corpus: RagPipeline(_BrokenQueryEmbedder(), InMemoryVectorStore()),
        retriever_id="degraded_fixture",
    )
    dataset = ConsumerLegalGoldenDataset(
        dataset_id="degraded-fixture",
        version="1.0.0",
        description="Small deterministic evaluator fixture for degraded retrieval.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(_case(),),
    )

    summary = await ConsumerLegalRetrievalEvaluator(retriever).run(dataset)

    assert summary.failed_case_count == 1
    assert "retrieval degraded: lexical_only" in summary.cases[0].errors[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_evaluation.py -k degraded -q`
Expected: FAIL — the case scores as a completed retrieval.

- [ ] **Step 3: Implement**

In `app/evaluation/consumer_retrievers.py`, above `_LazyConsumerRetriever`:

```python
class DegradedRetrievalError(RuntimeError):
    """An evaluation query fell back from hybrid retrieval to one channel."""
```

and replace `_LazyConsumerRetriever.__call__` with:

```python
    async def __call__(self, query: str, k: int) -> list[RetrievedChunk]:
        pipeline, doc_id = await self._ready()
        results, trace = await pipeline.retrieve_with_trace(
            query, doc_id=doc_id, agent="direct", k=k, mode=RetrievalMode.HYBRID
        )
        if trace.degraded_mode:
            # Scored as hybrid, a lexical-only fallback would pass for the
            # configured stack's result; a failed case makes the run exit 2.
            raise DegradedRetrievalError(f"retrieval degraded: {trace.degraded_mode}")
        return results
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_evaluation.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/evaluation/consumer_retrievers.py tests/test_consumer_evaluation.py
git commit -m "fix(evaluation): fail a configured retrieval case that degraded to lexical-only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The golden query-vector cache and its embedder wrapper

**Files:**
- Create: `app/evaluation/query_vectors.py`
- Modify: `pyproject.toml` (coverage source)
- Test: `tests/test_query_vectors.py`

**Interfaces:**
- Produces, in `app.evaluation.query_vectors`: `CONTRACT_FILENAME`, `VECTORS_FILENAME`; `QueryVectorCacheMiss(missing: int)`; `cache_contract(configuration: Mapping[str, object]) -> dict[str, object] | None`; `contract_id(contract) -> str`; `QueryVectorCache(directory, contract)` with `open(root, contract)`, `directory`, `__len__`, `get(query) -> list[float] | None`, `add(queries, vectors) -> None`; `CachedQueryEmbedder(inner, *, require_cached=False)` with `cache`, `bind(cache)`, `model_name`, `embed`, `embed_documents`, `embed_query`, `embed_queries`, attribute forwarding.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_vectors.py`:

```python
"""Golden query vectors cached per embedding contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.query_vectors import (
    CONTRACT_FILENAME,
    VECTORS_FILENAME,
    CachedQueryEmbedder,
    QueryVectorCache,
    QueryVectorCacheMiss,
    cache_contract,
    contract_id,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.rag import Chunk

CONTRACT: dict[str, object] = {
    "model_repository": "mock-hashed-bow-v1:128",
    "model_revision": "rev-1",
    "output_dimension": 128,
    "normalization": "l2",
    "document_formatter_version": "plain",
    "query_formatter_version": "instruction-prefix-v2",
    "query_instruction_sha256": None,
}


class _CountingEmbedder(MockEmbeddingClient):
    def __init__(self) -> None:
        super().__init__(query_instruction="Consulta jurídica")
        self.query_batches: list[list[str]] = []

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_batches.append(list(texts))
        return await super().embed_queries(texts)


async def _vectors(*texts: str) -> list[list[float]]:
    return await MockEmbeddingClient().embed_queries(list(texts))


def test_only_a_pinned_contract_is_cached_and_its_id_is_stable() -> None:
    configuration = {**CONTRACT, "require_model_revision": True}

    assert cache_contract(configuration) == CONTRACT
    assert cache_contract({**configuration, "model_revision": None}) is None
    assert cache_contract({**configuration, "model_revision": "  "}) is None
    assert contract_id(CONTRACT) == contract_id(dict(reversed(list(CONTRACT.items()))))
    assert len(contract_id(CONTRACT)) == 16
    assert contract_id({**CONTRACT, "model_revision": "rev-2"}) != contract_id(CONTRACT)


async def test_vectors_survive_a_reopen_without_query_text(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    vectors = await _vectors("primeira consulta", "segunda consulta")
    cache.add(["primeira consulta", "segunda consulta"], vectors)

    reopened = QueryVectorCache.open(tmp_path, CONTRACT)

    assert len(reopened) == 2
    assert reopened.get("primeira consulta") == vectors[0]
    assert reopened.get("terceira") is None
    assert "primeira" not in (reopened.directory / VECTORS_FILENAME).read_text(encoding="utf-8")
    contract_file = reopened.directory / CONTRACT_FILENAME
    assert json.loads(contract_file.read_text(encoding="utf-8")) == CONTRACT


async def test_a_partial_last_line_is_dropped_and_the_file_repaired(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["inteira"], await _vectors("inteira"))
    path = cache.directory / VECTORS_FILENAME
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"query_sha256": "abc", "vector": [0.1, 0.')

    repaired = QueryVectorCache.open(tmp_path, CONTRACT)
    repaired.add(["depois"], await _vectors("depois"))

    assert len(QueryVectorCache.open(tmp_path, CONTRACT)) == 2
    assert path.read_bytes().endswith(b"\n")


async def test_a_whole_unterminated_line_is_kept(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["unica"], await _vectors("unica"))
    path = cache.directory / VECTORS_FILENAME
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    assert QueryVectorCache.open(tmp_path, CONTRACT).get("unica") is not None
    assert path.read_bytes().endswith(b"\n")


async def test_windows_line_endings_and_blank_lines_load(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["a", "b"], await _vectors("a", "b"))
    path = cache.directory / VECTORS_FILENAME
    path.write_bytes(b"\r\n" + path.read_bytes().replace(b"\n", b"\r\n"))

    assert len(QueryVectorCache.open(tmp_path, CONTRACT)) == 2


async def test_corruption_and_a_foreign_contract_are_refused(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)
    cache.add(["a"], await _vectors("a"))
    path = cache.directory / VECTORS_FILENAME
    good = path.read_bytes()

    path.write_bytes(b"not json\n" + good)
    with pytest.raises(ValueError, match=":1 is not a cached query vector"):
        QueryVectorCache.open(tmp_path, CONTRACT)
    short_key = json.dumps({"query_sha256": "abc", "vector": [1.0] + [0.0] * 127})
    path.write_bytes(short_key.encode("utf-8") + b"\n" + good)
    with pytest.raises(ValueError, match=":1 is not a cached query vector"):
        QueryVectorCache.open(tmp_path, CONTRACT)
    with pytest.raises(ValueError, match="different embedding contract"):
        QueryVectorCache(cache.directory, {**CONTRACT, "model_revision": "rev-2"})


async def test_vectors_are_validated(tmp_path: Path) -> None:
    cache = QueryVectorCache.open(tmp_path, CONTRACT)

    with pytest.raises(ValueError, match="dimension"):
        cache.add(["curto"], [[1.0]])
    with pytest.raises(ValueError, match="normalized"):
        cache.add(["longo"], [[1.0] * 128])
    stored = json.dumps({"query_sha256": "f" * 64, "vector": [1.0] * 128})
    (cache.directory / VECTORS_FILENAME).write_text(stored + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="normalized"):
        QueryVectorCache.open(tmp_path, CONTRACT)
    free = QueryVectorCache.open(tmp_path / "free", {**CONTRACT, "output_dimension": None})
    free.add(["x"], await _vectors("x"))
    assert len(free) == 1


async def test_the_wrapper_embeds_each_missing_query_once(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))

    first = await embedder.embed_queries(["a", "a", "b"])
    second = await embedder.embed_queries(["b", "a"])

    assert inner.query_batches == [["a", "b"]]
    assert first[0] == first[1] == second[1]
    assert second[0] == first[2]
    assert await embedder.embed_query("a") == first[0]


async def test_a_strict_wrapper_never_calls_the_model(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner, require_cached=True)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))

    with pytest.raises(QueryVectorCacheMiss, match="2 golden query vector"):
        await embedder.embed_queries(["a", "b", "a"])
    assert inner.query_batches == []


async def test_an_unbound_wrapper_passes_through_and_documents_always_do() -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)

    await embedder.embed_queries(["a"])

    assert embedder.cache is None
    assert inner.query_batches == [["a"]]
    assert await embedder.embed(["texto"]) == await inner.embed_documents(["texto"])


async def test_a_pipeline_on_the_wrapper_records_the_real_embedder(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))
    pipeline = RagPipeline(embedder, InMemoryVectorStore())
    await pipeline.index_chunks(
        [
            Chunk(
                chunk_id="d:0",
                doc_id="d",
                text="cobrança indevida na fatura",
                page_start=1,
                page_end=1,
            )
        ]
    )

    _, trace = await pipeline.retrieve_with_trace(
        "cobrança indevida", doc_id="d", agent="test", k=1
    )

    assert trace.embedding_model == inner.model_name
    assert trace.embedding_query_instruction == "Consulta jurídica"
    configuration = pipeline.embedding_contract_configuration()
    assert configuration["query_formatter_version"] == inner.query_format_version
    with pytest.raises(AttributeError):
        _ = embedder.__missing_dunder__
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_query_vectors.py -q`
Expected: FAIL — `app.evaluation.query_vectors` does not exist.

- [ ] **Step 3: Create `app/evaluation/query_vectors.py`**

```python
"""Golden query vectors cached per embedding contract, for evaluation only.

A configured evaluation embeds the same deterministic golden queries on every
run, and on a CPU the 4B JUÁ model cannot embed all of them in one process.
This cache stores each query's vector once, keyed by the SHA-256 of the query
text, in a directory named after the embedding contract, so later runs read
vectors instead of loading the model.

Only evaluation uses it. The production query path never persists a query:
queries there are consumers' complaints.

Layout: ``<data_dir>/evaluation/query_vectors/<contract_id>/`` holds
``contract.json`` and an append-only ``vectors.jsonl``. No query text is
stored. A crash can leave a partial last line; the next load drops it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.consumer.embedding_artifacts import atomic_write
from app.core.hashing import canonical_json_sha256, sha256_hex
from app.rag.embeddings import (
    EmbeddingBackend,
    embed_document_texts,
    embed_query_texts,
    validate_embedding_vectors,
)

CONTRACT_FILENAME = "contract.json"
VECTORS_FILENAME = "vectors.jsonl"
# Recorded in a pipeline's contract but irrelevant to the vectors themselves.
_NON_VECTOR_FIELDS = frozenset({"require_model_revision"})
_SHA256_HEX_LENGTH = 64


class QueryVectorCacheMiss(RuntimeError):
    """Golden queries that are not cached when the model may not be loaded."""

    def __init__(self, missing: int) -> None:
        self.missing = missing
        super().__init__(
            f"{missing} golden query vector(s) are not cached; "
            "fill the cache with `python -m app.evaluation.query_vectors`"
        )


def cache_contract(configuration: Mapping[str, object]) -> dict[str, object] | None:
    """The vector-determining part of an embedding contract; None when unpinned.

    A model without an exact revision can change under the same name, so its
    vectors are never cached.
    """

    if not str(configuration.get("model_revision") or "").strip():
        return None
    return {
        key: value
        for key, value in sorted(configuration.items())
        if key not in _NON_VECTOR_FIELDS
    }


def contract_id(contract: Mapping[str, object]) -> str:
    """The cache directory name for one contract."""
    return canonical_json_sha256(dict(contract))[:16]


class QueryVectorCache:
    """Vectors of golden queries for one embedding contract, as JSON lines."""

    def __init__(self, directory: Path, contract: Mapping[str, object]) -> None:
        self._directory = directory
        self._contract = dict(contract)
        dimension = self._contract.get("output_dimension")
        self._dimension = int(str(dimension)) if dimension is not None else None
        directory.mkdir(parents=True, exist_ok=True)
        self._check_contract()
        self._vectors = self._load()

    @classmethod
    def open(cls, root: Path, contract: Mapping[str, object]) -> QueryVectorCache:
        return cls(root / contract_id(contract), contract)

    @property
    def directory(self) -> Path:
        return self._directory

    def __len__(self) -> int:
        return len(self._vectors)

    def get(self, query: str) -> list[float] | None:
        vector = self._vectors.get(sha256_hex(query))
        return list(vector) if vector is not None else None

    def add(self, queries: Sequence[str], vectors: Sequence[list[float]]) -> None:
        """Persist new vectors; a query already cached keeps its first vector."""

        validate_embedding_vectors(
            list(vectors), expected_count=len(queries), expected_dimension=self._dimension
        )
        new: dict[str, list[float]] = {}
        for query, vector in zip(queries, vectors, strict=True):
            key = sha256_hex(query)
            if key not in self._vectors and key not in new:
                new[key] = [float(value) for value in vector]
        if not new:
            return
        path = self._directory / VECTORS_FILENAME
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(_record(key, vector) for key, vector in new.items()))
            handle.flush()
            os.fsync(handle.fileno())
        self._vectors.update(new)

    def _check_contract(self) -> None:
        path = self._directory / CONTRACT_FILENAME
        if not path.exists():
            rendered = json.dumps(self._contract, indent=2, sort_keys=True) + "\n"
            atomic_write(path, rendered.encode("utf-8"))
            return
        if json.loads(path.read_text(encoding="utf-8")) != self._contract:
            raise ValueError(f"{path} records a different embedding contract")

    def _load(self) -> dict[str, list[float]]:
        path = self._directory / VECTORS_FILENAME
        if not path.exists():
            return {}
        lines = path.read_text(encoding="utf-8").split("\n")
        tail = lines.pop()  # empty when the file ends with a newline
        vectors: dict[str, list[float]] = {}
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            parsed = self._parse(line)
            if parsed is None:
                raise ValueError(f"{path}:{number} is not a cached query vector")
            vectors.setdefault(parsed[0], parsed[1])
        if tail.strip():
            # An unterminated last line is what a crash mid-append leaves. It is
            # kept only if whole, and the file is rewritten so the next append
            # starts on a fresh line.
            parsed = self._parse(tail)
            if parsed is not None:
                vectors.setdefault(parsed[0], parsed[1])
            rewritten = "".join(_record(key, vector) for key, vector in vectors.items())
            atomic_write(path, rewritten.encode("utf-8"))
        return vectors

    def _parse(self, line: str) -> tuple[str, list[float]] | None:
        try:
            record = json.loads(line)
            key = str(record["query_sha256"])
            vector = [float(value) for value in record["vector"]]
        except (ValueError, TypeError, KeyError):
            return None
        if len(key) != _SHA256_HEX_LENGTH:
            return None
        validate_embedding_vectors([vector], expected_count=1, expected_dimension=self._dimension)
        return key, vector


class CachedQueryEmbedder:
    """An embedding backend that answers queries from a bound QueryVectorCache.

    Unbound, it passes every call through: a model without a pinned revision is
    never cached. Every attribute it does not define is read from the real
    embedder, so a pipeline built on it records the same model, revision,
    instruction and formatter versions as one built without it.
    """

    def __init__(self, inner: EmbeddingBackend, *, require_cached: bool = False) -> None:
        self._inner = inner
        self._require_cached = require_cached
        self._cache: QueryVectorCache | None = None

    @property
    def model_name(self) -> str:
        return str(getattr(self._inner, "model_name", type(self._inner).__name__))

    @property
    def cache(self) -> QueryVectorCache | None:
        return self._cache

    def bind(self, cache: QueryVectorCache) -> None:
        self._cache = cache

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.__dict__["_inner"], name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_documents(texts)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await embed_document_texts(self._inner, texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        cache = self._cache
        if cache is None:
            return await embed_query_texts(self._inner, texts)
        missing = list(dict.fromkeys(text for text in texts if cache.get(text) is None))
        if missing:
            if self._require_cached:
                raise QueryVectorCacheMiss(len(missing))
            cache.add(missing, await embed_query_texts(self._inner, missing))
        vectors = [cache.get(text) for text in texts]
        return [vector for vector in vectors if vector is not None]


def _record(key: str, vector: list[float]) -> str:
    return json.dumps({"query_sha256": key, "vector": vector}, separators=(",", ":")) + "\n"
```

In `pyproject.toml`, add `"app.evaluation.query_vectors",` to the coverage source.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_query_vectors.py -q`
Expected: PASS.

Run: `.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q`
Expected: PASS with `app/evaluation/query_vectors.py` at 100%.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/evaluation/query_vectors.py pyproject.toml tests/test_query_vectors.py
git commit -m "feat(evaluation): cache golden query vectors per embedding contract

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Configured runs read through the cache; the cache-filling CLI

**Files:**
- Modify: `app/evaluation/query_vectors.py` (`query_vector_root`, `golden_queries`, `CaseCoverage`, `fill_query_vectors`, `_configured_embedder`, `_cli`)
- Modify: `app/evaluation/consumer_retrievers.py` (`configure_query_vectors`, `cached_configured_pipeline`, `configured_pipeline`)
- Modify: `app/evaluation/consumer_runner.py` (`--require-cached-queries` in `_prepare`)
- Modify: `app/evaluation/label_ranks.py` (`--require-cached-queries`)
- Modify: `.gitignore`
- Test: `tests/test_query_vectors.py`, `tests/test_consumer_evaluation.py`, `tests/test_label_ranks.py`

**Interfaces:**
- Consumes: `CachedQueryEmbedder`, `QueryVectorCache`, `cache_contract` (Task 7); `dataset_split`, `_prepare` (Task 2).
- Produces: `query_vector_root(settings) -> Path`; `golden_queries(dataset) -> dict[str, list[str]]`; `CaseCoverage(case_id, cached, embedded)`; `fill_query_vectors(embedder, queries_by_case, *, embed=True) -> list[CaseCoverage]`; `consumer_retrievers.configure_query_vectors(*, require_cached: bool)`; `consumer_retrievers.cached_configured_pipeline(corpus, settings=None) -> tuple[RagPipeline, CachedQueryEmbedder]`; `configured_pipeline(corpus, settings=None) -> RagPipeline`.
- Spec deviation (deliberate): spec §5.6 says an unpinned model gets "the plain pipeline". Here it gets the same pipeline with an *unbound* wrapper, which forwards every call and attribute to the real embedder. The behaviour is identical and the stack is not built twice.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_vectors.py` (add imports `from types import SimpleNamespace`, `from app.consumer.legal_corpus import get_default_legal_corpus`, `from app.core.config import Settings`, `from app.evaluation import consumer_retrievers, query_vectors`, `from app.evaluation.consumer_golden import load_consumer_legal_dataset`, `from app.evaluation.query_vectors import CaseCoverage, fill_query_vectors, golden_queries`):

```python
DATASET_PATH = Path("eval_data/consumer_legal_retrieval")


class _PinnedEmbedder(MockEmbeddingClient):
    model_revision = "rev-1"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="mock",
        embedding_provider="mock",
        embedding_model_revision=None,
        embedding_expected_dimensions=None,
        embedding_require_model_revision=False,
        vector_store="memory",
        data_dir=tmp_path,
    )


def test_golden_queries_are_the_production_queries_of_in_scope_cases() -> None:
    queries = golden_queries(load_consumer_legal_dataset(DATASET_PATH))

    assert len(queries) == 39
    assert "salario_atrasado" not in queries
    assert sum(len(texts) for texts in queries.values()) == 78


async def test_filling_resumes_and_reports_coverage(tmp_path: Path) -> None:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))
    queries = {"um": ["q1", "q2"], "dois": ["q2", "q3"]}

    checked = await fill_query_vectors(embedder, queries, embed=False)
    first = await fill_query_vectors(embedder, queries)
    second = await fill_query_vectors(embedder, queries)

    assert checked == [CaseCoverage("um", 0, 0), CaseCoverage("dois", 0, 0)]
    assert first == [CaseCoverage("um", 0, 2), CaseCoverage("dois", 1, 1)]
    assert second == [CaseCoverage("um", 2, 0), CaseCoverage("dois", 2, 0)]
    assert inner.query_batches == [["q1", "q2"], ["q3"]]
    with pytest.raises(ValueError, match="no pinned revision"):
        await fill_query_vectors(CachedQueryEmbedder(inner), queries)


def test_a_pinned_configured_stack_reads_the_golden_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumer_retrievers, "_require_cached_queries", False)
    monkeypatch.setattr(
        consumer_retrievers, "create_embedding_client", lambda _settings: _PinnedEmbedder()
    )

    pipeline, embedder = consumer_retrievers.cached_configured_pipeline(
        get_default_legal_corpus(), _settings(tmp_path)
    )
    try:
        assert embedder.cache is not None
        assert embedder.cache.directory.parent == tmp_path / "evaluation" / "query_vectors"
        configuration = pipeline.retrieval_configuration(requested_k=8)
        assert configuration["embedding_model_revision"] == "rev-1"
    finally:
        pipeline.close()


def test_an_unpinned_configured_stack_is_not_cached_unless_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumer_retrievers, "_require_cached_queries", False)
    corpus = get_default_legal_corpus()

    pipeline, embedder = consumer_retrievers.cached_configured_pipeline(
        corpus, _settings(tmp_path)
    )
    pipeline.close()
    plain = consumer_retrievers.configured_pipeline(corpus, _settings(tmp_path))
    plain.close()
    consumer_retrievers.configure_query_vectors(require_cached=True)

    assert embedder.cache is None
    with pytest.raises(ValueError, match="pinned model revision"):
        consumer_retrievers.cached_configured_pipeline(corpus, _settings(tmp_path))


def _patch_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _CountingEmbedder:
    inner = _CountingEmbedder()
    embedder = CachedQueryEmbedder(inner)
    embedder.bind(QueryVectorCache.open(tmp_path, CONTRACT))
    monkeypatch.setattr(
        query_vectors,
        "_configured_embedder",
        lambda: (embedder, SimpleNamespace(close=lambda: None)),
    )
    return inner


def _report(printed: str) -> list[str]:
    return [line for line in printed.splitlines() if "embedded" in line or "coverage:" in line]


async def test_cli_fills_one_case_then_checks_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inner = _patch_configured(monkeypatch, tmp_path)
    case = ["--case", "produto_duravel_com_vicio"]

    assert await query_vectors._cli([str(DATASET_PATH), *case]) == 0
    assert await query_vectors._cli([str(DATASET_PATH), "--check", *case]) == 0
    assert await query_vectors._cli([str(DATASET_PATH), "--check"]) == 1

    lines = _report(capsys.readouterr().out)
    assert lines[0] == "produto_duravel_com_vicio: 0 cached, 2 embedded"
    assert lines[2] == "produto_duravel_com_vicio: 2 cached, 0 embedded"
    assert lines[-1].startswith("coverage: 2/78 golden queries cached in ")
    assert len(inner.query_batches) == 1


async def test_cli_rejects_an_unknown_case_and_an_unpinned_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_configured(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        await query_vectors._cli([str(DATASET_PATH), "--case", "salario_atrasado"])
    assert excinfo.value.code == 2

    monkeypatch.setattr(
        query_vectors,
        "_configured_embedder",
        lambda: (CachedQueryEmbedder(_CountingEmbedder()), SimpleNamespace(close=lambda: None)),
    )
    assert await query_vectors._cli([str(DATASET_PATH)]) == 2
    assert "no pinned revision" in capsys.readouterr().err


def test_the_cli_builds_the_configured_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, embedder = object(), object()
    monkeypatch.setattr(
        consumer_retrievers, "cached_configured_pipeline", lambda _corpus: (pipeline, embedder)
    )

    assert query_vectors._configured_embedder() == (embedder, pipeline)
```

In `tests/test_consumer_evaluation.py`, add `from app.evaluation import consumer_retrievers` and:

```python
def test_cli_can_forbid_loading_the_embedding_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumer_retrievers, "_require_cached_queries", False)
    dataset_path = _write_dataset(tmp_path / "dataset.json", _split_dataset())
    monkeypatch.setattr(
        sys,
        "argv",
        ["consumer_runner", str(dataset_path), "--empty-baseline", "--require-cached-queries"],
    )

    asyncio.run(consumer_runner._cli())

    assert consumer_retrievers._require_cached_queries is True
```

In `tests/test_label_ranks.py`, add `from app.evaluation import consumer_retrievers`, give `test_cli_ranks_one_split` a `monkeypatch: pytest.MonkeyPatch` parameter, put `monkeypatch.setattr(consumer_retrievers, "_require_cached_queries", False)` as its first line, add `"--require-cached-queries"` to its `_cli` arguments, and append:

```python
    assert consumer_retrievers._require_cached_queries is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_query_vectors.py tests/test_consumer_evaluation.py tests/test_label_ranks.py -q`
Expected: FAIL — `golden_queries`, `cached_configured_pipeline` and the flag do not exist.

- [ ] **Step 3: Add the builder to `app/evaluation/query_vectors.py`**

Add imports:

```python
import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.retrieval import build_legal_queries, is_consumer_scope
from app.consumer.schemas import ConsumerCaseFacts
from app.core.config import Settings
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.schemas.evaluation import ConsumerLegalGoldenDataset

if TYPE_CHECKING:
    from app.rag.pipeline import RagPipeline
```

(`Sequence` and `Mapping` are already imported from `collections.abc`; `asyncio` is used by the `__main__` block.)

Below the constants:

```python
QUERY_VECTOR_DIRECTORY = Path("evaluation") / "query_vectors"


def query_vector_root(settings: Settings) -> Path:
    """Where the configured evaluation keeps its query-vector caches."""
    return settings.data_dir / QUERY_VECTOR_DIRECTORY
```

At the end of the module, before `_record`:

```python
@dataclass(frozen=True, slots=True)
class CaseCoverage:
    """How many of one case's queries were already cached and how many were embedded."""

    case_id: str
    cached: int
    embedded: int


def golden_queries(dataset: ConsumerLegalGoldenDataset) -> dict[str, list[str]]:
    """The production ranking queries of every case the scope gate lets through."""

    return {
        case.case_id: build_legal_queries(
            ConsumerCaseFacts(
                complaint_summary=case.complaint, desired_resolution=case.desired_resolution
            )
        )
        for case in dataset.cases
        if is_consumer_scope(complaint=case.complaint)
    }


async def fill_query_vectors(
    embedder: CachedQueryEmbedder,
    queries_by_case: Mapping[str, list[str]],
    *,
    embed: bool = True,
) -> list[CaseCoverage]:
    """Embed each case's missing queries in one call, persisting before the next case.

    The embedder is called directly, not through the pipeline's query guard, so
    a slow CPU model cannot time out into a lexical fallback. With
    ``embed=False`` nothing is embedded and the result only reports coverage.
    """

    cache = embedder.cache
    if cache is None:
        raise ValueError("the configured embedding model has no pinned revision; nothing is cached")
    coverage: list[CaseCoverage] = []
    for case_id, queries in queries_by_case.items():
        cached = sum(cache.get(query) is not None for query in queries)
        embedded = len(queries) - cached if embed else 0
        if embedded:
            await embedder.embed_queries(queries)
        coverage.append(CaseCoverage(case_id, cached, embedded))
    return coverage


def _configured_embedder() -> tuple[CachedQueryEmbedder, RagPipeline]:
    from app.evaluation.consumer_retrievers import cached_configured_pipeline

    pipeline, embedder = cached_configured_pipeline(get_default_legal_corpus())
    return embedder, pipeline


async def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fill the golden query-vector cache for the configured embedding model, "
            "one case at a time."
        )
    )
    parser.add_argument("dataset", nargs="?", default="eval_data/consumer_legal_retrieval")
    parser.add_argument(
        "--case", action="append", default=[], metavar="CASE_ID", help="only this case (repeatable)"
    )
    parser.add_argument(
        "--check", action="store_true", help="embed nothing; exit 1 if any query is missing"
    )
    args = parser.parse_args(argv)
    queries = golden_queries(load_consumer_legal_dataset(Path(args.dataset)))
    if args.case:
        unknown = sorted(set(args.case) - queries.keys())
        if unknown:
            parser.error("not an in-scope golden case: " + ", ".join(unknown))
        queries = {case_id: queries[case_id] for case_id in args.case}
    embedder, pipeline = _configured_embedder()
    try:
        cache = embedder.cache
        if cache is None:
            print(
                "query_vectors: the configured embedding model has no pinned revision; "
                "nothing is cached",
                file=sys.stderr,
            )
            return 2
        coverage = await fill_query_vectors(embedder, queries, embed=not args.check)
    finally:
        pipeline.close()
    for item in coverage:
        print(f"{item.case_id}: {item.cached} cached, {item.embedded} embedded")
    total = sum(len(texts) for texts in queries.values())
    covered = sum(item.cached + item.embedded for item in coverage)
    print(f"coverage: {covered}/{total} golden queries cached in {cache.directory}")
    return 1 if covered < total else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_cli()))
```

Wrap any line ruff reports over 100 characters.

- [ ] **Step 4: Wire `configured_pipeline` through the cache**

In `app/evaluation/consumer_retrievers.py`, add imports:

```python
from app.evaluation.query_vectors import (
    CachedQueryEmbedder,
    QueryVectorCache,
    cache_contract,
    query_vector_root,
)
from app.rag.factory import create_embedding_client, create_rag_pipeline
```

(replacing the existing `create_rag_pipeline` import), and replace `configured_pipeline` with:

```python
_require_cached_queries = False


def configure_query_vectors(*, require_cached: bool) -> None:
    """Whether configured evaluations may load the model for uncached queries.

    The default reads through the golden query-vector cache and embeds what is
    missing. ``require_cached`` turns a miss into an error, so a run is
    guaranteed never to load the embedding model.
    """
    global _require_cached_queries
    _require_cached_queries = require_cached


def cached_configured_pipeline(
    corpus: LegalCorpus, settings: Settings | None = None
) -> tuple[RagPipeline, CachedQueryEmbedder]:
    """The configured stack, its query embedder reading the golden cache."""

    effective = settings or Settings()
    embedder = CachedQueryEmbedder(
        create_embedding_client(effective), require_cached=_require_cached_queries
    )
    pipeline = create_rag_pipeline(
        effective,
        corpus_version=f"{corpus.release_id}-{corpus.corpus_sha256[:12]}",
        embedder=embedder,
    )
    contract = cache_contract(pipeline.embedding_contract_configuration())
    if contract is not None:
        embedder.bind(QueryVectorCache.open(query_vector_root(effective), contract))
    elif _require_cached_queries:
        pipeline.close()
        raise ValueError(
            "golden query vectors are cached only for a pinned model revision; "
            "set LITIGATION_EMBEDDING_MODEL_REVISION"
        )
    return pipeline, embedder


def configured_pipeline(corpus: LegalCorpus, settings: Settings | None = None) -> RagPipeline:
    """Build the provider selected by LITIGATION_EMBEDDING_* settings."""

    pipeline, _ = cached_configured_pipeline(corpus, settings)
    return pipeline
```

- [ ] **Step 5: Add `--require-cached-queries` to both CLIs, and gitignore the cache**

In `app/evaluation/consumer_runner.py`, add to `_cli` before `parse_args`:

```python
    parser.add_argument(
        "--require-cached-queries",
        action="store_true",
        help=(
            "configured stacks only: fail on a golden query whose vector is not cached "
            "instead of loading the embedding model"
        ),
    )
```

and make the first lines of `_prepare`:

```python
    if args.require_cached_queries:
        from app.evaluation.consumer_retrievers import configure_query_vectors

        configure_query_vectors(require_cached=True)
```

In `app/evaluation/label_ranks.py`, import `configure_query_vectors` alongside `configured_pipeline`, add the same argument to `_cli`, and after `args = parser.parse_args(argv)`:

```python
    if args.require_cached_queries:
        configure_query_vectors(require_cached=True)
```

In `.gitignore`, below `data/embedding_generations/`, add:

```
data/evaluation/
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_query_vectors.py tests/test_consumer_evaluation.py tests/test_label_ranks.py -q`
Expected: PASS.

Run: `.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q`
Expected: PASS with `app/evaluation/query_vectors.py` at 100%.

- [ ] **Step 7: Lint, type-check, check imports and dead code, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
.venv/Scripts/lint-imports && .venv/Scripts/vulture app --min-confidence 90
git add app/evaluation/query_vectors.py app/evaluation/consumer_retrievers.py app/evaluation/consumer_runner.py app/evaluation/label_ranks.py .gitignore tests/test_query_vectors.py tests/test_consumer_evaluation.py tests/test_label_ranks.py
git commit -m "feat(evaluation): read configured runs through the golden query-vector cache

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: CI gates on development, a holdout report, ADR 0021 and the READMEs

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `docs/adr/0021-evaluation-precision-holdout-uncertainty.md`
- Modify: `docs/architecture.md` (ADR index), `README.md`, `README-pt.md`

**Interfaces:**
- Consumes: `--split`, `consumer_notice_labelled_grounds` (Tasks 1–3), the compare and cache CLIs (Tasks 5, 8).

- [ ] **Step 1: Re-run the exact CI commands locally and confirm the baselines**

```bash
.venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --split development --output dev-retrieval.json --min consumer_retrieval_success@5=1.0 --min consumer_recall@5=0.14 --min consumer_article_recall@5=0.22 --min consumer_ndcg@5=0.07 --min consumer_abstention@5=1.0 --max consumer_hard_negative_rate@5=0.03 --max consumer_inactive_provision_rate@5=0.0 --max consumer_unknown_status_rate@5=0.0
.venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --split development --output dev-notice.json --max consumer_notice_known_bad_citations=0 --min consumer_notice_abstention=1.0 --min consumer_notice_labelled_grounds=1
.venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --split holdout --output holdout-retrieval.json
.venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --split holdout --output holdout-notice.json
.venv/Scripts/python.exe -m app.evaluation.compare dev-notice.json dev-notice.json
```

Expected: every command exits 0; the development retrieval averages are recall@5 0.142, article recall@5 0.225, nDCG@5 0.075, hard-negative rate@5 0.025; the development notice totals are 17 grounds, 1 labelled, 16 unlabelled, 0 known-bad. Delete the five JSON files afterwards (they are not committed). If any number differs from the Global Constraints table, stop and report it instead of adjusting a gate.

- [ ] **Step 2: Update `.github/workflows/ci.yml`**

Retrieval gates: append to the step's comment block:

```yaml
        #
        # Re-baselined on 2026-10-02 for the development split of dataset 2.2.0
        # (the 22 original cases plus the traffic-fine and private-loan cases).
        # The 19 holdout cases are reported by their own step and never gated, so
        # no parameter is tuned against them. Development: recall@5 0.142,
        # article recall@5 0.225, nDCG@5 0.075, hard-negative rate@5 0.025.
```

and replace its command with:

```yaml
        run: |
          python -m app.evaluation.consumer_runner \
            eval_data/consumer_legal_retrieval \
            --split development \
            --output consumer-legal-retrieval-results.json \
            --min consumer_retrieval_success@5=1.0 \
            --min consumer_recall@5=0.14 \
            --min consumer_article_recall@5=0.22 \
            --min consumer_ndcg@5=0.07 \
            --min consumer_abstention@5=1.0 \
            --max consumer_hard_negative_rate@5=0.03 \
            --max consumer_inactive_provision_rate@5=0.0 \
            --max consumer_unknown_status_rate@5=0.0
```

Notice gates: append to the comment block:

```yaml
        #
        # Re-measured on 2026-10-02 for the development split of dataset 2.2.0:
        # 17 grounds, 1 labelled, 16 unlabelled, known-bad 0, abstention 1.0.
        # consumer_notice_labelled_grounds >= 1 fails a selector that cites
        # nothing, which the known-bad and abstention gates alone would pass.
```

and replace its command with:

```yaml
        run: |
          python -m app.evaluation.consumer_runner \
            eval_data/consumer_legal_retrieval \
            --evaluate-notice \
            --split development \
            --output consumer-notice-results.json \
            --max consumer_notice_known_bad_citations=0 \
            --min consumer_notice_abstention=1.0 \
            --min consumer_notice_labelled_grounds=1
```

Add a step after the notice gates:

```yaml
      - name: Consumer holdout report (not gated)
        # The holdout split is measured on every build and never gated, so no
        # parameter is tuned against it. Its results are uploaded below.
        run: |
          python -m app.evaluation.consumer_runner \
            eval_data/consumer_legal_retrieval \
            --split holdout \
            --output consumer-legal-retrieval-holdout.json
          python -m app.evaluation.consumer_runner \
            eval_data/consumer_legal_retrieval \
            --evaluate-notice \
            --split holdout \
            --output consumer-notice-holdout.json
```

and add both files to the upload step's `path`:

```yaml
          path: |
            consumer-legal-retrieval-results.json
            consumer-notice-results.json
            consumer-legal-retrieval-holdout.json
            consumer-notice-holdout.json
```

- [ ] **Step 3: Write ADR 0021**

Create `docs/adr/0021-evaluation-precision-holdout-uncertainty.md`:

```markdown
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
   resumes after a crash; `--require-cached-queries` forbids loading the model.
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
```

In `docs/architecture.md`, append to the ADR index:

```markdown
- [0021](adr/0021-evaluation-precision-holdout-uncertainty.md) — precision, a holdout and uncertainty in evaluation
```

- [ ] **Step 4: Update the READMEs**

In `README.md`, section "Evaluation and verification", after the paragraph that ends "still requires independent Brazilian legal review.", add:

````markdown
The golden set has a development and a holdout split (dataset 2.2.0). Tune on
development; the holdout is reported on every build and never gated, and a
holdout case that informed a decision moves to development in the next
version. `--split development|holdout` evaluates one split, and every summary
reports `by_split` and 95% bootstrap `intervals`.

The notice evaluation counts every cited ground once, as known-bad (a labelled
hard negative), labelled (an article the case labels) or unlabelled, and
reports `consumer_notice_precision` (labelled ÷ cited, only for cases that
cite something) and `consumer_notice_article_recall` beside exact recall.

Compare two runs case by case, with paired-bootstrap intervals:

```bash
python -m app.evaluation.compare before.json after.json --split development
```

The configured stack reads golden query vectors from a cache keyed by the
embedding contract under `data/evaluation/query_vectors/`. Fill it once, one
case at a time (an interrupted run resumes), then evaluate without loading
the model:

```bash
python -m app.evaluation.query_vectors                 # or --case CASE_ID, repeatable
python -m app.evaluation.query_vectors --check
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured \
  --require-cached-queries --output configured-notice.json
```

A configured retrieval run that degrades to lexical-only fails the case and
exits 2 instead of scoring it as hybrid.
````

In `README-pt.md`, in the matching evaluation section, add the Portuguese equivalent:

````markdown
O conjunto golden tem uma divisão de desenvolvimento e uma de holdout (dataset
2.2.0). Ajuste parâmetros só no desenvolvimento; o holdout é medido em todo
build e nunca vira gate, e um caso de holdout que embasou uma decisão passa
para o desenvolvimento na versão seguinte. `--split development|holdout`
avalia uma divisão, e todo resumo traz `by_split` e intervalos bootstrap de
95% (`intervals`).

A avaliação da notificação conta cada fundamento citado uma vez, como
conhecidamente errado (um hard negative rotulado), rotulado (um artigo que o
caso rotula) ou não rotulado, e informa `consumer_notice_precision` (rotulados
÷ citados, só para casos que citam algo) e `consumer_notice_article_recall`
ao lado do recall exato.

Compare duas execuções caso a caso, com intervalos bootstrap pareados:

```bash
python -m app.evaluation.compare antes.json depois.json --split development
```

A pilha configurada lê os vetores das consultas golden de um cache indexado
pelo contrato de embedding em `data/evaluation/query_vectors/`. Preencha-o uma
vez, um caso por vez (uma execução interrompida continua de onde parou), e
depois avalie sem carregar o modelo:

```bash
python -m app.evaluation.query_vectors                 # ou --case CASE_ID, repetível
python -m app.evaluation.query_vectors --check
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured \
  --require-cached-queries --output configured-notice.json
```

Uma recuperação configurada que degrada para apenas lexical falha o caso e
termina com código 2, em vez de ser pontuada como híbrida.
````

- [ ] **Step 5: Full verification**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q
.venv/Scripts/python.exe -m ruff check app tests
.venv/Scripts/python.exe -m mypy app
.venv/Scripts/lint-imports
.venv/Scripts/vulture app --min-confidence 90
```

Expected: all pass; coverage 100% on the scoped modules.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml docs/adr/0021-evaluation-precision-holdout-uncertainty.md docs/architecture.md README.md README-pt.md
git commit -m "docs(adr-0021): gate the development split and report the holdout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
