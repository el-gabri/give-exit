```http
Content-Type: multipart/form-data
X-Consumer-Case-Token: {case_token}
```

### Formulário

| Campo | Tipo | Descrição |
|---|---|---|
| `file` | arquivo | PDF, PNG, JPG ou JPEG (máx. 20 MB) |

O arquivo passa por extração de texto/OCR e varredura de prompt injection. Só
documentos **aceitos** entram no RAG.

### Exemplo (curl)

```bash
curl -X POST "http://localhost:8000/consumer/cases/CASE_ID/documents" \
  -H "X-Consumer-Case-Token: CASE_TOKEN" \
  -F "file=@/caminho/para/extrato.pdf;type=application/pdf"
```

### Exemplo (PowerShell)

```powershell
$headers = @{ "X-Consumer-Case-Token" = $token }
$form = @{ file = Get-Item "C:\caminho\para\extrato.pdf" }
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/consumer/cases/$caseId/documents" `
  -Headers $headers -Form $form
```

|---|---|---|



```bash


```powershell
```





```


