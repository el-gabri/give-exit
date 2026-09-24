# LGPD and Civil Code in the Consumer legal corpus — design

- Date: 2026-09-12
- Status: approved section by section in brainstorming; pending review of this written spec
- Classification: architectural — amends ADR 0012 §3, ADR 0013 §1 and ADR 0014
- Evidence: measurements taken on 2026-09-12 against this checkout and the official Planalto pages (section 4)

## 1. Goal

Add the Lei Geral de Proteção de Dados Pessoais (LGPD, Lei nº 13.709/2018) and the Código Civil
(CC, Lei nº 10.406/2002) to the versioned legal corpus and to the vector index used by the Consumer
extrajudicial-notice journey. Keep every existing provenance guarantee (official compiled Planalto
text, pinned snapshots, content hashes, canonical citation reconstruction) and keep the CDC as the
primary authority of every notice.

## 2. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Role of the Civil Code | Subsidiary source for consumer notices. Only PARTE GERAL and PARTE ESPECIAL › LIVRO I (Do Direito das Obrigações) are indexed and citable; every other book stays in the corpus as `audit_only`. |
| D2 | LGPD scope | Whole law indexed; citation eligibility decided by chapter. |
| D3 | Precedence | CDC anchor plus cap: LGPD/CC grounds appear only when at least one CDC ground is selected, and at most 3 complementary grounds per notice. |
| D4 | Degraded retrieval | While any legal trace is `lexical_only`, LGPD and CC are never cited. |
| D5 | Ingestion | One generic Planalto statute parser driven by a declarative `StatuteSpec` per law (CDC, LGPD, CC). |
| D6 | Vector reuse | In scope: reuse vectors by chunk-text hash from verified earlier embedded generations, guarded by a canary. |
| D7 | Final-citation evaluation | Phase 0 of this project: expose the ground selector as a pure function and evaluate final grounds before and after the expansion. |
| D8 | Non-consumer disputes | Out of scope: the consumer-scope gate is unchanged. |

## 3. Non-goals

- The other findings of the local RAG review (`plan.md`, 2026-09-07): factual-support predicates for
  grounds (finding 1), degraded-mode abstention for CDC/CF grounds (finding 2 beyond D4), the evidence
  chunker (finding 3), trace score labels and query formatter (finding 5), cancellation slot
  ownership (finding 6).
- New intake categories, or UI changes other than the disclaimer text.
- Indexing the whole Civil Code, a curated CC article list, or additional CF provisions.
- Changing the embedding model or revision, the chunk size, or the chunk text of existing CDC/CF units.
- One vector document per law: the corpus remains a single `doc_id`.
- Legal validation. The CC scope, the LGPD eligibility rules and the new golden labels remain
  `requires_legal_review`.

## 4. Evidence gathered on 2026-09-12

### 4.1 Official sources

The pages were downloaded to a scratch directory only; the implementation downloads them again
through the refresher (section 6.1).

| | LGPD | CC |
|---|---|---|
| URL | `https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm` | `https://www.planalto.gov.br/ccivil_03/leis/2002/l10406compilada.htm` |
| Size and encoding | 230,913 bytes, windows-1252 | 923,899 bytes, windows-1252 |
| Article headings | 80: arts. 1–65, 55-A…55-M, 58-A, 58-B | 2,083: arts. 1–2.046 plus 46 suffixed, minus 1.621–1.629 |
| Heading levels | CAPÍTULO 10, SEÇÃO 14 | PARTE 2, LIVRO 9, TÍTULO 43, SUBTÍTULO 8, CAPÍTULO 176, SEÇÃO 153, SUBSEÇÃO 15 |
| Most recent amending law in the page | Lei nº 15.352/2026 (Cap. IX rewritten; ANPD becomes the Agência Nacional de Proteção de Dados) | Leis nº 14.905, 15.040 and 15.068, all of 2024 |

- Neither page carries superseded text. Their only `<strike>`/`<s>` elements wrap whitespace or the
  ordinal "º", as in the pinned CDC snapshot.
