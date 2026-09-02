---
name: test-and-ship
description: >
  Workflow "testar e depois shippar". Use SEMPRE que o usuário quiser validar E subir de uma
  vez — "/test-and-ship", "testa tudo e sobe", "roda os testes e manda", "valida e abre o PR",
  "deixa verde e shippa", "testa, conserta e shippa". Duas fases via subagents. Fase 0 (primeira
  execução no repo): instala e configura os gates que faltarem (unitário, cobertura, integração
  HTTP, mutação, complexidade, arquitetura de imports, órfãos, segurança, migrations). Fase 1
  (paralelo): tester roda estilo + tipos + suítes que o diff alcança + gates estruturais +
  integração + mutação incremental, CONSERTANDO cada falha até verde; + doc-checker (edita doc
  stale) e knowledge-curator (read-only, recomenda). Fase 2 (só com tester 100% verde): shipper
  commita a allowlist numa feature branch, abre o PR via gh e merga quando o CI fechar. Depois o
  orquestrador atualiza o tracker (Linear/Jira/GitHub Issues; follow-ups → Backlog). Se os testes
  não ficarem verdes, NÃO shippa — para e reporta. NÃO dispare para: testar SEM shippar (só rodar
  os testes), shippar SEM testar, abrir só um PR, ou só mexer no tracker — é o combo
  testar+shippar.
compatibility: Python 3.10+ · Django ou FastAPI · pytest · ruff · mypy · mutmut · import-linter · vulture · pip-audit · git · gh (obrigatório na Fase 2) · tracker via MCP (opcional — fallback lista no chat) · subagents (Agent tool)
---

# /test-and-ship — Testa rigorosamente, conserta, e só então shippa (backend Python)

Junta verde-local + verificação de docs/conhecimento + ship (commit + push + PR + tracker) num fluxo único com **gate de qualidade**: o ship só acontece se os testes passarem. Tudo via subagents — sua sessão principal não muda de estado, só orquestra.

São **duas fases** (mais a Fase 0 de bootstrap, só na primeira execução). A Fase 1 roda **três subagents em paralelo**; a Fase 2 roda **um subagent** (ship), e o **tracker é feito por você (orquestrador)** depois.

| Fase | Subagent | `subagent_type` · modelo | O que faz |
|---|---|---|---|
| 1 | tester | `general-purpose` · médio (sonnet) | estilo + mypy + suítes que o diff alcança + gates estruturais + integração HTTP + mutação incremental + **conserta tudo**. É o GATE. |
| 1 | doc-checker | `general-purpose` · médio (sonnet) | decide se README/docs/CLAUDE.md/specs ficaram stale e **edita se preciso** |
| 1 | knowledge-curator | `Explore` · médio (read-only por construção) | arquivo-mapa + catálogo de skills — **recomenda** |
| 2 | shipper | `general-purpose` · rápido (haiku) | branch-guard + commit da allowlist + push + **abre e merga PR** via `gh` |
| 2 | (você) | orquestrador | **tracker**: issue → done/em revisão; follow-ups → Backlog; relatório final |

> **Por que `Explore` no read-only:** ele roda AO MESMO TEMPO que o tester (que edita código). `Explore` é **read-only por construção** (sem Edit/Write) — impossível brigar com o tester ou subir arquivo não-validado. Paralelismo seguro por design. (Sem um agente read-only no seu ambiente, use `general-purpose` com instrução read-only explícita — menos garantido.)

> **Por que o tracker é do orquestrador, não do shipper:** MCPs (Linear/Jira) **podem não estar disponíveis dentro de um subagent**. A sessão principal os tem de forma confiável e tem o contexto pra identificar a issue. O shipper faz **git + gh** (sempre funciona) e você fecha o tracker no Passo 5.

> **Gate inegociável:** a Fase 2 só roda se o **tester** retornar **100% verde**. Tester vermelho → o fluxo **para** (sem commit, sem tracker) e você reporta as falhas. doc-checker/knowledge-curator **não são gate** — só editam/recomendam.

> **UI opcional:** se o projeto serve templates/HTML (Django templates, Jinja), dá pra acrescentar um gate condicional de E2E com Playwright (`uv add --dev playwright pytest-playwright` + um smoke por página crítica) e, se fizer sentido, um frontend-checker na Fase 1. API pura: não se aplica — e o relatório diz isso em vez de instalar peso morto.

