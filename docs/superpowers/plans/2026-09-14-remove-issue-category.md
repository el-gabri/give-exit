# Remove the Consumer Issue Category Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `issue_category` from the Consumer journey — interface, domain model, API and retrieval query builder — so that what the system knows about a case comes only from the consumer's own account and requested remedy.

**Architecture:**
- The 17 routed query expansions collapse into one fixed `LEGAL_LEXICON` constant, and the query builder returns two queries instead of three (the third would be a constant).
- `ConsumerIssueCategory` is deleted. The scope gate, the notice subject, the recommended-document checklist and the readiness contract all stop depending on it.
- The cross-encoder reranker, already implemented and disabled, takes over the ranking work the routed expansion was doing. No reindex is required.
- The golden dataset gains the real Bradesco case, whose hard negatives are the exact provisions the routed expansion fabricated.

**Tech Stack:** Python 3.10+ syntax, Pydantic v2, pytest (`asyncio_mode = "auto"`), Streamlit, FastAPI, ruff, strict mypy, `lint-imports`, `vulture`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-14-remove-issue-category-design.md`

**Delivery order:** one branch, `feat/remove-issue-category`, which already holds the spec commit. Task 1 is a gate: it can stop the whole plan.

## Global Constraints

- `QUERY_BUILDER_VERSION` becomes exactly `consumer-legal-two-query-v5`; `queries_per_case` becomes `2`.
- Golden dataset `version` goes from `1.2.0` to `2.0.0`; `dataset_sha256` is recomputed by the loader, never hand-written.
- Corpus release stays `br-consumer-law-2026-09-12-v4`; chunking stays `legal-hierarchy-v3:target=1200`; eligibility policy stays `consumer-notice-scope-eligibility-v3`. Nothing in this plan reindexes.
- `settings.reranker_provider` default stays `RerankerProvider.NONE` in code. Enabling it is configuration only.
- User-facing copy is Portuguese. Code, comments, docs and commit messages are English.
- After every task: `ruff check .`, `ruff format --check .`, `mypy app frontend`, and the touched tests. Commit per task.
- Commit trailers on every commit:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z
  ```
- Never push and never open a pull request. Merge happens locally, only when the user asks.

---

### Task 1: Measure the single lexicon (gate)

This task writes no production code. It answers the one question the spec could not: does a fixed lexicon preserve the routed expansion's benefit? Two earlier attempts segfaulted (exit 139) on the 4B embedder with 11.8 GB free.

**Files:**
- Create: `<scratchpad>/measure_lexicon.py` (throwaway, never committed)
- Modify: `docs/superpowers/specs/2026-09-14-remove-issue-category-design.md` (record the result in section 4.3)

**Interfaces:**
- Consumes: `app.evaluation.consumer_runner.ConsumerLegalRetrievalEvaluator`, `app.evaluation.consumer_retrievers.configured_hybrid_retriever`, `app.evaluation.consumer_golden.load_consumer_legal_dataset`
- Produces: measured `consumer_article_recall@5` for the lexicon arm, recorded in the spec. Tasks 2 and 12 depend on this number.

- [ ] **Step 1: Confirm the index and the model are available**

```bash
python -c "
import psycopg
with psycopg.connect('postgresql://postgres:giveexit@localhost:5432/postgres', connect_timeout=8) as c:
    with c.cursor() as cur:
        cur.execute('select namespace, count(*) from give_exit_vector_chunks group by namespace order by 2 desc')
        for r in cur.fetchall(): print(r[1], r[0])
"
```

Expected: a row with 2456 for `give-exit-consumer-br-consumer-law-2026-09-12-v4-6b18d3db4b-6648b139826d`. If Postgres is down, start it before continuing; do not fall back to the offline stack, whose dense side is a hashed 128-dimension embedder and cannot answer this question.

- [ ] **Step 2: Free memory before running**

```bash
python -c "
import psutil, os
print('livre GB:', round(psutil.virtual_memory().available/1e9, 1))
" 2>/dev/null || powershell -Command "\$os=Get-CimInstance Win32_OperatingSystem; [math]::Round(\$os.FreePhysicalMemory/1MB,1)"
```

Expected: at least 14 GB free. Below that, close other applications first — the previous two failures were at 11.8 GB.

- [ ] **Step 3: Write the measurement script**

Write to the scratchpad directory (not the repo):

```python
"""Throwaway: single lexicon vs routed expansion, configured JUA stack."""
import asyncio, json
from pathlib import Path
from app.core.logging import configure_logging
configure_logging(level="WARNING")
from app.evaluation.consumer_runner import ConsumerLegalRetrievalEvaluator
from app.evaluation.consumer_retrievers import configured_hybrid_retriever
from app.evaluation.consumer_golden import load_consumer_legal_dataset
import app.consumer.retrieval as R

DATASET = Path("eval_data/consumer_legal_retrieval")
M = ["consumer_recall@5", "consumer_article_recall@5", "consumer_mrr@5",
     "consumer_ndcg@5", "consumer_article_precision@5",
     "consumer_hard_negative_rate@5", "consumer_recall@10",
     "consumer_article_recall@10", "consumer_ndcg@10"]

LEXICO = (
    "direitos básicos do consumidor informação adequada e clara prática abusiva "
    "serviço não solicitado vício do produto ou serviço cobrança indevida "
    "contrato de adesão cláusula abusiva reparação de danos fornecedor"
)

async def main() -> None:
    ds = load_consumer_legal_dataset(DATASET)
    ev = ConsumerLegalRetrievalEvaluator(configured_hybrid_retriever)
    original = dict(R._CATEGORY_EXPANSIONS)
    for key in R._CATEGORY_EXPANSIONS:
        R._CATEGORY_EXPANSIONS[key] = LEXICO
    lex = await ev.run(ds)
    R._CATEGORY_EXPANSIONS.update(original)

    baseline = {
        "consumer_recall@5": 0.412, "consumer_article_recall@5": 0.658,
        "consumer_mrr@5": 0.372, "consumer_ndcg@5": 0.368,
        "consumer_article_precision@5": 0.200, "consumer_hard_negative_rate@5": 0.057,
        "consumer_recall@10": 0.561, "consumer_article_recall@10": 0.768,
        "consumer_ndcg@10": 0.412,
    }
    no_expansion = {
        "consumer_recall@5": 0.281, "consumer_article_recall@5": 0.425,
        "consumer_mrr@5": 0.223, "consumer_ndcg@5": 0.223,
        "consumer_article_precision@5": 0.126, "consumer_hard_negative_rate@5": 0.038,
        "consumer_recall@10": 0.522, "consumer_article_recall@10": 0.645,
        "consumer_ndcg@10": 0.310,
    }
    print(f"{'metric':36} {'routed':>9} {'lexicon':>9} {'none':>9}")
    for name in M:
        print(f"{name:36} {baseline[name]:9.3f} {lex.averages[name]:9.3f} {no_expansion[name]:9.3f}")
    json.dump(lex.averages, open("lexicon_arm.json", "w"), indent=1)

asyncio.run(main())
```