- Source quirks the parser must handle:
  - "P A R T E G E R A L" with spaced letters;
  - "Art 1.636." without the dot after "Art";
  - "Art. 759.(Revogado…" with no space after the dot (the article pattern accepts "(");
  - "Art. 1.620. a 1.629. (Revogados pela Lei nº 12.010, de 2009)": ten revoked articles in one
    paragraph;
  - LGPD "Art. 5 7. (VETADO)." for art. 57;
  - "Brasília , 14 de agosto" with a space before the comma;
  - unaccented "TITULO" and "CAPITULO" headings;
  - headings whose name comes on following paragraphs, sometimes with a standalone
    "(Redação dada…)" note in between;
  - the SUBTÍTULO level (CC Livro IV).
- Planalto's F5 WAF resets connections carrying the refresher's current user agent
  (`give-exit-legal-corpus-maintainer/1.0`) and accepts a browser user agent. Every response appends
  a random `<script id="f5_cspm">` after `</html>`: two downloads of the LGPD page had the same size,
  different bytes and identical extracted paragraphs. The pinned CDC snapshot contains the same
  script.
- With the rules of section 6.2, all three laws produce zero heading-like or all-uppercase
  paragraphs inside article text, and the CDC's 33 headings come out unchanged.

### 4.2 Size of the index

| Set | Provisions | Chunks |
|---|---:|---:|
| Current corpus (130 CDC + 7 CF) | 137 | 472 (measured) |
| LGPD, whole law | 80 | ≈ 488 |
| CC PARTE GERAL (Livros I–III) | 235 | ≈ 497 |
| CC PARTE ESPECIAL › Livro I (Obrigações) | 736 | ≈ 1,000 |
| Other CC books (`audit_only`, not indexed) | 1,112 | (≈ 2,370 not indexed) |
| **New corpus and index** | **2,300** | **≈ 2,457** |

The estimates assume a 260-character chunk header. The implementation records exact counts.

### 4.3 Embedding cost

- The active embedded generation `7a7b20a89b67c75d` holds 472 chunks, `ufca-llms/jua-4B-mixed` at
  revision `57f491c1…`, 2,560 dimensions, shard size 25, built on CPU (Intel Family 6 Model 165,
  win32). Its manifest shows 2 days 2 hours between creation and validation, pauses included;
  per-shard timing is not recorded. Its artifacts take 3.8 MB.
- `ParsedDocument.doc_id` is the SHA-256 of the full corpus text, so any corpus change renames every
  chunk id and today forces a full re-embedding. Under this design the CDC/CF chunk texts do not
  change, which makes their 472 vectors reusable.

### 4.4 Performance

- `ParsedDocument.doc_id` rejoins every page and rehashes the whole text on each access.
  `LegalCorpus.as_chunks()` reads it 1,015 times for 472 chunks (0.51 s). Projected for the new
  corpus: about 22 s per call; with the id computed once: about 0.06 s.
- Extracting the paragraphs of the CC page takes 0.13 s.

### 4.5 Ground-selection baseline

From `plan.md` (2026-09-07), on the offline mock path at k = 8 over the 15 golden cases: 79 final
grounds, 4 labelled hard negatives cited, macro final-ground exact recall 0.3333. The CI retrieval
gates never call `_legal_grounds`.

## 5. Phases

0. Final-ground evaluator and pure selector, baselined on the current corpus.
1. Generic statute parser, specs and snapshots: CDC migration first, then LGPD and CC.
2. Data model and corpus.
3. Eligibility, precedence and retrieval vocabulary.
4. Vector reuse and the `preindex_legal` CLI.
5. Golden dataset, CI gates and documentation; reindex and rollout.

Every phase leaves the full test suite and the CI gates green. Delivery groups them into three pull
requests (plans in `docs/superpowers/plans/2026-09-12-plan-{a,b,c}-*.md`):

- **A:** Phase 0.
- **B:** Phase 4 (vector reuse), merged before any reindex.
- **C:** Phases 1, 2, 3 and 5.

Grouping C this way means the corpus release changes once, and the precedence rules land together
with the new sources.

## 6. Design

### 6.0 Phase 0 — final-ground evaluator

- Move the logic of `ConsumerCaseService._legal_grounds`, unchanged, into
  `app/consumer/ground_selection.py` as
  `select_legal_grounds(corpus, facts, result_sets, traces) -> list[LegalGround]`. The helpers and
  constants it needs move with it: `_merge_results`, `_chunk_query_occurrences`,
  `MAX_GROUND_CANDIDATES`, `MIN_GROUND_SCORE_RATIO`, `MAX_LEGAL_GROUNDS` and the issue-label map used
  by `application_to_facts`. Other callers import them from the new module. The service method stays
  as a thin wrapper, so existing tests and the `docs/rag-review` probes keep working.
