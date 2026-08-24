# Give Exit — AI Litigation Copilot

[Leia em português](README-pt.md)

AI assistant for Brazilian litigation, built as a **production-oriented,
single-node demonstration** of modern AI engineering: a fixed multi-stage
LangGraph workflow, auditable RAG, structured outputs, observability,
evaluation and explainability. It is not represented as a production legal
service: durable workers, user identity/tenant isolation and independent legal
validation remain explicit gaps.

> **Legal information and decision support, not legal advice.** Automated
> conclusions carry model-reported (uncalibrated) confidence and reasoning.
> When a model selects a valid evidence ID, the backend reconstructs the quote
> and page from the source; unsupported conclusions are marked for human review.
> Source integrity does not prove semantic entailment or legal correctness.

## Two product journeys

**Business** — a legal team uploads a lawsuit PDF and gets a structured report
in minutes: executive summary, classification, extracted entities (parties,
court, claim value, deadlines), timeline, claim-by-claim assessment, risk
analysis with financial exposure, defense strategy, settlement posture, and
case-number lookup/enrichment through DataJud (the official CNJ public-records API).
Export as Markdown, PDF, DOCX or JSON.

The Business legal analysis, risk and strategy are grounded in the uploaded
filing and chunks derived from it. They do **not** currently consult a
versioned statute or case-law corpus; legal propositions quoted inside a filing
remain a party's assertions, not independently verified law. DataJud enriches
the public case record but does not validate the merits. Treat any workflow as
filing-grounded unless its trace explicitly identifies a separate authoritative
corpus — today that explicit corpus boundary exists only in the Consumer flow.

**Consumer** — a guided chat structures a complaint against a supplier and
accepts PDF/PNG/JPEG evidence (rejected when OCR yields no reviewable text).
The assistant retrieves from a versioned offline snapshot of the compiled
[Consumer Defense Code (CDC)](https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm)
plus selected provisions of the
[Brazilian Constitution](https://www.planalto.gov.br/ccivil_03/constituicao/constituicaocompilado.htm),
and drafts a **notificação extrajudicial with a settlement proposal** — not a
lawsuit or court filing. The financial section is a transparent scenario
calculation: amounts found in evidence become candidates and enter the total
only after the consumer confirms them; amounts mentioned in chat are never
promoted automatically. Every component exposes its source, excerpt and hashes
for audit. The system never sends or files the notice — a qualified Brazilian
lawyer must review it first. In the current demo this is a product policy and
prominent warning, not an authenticated lawyer-approval workflow.
Candidate provisions also pass a versioned, deterministic category-eligibility
policy whose status is exposed as `requires_legal_review`; retrieval rank alone
never authorizes a legal ground.

In both journeys, every page of untrusted content passes a prompt-injection
security gate before indexing or downstream legal analysis (balanced/strict
scanning may itself call the configured LLM). Completed Business runs persist
RAG traces to local JSONL; Consumer traces stay with the in-memory case. Traces
record ranked chunk IDs, scores, hashes and which chunks reached each prompt.

## Architecture

```
Browser -> Streamlit -> FastAPI (202 + async job)
    -> LangGraph state machine:
       security_scan --+--> index -> classify -> extract --+--> analyze --+--> risk     --+
                       |                                  |              +--> strategy --+--> compose
                       |                                  +--> DataJud enrichment -------+
                       +--> halt for human review / block -------------------------------+
    -> RAG: Business dense retrieval | Consumer legal BM25+dense/RRF -> optional reranker
       -> isolated, versioned ChromaDB collections (corpus hash + embedding revision)
    -> LLM port: OpenAI | Anthropic Claude | Google Gemini | Mock (offline demo)
    -> Deterministic report composer -> MD / PDF / DOCX / JSON
```

Full diagram, layer map and the prompt-injection routing policy:
[docs/architecture.md](docs/architecture.md).
The supported storage threat boundary and the time-bounded Chroma advisory
exception are documented in [SECURITY.md](SECURITY.md).

### Design decisions (ADRs)

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-use-langgraph.md) | LangGraph over LangChain chains |
| [0002](docs/adr/0002-llm-provider-abstraction.md) | Own 2-method LLM port instead of LiteLLM |
| [0003](docs/adr/0003-chromadb-vector-store.md) | ChromaDB behind a VectorStore port |
| [0004](docs/adr/0004-async-first.md) | Async-first I/O from day one |
| [0005](docs/adr/0005-pymupdf-ocr-fallback.md) | PyMuPDF + heuristic OCR fallback |
| [0006](docs/adr/0006-section-aware-chunking.md) | Section-aware chunking for Brazilian petitions |
| [0007](docs/adr/0007-deterministic-report-composer.md) | No LLM at the last mile — the report is assembled by code |
| [0008](docs/adr/0008-citation-based-groundedness.md) | Fabricated-quote prevention by deterministic source reconstruction |
| [0009](docs/adr/0009-in-process-async-jobs.md) | In-process async jobs with a broker-ready interface |
| [0010](docs/adr/0010-prompt-injection-security-gate.md) | Scan untrusted content before indexing or LLM analysis |
| [0011](docs/adr/0011-retrieval-traceability-and-evaluation.md) | Persist query-to-context provenance; evaluate rankings |
| [0012](docs/adr/0012-bounded-consumer-extrajudicial-notice.md) | Bound the consumer flow to auditable, human-reviewed drafts |
| [0013](docs/adr/0013-versioned-consumer-law-retrieval.md) | Versioned official statutes + evaluated hybrid retrieval |
| [0014](docs/adr/0014-human-review-resume-path.md) | Human review as graph state, never a route override |