Note the third query is still produced by this script (the lexicon is non-empty), so this arm measures the lexicon *with* three queries. That is intentional: it isolates "routing vs no routing" from "three queries vs two". Task 2 measures the two-query form.

- [ ] **Step 4: Run it**

```bash
python <scratchpad>/measure_lexicon.py
```

Expected: a three-column table. Runtime is several minutes; the model loads once and embeds 63 queries.

If it exits 139 again, retry once with `PYTORCH_CUDA_ALLOC_CONF` unset and `OMP_NUM_THREADS=1`:

```bash
OMP_NUM_THREADS=1 python <scratchpad>/measure_lexicon.py
```

If it segfaults a third time, **stop and ask the user**. Do not proceed on the untested assumption, and do not substitute the offline stack.

- [ ] **Step 5: Apply the stop rule**

Read `consumer_article_recall@5` for the lexicon arm:

- **≥ 0.60** — the lexicon carries nearly all the benefit. Proceed; note in the spec that D2 is confirmed.
- **0.425 – 0.60** — partial. Proceed, and record that the reranker in Task 13 is now load-bearing rather than optional.
- **< 0.425** — the lexicon is worse than no expansion at all, meaning it is actively misdirecting retrieval. **Stop the plan** and return to brainstorming: D2 is wrong and approach 3 from the design discussion (signal-triggered vocabulary) needs reconsidering.

- [ ] **Step 6: Record the result in the spec**

Replace the second bullet of spec section 4.3 with the measured table and the bucket that applied. Remove the sentence "So the claim … is untested, and phase 1 exists to test it."

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-09-14-remove-issue-category-design.md
git commit -m "docs: record the measured single-lexicon retrieval arm

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 2: Replace routed expansions with a single lexicon and two queries

**Files:**
- Modify: `app/consumer/retrieval.py` (remove `_CATEGORY_EXPANSIONS`, `_SUBCATEGORY_SIGNALS`, `infer_retrieval_category`; add `LEGAL_LEXICON`; change both query builders)
- Modify: `app/evaluation/consumer_runner.py:52` (`QUERY_BUILDER_VERSION`), `:376` (`queries_per_case`)
- Test: `tests/test_consumer_retrieval.py`

**Interfaces:**
- Consumes: nothing from Task 1 but its go/no-go decision.
- Produces:
  ```python
  LEGAL_LEXICON: str
  def build_legal_queries(facts: ConsumerCaseFacts) -> list[str]          # 2 queries
  def build_legal_queries_for_case(*, complaint: str, desired_resolution: str) -> list[str]
  ```
  `build_legal_queries_for_case` loses its `category` keyword. Tasks 6 and 10 call the new signature.

- [ ] **Step 1: Write the failing tests**

Replace `test_legal_queries_are_driven_by_consumer_narrative` and delete `test_lay_intake_category_is_refined_for_legal_retrieval`, `test_retrieval_category_uses_confirmed_real_world_phrasing`, `test_personal_data_complaints_get_data_protection_vocabulary` and `test_specific_consumer_signals_still_win_over_personal_data` from `tests/test_consumer_retrieval.py`. Add:

```python
def test_legal_queries_are_two_and_driven_by_the_narrative() -> None:
    queries = build_legal_queries(_facts())

    assert len(queries) == 2
    assert all("cobrou duas vezes" in query for query in queries)
    assert LEGAL_LEXICON in queries[1]
    assert "devolução" in queries[0]


def test_the_lexicon_is_identical_for_every_complaint() -> None:
    """No routing: two unrelated complaints get the same injected vocabulary."""
    charge = build_legal_queries(_facts(complaint_summary="Cobraram tarifa que não pedi."))
    defect = build_legal_queries(_facts(complaint_summary="A geladeira parou de gelar."))

    assert charge[1].endswith(LEGAL_LEXICON)
    assert defect[1].endswith(LEGAL_LEXICON)


def test_the_lexicon_names_no_article_numbers() -> None:
    """Article numbers now name provisions in three statutes (ADR 0016)."""
    assert not any(character.isdigit() for character in LEGAL_LEXICON)


def test_no_query_is_a_constant_across_cases() -> None:
    """A case-independent query would feed every notice the same chunks."""
    first = build_legal_queries(_facts(complaint_summary="Cobrança em duplicidade."))
    second = build_legal_queries(_facts(complaint_summary="Produto nunca entregue."))

    assert not set(first) & set(second)


def test_long_complaint_does_not_remove_resolution_or_lexicon() -> None:
    facts = _facts(
        complaint_summary=("relato muito longo " * 500),
        desired_resolution="Quero devolução integral comprovada.",
    )

    queries = build_legal_queries(facts)

    assert all("devolução integral comprovada" in query for query in queries)
    assert LEGAL_LEXICON in queries[1]
```