> **⚠️ SUBAGENT: rode tudo SÍNCRONO.** A notificação de conclusão de um comando em background chega a **quem lançou** o subagent, **nunca** ao subagent que o disparou. Subagent que encerra o turno "esperando a notificação" trava para sempre e volta ao orquestrador sem commit nem PR. Se você é subagent: nada de `run_in_background` — rode e espere no mesmo turno, ainda que leve minutos. (O poll do Passo 4.5 é diferente: ali é o ORQUESTRADOR, que é quem recebe notificações.)

---

## Como ler o verde do CI — "All checks have passed" NÃO quer dizer que o gate rodou

O GitHub conta job **`skipped` como sucesso** e estampa *"All checks have passed"* em verde. Um job agregador ("verify"/"ci") também fica **verde por vacuidade** quando tudo que ele agrega foi pulado. Um PR em que nenhum teste rodou exibe exatamente o mesmo verde de um PR com o gate inteiro executado.

**A leitura correta é o CONTADOR de skipped, não a frase.** Se os jobs de teste/cobertura estão na lista de skipped, nada rodou — não importa o que a frase verde diz. Comando que responde sem ambiguidade (use este, não a UI):

```bash
gh pr view <n> --json statusCheckRollup \
  --jq '{pulados:[.statusCheckRollup[]?|select(.conclusion=="SKIPPED")|.name],
         verdes:[.statusCheckRollup[]?|select(.conclusion=="SUCCESS")|.name]}'
```

**Corolário — nunca reporte "gate verde" a partir do painel do PR.** Diga *"o rápido passou; o pesado ficou skipped"* ou *"o pesado rodou e passou"*, e prove com a lista acima. Verde que significa "não verifiquei" é indistinguível de verde que significa "verifiquei" — só o contador os separa. Se o repo usa labels de CI opt-in (ex.: uma label que liga a suíte pesada), aplicá-la é decisão **deliberada** de quem shippa (diff de infra, PR grande, migration) — não rotina.

---

## Política de autonomia e perguntas (leia antes de tudo)

O fluxo é **autônomo por padrão**. Implemente e decida sozinho, a fundo. Antes de fechar qualquer decisão, **investigue o impacto no resto do sistema**: quem mais consome isto, quebra algum contrato/comportamento, faz algo parar de funcionar que não deveria. Trabalho extra e análise caso a caso são esperados — não são motivo para pedir ajuda.

**Só pare e pergunte quando a dúvida for de UMA destas quatro naturezas:**
- **Regra de negócio** — o comportamento correto depende de decisão de produto que não está no código nem na spec.
- **Banco de dados** — migração destrutiva, backfill de dado de produção, mudança de schema com risco de perda. **Num backend este é o caso que mais morde: pare e pergunte SEMPRE.** Migration gerada nesta sessão → **leia o SQL antes de commitar** (`python manage.py sqlmigrate <app> <numero>`, ou o SQL do Alembic) — operação destrutiva dentro dela é pergunta, não commit.
- **Gitflow** — merge/reset/force-push em branch protegida, sincronização entre branches de integração, reescrita de histórico.
- **Downtime** — mudança que pode derrubar produção ou exige janela/ordem de deploy.

**Antes de rotular uma dúvida com uma das 4 naturezas, VALIDE A PREMISSA TÉCNICA dela.** Um trade-off só é decisão de negócio se ele **existe**. Teste o mecanismo, leia a doc da lib, rode o experimento — e **nunca herde a alegação de um comentário, docstring ou ticket anterior sem revalidar**. Já se perdeu uma rodada inteira de espera humana com uma pergunta cujo trade-off não existia (as duas opções eram compatíveis). Se a validação mostrar que não há trade-off, **não pergunte — implemente**.

**Nunca pergunte sobre decisão pequena de implementação** — nome de variável/arquivo, onde colocar um helper, qual das libs já instaladas usar, formato de teste, refactor interno. Havendo duas opções razoáveis sem risco de negócio/DB/gitflow/downtime, escolha a mais alinhada ao código ao redor, registre a escolha em uma linha e siga.

**Testes: sempre completos.** Nunca pergunte se deve testar ou o quanto — rode o gate inteiro, sempre.

**Ao perguntar** (só nos 4 casos acima), seja cirúrgico. **Toda dúvida a humano abre com `🚨🚨🚨` na primeira linha** — no comentário do tracker E no report ao usuário. Estrutura: **(1)** contexto em 1-2 frases, **(2)** pergunta fechada, respondível com uma única escolha, **(3)** as opções que você enxerga, com o trade-off de cada uma e **qual você recomenda**. Se o humano precisa reler pra entender o que está sendo perguntado, reescreva.

---

## Como usar