### Explainability

Important model conclusions use `ConfidentConclusion`. The model selects only
an evidence ID; quote and page are reconstructed by the backend:

```json
{
  "statement": "Opcao preliminar: avaliar acordo dentro dos valores documentados",
  "confidence": 0.87,
  "reasoning": "A peticao alega cobrancas mensais e informa o valor discutido...",
  "citations": [{
    "chunk_id": "abc123:0007",
    "quote": "cobrancas mensais indevidas",
    "page": 3
  }]
}
```

Unknown, foreign, duplicate or source-inconsistent evidence IDs are rejected.
The report exposes a deterministic evidence-quality gate and cannot silently
turn a fabricated quote into a citation. This verifies provenance/location,
not whether the excerpt logically supports the claim. Confidence percentages
are self-reported by the selected LLM and are not outcome probabilities.

### Observability

Every currently implemented agent and security-review call uses the typed
`LLMClient` boundary, which returns provider, model, latency, tokens, cost and
prompt version. Per-run aggregates for completed Business analyses persist to a
JSONL run store surfaced at `/runs` and `/runs/totals`
and in the UI cost panel. The full retrieval audit for a job is available at
`/analyses/{job_id}/retrievals` (chunk text previews are opt-in via
`LITIGATION_RETRIEVAL_TRACE_INCLUDE_PREVIEWS`); the Streamlit explainability
tab shows the same data as a filterable table.

Before durable JSONL writes, natural-language retrieval queries are replaced by
`[QUERY_REDACTED:<hash-prefix>]`; filenames and reviewer labels become HMAC
references. Reviewer comments, result sections/previews and arbitrary source
metadata are omitted, while durable errors retain only an exception-type
marker. This is data minimization, not anonymization: current in-memory job
endpoints still expose raw traces, Consumer cases/traces remain in memory, and
hashes or pseudonymous metadata can remain personal data. Without
`LITIGATION_TELEMETRY_PSEUDONYM_KEY`, pseudonyms intentionally change after a
process restart; configure a high-entropy secret only when cross-process
correlation is required.

### Prompt-injection defense