- Add a notice-path mode to the runner:
  `python -m app.evaluation.consumer_runner --evaluate-notice [--notice-pipeline offline|configured] [--require-semantic] --output notice-results.json`.
  - `offline` (default): `RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())` with
    `corpus.as_chunks()` indexed once.
  - `configured`: `create_consumer_rag(Settings())`; the run fails if the legal index is not ready.
  - Per case: scope gate, then
    `retrieve_many_with_traces(build_legal_queries(facts), doc_id=…, agent="consumer_legal_authorities", k=8, mode="hybrid")`,
    then `select_legal_grounds`.
  - Totals:
    - `consumer_notice_grounds`;
    - `consumer_notice_known_bad_citations` (a ground whose provision or unit id is one of the case's
      hard negatives);
    - `consumer_notice_complementary_grounds` (grounds with `law_id` in `br-lgpd`, `br-cc`).
  - Macro means:
    - `consumer_notice_exact_recall` (over applicable cases; a unit judgment matches by unit id, an
      article-only judgment by article id);
    - `consumer_notice_abstention` (no-applicable-ground cases with zero grounds);
    - `consumer_notice_semantic_success` (cases with no degraded trace).
  - `--require-semantic` fails the run on any degraded trace. `--min` and `--max` gates work on
    these metrics.
  - Run metadata: dataset hash, corpus release and hash, query-builder version, ground-policy
    version, k.
- Acceptance: on the unchanged corpus the offline mode reproduces 79 / 4 / 0.3333. Any deviation is
  explained before Phase 1 starts. CI gets a step with
  `--max consumer_notice_known_bad_citations=4`.

### 6.1 Statute snapshots

- A new package `app/consumer/statutes/` holds `spec.py`, `registry.py`, `snapshot.py` and
  `parser.py`. It replaces `app/consumer/cdc_snapshot.py`.
- `app/consumer/update_cdc_snapshot.py` becomes
  `python -m app.consumer.update_statute_snapshot --law {cdc,lgpd,cc}`.
- Data layout: `app/consumer/data/{cdc,lgpd,cc}/`, each with the official file (`l8078compilado.html`,
  `l13709compilado.html`, `l10406compilada.html`) and its `manifest.json`.
- Configuration:
  - `.gitattributes`: `app/consumer/data/**/*.html binary`, so line-ending conversion cannot break
    the pinned SHA-256;
  - `pyproject.toml` package-data: `data/*/*.html` and `data/*/*.json` (today only `data/cdc/*` is
    packaged).
- `StatuteSpec` is a frozen dataclass with:
  - `law_id`, `source`, `source_name`, `citation_prefix`;
  - `source_url`, `snapshot_file`, `encoding`;
  - `last_article`, `expected_article_count`, `required_articles`;
  - `known_absent` (article number → reason);
  - `text_corrections` (find, replace, reason);
  - `index_scope` (division selectors; `None` means the whole law).

| | CDC | LGPD | CC |
|---|---|---|---|
| `law_id` | `br-cdc` | `br-lgpd` | `br-cc` |
| Last article | 119 | 65 | 2.046 |
| Provisions | 130 | 80 | 2,083 |
| Known absent | — | — | 1.621–1.629, revoked in bloc in the "Art. 1.620. a 1.629." paragraph |
| Corrections | — | "Art. 5 7. (VETADO)." → "Art. 57. (VETADO)." | "P A R T E G E R A L" → "PARTE GERAL"; "Art 1.636." → "Art. 1.636." |
| Index scope | whole law | whole law | PARTE GERAL; PARTE ESPECIAL › LIVRO I |
| Citation prefix | CDC | LGPD | Código Civil |

- Snapshot manifest, schema 3 (`StatuteSnapshotManifest`):
  - keeps every field of the current CDC manifest;
  - adds `parsed_text_sha256` (SHA-256 of the extracted paragraphs, newline-joined) and
    `http_user_agent`;
  - `law_id` must exist in the registry, and `source_url` must equal the spec's;
  - `parser_version` must equal the runtime parser, `planalto-statute-parser-v1`.
