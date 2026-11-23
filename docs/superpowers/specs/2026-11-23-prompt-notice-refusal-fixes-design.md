# Prompt-notice refusal fixes — design

Date: 2026-11-23 · Status: approved in chat ("Implement" fixes 1, 2, 4 and 5)

## Context

A real prompt notice was refused with "a recuperação não encontrou suporte
verificável em fundamentos jurídicos". The complaint was 2,812 characters
long: the wrong tablet model was delivered, and the consumer asked for
forced fulfilment of the offer under CDC arts. 30 and 35, I. The case was
reproduced on the configured stack (JUÁ 4B + PostgreSQL BM25) through the
production retrieval and ground-selection code. It failed for four reasons
that compound:

1. **No remedy was recognised.** Request detection matched only "quero",
   "desejo" and "solicito", so `desired_resolution` stayed empty.
2. **63% of the text was cut before searching.** The narrative keeps the
   first 1,050 characters, and the cut fell before the consumer's own
   "artigos 30 e 35, inciso I, do CDC".
3. **The two retrieval channels disagreed.** JUÁ's top 10 held CDC 35
   I/II/III, 18 and 25, while BM25's top 10 was alias noise. No chunk agreed,
   so no ground was cited.
4. **The fallback could only refuse.** With the lexical-only fallback (a cold
   start inside the 300 s embedding timeout) and no remedy, there was only
   one corroboration query, so a refusal was certain.

The refusal then said "Tente novamente" with HTTP 503, but a retry repeats
exactly the same outcome. The prompt route also deleted the case, so the
retrieval audit was lost.

Measured on that case, with the production gate at depth 10:
- recognising the remedy cites 35 I;
- parsing the consumer's own citations resolves 30 and 35 I deterministically;
- raising the gate depth to 20 would cite CDC art. 5 III (police stations)
  through an LLM alias, so loosening the gate is not the fix.

## Decisions

1. **Request sentences (intake).** A sentence counts as a request when it
   holds one of these first-person request verbs: quero, queria, gostaria,
   desejo, solicito, peço/peco, exijo, requeiro.
   - Nouns are left out. "meu pedido" and "minha solicitação" also name the
     order or an earlier request inside the account ("meu pedido não
     chegou"), and a sentence moved by mistake is printed as a request.
   - The prompt route gains an optional `desired_resolution` form field. When
     it is sent, `text` is the account verbatim and nothing is moved between
     the two.
2. **Consumer-cited provisions (ground selection).** An explicit citation
   that names a law in the complaint or the remedy resolves to a citable unit.
   - Laws are recognised by acronym, name or law number: CDC, LGPD, Código
     Civil.
   - With no subdivision, the citation resolves to the caput. It also
     resolves "§ / parágrafo único / inciso / alínea", and the reverse order
     "§ 1º do art. 18".
   - The resolved unit becomes a ground without the agreement gate. That gate
     decides whether the system's own search may cite an article, while a
     provision the consumer names is the consumer's claim.
   - Everything else still applies: the scope gate, active and indexed units
     only, statute-structure eligibility, and refusal of defense/exclusion
     units (the ADR 0022 rule).
   - So do the limits: 3 complementary grounds, the CDC anchor, 8 grounds in
     total, and the optional verifier.
   - There is one ground per cited unit. The consumer's unit replaces a
     different retrieved unit of the same article. A retrieved ground for the
     same unit, or for any unit when the article was cited whole, is kept with
     its ranks.
   - Every such ground carries `LegalGround.cited_by_consumer = true` and a
     rationale that says so. The ground policy version moves to v7.
   - These forms are ignored: an article with no law named, ranges ("arts. 18
     a 25"), the law named first ("o CDC, em seu art. 35") and the
     Constitution.
3. **Startup warm-up.** The lifespan starts a background task that loads a
   lazily loaded local query model and runs one query.
   - The setting `embedding_warm_up_on_startup` defaults to true.
   - `/health` reports `query_embedding_warm`.
   - A failure is logged and never stops startup.
4. **Actionable refusals.** A refusal is a `NoticeRefusal`: reasons, hints, a
   `retryable` flag and the retrieval traces. It is stored on the case and
   served at `GET /consumer/cases/{id}/notice/refusal`.
   - HTTP 422 when the outcome is deterministic, 503 when retrieval was
     degraded (a retry may help).
   - The detail is `{message, reasons, hints, refusal_url}`. On the prompt
     route it also carries `case_id` and `case_token`, and that route now
     keeps the case after a refusal.
   - Reasons: `semantic_retrieval_unavailable`, `no_requested_resolution`,
     `narrative_truncated`, `no_retrieval_agreement`, `no_eligible_ground`,
     `grounds_removed_by_verifier`, `no_evidence_support`.
   - The Streamlit client shows the hints under the message.

Out of scope, proposed separately:
- sentence windows for long complaints;
- long prompt-path evaluation cases and an in-scope refusal-rate gate;
- LLM issue queries, an alias review, and excluding CDC arts. 4–5.

## Evidence required

- The offline CI evaluations (development and holdout, retrieval and notice)
  must give identical results before and after, except
  `ground_policy_version`. The golden set has no explicit citation, and the
  evaluation does not go through intake.
- The real complaint must produce a notice through the production code, and
  the case's text must never be committed.