Deterministic Portuguese/English rules check every page before indexing;
document text is always treated as untrusted data. Routing is deterministic:
`none`/`low` proceeds, `medium` proceeds with a warning and masks flagged
excerpts, `high` halts as `review_required`, `critical` ends as `blocked`.
A `review_required` halt is resolved by a named human via
`POST /analyses/{job_id}/review`: approval re-runs the pipeline with the
flagged excerpts still masked and records the reviewer in the report;
rejection ends the run as `rejected`. The immutable decision is bound to the
job, document and reviewed security-assessment hash and is persisted before a
resume is scheduled. Reviewer identity is still self-declared behind the
deployment-level API key; production RBAC is not implemented. A `blocked`
verdict and an incomplete scan are never overridable.
`LITIGATION_PROMPT_INJECTION_SCAN_MODE` selects `rules` (deterministic only),
`balanced` (default — semantic review of suspicious excerpts) or `strict`
(bounded semantic review of all text; exceeding its budget fails closed).

**Measured effectiveness.** A labeled adversarial set
([eval_data/security](eval_data/security/injection_benchmark.json), 26 attacks
across 6 categories plus 14 benign Brazilian legal passages) scores the
deterministic rules offline:

```bash
python -m app.evaluation.security_benchmark
```

| Metric | Rules only |
|---|---|
| Attack recall | **0.769** (20/26) |
| False positives on benign legal text | **0.000** (0/14) |
| Direct attacks (PT + EN) | 1.000 |
| Obfuscation (base64, zero-width, homoglyph, HTML comment, page-split) | 1.000 |
| Paraphrased attacks | **0.000** (0/6) |

The paraphrase row is the point: lexical rules catch every attack that names
its intent and none that does not. That is a property of pattern matching, not
a bug to be patched away with more patterns.

Balanced mode only reviews excerpts the candidate selector forwards, so a
rules-missed attack is invisible to the reviewer too unless it is escalated.
The benchmark measures that hand-off directly: **5 of the 6** rules-missed
attacks are escalated to the semantic reviewer, while only **1 of 14** benign
passages is (escalation costs tokens, not correctness — the reviewer still has
to confirm a finding, and it can never clear a rule finding). That hand-off is
what makes `balanced` stronger than `rules`; only `strict` reviews text no
heuristic pointed at.

`tests/test_security_benchmark.py` asserts every threshold above, so a
weakened rule or a narrowed candidate selector fails the build. The gate is
risk reduction with a known ceiling, not proof that a document is safe.

### Evaluation

```bash
python -m app.evaluation                    # business golden dataset (offline in CI)
python -m app.evaluation.consumer_runner    # consumer legal retrieval, mock+BM25 baseline
python -m app.evaluation.security_benchmark # prompt-injection recall / false positives
```

Metrics include groundedness, hallucination rate, citation coverage,
extraction/classification accuracy, Precision/Recall/HitRate/MRR/NDCG@K, and
LLM-as-judge quality (real provider only). The consumer suite pins the exact
corpus release and SHA-256, adds article/subdivision metrics, hard negatives,
inactive-authority checks and abstention on out-of-scope complaints — it is an
engineering bake-off seed, not yet a production legal benchmark. The pinned CDC
snapshot is refreshed only by an explicit maintainer operation:

```bash
python -m app.consumer.update_cdc_snapshot --retrieved-on YYYY-MM-DD
pytest tests/test_consumer_legal_corpus.py tests/test_consumer_evaluation.py
```

Review the statutory diff, manifest hash and golden labels before promoting a
new release. Runtime requests never download law from Planalto.

The deterministic offline Consumer baseline currently gated in CI is:

| Metric | Baseline / gate |
|---|---:|
| Exact Recall@5 | 0.295 / >= 0.29 |
| Article Recall@5 | 0.756 / >= 0.75 |
| NDCG@5 | 0.254 / >= 0.25 |
| Hard-negative rate@5 | 0.067 / <= 0.07 |
| Out-of-scope abstention@5 | 1.000 / = 1.00 |

