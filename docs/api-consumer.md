# API Consumer — guia rápido

## Endpoint one-shot (integração B2B)

Gera um rascunho em **uma requisição**: texto livre e, opcionalmente, um anexo.
O fluxo multi-campo (`/consumer/cases` … `/notice`) permanece disponível para a
UI Streamlit.

```http
POST /consumer/prompt-notices
Content-Type: multipart/form-data
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `text` | Sim | Relato / prompt (1–20.000 caracteres) |
| `file` | Não | PDF, PNG, JPG ou JPEG (máx. 20 MB) |

Sem `X-Consumer-Case-Token`. Se `LITIGATION_API_AUTH_KEY` estiver configurada,
envie `X-API-Key`.

### Respostas

| Status | Corpo |
|---|---|
| `200` | `text/plain; charset=utf-8` — Markdown do rascunho |
| `422` | Texto inválido, fora de escopo de consumo, arquivo inválido ou bloqueado |
| `503` | Índice legal ausente ou falha na recuperação RAG |

### Exemplos

```bash
# Somente texto
curl -X POST http://localhost:8000/consumer/prompt-notices \
  -F "text=O Nubank debitou R$ 100,00 em julho de 2026 sem autorização. Quero o estorno."

# Texto + PDF
curl -X POST http://localhost:8000/consumer/prompt-notices \
  -F "text=Cobrança indevida da Loja Exemplo. Quero o estorno." \
  -F "file=@extrato.pdf;type=application/pdf"
```

```powershell
# PowerShell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/consumer/prompt-notices" `
  -Form @{ text = "Cobrança indevida do Nubank. Quero o estorno." }
```

### Comportamento

1. A API cria um caso efêmero interno.
2. Extrai o que puder do texto (fornecedor, data, pedido) e completa defaults.
3. Se houver anexo aceito pela varredura de segurança, indexa e cita evidência.
4. Sem anexo, faz só RAG jurídico e indica “Nenhum documento anexado”.
5. Devolve o Markdown e apaga o caso (não deixa estado no servidor).

Pré-requisito: `GET /health` com `legal_corpus_ready: true`. No Docker isso
acontece automaticamente no `docker compose up` (serviço `legal-index`). Em
Python local: `python -m app.consumer.preindex_legal`.

## Fluxo multi-campo (legado / Streamlit)

```text
POST   /consumer/cases
PATCH  /consumer/cases/{id}/facts
POST   /consumer/cases/{id}/documents
PATCH  /consumer/cases/{id}/facts   # facts_confirmed: true
POST   /consumer/cases/{id}/notice  # JSON ConsumerNotice
```

Todas as rotas do caso exigem `X-Consumer-Case-Token`.

Documentação interativa: <http://localhost:8000/docs>