```bash
/test-and-ship                       # testa, conserta, shippa na branch principal (MODO = full)
/test-and-ship stag                  # PR só pra branch de staging/dev, se o repo tiver (MODO = staging)
/test-and-ship (branch minha-base)   # PR só pra branch(es) explícita(s) (MODO = branch)
```

Ou linguagem natural: "testa tudo e sobe", "roda os testes, conserta e manda", "valida e abre o PR".

**Resolução do MODO (antes de tudo):**
- Invocação contém `(branch <nome>)` / `(branches <A> e <B>)` → **MODO = branch**, `BRANCHES_ALVO = [...]`. Destino explícito **vence** qualquer outro modificador. O ship abre PR **só** nesses alvos — nunca na branch principal nem na de staging.
- Invocação contém `stag`/`staging` **e** o repo tem branch de staging (`dev`/`develop`/`staging`) → **MODO = staging** — PR só pra ela.
- Caso contrário → **MODO = full** — **um** PR, alvo a branch principal (`main`/`master`).

Passe o MODO explicitamente ao shipper no Passo 4.

**⚠️ Backend costuma ter CD no merge.** Antes de qualquer merge em MODO `full`, descubra se o merge **deploya** (workflow com trigger em `pull_request: closed` / push na principal / tag automática). Se deploya: **avise o usuário no relatório antes do Passo 4.5 concluir o merge** e trate como decisão dele se ele não autorizou deploy no pedido. E só declare "em produção" quando o **run de deploy** fechar `success` — não quando o PR mergear (ver Passo 4.5).

---

## Fase 0 — Diagnóstico e instalação dos gates (primeira execução no repo)

Antes da primeira Fase 1 num repositório, inventarie o que existe e instale o que falta. **Detecte o ambiente primeiro:** `uv.lock` → uv, `poetry.lock` → poetry, `Pipfile.lock` → pipenv, senão pip + venv; `manage.py` → Django, `fastapi` no manifesto → FastAPI. Use o runner detectado em tudo (exemplos com `uv run`; troque por `poetry run`/`pipenv run`/venv ativado).

| # | Gate | Existe se | Instalar/configurar quando faltar |
|---|---|---|---|
| 1 | Estilo | `[tool.ruff]` no pyproject | `uv add --dev ruff`; gate = `ruff check .` **+** `ruff format --check .`; regras base `E,F,W,I,UP,B` em `[tool.ruff.lint] select` |
| 2 | Tipos | `[tool.mypy]` | `uv add --dev mypy` (+ `django-stubs` no Django, `types-*` conforme o caso); comece com `check_untyped_defs = true` e aperte módulo a módulo — `strict` global de saída num repo legado trava tudo |
| 3 | Unitário | `pytest` no manifesto | `uv add --dev pytest` (+ `pytest-django` com `DJANGO_SETTINGS_MODULE` em `[tool.pytest.ini_options]`; FastAPI async: `pytest-asyncio`) |
| 4 | Cobertura | `[tool.coverage]` | `uv add --dev pytest-cov`; `[tool.coverage.report] fail_under = 100` com `[tool.coverage.run] source` **escopado só ao que tem teste** (regra abaixo), crescendo módulo a módulo |
| 5 | Integração HTTP | testes de endpoint | Django: `Client`/`APIClient` (DRF); FastAPI: `TestClient` (httpx). Um teste por endpoint crítico: rota + status + shape da resposta, **banco de teste isolado** (o que o repo já usa: SQLite em memória, schema de teste, container) |
| 6 | Mutação | `mutmut` no manifesto | `uv add --dev mutmut`; `[tool.mutmut] paths_to_mutate` mirando **só os módulos de regra de negócio** — nunca migrations/settings |
| 7 | Complexidade ciclomática | `C90` no ruff, ou `xenon` | Caminho curto: `select += ["C90"]` com `[tool.ruff.lint.mccabe] max-complexity = 10`. Alternativa com relatório: `uv add --dev radon xenon` e gate `xenon --max-absolute B --max-average A <pacote>` |
| 8 | Arquitetura + ciclos | `[tool.importlinter]` | `uv add --dev import-linter`; contratos: `layers` (ex.: `domain` não importa `api` nem `infra`) e `independence` entre apps/domínios; gate = `lint-imports`. Ciclo de import em Python quebra em **runtime** com `ImportError` parcial — este gate pega antes |
| 9 | Código órfão | `vulture` no manifesto | `uv add --dev vulture`; gate = `vulture <pacote> --min-confidence 90`; falsos positivos (símbolos usados por reflexão/signals/admin do Django) → **whitelist versionada, com o motivo em comentário** |
| 10 | Segurança | — | `uv add --dev pip-audit`; gate = `pip-audit` (falha em vulnerabilidade conhecida nas dependências instaladas) |
| 11 | Migrations | framework com ORM | Django: `python manage.py makemigrations --check --dry-run` (model mudou sem migration = vermelho). Alembic (FastAPI): `alembic check` |
| 12 | Contratos do projeto | testes de convenção | pergunte ao usuário quais convenções o time tem (todo endpoint autenticado? erro no envelope padrão? OpenAPI validado contra as respostas reais? log estruturado por request?) e escreva o teste de contrato de cada uma |

