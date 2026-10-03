# Lay-language alias chunks — design

- Date: 2026-10-03
- Status: approved section by section in brainstorming; pending review of this written spec
- Classification: architectural — adds a corpus input, a new chunk level, a corpus release and an
  offline generator; changes citation resolution
- Branch: `feat/lay-aliases`
- Context: step 2 of the 2026-10-02 RAG review, measured with the evaluation of ADR 0021

## 1. Goal

Lay complaints rarely share words with the statute, and the retrieval-agreement gate needs both
the dense and the lexical channel to rank a unit within 13. Where the lexical channel never ranks
the right unit, no gate depth and no dense model can make it citable (ADR 0019: 11 of 42 labels in
2.0.0, 20 of 66 in 2.1.0, share no token with their case's queries). This work gives each citable
unit of the consumer and data-protection law a few plain-language access points, so a lay
complaint can reach the right article, while every citation still quotes official text.

Success means, on the development split of the golden set:

1. notice article recall rises;
2. notice precision does not fall;
3. known-bad citations stay at 0;

with the holdout reported, never used for adjustment, and a configured-stack (JUÁ) comparison
recorded once its golden query cache is filled.

## 2. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Who writes aliases | An offline generator calling the configured LLM, blind to the golden set; output reviewable and versioned. |
| D2 | Coverage | Active units of citable CDC, LGPD and Federal Constitution provisions (584 units). The Civil Code is out of this release. |
| D3 | Index form | One separate alias chunk per aliased unit; existing chunks are unchanged. |
| D4 | Citation | An alias is never quoted. A citation found through an alias quotes the unit's official text and records the alias that matched. |
| D5 | Granularity of generation | One LLM call per article, returning aliases for each of its units. |
| D6 | Integrity | Aliases are part of the corpus identity; an entry whose source text changed fails corpus load. |
| D7 | Stemming | Out of scope; a follow-up spike after a configured baseline exists. |

## 3. Non-goals

- Light stemming of the BM25 tokens (D7).
- Collapsing sibling units before truncation (review step 4). Crowding by alias chunks is measured,
  not fixed, here.
- Any change to the agreement gate, the eligibility policy, the ground policy version, the query
  builder or the scope gate.
- Query-time LLM use. Runtime stays deterministic.
- Legal validation. The alias file is `requires_legal_review` like the rest of the corpus.

## 4. Evidence

- ADR 0019 "Why labelled provisions are missed": the lexical channel, not the dense model, sets the
  ceiling of what notices can cite; CDC art. 39 I ("venda casada") shares no token with its case.
- 2026-10-02 review: the water-outage case (label CDC art. 22) shares one token with the art. 22
  caput; its top 11 hits are product-defect units of arts. 18 and 26.
- ADR 0018: identical vocabulary appended to every *query* diluted retrieval with a monotonic
  dose-response. Aliases differ in kind: they are specific to each *document*, so they add
  discriminating tokens rather than shared ones. No alias chunk carries a constant prefix.
- Vector reuse (ADR 0017) keys vectors by chunk-text hash, so a corpus release bump re-embeds only
  chunks whose text is new.
- Citable units today: CDC 294, LGPD 283, Federal Constitution 7, Civil Code 1,019; 1,644 indexed
  chunks. Articles in coverage: CDC 60, LGPD 41, Constitution 7.

## 5. Design

### 5.1 The alias artifact

`app/consumer/data/aliases/aliases.json`, packaged with the other corpus data
(`pyproject.toml` package-data already covers `data/*/*.json`).

```json
{
  "schema_version": 1,
  "review_status": "requires_legal_review",
  "prompt_version": "consumer-lay-aliases:v1",
  "prompt_sha256": "<sha256 of the system prompt and the user template>",
  "model": "<model that generated the generated entries>",
  "generated_on": "<YYYY-MM-DD of the last generation run>",
  "entries": [
    {
      "unit_key": "br-cdc-art-39-inciso-i",
      "provision_id": "br-cdc-art-39",
      "source_sha256": "<sha256 of the exact unit text block the model saw>",
      "aliases": ["…", "…", "…"],
      "status": "generated"
    }
  ]
}
```

- `unit_key` is the unit id, or the provision id for a provision without units (the Constitution
  entries).
- `status` is `generated`, `reviewed` or `rejected`. `generated` and `reviewed` entries are
  indexed; `rejected` entries are kept for audit and not indexed. A reviewer edits `aliases` and
  sets `reviewed`.
- Entries are sorted by corpus order (provision position, then unit position), so diffs are
  stable.
- A Pydantic schema (`app/consumer/aliases.py`) validates the file. Each entry has 2 to 4
  aliases, each 12 to 220 characters after whitespace normalisation, unique within the entry, and
  free of legal references: none may match
  `\b(?:art(?:igo)?s?\.?|lei|inciso|par[aá]grafo|cdc|lgpd|c[oó]digo)\b|§` (case-insensitive,
  accent-insensitive). Digits are allowed ("sete dias").
- Unknown `unit_key`, a `provision_id` that does not own the unit, duplicate `unit_key`, or a unit
  outside coverage (D2) fail validation.

### 5.2 Corpus integration

- `LegalCorpus` loads the alias file through `get_default_legal_corpus()`; a corpus built from
  explicit provisions in tests may pass `aliases=None` (no alias chunks).
- **Stale entries fail closed.** For every indexed entry, `source_sha256` must equal the hash of
  the unit text block the generator would send today (5.3). A mismatch raises at corpus load,
  naming the units, with the instruction to regenerate (`python -m app.consumer.generate_aliases`).
- `as_chunks()` appends, after each provision's own chunks, one alias chunk per indexed entry of
  that provision:
  - `chunk_id`: `{document_id}:legal:{unit_key}:alias-01`;
  - `text`: the aliases joined by `"\n"`, nothing else;
  - `section`, `page_start`, `page_end`: those of the unit's provision;
  - `metadata`: `_legal_metadata(provision, unit, …)` with `chunk_level: "alias"`.
  Only entries whose unit is active and whose provision is retrievable and eligible produce alias
  chunks; `include_uncitable`/`include_inactive` do not resurrect them.
- `unit_for_chunk` recognises `:legal:{unit_id}:alias-` as well as `:part-`.
- **Corpus identity.** `corpus_sha256` (payload `schema_version` 3) includes the alias manifest
  fields and, per indexed entry, `unit_key`, `source_sha256`, the normalised aliases and `status`.
  `CONSUMER_LAW_CORPUS_RELEASE_ID` becomes `br-consumer-law-<date of the real generation run>-v5` and
  `LEGAL_CHUNKING_VERSION` becomes `legal-hierarchy-v5`.
- The golden dataset's `target_corpus_release_id` and `target_corpus_sha256` move to v5 (dataset
  2.3.0; labels and splits unchanged).

