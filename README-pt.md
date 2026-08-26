# Give Exit

Assistente brasileiro de informação jurídica voltado exclusivamente a
consumidores. O Give Exit transforma o relato confirmado e os documentos do
consumidor em um rascunho auditável de notificação extrajudicial, fundamentado
em um corpus versionado de direito do consumidor.

Este repositório é uma demonstração orientada à produção, não um escritório de
advocacia nem um serviço jurídico pronto para produção. Ele não determina se o
consumidor está juridicamente certo, não prevê resultados judiciais, não envia
notificações automaticamente e não substitui a revisão de advogado habilitado.

## Escopo do produto

1. O consumidor descreve o problema e a solução esperada.
2. A triagem determinística extrai somente fatos explícitos. Tudo permanece
   como alegação até revisão e confirmação pelo próprio consumidor.
3. O consumidor envia evidências em PDF, PNG, JPG ou JPEG.
4. A API valida formato e tamanho, extrai texto ou aplica OCR e verifica prompt
   injection antes de indexar qualquer conteúdo.
5. O RAG híbrido pesquisa as evidências e o corpus jurídico revisado.
6. Uma política determinística seleciona fundamentos elegíveis e reconstrói as
   citações a partir de metadados e hashes das fontes.
7. A notificação é exportada em Markdown, PDF ou DOCX com auditoria completa da
   recuperação.

Não existem mais jornada empresarial, análise de petição para defesa,
estratégia processual, DataJud, grafo multiagente ou relatório geral de litígio.

## Arquitetura

```text
Streamlit para o consumidor
        |
        v
FastAPI /consumer/cases
        |
        +--> triagem determinística + confirmação explícita
        +--> upload limitado --> extração/OCR --> segurança de documento
        +--> ChromaDB + dense + BM25 + RRF + reranker opcional
        |      +--> evidências aceitas
        |      +--> CDC versionado + dispositivos selecionados da CF
        +--> política jurídica e cenário financeiro determinísticos
        +--> notificação auditável --> Markdown / PDF / DOCX
```

O LLM conversacional **não redige a notificação**. Quando configurado, ele é
usado somente na revisão semântica limitada de trechos suspeitos encontrados
nos documentos. O provedor de embeddings é configurado separadamente.

## Fontes e citações

- O CDC vem de snapshot fixado do Planalto, acompanhado de manifesto e hashes.
- Dispositivos constitucionais selecionados são versionados no corpus.
- Os chunks preservam lei, artigo, subdivisão, URL oficial, release, vigência e
  hashes de origem.
- A recuperação jurídica combina semântica e correspondência lexical exata.
- Recuperar um chunk não o transforma automaticamente em fundamento: uma
  política determinística controla sua elegibilidade e está marcada como
  `requires_legal_review`.
- As citações são reconstruídas no backend; não são strings de citação geradas
  e aceitas do modelo.

Auditoria:

```text
GET /consumer/cases/{case_id}/notice/retrievals
X-Consumer-Case-Token: <token opaco do caso>
```

## Execução

### Docker

```bash
docker compose up --build
```

- Interface: <http://localhost:8501>
- OpenAPI: <http://localhost:8000/docs>

O container instala Tesseract em português. O arquivo bruto enviado é apagado
depois da ingestão.

### Python local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,frontend,ocr]"
python -m app.consumer.preindex_legal
```

Depois da pré-indexação, execute em dois terminais:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --reload

# Terminal 2
$env:LITIGATION_API_URL="http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m streamlit run frontend/streamlit_app.py
```

OCR local de imagens também exige Tesseract e o pacote de idioma português no
Windows.

## Configuração

Copie `.env.example` para `.env`. O prefixo histórico `LITIGATION_` foi
mantido por compatibilidade, embora o runtime agora seja exclusivamente
Consumer.

Configuração para testar o JUÁ:

```powershell
python -m pip install -e ".[local-embeddings]"
```

