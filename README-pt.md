# Give Exit — AI Litigation Copilot

[Read in English](README.md)

Assistente de IA para litígios brasileiros, construído como demonstração de
nível de produção de engenharia de IA moderna: orquestração multiagente
(LangGraph), RAG auditável, saídas estruturadas, observabilidade, avaliação e
explicabilidade.

> **Não substitui advogados.** Toda conclusão carrega um grau de confiança,
> raciocínio explícito e citações literais do documento de origem, para que
> um humano possa auditar cada afirmação.

## Duas jornadas de produto

**Empresarial** — o time jurídico envia o PDF de uma ação e recebe em minutos
um relatório estruturado: resumo executivo, classificação, entidades extraídas
(partes, juízo, valor da causa, prazos), linha do tempo, avaliação pedido a
pedido, análise de risco com exposição financeira, estratégia de defesa,
postura de acordo e validação do número do processo no DataJud (API oficial de
registros processuais do CNJ). Exportação em Markdown, PDF, DOCX ou JSON.

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
brasileiro habilitado deve revisá-la antes.

Nas duas jornadas, cada página de conteúdo não confiável passa por um portão de
segurança contra prompt injection antes de chegar às camadas de RAG ou LLM, e
cada consulta de RAG deixa uma trilha de auditoria durável (IDs dos chunks
ranqueados, scores, hashes e quais chunks de fato entraram em cada prompt).

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
| [0008](docs/adr/0008-citation-based-groundedness.md) | Detecção de alucinação por verificação mecânica de citações |
| [0009](docs/adr/0009-in-process-async-jobs.md) | Jobs assíncronos in-process com interface pronta para broker |
| [0010](docs/adr/0010-prompt-injection-security-gate.md) | Varredura de conteúdo não confiável antes de indexar ou analisar |
| [0011](docs/adr/0011-retrieval-traceability-and-evaluation.md) | Proveniência consulta→contexto persistida; rankings avaliados |
| [0012](docs/adr/0012-bounded-consumer-extrajudicial-notice.md) | Fluxo do consumidor limitado a rascunhos auditáveis com revisão humana |
| [0013](docs/adr/0013-versioned-consumer-law-retrieval.md) | Legislação oficial versionada + recuperação híbrida avaliada |
| [0014](docs/adr/0014-human-review-resume-path.md) | Revisão humana como estado do grafo, nunca sobreposição de rota |

### Explicabilidade

Toda conclusão importante é uma `ConfidentConclusion`:

```json
{
  "statement": "Recomendado buscar acordo ate R$ 8.000,00",
  "confidence": 0.87,
  "reasoning": "O documento comprova a cobranca indevida e o CDC preve...",
  "citations": [{"quote": "cobrancas mensais indevidas", "page": 3}]
}
```

O harness de avaliação verifica se cada citação ocorre de fato no documento de
origem — uma citação fabricada é capturada mecanicamente, não pela opinião de
outro LLM.

### Observabilidade

Toda chamada de LLM retorna metadados tipados (provedor, modelo, latência,
tokens, custo, versão do prompt) — agentes fisicamente não conseguem fazer
chamadas não rastreadas. Agregados por execução persistem em um run store JSONL
exposto em `/runs` e `/runs/totals` e no painel de custos da UI. A auditoria
completa de recuperação de um job fica em `/analyses/{job_id}/retrievals`
(prévias de texto dos chunks são opt-in via
`LITIGATION_RETRIEVAL_TRACE_INCLUDE_PREVIEWS`); a aba de explicabilidade do
Streamlit mostra os mesmos dados em tabela filtrável.

### Defesa contra prompt injection

Regras determinísticas em português e inglês verificam cada página antes da
indexação; o texto do documento é sempre tratado como dado não confiável. O
roteamento é determinístico: `none`/`low` prossegue, `medium` prossegue com
aviso e mascara os trechos sinalizados, `high` pausa como `review_required`,
`critical` termina como `blocked`.
Uma pausa `review_required` é resolvida por uma pessoa identificada em
`POST /analyses/{job_id}/review`: a aprovação reexecuta o pipeline com os
trechos sinalizados ainda mascarados e registra quem revisou no relatório; a
recusa encerra a execução como `rejected`. Um veredito `blocked` e uma
varredura incompleta nunca podem ser liberados por essa via.
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

Os chunks indexados contêm o texto integral do documento e seguem o mesmo
ciclo de vida: os vetores de um documento são apagados quando o job termina,
salvo `LITIGATION_RETAIN_INDEX=true`, e a inicialização remove vetores
deixados por um processo anterior (registros de casos e jobs são em memória).
O histórico de execuções — hashes e métricas, nunca o texto dos chunks a menos
que os previews estejam habilitados — permanece.

Defina `LITIGATION_API_AUTH_KEY` para exigir `X-API-Key` em todas as rotas
exceto `/health`; uploads também têm limite de taxa por cliente
(`LITIGATION_UPLOAD_RATE_LIMIT_PER_MINUTE`, 20 por padrão). Ambos são
obrigatórios em qualquer implantação acessível fora do localhost, junto com
isolamento por tenant e política de retenção documentada antes de usar dados
reais de processos.

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
- AuthN/AuthZ e isolamento de dados por tenant na camada de API
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