### 5.3 The generator

`python -m app.consumer.generate_aliases [--model M] [--article PROVISION_ID]... [--force]
[--dry-run]`, module `app/consumer/alias_generation.py`.

- **Input** is the corpus only. For each covered article (corpus order) it builds one request:
  the citation label, the hierarchy breadcrumb, and, for each active unit in index scope, a block
  `[unit_key] label: text`, where an inciso or alínea block is preceded by its lead-in (the same
  ancestor chain the unit chunk carries). The unit text block is what `source_sha256` hashes.
- **Prompt** (Portuguese, versioned as `consumer-lay-aliases:v1`, hashed into the manifest): for
  each unit, write 2 to 4 short sentences, in the first person of a lay consumer in Brazil,
  describing concrete situations the unit covers; specific to that unit and distinct from its
  siblings; no article numbers, law names, codes or legal jargon when a common word exists; at
  most 25 words each.
- **Output schema** (`LLMClient.parse`): a list of `{unit_key, aliases}`. Keys not requested are
  dropped; requested keys missing from the answer are left without an entry and reported.
- **Validation** applies the 5.1 rules per alias; an invalid alias is dropped, and an entry left
  with fewer than 2 aliases is not written and is reported.
- **Resume and protection.** The file is rewritten atomically after each article. A run skips a
  unit whose existing entry has the current `source_sha256` and the manifest's current prompt
  version. It never replaces an entry with status `reviewed` or `rejected` unless `--force`.
- `--dry-run` prints the requests without calling the model. Temperature is 0. `--model` defaults
  to the configured `LITIGATION_LLM_MODEL`; the model actually used is recorded in the manifest.
- The generator never reads `eval_data/`. A test asserts that no golden complaint or requested
  remedy appears in any request it builds.

### 5.4 Citations and audit

- `authority_for_chunk` resolves an alias chunk to its unit's canonical `part-01` chunk before
  building the citation, so `official_excerpt` and `official_excerpt_sha256` are official text.
