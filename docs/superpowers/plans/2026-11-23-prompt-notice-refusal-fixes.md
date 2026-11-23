# Prompt-Notice Refusal Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Long, real prompt complaints get a notice when the consumer states
a remedy or names an article. When no notice is possible, the refusal says
why, says whether a retry can help, and keeps its audit.

**Architecture:**
- The intake uses a broader rule to recognise request verbs.
- A new pure module resolves the consumer's own statutory citations against
  the corpus, and `select_legal_grounds` merges them ahead of retrieved
  grounds, so the service and the evaluator stay identical.
- A background lifespan task warms the lazily loaded query model.
- Refusals become a stored, typed `NoticeRefusal` with reason codes and
  hints, served by its own endpoint.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, pytest (asyncio auto), ruff
(C90 ≤ 10), mypy strict, branch coverage at 100% for the listed modules.

**Spec:** `docs/superpowers/specs/2026-11-23-prompt-notice-refusal-fixes-design.md`

## Global Constraints

- Messages to the consumer are in Portuguese.
- No LLM call is added to intake or retrieval.
- When a case has no consumer citation, `select_legal_grounds` output is
  byte-identical to `main`.
- `LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v7"`.
- New `app/` modules join `[tool.coverage.run] source` and are 100% branch
  covered.
- Never commit the real complaint text. Tests use an anonymised, invented
  long complaint.
- Verify with `.venv/Scripts/python.exe -m pytest -n auto --cov
  --cov-report=term-missing`, plus `ruff check app tests frontend`, `mypy
  app`, `lint-imports` and `vulture app --min-confidence 90`.

## Review Focus

1. **"meu pedido não chegou"** (order sense) must stay in the account. Test it
   in Task 1.
2. **A citation without a law name** ("o art. 35 garante…") resolves to
   nothing. Test it in Task 2.
3. **Uncitable targets.** A defense unit (CDC art. 14 § 3), a vetoed unit (CDC
   art. 6 IX) or a criminal article (CDC art. 61) is never cited. Test it in
   Task 2.
4. **A Civil-Code-only consumer citation** is dropped by the CDC anchor. Test
   it in Task 3.
5. **A degraded refusal** stays 503 with a retryable message, while a
   deterministic one is 422. Test it in Task 5.

---

### Task 1: Request verbs and an explicit remedy field

**Files:**
- Modify: `app/consumer/intake.py:122-176`, `app/consumer/service.py:358-373`, `app/api/consumer_routes.py:321-345`
- Test: `tests/test_consumer_intake.py`, `tests/test_consumer_prompt_notice.py`

**Interfaces:**
- Produces:
  - `ConsumerCaseService.start_prompt_case(text: str, *, desired_resolution: str | None = None) -> tuple[str, str]`
  - the form field `desired_resolution` (max 2,000 chars) on `POST /consumer/prompt-notices`.

- [ ] Step 1: failing tests:
  - `test_first_person_request_verbs_move_the_sentence_to_the_resolution`, parametrized over "Gostaria de…", "Eu queria…", "Peço…", "Peco…", "Exijo…", "Requeiro…", each after the account "A loja entregou um modelo diferente do anunciado.";
  - `test_order_and_past_requests_stay_in_the_account` ("Meu pedido não chegou até hoje. Minha solicitação de troca foi ignorada. A empresa solicitou mais prazo." gives `desired_resolution is None`);
  - `test_an_explicit_resolution_field_is_the_request` (prompt route, account = first NUBANK sentence, field = second sentence: the request is the field, `facts_summary` is the text, and the field is a case message);
  - `test_an_explicit_resolution_keeps_request_sentences_in_the_account` (service level: text with "Quero…" plus a field gives `complaint_summary` = whole text).
