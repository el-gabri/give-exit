# ADR 0024: Relationship-class scope gate and an optional LLM scope check

## Status

Accepted · Date: 2026-10-04 · Amends: [ADR 0018](0018-narrative-only-consumer-intake.md)

## Context

The scope gate decides whether a complaint may be answered with consumer law. It was a
deny-list: a complaint abstained only when it contained a listed non-consumer phrase
(labour, inheritance, traffic fines, "meu vizinho", private loans) and no clause showed a
consumer relationship. The list fitted the four development out-of-scope cases exactly,
one phrase per case, and none of the six holdout out-of-scope cases contained a listed
phrase. On the configured stack (ADR 0023), four of those six received legal grounds: a
car crash between private parties, a condominium fee, a tenancy deposit and a business
customer's unpaid invoice. Holdout abstention was 0.333 (v4: 0.500) and holdout
precision 0.281 (v4: 0.336).

## Decision

1. The gate moves to `app/consumer/scope.py` and recognises six non-consumer
   relationship classes instead of ad-hoc phrases. The signals are written from what
   defines each relationship in law:
   - **State.** The other party is the State as authority or tax collector: "imposto",
     "INSS", "prefeitura", "multa de trânsito", "creche pública".
   - **Tenancy.** Landlord and tenant, or condominium: "moro de aluguel", "inquilino",
     "condomínio", "síndico".
   - **Employment.** Employer and employee: "salário", "FGTS", "patrão", "mandado embora".
   - **Family.** Family and succession: "pensão", "ex-mulher", "herança", "inventário".
   - **Private parties.** Two individuals, neither acting professionally: "meu vizinho",
     "o outro motorista", "de uma pessoa", "vendi meu".
   - **Complainant as supplier.** The complainant is the professional side: "meu cliente",
     "minha empresa", "me contratou".

   Signals match whole words, a plural "s" allowed, so "suspensão" does not read as
   "pensão" nor "compartilhamento" as "partilha". "aluguel", "locação" and "locatário"
   alone are not tenancy, because car rentals are consumer contracts. "aviso prévio",
   "visita", and a friend or a child merely mentioned are not class signals, because
   consumer complaints use them every day. A complaint that names a business (a store, an
   app, a bank, a parking lot) is not a dispute between private parties.
2. **The consumer-clause override stays.** A clause naming a business counterparty
   together with a transaction or service keeps the case in scope, so "comprei um sofá
   numa loja e a entrega no meu condomínio atrasou" stays in. Two exceptions:
   - An employment clause still needs a personal card or invoice charge, as before.
   - A clause naming the complainant as supplier never satisfies the override, because the
     company named there is the complainant's customer. The complainant's occupation
     alone ("sou autônomo") does not count: a self-employed person still buys as a
     consumer.

   **The default stays in scope.** A missing word must never cost a consumer the notice.
   `assess_scope` reports the class and phrase behind every abstention.
3. **An optional LLM scope check,** `LITIGATION_SCOPE_VERIFIER=llm` (off by default), runs
   only on complaints the gate keeps in scope, and can only move a case out of scope.
   - The model answers `consumer`, `not_consumer` or `uncertain`, with the relationship
     type and an exact quote.
   - Only `not_consumer` with a quote code finds verbatim in the complaint (at least 12
     characters) removes the case. `uncertain`, a bad quote, a malformed answer or a
     provider error keeps it, and the failure is recorded.
   - **In the service, the check runs once per complaint text,** at notice generation.
     - On the one-call prompt path, a removal is a 422 with the out-of-scope message.
     - On the case path, it is a readiness refusal (`consumer_relationship`), remembered on
       the case while the complaint text is unchanged.
   - `consumer_runner --scope-verifier llm` measures it.

## Evaluation data

Dataset 2.4.0 adds 28 development cases, written before the gate code and reviewed by the
user:

- **20 out-of-scope cases** across the six classes, plus a theft and a partners' dispute.
  Several avoid the class's obvious keyword.
- **8 consumer cases that mention a non-consumer word:** a bank deducting insurance from
  an INSS pension, a private school's fees, a car bought from a dealer, a sofa delivered
  late to a condominium, among others.

**The holdout is not a blind test of scope.** The author read its six out-of-scope cases
before writing the classes, so their abstention below is reported, not evidence of
generalisation. A fresh holdout written by someone else is the follow-up.

**The development lexicon was adjusted twice to reach the bar,** both from class
definitions:

- the complainant-as-supplier exception to the override (`frete_nao_pago` showed it firing
  on "uma empresa que me contratou");
