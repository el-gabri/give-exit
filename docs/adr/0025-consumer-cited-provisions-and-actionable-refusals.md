# ADR 0025: Consumer-cited provisions, request verbs, query-model warm-up and actionable refusals

## Status

Accepted · Date: 2026-11-23 · Amends: [ADR 0018](0018-narrative-only-consumer-intake.md) (request sentences), [ADR 0019](0019-explicit-retrieval-agreement.md) and [ADR 0023](0023-agreement-depth-and-alias-cap.md) (what the agreement gate governs)

## Context

The first real long prompt notice was refused with "a recuperação não encontrou
suporte verificável em fundamentos jurídicos". The complaint was 2,812
characters. A marketplace seller had delivered the wrong tablet model, and the
consumer asked for forced fulfilment of the offer under CDC arts. 30 and 35, I.
The case was reproduced on the configured stack (JUÁ 4B, PostgreSQL BM25)
through the production retrieval and ground-selection code. Four causes
compounded:

1. **No remedy was recognised.** Intake took a sentence as the requested remedy
   only for "quero", "desejo" or "solicito". The remedy, "Eu queria
   simplesmente receber o produto…", stayed empty.
2. **The narrative keeps 1,050 characters.** 63% of the text was cut before
   retrieval, including the consumer's own "artigos 30 e 35, inciso I, do CDC".
3. **The channels disagreed.** JUÁ's top 10 held CDC 35 I/II/III, 18 and 25.
   BM25's top 10 was lay-alias noise (49, 7 sole paragraph, 12, 28 § 4, 54-F),
   with art. 30 beyond rank 200. No chunk was in both top tens, so there was no
   ground.
4. **A cold start could only refuse.** JUÁ loads inside the first request's
   300 s embedding guard. On a timeout, retrieval falls back to lexical-only,
   where a ground needs the complaint and the remedy to corroborate each other.
   With no remedy there was a single corroboration query, so a refusal was
   certain.

The refusal answered HTTP 503 with "Tente novamente", but a retry repeats
exactly the same outcome. The prompt route then deleted the case, losing the
retrieval audit exactly when it was needed.

The alternatives were measured on that case, with the production gate at depth
10:

| Variant | Grounds cited |
|---|---|
| Remedy extracted | 35 I |
| Whole complaint, no cut | 18 § 4 |
| Sentence windows | 35 I and 18, plus 29, 20 II and 41, which are wrong |
| Gate depth raised to 20 | CDC art. 5 III (police stations for consumer crimes), through an LLM alias |

Loosening the gate is therefore rejected.

## Decision

1. **Request verbs (amends ADR 0018).** A sentence becomes the requested remedy
   when it holds a first-person request verb: quero, queria, gostaria, desejo,
   solicito, peço/peco, exijo or requeiro.
   - Nouns are left out. "Meu pedido" is also the order ("meu pedido não
     chegou"), and "minha solicitação" an earlier request. Both belong to the
     account, and a sentence moved by mistake is printed in the notice as a
     request.
   - `POST /consumer/prompt-notices` takes an optional `desired_resolution`.
     When it is sent, it is the remedy verbatim and `text` is the account,
     unchanged.
2. **Consumer-cited provisions (policy v7).** `app/consumer/cited_provisions.py`
   reads explicit citations in the complaint and the remedy, with the law named
   after the article: CDC, LGPD or Código Civil, by acronym, name or law number.
   - It resolves the forward order ("art. 18, § 1º, II, do CDC") and the reverse
     order ("o § 1º do art. 18 do CDC"). It also resolves lists, paragraphs,
     incisos and alíneas. An article cited whole resolves to its caput.
   - `select_legal_grounds` puts these provisions ahead of the grounds retrieval
     earns, **without the agreement gate**. That gate decides when the system's
     own search may cite an article. A provision the consumer names is the
     consumer's claim, found or not.
   - Every other rule still applies: the scope gate, in-force and indexed units
     only, statute-structure eligibility, and refusal of supplier-defense units
     (the ADR 0022 rule). So do the complementary-source cap, the CDC anchor,
     the 8-ground limit and the optional verifier.
   - A retrieved ground stands in for a citation, and keeps its rank, when it
     quotes the cited unit or the article was cited whole. Otherwise the
     consumer's unit replaces it. A request under art. 35, I (forced
     fulfilment) must not be answered with art. 35, III (a refund).
   - Such grounds carry `LegalGround.cited_by_consumer`, and their rationale
     says the consumer named them.
   - These forms are ignored: an article with no law, ranges, the law named
     first, and the Constitution.
   - Every pattern repetition is bounded. Unbounded ones cost seconds of
     event-loop time on a 10,000-character run of citation words.