Regras da Fase 0:

- **Instale só o que falta.** Gate existente, ainda que configurado diferente do molde, fica como está.
- **Commit da infra separado.** As mudanças da Fase 0 saem num commit próprio (`chore: instala gates de teste`), nunca misturadas ao diff da feature — e passam pela MESMA Fase 1/2 que qualquer mudança.
- **Cobertura 100% escopada, não 100% global de saída.** `[tool.coverage.run] source`/`include` começa listando só os módulos que já têm teste e cresce módulo a módulo. Um `source = ["."]` de saída num repo sem testes travaria o primeiro PR pra sempre.
- **⚠️ O veredito de cobertura é POR ARQUIVO, nunca o total arredondado.** A tabela do coverage arredonda a 1 decimal e imprime `100.0%` **ao lado do próprio erro** — o veredito é **zero arquivo com Miss > 0** no `--cov-report=term-missing` / `coverage report --show-missing`. E `collected 0 items` é **vermelho**, não verde.
- **Gates de UI só se o projeto serve templates/HTML** (Playwright + smoke por página). API pura: **não se aplicam** — diga isso no relatório em vez de instalar peso morto.
- **O que não se aplica é dito, não pulado em silêncio.** Liste o que pulou e por quê no relatório.

---

## O que VOCÊ (orquestrador) faz

Você **não roda testes nem commita** — delega. Seu papel:

### Passo 0 — Branch-guard + allowlist da sessão

**Branch-guard:** rode `git branch --show-current`.

- Se estiver numa **feature branch** (≠ principal e ≠ staging) → ok, segue.
- Se estiver na **principal/staging** (ou detached) → **crie uma feature branch automaticamente** antes de qualquer commit: `git fetch origin <principal>` e `git checkout -b <tipo>/<slug> origin/<principal>`. **Reporte** a branch criada ao usuário.

**Allowlist:** reflita sobre **o que ESTA sessão editou/criou** e monte a lista explícita de arquivos (migrations geradas incluídas). O working tree pode ter WIP de outras sessões/humano — **só entram os arquivos desta sessão**. Se a sessão não editou nada, **pare e diga** — não há o que testar/shippar.

**Preflight de staleness:** branch de vida longa acumula divergência da base ENQUANTO você trabalha. Feche agora, não no conflito do PR:

```bash
git fetch origin <principal> -q
git rev-list --count HEAD..origin/<principal>
```

- `> 0` → `git merge origin/<principal>` e **resolva o conflito AQUI**; depois **re-rode a Fase 1** sobre o estado reconciliado (o gate verde tem que ser do código que vai subir, não do stale). ⚠️ Em Django, divergência costuma incluir **migrations novas na base**: depois do merge, confira o grafo (`makemigrations --check` acusa múltiplos leaves) e re-enraíze/merge-migration antes de seguir.
- ⚠️ Se o merge **recusar** com `Your local changes would be overwritten` (feature implementada e ainda não commitada — o caso comum aqui): **commite o WIP primeiro** (a recusa vira merge normal; reversível com `git reset --soft HEAD~1`). Não use `git stash` como primeira opção: o stash é compartilhado entre worktrees e o pop no lugar errado consome entrada alheia.
- ⚠️ **Sub-caso:** os commits novos da base são **o mesmo fix que você implementou** (outra sessão/pessoa pegou a mesma tarefa e mergeou primeiro). Sintoma: `git log --oneline HEAD..origin/<principal>` cita o SEU ticket. Aí `git merge` é a ação errada: compare diff a diff, **descarte o redundante**, re-enraíze (`git checkout -B <branch> origin/<principal>`), reaplique só o exclusivamente seu e re-rode a Fase 1.
- ⚠️ **Re-cheque antes do merge final, não só no início:** depois de uma pausa longa (compactação de contexto, espera de CI, resolução demorada), rode o `rev-list --count` de novo — o `0` do Passo 0 envelhece.

### Passo 1 — Spawnar os 3 subagents da Fase 1 (em PARALELO)