Update the import block at the top of the file to drop `infer_retrieval_category` and add `LEGAL_LEXICON`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_consumer_retrieval.py -v`
Expected: FAIL — `ImportError: cannot import name 'LEGAL_LEXICON'`.

- [ ] **Step 3: Implement**

In `app/consumer/retrieval.py`, delete `_CATEGORY_EXPANSIONS`, `_SUBCATEGORY_SIGNALS` and `infer_retrieval_category` entirely, and replace them with:

```python
# One vocabulary for every complaint. The previous per-category expansions
# routed the query by an intake label, which injected "repetição do indébito
# pagamento em excesso" into a complaint about charges that were never paid
# and retrieved five grounds whose shared premise did not hold. Carrying no
# article numbers matters more since ADR 0016: "42" and "43" now name
# provisions in three different statutes.
LEGAL_LEXICON = (
    "direitos básicos do consumidor informação adequada e clara prática abusiva "
    "serviço não solicitado vício do produto ou serviço cobrança indevida "
    "contrato de adesão cláusula abusiva reparação de danos fornecedor"
)
```

Rewrite both builders:

```python
def build_legal_queries(facts: ConsumerCaseFacts) -> list[str]:
    """Build bounded, replayable legal queries from confirmed case facts."""

    return build_legal_queries_for_case(
        complaint=facts.complaint_summary or "",
        desired_resolution=facts.desired_resolution or "",
    )


def build_legal_queries_for_case(*, complaint: str, desired_resolution: str) -> list[str]:
    """Build the same production queries for a golden-dataset case."""

    # Reserve space for every signal instead of allowing a long complaint to
    # truncate the requested remedy or the legal vocabulary.
    bounded_complaint = _bounded_component(complaint, 1_050)
    bounded_resolution = _bounded_component(desired_resolution, 500)
    narrative = _join_non_empty(bounded_complaint, bounded_resolution)
    # A third, vocabulary-only query is deliberately absent: with a single
    # lexicon it would be identical for every case and would feed the same
    # chunks into the merge of every notice.
    queries = [
        _bounded(
            "Situação de consumo relatada: "
            f"{narrative}. Localizar dispositivos legais diretamente aplicáveis."
        ),
        _bounded(f"{narrative}. {LEGAL_LEXICON}"),
    ]
    return _unique_non_empty(queries)
```

In `app/evaluation/consumer_runner.py`, set `QUERY_BUILDER_VERSION = "consumer-legal-two-query-v5"` and change `queries_per_case=3` to `queries_per_case=2`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_consumer_retrieval.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite to see the blast radius**

Run: `pytest -x -q`
Expected: failures only in tests that assert three queries or category vocabulary. Fix those assertions to the two-query shape; do not weaken any assertion about abstention or citation integrity.

- [ ] **Step 6: Commit**

```bash
git add app/consumer/retrieval.py app/evaluation/consumer_runner.py tests/
git commit -m "feat(consumer): replace routed query expansions with one lexicon

The intake category selected one of 17 expansions. On a complaint about
charges that were never paid it injected 'repeticao do indebito pagamento
em excesso', retrieving CDC art. 42 sole paragraph and Civil Code arts.
878, 880 and 881 -- five grounds premised on a payment that never
happened, one of which argues the supplier's side.

One fixed lexicon replaces the routing, and the builder drops from three
queries to two: with a single vocabulary the third query would be a
constant, feeding every notice the same chunks.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 3: Drop the category from the consumer-scope gate

**Files:**
- Modify: `app/consumer/retrieval.py` (`is_consumer_scope`, remove `_OUT_OF_SCOPE_CATEGORIES`)
- Modify: `app/consumer/ground_selection.py:69-72`, `app/consumer/service.py:799-808`
- Test: `tests/test_consumer_retrieval.py`

**Interfaces:**
- Consumes: `build_legal_queries` from Task 2 (unchanged here).
- Produces: `def is_consumer_scope(*, complaint: str) -> bool`. Tasks 6 and 7 call it.

- [ ] **Step 1: Write the failing test**

Replace `test_concrete_category_cannot_bypass_the_consumer_scope_gate` in `tests/test_consumer_retrieval.py` with:

```python
def test_scope_gate_decides_from_the_narrative_alone() -> None:
    """No category exists any more; the narrative is the only input."""
    assert not is_consumer_scope(
        complaint="Meu empregador não pagou meu salário nem registrou a hora extra.",
    )
    assert not is_consumer_scope(
        complaint="Meu empregador descontou o vale-transporte do meu salário.",
    )
    assert not is_consumer_scope(complaint="Meu vizinho construiu um muro no meu terreno.")
    assert is_consumer_scope(
        complaint="A operadora cobrou pelo serviço de internet que nunca funcionou.",
    )
    assert is_consumer_scope(
        complaint="O banco bloqueou minha conta bancária e não libera meu saldo.",
    )
```

Every other `is_consumer_scope(...)` call in the file loses its `category=` argument. Keep every expected boolean exactly as it is: this test file is the regression record for the abstention behaviour and none of its outcomes may change.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_consumer_retrieval.py -k scope -v`
Expected: FAIL — `TypeError: is_consumer_scope() got an unexpected keyword argument 'complaint'` is not raised, but `missing 1 required keyword-only argument: 'category'` is.

- [ ] **Step 3: Implement**

In `app/consumer/retrieval.py`, delete `_OUT_OF_SCOPE_CATEGORIES` and rewrite the signature and its first lines:

```python
def is_consumer_scope(*, complaint: str) -> bool:
    """Return whether a case can safely use the consumer-law corpus.

    This is a high-precision abstention rule, not a legal merits classifier.
    It prevents known non-consumer disputes from being dressed in CDC grounds
    while leaving uncertain cases for human review. The narrative is the only
    input: the intake taxonomy that used to short-circuit this gate was
    keyword-inferred from this same text, so it could never contradict it.
    """

    normalized_complaint = _scope_normalize(complaint)
```

Delete the `normalized_category` binding, the `_OUT_OF_SCOPE_CATEGORIES` early return, and the `if normalized_category in _CATEGORY_EXPANSIONS and normalized_category != "other": return True` branch. Keep the strong/weak/non-consumer signal logic and the final return exactly as they are.

In `app/consumer/ground_selection.py`, replace lines 69-72 with:

```python
    if not is_consumer_scope(complaint=facts.complaint_summary or ""):
        return []