Labels remain developer-authored and explicitly require independent Brazilian
legal review; these numbers are retrieval regression evidence, not proof of
legal quality.

## Quickstart

### Docker (recommended)

```bash
# Optional: copy .env.example .env to configure a real provider.
# With no .env, the backend starts in the keyless offline demo.
docker compose up --build
# UI:  http://localhost:8501
# API: http://localhost:8000/docs
```

Compose publishes both ports on `127.0.0.1` by default. Overriding
`LITIGATION_BIND_HOST` to expose them on another interface is a deployment
decision: also set `LITIGATION_DEPLOYMENT_MODE=production` and
`LITIGATION_API_AUTH_KEY`. Production mode refuses to start without that key.

CI runs Ruff, strict MyPy, the offline suite on Python 3.12 plus a Python 3.10
compatibility suite, dependency vulnerability audit, Business and Consumer
retrieval regression gates, the security benchmark, and API/frontend container
builds. The Business mock gate measures retrieval/pipeline behavior, not the
quality of generated legal analysis.

### Local development

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -e ".[dev,frontend,ocr]"
copy .env.example .env
pytest                                            # fully offline

uvicorn app.api.main:app --reload                 # terminal 1
streamlit run frontend/streamlit_app.py           # terminal 2
```

Local OCR also requires the Tesseract binary with Portuguese language data
(the API Docker image already installs both).

### Choose an AI provider

Configure `.env`. `LITIGATION_LLM_MODEL` is optional — when absent, the app
picks a valid default for the provider. Selecting a real provider without its
key fails immediately; the app never silently substitutes mock output.

```dotenv
# Offline demo (default; deterministic placeholders, not real analysis)
LITIGATION_LLM_PROVIDER=mock

# OpenAI ("auto" reuses the key for embeddings)
LITIGATION_LLM_PROVIDER=openai
LITIGATION_OPENAI_API_KEY=sk-...

# Anthropic Claude (no embeddings API: "auto" uses local BAAI/bge-m3 —
# install ".[local-embeddings]" and, for Docker, set
# LITIGATION_API_EXTRAS=ocr,local-embeddings)
LITIGATION_LLM_PROVIDER=anthropic
LITIGATION_ANTHROPIC_API_KEY=sk-ant-...

