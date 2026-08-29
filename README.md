# Give Exit

Consumer-first Brazilian legal-information assistant for producing auditable
drafts of extrajudicial notices. Give Exit turns a consumer's confirmed account
and uploaded evidence into a deterministic notice grounded in a versioned
consumer-law corpus.

This repository is a production-oriented demonstration, not a law firm or a
production legal service. It does not decide whether a consumer is legally
right, predict litigation outcomes, send notices automatically, or replace
review by a qualified Brazilian lawyer.

[Leia em português](README-pt.md)

## Product scope

Give Exit serves consumers only:

1. The consumer describes the problem and expected resolution.
2. Deterministic intake extracts only explicit facts; all facts remain
   allegations until the consumer reviews and confirms them.
3. The consumer uploads PDF, PNG, JPG or JPEG evidence.
4. The backend validates signatures and size, extracts text or applies OCR,
   and scans every document for prompt injection before indexing it.
5. Hybrid retrieval searches both the submitted evidence and the reviewed
   Brazilian consumer-law corpus.
6. A deterministic policy selects eligible legal authorities and reconstructs
   citations from source metadata and hashes.
7. The application exports the notice as Markdown, PDF or DOCX and exposes the
   complete retrieval audit.

There is no company-side lawsuit-analysis journey, defense strategy, DataJud
enrichment, multi-agent graph, or general litigation report.

## Architecture

```text
Streamlit consumer UI
        |
        v
FastAPI /consumer/cases API
        |
        +--> deterministic intake + explicit consumer confirmation
        |
        +--> bounded PDF/image upload --> extraction/OCR
        |                                --> prompt-injection gate
        |
        +--> PostgreSQL/Chroma hybrid RAG
        |      +--> accepted case evidence
        |      +--> versioned CDC + selected CF provisions
        |      +--> dense retrieval + lexical ranking + reciprocal-rank fusion
        |      +--> optional cross-encoder reranking
        |
        +--> deterministic legal-policy and settlement-scenario gates
        |
        +--> auditable notice --> Markdown / PDF / DOCX
```

There is no conversational legal-advice LLM. A configured LLM may perform
bounded semantic review of suspicious document excerpts. An optional,
separately configured OpenAI composer may phrase five prose fields only after
facts, evidence, legal grounds, requests, values and citations have been fixed
deterministically; invalid output falls back safely. Embeddings are independent.

## Legal grounding and citations

- The CDC is ingested from a pinned Planalto snapshot with a manifest and
  content hashes.
- Selected constitutional provisions are versioned in the legal corpus.
- Statutory chunks preserve law, article, subdivision, official URL, release,
  status and source hashes.
- Legal retrieval is hybrid because exact article references and institutional
  language complement semantic paraphrase matching.
- Retrieved chunks are candidates, not automatically accepted authorities. A
  deterministic policy controls eligibility and remains marked
  `requires_legal_review`.
- Eligibility is a property of the document, not of the consumer's answers.
  The issue category shapes the retrieval queries but never restricts which
  articles may be cited: it is a lay self-classification, one report often
  spans several problems, and a wrong pick must not deny somebody a notice.
  What an individual extrajudicial notice cannot rest on is excluded by the
  statute's own structure — the CDC chapters on criminal offences,
  administrative sanctions, collective litigation and the national
  consumer-protection system.
- The load-bearing precision control is retrieval agreement, not the category:
  an article becomes a ground only when dense and lexical retrieval both
  ranked it (or, in degraded mode, when two independent queries corroborate
  it in their top three).
- Notice citations are reconstructed from retrieved evidence and canonical
  legal metadata; they are not trusted model-generated citation strings.
- An evidence citation names exactly one file and one page. A retrieved chunk
  whose text spans more than one evidence page is dropped rather than quoted
  under the first page's filename: a shorter notice is recoverable, one that
  attributes another document's words to this file is not.
- Excerpts taken from uploaded files are escaped before they enter the notice
  Markdown, so a document cannot inject links or emphasis into the draft the
  consumer reads and exports.
- Retrieval traces record the query, hashes, model/revision, active generation
  ID, ranking/degraded mode, cache hits, scores, chunk IDs, source metadata and
  final inclusion decisions.

The Consumer retrieval audit is available at:

```text
GET /consumer/cases/{case_id}/notice/retrievals
X-Consumer-Case-Token: <opaque case token>
```