- [ ] Step 2: run them and see them fail.
- [ ] Step 3: replace `_REQUEST_TERMS` with `_REQUEST_RE = re.compile(r"\b(?:quero|queria|gostaria|desejo|solicito|pe[çc]o|exijo|requeiro)\b", re.IGNORECASE)`.
- [ ] Step 4: `start_prompt_case` records the field as a second user message, then sets `complaint_summary` to the normalised text and `desired_resolution` to the normalised field. The route passes the form field through.
- [ ] Step 5: the tests pass. Commit `feat(consumer): recognise more request verbs and accept an explicit remedy`.

### Task 2: Resolve the consumer's own statutory citations

**Files:**
- Create: `app/consumer/cited_provisions.py`
- Modify: `app/consumer/legal_corpus.py` (public `citation_chunk`), `pyproject.toml` (coverage source)
- Test: `tests/test_consumer_cited_provisions.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class ConsumerCitation: provision: LegalProvision; unit: LegalTextUnit | None; chunk: Chunk; whole_article: bool`;
  - `consumer_cited_provisions(corpus: LegalCorpus, facts: ConsumerCaseFacts) -> list[ConsumerCitation]`, in order of first mention, from the complaint then the remedy;
  - `LegalCorpus.citation_chunk(provision: LegalProvision, unit: LegalTextUnit | None) -> Chunk | None`, the indexed `part-01` chunk or None.
- Matching works on text folded by NFKD with accents dropped and casefold. Laws:
  - `br-cdc`: cdc, "código de defesa do consumidor", "código do consumidor", lei 8.078[/90];
  - `br-lgpd`: lgpd, "lei geral de proteção de dados[ pessoais]", lei 13.709[/2018];
  - `br-cc`: "código civil", cc[/02], lei 10.406[/2002].
- Resolution:
  - the article must be `retrievable_provisions()` and active;
  - the unit is matched on `kind`, `paragraph`, `inciso` and `alinea`;
  - a whole-article citation resolves to the caput, or to the provision without units;
  - the chunk must exist through `citation_chunk`;
  - `alias_exclusion_reason(key, corpus.alias_source_block(provision, unit))` must be None;
  - a whole-article citation is dropped when the same article is also cited by unit, and duplicates collapse.

- [ ] Step 1: failing tests. Each expected list holds the unit ids.
  - `test_the_tablet_case_citation_resolves_to_both_articles`: "…nos termos dos artigos 30 e 35, inciso I, do Código de Defesa do Consumidor (CDC)" gives `[br-cdc-art-30-caput, br-cdc-art-35-inciso-i]`, and the first is a whole-article citation.
  - `test_paragraph_inciso_and_reverse_order_forms`, parametrized:
    - "art. 18, § 1º, II, do CDC" gives `br-cdc-art-18-paragrafo-1-inciso-ii`;
    - "o § 1º do art. 18 do CDC" gives `br-cdc-art-18-paragrafo-1`;
    - "parágrafo único do artigo 42 do CDC" gives `br-cdc-art-42-paragrafo-unico`;
    - "art. 6º, inciso III, da Lei 8.078/90" gives `br-cdc-art-6-inciso-iii`;
    - "art. 54-F do CDC" gives `br-cdc-art-54-f-caput`;
    - "art. 18 da LGPD" gives `br-lgpd-art-18-caput`;
    - "art. 186 do Código Civil" gives `br-cc-art-186-caput`;
    - "incisos I e II do art. 35 do CDC" gives the two incisos;
    - "arts. 18, 19 e 20 do CDC" gives the three caputs.
  - `test_citations_that_resolve_to_nothing`, parametrized, each giving `[]`:
    - "o art. 35 garante a troca";
    - "art. 999 do CDC";
    - "art. 61 do CDC";
    - "art. 6º, IX, do CDC";
    - "art. 14, § 3º, do CDC";
    - "arts. 18 a 25 do CDC";
    - "o CDC, em seu art. 35".
  - `test_a_whole_article_citation_yields_to_a_unit_citation_of_the_same_article`.
  - `test_repeated_citations_collapse`.
