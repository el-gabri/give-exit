# Security policy

## Supported deployment boundary

Give Exit is a production-oriented, single-node demonstration, not a
multi-tenant legal service. The supported Chroma integration is embedded-only:
`ChromaVectorStore` constructs `chromadb.PersistentClient`, supplies embeddings
it computes itself, disables collection-provided embedding functions, and has
no `HttpClient` or Chroma server configuration path. Docker Compose exposes no
Chroma service. Do not deploy this repository against a shared or
network-reachable Chroma server.

Production mode requires the application API key, but that shared key is not
user identity, tenant isolation or RBAC. A network-facing or multi-tenant
deployment remains out of scope until those controls and a different storage
security review exist.

## Time-bounded Chroma advisory exception

Reviewed: **2026-08-24**  
Review again by: **2026-09-30**, or immediately when Chroma publishes a
patched release.

`chromadb 1.5.9` currently has four advisories without patched versions:

- [CVE-2026-45829 / GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c): remote code execution through a Chroma server collection request.
- [CVE-2026-45830 / GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr): cross-tenant collection authorization failure.
- [CVE-2026-45833 / GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf): code injection through a server-side collection update.
- [CVE-2026-45831 / GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr): SimpleRBAC resource-scope failure.

The repository accepts this dependency risk only for the embedded, local,
single-process adapter described above: no Chroma API endpoint is exposed, no
Chroma tenant/RBAC feature is relied upon, and an untrusted caller cannot
supply an embedding model or `trust_remote_code` option. The API container also
runs as a non-root user and Compose binds application ports to localhost by
default.

This is a scoped risk acceptance, not a claim that the dependency is safe.
Local filesystem compromise or a future switch to `HttpClient`, a shared
collection, or user-controlled embedding configuration invalidates it. CI
continues auditing every other dependency and ignores only these four IDs. At
the review date, upgrade to a patched Chroma release and remove all four
exceptions; if no patch exists, replace the adapter or keep the deployment
non-production.

## Reporting a vulnerability

Do not include court filings, personal data, API keys or exploit payloads in a
public issue. Contact the repository maintainer privately with the affected
version, reproduction conditions and a minimally sensitive proof of concept.