## Quickstart

### Docker

```bash
docker compose up --build
```

- UI: <http://localhost:8501>
- API docs: <http://localhost:8000/docs>

Docker installs Portuguese Tesseract OCR by default. Raw uploads are deleted
after ingestion. Downloaded Hugging Face model weights are cached in a dedicated `model-cache`
volume, separate from the application state in `consumer-data`, so rebuilding
the API image does not download JUÁ again and pruning case data never discards
the weights. The first download still requires several gigabytes and can take many
minutes; `LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS` controls how long the local
Streamlit client waits for that synchronous demo flow.

To verify both Docker settings without downloading JUÁ, start Docker Desktop (or
another Docker Engine) and run this exact command from the repository root:

```powershell
pwsh -NoProfile -File .\scripts\smoke_docker_runtime.ps1
```

The script creates an isolated Compose project, writes a lightweight sentinel to
the API service's `HF_HOME`, rebuilds the API image with `--no-cache`, and checks
that a new API container can still read the sentinel from `consumer-data`. It
also injects a non-default timeout and reads it inside the frontend container.
It removes the isolated containers and volume when done. Among the normal Docker
build progress, a successful run prints:

```text
HF_HOME=/models/huggingface
PASS HF_HOME=/models/huggingface persisted across the API image rebuild.
PASS LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS=4321 reached the frontend container.
PASS Docker runtime configuration smoke test completed.
```

### Local Python

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,frontend,ocr]"
python -m app.consumer.preindex_legal
```

After pre-indexing, run two terminals:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --reload

# Terminal 2
$env:LITIGATION_API_URL="http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m streamlit run frontend/streamlit_app.py
```

Local image OCR also requires the Tesseract executable and Portuguese language
data to be installed on the host.

## Configuration

Copy `.env.example` to `.env`. The historical `LITIGATION_` prefix is
retained for configuration compatibility, but the runtime is Consumer-only.

The default clone uses mock LLM and mock embeddings. Example OpenAI setup:

```env
LITIGATION_LLM_PROVIDER=openai
LITIGATION_OPENAI_API_KEY=sk-...
LITIGATION_LLM_MODEL=gpt-4o-mini
LITIGATION_EMBEDDING_PROVIDER=openai
LITIGATION_EMBEDDING_MODEL=text-embedding-3-small
LITIGATION_RETRIEVAL_MODE=hybrid
```

To have the final notice phrased by GPT-5.6 Terra while retaining deterministic
source selection and rendering of citations, requests and settlement values:

```env
LITIGATION_OPENAI_API_KEY=sk-...
LITIGATION_NOTICE_COMPOSER=openai
LITIGATION_NOTICE_COMPOSER_MODEL=gpt-5.6-terra
LITIGATION_NOTICE_COMPOSER_REASONING_EFFORT=low
LITIGATION_NOTICE_COMPOSER_MAX_OUTPUT_TOKENS=1200
```

This sends the confirmed facts, selected evidence excerpts and already-filtered
legal grounds to OpenAI only when a notice is generated. The database,
embeddings and legal corpus stay local. The model returns strict structured
prose through the Responses API (`store=False`); a provider failure or invalid
output falls back to the deterministic composer and is recorded in the notice.

Brazilian legal embedding bake-off:

```bash
python -m pip install -e ".[local-embeddings]"
```

```env
LITIGATION_EMBEDDING_PROVIDER=sentence_transformers
LITIGATION_EMBEDDING_MODEL=ufca-llms/jua-4B-mixed
LITIGATION_EMBEDDING_MODEL_REVISION=57f491c1718171c0ad71d723c4f6b2030684c4eb
LITIGATION_EMBEDDING_EXPECTED_DIMENSIONS=2560
LITIGATION_EMBEDDING_REQUIRE_MODEL_REVISION=true
LITIGATION_EMBEDDING_QUERY_INSTRUCTION=Instruct: Dada uma consulta jurídica brasileira, recupere os trechos ou dispositivos legais vigentes mais relevantes. Query:
LITIGATION_EMBEDDING_DEVICE=cpu
LITIGATION_EMBEDDING_BATCH_SIZE=2
LITIGATION_EMBEDDING_INDEX_SHARD_SIZE=25
```

