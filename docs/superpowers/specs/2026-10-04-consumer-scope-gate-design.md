# Relationship-class scope gate and an optional LLM scope check — design

Date: 2026-10-04 · Follows [ADR 0023](../../adr/0023-agreement-depth-and-alias-cap.md) ·
Amends the scope rule of [ADR 0018](../../adr/0018-narrative-only-consumer-intake.md)

## 1. Problem

`is_consumer_scope` (`app/consumer/retrieval.py`) decides whether a complaint may be answered
with consumer law. It is a deny-list: a complaint abstains only when it contains a listed
non-consumer phrase (labour, inheritance, traffic fines, "meu vizinho", private loans) and
no clause shows a consumer relationship. Everything else counts as in scope.

The list fits the four development out-of-scope cases exactly, one phrase per case. None of
the six holdout out-of-scope cases contains a listed phrase. On the configured stack
(ADR 0023), four of them still get legal grounds:

- a car crash between private parties;
- a condominium fee;
- a tenancy deposit;
- a business customer's unpaid invoice.

Holdout abstention is 0.333 (v4: 0.500) and holdout precision 0.281 (v4: 0.336). The change is
published on `main`.

## 2. Intent and success criteria

The user's goal is fewer notices citing consumer law for complaints that are not consumer
relationships, without losing real consumer cases.

On the development split, with the deterministic gate alone:

- **Zero false abstentions.** Every labelled in-scope case stays in scope: the 33 existing
  ones and the new in-scope look-alikes (§5.1). A lost in-scope case means a consumer gets no
  notice, so this is a hard requirement.
- **At least 16 of the 20 new out-of-scope development cases abstain (80%),** and all four
  existing ones still do.
- **The configured (JUÁ) development notice numbers on the existing cases are unchanged:**
  precision 0.491, article recall 0.592, known-bad 0. Their scope decisions must not change.

The optional LLM check (§4) is measured on top of the deterministic gate and reported: how
many remaining out-of-scope cases it removes, and any in-scope case it rejects. The holdout
is reported once, for both modes.

**Honesty constraint.** The author of this design has read the six holdout out-of-scope
cases, and they fall inside the relationship classes below. After this change the holdout
is therefore **not a blind test of scope**. ADR 0024 records this, and a fresh blind holdout
written by the user or a colleague is the follow-up that measures generalisation.

## 3. The deterministic gate

### 3.1 Module

The gate moves to a new module, `app/consumer/scope.py`, with:

- the normalisation and clause helpers that today sit in `retrieval.py`
  (`_scope_normalize`, `_scope_contains_any`, `_scope_clauses`,
  `_clause_has_consumer_relationship` and the signal tuples);
- `is_consumer_scope(*, complaint: str) -> bool`, with its signature unchanged;
- `assess_scope(*, complaint: str) -> ScopeAssessment`, where `ScopeAssessment` is a frozen
  pydantic model with `in_scope: bool`, `relationship: NonConsumerRelationship | None` and
  `signal: str | None`. `relationship` and `signal` are the class and the folded phrase that
  made the gate abstain; both are `None` when it does not.

`is_consumer_scope` returns `assess_scope(...).in_scope`. Every caller imports from
`app.consumer.scope`:

- `ground_selection.py`;
- `service.py` (two places);
- `evaluation/consumer_notice.py`;
- `evaluation/consumer_runner.py`;
- `evaluation/label_ranks.py`;
- `evaluation/query_vectors.py`;
- the tests.

`retrieval.py` no longer defines the gate.

### 3.2 Relationship classes

`NonConsumerRelationship` is a `StrEnum`. Each class has a tuple of signals, accent- and
case-folded the way `_scope_normalize` folds them, written from what defines the
relationship in law rather than from any evaluation case:

| class | defining relationship | example signals (non-exhaustive) |
|---|---|---|
| `state` | the other party is the State as authority or tax collector | imposto, receita federal, IPTU, IPVA, INSS, benefício previdenciário, aposentadoria negada, multa de trânsito, DETRAN, prefeitura (as authority), concurso público |
| `tenancy` | landlord and tenant, or condominium (Lei 8.245; Código Civil) | aluguel, locador, locatário, inquilino, dono do imóvel, proprietário do apartamento, caução, condomínio, síndico, taxa condominial |
| `employment` | employer and employee | today's labour signals (salário, empregador, hora extra, demissão, vale-transporte, contracheque, vínculo empregatício, …) |
| `family` | family and succession | pensão alimentícia, guarda, divórcio, herança, inventário, partilha |
| `private_parties` | two private individuals, neither acting professionally | meu vizinho, um amigo, meu primo, emprestei dinheiro, o outro motorista, bateu no meu carro, comprei de um particular, vendi meu carro para |
| `complainant_supplier` | the complainant is the professional side | meu cliente, minha empresa, a empresa que eu tenho, vendi para, prestei serviço para, minha loja |