- The CDC manifest is edited in place: new schema, parser version and `parsed_text_sha256`, and
  `http_user_agent: null` because it was not recorded at acquisition. The CDC HTML bytes are
  untouched.
- The refresher:
  - downloads with `urllib` and a pinned browser-compatible user agent, which is recorded in the
    manifest;
  - keeps the `--source-file` and `--acquisition-note` path;
  - if the parsed text equals the pinned snapshot's, reports "no textual change" and writes nothing
    unless `--force` is given;
  - writes new snapshots as `pending_review`. They become `engineering_validated` only after the
    completeness validation and a sampled comparison with the official page.

### 6.2 Parser rules, identical for every law

1. **Heading levels:** PARTE, LIVRO, TÍTULO, SUBTÍTULO, CAPÍTULO, SEÇÃO and SUBSEÇÃO, recognized
   without regard to accents or case. Each is followed by a roman numeral (optionally "-A"), ÚNICO,
   ÚNICA, GERAL, ESPECIAL or COMPLEMENTAR. A level resets every level below it.
2. **Paragraphs between a heading and the next article:**
   - all-uppercase → appended to the heading label;
   - standalone editorial note (`(Redação dada…)`, `(Incluído…)` and similar) → skipped;
   - the first mixed-case paragraph after a bare heading → appended as its name. A heading is bare
     when, with inline notes removed, it is only the level and the numeral;
   - anything else → parse error.
3. **Article headings:** `Art.`, the number with optional thousands separators, an optional ordinal
   (º, ° or "o", with or without a preceding space) an optional `-LETTER` suffix and an optional dot, followed by whitespace or "(". Numbers are
   stored without separators. The sequence is strict: the next number, or the next suffix letter
   after the base article or the previous suffix. Only spec-declared absences may break it; any
   other break is a parse error.
4. **End of text:** the first paragraph matching `^Brasília\s*,` after the spec's last article.
   Its absence is a parse error.
5. **Text corrections:** exact paragraph-prefix replacements. Each must apply exactly once, or the
   parse fails. This catches Planalto fixing its own HTML.
6. **Units, statuses and amendments:** unit segmentation, VETADO/REVOGADO statuses and
   amendment-only detection keep the current CDC logic.
7. **Completeness:** every article number from 1 to the last minus the known absences, every
   required suffixed article, and exactly `expected_article_count` provisions.
8. **CDC guarantee:** the generic parser reproduces every CDC provision and unit byte for byte. A
   test pins a digest computed with the current parser before the refactor.

### 6.3 Data model and corpus

- **Sources:** `LegalSource` gains `DATA_PROTECTION_LAW = "data_protection_law"` and
  `CIVIL_CODE = "civil_code"`.
- **ID patterns:** in `LegalTextUnit`, `LegalProvision` and `LegalAuthorityCitation` they accept
  `br-(cf|cdc|lgpd|cc)`. The default `law_id` comes from a source → law map instead of the current
  CF/CDC conditional, and a validator requires `provision_id` to start with `law_id`.
- **IDs and labels:**
  - IDs drop the thousands separator: `br-cc-art-1358-a-paragrafo-1`, `br-lgpd-art-55-a-inciso-i`;
  - labels keep it: "Código Civil, art. 1.358-A", "LGPD, art. 18";
  - source names: "Lei Geral de Proteção de Dados Pessoais (Lei nº 13.709/2018)" and
    "Código Civil (Lei nº 10.406/2002)".
- **Hierarchy:** optional `part`, `book`, `subtitle` and `subsection` join `title`, `chapter` and
  `section` in `LegalProvision`, `LegalAuthorityCitation`, the chunk metadata, the page text and the
  corpus-hash payload. The chunk-header breadcrumb lists a level only when it is present, so CDC and
  CF chunk texts do not change.
- **Index scope:** `LegalProvision` gets `index_scope: Literal["indexed", "audit_only"]`, set from
  the spec and included in the corpus hash. `as_chunks()` and `retrievable_provisions()` skip
  `audit_only` provisions, as they already skip amendment-only articles (LGPD art. 60, which amends
  the Marco Civil, is one).