Numa **única mensagem**, dispare os três `Agent` juntos. O `prompt` de cada um = **conteúdo integral do `agents/<arquivo>.md`** correspondente + a **allowlist** (Passo 0) + um **resumo de 1-3 linhas** do que a sessão fez.

> **⚠️ Guardrail de worktree:** se a sessão vive num worktree, todo prompt de subagent inclui o path absoluto do worktree como "DIRETÓRIO DE TRABALHO", proibindo edição fora dele. Ao receber os resultados, rode `git -C <checkout raiz> status --short` — modificação vazada lá é edição do subagent no lugar errado: recupere (patch → worktree) e restaure o raiz antes de prosseguir.

| Subagent | `subagent_type` | arquivo do prompt |
|---|---|---|
| tester | `general-purpose` | `agents/tester.md` |
| doc-checker | `general-purpose` | `agents/doc-checker.md` |
| knowledge-curator | `Explore` | `agents/knowledge-curator.md` |

Eles devolvem, em contrato parseável:
- **tester** → `STATUS` 🟢/🔴 · o que quebrou/consertou · `FILES_TOUCHED` · `PRE_EXISTING` · `Notas`.
- **doc-checker** → `DOC-CHECK` 📝/✅ · o que mudou · `DOCS_TOUCHED`.
- **knowledge-curator** → `KNOWLEDGE-CHECK` 🧠/✅/`[—]` · recomendações · `KNOWLEDGE_FOLLOWUPS`.

### Passo 2 — O GATE (sobre o tester)

- **tester 🔴** → **NÃO** spawne o shipper. Falha de implementação **é pra consertar, não pra perguntar**: garanta que a causa raiz foi investigada a fundo antes de encerrar. O fluxo só para sem Fase 2 quando resta um bloqueio real — e aí, se for uma das 4 naturezas da Política, apresente no formato cirúrgico; senão siga resolvendo. Edições do doc-checker ficam no working tree (não commitadas) e entram no próximo ship.
- **tester 🟢** → siga pro Passo 3.
- **Achados Alta** do knowledge-curator não são gate. Se forem auto-resolvíveis, **corrija e re-rode a Fase 1** antes de subir — não pergunte nem "ofereça". Só vira pergunta se cair nas 4 naturezas.

### Passo 3 — Merge da allowlist

Allowlist final = **allowlist do Passo 0** + **`FILES_TOUCHED`** do tester + **`DOCS_TOUCHED`** do doc-checker. (knowledge é read-only — não contribui arquivos.) Crítico: se um fix do tester não entrar no commit, o remoto sobe quebrado.

**Se o repo usa tracker com IDs citados no código/commit (`[PROJ-123]`):** confira os IDs que ESTA sessão escreveu antes do commit —

```bash
git diff -U0 -- <allowlist> | grep -oE '^\+.*\[[A-Z]{2,6}-[0-9]+\]' | grep -oE '[A-Z]{2,6}-[0-9]+' | sort -u
```

Para cada ID **novo**, confirme no tracker que ele existe **e é a issue que você quer citar**. Um ID plausível e inexistente passa por todos os gates (nada grepa IDs contra o tracker) — e um ID chutado pode colidir com uma issue real de outra coisa, que é pior que apontar pro vazio. **Cite a issue só depois de ela existir**; precisando da tag antes, crie a issue primeiro.

### Passo 4 — Spawnar o shipper

`Agent` com `subagent_type: general-purpose` (modelo rápido/barato), `prompt` = conteúdo integral de **`agents/shipper.md`** + a **allowlist final** + a nota: *"Os testes já passaram 100% verde na Fase 1 — NÃO re-rode testes; vá direto pro commit + push + PR. A branch atual é uma feature branch (o orquestrador garantiu). **MODO = [full|staging|branch]** (com `BRANCHES_ALVO=[...]` se branch). NÃO toque no tracker — quem faz isso é o orquestrador."*

Ele devolve **commit SHA**, **URLs dos PRs**, **branch**, e qualquer **ID de issue** que achou em branch/commits.

### Passo 4.5 — Validar o reporte do shipper + confirmar os merges (VOCÊ)

**Anti-alucinação (visto na prática: shipper reportou o HEAD antigo como "commit novo").** Ao receber o reporte, valide ANTES de seguir:

1. `git log --oneline -2` — o SHA reportado **existe** e é **novo** (≠ HEAD de antes do ship).
2. **Compare CONJUNTOS, não leia a lista a olho** — dois arquivos rastreados e modificados já ficaram fora de um commit "de sucesso":
   ```bash
   comm -13 <(git show --name-only --format= <SHA> | sort) <(printf '%s\n' <ARQUIVOS DA ALLOWLIST> | sort)
   ```
   Saída **vazia** = a allowlist inteira entrou. Qualquer linha = commit incompleto: **não avance** — commite o que faltou (`git commit --only <faltantes> --amend --no-edit`, ou 2º commit se o PR já existir) e recheque. (Path da allowlist que nunca teve mudança aparece como falso-faltante; confirme com `git diff HEAD -- <path>` vazio antes de descartá-lo.)
3. `gh pr list --state open --json headRefName,baseRefName,url` — head/base esperados pro MODO. PR com par errado (base que o MODO proíbe) → **feche-o** (`gh pr close`) antes que mergeie, e refaça o passo você mesmo.

**Confirmar os merges (o `--auto` é assíncrono — arma, não funde):** poll `gh pr view <pr> --json state` (30s de intervalo, ~10 min, em background) até `MERGED` em todos. Required check **falhou** num PR cujo diff **não pode** causá-lo (ex.: diff só de `.md` reprovando em teste)? Olhe o log do job: flake → `gh run rerun <run-id> --failed` **uma** vez; falhou de novo **no mesmo teste** → não é flake: pare e reporte. Só avance pro tracker com todos os PRs `MERGED` — "concluído" antes do merge real é mentira no tracker.

**⚠️ `MERGED` NÃO prova gate verde — confira os checks DEPOIS do merge e conserte pra frente.** Um PR pode fundir com o CI ainda rodando (base sem required check, ou required que não cobre tudo) e o check concluir FAILURE **depois** do merge. Então, após `MERGED`, espere os checks **concluírem**:

```bash
gh pr view <pr> --json state,statusCheckRollup \
  --jq '{state, pending:[.statusCheckRollup[]?|select(.conclusion==null and .name!=null)|.name],
         failed:[.statusCheckRollup[]?|select(.conclusion=="FAILURE")|.name]}'
```

`pending` não-vazio → siga pollando. `failed` não-vazio → **conserte e suba de novo até verde; NÃO reverta** por padrão (revert em branch compartilhada desfaz trabalho bom junto — só reverta se o usuário pedir). Antes de consertar, **descubra de quem é a falha** (`gh run view <run-id> --log-failed` + cruze com a sua allowlist): é do seu diff → corrija na feature, novo commit, novo PR pelo mesmo fluxo; **não é do seu diff** → herdado de PR anterior — prove (`gh pr list --base <branch> --state merged --limit 6`), cite a origem, e conserte você mesmo se ninguém estiver cuidando (é a branch compartilhada). Nunca reporte a falha como sua sem essa checagem, nem a ignore como "não é minha" sem provar.

**⚠️ Se o merge DEPLOYA (CD no merge — comum em backend): o run de deploy é o que prova, não o merge.** Só declare "em produção" quando o run do workflow de deploy fechar `success`:

```bash
gh run list --workflow "<nome do workflow de deploy>" --limit 5 --json headBranch,headSha,status,conclusion
```

⚠️ **Case o run pelo HEAD do PR, não pelo commit de merge**: em evento `pull_request: closed` o run se prende ao head do PR — buscar por `--commit <sha-do-merge>` volta **vazio** mesmo com o deploy tendo rodado, e o vazio parece confirmar "não deployou". E antes de afirmar como algo deploya, confirme que **aquele workflow** teve run recente — dois workflows de deploy podem coexistir no repo e só um roda (o outro é fóssil com trigger que ninguém dispara). Run de deploy vermelho → o endpoint novo NÃO está no ar; não declare produção e trate o conserto como prioridade.

**Verificação de CONTEÚDO — `MERGED` NÃO prova que o trabalho chegou.** Com os PRs fechados, confirme por alvo:

```bash
git fetch origin <alvo> -q
git diff --stat HEAD "origin/<alvo>" -- <arquivos da allowlist final>   # vazio = chegou
```

**Vazio = chegou.** Qualquer linha → leia o diff **antes** de tocar o tracker. Já aconteceu de um cherry-pick levar só o commit de docs, não conflitar, o PR mergear — e o alvo ficar com o gotcha documentado e **sem o fix** (pior que o bug original). Saída não-vazia tem causa benigna (o alvo tem mudanças adicionais nesses arquivos, de outra sessão) e grave (o seu trabalho não chegou) — distinga lendo. Grave → cherry-pick dos commits que faltam + PR de conserto.