The signal lists are written once, from these definitions, and adjusted only on development
evidence. Each adjustment is recorded in the plan's ledger.

Public utilities run by concessionaires (water, energy, telephony, transport) stay in scope,
because the CDC covers them. `state` signals name the State as authority, not a
concessionaire.

### 3.3 Override and default

- **The consumer-clause override stays.** A clause that also shows a consumer relationship
  keeps the case in scope: a business counterparty plus a transaction or service, or the
  existing bank-account rule. The existing labour-specific exception moves with it.
  "O condomínio contratou uma empresa que cobrou a mais" is not decided by "condomínio"
  alone.
- **The default stays in scope.** A complaint with no class signal passes, as today. No
  in-scope case can be lost to a missing word; the optional LLM check (§4) is the backstop
  for unrecognised non-consumer complaints.

## 4. The optional LLM scope check

### 4.1 Configuration and wiring

- `ScopeVerifierMode` (`none` | `llm`) sits in `app/core/config.py` beside
  `GroundVerifierMode`. The setting is `scope_verifier`, environment variable
  `LITIGATION_SCOPE_VERIFIER`, default `none`. `scope_verifier_max_output_tokens` follows the
  ground verifier's bounds (default 1,000, between 256 and 16,384).
- A new module, `app/consumer/scope_verifier.py`, provides:
  - a `ScopeVerifier` protocol with `async verify(complaint: str) -> ScopeVerification`;
  - `NoScopeVerifier`, which always returns an unchanged in-scope verdict;
  - `LLMScopeVerifier`;
  - `create_scope_verifier(settings)`, wired in `app/api/main.py` the way
    `create_ground_verifier` is, and passed to `ConsumerCaseService` as `scope_verifier`.

### 4.2 Behaviour

- **When it runs.** Only when the deterministic gate says in scope, and before legal
  retrieval, so a rejected case costs no retrieval. It can only move a case out of scope,
  never into it.
- **What the model is asked.** Whether the complaint describes a consumer relationship as
  CDC arts. 2–3 define it: a final consumer and a supplier acting professionally. A
  data-protection complaint against an organisation (LGPD) also counts.
- **What it must answer.** `consumer`, `not_consumer` or `uncertain`, with a relationship
  type (one of the §3.2 classes, or `other`). For `not_consumer`, it must give an exact quote
  from the complaint showing the non-consumer relationship.