The setting holds only the task description; the client renders the template
JUÁ declares in its own `config_sentence_transformers.json` —
`Instruct: {task}
Query: {query}` — so the newline separates the task from
`Query:`, not `Query:` from the text. Legal documents stay plain, because the
model registers an empty `document` prompt. Changing the corpus release, model, exact revision,
formatter or instruction hash produces a distinct generation. Offline indexing
writes checksummed gzip shards and a manifest under
`data/embedding_generations/<generation-id>/`, resumes only verified shards,
validates full chunk coverage/dimension/L2 normalization and activates the
destination namespace only after a successful import.

Chroma is the default backend. PostgreSQL uses server-side Portuguese full-text
search plus pgvector. To move an existing local collection to PostgreSQL without
recomputing its embeddings, install the optional dependency, enable pgvector
in the destination database and configure a DSN:

```powershell
python -m pip install -e ".[postgres]"
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

```env
LITIGATION_POSTGRES_DSN=postgresql://postgres:YOUR_PASSWORD@localhost:5432/postgres
LITIGATION_DOCKER_POSTGRES_DSN=postgresql://postgres:YOUR_PASSWORD@host.docker.internal:5432/postgres
```

```powershell
python -m app.consumer.migrate_legal_index_to_postgres
```

After success, switch the runtime backend:

```env
LITIGATION_VECTOR_STORE=postgres
```

Local Python processes use `LITIGATION_POSTGRES_DSN`. Docker Compose overrides
that value inside the API container with `LITIGATION_DOCKER_POSTGRES_DSN`, so
local runs use `localhost` while Docker Desktop uses `host.docker.internal`.

Then validate the normal runtime path:

```powershell
python -m app.consumer.preindex_legal --check
```

The first JUÁ CPU run can take tens of minutes or hours, depending on the
hardware and chunk sizes. Each completed shard is durable, so an interrupted
run can be restarted with the same command. Once complete, the API reuses the
472 persisted legal chunks instead of recomputing them inside a notice request.
Use `--force` only for a deliberate rebuild.

For a legacy namespace whose exact cached model revision was independently
verified by the operator, promotion can avoid another multi-hour embedding run:

```powershell
python -m app.consumer.preindex_legal `
  --adopt-source-index <legacy-namespace> `
  --attest-source-revision <exact-hugging-face-commit>
```

The manifest labels this provenance as `adopted_existing_vectors`; attestation
does not retroactively prove metadata that the legacy run failed to record.

At query time, embedding calls have a timeout, concurrency bound, short-lived
query-hash cache and circuit breaker. The concurrency bound is a queue, not a
rejection: an overlapping request waits up to
`LITIGATION_EMBEDDING_QUERY_QUEUE_TIMEOUT_SECONDS` for a free slot, because a
single slow local model makes concurrent users the normal case rather than an
overload. Hybrid retrieval may still degrade to audited lexical-only search;
deterministic legal-support and citation gates continue to apply and can
abstain. A degraded draft is labelled as such: the notice carries
`retrieval_degraded_modes` and a reader-facing warning, not only a
`degraded_mode` field inside the per-query retrieval traces.

## API surface

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/health` | API liveness and legal-corpus readiness |
| `POST` | `/consumer/cases` | Create an ephemeral case and possession token |
| `GET` | `/consumer/cases/{id}` | Read the authorized case |
| `POST` | `/consumer/cases/{id}/messages` | Add a consumer message |
| `PATCH` | `/consumer/cases/{id}/facts` | Review, correct and confirm facts |
| `POST` | `/consumer/cases/{id}/documents` | Upload evidence |
| `POST` | `/consumer/cases/{id}/notice` | Generate the grounded notice |
| `GET` | `/consumer/cases/{id}/notice` | Read structured notice output |
| `GET` | `/consumer/cases/{id}/notice.{md,pdf,docx}` | Export the notice |
| `GET` | `/consumer/cases/{id}/notice/retrievals` | Inspect retrieval provenance |
| `DELETE` | `/consumer/cases/{id}` | Delete case state and evidence vectors |

Every case operation requires the opaque possession token returned at case
creation. Production mode also requires a configured API key.

### Resource bounds

Case state lives in process memory and every write path allocates on the
caller's behalf, so each of these is a denial-of-service control as much as a
product limit:

