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
        +--> ChromaDB hybrid RAG
        |      +--> accepted case evidence
        |      +--> versioned CDC + selected CF provisions
        |      +--> dense retrieval + BM25 + reciprocal-rank fusion
        |      +--> optional cross-encoder reranking
        |
        +--> deterministic legal-policy and settlement-scenario gates
        |
        +--> auditable notice --> Markdown / PDF / DOCX
```

The conversational LLM does **not** write the notice. A configured LLM is used
only for bounded semantic review of suspicious document excerpts. Embeddings
are independently configurable. The offline mock provider keeps tests and the
demo deterministic and keyless.

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
- Notice citations are reconstructed from retrieved evidence and canonical
  legal metadata; they are not trusted model-generated citation strings.
- Retrieval traces record the query, hashes, model/revision, ranking mode,
  scores, chunk IDs, source metadata and final inclusion decisions.

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
after ingestion.

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

Brazilian legal embedding bake-off:

```bash
python -m pip install -e ".[local-embeddings]"
```

```env
LITIGATION_EMBEDDING_PROVIDER=sentence_transformers
LITIGATION_EMBEDDING_MODEL=ufca-llms/jua-4B-mixed
LITIGATION_EMBEDDING_QUERY_INSTRUCTION=Instruct: Dada uma consulta jurídica brasileira, recupere os trechos ou dispositivos legais vigentes mais relevantes. Query:
LITIGATION_EMBEDDING_DEVICE=cpu
LITIGATION_EMBEDDING_BATCH_SIZE=2
```

Changing the corpus release, embedding model or pinned revision produces a new
versioned Chroma collection instead of mixing incompatible vectors. Materialize
that collection before accepting notice-generation traffic:

```powershell
python -m app.consumer.preindex_legal
python -m app.consumer.preindex_legal --check
```

The first JUÁ CPU run can take tens of minutes or hours, depending on the
hardware and chunk sizes. The command reports progress in the terminal. Once
complete, the API reuses the 460 persisted legal chunks instead of recomputing
them inside a notice request. Use `--force` only for a deliberate reindex.

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

To evaluate the configured embedding provider rather than the offline baseline:

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
- Cases and notices are currently in process memory. The legal index persists;
  case evidence vectors are removed on case deletion and orphaned evidence is
  purged at startup.
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
├── rag/          Chunking, embeddings, Chroma, BM25/RRF and reranking
├── reporting/    PDF/DOCX export
├── security/     Prompt-injection scanning, sanitization and telemetry privacy
└── evaluation/   Consumer legal retrieval and document-security benchmarks
frontend/         Consumer-only Streamlit UI and HTTP client
eval_data/        Consumer legal-retrieval and security datasets
tests/            Consumer and shared-infrastructure regression tests
```