- **Code decides, not the model.** The quote must be a verbatim substring of the complaint
  and at least 12 characters long (whitespace-normalised, the ground verifier's rule). Only
  `not_consumer` with a verified quote removes the case from scope.
- **Failing safe.** `uncertain`, a missing or unverifiable quote, a malformed answer and any
  provider error all keep the deterministic decision (in scope). The failure is recorded.
- **Prompt.** `consumer-scope-verifier:v1`, temperature 0, or the reasoning-effort path for
  reasoning models, as the ground verifier does.

### 4.3 Where the service applies it, and traceability

`ScopeVerification` is a frozen pydantic model in `app/consumer/schemas.py`. It records the
verdict, the relationship type, the verified quote (or none), the failure reason (or none),
the prompt version and the `complaint_sha256` it was computed for.

Today the service uses the deterministic gate in two places, and the check joins both:

- **The one-call prompt path.** `start_prompt_case` (`service.py:365`) is synchronous and
  keeps the deterministic gate. The asynchronous verifier therefore runs at the start of
  `generate_prompt_notice`, before legal retrieval. A removal raises the same
  `ConsumerPromptNoticeError` ("O relato não caracteriza relação de consumo elegível para
  este rascunho."), which the route maps to 422 like the deterministic refusal. The case
  already exists by then and records the refusal.
- **The case path.** When a notice is generated, and before legal retrieval, the verifier
  runs if the deterministic gate says in scope. Its result is stored on the case record as
  `scope_verification`. A removal refuses the generation with the same out-of-scope message.
  The synchronous readiness check (`_readiness_missing`, `service.py:763`) then lists
  `consumer_relationship` as missing for as long as the stored verification's
  `complaint_sha256` matches the current complaint. If the complaint is edited, the stored
  verdict no longer applies and the check runs again at the next generation.

The case record keeps the `ScopeVerification`. The deterministic `ScopeAssessment` is a pure
function of the complaint, so it is recomputed rather than stored. Between them, the audit
shows which rule or which verified quote made a case abstain.

- **The notice evaluation** counts:
  - `consumer_notice_scope_verifier_removed`, cases the check moved out of scope;
  - `consumer_notice_scope_verifier_failures`.

  This mirrors the ground verifier's `verifier_removed` and `verifier_failures`.

### 4.4 Evaluation flag

`consumer_runner --scope-verifier {none,llm}` (default `none`) runs the check inside the
notice evaluation, beside `--ground-verifier`. CI and the default runs stay deterministic.

## 5. Evaluation data and measurement

### 5.1 New development cases (dataset 2.3.0 → 2.4.0)

The cases are written **before** any gate code and reviewed by the user before they are
committed:

- **20 out-of-scope development cases.** They cover all six classes, plus a criminal
  matter and a dispute between business partners. Each class gets several wordings, and some
  avoid the class's obvious keyword ("o dono do apartamento", "o rapaz que bateu no meu
  carro"). They do not copy the holdout cases' scenarios or wording.
- **8 in-scope look-alikes.** These are consumer cases that mention a non-consumer
  word:
  - a bank deducting an unrequested insurance from an INSS benefit;
  - a private school raising a child's fees;
  - a moving company damaging furniture;
  - a ride-hailing driver causing an accident;
  - an energy concessionaire cutting power;
  - a used car bought from a dealer;
  - a condominium's contracted company overcharging the resident;
  - a health plan denying a family member's procedure.

  Each gets conservative article labels drafted for the user's review.

All new cases are `split: development`, and the existing cases are unchanged.
`eval_data/consumer_legal_retrieval/dataset.json` gets the version 2.4.0, and its
corpus-hash pin is unchanged.

### 5.2 Query vectors

`query_vectors.golden_queries` caches only in-scope cases' queries. The new in-scope cases,
and any new out-of-scope case the deterministic gate keeps in scope, need JUÁ query vectors
before configured runs. The cache is filled one case per process
(`python -m app.evaluation.query_vectors --case <id>`, JUÁ environment), resumable; it is a
long CPU run, confirmed with the user first. `--check` must then report full coverage.

### 5.3 Measurement protocol

1. **Gate decisions (pure, no retrieval).** For every dataset case, record the deterministic
   decision and class. This checks the zero-false-abstention requirement and the 80%
   out-of-scope bar.
2. **Offline notice evaluation,** development and holdout, before (`main`) and after.
3. **Configured (JUÁ) notice evaluation, deterministic gate.**
   - **Development.** The existing cases must reproduce ADR 0023's numbers. Totals change only
     through the new cases.
   - **Holdout.** Reported once.
4. **Optional LLM check, configured stack.** `--scope-verifier llm` with the configured model
   (gpt-5.6-terra), on development and then holdout. That is one call per case that reaches
   the check, roughly 60 calls; confirmed with the user before running.
5. **Comparisons.** Paired `app.evaluation.compare` against `main`'s configured runs, which
   are re-measured on dataset 2.4.0 so the cases pair.

### 5.4 Stop rules

Stop and report to the user:

- if any labelled in-scope case abstains;
- if the configured development numbers on the existing cases change;
- if the out-of-scope bar is not met after the ledgered development adjustments.

## 6. Records

- **ADR 0024**, "Relationship-class scope gate and an optional LLM scope check". It amends
  ADR 0018 and records: the problem, the classes, the override, the LLM check and its fail-safe
  rules, the measurements in both modes, the honesty constraint (holdout not blind for scope)
  and the blind-holdout follow-up.
- **Docs.**
  - `README.md` and `README-pt.md` gain a short `LITIGATION_SCOPE_VERIFIER` note.
  - `docs/architecture.md` updates its scope description and lists ADR 0024.
  - `.env.example` gains `LITIGATION_SCOPE_VERIFIER=none`.

## 7. Tests

Each test is written first and seen to fail.

- **Gate.**
  - One test per relationship class: a signal makes the gate abstain and `assess_scope`
    reports that class and signal.
  - The consumer-clause override keeps a class-signal complaint in scope.
  - A complaint with no signal stays in scope.
  - Concessionaires stay in scope.
  - The existing `tests/test_consumer_retrieval.py` scope assertions keep passing, with
    imports moved to `app.consumer.scope`.
- **Verifier, with a fake LLM:**
  - `not_consumer` with a verbatim quote removes;
  - a paraphrased quote, `uncertain`, a malformed answer and a provider error each keep the
    case in scope and record why;
  - a quote shorter than 12 characters is rejected;
  - `NoScopeVerifier` never removes;
  - `create_scope_verifier` honours the setting.
- **Service.**
  - The verifier is called only for deterministically in-scope cases.
  - A removal yields no legal grounds and records both assessments.
- **Evaluation.**
  - `--scope-verifier` is accepted.
  - The two counts appear.
  - Default runs never construct an LLM verifier.
- **Dataset.** The counts by split and scope are right, every new label resolves in the
  corpus, and the version is 2.4.0.

Coverage stays at 100%; ruff, mypy, import-linter and vulture stay clean.

## 8. Non-goals

- Changing the agreement gate, the alias cap or retrieval.
- A per-ground relevance check: the existing ground verifier covers that.
- Writing the blind holdout. That is the user's follow-up.
- Pushing, or merging without the user.