3. **Query-model warm-up.** The lifespan starts a background task that loads a
   lazily loaded local query model and runs one query.
   - The warm-up bypasses the query guard, so it neither trips the circuit
     breaker nor holds a slot.
   - A failure is logged and never stops startup.
   - `LITIGATION_EMBEDDING_WARM_UP_ON_STARTUP` (default true) turns it off, and
     `/health` reports `query_embedding_warm`.
4. **Actionable refusals.** A refusal is a `NoticeRefusal`: reasons, one hint
   each in Portuguese, `retryable`, and the retrieval traces. The reasons are
   `semantic_retrieval_unavailable`, `no_requested_resolution`,
   `narrative_truncated`, `no_retrieval_agreement`, `no_eligible_ground`,
   `grounds_removed_by_verifier` and `no_evidence_support`.
   - The response is HTTP 422 when generating again would refuse again, and 503
     only when retrieval ran degraded.
   - The detail is `{message, reasons, hints, refusal_url}`. On the prompt route
     it also carries the case credentials, and the case is kept.
   - `GET /consumer/cases/{id}/notice/refusal` serves the latest refusal, which
     the next notice clears.

## Consequences

- (+) **The motivating complaint now gets a notice.** Through the production
  service on the configured PostgreSQL index:
  - With retrieval degraded to lexical-only, it cites CDC art. 30 (caput) and
    art. 35, I, both `cited_by_consumer`, and says that semantic retrieval was
    unavailable. Before, the outcome was a certain refusal.
  - With JUÁ warmed at startup (`query_embedding_warm` true) and healthy hybrid
    retrieval, it cites the same two provisions. Retrieval ranked CDC art. 35,
    III first, and the consumer's art. 35, I replaced it. Without that rule,
    the notice would ask for the refund the consumer refused.
- (+) **Without its citation, the same complaint no longer fails silently.**
  - Degraded, it is a retryable 503 with three reasons, each with a hint:
    `semantic_retrieval_unavailable`, `narrative_truncated` and
    `no_retrieval_agreement`.
  - Healthy, the recognised remedy now lets both channels agree on CDC art. 35,
    III at rank 1, so a notice is drafted.
- (-) **That last notice cites the wrong remedy.** Art. 35, III is the refund
  the consumer refused. Her forced-fulfilment sentence still falls after the
  1,050-character cut, so retrieval never reads it. Only her explicit citation
  corrects the notice; the sentence-window follow-up addresses the cut itself.
- (+) **The offline CI evaluations are unchanged.** All four (development and
  holdout, retrieval and notice) are identical to `main`, except
  `ground_policy_version` (v6 to v7). The golden set has no explicit citation,
  and the evaluation does not go through intake.
- (+) A refused prompt keeps its audit. In the ground-verifier test, the reason
  codes also showed that the old facts never produced a ground for the
  verifier to reject. The old 503 could not tell those two refusals apart.
- (-) **The agreement gate no longer decides every ground.** A provision the
  consumer cites needs no retrieval support, so a wrong citation by the
  consumer (for example, art. 49 for an in-store purchase) is printed. Three
  things limit the cost:
  - the rationale and `cited_by_consumer` say whose claim it is;
  - eligibility and the defense-unit filter still apply;
  - every notice stays `requires_legal_review`.
- (-) **A passing mention reads as a claim.** For example, "a empresa citou o
  art. 18 do CDC". The defense-unit filter catches supplier defenses only, and
  negation is not interpreted.
- (-) **"Queria" also narrates a past wish** ("eu queria um modelo maior"), and
  such a sentence is printed as a request. The case journey's review corrects
  it, and a prompt notice warns that its facts were not reviewed.
- (-) **Not measured in aggregate on the configured stack.** The golden set has
  neither explicit citations nor long complaints. The refusal rate of in-scope
  complaints is still not a metric.
- (-) **The warm-up loads JUÁ (about 7.7 GB in bf16) at every API start,**
  whether or not a notice is requested. A request that arrives before it
  finishes still waits for the model inside its own timeout.
- (-) **The HTTP contract changes.** Grounding refusals that were 503 are 422
  unless retrieval was degraded, and their `detail` is an object, so clients
  read `detail.message`. The Streamlit client was updated.

## Follow-ups

- **Long complaints.** Use sentence windows instead of the 1,050-character
  cut, with a result-aggregation rule measured on long cases first.
- **Evaluation.** Add long prompt-path evaluation cases, including explicit
  citations, and gate CI on the refusal rate of in-scope complaints.
- **Retrieval.** Three candidates:
  - optional LLM issue queries (the most stable variant on the motivating case,
    but they cross the no-LLM-in-retrieval boundary);
  - a review of the 329 generated aliases;
  - removing CDC arts. 4–5 (consumer-protection policy) from the citable
    divisions.
- **Supplier extraction.** "oficial da Magazine Luiza" was read as the
  supplier, and a second supplier (the marketplace's joint liability) cannot
  be expressed.
