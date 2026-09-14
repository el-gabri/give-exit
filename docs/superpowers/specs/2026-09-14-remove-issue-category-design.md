# Removing the consumer issue category — design

- Date: 2026-09-14
- Status: approved section by section in brainstorming; pending review of this written spec
- Classification: architectural — amends ADR 0012 §1 and its category consequence
- Evidence: measurements taken on 2026-09-14 against this checkout, corpus release
  `br-consumer-law-2026-09-12-v4` and the live Postgres index (section 4)

## 1. Goal

Remove `issue_category` from the Consumer journey: from the interface, from the domain model, from
the API, and from the retrieval query builder. What the system knows about a case comes from the
consumer's own account (`complaint_summary`) and requested remedy (`desired_resolution`), with no
intermediate label between what the person wrote and what is retrieved.

The interface simplification is the request. The measured defect it also fixes is separate and
concrete: the category selects a retrieval expansion, and on a real complaint about charges never
paid the `unauthorized_charge` expansion injected "repetição do indébito pagamento em excesso",
which retrieved CDC art. 42 § único and Civil Code arts. 878, 880 and 881 — five grounds whose
shared premise (a payment was made) did not hold. CC art. 880 exempts the recipient from restituting;
it argues the supplier's side.

## 2. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Scope | Remove the concept, not just the widget. `ConsumerIssueCategory` ceases to exist. |
| D2 | Query expansion | One fixed `LEGAL_LEXICON` string for every case. No routing. |
| D3 | Query count | Two queries per case, not three. See section 5. |
| D4 | Ranking | Enable the already-implemented cross-encoder reranker on the configured JUÁ stack to recover the ranking loss the routed expansion was providing. Code default stays `NONE`. |
| D5 | Notice subject | Delete the `**Assunto:**` line. Section 1 (Finalidade) already describes the case in prose. |
| D6 | Recommended documents | One generic list for every case (today's `other` list). |
| D7 | Compatibility | Clean removal. The field leaves the API, the schema, the enum and the golden dataset. No deprecation window. |
| D8 | Scope gate | `is_consumer_scope` loses its `category` parameter. The narrative already decides, and the code already checks the narrative first. |
| D9 | Golden coverage | The Bradesco case enters the dataset as part of this change, not after it. |

## 3. Non-goals

- Replacing the category with an LLM classifier, or with any other per-case routing.
- Changing the corpus, the chunking, the embedding model or revision, or reindexing. The reranker is
  not part of the vector-store namespace (section 4.4).
- Changing ground eligibility policy (`consumer-notice-scope-eligibility-v3`), the CDC anchor, the
  complementary-ground cap, or the degraded-mode rules from ADR 0016.
- The settlement defect found alongside this one: the contested debt amount booked as
  `direct_loss_amount`. Recorded as a follow-up in section 10.
- Legal validation. The new golden labels remain `requires_legal_review`.

## 4. Evidence gathered on 2026-09-14

### 4.1 What the category reaches

42 occurrences across 9 application files and 39 across 9 test files. Its five consumers:

| Consumer | Location |
|---|---|
| Selects the retrieval expansion | `retrieval.py` `_CATEGORY_EXPANSIONS` |
| Notice subject line and case summary label | `ground_selection.issue_label` → `service.py:1338` |
| Recommended-document checklist | `intake.recommended_documents` |
| Consumer-scope abstention gate | `retrieval.is_consumer_scope` |
| Required readiness field | `schemas.missing_fields` |

`ground_selection.py:75-79` already records that the category no longer decides which articles may
be cited. Its remaining influence on the citation is entirely through the query.

### 4.2 Effect of the expansion — configured JUÁ stack

21 golden cases, corpus v4, index
`give-exit-consumer-br-consumer-law-2026-09-12-v4-6b18d3db4b-6648b139826d`, reranker off.
Arm B sets every expansion to the empty string, which also drops the third query.

| Metric | Routed expansion (baseline) | No expansion | Delta |
|---|---|---|---|
| `consumer_article_recall@5` | 0.658 | 0.425 | −0.233 |
| `consumer_recall@5` | 0.412 | 0.281 | −0.131 |
| `consumer_ndcg@5` | 0.368 | 0.223 | −0.145 |
| `consumer_mrr@5` | 0.372 | 0.223 | −0.149 |
| `consumer_article_precision@5` | 0.200 | 0.126 | −0.074 |
| `consumer_hard_negative_rate@5` | 0.057 | 0.038 | −0.019 |
| `consumer_recall@10` | 0.561 | 0.522 | −0.039 |
| `consumer_article_recall@10` | 0.768 | 0.645 | −0.123 |
| `consumer_ndcg@10` | 0.412 | 0.310 | −0.102 |

Two readings drive this design:

1. **The loss is ranking, not findability.** `recall@10` moves by −0.039 while `recall@5` moves by
   −0.131. The expansion reorders provisions that hybrid retrieval already reaches by rank 10; it rarely
   surfaces one that was otherwise unreachable. That is the function of a reranker, which is why D4
   exists.
2. **The expansion trades precision for recall.** `hard_negative_rate@5` *falls* when it is removed.
   It retrieves more relevant provisions and more labelled bad ones at the same time.

### 4.3 Limits of this evidence

- The offline stack (`offline_mock_bm25_hybrid`) was measured first and showed a larger drop
  (article recall@5 0.482 → 0.219). It is not evidence for this decision: its dense side is a hashed
  128-dimension embedder, so the arm is effectively BM25, where removing legal keywords must hurt.
- **The single-lexicon arm was not measured.** Two attempts to run it segfaulted (exit 139) on the
  4B model with 11.8 GB free of 31.6 GB. So the claim "a fixed lexicon preserves most of the routed
  expansion's benefit" is untested, and phase 1 exists to test it.
- The golden set scores recall against labelled relevant provisions. It cannot see the failure that
  motivated this work: CC arts. 878, 880 and 881 are not labelled as hard negatives in any case.
  D9 exists because of this blind spot.

### 4.4 The reranker does not invalidate the index

`PostgresVectorStore` namespaces rows by `index_name` (`vector_store.py:329`). The reranker appears
only in `index_version` (`pipeline.py:125`), a provenance string carried in traces. Enabling it
changes recorded trace metadata and requires no reindex; the 2,455 v4 vectors stay valid.

## 5. Query structure

Today `build_legal_queries_for_case` returns three queries: framed narrative, narrative + expansion,
and expansion alone. The third exists so a long lay narrative cannot drown explicit legal anchors.

With a single lexicon, the third query becomes a **constant**: identical for every case, returning
the same chunks, which then enter the RRF merge of every notice. That is constant noise, not signal,
and it would make every notice share a floor of generic grounds.

The builder therefore returns **two** queries:

1. `"Situação de consumo relatada: {narrative}. Localizar dispositivos legais diretamente aplicáveis."`
2. `"{narrative}. {LEGAL_LEXICON}"`

where `narrative` is the bounded complaint joined with the bounded desired resolution, unchanged.
`QUERY_BUILDER_VERSION` becomes `consumer-legal-two-query-v5`; `queries_per_case` becomes 2.

`LEGAL_LEXICON` is one string of general consumer-law vocabulary, deliberately not case-specific:

```
direitos básicos do consumidor informação adequada e clara prática abusiva serviço não
solicitado vício do produto ou serviço cobrança indevida contrato de adesão cláusula
abusiva reparação de danos fornecedor
```

It carries no article numbers. ADR 0016's note applies with more force now that "42" and "43" name
articles in three statutes.

## 6. Design

### 6.1 `app/consumer/retrieval.py`

Removed: `_CATEGORY_EXPANSIONS`, `_SUBCATEGORY_SIGNALS`, `infer_retrieval_category`,
`_OUT_OF_SCOPE_CATEGORIES`.

Added: `LEGAL_LEXICON`.

Changed signatures:

```python
def build_legal_queries(facts: ConsumerCaseFacts) -> list[str]
def build_legal_queries_for_case(*, complaint: str, desired_resolution: str) -> list[str]
def is_consumer_scope(*, complaint: str) -> bool
```

`_bounded_component` budgets stay (1,050 complaint / 500 resolution / 320 expansion → lexicon).
The module docstring's replayability claim stays true and gets stronger: there is now no
category-dependent branch in query construction at all.

`is_consumer_scope` keeps every narrative-based rule: strong/weak/non-consumer signals, clause
splitting, the bank-account carve-out and the mixed-domain employer rule. Only the category
short-circuit and the out-of-scope category set go. Section 7 covers why this is the highest-risk
deletion in the change.

### 6.2 `app/consumer/schemas.py`

`ConsumerIssueCategory` deleted. `issue_category` removed from `ConsumerCaseFacts`, from
`ConsumerIntakeExtraction` and from the `missing_fields()` required map, which drops from six
entries to five.

### 6.3 `app/consumer/intake.py`

`_classify` and `_CATEGORY_TERMS` deleted. `extract_explicit_facts` stops producing a category.
`_RECOMMENDED_DOCUMENTS` collapses from eight keyed lists to one module constant:

```
contrato, fatura ou extrato relacionado
protocolos e respostas da empresa ou instituição
comprovantes do prejuízo alegado
```

`recommended_documents()` takes no argument. The `issue_category` entry leaves the
`next_assistant_message` question map, so the chat no longer asks "Qual tipo de problema ocorreu…".

### 6.4 `app/consumer/ground_selection.py`

`CATEGORY_LABELS` and `issue_label` deleted. `select_legal_grounds` calls
`is_consumer_scope(complaint=...)`.

The label also appears inside every ground's `application_to_facts`, which becomes:

> O texto oficial em {citation_label} foi localizado pela política de recuperação a partir do relato
> do consumidor. Sua aplicabilidade ao caso não foi decidida pelo sistema e deve ser validada por
> profissional habilitado contra os fatos e documentos citados.

### 6.5 `app/consumer/service.py`

The `**Assunto:**` header line and its blank line are removed from the notice. `recommended_documents()`
is called without an argument. `_readiness_missing` drops the category variable and calls the new
scope-gate signature.

### 6.6 `app/api/consumer_routes.py`

`issue_category` leaves `ConsumerFactsPatch` (`consumer_routes.py:80`). That model is
`ConfigDict(extra="forbid")`, so a client that keeps sending the field gets a **422**, not a silent
drop. This is the intended behaviour under D7 and must be stated in the ADR: the break is loud.

### 6.7 `frontend/consumer_view.py`

The `selectbox` and `ISSUE_LABELS` are removed. The two-column row that held category and date
becomes a single full-width date input. The case summary shows supplier and complaint excerpt, with
the "Problema:" line gone. `consumer_fact_issue_category` leaves the payload, the required widget-key
set and `_sync_fact_widgets`. `FACT_LABELS` loses its `issue_category` entry, and the
`consumer_relationship` entry stays — it labels the scope-gate readiness failure, not a category.

### 6.8 Evaluation and CI

`intake_category` is removed from `ConsumerLegalGoldenCase` (`app/schemas/evaluation.py:277`) and
from `dataset.json`. `category` stays as descriptive metadata for the `by_category` breakdown only;
nothing reads it to build a query. `consumer_runner._run_case` and `consumer_notice` stop passing it.

Dataset version 1.2.0 → 2.0.0, with a new `dataset_sha256`. The CI gates are re-baselined on the
measured post-change values; section 9 lists which.

### 6.9 The Bradesco golden case

Added to `dataset.json` from the real 2026-09-14 complaint:

- complaint: cheque-especial interest plus an unrequested account-maintenance service, charged and
  not paid; SAC protocol recorded.
- `relevant`: CDC art. 39 III (`br-cdc-art-39-inciso-iii`), CDC art. 6 III
  (`br-cdc-art-6-inciso-iii`), CDC art. 52 (`br-cdc-art-52`).
- `hard_negatives`: CC arts. 878, 880, 881 and CDC art. 54-G I.

All four hard negatives and all three relevant provisions were verified present, active and
retrievable in corpus v4 on 2026-09-14, so the case is a retrieval-quality probe, not a corpus gap.

### 6.10 ADR

A new ADR records the removal and supersedes ADR 0012's category consequence bullet
("the initial category set will not cover every consumer dispute…"). ADR 0012 §1 already bounds the
intake to complaint, supplier, dates, protocols, confirmed loss, desired resolution and evidence —
it never listed the category — so the decision text needs no amendment, only the consequence.

## 7. Phases

**Phase 1 — measure the lexicon.** Before any removal, measure the single-lexicon two-query builder
against the routed baseline on the configured stack. This is the arm that segfaulted; run it with
reduced memory (float16, batch 1) or on a machine with more headroom. Record the result in this spec.

Stop rule: if the lexicon loses more than the no-expansion arm already lost
(article_recall@5 < 0.425), the lexicon is not carrying vocabulary at all and the design returns to
brainstorming rather than proceeding.

**Phase 2 — remove the concept.** Sections 6.1 through 6.7, with tests, in one branch.

**Phase 3 — evaluation, golden and gates.** Sections 6.8 and 6.9. Re-baseline.

**Phase 4 — reranker.** Enable on the configured stack, measure, and decide. Its acceptance is
conditional (section 8): a cross-encoder alongside a 4B embedder may not fit in memory on this
machine. "It does not fit here" is a valid outcome and is recorded, not worked around.

**Phase 5 — ADR and documentation.**

## 8. Acceptance criteria

1. `consumer_abstention@5` stays at 1.0. Both out-of-scope cases (`conflito_entre_vizinhos`,
   `salario_atrasado`) still abstain with no category available to the gate.
2. `consumer_retrieval_success@5` stays at 1.0.
3. `consumer_inactive_provision_rate@5` and `consumer_unknown_status_rate@5` stay at 0.0.
4. On the Bradesco case, no ground from `hard_negatives` is cited, and at least one of the three
   `relevant` provisions is.
5. The CDC anchor still holds: every notice that cites a complementary ground also cites a CDC one.
6. No occurrence of `issue_category`, `ConsumerIssueCategory`, `intake_category`, `issue_label`,
   `CATEGORY_LABELS`, `_CATEGORY_EXPANSIONS` or `infer_retrieval_category` remains in `app/`,
   `frontend/`, `tests/` or `eval_data/`.
7. The full suite passes, and the notice-path evaluation runs without a lexical-only fallback.
8. Phase 4 reports a measured decision on the reranker, whether or not it is enabled.

## 9. Version identifiers

| Identifier | From | To |
|---|---|---|
| `QUERY_BUILDER_VERSION` | `consumer-legal-three-query-v4` | `consumer-legal-two-query-v5` |
| `queries_per_case` | 3 | 2 |
| dataset version | 1.2.0 | 2.0.0 |
| `dataset_sha256` | `b2b45282…` | recomputed |
| corpus release | `br-consumer-law-2026-09-12-v4` | unchanged |
| chunking version | `legal-hierarchy-v3:target=1200` | unchanged |
| policy version | `consumer-notice-scope-eligibility-v3` | unchanged |

CI gates to re-baseline on measured values: `consumer_recall@5`, `consumer_article_recall@5`,
`consumer_ndcg@5`, `consumer_hard_negative_rate@5`, and the notice known-bad ceiling.

## 10. Risks

| Risk | Mitigation |
|---|---|
| The single lexicon does not preserve the routed expansion's benefit. Untested (4.3). | Phase 1 measures it first, with an explicit stop rule. |
| The scope gate weakens without its category parameter and an out-of-scope case reaches a notice. | Acceptance criterion 1; the two abstention cases are gates, not assertions. |
| The reranker does not fit in memory next to the 4B embedder. | Phase 4 treats this as a possible result. Default stays `NONE`. |
| Re-baselining gates one day after the v4 re-baseline hides a real regression behind a moving floor. | Gates move only to measured post-change values, and the Bradesco case adds a floor the aggregates cannot smooth over. |
| Two queries instead of three reduces candidate diversity before RRF. | Measured in phase 1, which runs the two-query builder end to end. |

## 11. Follow-ups

- The contested debt amount is booked as `direct_loss_amount`: on the Bradesco case the
  R$ 9.208,80 the supplier demands became the consumer's "prejuízo direto" and then the public
  settlement proposal. Separate defect, separate fix.
- A fact-to-ground coherence gate: with no payment confirmed in the facts, CDC art. 42 § único and
  the Civil Code's undue-payment chapter should be ineligible regardless of retrieval rank. This
  would have removed four of the five wrong grounds on its own.
- Treat exemption-of-liability provisions (CC art. 880 and its kind) as non-citable in a consumer's
  own notice, the way ADR 0016's Título VI exclusion is handled in `legal_policy.py`.
- Duplicated blocks in the rendered notice: the facts section prints the raw and rewritten account,
  and the requests section repeats the consumer's own text.
