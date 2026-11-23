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
responde `503`. `GET /health` informa `legal_corpus_ready` e
`query_embedding_warm`. Este último é `false` enquanto um modelo local de
consulta ainda carrega na inicialização; uma notificação pedida nesse intervalo
espera o modelo.

## Notificação em uma chamada

```http
POST /consumer/prompt-notices
Content-Type: multipart/form-data
```

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `text` | texto | sim | Relato do problema e do pedido, até 20.000 caracteres |
| `desired_resolution` | texto | não | O pedido à empresa, até 2.000 caracteres. Quando enviado, é o pedido literal e `text` fica inteiro como relato |
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
- Sem `desired_resolution`, as frases com um verbo de pedido em primeira pessoa
  ("quero", "queria", "gostaria", "desejo", "solicito", "peço", "exijo",
  "requeiro") viram o pedido, e o restante é o relato dos fatos. "Meu pedido"
  não conta, porque também é a compra ("meu pedido não chegou").
- Sem anexo, ou com um anexo que não sustenta os fatos, a notificação não cita
  evidência e diz isso no texto e nos avisos.
- A mensagem original fica registrada no caso. Os fundamentos jurídicos passam
  pela mesma política de seleção e pela mesma exigência de concordância entre as
  buscas densa e lexical.
- Um dispositivo que o próprio consumidor cita, com a lei nomeada depois do
  artigo, é citado sem essa exigência de concordância, mas com a mesma política
  de elegibilidade (ADR 0025). Valem, por exemplo, "art. 35, inciso I, do CDC",
  "§ 1º do art. 18 da Lei 8.078/90", "art. 18 da LGPD" e "art. 186 do Código
  Civil". Esse fundamento tem `cited_by_consumer: true`.

### Erros

| Código | Quando |
|---|---|
| `422` | Texto vazio ou longo demais; relato fora de relação de consumo; anexo em formato não suportado, corrompido ou com instruções dirigidas à IA; notificação recusada por falta de suporte que se repetiria com o mesmo texto |
| `413` | Arquivo maior que `LITIGATION_MAX_UPLOAD_BYTES` (20 MB por padrão) |
| `429` | Limite por minuto de criação de casos, envios ou geração de notificações |
| `503` | Corpus jurídico não indexado; notificação recusada enquanto a busca semântica estava indisponível (tentar de novo pode resolver); capacidade de casos esgotada |

#### Recusa por falta de suporte

Quando a recuperação não encontra fundamento jurídico verificável, a resposta
explica o motivo em vez de pedir uma nova tentativa às cegas:

```json
{
  "detail": {
    "message": "Não foi possível gerar o rascunho com segurança porque a recuperação não encontrou suporte verificável em fundamentos jurídicos. Gerar de novo com os mesmos dados levará ao mesmo resultado; veja as sugestões para ajustar o relato.",
    "reasons": ["no_requested_resolution", "narrative_truncated", "no_retrieval_agreement"],
    "hints": ["Diga o que você quer que a empresa faça em uma frase própria, ...", "..."],
    "refusal_url": "/consumer/cases/2c04.../notice/refusal",
    "case_id": "2c04...",
    "case_token": "B993..."
  }
}
```

| Motivo (`reasons`) | Significado |
|---|---|
| `semantic_retrieval_unavailable` | A busca semântica estava indisponível. A resposta é `503`, e tentar de novo pode resolver |
| `no_requested_resolution` | Nenhuma frase diz o que o consumidor pede |
| `narrative_truncated` | O relato (mais de 1.050 caracteres) ou o pedido (mais de 500) é maior que o trecho lido pela busca jurídica |
| `no_retrieval_agreement` | Nenhum dispositivo foi apontado pelas duas buscas ao mesmo tempo |
| `no_eligible_ground` | O que foi encontrado não pode fundamentar a notificação, como normas penais ou processuais |
| `grounds_removed_by_verifier` | O verificador opcional removeu todos os fundamentos |
| `no_evidence_support` | Jornada do caso: nenhum trecho dos documentos sustenta os fatos |

Cada motivo tem uma sugestão em `hints`, na mesma ordem. Na recusa, o caso
**não** é apagado: as credenciais vêm no corpo e nos cabeçalhos
`X-Consumer-Case-Id` e `X-Consumer-Case-Token`, e `refusal_url` devolve a
recusa completa, com a auditoria das buscas. Em qualquer outro erro depois da
criação do caso, o caso é apagado: o cliente nunca recebeu o token, então nada
fica guardado.

## Depois da geração

Todas as rotas abaixo exigem o cabeçalho `X-Consumer-Case-Token`.

| Método | Rota | Resultado |
|---|---|---|
| `GET` | `/consumer/cases/{id}/notice` | Notificação estruturada |
| `GET` | `/consumer/cases/{id}/notice.md` | Markdown |
| `GET` | `/consumer/cases/{id}/notice.pdf` | PDF |
| `GET` | `/consumer/cases/{id}/notice.docx` | DOCX |
| `GET` | `/consumer/cases/{id}/notice/retrievals` | Auditoria: consultas, rankings por canal, IDs e hashes |
| `GET` | `/consumer/cases/{id}/notice/refusal` | A última recusa: motivos, sugestões e a auditoria das buscas (`404` se não houve) |
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
   `case`). Se faltar algo, responde `409` com a lista `missing`. Sem suporte
   verificável, responde com a mesma recusa estruturada do caminho em uma
   chamada (`422`, ou `503` com a busca semântica indisponível), sem as
   credenciais, que o cliente já tem.

A especificação completa, com todos os campos, está em
`http://localhost:8000/docs`.