- **Corpus:**
  - release `br-consumer-law-YYYY-MM-DD-v4`, where the date is the `retrieved_on` of the LGPD and CC
    snapshots, refreshed on the same day;
  - `verified_on` per law, from its manifest; CF keeps 2026-08-04;
  - corpus-hash payload `schema_version: 2`, listing every snapshot manifest ordered by `law_id`;
  - provenance validation for every snapshot-backed law;
  - `as_parsed_document()` warnings describing each source;
  - chunking identity `legal-hierarchy-v3:target=1200`.
- **Summaries:** LGPD and CC provisions use the caput extract. The extractor adopts the parser's
  article pattern; today it does not strip "Art. 1.358-A." or "Art. 3 o".
- **Performance fixes:**
  - `LegalCorpus` computes the document id once and uses it in `as_chunks()`,
    `provisions_for_chunk()` and the canonical chunk map;
  - `CURATED_PROVISIONS` is no longer built at import time but lazily inside
    `get_default_legal_corpus()`.

### 6.4 Eligibility, precedence and retrieval

**Eligibility** (`provision_is_eligible`) stays structural:
- CDC and CF: unchanged.
- LGPD: citable in Caps. I–III and V–VII. Not citable in Cap. IV (tratamento pelo Poder Público),
  VIII (sanções administrativas), IX (Agência and Conselho) or X (disposições finais e
  transitórias).
- CC: citable if and only if `index_scope == "indexed"`. An `audit_only` provision is never
  citable, even if a stale index still contains it.

**Selection** is a pure function in `legal_policy.py`, applied by `select_legal_grounds`:
1. Candidates keep their rank order. LGPD and CC are complementary sources.
2. If any legal trace has `degraded_mode == "lexical_only"`, complementary candidates are removed
   first.
3. The 8-candidate window admits at most 3 complementary candidates. Further complementary
   candidates are skipped without consuming window slots, so the CC cannot push CDC articles out of
   the window.
4. The score floor and the limit of 8 grounds are unchanged.
5. If no CDC ground survives, complementary grounds are dropped. If nothing remains, the existing
   "fundamentos jurídicos" readiness error applies. CF grounds keep today's behaviour.
6. With no complementary candidates the output is identical to the current selector.

`LEGAL_GROUND_POLICY_VERSION` becomes `consumer-notice-scope-eligibility-v3`.

**Retrieval:**
- New subcategory `personal_data`:
  - it comes last in `_SUBCATEGORY_SIGNALS`, so the more specific consumer subcategories still take
    priority;
  - specific phrases trigger it: "vazamento de dados", "vazaram meus dados", "compartilharam meus
    dados", "venderam meus dados", "excluir meus dados", "apagar meus dados", "lgpd", "proteção de
    dados" and similar;
  - its expansion uses vocabulary only, with no article numbers. It deliberately includes CDC terms
    (segurança do serviço, informação, cadastros e dados do consumidor), so a CDC anchor can be
    retrieved.
- Numeric anchors in the existing expansions are qualified with "CDC" ("CDC artigo 42",
  "CDC artigos 18 20"). The bare numbers now also match LGPD arts. 18, 20, 42, 43 and CC arts. 18,
  20, 42, 43, 104, all indexed. "CDC" appears in the header of every CDC chunk
  ("Citação: CDC, art. 42").
- No new intake category.
- `QUERY_BUILDER_VERSION` becomes `consumer-legal-three-query-v4`.
- The consumer-scope gate is unchanged.

### 6.5 Vector reuse

- A new module, `app/consumer/vector_reuse.py`, is used by
  `EmbeddingGenerationManager.build_and_activate`.
- **Sources:** generations under `embedding_artifacts_dir` qualify when:
  - status is `validated` or `active`;
  - `provenance == "embedded"`;
  - `contract.document_identity()` equals the build's. If the runtime dimension is unset, the
    sources must agree on it and supply it.

  `adopted_existing_vectors` generations are never sources. The generation being built is excluded;
  its own resume logic is unchanged.
- **Verification:** each source shard is verified in full: artifact SHA-256, the chunk-id, chunk and
  vector hashes recomputed and compared with its manifest, and the dimension. Invalid shards are
  skipped with a warning.
- **Index:** `sha256(chunk.text)` → float32 vector plus origin (generation id, chunk id, artifact
  SHA-256). Among duplicates the choice is deterministic: active before validated, then the newest
  activation.