```env
LITIGATION_EMBEDDING_PROVIDER=sentence_transformers
LITIGATION_EMBEDDING_MODEL=ufca-llms/jua-4B-mixed
LITIGATION_EMBEDDING_QUERY_INSTRUCTION=Instruct: Dada uma consulta jurídica brasileira, recupere os trechos ou dispositivos legais vigentes mais relevantes. Query:
LITIGATION_EMBEDDING_DEVICE=cpu
LITIGATION_EMBEDDING_BATCH_SIZE=2
LITIGATION_RETRIEVAL_MODE=hybrid
```

Alterar corpus, modelo ou revisão do embedding cria um namespace versionado,
sem misturar vetores incompatíveis. Por padrão ele é uma coleção Chroma.
Para migrar uma coleção local já pronta para PostgreSQL sem recalcular os
embeddings, instale o extra, habilite pgvector no banco de destino e configure
o DSN:

```powershell
python -m pip install -e ".[postgres]"
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

```env
LITIGATION_POSTGRES_DSN=postgresql://postgres:SUA_SENHA@localhost:5432/postgres
```

```powershell
python -m app.consumer.migrate_legal_index_to_postgres
```

Após a mensagem de sucesso, altere também:

```env
LITIGATION_VECTOR_STORE=postgres
```

e valide a execução normal:

```powershell
python -m app.consumer.preindex_legal --check
```

O primeiro comando pode levar dezenas de minutos ou horas com o JUÁ em CPU,
dependendo do hardware e do tamanho dos chunks. A execução mostra o progresso
no terminal. Depois de concluído, a API reutiliza os 460 chunks persistidos e
não recalcula a base legal durante a geração do rascunho. Use `--force` somente
para uma reindexação deliberada.

## API

| Método | Rota | Finalidade |
|---|---|---|
| `GET` | `/health` | Vida da API e prontidão do corpus legal |
| `POST` | `/consumer/cases` | Criar caso efêmero e token |
| `GET` | `/consumer/cases/{id}` | Consultar caso autorizado |
| `POST` | `/consumer/cases/{id}/messages` | Adicionar mensagem |
| `PATCH` | `/consumer/cases/{id}/facts` | Revisar, corrigir e confirmar fatos |
| `POST` | `/consumer/cases/{id}/documents` | Enviar evidência |
| `POST` | `/consumer/cases/{id}/notice` | Gerar notificação fundamentada |
| `GET` | `/consumer/cases/{id}/notice` | Ler saída estruturada |
| `GET` | `/consumer/cases/{id}/notice.{md,pdf,docx}` | Exportar notificação |
| `GET` | `/consumer/cases/{id}/notice/retrievals` | Auditar recuperação |
| `DELETE` | `/consumer/cases/{id}` | Apagar caso e vetores de evidência |

Todas as operações do caso exigem o token opaco devolvido na criação. O modo
produção também exige uma API key configurada.

## Testes e avaliação

```powershell
pytest -q
python -m app.evaluation.consumer_runner
python -m app.evaluation.security_benchmark
```

O golden set Consumer mede Recall, Recall por artigo, MRR, NDCG, precisão por
subdivisão, hard negatives, autoridades inativas e abstenção fora do escopo.
Ele é uma semente criada por engenharia e ainda exige revisão jurídica
brasileira independente.

Para avaliar o embedding configurado:

```powershell
python -m app.evaluation.consumer_runner `
  --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever `
  --output consumer-retrieval-results.json
```

## Privacidade e limitações

- Uploads brutos são apagados, mas texto extraído e chunks continuam sendo
  dados pessoais potencialmente sensíveis.
- Casos e notificações ficam em memória. O índice jurídico persiste; vetores de
  evidência são removidos ao apagar o caso e órfãos são eliminados no startup.
- Token de posse e API key compartilhada não substituem autenticação e
  autorização multi-tenant.
- Rate limiting é local ao processo.
- Ainda não existem banco durável criptografado, isolamento por tenant, ledger
  de consentimento, rotina de retenção, fila assíncrona nem redação automática
  de PII nos documentos exportados.
- O corpus constitucional contém dispositivos selecionados, não a Constituição
  completa.
- Corpus, políticas e labels de avaliação exigem revisão jurídica independente
  antes de uso público em produção.

Consulte [SECURITY.md](SECURITY.md),
[docs/architecture.md](docs/architecture.md) e os [ADRs](docs/adr).
