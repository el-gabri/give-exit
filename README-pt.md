# Give Exit — AI Litigation Copilot

[Read in English](README.md)

Assistente de IA para litígios brasileiros, construído como demonstração
**orientada à produção e limitada a um único nó**: workflow LangGraph fixo em
múltiplas etapas, RAG auditável, saídas estruturadas, observabilidade, avaliação
e explicabilidade. Não é apresentado como serviço jurídico de produção:
workers duráveis, identidade/isolamento por tenant e validação jurídica
independente ainda são lacunas explícitas.

> **Informação jurídica e apoio à decisão, não aconselhamento jurídico.** As
> conclusões automatizadas trazem confiança autodeclarada pelo modelo (não
> calibrada) e raciocínio. Quando o modelo seleciona um ID de evidência válido,
> o backend reconstrói trecho e página da fonte. Integridade da fonte não prova
> nexo semântico nem correção jurídica.

## Duas jornadas de produto

**Empresarial** — o time jurídico envia o PDF de uma ação e recebe em minutos
um relatório estruturado: resumo executivo, classificação, entidades extraídas
(partes, juízo, valor da causa, prazos), linha do tempo, avaliação pedido a
pedido, análise de risco com exposição financeira, estratégia de defesa,
postura de acordo e consulta/enriquecimento do número do processo no DataJud
(API pública oficial do CNJ). Exportação em Markdown, PDF, DOCX ou JSON.

A análise jurídica, de risco e de estratégia da jornada Empresarial se baseia
na petição enviada e nos chunks derivados dela. Hoje ela **não** consulta um
corpus versionado de legislação ou jurisprudência; proposições jurídicas
citadas na peça continuam sendo alegações da parte, não direito validado de
forma independente. O DataJud enriquece o registro público, mas não valida o
mérito. Considere qualquer workflow fundamentado apenas na peça salvo quando o
trace identificar explicitamente outro corpus oficial — atualmente isso ocorre
somente na jornada Consumidor.