- **Canary:** before any reuse, re-embed the 2 reused texts with the lowest hashes using the current
  model and require cosine ≥ 0.9999. Otherwise the build fails; the error is recorded in the
  manifest and the CLI suggests `--no-reuse`. This catches a document-formatter change made without
  a version bump, and cross-platform drift: the source was built on Windows, and the compose
  `indexer` runs Linux.
- **Shard assembly:** reused vectors, plus one `embed_document_batch` call per shard for the missing
  chunks. Reused JSONL lines carry `"source": {"generation_id", "chunk_id", "artifact_sha256"}`,
  covered by the artifact hash.
- **Manifest:** `embedding-generation-manifest-v2`; v1 manifests remain readable.
  - It adds `reused_chunk_count`, `reuse_sources` and `reuse_canary` (checked texts, minimum cosine,
    threshold), and shards gain `reused_chunk_count`.
  - `provenance` stays `embedded`: every vector was produced under this contract, either now or in
    a verified generation, and each line records its origin.
  - `generation_id` does not depend on reuse.
- **CLI:** `preindex_legal` reuses by default and prints the reused and embedded counts.
  `--no-reuse` disables reuse but keeps resume. `--force` restarts from scratch without reuse.

### 6.6 Rollout

1. Merge code and snapshots without restarting the API. With the new code the API would refuse
   notice generation until the new index exists; that is current behaviour.
2. Run `python -m app.consumer.preindex_legal` on the host, where the source generation was built,
   or `docker compose --profile tools run --rm indexer`.
   - Expected: 472 vectors reused and about 1,985 embedded, in resumable shards of 25.
   - This is about 4.2× the last job on this CPU. Use `LITIGATION_EMBEDDING_DEVICE=cuda` if a GPU is
     available.
3. Confirm that `python -m app.consumer.preindex_legal --check` exits 0.
4. Restart the API.
5. The old PostgreSQL namespace and old generations stay until removed manually. The new generation
   (about 20 MB) becomes the reuse source for later releases.
6. Run the configured retrieval benchmark and
   `--evaluate-notice --notice-pipeline configured --require-semantic`, and compare them with
   `configured.json`.

### 6.7 Tests, evaluation and CI

- **Parser:** a minimal HTML fixture for every rule in section 6.2, including each failure case.
- **Real snapshots:**
  - provision counts: 130, 80 and 2,083;
  - the known absences;
  - index-scope counts;
  - the CDC digest.
- **Corpus and schemas:** IDs, law IDs, the hierarchy fields, `index_scope`, and hash coverage of
  every manifest. Existing count assertions are updated (137 → 2,300 provisions).
- **Policy and selection:**
  - LGPD chapters;
  - `audit_only` never citable;
  - window semantics and the CDC anchor;
  - CF unchanged;
  - the degraded block;
  - identical output for CDC-only candidate lists.
- **Retrieval:** `personal_data` inference and its position in the signal list, and the qualified
  numeric anchors.
- **Vector reuse:**
  - only missing chunks are embedded (counting embedder calls);
  - provenance is recorded per line;
  - adopted sources are ignored;
  - a different document identity means no reuse;
  - a corrupted source shard is skipped;
  - a failed canary aborts the build;
  - `--no-reuse` works;
  - v1 manifests remain readable.
- **Refresher:** the user agent is recorded, unchanged text writes nothing, and new snapshots are
  `pending_review`.
- **Code gates:** the coverage source list adds `app.consumer.statutes`,
  `app.consumer.vector_reuse` and `app.consumer.ground_selection` (100% gate). The ruff C901
  per-file ignore moves to the new parser.
- **Golden dataset 1.2.0:**
  - new target release and hash;
  - 6 new cases, all `requires_legal_review`. LGPD: data breach, ignored deletion request, sharing
    without consent. CC as complement: undue payment and unjust enrichment, late-payment interest,
    adhesion-contract clause;
  - LGPD art. 42 and CC art. 42 added as hard negatives to the charge cases, to catch
    numeric-anchor collisions.
- **Gates:**
  - the retrieval gates keep their thresholds, and `consumer_retrieval_success@5 = 1.0` also covers
    the new cases;
  - any threshold change requires explicit approval from the maintainer;
  - the notice gate starts at `--max consumer_notice_known_bad_citations=4`;
  - after the expansion, known-bad citations on the original 15 cases must not exceed 4, and the
    complementary-ground count is reported.