- [ ] Step 2: run them and see them fail.
- [ ] Step 3: implement. Small functions keep C90 ≤ 10: clause regex, tokenizer, a reference walker that refines the last reference, and the resolver.
- [ ] Step 4: the tests pass, and the module has 100% branch coverage. Commit `feat(consumer): resolve statutory citations the consumer writes`.

### Task 3: Consumer-cited grounds in the shared selector

**Files:**
- Modify: `app/consumer/ground_selection.py`, `app/consumer/schemas.py` (`LegalGround.cited_by_consumer: bool = False`), `app/consumer/legal_policy.py` (v7 + docstring)
- Test: `tests/test_ground_selection.py`, `tests/test_alias_support_cap.py:98`, `tests/test_consumer_api.py:168`

**Interfaces:**
- Consumes: Task 2's `consumer_cited_provisions`.
- Produces: `select_legal_grounds` keeps its signature. The retrieval half moves unchanged into `_retrieved_grounds(...) -> list[tuple[LegalProvision, LegalGround]]`, and `_with_consumer_citations(corpus, citations, retrieved)` merges the two.
- Rationale texts:
  - cited only: "O(a) consumidor(a) indicou {label} no próprio relato; o texto oficial foi localizado no corpus legal. Sua aplicabilidade ao caso não foi decidida pelo sistema e deve ser validada por profissional habilitado contra os fatos e documentos citados."
  - cited and retrieved: the same, with "…no próprio relato, e a política de recuperação também o localizou a partir do relato. Sua aplicabilidade…".

- [ ] Step 1: failing tests:
  - `test_a_consumer_cited_article_is_a_ground_without_retrieval_agreement`: no traces gives `[br-cdc-art-49-caput]` with `cited_by_consumer` set and `retrieval_rank is None`;
  - `test_a_matching_retrieved_unit_keeps_its_rank_and_is_marked_cited`;
  - `test_the_consumers_unit_replaces_a_different_retrieved_unit_of_the_same_article`;
  - `test_a_whole_article_citation_keeps_the_retrieved_unit`;
  - `test_consumer_citations_come_first_and_retrieved_grounds_follow`;
  - `test_out_of_scope_complaints_get_no_consumer_cited_grounds`;
  - `test_a_civil_code_only_citation_needs_a_cdc_ground`;
  - `test_consumer_cited_complementary_grounds_are_capped`;
  - the version pins become v7.
- [ ] Step 2: run them and see them fail.
- [ ] Step 3: implement. Ordering is the consumer's citations, then the retrieved grounds for other articles, then `precedence_window(window=MAX_LEGAL_GROUNDS, lexical_only=False)`, then `enforce_cdc_anchor`.
- [ ] Step 4: the full ground-selection, notice-evaluation and API tests pass. Commit `feat(consumer): cite the provisions the consumer names (policy v7)`.

### Task 4: Warm the query model at startup

**Files:**
- Modify: `app/rag/embeddings.py`, `app/rag/pipeline.py`, `app/consumer/service.py`, `app/api/main.py`, `app/api/routes.py`, `app/core/config.py`, `.env.example`
- Test: `tests/test_embeddings.py`, `tests/test_rag.py` (or the closest pipeline test file), `tests/test_consumer_api.py`

**Interfaces:**
- Produces:
  - `WarmableEmbeddingClient` Protocol (`async def warm_up(self) -> None`, `@property def warm(self) -> bool`), implemented by `SentenceTransformerEmbeddingClient`, which becomes warm after its first successful encode;
  - `RagPipeline.warm_up_query_embedding() -> bool`, False when nothing needs warming;
  - `RagPipeline.query_embedding_warm -> bool`;
  - `ConsumerCaseService.warm_up_query_embedding() -> None`, which never raises;
  - `ConsumerCaseService.query_embedding_warm -> bool`;
  - `Settings.embedding_warm_up_on_startup: bool = True`;
  - `/health` gets `"query_embedding_warm"`.