- `LegalAuthorityCitation` gains `matched_chunk_id: str | None` — the retrieved chunk id when it
  differs from `chunk_id` (the alias); `None` otherwise.
- `_annotate_composer_selection` in the notice service treats both ids of a ground as included, so
  the trace marks the alias hit that led to the citation.
- Ground selection, eligibility, the precedence window and the CDC anchor read the provision and
  unit, so they need no change; the agreement gate applies to the alias chunk like any chunk.

### 5.5 Evaluation, measurement and CI

- The retrieval evaluator and `label_ranks` map hits through chunk metadata (`unit_id`,
  `provision_id`), so an alias hit counts as its unit's hit with no change; tests pin this.
- After the real generation run (5.6), the offline notice and retrieval evaluations run on both
  splits before and after, and `python -m app.evaluation.compare` reports the development
  differences.
- **Stop rules**: if development `consumer_notice_known_bad_citations` rises above 0, or
  development `consumer_notice_precision` or `consumer_notice_article_recall` falls, execution
  stops and reports the comparison for a decision. Prompt changes made on development results are
  allowed by the split policy (ADR 0021) and are a user decision.
- Otherwise test pins and CI gates are re-baselined on the development split with the usual
  history comments; known-bad stays `<= 0`.
- `label_ranks.DEFAULT_DEPTH` (2,500) must exceed the indexed chunk count (1,644 + up to 584); its
  comment is updated.

### 5.6 The real generation run

A plan step stops before calling OpenAI, states the article count (108) and asks for confirmation.
It sends only public statute text. The run writes `aliases.json`, which is committed with its
manifest.

### 5.7 Configured-stack protocol

Run on the development machine, each long step confirmed when reached:

1. Fill the JUÁ golden query cache (`python -m app.evaluation.query_vectors --case …`, one case per
   process, in the background; corpus-independent).
2. From `main` (corpus v4), run the configured notice and retrieval evaluations on the development
   split with `--require-cached-queries` — the v4 baseline.
3. On the branch, `python -m app.consumer.preindex_legal` builds the v5 generation: vector reuse
   supplies the 1,644 unchanged chunks after the canary and only the alias chunks are embedded.
4. Run the same evaluations on v5 and `compare` them with step 2; record the result in ADR 0022.

## 6. Testing

Test-first, with the LLM always faked:

- alias schema: bounds, uniqueness, the legal-reference rule with and without accents, coverage,
  unknown and mismatched keys;
- stale `source_sha256` fails corpus load; `rejected` entries produce no chunk;
- alias chunks: id, text, metadata, `chunk_level`, one per indexed entry, only active units of
  eligible provisions, ignored by `include_uncitable`/`include_inactive`;
- `corpus_sha256` changes when an alias, a status or the manifest changes;
- the generator: one request per article, blocks with lead-ins, no golden text in any request,
  resume after an interruption, protected `reviewed`/`rejected` entries, `--force`, `--dry-run`,
  invalid aliases dropped, short entries skipped and reported;
- citations: an alias hit quotes official text and records `matched_chunk_id`; the trace marks
  the alias as included;
- retrieval evaluation and `label_ranks` count an alias hit as the unit's hit.

`app.consumer.aliases` and `app.consumer.alias_generation` join the 100% coverage scope.

## 7. Documentation

- ADR 0022, "Lay-language alias chunks": D1–D7, the measured offline results and, when available,
  the configured comparison.
- README and README-pt: the alias artifact, its review statuses and the generator command, in the
  legal-corpus and configuration sections.
- The dataset description records the 2.3.0 target-corpus move.

## 8. Risks and follow-ups

- **Crowding**: alias chunks add up to one more same-article candidate per unit to the 8 slots per
  query; step 4 addresses it.
- **Precision**: generic aliases could match many complaints. The precision metric and the
  unlabelled-ground count now show it; the stop rules catch it.
- **Leakage of real-world scenarios** into aliases that also appear in golden cases is legitimate
  generalisation, not contamination, as long as the generator never sees the golden set (5.3).
- **Review**: aliases are model output marked `requires_legal_review`; a reviewer can reject or
  edit entries without regenerating the rest.
- Follow-ups: light stemming on the configured stack (D7), the gate structure (step 3), sibling
  collapse (step 4), and Civil Code aliases if measurement supports them.