### 6.8 Documentation and texts

- **ADR 0016**, multi-statute corpus: LGPD, the scoped Civil Code, the CDC anchor and the degraded
  rule. It amends ADR 0012 §3 and ADR 0013 §1.
- **ADR 0017**, cross-generation vector reuse. It amends ADR 0014. The original ADRs get
  "Amended by" notes.
- **`docs/architecture.md`:** runtime diagram, legal provenance section, and the ADR index, which is
  also missing 0014 and 0015 today.
- **READMEs** (`README.md`, `README-pt.md`): corpus description, chunk count, refresher command,
  limitations.
- **UI texts:** the disclaimer in `frontend/consumer_view.py` and the caveat in
  `app/consumer/settlement.py` mention the LGPD and the Código Civil.
- **Historical evidence:** `results.json`, `configured.json` and `docs/rag-review/` stay untouched.

## 7. Acceptance criteria

1. CDC provisions and units are byte-identical to the current parser's (digest test).
2. LGPD (80) and CC (2,083) parse with the completeness validation and no unknown paragraphs
   between headings and articles.
3. The corpus has 2,300 provisions, and the exact indexed chunk count (about 2,457) is recorded.
4. ruff, strict mypy, pytest on Python 3.10 and 3.12, coverage on the listed modules,
   import-linter and vulture pass.
5. The retrieval gates pass at their current thresholds, or with a maintainer-approved change.
6. The Phase 0 baseline is reproduced. After the expansion, known-bad citations on the original 15
   cases are at most 4, and grounds, complementary grounds, exact recall and abstention are reported
   together.
7. The new generation is active with `reused_chunk_count = 472`, or with a documented canary
   failure, and `preindex_legal --check` exits 0.
8. The configured retrieval benchmark and the configured notice evaluation are re-run and compared
   with the previous configured results.

## 8. Risks

| Risk | Mitigation |
|---|---|
| The parser refactor changes CDC text | Byte-identical digest test, written before any other change |
| Planalto edits a page or its WAF behaviour | Fail-closed parse; corrections must apply exactly once; parsed-text hash; the refresher reports textual changes |
| Unsupported LGPD/CC grounds (review finding 1, amplified by a 5× larger index) | CDC anchor, cap and degraded block; Phase 0 measures final citations; factual-support predicates stay a follow-up |
| Numeric anchors match other laws | "CDC" qualification; hard negatives on LGPD and CC art. 42 |
| Long CPU embedding | Vector reuse, resumable shards, GPU option; the API keeps the old index until the check passes |
| Cross-platform drift in reused vectors | Canary; `--no-reuse` |
| Legal correctness of the scope and eligibility rules | Recorded as `requires_legal_review`; ADR 0016 documents the boundary |

## 9. Version identifiers

| Identifier | From | To |
|---|---|---|
| Corpus release | `br-consumer-law-2026-08-04-v3` | `br-consumer-law-YYYY-MM-DD-v4` |
| Statute parser | `cdc-html-parser-v2` | `planalto-statute-parser-v1` |
| Snapshot manifest schema | 2 | 3 |
| Corpus-hash payload schema | 1 | 2 |
| Chunking identity | `legal-hierarchy-v2:target=1200` | `legal-hierarchy-v3:target=1200` |
| Ground policy | `consumer-notice-scope-eligibility-v2` | `consumer-notice-scope-eligibility-v3` |
| Query builder | `consumer-legal-three-query-v3` | `consumer-legal-three-query-v4` |
| Embedding generation manifest | `embedding-generation-manifest-v1` | `embedding-generation-manifest-v2` |
| Golden dataset | 1.1.0 | 1.2.0 |

## 10. Follow-ups

- Local review findings 1 (factual-support predicates), 2 (degraded abstention for every source),
  3, 5 and 6.
- Specialist legal review of the CC scope, the LGPD eligibility rules and the new golden labels.
- A pruning policy for old embedding generations and PostgreSQL namespaces.
- The retrieval evaluator's `_is_hard_negative` matches ids by prefix, so it treats art. 42-A as
  a subdivision of art. 42 and counts 5 known-bad citations where the Phase 0 matcher counts 4.