```

In `app/consumer/service.py`, replace the `category` binding and the call in `_readiness_missing` with:

```python
        if not is_consumer_scope(complaint=record.facts.complaint_summary or ""):
            missing.append("consumer_relationship")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_consumer_retrieval.py tests/test_ground_selection.py -v`
Expected: PASS.

- [ ] **Step 5: Verify abstention end to end**

Run: `python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --min consumer_abstention@5=1.0`
Expected: exit 0. Both `conflito_entre_vizinhos` and `salario_atrasado` must still abstain. If either stops abstaining, the narrative rules are load-bearing in a way the category was masking — stop and report, do not lower the gate.

- [ ] **Step 6: Commit**

```bash
git add app/consumer/retrieval.py app/consumer/ground_selection.py app/consumer/service.py tests/
git commit -m "refactor(consumer): decide consumer scope from the narrative alone

The gate already checked the narrative first, and the category it also
took was keyword-inferred from that same text, so it could never
contradict it. Both out-of-scope golden cases still abstain.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 4: Delete the notice subject line and the category label

**Files:**
- Modify: `app/consumer/ground_selection.py` (remove `CATEGORY_LABELS` and `issue_label`; reword `application_to_facts`)
- Modify: `app/consumer/service.py:1322` and `:1338`
- Test: `tests/test_ground_selection.py`, `tests/test_consumer_citation_integrity.py`

**Interfaces:**
- Consumes: `is_consumer_scope(complaint=...)` from Task 3.
- Produces: `ground_selection` no longer exports `issue_label` or `CATEGORY_LABELS`. Task 9 relies on the notice having no `**Assunto:**` line.

- [ ] **Step 1: Write the failing tests**

Replace `test_issue_label_defaults_to_a_generic_consumer_dispute` in `tests/test_ground_selection.py` with:

```python
def test_ground_rationale_does_not_name_an_issue_category() -> None:
    corpus = get_default_legal_corpus()
    chunk = _chunk_for_unit("br-cdc-art-42-paragrafo-unico")
    results = [[RetrievedChunk(chunk=chunk, score=0.03)]]
    grounds = select_legal_grounds(corpus, _facts(), results, [_trace(results[0])])

    assert grounds
    for ground in grounds:
        assert "a partir do relato do consumidor" in ground.application_to_facts
        assert "cobrança não reconhecida" not in ground.application_to_facts
```

These are the file's own helpers (`_facts`, `_trace`, `_chunk_for_unit`, and `get_default_legal_corpus` imported at the top) in the same shape `test_service_wrapper_returns_exactly_the_pure_selection` uses at line 85. Drop `issue_label` from the import block.

In `tests/test_consumer_citation_integrity.py`, add — note the tests there are async, use the `notice_client` fixture, the `X-Consumer-Case-Token` header via `_case_with_documents`, and read the markdown from `GET /consumer/cases/{case_id}/notice.md`:

```python
async def test_notice_has_no_subject_line(notice_client: httpx.AsyncClient) -> None:
    case_id, headers = await _case_with_documents(
        notice_client, [("fatura_loja.pdf", FATURA)]
    )

    await notice_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)
    markdown = (
        await notice_client.get(f"/consumer/cases/{case_id}/notice.md", headers=headers)
    ).text

    assert "**Assunto:**" not in markdown
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ground_selection.py tests/test_consumer_citation_integrity.py -v`
Expected: FAIL — the rationale still contains the category label, and the notice still has the subject line.

- [ ] **Step 3: Implement**

In `app/consumer/ground_selection.py` delete the `CATEGORY_LABELS` dict and the `issue_label` function, delete the `issue = issue_label(facts)` line, and change the rationale to:

```python
                    application_to_facts=(
                        f"O texto oficial em {provision.citation_label} foi localizado "
                        "pela política de recuperação a partir do relato do consumidor. "
                        "Sua aplicabilidade ao caso não foi decidida pelo sistema e deve "
                        "ser validada por profissional habilitado contra os fatos e "
                        "documentos citados."
                    ),
```

In `app/consumer/service.py` remove the `subject = issue_label(facts)` binding, remove `f"**Assunto:** {subject}",` and the `""` line that follows it from the `lines` list, and drop `issue_label` from the import on line 26.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ground_selection.py tests/test_consumer_citation_integrity.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/consumer/ground_selection.py app/consumer/service.py tests/
git commit -m "feat(consumer): drop the notice subject line and the category label

Section 1 (Finalidade) already describes the case in prose, and every
ground's rationale now credits the consumer's account rather than an
intake label.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 5: Stop classifying at intake and collapse the document checklist

**Files:**
- Modify: `app/consumer/intake.py` (remove `_classify` and `_CATEGORY_TERMS`; collapse `_RECOMMENDED_DOCUMENTS`; drop the category question)
- Modify: `app/consumer/service.py:789`
- Test: `tests/test_consumer_intake.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `def recommended_documents() -> list[str]`. Task 7 relies on `extract_explicit_facts` no longer producing a category.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_intake.py`, change `test_extracts_non_bank_supplier_and_service_failure` to drop its category assertion (keep the supplier one) and rename it to `test_extracts_non_bank_supplier`. Replace `test_generic_recommendations_do_not_assume_a_bank` with:

```python
def test_recommended_documents_are_one_generic_list() -> None:
    documents = recommended_documents()

    assert documents
    assert all("banco" not in document.casefold() for document in documents)


def test_intake_never_asks_for_a_problem_type() -> None:
    message = next_assistant_message(ConsumerCaseFacts(), has_evidence=False)

    assert "tipo de problema" not in message.casefold()
```

Drop `ConsumerIssueCategory` from the imports in this file.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_consumer_intake.py -v`
Expected: FAIL — `recommended_documents()` still requires an argument.

- [ ] **Step 3: Implement**

In `app/consumer/intake.py`:

- Delete `_CATEGORY_TERMS` and `_classify`.
- In `extract_explicit_facts`, delete the `category = ...` line and the `issue_category=category,` argument.
- Replace the whole `_RECOMMENDED_DOCUMENTS` mapping with:

```python
# One checklist for every case. The per-category lists depended on an intake
# label the journey no longer collects, and the generic list already covered
# the documents a notice actually needs.
_RECOMMENDED_DOCUMENTS = (
    "contrato, fatura ou extrato relacionado",
    "protocolos e respostas da empresa ou instituição",
    "comprovantes do prejuízo alegado",
)


