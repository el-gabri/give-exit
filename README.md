# Give Exit — AI Litigation Copilot

[Leia em português](README-pt.md)

AI assistant for Brazilian litigation, built as a production-grade demonstration
of modern AI engineering: multi-agent orchestration (LangGraph), auditable RAG,
structured outputs, observability, evaluation and explainability.

> **Not a replacement for lawyers.** Every conclusion carries a confidence
> score, explicit reasoning and verbatim citations from the source document,
> so a human can audit every claim.

## Two product journeys

**Business** — a legal team uploads a lawsuit PDF and gets a structured report
in minutes: executive summary, classification, extracted entities (parties,
court, claim value, deadlines), timeline, claim-by-claim assessment, risk
analysis with financial exposure, defense strategy, settlement posture, and
case-number validation against DataJud (the official CNJ court-records API).
Export as Markdown, PDF, DOCX or JSON.

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
lawyer must review it first.

In both journeys, every page of untrusted content passes a prompt-injection
security gate before reaching the RAG or LLM layers, and every RAG query
leaves a durable audit trail (ranked chunk IDs, scores, hashes, and which
chunks actually reached each prompt).

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
| [0008](docs/adr/0008-citation-based-groundedness.md) | Hallucination detection by mechanical citation verification |
| [0009](docs/adr/0009-in-process-async-jobs.md) | In-process async jobs with a broker-ready interface |
| [0010](docs/adr/0010-prompt-injection-security-gate.md) | Scan untrusted content before indexing or LLM analysis |
| [0011](docs/adr/0011-retrieval-traceability-and-evaluation.md) | Persist query-to-context provenance; evaluate rankings |
| [0012](docs/adr/0012-bounded-consumer-extrajudicial-notice.md) | Bound the consumer flow to auditable, human-reviewed drafts |
| [0013](docs/adr/0013-versioned-consumer-law-retrieval.md) | Versioned official statutes + evaluated hybrid retrieval |
| [0014](docs/adr/0014-human-review-resume-path.md) | Human review as graph state, never a route override |

### Explainability

Every important conclusion is a `ConfidentConclusion`:

```json
{
  "statement": "Recomendado buscar acordo ate R$ 8.000,00",
  "confidence": 0.87,
  "reasoning": "O documento comprova a cobranca indevida e o CDC preve...",
  "citations": [{"quote": "cobrancas mensais indevidas", "page": 3}]
}
```

The evaluation harness verifies each citation actually occurs in the source
document — a fabricated quote is caught mechanically, not by another LLM's
opinion.

### Observability

Every LLM call returns typed metadata (provider, model, latency, tokens, cost,
prompt version) — agents physically cannot make untracked calls. Per-run
aggregates persist to a JSONL run store surfaced at `/runs` and `/runs/totals`
and in the UI cost panel. The full retrieval audit for a job is available at
`/analyses/{job_id}/retrievals` (chunk text previews are opt-in via
`LITIGATION_RETRIEVAL_TRACE_INCLUDE_PREVIEWS`); the Streamlit explainability
tab shows the same data as a filterable table.

### Prompt-injection defense

Deterministic Portuguese/English rules check every page before indexing;
document text is always treated as untrusted data. Routing is deterministic:
`none`/`low` proceeds, `medium` proceeds with a warning and masks flagged
excerpts, `high` halts as `review_required`, `critical` ends as `blocked`.
A `review_required` halt is resolved by a named human via
`POST /analyses/{job_id}/review`: approval re-runs the pipeline with the
flagged excerpts still masked and records the reviewer in the report;
rejection ends the run as `rejected`. A `blocked` verdict and an incomplete
scan are never overridable.
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

## Quickstart

### Docker (recommended)

```bash
# Optional: copy .env.example .env to configure a real provider.
# With no .env, the backend starts in the keyless offline demo.
docker compose up --build
# UI:  http://localhost:8501
# API: http://localhost:8000/docs
```

CI runs ruff, `mypy --strict` (clean, enforced), the offline test suite, a
dependency vulnerability audit, the evaluation harness with retrieval
regression gates, and the security benchmark.

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

Indexed chunks contain the full document text, so they follow the same
lifecycle: a document's vectors are deleted when its job finishes unless
`LITIGATION_RETAIN_INDEX=true`, and startup purges any vectors left behind by
a previous process (case and job records are in-memory). Run history — hashes
and metrics, never chunk text unless previews are enabled — persists.

Set `LITIGATION_API_AUTH_KEY` to require `X-API-Key` on every route except
`/health`; uploads are additionally rate-limited per client
(`LITIGATION_UPLOAD_RATE_LIMIT_PER_MINUTE`, 20 by default). Both are required
for any deployment reachable beyond localhost, alongside tenant isolation and
a documented retention policy before using real case data.

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