**Diagnóstico de PR travado (2s, antes de qualquer hipótese):** PR aberto e **sem nenhum run de CI** (run ausente, não vermelho) quase sempre é **CONFLITO** — workflow `pull_request` roda sobre `refs/pull/N/merge`, que o GitHub não cria com o PR em conflito; nenhum run nasce, sem erro nem annotation. Cheque `gh pr view <n> --json mergeStateStatus` (⚠️ a primeira leitura após um push pode vir `UNKNOWN` — leia duas vezes): `DIRTY` → `git merge origin/<base>`, resolva, empurre — o run nasce no primeiro `synchronize`. E `close`+`reopen` (recuperação pra run perdido) **desarma o auto-merge em silêncio** — re-arme com `gh pr merge --auto` depois.

### Passo 5 — Tracker (VOCÊ, via MCP) + follow-ups

Atualize o tracker que o repo usa (Linear/Jira via MCP; GitHub Issues via `gh issue`). Sem tracker disponível → **liste tudo no chat** pro usuário — não falhe o ship por causa do tracker (o código já está no remoto).

1. **Issue da sessão:** ache a issue vinculada (ID no nome da branch/commits, ou contexto da conversa). MODO `full` (PR na principal mergeado **e, se houver CD, run de deploy verde**) → estado "concluído/em produção". MODO `staging` → "em revisão" (padrão), a menos que não haja promoção pendente à principal (solução terminal em staging) — aí "concluído". MODO `branch` → "em revisão", sempre: há promoção pendente por definição; registre no comentário PRA QUAL branch foi. Se a automação tracker↔GitHub mover o estado sozinha ao mergear (comum), **set o estado DEPOIS de confirmar os merges e re-confira** — automação que marca "em produção" num merge pra branch pessoal está errada: corrija.
2. **Follow-ups → Backlog:** para cada item de `KNOWLEDGE_FOLLOWUPS` + `PRE_EXISTING` do tester + sinais de cobertura, crie uma issue de Backlog usando `titulo` e `motivo`/`local`. Sem teto fixo — uma por item legítimo, por valor. NÃO re-julgue — os subagents já filtraram. Regras:
   - **Dedupe antes de criar:** busque no tracker por palavras-chave do título; issue equivalente aberta → referencie em vez de criar. E follow-up cujo sintoma você observou direto num arquivo/config: **reconfirme o arquivo imediatamente antes de criar** — outra sessão pode ter consertado no meio-tempo.
   - **Nunca crie issue sem dono.** O assignee é a pessoa da sessão; não sabe quem é → pergunte antes de criar.
   - **"Precisa de humano"** (label/flag equivalente) = só os 4 casos da Política, e SEMPRE com a pergunta explícita `🚨🚨🚨` no comentário — contexto + pergunta fechada + opções com trade-off + recomendação.

### Passo 6 — Sincronizar o checkout local

Os PRs mergearam → a principal remota avançou. `git fetch origin <principal> -q && git checkout <principal> && git pull --ff-only origin <principal>`. Guardas: checkout com WIP alheio ou branch de outra sessão → só `fetch`, avise; `--ff-only` falhou (divergiu) → reporte em vez de forçar; MODO staging/branch → a principal não mudou, fetch-only.

### Passo 7 — Loop de follow-ups (resolver até esgotar)

O ship não é o fim quando a sessão deixou follow-ups **auto-resolvíveis**. Entre num loop até esgotá-los:

1. Junte os follow-ups que ESTA rodada criou (qualquer fonte).
2. **Classifique — o padrão é IMPLEMENTAR:** refactor, cobertura, dead-code, doc — inclusive itens grandes (se for grande, em rodadas sucessivas). **PULE (Backlog + "Precisa de humano" + pergunta) só:** (a) os 4 casos da Política; (b) criação/reestruturação de skill (trabalho interativo); (c) gap de cobertura que já bate 100% isolado (flaky — vira investigação de causa raiz).
   ⚠️ **"Código já shipado" é PREMISSA A CONFERIR quando o follow-up cita outro PR/issue:** confirme o merge (`gh pr view <N> --json state` = `MERGED`) antes de implementar algo que documenta/depende daquele estado — senão você escreve documentação de um estado que não existe. E premissa falsa não é falsa pra sempre: re-cheque os adiados antes de fechar o loop.
3. Há ≥1 auto-resolvível → **implemente só esses** (escopo restrito), depois **re-rode esta MESMA skill do zero** (Fase 1 completa + Fase 2). As issues resolvidas migram de Backlog → concluído.
4. Repita sobre os follow-ups NOVOS daquela rodada.

