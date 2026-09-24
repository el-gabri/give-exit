# API do consumidor: guia de integração

A API oferece dois caminhos para chegar a uma notificação extrajudicial. Ambos
criam um **caso** efêmero, protegido por um token de posse, e guardam a mesma
trilha de auditoria (buscas, citações, hashes das fontes).

| Caminho | Quando usar | Revisão dos fatos | Evidência |
|---|---|---|---|
| `POST /consumer/prompt-notices` | Uma chamada com o relato em texto livre | Não: os fatos são extraídos automaticamente | Opcional (um arquivo) |
| Jornada do caso (`/consumer/cases/...`) | Interface guiada, com revisão | Sim: o consumidor confirma os fatos | Obrigatória |

Antes de gerar qualquer notificação, o corpus jurídico precisa estar indexado.
Com Docker Compose isso acontece no serviço `legal-index`; sem Docker, rode
`python -m app.consumer.preindex_legal`. Enquanto não houver índice, a geração
responde `503`. `GET /health` informa `legal_corpus_ready`.

## Notificação em uma chamada

```http
POST /consumer/prompt-notices
Content-Type: multipart/form-data
```

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `text` | texto | sim | Relato do problema e do pedido, até 20.000 caracteres |
| `file` | arquivo | não | Uma evidência em PDF, PNG, JPG ou JPEG |

| Parâmetro | Valores | Padrão |
|---|---|---|
| `format` | `json`, `markdown` | `json` |

### Exemplo (curl)

```bash
curl -X POST "http://localhost:8000/consumer/prompt-notices" \
  -F "text=O Nubank debitou R\$ 100,00 em julho de 2026 sem autorização. Quero o estorno imediato da cobrança." \
  -F "file=@extrato.pdf;type=application/pdf"
```

### Exemplo (PowerShell 7)

```powershell
$form = @{
  text = "O Nubank debitou R$ 100,00 em julho de 2026 sem autorização. Quero o estorno imediato da cobrança."
  file = Get-Item ".\extrato.pdf"
}
$result = Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/consumer/prompt-notices" -Form $form
$result.notice.full_text
```

### Resposta `201` (JSON)

```json
{
  "case_id": "2c04206bb24b4f628e57253443e4a5ee",
  "case_token": "B993MLLM60xYUrPgaBS1B7zSHeF3rfUNpoLGlaBDv4c",
  "notice": {
    "generation_mode": "prompt",
    "full_text": "# NOTIFICAÇÃO EXTRAJUDICIAL COM PROPOSTA DE ACORDO\n...",
    "legal_grounds": ["..."],
    "evidence_references": [],
    "warnings": [
      "Rascunho gerado a partir de um único texto: os fatos foram extraídos automaticamente e não foram revisados pelo(a) consumidor(a).",
      "Nenhum documento foi anexado; a notificação não cita evidências.",
      "Preencha antes de enviar: nome do(a) consumidor(a)."
    ]
  }
}
```

`notice` tem o mesmo formato de `GET /consumer/cases/{id}/notice`. Guarde
`case_id` e `case_token`: com eles o cliente exporta a notificação, consulta a
auditoria ou apaga o caso (veja abaixo).

Com `?format=markdown`, o corpo é só o texto da notificação
(`text/markdown`), e as credenciais do caso vêm nos cabeçalhos
`X-Consumer-Case-Id` e `X-Consumer-Case-Token`.

### O que muda em relação à jornada revisada

- `generation_mode` é `prompt`, e os avisos dizem que os fatos não foram
  revisados.
- O que o texto não informa não é inventado: nome do consumidor, empresa
  notificada e data aparecem como `[PREENCHER ...]` na notificação e são
  listados no aviso "Preencha antes de enviar".
- Frases com "quero", "desejo" ou "solicito" viram o pedido; o restante é o
  relato dos fatos.
- Sem anexo, ou com um anexo que não sustenta os fatos, a notificação não cita
  evidência e diz isso no texto e nos avisos.
- A mensagem original fica registrada no caso, e os fundamentos jurídicos
  passam pela mesma política de seleção e pela mesma exigência de concordância
  entre as buscas densa e lexical.

### Erros

| Código | Quando |
|---|---|
| `422` | Texto vazio ou longo demais; relato fora de relação de consumo; anexo em formato não suportado, corrompido ou com instruções dirigidas à IA |
| `413` | Arquivo maior que `LITIGATION_MAX_UPLOAD_BYTES` (20 MB por padrão) |
| `429` | Limite por minuto de criação de casos, envios ou geração de notificações |
| `503` | Corpus jurídico não indexado; nenhum fundamento jurídico sustentado pela recuperação; capacidade de casos esgotada |

Em qualquer erro depois da criação do caso, o caso é apagado: o cliente nunca
recebeu o token, então nada fica guardado.

## Depois da geração

Todas as rotas abaixo exigem o cabeçalho `X-Consumer-Case-Token`.

| Método | Rota | Resultado |
|---|---|---|
| `GET` | `/consumer/cases/{id}/notice` | Notificação estruturada |
| `GET` | `/consumer/cases/{id}/notice.md` | Markdown |
| `GET` | `/consumer/cases/{id}/notice.pdf` | PDF |
| `GET` | `/consumer/cases/{id}/notice.docx` | DOCX |
| `GET` | `/consumer/cases/{id}/notice/retrievals` | Auditoria: consultas, rankings por canal, IDs e hashes |
| `GET` | `/consumer/cases/{id}` | Caso, com mensagens e fatos extraídos |
| `DELETE` | `/consumer/cases/{id}` | Apaga o caso e os vetores de evidência (`204`) |

```bash
curl -H "X-Consumer-Case-Token: $TOKEN" \
  "http://localhost:8000/consumer/cases/$CASE_ID/notice.pdf" -o notificacao.pdf
```

O caso expira depois de `LITIGATION_CASE_IDLE_TTL_SECONDS` sem uso (24 h por
padrão). Quem não precisa da auditoria pode chamar `DELETE` logo após baixar a
notificação.

## Jornada do caso

1. `POST /consumer/cases` devolve `case_id` e `case_token`.
2. `POST /consumer/cases/{id}/messages` com `{"text": "..."}` registra o
   relato e extrai os fatos explícitos.
3. `POST /consumer/cases/{id}/documents` (multipart, campo `file`) envia cada
   evidência. Só documentos aceitos pela varredura entram no RAG.
4. `PATCH /consumer/cases/{id}/facts` corrige os fatos e, com
   `"facts_confirmed": true`, confirma.
5. `POST /consumer/cases/{id}/notice` gera a notificação (`generation_mode`
   `case`). Se faltar algo, responde `409` com a lista `missing`.

A especificação completa, com todos os campos, está em
`http://localhost:8000/docs`.
