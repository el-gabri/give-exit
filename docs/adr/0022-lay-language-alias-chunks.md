# ADR 0022: Lay-language alias chunks

## Status

Accepted · Date: 2026-10-03 · Amends: [ADR 0013](0013-versioned-consumer-law-retrieval.md),
[ADR 0019](0019-explicit-retrieval-agreement.md)

## Context

The retrieval-agreement gate cites a unit only when both the dense and the lexical
channel rank it within 13, and the lexical channel sets the ceiling: in dataset 2.1.0,
20 of 66 labels shared no token with their case's queries (ADR 0019). CDC art. 39 I
("venda casada") shares none with its case; the water-outage case shares one token with
CDC art. 22. A fixed vocabulary appended to every query diluted retrieval (ADR 0018);
the missing words belong on the document side, specific to each unit.

## Decision

1. Each active unit of a citable CDC, LGPD or Federal Constitution provision may carry
   two to four lay sentences in `app/consumer/data/aliases/aliases.json`, indexed as one
   alias chunk per unit (`…:alias-01`, `chunk_level: alias`, `content_kind: lay_alias`).
   An alias chunk carries nothing but its sentences: no breadcrumb and no constant prefix.
   The Civil Code has no aliases in this release.
2. An alias is never quoted. A citation found through one quotes the unit's first
   official chunk and records the alias as `matched_chunk_id`; the notice trace marks it.
   The agreement gate, the eligibility policy and the CDC anchor are unchanged and apply
   to an alias chunk like any chunk.
3. Aliases are generated offline by `python -m app.consumer.generate_aliases`, one LLM
   call per article, from the statute text alone. The generator never reads the golden
   set; a test checks that no golden complaint or remedy reaches a request. Reasoning
   models that reject temperature 0 are called through `--reasoning-effort`.
4. Entries are `generated`, `reviewed` or `rejected`; the first two are indexed. An alias
   naming an article, statute or code is refused. Each entry records the prompt version
   it came from: a generated entry from an older prompt is regenerated, and one that the
   new prompt answers with nothing usable is dropped rather than kept. Reviewed and
   rejected entries are never replaced without `--force`. An entry generated from statute
   text that has since changed fails corpus load.
5. Aliases are part of the corpus identity: corpus release `br-consumer-law-2026-10-03-v5`,
   chunking `legal-hierarchy-v5`, golden dataset 2.3.0. An empty alias set keeps the
   identity of earlier releases. Vector reuse keys vectors by chunk text, so the 1,644
   official chunks keep their vectors and only alias chunks are embedded.

## Generation (2026-10-03)

