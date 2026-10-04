# ADR 0023: Agreement depth 10 and an alias support cap

## Status

Accepted · Date: 2026-10-03 · Amends: [ADR 0019](0019-explicit-retrieval-agreement.md),
[ADR 0022](0022-lay-language-alias-chunks.md)

## Context

The lay alias chunks of ADR 0022 lifted retrieval on the configured stack (JUÁ,
development: article recall@5 0.438 → 0.733), but the notice cited 67 grounds against v4's
34. Development precision fell 0.390 → 0.312 and the known-bad count rose 1 → 2, so the stop
rules of the alias spec fired.

The cause sits in the agreement gate of ADR 0019. An alias chunk is a few lay sentences, and
the dense and the lexical channel both score the same paraphrase, so its two-channel
"agreement" is not two independent signals. 66 of the 67 development grounds passed the gate
through an alias.

## Decision

1. `AGREEMENT_MAX_RANK` goes from 13 to 10. Depth 13 was chosen before aliases, when every
   exact hit ranked within 12 in both channels.
2. On the two-channel path each query contributes at most `ALIAS_SUPPORT_CAP` = 3 alias
   chunks to the supported set, best rank first. An alias whose channels do not agree takes no
   slot, and official chunks are never capped. The degraded single-channel path (top 3 in two
   independent queries) is unchanged, since a cap of 3 cannot bind there.
3. The ground policy version becomes `consumer-notice-scope-eligibility-v6`.

Nothing else changes: the index, the aliases, citations, the eligibility policy, the CDC
anchor, ground selection and retrieval. Retrieval metrics do not pass through the gate.

## Evidence (spikes, JUÁ, development, 2026-10-03)

The spikes ran with the golden query cache and the v5 index. They replaced the gate or the
retrieval inside a throwaway script, and nothing was tuned on the holdout. Intervals are 95%
paired bootstrap intervals against v4 (depth 13, no aliases).

| configuration | precision | article recall (interval vs v4) | unlabelled | known-bad |
|---|---|---|---|---|
| depth 13 (corpus v5 as released by ADR 0022) | 0.312 | 0.608 [+0.013, +0.375] | 48 | 2 |
| depth 12 | 0.308 | 0.592 [−0.004, +0.371] | 47 | 1 |
| depth 10 | 0.457 | 0.592 [−0.004, +0.371] | 29 | 1 |
| **depth 10, at most 3 alias chunks per query** | **0.491** | **0.592 [−0.004, +0.371]** | **24** | **0** |
| depth 10, at most 2 alias chunks per query | 0.546 | 0.517 [−0.117, +0.325] | 19 | 0 |
| depth 8 | 0.430 | 0.475 [−0.150, +0.258] | 26 | 0 |
| depth 10, alias passes only beside its official text in the dense pool | 0.583 | 0.367 | 10 | 1 |
| grounds per notice capped at 4 or 5 | within 0.02 of uncapped | unchanged | | |

What the spikes ruled out:

- **Collapsing sibling units before the k = 8 cut** (review step 4 as first proposed) changed
  no ground in any of the 20 in-scope development cases. Each query's top 8 counted distinct
  provisions instead of chunks, and that admitted one extra gate-passing chunk in total. A
  chunk both channels rank within the gate depth already sits inside its query's fused top 8,
  and ground selection already works per provision.
- **A depth for alias chunks only** gave the same numbers as the global depth, because almost
  every ground comes through an alias.
- **Requiring the official text to back an alias** removes the recall the aliases exist for.

## Measurements (configured stack)

The production gate reproduced the spike exactly on development.

| Development (24 cases) | v4 | v5, depth 13 | **v5, depth 10 + cap 3** | release − v4, 95% interval |
|---|---|---|---|---|
| notice article recall | 0.421 | 0.608 | **0.592** | +0.171 [−0.004, +0.371] |
| notice exact recall | 0.225 | 0.400 | **0.400** | +0.175 [−0.008, +0.342] |
| notice precision | 0.390 | 0.312 | **0.491** | +0.084 [−0.147, +0.299] (17 paired cases) |
| grounds / labelled / unlabelled | 34 / 13 / 20 | 67 / 17 / 48 | **40 / 16 / 24** | labelled +3 [−3, +9]; unlabelled +4 [−7, +15] |
| known-bad citations | 1 | 2 | **0** | −1 [−3, 0] |

Against v5 at depth 13, the step alone:
- removes 24 unlabelled grounds, interval [−34, −15];
- removes both known-bad citations;
- raises precision by +0.162, interval [+0.074, +0.278];
- costs 0.016 article recall, interval [−0.050, 0] — one labelled ground.

| Holdout (19 cases, measured once, reported only) | v4 | v5, depth 13 | **v5, depth 10 + cap 3** |
|---|---|---|---|
| notice article recall | 0.397 | 0.590 | **0.551** |
| notice exact recall | 0.019 | 0.346 | **0.346** |
| notice precision | 0.336 | 0.229 | **0.281** |
| notice abstention | 0.500 | 0.167 | **0.333** |
| known-bad citations | 2 | 2 | **2** |

The holdout's two known-bad citations are both on out-of-scope complaints: CDC art. 42 sole
paragraph on a business customer's unpaid invoice, and CDC art. 51 XVI on a tenancy deposit.
Of the six out-of-scope holdout complaints, four still get grounds: these two, a car crash
between private parties and a condominium fee. The social-security denial no longer does. The
scope gate lets these complaints through; that is not this step's lever.

**Offline (regression tripwire).**
- Development: 22 grounds, 6 labelled, 15 unlabelled, 1 known-bad. Precision 0.292, article
  recall 0.225. Corpus v5 at depth 13 gave 31 / 8 / 22 / 1, 0.289 and 0.267.
- Holdout: 21 grounds, 6 labelled, 14 unlabelled, 1 known-bad.
- The CI notice gates are re-baselined to known-bad ≤ 1 and labelled ≥ 6. The single offline
  known-bad citation is still CDC art. 42 sole paragraph on `cobranca_com_ameacas`, which the
  configured stack no longer cites.

## Consequences

- (+) Development precision 0.491 against v4's 0.390, no known-bad citation, and every exact
  hit kept (exact recall 0.400 at depths 10 and 13).
- (+) Article recall +0.171 over v4, keeping most of the +0.187 the aliases brought at depth 13.
- (−) The recall gain's 95% interval touches zero (lower bound −0.004). The user accepted it
  as meeting the bar.
- (−) Precision is sensitive to the depth on 20 paired cases (0.457 at 10, 0.308 at 12).
  Re-measure the depth whenever the aliases or the corpus change.
- (−) The crowding of ADR 0022 shrinks in the notice, not in retrieval: hub aliases still fill
  retrieval slots.
- (−) On the holdout, precision stays below v4 (0.281 against 0.336) and out-of-scope
  complaints still get grounds (abstention 0.333 against 0.500), while recall rises (0.551
  against 0.397). The scope gate, not the agreement gate, is the next lever.