# Google Gemini ("auto" reuses the key for Gemini embeddings)
LITIGATION_LLM_PROVIDER=gemini
LITIGATION_GEMINI_API_KEY=...
```

Changing embedding provider, model or dimensions creates a new Chroma
collection; existing documents must be reindexed.

### Document handling

Uploads stream with a 20 MB limit. Business accepts PDF only (up to
`LITIGATION_MAX_DOCUMENT_PAGES`, 250 by default); consumer evidence accepts
PDF/PNG/JPEG with a 40-megapixel cap. Raw consumer evidence is deleted right
after ingestion; business PDFs are deleted after analysis unless
`LITIGATION_RETAIN_UPLOADS=true`.

Indexed chunks contain the full document text. Business vectors are deleted
when their job finishes unless `LITIGATION_RETAIN_INDEX=true`; when retention
is disabled, startup also purges Business vectors left by a previous process.
Consumer evidence vectors remain only while their in-memory case exists: case
deletion removes them, and startup purges evidence orphaned by a restart while
preserving the canonical legal corpus. Business job/report state and all
Consumer case state are in memory. Completed Business run metadata and
minimized retrieval traces are the durable exception; they do not recreate a
job or Consumer case after a restart. Chunk previews remain live-only and are
never written to the JSONL ledger.

Business execution is bounded in one process by
`LITIGATION_MAX_CONCURRENT_JOBS` (4 by default) plus
`LITIGATION_MAX_QUEUED_JOBS` (16). Requests above the combined reservation
capacity receive `503` with `Retry-After`. This is an in-process semaphore and
bounded backlog, not a durable broker: queued/running/review-pending jobs are
lost on restart, replicas do not share capacity or job state, and the JSONL run
store cannot resume work. Use a broker, shared state and horizontally safe
workers before multi-replica deployment.

Security-halted jobs keep parsed documents only for a bounded resume window:
`LITIGATION_MAX_REVIEW_REQUIRED_JOBS` (20) and
`LITIGATION_REVIEW_REQUIRED_TTL_SECONDS` (24 hours) cap count and age. Oldest or
expired jobs are evicted after their audit record is durable. If that write
fails, the halt fails closed and its full resumable state is discarded.

In local mode, setting `LITIGATION_API_AUTH_KEY` requires `X-API-Key` on every
route except `/health`; in production mode the setting is mandatory at startup.
Uploads are additionally rate-limited per client
(`LITIGATION_UPLOAD_RATE_LIMIT_PER_MINUTE`, 20 by default). This is one shared
deployment key, not user identity, RBAC or tenant isolation. Those controls and
a documented retention policy remain prerequisites before using real case data.

### Demo data governance

[`demo/manifest.json`](demo/manifest.json) inventories every committed PDF with
its SHA-256, synthetic/personal-data classification, provenance and review
status. One retained non-synthetic judicial record contains personal data; its
exact source URL and redistribution basis are not established. Reported public
accessibility and repository presence do **not** by themselves grant
redistribution rights, waive LGPD duties or establish compliance with court
terms. Do not publish or redistribute it without an independent rights/privacy
review. Synthetic fixtures also contain identifier-shaped placeholders and
should remain telemetry-sensitive. See [`demo/README.md`](demo/README.md).

## Project structure

```
app/
├── core/           config (pydantic-settings), structured logging
├── consumer/       guided intake · legal corpus · evidence · notice composer
├── llm/            LLMClient port · OpenAI/Claude/Gemini/Mock adapters · pricing
├── schemas/        typed contracts for every layer (the domain model)
├── ingestion/      PDF/image -> text, OCR fallback, language detection
├── security/       prompt-injection scanning, policy and safe context masking
├── rag/            chunking · embeddings · hybrid retrieval · reranking
├── agents/         classifier · extraction · legal analysis · risk · strategy
├── prompts/        versioned PT-BR prompt templates
├── orchestration/  LangGraph state machine
├── enrichment/     DataJud (CNJ) client + graph node
├── services/       analysis use case · deterministic report composer
├── evaluation/     metrics · golden runner · LLM judge · CLI
├── observability/  JSONL run store with agent/retrieval traces
├── reporting/      Markdown (canonical) -> PDF / DOCX converters
└── api/            FastAPI app · async job manager · routes
frontend/           Streamlit UI (pure API client)
eval_data/          golden datasets + adversarial security benchmark
docs/               architecture + 14 ADRs + demo script
tests/              offline unit, integration and security tests
```

## Future improvements

- Versioned CLT (labor-law) corpus grounding the labor journey, following the
  CDC snapshot pattern
- Brazilian jurisprudence (case-law RAG) as a second corpus
- Redis-backed job queue + horizontal workers (ADR 0009 documents the path)
- Per-user AuthN/AuthZ and tenant data isolation (today's shared API key
  authenticates the deployment, not individual users)
- Managed vector store adapter (e.g. pgvector/Pinecone) for multi-tenant scale
- Human feedback loop: lawyer corrections feeding the golden dataset
- Calibrated outcome models from reviewed settlement and judgment data;
  statutory text alone cannot supply win probabilities

## Disclaimer

Reports and consumer notices are AI-generated decision support with explicit
provenance. They do not constitute legal advice and must be reviewed by a
qualified lawyer. The consumer flow does not create an attorney-client
relationship, submit a complaint, interrupt a limitation period or replace
urgent help from a lawyer or the competent authorities.
