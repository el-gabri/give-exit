# ADR 0020: The LGPD may ground a notice alone

## Status

Accepted · Date: 2026-09-24 · Amends: [ADR 0016](0016-multi-statute-consumer-corpus.md)

## Context

ADR 0016 added the LGPD and part of the Civil Code as complementary sources:
at most three of their grounds per notice, none under lexical-only retrieval,
and only beside a CDC ground. The CDC anchor was meant to keep off-topic
complementary grounds out of notices. On the configured stack the off-topic
ones were Civil Code contract types (Título VI), which ADR 0016 then excluded.

The first full notice evaluation on the configured stack (JUÁ 4B and
PostgreSQL, 2026-09-24; see ADR 0019, "Configured-stack measurement") cited
nothing in two data-protection cases whose ranked retrieval, measured in the
same run, was led by LGPD articles:

- `vazamento_de_dados_cadastrais`: the top five candidates were all LGPD
  (arts. 42 § 1 II, 44, 44 II, 45 and 11 § 1).
- `compartilhamento_de_dados_sem_consentimento`: LGPD arts. 7 § 5 and 5 XVI
  led the list.

Whenever no CDC article clears the agreement gate, the anchor removes every
LGPD ground that did, so such a case ends without grounds unless a
constitutional provision cleared the gate, and the service answers it with a
503 asking for a retry. The configured run under this ADR confirms it: the
same retrieval then cites four LGPD grounds in those two cases, which had
cleared the gate and been dropped by the anchor. A consumer whose
complaint is about how a supplier handled their personal data gets no notice,
although the LGPD is the statute that governs exactly that.

## Decision

1. **LGPD grounds need no CDC ground.** Only the Civil Code stays in
   `CDC_ANCHORED_SOURCES`: its grounds still stand only beside a CDC ground,
   and an LGPD ground does not anchor them in the CDC's place. The optional
   ground verifier (ADR 0019) applies the same rule after removing grounds.
2. **The LGPD keeps its other limits.** It still shares the three
   complementary slots with the Civil Code, is still not admitted under
   lexical-only retrieval, and its chapters on processing by public bodies,
   sanctions, the national authority and final provisions remain uncitable.
3. The ground policy version becomes `consumer-notice-scope-eligibility-v5`.

## Measurements (offline stack: hashed-BOW embeddings, in-process BM25)

| Metric | Before | After |
|---|---|---|
| notice grounds (22 cases) | 20 | 24 |
| notice complementary grounds | 3 | 7 |
| notice known-bad citations | 0 | 0 |
| notice exact recall | 0.017 | 0.017 |

Offline, `vazamento_de_dados_cadastrais` now cites LGPD arts. 5, 18 VII and 47
(the security duty) and `pedido_de_exclusao_de_dados_ignorado` cites art. 50
§ 2; both cited nothing before. The offline dense channel is a hashed bag of
words, so which articles it picks is weak evidence. The configured stack
decides: `python -m app.evaluation.consumer_runner --evaluate-notice
--notice-pipeline configured`.

## Consequences

- (+) A data-protection complaint gets a notice grounded in the statute that
  regulates the conduct, instead of a refusal.
- (+) The Civil Code keeps the anchor that the configured-stack evidence
  supports.
- (-) An LGPD-only notice has no CDC ground, and nothing in the selection
  checks that the supplier acted as a controller or processor of the
  consumer's data. As with every ground, applicability is left to legal
  review (`requires_legal_review`); the optional verifier can remove a ground
  whose text does not match the account.

## Configured-stack result (JUÁ 4B, PostgreSQL, 2026-09-24)

With this rule and agreement depth 20 the notice evaluation cites 57 grounds
instead of 53, the four new ones LGPD grounds in the two cases that cited
nothing. Known-bad citations and exact recall (0.225) did not change.

- `vazamento_de_dados_cadastrais`: LGPD arts. 42 § 1 II (joint liability of
  the processor) and 44 (processing is irregular without the security the
  data subject can expect).
- `compartilhamento_de_dados_sem_consentimento`: LGPD arts. 7 § 5 (sharing
  needs specific consent) and 5 XVI.

Neither case cites a labelled unit (42, 46, 48 and CDC 14; 18 VII, 7 I and
CDC 43), so exact recall does not move, but every new ground is on topic. The
optional verifier kept arts. 42 § 1 II, 44 and 7 § 5. At depth 12 both cases
still have grounds (ADR 0019).