def recommended_documents() -> list[str]:
    return list(_RECOMMENDED_DOCUMENTS)
```

- Delete the `"issue_category": (...)` entry from the `questions` map in `next_assistant_message`.

In `app/consumer/service.py:789`, change `recommended_documents(record.facts.issue_category)` to `recommended_documents()`.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_consumer_intake.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/consumer/intake.py app/consumer/service.py tests/
git commit -m "feat(consumer): stop classifying the complaint at intake

The keyword classifier and the eight per-category document checklists both
existed to serve a field the journey no longer collects.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 6: Remove the category from the evaluation harness and dataset

This must land **before** Task 7: `consumer_runner` and `consumer_notice` construct `ConsumerCaseFacts(issue_category=...)`, so deleting the field first would break the evaluator.

**Files:**
- Modify: `app/schemas/evaluation.py:277` (drop `intake_category`)
- Modify: `app/evaluation/consumer_runner.py:388,393`, `app/evaluation/consumer_notice.py:200,208`
- Modify: `eval_data/consumer_legal_retrieval/dataset.json` (drop 21 `intake_category` keys, bump `version`)
- Test: `tests/test_consumer_evaluation.py`, `tests/test_consumer_notice_evaluation.py`

**Interfaces:**
- Consumes: `is_consumer_scope(complaint=...)` from Task 3.
- Produces: `ConsumerLegalGoldenCase` without `intake_category`. Task 7 can then delete the enum.

- [ ] **Step 1: Write the failing test**

In `tests/test_consumer_evaluation.py`, delete `test_evaluator_uses_production_intake_category_for_queries` and `test_golden_case_rejects_non_production_intake_category`, and add:

```python
def test_golden_case_rejects_an_intake_category() -> None:
    """The field is gone; a dataset that still carries it must fail loudly."""
    payload = _case().model_dump(mode="json")
    payload["intake_category"] = "unauthorized_charge"

    with pytest.raises(ValidationError, match="intake_category"):
        ConsumerLegalGoldenCase.model_validate(payload)


def test_dataset_version_is_two_zero_zero() -> None:
    dataset = load_consumer_legal_dataset(Path("eval_data/consumer_legal_retrieval"))

    assert dataset.version == "2.0.0"
```

`_case()` is the file's existing fixture builder, used the same way at line 348 today. Also drop `ConsumerIssueCategory` from this file's imports (line 13) and add `from app.consumer.retrieval import LEGAL_LEXICON`.

The deleted `test_evaluator_uses_production_intake_category_for_queries` also covered something worth keeping: that the descriptive `category` still drives the `by_category` reporting breakdown. Replace it with a version that keeps only that:

```python
async def test_descriptive_category_still_drives_the_reporting_breakdown() -> None:
    case = _case().model_copy(update={"category": "right_of_withdrawal"})
    dataset = ConsumerLegalGoldenDataset(
        dataset_id="category-reporting-fixture",
        version="1.0.0",
        description="Fixture proving the descriptive category is reporting only.",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        authoring="developer_authored_seed",
        review_status="requires_legal_review",
        cases=(case,),
    )

    summary = await ConsumerLegalRetrievalEvaluator(lambda _query, _k: []).run(dataset)

    result = summary.cases[0]
    assert result.category == "right_of_withdrawal"
    assert summary.by_category["right_of_withdrawal"].case_count == 1
    # The category no longer reaches the query: two queries, one fixed lexicon.
    assert len(result.queries) == 2
    assert result.queries[1].endswith(LEGAL_LEXICON)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_consumer_evaluation.py -v`
Expected: FAIL — the model still accepts `intake_category`, and the version is `1.2.0`.

- [ ] **Step 3: Implement**

Delete the `intake_category` field from `ConsumerLegalGoldenCase` in `app/schemas/evaluation.py`. Confirm the model config is `extra="forbid"`; if it is not, add it, because criterion "fail loudly" depends on it.

In `app/evaluation/consumer_runner.py:_run_case`, replace the facts construction and the gate:

```python
            facts = ConsumerCaseFacts(
                complaint_summary=case.complaint,
                desired_resolution=case.desired_resolution,
            )
            if not is_consumer_scope(complaint=facts.complaint_summary or ""):
```

Apply the same two edits in `app/evaluation/consumer_notice.py:200,208`.

Strip the dataset with a one-off script, then bump the version by hand:

```bash
python - <<'PY'
import json, pathlib
path = pathlib.Path("eval_data/consumer_legal_retrieval/dataset.json")
data = json.loads(path.read_text(encoding="utf-8"))
for case in data["cases"]:
    case.pop("intake_category", None)
data["version"] = "2.0.0"
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("cases:", len(data["cases"]), "version:", data["version"])
PY
```

Expected: `cases: 21 version: 2.0.0`.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py -v`
Expected: PASS.

- [ ] **Step 5: Confirm the evaluator still runs**

Run: `python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --min consumer_abstention@5=1.0`
Expected: exit 0, and the printed run metadata shows `queries_per_case: 2` and `query_builder_version: consumer-legal-two-query-v5`.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/evaluation.py app/evaluation/ eval_data/ tests/
git commit -m "test(consumer): drop the intake category from the golden dataset

Dataset 1.2.0 -> 2.0.0. The descriptive 'category' field stays for the
by-category breakdown; nothing reads it to build a query.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 7: Delete the field and the enum from the domain model and the API

Nothing reads the category by this point. This task removes it.

**Files:**
- Modify: `app/consumer/schemas.py:25-35` (delete `ConsumerIssueCategory`), `:125`, `:186`, `:168-178` (`missing_fields`)
- Modify: `app/api/consumer_routes.py:80`
- Test: `tests/test_consumer_schemas.py`, `tests/test_consumer_api.py`, `tests/test_consumer_grounds.py`