| Bound | Setting | Default |
|---|---|---|
| Request body, refused before buffering | `LITIGATION_MAX_UPLOAD_BYTES` | 20 MB |
| Evidence documents per case | `LITIGATION_MAX_DOCUMENTS_PER_CASE` | 20 |
| Retained chat turns per case | `LITIGATION_MAX_MESSAGES_PER_CASE` | 200 |
| Idempotency keys per case | `LITIGATION_MAX_IDEMPOTENCY_KEYS_PER_CASE` | 50 |
| Live cases before `503` | `LITIGATION_MAX_ACTIVE_CASES` | 500 |
| Idle case expiry | `LITIGATION_CASE_IDLE_TTL_SECONDS` | 24 h |
| Case creation / minute | `LITIGATION_CASE_RATE_LIMIT_PER_MINUTE` | 30 |
| Messages / minute | `LITIGATION_MESSAGE_RATE_LIMIT_PER_MINUTE` | 60 |
| Uploads / minute | `LITIGATION_UPLOAD_RATE_LIMIT_PER_MINUTE` | 20 |
| Notice generation / minute | `LITIGATION_NOTICE_RATE_LIMIT_PER_MINUTE` | 10 |

A full store refuses new cases rather than evicting a live one. Idle cases and
their evidence vectors are reclaimed by a background sweeper.

Rate limiting is keyed on the socket peer. Behind a reverse proxy that address
is the proxy itself, which would put every caller in one bucket, so set
`LITIGATION_TRUSTED_PROXY_HOPS` to the real number of proxies; only that many
`X-Forwarded-For` entries are trusted.

`LITIGATION_PURGE_ORPHANED_EVIDENCE_ON_STARTUP` deletes evidence vectors with
no live case. It is correct only when the process owns the vector-store
namespace outright. **Set it to `false` for multiple workers, replicas or any
deployment sharing one PostgreSQL/Chroma namespace**, otherwise each start
deletes the other processes' live case evidence.

## Evaluation and verification

```bash
pytest -q
python -m app.evaluation.consumer_runner
python -m app.evaluation.security_benchmark
```

The Consumer golden set reports Recall, article-level Recall, MRR, NDCG,
subdivision precision, hard-negative rate, inactive-authority rate and
out-of-scope abstention. It is an engineering-authored seed and still requires
independent Brazilian legal review.

To evaluate the active configured embedding generation rather than the offline
baseline (this command refuses to re-embed the corpus implicitly):

```bash
python -m app.evaluation.consumer_runner \
  --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever \
  --output consumer-retrieval-results.json
```

CI runs Ruff, strict MyPy, Python 3.10/3.12 tests, dependency auditing, Consumer
retrieval regression gates, the prompt-injection benchmark and container builds.

## Privacy and current limitations

- Raw uploads are deleted after ingestion, but safe extracted text and chunks
  remain sensitive personal data.
- Cases and notices are currently in process memory, bounded by count, size and
  an idle TTL, and lost on restart. The legal index persists; case evidence
  vectors are removed on case deletion, on case expiry, and — when this process
  owns the namespace — for orphans at startup.
- The possession token and optional shared API key are appropriate only for a
  single-user demonstration, not multi-tenant production authentication.
- Rate limiting is in-process and not horizontally coordinated.
- There is no encrypted durable case store, tenant isolation, consent ledger,
  retention scheduler, asynchronous worker queue or automatic PII redaction in
  exported notices yet.
- The constitutional corpus contains selected provisions, not the complete
  Constitution.
- Legal sources, policy labels and evaluation judgments require independent
  legal review before public production use.

See [SECURITY.md](SECURITY.md), [architecture.md](docs/architecture.md) and the
[ADRs](docs/adr) for the accepted security boundary and design rationale.

## Repository map

```text
app/
├── api/          Consumer HTTP contract, auth, rate limiting and upload bounds
├── consumer/     Intake, legal corpus, policy, settlement and notice service
├── ingestion/    PDF/image extraction and Portuguese OCR
├── llm/          Provider-neutral bounded security-review clients
├── rag/          Chunking, embeddings, vector stores, lexical/RRF and reranking
├── reporting/    PDF/DOCX export
├── security/     Prompt-injection scanning, sanitization and telemetry privacy
└── evaluation/   Consumer legal retrieval and document-security benchmarks
frontend/         Consumer-only Streamlit UI and HTTP client
eval_data/        Consumer legal-retrieval and security datasets
tests/            Consumer and shared-infrastructure regression tests
```