**Consumidor** — um chat guiado estrutura a reclamação contra um fornecedor e
aceita evidências em PDF/PNG/JPEG (rejeitadas quando o OCR não produz texto
revisável). O assistente recupera de um snapshot offline versionado do
[Código de Defesa do Consumidor (CDC)](https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm)
compilado, além de dispositivos selecionados da
[Constituição Federal](https://www.planalto.gov.br/ccivil_03/constituicao/constituicaocompilado.htm),
e redige uma **notificação extrajudicial com proposta de acordo** — não uma
ação judicial nem peça processual. A seção financeira é um cálculo de cenário
transparente: valores encontrados nas evidências viram candidatos e só entram
no total após confirmação do consumidor; valores mencionados no chat nunca são
promovidos automaticamente. Cada componente expõe fonte, trecho e hashes para
auditoria. O sistema nunca envia nem protocola a notificação — um advogado
brasileiro habilitado deve revisá-la antes. No demo, isso é política e aviso
visível, não um workflow autenticado de aprovação por advogado.
Dispositivos candidatos também passam por uma política determinística e
versionada de elegibilidade por categoria, exposta como
`requires_legal_review`; ranking de recuperação sozinho nunca autoriza um
fundamento jurídico.

Nas duas jornadas, cada página de conteúdo não confiável passa por um portão de
segurança contra prompt injection antes da indexação ou análise jurídica
subsequente (os modos `balanced`/`strict` podem chamar o LLM configurado).
Execuções Empresariais concluídas persistem traces de RAG em JSONL local; os
traces do Consumidor ficam no caso em memória. Ambos registram IDs ranqueados,
scores, hashes e quais chunks de fato entraram no prompt.

## Arquitetura

```
Browser -> Streamlit -> FastAPI (202 + job assíncrono)
    -> Máquina de estados LangGraph:
       security_scan --+--> index -> classify -> extract --+--> analyze --+--> risk     --+
                       |                                  |              +--> strategy --+--> compose
                       |                                  +--> enriquecimento DataJud ---+
                       +--> pausa para revisão humana / bloqueio ------------------------+
    -> RAG: Empresarial denso | Consumidor jurídico BM25+denso/RRF -> reranker opcional
       -> coleções ChromaDB isoladas e versionadas (hash do corpus + revisão do embedding)
    -> Porta LLM: OpenAI | Anthropic Claude | Google Gemini | Mock (demo offline)
    -> Compositor determinístico de relatório -> MD / PDF / DOCX / JSON
```

Diagrama completo, mapa de camadas e a política de roteamento de prompt
injection: [docs/architecture.md](docs/architecture.md).
O limite de segurança suportado para armazenamento e a exceção temporária das
advisories do Chroma estão documentados em [SECURITY.md](SECURITY.md).

### Decisões de projeto (ADRs)

| ADR | Decisão |
|---|---|
| [0001](docs/adr/0001-use-langgraph.md) | LangGraph em vez de chains do LangChain |
| [0002](docs/adr/0002-llm-provider-abstraction.md) | Porta LLM própria de 2 métodos em vez de LiteLLM |
| [0003](docs/adr/0003-chromadb-vector-store.md) | ChromaDB atrás de uma porta VectorStore |
| [0004](docs/adr/0004-async-first.md) | I/O async-first desde o primeiro dia |
| [0005](docs/adr/0005-pymupdf-ocr-fallback.md) | PyMuPDF + fallback heurístico de OCR |
| [0006](docs/adr/0006-section-aware-chunking.md) | Chunking ciente de seções de petições brasileiras |
| [0007](docs/adr/0007-deterministic-report-composer.md) | Sem LLM na última milha — o relatório é montado por código |
| [0008](docs/adr/0008-citation-based-groundedness.md) | Prevenção de trecho fabricado por reconstrução determinística da fonte |
| [0009](docs/adr/0009-in-process-async-jobs.md) | Jobs assíncronos in-process com interface pronta para broker |
| [0010](docs/adr/0010-prompt-injection-security-gate.md) | Varredura de conteúdo não confiável antes de indexar ou analisar |
| [0011](docs/adr/0011-retrieval-traceability-and-evaluation.md) | Proveniência consulta→contexto persistida; rankings avaliados |
| [0012](docs/adr/0012-bounded-consumer-extrajudicial-notice.md) | Fluxo do consumidor limitado a rascunhos auditáveis com revisão humana |
| [0013](docs/adr/0013-versioned-consumer-law-retrieval.md) | Legislação oficial versionada + recuperação híbrida avaliada |
| [0014](docs/adr/0014-human-review-resume-path.md) | Revisão humana como estado do grafo, nunca sobreposição de rota |

### Explicabilidade

Conclusões importantes do modelo usam `ConfidentConclusion`. O modelo seleciona
apenas o ID de evidência; trecho e página são reconstruídos pelo backend:

```json
{
  "statement": "Opcao preliminar: avaliar acordo dentro dos valores documentados",
  "confidence": 0.87,
  "reasoning": "A peticao alega cobrancas mensais e informa o valor discutido...",
  "citations": [{
    "chunk_id": "abc123:0007",
    "quote": "cobrancas mensais indevidas",
    "page": 3
  }]
}
```

IDs desconhecidos, estrangeiros, duplicados ou incompatíveis com a fonte são
rejeitados. O relatório expõe um portão determinístico de integridade de fontes:
ele prova proveniência/localização, não que o trecho sustente logicamente a
conclusão. Percentuais de confiança são autodeclarados pelo LLM e não são
probabilidades de desfecho.

### Observabilidade

Todas as chamadas atualmente implementadas de agentes e revisão de segurança
usam a porta tipada `LLMClient`, com provedor, modelo, latência, tokens, custo e
versão do prompt. Agregados de execuções Empresariais concluídas persistem em
um run store JSONL exposto em `/runs` e `/runs/totals`. A auditoria
completa de recuperação de um job fica em `/analyses/{job_id}/retrievals`
(prévias de texto dos chunks são opt-in via
`LITIGATION_RETRIEVAL_TRACE_INCLUDE_PREVIEWS`); a aba de explicabilidade do
Streamlit mostra os mesmos dados em tabela filtrável.

Antes da gravação durável, consultas em linguagem natural viram
`[QUERY_REDACTED:<prefixo-do-hash>]`; nomes de arquivo e labels de revisor viram
referências HMAC. Comentários de revisão, seções/prévias de resultados e
metadados arbitrários de fonte são omitidos; erros duráveis preservam apenas o
tipo da exceção. Isso é minimização, não anonimização: endpoints de jobs em
memória ainda expõem traces brutos, casos e traces do Consumidor ficam em
memória, e hashes ou metadados pseudonimizados ainda podem ser dados pessoais.
Sem `LITIGATION_TELEMETRY_PSEUDONYM_KEY`, pseudônimos
mudam intencionalmente após reiniciar o processo.

### Defesa contra prompt injection

Regras determinísticas em português e inglês verificam cada página antes da
indexação; o texto do documento é sempre tratado como dado não confiável. O
roteamento é determinístico: `none`/`low` prossegue, `medium` prossegue com
aviso e mascara os trechos sinalizados, `high` pausa como `review_required`,
`critical` termina como `blocked`.
Uma pausa `review_required` é resolvida por uma pessoa identificada em
`POST /analyses/{job_id}/review`: a aprovação reexecuta o pipeline com os
trechos sinalizados ainda mascarados e registra quem revisou no relatório; a
recusa encerra a execução como `rejected`. A decisão imutável é vinculada ao
job, documento e hash da avaliação de segurança, e persistida antes do resume.
A identidade do revisor ainda é autodeclarada atrás da chave compartilhada da
implantação; não existe RBAC de produção. Um veredito `blocked` e uma varredura
incompleta nunca podem ser liberados por essa via.
`LITIGATION_PROMPT_INJECTION_SCAN_MODE` seleciona `rules` (apenas
determinístico), `balanced` (padrão — revisão semântica dos trechos suspeitos)
ou `strict` (revisão semântica de todo o texto com orçamento limitado; exceder
o orçamento falha de forma segura).

### Avaliação

```bash
python -m app.evaluation                    # golden dataset empresarial (offline no CI)
python -m app.evaluation.consumer_runner    # recuperação jurídica do consumidor, baseline mock+BM25
```

As métricas incluem groundedness, taxa de alucinação, cobertura de citações,
acurácia de extração/classificação, Precision/Recall/HitRate/MRR/NDCG@K e
qualidade por LLM-juiz (apenas com provedor real). A suíte do consumidor fixa a
release exata do corpus e seu SHA-256, adiciona métricas por artigo/subdivisão,
hard negatives, checagens de dispositivos revogados e abstenção em reclamações
fora do escopo — é uma semente para bake-offs de engenharia, ainda não um
benchmark jurídico de produção. O snapshot fixado do CDC só é atualizado por
operação explícita do mantenedor:

```bash
python -m app.consumer.update_cdc_snapshot --retrieved-on AAAA-MM-DD
pytest tests/test_consumer_legal_corpus.py tests/test_consumer_evaluation.py
```

Revise o diff do texto legal, o hash do manifest e os golden labels antes de
promover uma nova release. Requisições em runtime nunca baixam lei do Planalto.

## Início rápido

### Docker (recomendado)

```bash
# Opcional: copy .env.example .env para configurar um provedor real.
# Sem .env, o backend inicia no demo offline sem chave.
docker compose up --build
# UI:  http://localhost:8501
# API: http://localhost:8000/docs
```

O Compose publica as duas portas em `127.0.0.1` por padrão. Para sobrescrever
`LITIGATION_BIND_HOST` e expor outra interface, defina também
`LITIGATION_DEPLOYMENT_MODE=production` e `LITIGATION_API_AUTH_KEY`; o modo de
produção recusa inicializar sem essa chave.

### Desenvolvimento local

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -e ".[dev,frontend,ocr]"
copy .env.example .env
pytest                                            # totalmente offline

uvicorn app.api.main:app --reload                 # terminal 1
streamlit run frontend/streamlit_app.py           # terminal 2
```

OCR local também exige o binário do Tesseract com dados de idioma em português
(a imagem Docker da API já instala ambos).

### Escolha um provedor de IA

Configure o `.env`. `LITIGATION_LLM_MODEL` é opcional — quando ausente, o app
escolhe um padrão válido para o provedor. Selecionar um provedor real sem a
chave correspondente falha imediatamente; o app nunca substitui a saída por
mock silenciosamente.

```dotenv
# Demo offline (padrão; placeholders determinísticos, não é análise real)
LITIGATION_LLM_PROVIDER=mock

# OpenAI ("auto" reusa a chave para embeddings)
LITIGATION_LLM_PROVIDER=openai
LITIGATION_OPENAI_API_KEY=sk-...

# Anthropic Claude (sem API de embeddings: "auto" usa BAAI/bge-m3 local —
# instale ".[local-embeddings]" e, no Docker, defina
# LITIGATION_API_EXTRAS=ocr,local-embeddings)
LITIGATION_LLM_PROVIDER=anthropic
LITIGATION_ANTHROPIC_API_KEY=sk-ant-...

# Google Gemini ("auto" reusa a chave para embeddings Gemini)
LITIGATION_LLM_PROVIDER=gemini
LITIGATION_GEMINI_API_KEY=...
```

Trocar provedor, modelo ou dimensões de embedding cria uma nova coleção no
Chroma; documentos existentes precisam ser reindexados.

### Tratamento de documentos

Uploads são transmitidos com limite de 20 MB. A jornada empresarial aceita
apenas PDF (até `LITIGATION_MAX_DOCUMENT_PAGES`, 250 por padrão); evidências do
consumidor aceitam PDF/PNG/JPEG com teto de 40 megapixels. Evidência bruta do
consumidor é apagada logo após a ingestão; PDFs empresariais são apagados após
a análise, salvo `LITIGATION_RETAIN_UPLOADS=true`.

Os chunks indexados contêm o texto integral do documento. Vetores Empresariais
são apagados quando o job termina, salvo `LITIGATION_RETAIN_INDEX=true`; com a
retenção desativada, a inicialização também remove vetores Empresariais deixados
por um processo anterior. Vetores de evidências do Consumidor permanecem apenas
enquanto o caso em memória existe: excluir o caso os remove, e a inicialização
elimina evidências órfãs de um reinício preservando o corpus jurídico canônico.
Estado de job/relatório Empresarial e todo o estado de caso Consumidor ficam em
memória. Metadados e traces minimizados de execuções Empresariais concluídas são
a exceção durável; eles não recriam um job ou caso após reinício. Prévias de
chunks ficam apenas no estado vivo e nunca são gravadas no JSONL.

A execução Empresarial é limitada no processo por
`LITIGATION_MAX_CONCURRENT_JOBS` (4 por padrão) mais
`LITIGATION_MAX_QUEUED_JOBS` (16). Acima da capacidade combinada, a API retorna
`503` com `Retry-After`. Trata-se de semáforo e backlog limitados em um único
processo, não de broker durável: jobs na fila/em execução/aguardando revisão são
perdidos no reinício, réplicas não compartilham capacidade nem estado, e o JSONL
não retoma trabalho. Escala horizontal exige broker, estado compartilhado e
workers apropriados.

Jobs interrompidos pelo portão de segurança mantêm o documento parseado apenas
numa janela limitada: `LITIGATION_MAX_REVIEW_REQUIRED_JOBS` (20) e
`LITIGATION_REVIEW_REQUIRED_TTL_SECONDS` (24 horas) limitam quantidade e idade.
Jobs antigos ou expirados são removidos depois que o registro de auditoria se
torna durável. Se essa gravação falhar, a pausa falha fechada e descarta o estado
completo necessário ao resume.

No modo local, definir `LITIGATION_API_AUTH_KEY` exige `X-API-Key` em todas as
rotas exceto `/health`; no modo de produção, a chave é obrigatória no startup.
Uploads também têm limite por cliente
(`LITIGATION_UPLOAD_RATE_LIMIT_PER_MINUTE`, 20 por padrão). Essa é uma chave
compartilhada da implantação, não identidade de usuário, RBAC ou isolamento por
tenant. Esses controles e uma política de retenção documentada continuam sendo
pré-requisitos para dados reais.

### Governança dos dados de demo

[`demo/manifest.json`](demo/manifest.json) inventaria cada PDF versionado com
SHA-256, classificação sintético/dados pessoais, proveniência e revisão. Um
registro judicial não sintético retido contém dados pessoais; URL de origem e
base de redistribuição não estão estabelecidas. Acesso público reportado e
presença no repositório **não** concedem por si só direito de redistribuição,
afastam deveres da LGPD ou provam conformidade com termos judiciais. Não o
publique nem redistribua sem revisão independente de direitos e privacidade.
Fixtures sintéticas também têm identificadores com formato de PII e devem ser
tratadas como sensíveis em telemetria. Veja [`demo/README.md`](demo/README.md).

## Estrutura do projeto

```
app/
├── core/           configuração (pydantic-settings), logging estruturado
├── consumer/       intake guiado · corpus jurídico · evidências · compositor da notificação
├── llm/            porta LLMClient · adaptadores OpenAI/Claude/Gemini/Mock · pricing
├── schemas/        contratos tipados de todas as camadas (o modelo de domínio)
├── ingestion/      PDF/imagem -> texto, fallback de OCR, detecção de idioma
├── security/       varredura de prompt injection, política e mascaramento seguro
├── rag/            chunking · embeddings · recuperação híbrida · reranking
├── agents/         classificador · extração · análise jurídica · risco · estratégia
├── prompts/        templates de prompt versionados em PT-BR
├── orchestration/  máquina de estados LangGraph
├── enrichment/     cliente DataJud (CNJ) + nó do grafo
├── services/       caso de uso de análise · compositor determinístico
├── evaluation/     métricas · golden runner · LLM-juiz · CLI
├── observability/  run store JSONL com traces de agentes/recuperação
├── reporting/      Markdown (canônico) -> conversores PDF / DOCX
└── api/            app FastAPI · gerenciador de jobs assíncronos · rotas
frontend/           UI Streamlit (cliente puro da API)
eval_data/          golden datasets
docs/               arquitetura + 14 ADRs + roteiro de demo
tests/              testes offline de unidade, integração e segurança
```

## Melhorias futuras

- Corpus versionado da CLT (legislação trabalhista) fundamentando a jornada
  trabalhista, no mesmo padrão do snapshot do CDC
- Jurisprudência brasileira (RAG de decisões) como segundo corpus
- Fila de jobs com Redis + workers horizontais (o ADR 0009 documenta o caminho)
- AuthN/AuthZ por usuário e isolamento de dados por tenant (a chave atual é da
  implantação, não do usuário)
- Adaptador de vector store gerenciado (ex.: pgvector/Pinecone) para escala multi-tenant
- Loop de feedback humano: correções de advogados alimentando o golden dataset
- Modelos calibrados de desfecho a partir de dados revisados de acordos e
  sentenças; texto legal sozinho não fornece probabilidade de vitória

## Aviso legal

Relatórios e notificações são apoio à decisão gerado por IA com proveniência
explícita. Não constituem aconselhamento jurídico e devem ser revisados por
advogado habilitado. O fluxo do consumidor não cria relação advogado-cliente,
não protocola reclamação, não interrompe prazo prescricional e não substitui
ajuda urgente de um advogado ou das autoridades competentes.