**Interfaces:**
- Consumes: Tasks 2–6, which removed every reader.
- Produces: `ConsumerCaseFacts` with five required fields. `ConsumerFactsPatch` rejects `issue_category` with HTTP 422.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_schemas.py`, update the readiness test:

```python
def test_consumer_facts_report_only_drafting_critical_gaps() -> None:
    empty = ConsumerCaseFacts()
    assert empty.missing_fields() == [
        "bank_name",
        "consumer_name",
        "complaint_summary",
        "incident_date_or_period",
        "desired_resolution",
    ]

    complete = ConsumerCaseFacts(
        consumer_name="Pessoa Consumidora",
        bank_name="Banco Exemplo",
        complaint_summary="Uma compra não reconhecida apareceu na fatura.",
        incident_date_or_period="julho de 2026",
        desired_resolution="Estorno e bloqueio da cobrança.",
    )
    assert complete.missing_fields() == []
```

In `tests/test_consumer_api.py`, add:

```python
async def test_facts_patch_rejects_the_removed_issue_category(
    consumer_client: httpx.AsyncClient,
) -> None:
    case_id, token = await _new_case(consumer_client)

    response = await consumer_client.patch(
        f"/consumer/cases/{case_id}/facts",
        headers=_headers(token),
        json={"issue_category": "unauthorized_charge"},
    )

    assert response.status_code == 422
```

These are the file's own fixture and helpers: the async `consumer_client` fixture, `_new_case(client)` returning `(case_id, token)`, and `_headers(token)` producing the `X-Consumer-Case-Token` header — not an `Authorization: Bearer` one. Remove `"issue_category": ...` from every existing facts payload in that file (lines 116, 267, 311) and drop the `issue_category` assertion on line 103.

In `tests/test_consumer_grounds.py`, delete `test_issue_category_does_not_filter_the_authorities` — it asserts a property of a field that no longer exists — and remove `"issue_category": category` from the `_facts` helper.

Remove the `"issue_category": ...` entry from the `_facts` payload helper in `tests/test_consumer_retrieval.py`, `tests/test_ground_selection.py` and `tests/test_consumer_citation_integrity.py` (line 561 and `_confirmed_facts`), and drop `ConsumerIssueCategory` from every remaining import across `tests/`. After this task the name must not appear anywhere.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_consumer_schemas.py tests/test_consumer_api.py -v`
Expected: FAIL — `missing_fields()` still returns six entries and the PATCH returns 200.

- [ ] **Step 3: Implement**

In `app/consumer/schemas.py`: delete the `ConsumerIssueCategory` class, delete `issue_category` from `ConsumerCaseFacts` and `ConsumerIntakeExtraction`, and delete the `"issue_category": self.issue_category,` entry from `missing_fields`'s `required` dict.

In `app/api/consumer_routes.py`: delete the `issue_category` field from `ConsumerFactsPatch` and drop `ConsumerIssueCategory` from its imports. The model is already `ConfigDict(extra="forbid")`, so a client that keeps sending the field gets 422 — that is the intended, loud break.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_consumer_schemas.py tests/test_consumer_api.py tests/test_consumer_grounds.py -v`
Expected: PASS.

- [ ] **Step 5: Prove the concept is gone from the backend**

```bash
grep -rn "issue_category\|ConsumerIssueCategory\|intake_category\|issue_label\|CATEGORY_LABELS\|_CATEGORY_EXPANSIONS\|infer_retrieval_category" app/ eval_data/ tests/
```

Expected: no output except matches inside `frontend/`, which Task 8 removes.

- [ ] **Step 6: Commit**

```bash
git add app/consumer/schemas.py app/api/consumer_routes.py tests/
git commit -m "feat(consumer)!: remove the issue category from the facts model

ConsumerIssueCategory is deleted and issue_category leaves ConsumerCaseFacts,
ConsumerIntakeExtraction, the readiness contract and ConsumerFactsPatch.
That patch model forbids extra fields, so a client still sending the
category now gets HTTP 422 rather than a silent drop.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 8: Remove the field from the Streamlit interface

**Files:**
- Modify: `frontend/consumer_view.py` (`ISSUE_LABELS`, the selectbox at 429-435, the summary at 365-372, the payload at 526, the widget-key set at 556, the sync at 572-578, `FACT_LABELS`)

**Interfaces:**
- Consumes: `ConsumerFactsPatch` from Task 7, which now rejects the field.
- Produces: no new interface. This is the deliverable the user asked for.

- [ ] **Step 1: Delete `ISSUE_LABELS`**

Remove the whole `ISSUE_LABELS` dict from the top of the file and the `"issue_category": "Tipo de problema",` entry from `FACT_LABELS`. Keep the `consumer_relationship` entry in `FACT_LABELS`: it labels the scope-gate readiness failure, not a category.

- [ ] **Step 2: Replace the two-column row with a single date input**

```python
        incident_period = st.text_input(
            "Data ou período do ocorrido",
            key="consumer_fact_incident_date_or_period",
            help="Pode ser uma data aproximada.",
        )
```

This replaces the `category_column, date_column = st.columns(2)` line, the `issue_category = category_column.selectbox(...)` block and the `date_column.text_input(...)` block.

- [ ] **Step 3: Drop the field from the case summary**

In the `summary_column` block, delete the `category = ISSUE_LABELS.get(...)` binding and the `st.markdown(f"**Problema:** {category}")` line. The supplier line and the complaint caption stay.

- [ ] **Step 4: Drop the field from the payload and the widget state**

Remove `"issue_category": issue_category,` from the `payload` dict, `"consumer_fact_issue_category",` from `required_widget_keys`, and the `current_category` binding together with `"consumer_fact_issue_category": current_category,` from the `values` dict in `_sync_fact_widgets`.

- [ ] **Step 5: Verify no reference survives**

```bash
grep -rn "issue_category\|ISSUE_LABELS" frontend/
```

Expected: no output.

- [ ] **Step 6: Run the app and confirm the form**

