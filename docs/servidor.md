# Servidor de produção

Este checkout sobe em `srv1983965` (`2.25.225.28`). Sem as alterações abaixo, `POST /consumer/prompt-notices` responde que a recuperação não encontrou fundamento jurídico: a API fica no índice de demonstração (Chroma e embeddings fictícios) e ignora os vetores reais.

O arquivo `.env` não entra no Git. Ele já existe neste servidor e está no `.gitignore`.

## Aplicação Python

Arquivo: `/opt/give-exit-python/give-exit/.env`

- `LITIGATION_API_EXTRAS=ocr,postgres,local-embeddings` — a imagem precisa ser reconstruída (`docker compose up --build`) depois de mudar este valor. Sem `postgres` e `local-embeddings` não há cliente do banco nem o JUÁ.
- `LITIGATION_VECTOR_STORE=postgres`
- `LITIGATION_DOCKER_POSTGRES_DSN` aponta para `host.docker.internal:5432/give_exit_vet`, não para o database `postgres`.
- Embeddings: `sentence_transformers`, modelo `ufca-llms/jua-4B-mixed`, revisão `57f491c1718171c0ad71d723c4f6b2030684c4eb`, 2560 dimensões, revisão obrigatória. Outro modelo não lê o namespace restaurado.
- A instrução de consulta é a do `.env.example`.

`compose.override.yaml` (também fora do Git; o Compose o carrega sozinho) mantém `restart: unless-stopped` na API e no frontend. O `legal-index` não reembeda o corpus: adota o índice já restaurado

`give-exit-consumer-br-consumer-law-2026-09-12-v4-6b18d3db4b-6648b139826d`

com `--attest-source-revision` igual à revisão do JUÁ acima. Sem esse manifesto local, o boot tenta recalcular os 1644 chunks na CPU.

A API escuta só em `127.0.0.1:8000`. Do Windows, `curl` do PowerShell não é o curl: use `curl.exe`. O IP público na porta 8000 não alcança o processo.

Serviço de boot: `give-exit-python.service` (`docker compose up -d` depois do Docker e do PostgreSQL).

## PostgreSQL

Os vetores estão em `give_exit_vet`, tabela `public.give_exit_vector_chunks` (extensão `vector`). O backup restaurado foi `/opt/db-backup/bkp_vet_24092026`.

O Postgres escutava só em localhost, então o container não conectava. Neste host:

- `/etc/postgresql/18/main/conf.d/docker.conf` define `listen_addresses` com `localhost`, `172.17.0.1` e `172.18.0.1`. Não usar `*` na interface pública.
- `pg_hba.conf` autoriza o usuário `postgres` no database `give_exit_vet` a partir de `172.16.0.0/12`.
- O UFW libera TCP 5432 só dessa mesma rede Docker. A porta 5432 não fica aberta na internet.

## Fora deste repositório

- Java: `give-exit-java.service`, trabalho `/opt/give-exit`, porta 8080. O nginx encaminha o site para essa porta.
- HTTPS: certificado Let's Encrypt `giveexit.tech`, com `www.giveexit.tech` e `ia.giveexit.tech`, instalado em `/etc/nginx/sites-available/ia.giveexit.tech`. HTTP desses nomes redireciona para HTTPS. Renovação pelo Certbot; o nginx precisa continuar escutando 443.