- the private-parties signal "de uma pessoa".

The development abstention figure below is therefore tuned on development.

## Measurements

The final review's fixes (whole-word signals, narrower tenancy, family and private-party
signals, the named-business rule) change no decision on the 71 dataset cases, so the
measurements below stand.

**Gate decisions.**
- No labelled in-scope case abstains: 41 cases, the 33 earlier ones plus the 8 look-alikes.
- 16 of the 20 new development out-of-scope cases abstain at the gate, and all 4 earlier
  ones. The four that pass name no class signal by design:
  - `proprietaria_quer_que_eu_saia`;
  - `casa_que_o_pai_deixou`;
  - `cliente_da_costureira_nao_pagou`;
  - `bicicleta_furtada`, a theft, which has no class at all.
- On the holdout, 4 of 6 out-of-scope cases abstain at the gate: the INSS benefit, the tax
  refund, the car crash and the condominium fee.

**Configured stack (JUÁ, dataset 2.4.0).** "Before" is the previous gate, measured from
the dataset commit.

| Development (52 cases) | before | **relationship-class gate** | difference, 95% interval |
|---|---|---|---|
| out-of-scope abstention (24) | 0.708 | **1.000** | +0.292 [+0.125, +0.500] |
| grounds / labelled / unlabelled | 56 / 18 / 38 | **46 / 18 / 28** | unlabelled −10 [−18, −4] |
| article recall / exact recall | 0.458 / 0.321 | **0.458 / 0.321** | unchanged |
| precision | 0.344 | **0.449** | paired cases unchanged |
| known-bad citations | 0 | **0** | unchanged |

The 24 earlier development cases cite exactly what they cited under ADR 0023.

| Holdout (19 cases, reported, not blind) | before | **relationship-class gate** |
|---|---|---|
| out-of-scope abstention (6) | 0.333 | **0.667** |
| grounds / unlabelled | 37 / 25 | **32 / 20** |
| precision | 0.281 | **0.321** |
| article recall | 0.551 | **0.551** |
| known-bad citations | 2 | **2** |

Both remaining holdout known-bad citations sit on the two out-of-scope cases the gate lets
through: CDC art. 42 sole paragraph on the business invoice, and CDC art. 51 XVI on the
tenancy deposit.

**The optional LLM check (gpt-4o-mini, 47 calls, no failures) catches every out-of-scope
case the gate misses, but removes real consumer cases too:**

| | Development | Holdout |
|---|---|---|
| out-of-scope cases it removed | 4 of 4 left by the gate | 2 of 2 (abstention 1.0, known-bad 2 → 0) |
| labelled consumer cases it removed | 4: debt collection, credit-registry listing, over-indebtedness, insurance deducted from a pension | 3: a capitalisation bond tied to a loan, early loan payoff, a gym cancellation fee |
| article recall | 0.458 → 0.333 [−0.250, −0.018] | 0.551 → 0.397 |

It removes 7 of the 41 labelled consumer cases, about one in six. With this model the
check is not safe to enable, so it stays off by default. Measuring a stronger model needs a
reasoning-effort setting for the verifier, which reasoning models such as gpt-5.6-terra
require. That is the follow-up.

**Offline stack and CI (regression tripwire).** On development, abstention goes from 0.500
to 0.875, grounds from 45 to 32 and known-bad from 2 to 1. The CI development gates are
re-baselined for dataset 2.4.0:

- **Retrieval:**
  - recall@5 ≥ 0.20;
  - article recall@5 ≥ 0.44;
  - nDCG@5 ≥ 0.15;
  - abstention@5 ≥ 0.83.

  The 24 earlier cases score exactly as before; the 8 look-alikes pull the averages down.
- **Notice:**
  - abstention ≥ 0.87;
  - labelled grounds ≥ 8;
  - known-bad ≤ 1.

Abstention is no longer pinned at 1.0, because four new out-of-scope development cases are
keyword-free by design and CI runs without the LLM check.

## Consequences

- (+) On the configured stack every development out-of-scope complaint abstains. Holdout
  abstention doubles. No labelled consumer case loses a ground.
- (+) Every abstention names its relationship class and phrase.
- (−) Lexical classes still miss relationships no signal names: 4 development and 2
  holdout cases. The LLM check that catches them also drops about one in six real consumer
  cases with gpt-4o-mini, so it stays off.
- (−) CI no longer requires every development out-of-scope case to abstain offline.
- (−) The holdout is spent for scope. A blind holdout written by the user or a colleague is
  needed before claiming the classes generalise.
