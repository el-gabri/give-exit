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

Para clientes da API, `POST /consumer/prompt-notices` resume essa jornada a uma
requisição: texto livre e, opcionalmente, um arquivo de evidência. Os passos 2 e
3 tornam-se automáticos e opcionais, e a notificação registra isso
(`generation_mode: prompt`, avisos e marcadores `[PREENCHER ...]` para o que o
texto não informou). Fundamentação jurídica, caso, exportações e auditoria da
recuperação são os mesmos da jornada revisada. Veja
[docs/api-consumer.md](docs/api-consumer.md).

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
        +--> PostgreSQL/Chroma + dense + BM25 (igual em todo backend) + RRF
        |      +--> evidências aceitas
        |      +--> CDC, LGPD e Código Civil (escopo limitado) + dispositivos da CF
        +--> política jurídica e cenário financeiro determinísticos
        |      +--> verificação opcional, por LLM, de cada fundamento com trechos literais
        +--> notificação auditável --> Markdown / PDF / DOCX
```

Não existe LLM conversacional dando aconselhamento jurídico. Um LLM configurado
pode revisar semanticamente trechos suspeitos de documentos. Um verificador
opcional pode julgar se cada fundamento selecionado se aplica aos fatos
confirmados, mas só contam veredictos sustentados por dois trechos literais, e
ele só pode retirar fundamentos, nunca acrescentar. Um compositor OpenAI
opcional e separado pode redigir somente cinco campos de prosa depois que
fatos, evidências, fundamentos, pedidos, valores e citações já foram fixados de
forma determinística; saída inválida aciona o compositor determinístico.

## Fontes e citações

- CDC, LGPD e Código Civil vêm de snapshots fixados do Planalto, lidos por um
  único parser de leis, com manifestos que fixam os hashes do arquivo e do texto
  extraído. Do Código Civil só a Parte Geral e o Livro I da Parte Especial entram
  no índice; os demais livros ficam no corpus para auditoria. Dispositivos que
  uma notificação nunca pode citar também ficam fora do índice, para não
  ocuparem posições da busca (ADR 0019).
- Dispositivos constitucionais selecionados são versionados no corpus.
- Fundamentos da LGPD e do Código Civil dividem no máximo três vagas por
  notificação e nenhuma em recuperação apenas lexical. O Código Civil só é
  citado ao lado de um fundamento do CDC; a LGPD pode sustentar sozinha uma
  notificação sobre dados pessoais (ADR 0020). O Título VI do Livro I da Parte Especial do Código Civil (espécies
  de contrato) não é citado nem indexado.
- Os chunks preservam lei, artigo, subdivisão, URL oficial, release, vigência e
  hashes de origem.
- A recuperação jurídica combina semântica e correspondência lexical exata.
- O principal controle de precisão é a concordância: um artigo só vira
  fundamento quando a busca densa e a lexical o colocam entre os 13 primeiros.
  A posição em cada canal fica no trace e o controle a lê de lá, então vale para
  quaisquer pesos de fusão e com reranker. Em modo apenas lexical, o relato e a
  solução desejada também são buscados separadamente e o artigo precisa estar
  entre os três primeiros nas duas buscas; senão o serviço pede nova tentativa.
- Evidências são buscadas com as palavras do consumidor e com os
  identificadores que os documentos imprimem (protocolos, data, valores), e
  indexadas apenas com o texto do próprio documento. O trecho citado é a
  passagem do chunk que melhor corresponde aos fatos confirmados.
- Recuperar um chunk não o transforma automaticamente em fundamento: uma
  política determinística controla sua elegibilidade e está marcada como
  `requires_legal_review`.
- As citações são reconstruídas no backend; não são strings de citação geradas
  e aceitas do modelo.
- Os traces registram revisão e geração ativa do embedding, cache, eventual
  modo degradado, ranking, posição em cada canal, chunk IDs e hashes das fontes.

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

O serviço `legal-index` roda uma vez antes da API: cria o índice legal ou
confirma que ele está atual e termina. As gerações de embeddings ficam em
`./data/embedding_generations`, compartilhadas com execuções locais de
`python -m app.consumer.preindex_legal`, então um índice criado fora do Docker é
reaproveitado. A API monta esse diretório somente leitura. Para forçar a
reconstrução:

```bash
docker compose run --rm legal-index python -m app.consumer.preindex_legal --force
```

A imagem da API inicia como root apenas para ajustar a posse de volumes novos e
depois roda como `appuser` (uid 10001). O Compose mapeia `host.docker.internal`
para o gateway do host, então o DSN padrão do PostgreSQL também funciona no
Docker Engine em Linux.

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
LITIGATION_EMBEDDING_MODEL_REVISION=57f491c1718171c0ad71d723c4f6b2030684c4eb
LITIGATION_EMBEDDING_EXPECTED_DIMENSIONS=2560
LITIGATION_EMBEDDING_REQUIRE_MODEL_REVISION=true
LITIGATION_EMBEDDING_QUERY_INSTRUCTION=Instruct: Dada uma consulta jurídica brasileira, recupere os trechos ou dispositivos legais vigentes mais relevantes. Query:
LITIGATION_EMBEDDING_DEVICE=cpu
LITIGATION_EMBEDDING_BATCH_SIZE=2
LITIGATION_EMBEDDING_INDEX_SHARD_SIZE=25
LITIGATION_RETRIEVAL_MODE=hybrid
```

O formatador insere a quebra de linha exigida pelo JUÁ depois de `Query:`; os
documentos legais permanecem sem prefixo. Alterar corpus, modelo, revisão exata,
formatador ou hash da instrução cria uma geração diferente. A indexação grava
shards gzip com checksum e manifesto em
`data/embedding_generations/<generation-id>/`, retoma somente shards válidos e
só ativa o namespace após validar cobertura, dimensão e normalização.