**Prompt v1** (`consumer-lay-aliases:v1`, gpt-5.6-terra at low reasoning effort) wrote
560 entries for the 562 aliasable units. Measured offline it raised recall but cited two
known-bad grounds on the development split, both CDC art. 18 (product defects): its
aliases described outcomes many articles share ("após 30 dias sem solução, posso pedir
meu dinheiro de volta"), so a service-repair complaint and an undelivered offer reached
the product-defect article.

**Prompt v2** (`consumer-lay-aliases:v2`), changed on that development evidence, requires
each sentence to carry the condition that distinguishes its unit (product or service, the
practice, the personal data, the situation), forbids shared outcomes on their own, and
allows an empty answer for units that describe no concrete consumer situation
(definitions, principles, public bodies, cross-references). It kept **364 entries**: CDC
223 of 284 units, LGPD 138 of 271, Constitution 3 of 7. The 198 units without aliases are
mostly v2's deliberate empty answers; LGPD art. 14 returned no answer twice and is the
one gap to revisit. Eight sampled entries:

- `br-cdc-art-54-d-paragrafo-unico`: Se me ofereceram crédito sem explicar custos, consequências do atraso ou avaliar minha situação, posso pedir ao juiz redução de juros e cobranças extras. / Dependendo da falha da empresa e da minha renda, o juiz pode aumentar o prazo original para eu pagar a dívida.
- `br-cdc-art-104-a-paragrafo-4-inciso-ii`: Meu plano de pagamento deve dizer o que acontecerá com os processos judiciais de cobrança que já estão em andamento. / O acordo pode prever a suspensão ou o encerramento dessas ações de cobrança.
- `br-cdc-art-6-inciso-xiii`: Quero saber o preço por quilo para comparar embalagens de tamanhos diferentes. / No mercado, preciso ver o preço por litro ou por metro, além do valor total da embalagem.
- `br-cdc-art-42-caput`: Estou devendo uma loja, mas os cobradores estão me humilhando na frente de outras pessoas. / Recebi ameaças por uma dívida de consumo atrasada. / A empresa expôs minha dívida para me constranger durante a cobrança.
- `br-lgpd-art-7-paragrafo-6`: Mesmo sem precisar da minha autorização em uma situação específica, a empresa ainda deve cuidar dos meus dados. / A dispensa de permissão para usar meus dados não libera o serviço de tratar essas informações de forma correta.
- `br-lgpd-art-7-inciso-i`: Eu autorizei uma loja a usar meu telefone para me enviar ofertas. / Eu dei permissão para um aplicativo guardar minha localização durante o uso.
- `br-cdc-art-54-g-inciso-iii`: Usaram meu cartão sem minha autorização, e a administradora está dificultando o bloqueio imediato daquela cobrança. / Identifiquei uma compra fraudulenta no cartão e quero pedir o cancelamento do pagamento, quando isso for possível. / A empresa recebeu dinheiro de uma compra feita com meu cartão por fraude e está impedindo a devolução do valor.
- `br-cdc-art-51-inciso-xi`: A empresa pode cancelar o contrato quando quiser, mas eu não tenho o mesmo direito. / O contrato dá só à fornecedora a opção de encerrar o serviço por decisão própria.

## Measurements (offline stack, agreement depth 13)

Averages are over the cases each metric applies to; intervals are 95% paired bootstrap
intervals of the difference against v4 (`python -m app.evaluation.compare`).

| Development (24 cases) | v4 | v5, prompt v1 | **v5, prompt v2** | v2 − v4, 95% interval |
|---|---|---|---|---|
| notice article recall | 0.025 | 0.125 | **0.267** | +0.242 [+0.058, +0.442] |
| notice exact recall | 0.000 | 0.050 | **0.125** | +0.125 [+0.000, +0.275] |
| notice precision | 0.042 | 0.135 | **0.300** | +0.313 [+0.000, +0.688] (8 paired cases) |
| grounds / labelled / unlabelled | 17 / 1 / 16 | 32 / 3 / 27 | **29 / 8 / 20** | labelled +7 [+2, +13] |
| known-bad citations | 0 | 2 | **1** | +1 [0, +3] |
| retrieval article recall@5 | 0.225 | — | **0.550** | +0.325 [+0.108, +0.542] |
| retrieval recall@5 | 0.142 | — | **0.242** | +0.100 [−0.117, +0.308] |
| retrieval hard-negative rate@5 | 0.025 | — | **0.025** | unchanged |

| Holdout (19 cases, reported only) | v4 | v5, prompt v1 | **v5, prompt v2** |
|---|---|---|---|
| notice article recall | 0.077 | 0.269 | **0.308** |
| notice exact recall | 0.000 | 0.231 | **0.192** |
| notice precision | 0.167 | 0.161 | **0.192** |
| notice abstention | 0.667 | 0.500 | **0.833** |
| known-bad citations | 0 | 1 | **1** |

On the motivating case, CDC art. 39 I for `venda_casada_seguro` now ranks first
lexically and seventh dense, inside the gate; before v5 the lexical channel never ranked
it. Labelled articles now cited include, on the development split, CDC arts. 39 I,
20 II, 35 III, 43, 12 § 1 II, 51 II, 54-A § 1 and 104-C § 1, and on the holdout, arts.
39 VI, 39 IX, 40 § 3, 52 § 2 and 51 XI.

**The remaining known-bad citation** is CDC art. 42 sole paragraph (refund in double) on
`cobranca_com_ameacas`, whose label is art. 42 caput (abusive debt collection). Art. 42
caput has an accurate alias ("os cobradores estão me humilhando na frente de outras
pessoas"), but a notice cites one unit per article and the selector quoted the sibling
paragraph. The holdout's one known-bad citation is the same unit, on the B2B no-ground
case. The development known-bad ceiling in CI is 1 by decision, until collapsing siblings
before truncation (review step 4) is measured.

**Limits of the offline stack.** Its dense channel is a hashed bag of words without IDF.
The lay alias chunks share everyday words ("quero", "paguei") with every lay query, so
they crowd that channel: a one-line undue-payment complaint that v4 grounded lost its
dense rank in v5 (the prompt-notice API test fixture was re-chosen). JUÁ is a semantic
model and is measured below.

## Configured stack

Pending the protocol of the spec (§5.7): the golden query cache, a v4 baseline, the v5
generation and a paired comparison. This section is completed when it runs.

## Consequences

- (+) Lay wording reaches units whose statutory text it never shares, and precision rose
  with recall on the development split.
- (+) Citations, the gate and the eligibility policy are unchanged; an alias never reaches
  the notice text.
- (−) Up to one more same-article candidate per unit competes for the eight slots per
  query, and the one remaining known-bad citation is a sibling paragraph (review step 4).
- (−) Aliases are model output marked `requires_legal_review`; 198 units have none.