- [ ] Step 1: failing tests:
  - `test_sentence_transformer_warm_up_encodes_one_query` (fake model);
  - `test_pipeline_warm_up_skips_eager_embedders`;
  - `test_pipeline_warm_up_calls_a_warmable_embedder`;
  - `test_service_warm_up_failure_is_logged_not_raised`;
  - `test_startup_warms_the_query_model_unless_disabled`;
  - `test_health_reports_query_embedding_warmth`.
- [ ] Step 2: run them and see them fail. Step 3: implement. The lifespan starts the task after `legal_corpus_ready()` and cancels and awaits it on shutdown.
- [ ] Step 4: the tests pass. Commit `feat(rag): warm the local query model at startup`.

### Task 5: Actionable, audited refusals

**Files:**
- Create: `app/consumer/refusals.py`
- Modify:
  - `app/consumer/schemas.py` (`NoticeRefusalReason`, `NoticeRefusal`);
  - `app/consumer/store.py` (`last_refusal`);
  - `app/consumer/retrieval.py` (`truncated_narrative_fields`);
  - `app/consumer/service.py` (`ConsumerNoticeRefusedError`, `get_notice_refusal`, replacing `_require_grounding`);
  - `app/api/consumer_routes.py`;
  - `frontend/api_client.py` (hints);
  - `pyproject.toml`.
- Test: `tests/test_consumer_refusals.py`, `tests/test_consumer_prompt_notice.py`, `tests/test_consumer_citation_integrity.py:588-603`, `tests/test_ground_verifier.py:253-297`, `tests/test_frontend_entrypoint.py`

**Interfaces:**
- Produces:
  - `notice_refusal(*, case_id, mode, facts, legal_traces, evidence_traces, supported_chunks: int, selected_grounds: int, verified_grounds: int, evidence_required: bool, evidence_cited: int, corpus_release_id, corpus_sha256) -> NoticeRefusal`;
  - `ConsumerNoticeRefusedError(RuntimeError)` with `.refusal`;
  - `GET /consumer/cases/{case_id}/notice/refusal` returns `NoticeRefusal`, or 404 with "Refusal not found".
- HTTP: 422 when `retryable` is false, otherwise 503. The detail is `{"message", "reasons", "hints", "refusal_url"}`, and on the prompt route also `"case_id", "case_token"`, with the credential headers too.

- [ ] Step 1: failing tests:
  - unit tests for each reason, its order, the truncation hint numbers, `retryable` and the message wording;
  - `test_a_refused_prompt_keeps_the_case_and_says_why` (gate patched to empty: 422, reasons, credentials, refusal endpoint traces, then delete);
  - `test_the_long_anonymised_complaint_cites_what_the_consumer_named`, which gives 201 with 30 caput and 35 I `cited_by_consumer`;
  - the two updated legacy refusal tests (degraded stays 503, the verifier case becomes 422);
  - the frontend detail with hints.
- [ ] Step 2: run them and see them fail. Step 3: implement. The service computes `strongly_supported_chunk_ids(legal_traces)` through its module-level name, so the existing patch point still works, and clears `last_refusal` on success.
- [ ] Step 4: the tests pass. Commit `feat(consumer): actionable notice refusals with a kept audit`.

### Task 6: Docs and whole-branch verification

**Files:** `docs/adr/0025-consumer-cited-provisions-and-actionable-refusals.md`, `docs/api-consumer.md`, README(s) where the prompt route or the 503 is described.

- [ ] Write ADR 0025 (decisions, measured evidence, consequences, follow-ups), and update the API docs.
- [ ] Run the full suite, lint, mypy, lint-imports and vulture. The suite also runs on Python 3.10 (uv venv, see memory).
- [ ] Run the four offline evaluations from a clean export of the branch, and diff them against `scratchpad/eval-base`. Only `ground_policy_version` may differ.
- [ ] Re-run the real complaint (scratchpad only) through `start_prompt_case` and `generate_prompt_notice` on the configured Postgres stack, in degraded mode (no model load), and report the grounds. Ask before loading JUÁ for a healthy-mode run.
- [ ] Commit the docs, then offer the finishing menu.
