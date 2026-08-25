# ADR 0003: ChromaDB behind a VectorStore port

Status: accepted · Date: 2026-07-23 · Amended: 2026-08-24

## Context

RAG needs a vector database. Candidates: ChromaDB (embedded), FAISS
(library), Pinecone/Qdrant (managed).

## Decision

ChromaDB with local persistence for development and single-node deployment,
accessed exclusively through a `VectorStore` protocol defined by us.
Pinecone becomes a new adapter when scale demands it.

The supported adapter is embedded-only: it constructs `PersistentClient`,
supplies application-computed embeddings, disables collection-provided
embedding functions and exposes no Chroma server or `HttpClient` configuration.
Current no-fix Chroma server/RBAC advisories and their time-bounded risk
acceptance are recorded in [`SECURITY.md`](../../SECURITY.md). A shared,
network-reachable or multi-tenant Chroma deployment is outside this decision.

## Consequences

- (+) Zero-infrastructure start; runs inside Docker Compose.
- (+) Metadata filtering (per-document isolation) built in - FAISS would
  need extra bookkeeping for this.
- (+) Migration path to managed stores without changing the Consumer service.
- (+) The narrow embedded boundary avoids relying on Chroma's server-side
  authentication, tenant authorization or remote embedding configuration.
- (-) Not horizontally scalable; acceptable for the current stage and
  explicitly addressed by the port.
- (-) The vulnerable dependency still executes in the API process. The
  documented exception must be re-reviewed or removed when a patched release
  exists; switching to Chroma client/server invalidates the exception.
