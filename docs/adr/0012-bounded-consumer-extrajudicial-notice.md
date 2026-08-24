# ADR 0012: Bounded consumer extrajudicial-notice assistant

Status: accepted · Date: 2026-08-04 · Amended: 2026-08-24

## Context

The product originally supports companies receiving a lawsuit. A second
journey can help a consumer organize a dispute with a supplier, connect evidence
to applicable consumer protections and prepare a settlement request without
removing or weakening the business workflow.

That journey has a different risk profile. Messages and uploaded documents can
contain sensitive personal and financial data. Retrieved evidence must not be
confused with legal authority. Statutory text does not establish the outcome
probability or monetary value of a particular dispute. Generating a court
filing or presenting individualized legal advice would also exceed the safe
scope of this demonstration.

## Decision

1. Add a separate consumer mode while preserving the existing
   business mode. Bound the first consumer intake to the complaint, supplier,
   relevant dates, prior protocols, user-confirmed loss, desired resolution and
   supporting PDF or image evidence. Do not silently infer absent facts.
2. Produce a **notificação extrajudicial com proposta de acordo**, not a
   lawsuit, initial petition or court filing. The application only creates an
   exportable draft; it does not send, file or represent the consumer. A human
   reviewer remains responsible for every use of the artifact.
3. Retrieve legal grounds from versioned sources: an integrity-checked snapshot
   of the complete compiled
   [Consumer Defense Code](https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm)
   and an explicitly identified set of provisions transcribed from the official
   compiled [Brazilian Constitution](https://www.planalto.gov.br/ccivil_03/constituicao/constituicaocompilado.htm),
   with editorial summaries kept separate from official text.
   Each entry has a stable provision/subdivision ID, source, article reference,
   official URL, corpus release, verification date and content hashes. The CDC
   official text is the primary authority; summaries remain navigation aids.
   ADR 0013 defines the statutory ingestion and retrieval details.
4. Keep distinct provenance chains. Evidence references identify the original
   file hash, extracted-text hash, page and retrieved chunk supporting a factual
   assertion. Financial components point either to a consumer-confirmed fact or
   to a selected monetary candidate with file, page, excerpt and hashes. Legal
   references identify the provision, corpus release and official source
   supporting a legal ground. The notice composer cannot convert one type into
   another or cite material that was not supplied to it.
5. Treat chat messages, uploaded documents and retrieved passages as untrusted
   data. Scan
   every uploaded document before indexing. Quarantine documents that require
   review or are blocked by the deterministic prompt-injection policy; mask
   flagged medium-risk excerpts from downstream prompts while retaining the
   finding for audit. Document text cannot change system policy or authorize an
   external action.
6. Calculate settlement values as transparent scenarios. Monetary values found
   by text extraction or OCR are candidates only and require explicit consumer
   confirmation before use. Amounts mentioned in chat remain unconfirmed
   narrative and must not be promoted automatically to calculation inputs. The
   public proposal is limited to confirmed direct loss plus any clearly labeled,
   conditional legal increment; it does
   not accept a consumer-selected additional compensation amount. Preserve the
   formula, component sources and a canonical calculation hash. Do not calculate
   success likelihood, expected value or inferred damages from the Constitution
   or CDC.
7. Assemble the final notice deterministically from confirmed facts, accepted
   evidence and retrieved legal grounds. Mark uncertainty and missing evidence
   rather than asking the language model to fill gaps. Exports carry the review
   warning and source provenance.
8. Do not position the consumer mode as production-ready until all of these
   blockers are addressed:
   - authenticated users, authorization and tenant-isolated storage/retrieval;
   - an LGPD-compliant lawful basis, consent/notice where applicable, data
     minimization, encryption, access logs, deletion and retention policy;
   - qualified-lawyer review and a documented boundary consistent with the OAB
     rules governing legal advice and representation; and
   - representative, reviewed outcome data plus calibration and monitoring
     before any empirical probability or outcome prediction is presented.

## Consequences

- (+) Consumers receive a structured, auditable negotiation draft without the
  product claiming to file a case or replace counsel.
- (+) Separate evidence and legal provenance makes every factual and legal
  support path reviewable and prevents a statute summary from masquerading as
  case evidence.
- (+) A versioned corpus makes legal-source drift detectable and permits a
  release to be withdrawn when legislation changes.
- (+) Scenario calculations expose only confirmed amounts and conditional legal
  increments, without pseudo-probabilities or expected-value precision.
- (-) The initial category set will not cover every consumer dispute, procedural
  remedy, limitation issue or jurisdiction-specific practice.
- (-) Curated summaries require legal review and ongoing version maintenance;
  links to official sources do not automate that governance.
- (-) Export-only, human-reviewed drafts provide less automation, but avoid an
  irreversible filing or communication based on incomplete facts.