Um modelo fixado em um commit carrega do cache local do Hugging Face depois do
primeiro download, sem consultar o Hub: a inicialização fica mais rápida e o
aviso "unauthenticated requests to the HF Hub" só aparece no primeiro download.
A cópia em cache só é usada se o `modules.json` também estiver lá; sem ele, o
sentence-transformers montaria silenciosamente outro encoder a partir dos pesos.
Revisões por branch ou tag continuam consultando o Hub.
`LITIGATION_HF_HUB_OFFLINE=1` (ou `HF_HUB_OFFLINE=1`) proíbe o Hub e falha com uma
mensagem clara se o modelo não estiver em cache. Um `HF_TOKEN` opcional no `.env`
é repassado ao modelo de embedding quando o Hub é consultado. A aplicação lê os
dois do `.env`; as bibliotecas do Hugging Face sozinhas leem apenas o ambiente
do processo.

Por padrão o backend é Chroma. No PostgreSQL, a busca lexical ocorre no banco
com full-text search em português e a busca densa usa pgvector. Para migrar uma
coleção local já pronta sem recalcular embeddings, instale o extra, habilite
pgvector e configure o DSN:

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
dependendo do hardware e do tamanho dos chunks. Cada shard concluído é durável;
reiniciar o mesmo comando continua do último shard verificado. Depois de
concluído, a API reutiliza os 1.644 chunks persistidos. Use `--force` somente para
uma reconstrução deliberada.

O chunking `legal-hierarchy-v4` (ADR 0019) deixa fora do índice os capítulos que
não podem ser citados. Um índice construído com a v3 aparece como não pronto;
executar a pré-indexação uma vez o reconstrói. Só dois textos mudaram: as duas
partes do art. 54-G, I, do CDC, que agora quebram no fim de uma frase. Havendo
uma geração anterior embedada (não adotada) do mesmo modelo, o reaproveitamento
abaixo fornece os outros 1.642 vetores e só esses dois são embedados.

Um novo release do corpus reaproveita os vetores de todo chunk cujo texto não
mudou, vindos de gerações anteriores verificadas do mesmo modelo, revisão e
formatador de documento. Antes, dois textos reaproveitados são re-embedados
como canário; se divergirem, a construção para e sugere `--no-reuse`, que
recalcula tudo mantendo a retomada.

Um namespace legado pode ser promovido sem outra execução longa se o operador
tiver verificado de forma independente a revisão exata presente no cache:

```powershell
python -m app.consumer.preindex_legal `
  --adopt-source-index <namespace-legado> `
  --attest-source-revision <commit-exato-do-hugging-face>
```

O manifesto registra `adopted_existing_vectors`; essa atestação não vira prova
retroativa de metadados que a execução antiga não registrou.

Os snapshots das leis são atualizados explicitamente, nunca em tempo de execução:

```powershell
python -m app.consumer.update_statute_snapshot --law lgpd
```

O refresher registra o user agent usado, não grava nada quando o texto extraído
não mudou e grava snapshots novos como `pending_review`; confira artigos por
amostragem na página oficial antes de promover o snapshot e de gerar um novo
release do corpus.

Nas consultas, timeout, limite de concorrência, cache por hash e circuit breaker
protegem o modelo local. Se ele falhar, o modo híbrido pode degradar para busca
lexical auditada (`degraded_mode=lexical_only`), sem relaxar os gates jurídicos
ou de citação.

## API

| Método | Rota | Finalidade |
|---|---|---|
| `GET` | `/health` | Vida da API e prontidão do corpus legal |
| `POST` | `/consumer/prompt-notices` | Uma requisição: texto livre e arquivo opcional viram notificação, caso e token |
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
produção também exige uma API key configurada. Exemplos de requisição e
resposta, erros e o fluxo em uma requisição estão em
[docs/api-consumer.md](docs/api-consumer.md).

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

Para avaliar a geração configurada que já está ativa, sem reindexação implícita:

```powershell
python -m app.evaluation.consumer_runner `
  --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever `
  --output consumer-retrieval-results.json
```

A avaliação pelo caminho da notificação mede os fundamentos que o seletor de
produção realmente citaria (as três consultas de produção, k=8, e o mesmo
`select_legal_grounds` usado pelo serviço), e não candidatos ranqueados:

```powershell
python -m app.evaluation.consumer_runner --evaluate-notice --output notice-results.json
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured `
  --require-semantic --output configured-notice-results.json
```

O relatório traz fundamentos citados, citações ruins conhecidas (hard negatives
rotulados que foram citados), recall exato das unidades citadas, abstenção e
sucesso da recuperação semântica. Duas opções medem os controles de precisão em
qualquer stack. `--agreement-max-rank N`, repetida, compara profundidades do
gate a partir de uma única recuperação e imprime uma tabela;
`--ground-verifier llm` executa o verificador de fundamentos configurado e
conta os fundamentos que ele remove:

```powershell
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured `
  --agreement-max-rank 12 --agreement-max-rank 16 --agreement-max-rank 20 `
  --output configured-notice-sweep.json
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured `
  --ground-verifier llm --output configured-notice-verified.json
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
- Do Código Civil só a Parte Geral e o Livro I da Parte Especial são indexados;
  esse escopo, as regras de elegibilidade da LGPD e os novos casos do golden
  ainda exigem revisão jurídica especializada.
- Corpus, políticas e labels de avaliação exigem revisão jurídica independente
  antes de uso público em produção.

Consulte [SECURITY.md](SECURITY.md),
[docs/architecture.md](docs/architecture.md) e os [ADRs](docs/adr).