Run: `streamlit run frontend/app.py` (or the project's documented entry point), open the Consumer journey, and confirm the facts form shows no "Tipo principal do problema" control and that saving facts succeeds.

Expected: the form saves without a 422. A 422 here means a stale widget key is still in the payload.

- [ ] **Step 7: Commit**

```bash
git add frontend/consumer_view.py
git commit -m "feat(frontend): remove the problem-type field from the facts form

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 9: Add the Bradesco case to the golden dataset

Without this case no metric in the plan can see the defect that motivated it: CC arts. 878, 880 and 881 are not labelled as hard negatives anywhere in the dataset.

**Files:**
- Modify: `eval_data/consumer_legal_retrieval/dataset.json`
- Test: `tests/test_consumer_evaluation.py`

**Interfaces:**
- Consumes: the dataset schema from Task 6 (no `intake_category`).
- Produces: case `cobranca_de_servico_nao_solicitado`, used by Task 10's gates.

- [ ] **Step 1: Verify every labelled provision exists and is retrievable**

```bash
python -c "
from app.consumer.legal_corpus import get_default_legal_corpus
c = get_default_legal_corpus()
retrievable = {p.provision_id for p in c.retrievable_provisions()}
by_id = {p.provision_id: p for p in c.provisions}
for pid in ('br-cdc-art-39','br-cdc-art-6','br-cdc-art-52','br-cc-art-878','br-cc-art-880','br-cc-art-881','br-cdc-art-54-g'):
    p = by_id.get(pid)
    print(pid, '| exists:', p is not None, '| retrievable:', pid in retrievable, '| status:', p.status.value if p else '-')
"
```

Expected: every id exists, is retrievable and is `active`. If a Civil Code id differs (the parser may spell it `br-cc-art-878` or otherwise), use the printed spelling in the dataset — do not invent ids.

- [ ] **Step 2: Write the failing test**

```python
def test_dataset_covers_an_unrequested_service_charge() -> None:
    dataset = load_consumer_legal_dataset(Path("eval_data/consumer_legal_retrieval"))
    case = next(c for c in dataset.cases if c.case_id == "cobranca_de_servico_nao_solicitado")

    assert any(item.article_id == "br-cdc-art-39" for item in case.relevant)
    assert "br-cc-art-880" in case.hard_negatives
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_consumer_evaluation.py -k unrequested -v`
Expected: FAIL — `StopIteration`, the case does not exist.

- [ ] **Step 4: Add the case**

Append to `cases` in `dataset.json`:

```json
{
  "case_id": "cobranca_de_servico_nao_solicitado",
  "category": "abusive_practice",
  "slices": ["supplier:bank", "remedy:revision", "wording:lay"],
  "complaint": "Estou recebendo cobranças indevidas do banco por juros do cheque especial. Cobram também um serviço de manutenção de conta corrente que eu não solicitei. Registrei reclamação no SAC e nada foi corrigido. Ainda não paguei nada do que está sendo cobrado.",
  "desired_resolution": "Quero a revisão da dívida com a exclusão dos encargos que não contratei e que não haja negativação.",
  "relevant": [
    {
      "article_id": "br-cdc-art-39",
      "unit_id": "br-cdc-art-39-inciso-iii",
      "grade": 3,
      "rationale": "Fornecer serviço sem solicitação prévia é prática abusiva; é a hipótese literal da tarifa de manutenção não contratada."
    },
    {
      "article_id": "br-cdc-art-6",
      "unit_id": "br-cdc-art-6-inciso-iii",
      "grade": 2,
      "rationale": "A cobrança não discrimina a composição do débito, violando o direito à informação adequada e clara."
    },
    {
      "article_id": "br-cdc-art-52",
      "unit_id": null,
      "grade": 2,
      "rationale": "Juros de cheque especial exigem informação prévia sobre encargos e custo efetivo total."
    }
  ],
  "hard_negatives": [
    "br-cc-art-878",
    "br-cc-art-880",
    "br-cc-art-881",
    "br-cdc-art-54-g"
  ]
}
```

The complaint states explicitly that nothing was paid. That sentence is what makes the undue-payment chapter a hard negative rather than a plausible ground.

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_consumer_evaluation.py -v`
Expected: PASS, and the dataset now loads 22 cases.

- [ ] **Step 6: Commit**

```bash
git add eval_data/ tests/
git commit -m "test(consumer): add the unrequested-service charge to the golden

Taken from a real 2026-09-14 complaint. Its hard negatives are the exact
provisions the routed expansion fabricated: Civil Code arts. 878, 880 and
881, whose chapter presumes a payment the complaint says never happened.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 10: Re-baseline the CI gates on measured values

**Files:**
- Modify: `.github/workflows/ci.yml:73-95`

**Interfaces:**
- Consumes: every earlier task.
- Produces: gates the rest of the project reads as the regression floor.

- [ ] **Step 1: Measure the offline stack after the change**

```bash
python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --output post-change-offline.json
python -c "
import json
a = json.load(open('post-change-offline.json'))['averages']
for k in ('consumer_recall@5','consumer_article_recall@5','consumer_ndcg@5','consumer_hard_negative_rate@5','consumer_abstention@5','consumer_retrieval_success@5'):
    print(f'{k:38} {a[k]:.3f}')
"
```

Record these six numbers. The CI job runs the offline stack, so these — not the JUÁ numbers — are what the gates must track.

- [ ] **Step 2: Measure the notice path**

```bash
python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --output post-change-notice.json
python -c "
import json
a = json.load(open('post-change-notice.json'))['averages']
print('known_bad:', a.get('consumer_notice_known_bad_citations'))
print('abstention:', a.get('consumer_notice_abstention'))
"
```

- [ ] **Step 3: Set the gates**

Set each `--min` to the measured value rounded **down** to two decimals, and each `--max` to the measured value rounded **up**. Update the comment block above the step to say what was re-baselined and why:

```yaml
        # Re-baselined on 2026-09-14 for the two-query builder with a single
        # legal lexicon (no intake category). The dataset also gained the
        # unrequested-service case, so the case count moves from 21 to 22.
```

Hard invariants that do **not** move regardless of measurement:

```
--min consumer_retrieval_success@5=1.0
--min consumer_abstention@5=1.0
--max consumer_inactive_provision_rate@5=0.0
--max consumer_unknown_status_rate@5=0.0
```

The notice known-bad ceiling may only go down from its current value of 1. If it measures higher than 1, **stop**: the change made citations worse, and that is the outcome this plan exists to prevent.

- [ ] **Step 4: Run the gates exactly as CI will**

```bash
python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval \
  --output consumer-legal-retrieval-results.json \
  --min consumer_retrieval_success@5=1.0 \
  --min consumer_abstention@5=1.0 \
  --max consumer_inactive_provision_rate@5=0.0 \
  --max consumer_unknown_status_rate@5=0.0
```

Expected: exit 0.

- [ ] **Step 5: Run the full suite and the static gates**

```bash
pytest -q && ruff check . && ruff format --check . && mypy app frontend && lint-imports && vulture app --min-confidence 90
```

Expected: all pass. `vulture` is the one most likely to complain, since this plan deletes a lot; if it reports newly-dead code that the deletions orphaned, remove that too rather than lowering the confidence threshold.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: re-baseline the consumer gates on the two-query builder

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 11: Measure and decide the reranker

The design's D4 expects the cross-encoder to recover the ranking the routed expansion provided. Whether it fits on this machine is an open question: the 4B embedder already segfaulted twice under memory pressure.

**Files:**
- Modify: `.env` (configuration only, not committed if it is gitignored — check first)
- Modify: `docs/superpowers/specs/2026-09-14-remove-issue-category-design.md` (record the decision)

**Interfaces:**
- Consumes: the post-change query builder from Task 2.
- Produces: a recorded decision. No code default changes.

- [ ] **Step 1: Confirm the default stays NONE**

```bash
grep -n "reranker_provider" app/core/config.py
```

Expected: `reranker_provider: RerankerProvider = RerankerProvider.NONE`. Do not change this line. Anyone running without the cross-encoder model must keep working.

- [ ] **Step 2: Enable it in configuration only**

Add to `.env`:

```
LITIGATION_RERANKER_PROVIDER=sentence_transformers
LITIGATION_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

Confirm the env-var prefix by reading `app/core/config.py`; use whatever prefix the other settings use rather than assuming `LITIGATION_`.

- [ ] **Step 3: Measure the configured stack with the reranker on**

```bash
python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval \
  --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever \
  --output post-change-reranked.json
```

Expected: a run whose `run.retrieval.reranker_model` is `BAAI/bge-reranker-v2-m3`. Compare `consumer_ndcg@5` and `consumer_article_recall@5` against the routed baseline of 0.368 and 0.658.

If this segfaults or the process is killed, that is the answer: the reranker does not fit alongside the 4B embedder on this machine. Record it and move on — do not spend more than two attempts.

- [ ] **Step 4: Decide and record**

Append to spec section 4 a subsection `4.5 Reranker measurement` with the numbers, or with the memory failure, and state the decision: enabled in the JUÁ configuration, or deferred with the reason.

- [ ] **Step 5: Revert `.env` if the answer is "deferred"**

Leave the configuration in whatever state the decision calls for, and say so explicitly in the commit message.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-09-14-remove-issue-category-design.md
git commit -m "docs: record the reranker measurement and decision

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

### Task 12: Record the ADR and update the documentation

**Files:**
- Create: `docs/adr/0018-narrative-only-consumer-intake.md`
- Modify: `docs/adr/0012-bounded-consumer-extrajudicial-notice.md` (amendment header and the category consequence bullet)
- Modify: `README.md` and `README-pt.md` if either documents the intake fields

**Interfaces:**
- Consumes: the decisions recorded in Tasks 1–11.
- Produces: the project's permanent record of this change.

- [ ] **Step 1: Check what the READMEs claim about intake**

```bash
grep -n "categoria\|category\|Tipo de problema\|tipo do problema" README.md README-pt.md
```

Fix whatever describes a category field. If nothing matches, skip the README edits.

- [ ] **Step 2: Write ADR 0018**

Follow the structure of `docs/adr/0016-multi-statute-consumer-corpus.md`: Status/Date header, Context, Decision as a numbered list, Consequences as `(+)`/`(-)` bullets. The decision must state:

1. The Consumer intake collects no issue category. Retrieval queries are built from the complaint and the requested remedy alone.
2. One fixed legal lexicon replaces the 17 category expansions; the query builder returns two queries (`consumer-legal-two-query-v5`).
3. The consumer-scope gate decides from the narrative alone.
4. `ConsumerFactsPatch` forbids extra fields, so a client still sending `issue_category` receives HTTP 422. The break is intentional and loud.
5. The notice has no subject line, and every ground's rationale credits the consumer's account.

Consequences must include the measured trade-off, not just the benefit: removing the routed expansion cost article recall@5 on the configured stack (0.658 → 0.425 with no expansion; the lexicon arm's measured value from Task 1), and the fabricated-ground failure it prevents.

- [ ] **Step 3: Amend ADR 0012**

Change its header line to add `· Amended by [ADR 0018](0018-narrative-only-consumer-intake.md)` and mark the consequence bullet "(-) The initial category set will not cover every consumer dispute…" as superseded by ADR 0018. Decision §1 needs no change: it never listed the category among the bounded intake fields.

- [ ] **Step 4: Final verification**

```bash
grep -rn "issue_category\|ConsumerIssueCategory\|intake_category\|issue_label\|CATEGORY_LABELS\|_CATEGORY_EXPANSIONS\|infer_retrieval_category" app/ frontend/ tests/ eval_data/
pytest -q && ruff check . && ruff format --check . && mypy app frontend && lint-imports && vulture app --min-confidence 90
```

Expected: the grep prints nothing; every gate passes.

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs: record ADR 0018 on the narrative-only consumer intake

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RRr3kc3AwAsKmNSj8j661z"
```

---

## Acceptance

The plan is done when all of the following hold, quoting spec section 8:

1. `consumer_abstention@5` is 1.0; `conflito_entre_vizinhos` and `salario_atrasado` still abstain.
2. `consumer_retrieval_success@5` is 1.0.
3. `consumer_inactive_provision_rate@5` and `consumer_unknown_status_rate@5` are 0.0.
4. On `cobranca_de_servico_nao_solicitado`, no `hard_negatives` provision is cited and at least one `relevant` provision is.
5. The CDC anchor still holds: a notice citing a complementary ground also cites a CDC one.
6. The grep in Task 12 Step 4 prints nothing.
7. The full suite and every static gate pass; the notice evaluation runs without a lexical-only fallback.
8. Task 11 recorded a measured reranker decision, whether or not it was enabled.

Then offer the finishing menu from `superpowers:finishing-a-development-branch`. Do not push and do not open a pull request unless the user asks.