**Parada: fixpoint** — uma rodada não produz follow-up auto-resolvível novo. Sem teto de rodadas. **Invariantes:** só follow-ups DESTA cadeia (nunca varra o Backlog inteiro); cada rodada re-passa pelo tester 🟢 (não afrouxe o gate pra "fechar o loop"); não-automatizáveis ficam rastreados com a pergunta que destrava.

---

## Por que este desenho

- **Modelos por tarefa:** teste/conserto + julgamento de doc = raciocínio denso → modelo médio; commit/push mecânico → modelo rápido. Sua sessão principal fica livre.
- **Paralelismo seguro na Fase 1:** tester edita código; doc-checker edita só `.md`; knowledge é read-only. Conjuntos de escrita disjuntos → sem corrida.
- **Sem duplicar gate:** a parte mecânica (ruff/mypy/import-linter) é gate do tester. O knowledge-curator cobre o julgamento que o gate não pega.
- **Tracker no orquestrador:** robustez de MCP.

## Limitations & Notes

- **Integração HTTP exige ambiente de teste são** (banco de teste, settings de teste, secrets dummy). O tester distingue bug do código × ambiente quebrado — e conserta os dois, mas reporta a diferença.
- **Working tree sujo com WIP alheio:** tester e shipper respeitam a allowlist — só tocam o que é da sessão.
- **Allowlist incompleta:** arquivo esquecido não é testado-no-escopo nem commitado. Reflita com cuidado no Passo 0 — migration gerada é o esquecimento clássico.
- **Mutação completa é cara:** o gate roda nos módulos do diff; o run completo é agendado/manual.
- **`gh` é obrigatório na Fase 2** — sem fallback pra git puro em PR/merge.
- **Merge que deploya**: ver o aviso do MODO e o Passo 4.5 — o run de deploy é o veredito, não o merge.

---

## Relatório final (OBRIGATÓRIO)

Emita SEMPRE ao fim — nunca encerre a skill sem ele. **1ª linha = TL;DR** com o emoji do veredito:

- **✅ bom** — testado e shipado, gate verde, tudo subiu (deploy verde, se houver CD), tracker atualizado, exit liberado.
- **⚠️ atenção** — shipou, mas há o que saber: follow-up na fila (loop continua), item no Backlog, deploy ainda rodando, ou parte em staging aguardando promoção.
- **🚨 ruim** — travou: gate da Fase 1 vermelho (sem Fase 2), PR que não mergeou, deploy vermelho, pergunta a humano aguardando, ou algo shipável que não subiu.

Logo após o TL;DR, **SEMPRE** a tabela-resumo (mesmo com 1 issue/1 PR):

| Tarefa | PR | Alvo | Estado |
|---|---|---|---|
| PROJ-123 | #42 | `main` | ✅ concluído (deploy `success`) |
| PROJ-124 (follow-up) | — | — | 📋 Backlog |

Corpo do relatório:

```
── Fase 0 (se rodou) ──
Gates instalados: <lista, ou "nada — tudo já existia"> · Pulados por não se aplicar: <lista + motivo>

── Fase 1 (3 subagents em paralelo) ──
Testes (tester):
- [x] ruff check + format nos arquivos da allowlist
- [x] mypy · suítes que o diff alcança (<N> testes) · cobertura (zero arquivo com Miss > 0)
- [x] lint-imports · vulture · pip-audit · migrations (makemigrations --check)
- [x] integração HTTP (<N> endpoints) — ou [—] (skip: <motivo>)
- [x] mutação incremental (<mortos>/<total>) — ou [—] (skip: <motivo>)
Doc-check: 📝 <o que> — ou ✅ sem mudança
Knowledge: 🧠 <N> — ou ✅ sem ação
Consertado no caminho: <o que estava vermelho e virou verde>
Arquivos commitados: <allowlist final>

── Fase 2 — ship ──
Commit `<sha>` na feature `<branch>` (validado: allowlist inteira no commit).
MODO: <full|staging|branch>
PR: <url> (MERGED confirmado · checks pós-merge verdes · conteúdo conferido no alvo)
Deploy: <run `success` confirmado pelo head do PR — ou "repo sem CD no merge">

Tracker: <issue → estado> · Backlog: <follow-ups criados>
⚠️ Exit? — ✅ pode dar exit: loop no fixpoint, PRs MERGED com checks verdes, deploy verde (se houver), tracker ok, sem pergunta pendente.
        — ou ⚠️ NÃO dê exit: <motivo>.
```

Se a Fase 1 não ficou verde, o relatório para na Fase 1 com `[!]` no passo que falhou, o erro e a pergunta ao usuário — **sem Fase 2**.
