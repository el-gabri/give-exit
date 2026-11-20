# SPEC: Testes de Regressão Gabriel (Give Exit)

**Branch:** `back/TestesRegressaoGabriel`  
**Apps:** Java BFF/front (`/opt/give-exit`) + Python API consumer (`/opt/give-exit-python/give-exit`)  
**Método:** SDD + TDD · DRY · KISS  
**Regra de paralelismo:** cada task toca **apenas** os arquivos listados (sem overlap)

---

## 1. Contexto e aplicações

| App | Papel | Porta local |
|-----|--------|-------------|
| Java (`give-exit`) | UI Thymeleaf + BFF: autenticação, chat, persiste notificação, chama API | 8080 |
| Python (`give-exit-consumer`) | Gera notificação extrajudicial (`POST /consumer/prompt-notices`), `/health` | 8000 |

Fluxo feliz atual:

1. Usuário autenticado envia mensagem em `/formJudicialNotification`
2. Browser `POST /api/notificacoes/chat`
3. Java faz `GET {api.consumer.host}/health`
4. Java persiste `notificacoes` (campos de formulário antigo opcionais)
5. Java `POST {api.consumer.host}/consumer/prompt-notices` (multipart, HTTP/1.1)
6. Java grava resposta e devolve 201 ao browser

Falhas reais já vistas em produção (devem virar regressão):

| Sintoma | Causa raiz |
|---------|------------|
| HTTP **502** no chat | `api.consumer.host` apontava ngrok offline → health falhou → `IaServerException` → controller mapeia 502 |
| HTTP **500** no chat | `solucao_esperada` NOT NULL → insert falhou → `DataIntegrityViolationException` |
| HTTP **502** após fix DB | Java `HttpClient` HTTP/2 (h2c) → Uvicorn `Invalid HTTP request` no multipart |
| Config leak | `application.properties` com secrets foi commitado (deve permanecer fora do Git) |

---

## 2. História de usuário

**Como** mantenedor do Give Exit,  
**quero** uma suíte de regressão automatizada cobrindo contrato Java↔API e regras de config,  
**para** que mudanças futuras não reintroduzam 502/500/erros de protocolo/config silenciosos.

Critério de aceite geral: todos os requisitos R-* abaixo têm teste automatizado verde na branch.

---

## 3. Problemas a evitar → como evitar

| ID | Problema | Estratégia de prevenção |
|----|----------|-------------------------|
| P1 | Host da API apontando para túnel morto | Teste de contrato: health/prompt usam base URL configurável; documentação/teste de ignore do properties |
| P2 | Insert sem campos opcionais quebra | Teste de entidade/`salvarPendente` sem `solucaoEsperada` |
| P3 | Multipart rejeitado (h2c) | Teste: `HttpClient` em HTTP_1_1; API Dockerfile/compose com `--http h11` |
| P4 | Falha da API vira 500 genérico | Teste: `IaServerException` → HTTP 502 no chat |
| P5 | Secrets no Git | Teste estático: `.gitignore` contém `application.properties`; arquivo não está no índice git |
| P6 | `/health` da API muda contrato | Teste: status 200 + `status`/`product` presentes |
| P7 | `prompt-notices` text-only quebra contrato | Teste: 201 + `notice.full_text` (ou equivalente) com LLM mock |

---

## 4. Requisitos testáveis

### Java (código)

| ID | Requisito | Como testar | Arquivo(s) exclusivos da task |
|----|-----------|-------------|-------------------------------|
| R-J1 | `IaServerService` usa `HttpClient.Version.HTTP_1_1` | Assert via reflexão no campo `client` | `.../regression/IaServerHttpVersionRegressionTest.java` |
| R-J2 | Health da consumer HTTP ≠ 200 lança `IaServerException` | `HttpServer` JDK local + service real | `.../regression/IaServerHealthFailureRegressionTest.java` |
| R-J3 | Prompt-notices HTTP 2xx interpreta JSON com `notice.full_text` | `HttpServer` JDK local + multipart | `.../regression/IaServerPromptSuccessRegressionTest.java` |
| R-J4 | Prompt-notices HTTP 4xx lança `IaServerException` (não engole) | `HttpServer` JDK local | `.../regression/IaServerPromptFailureRegressionTest.java` |
| R-J5 | `POST /api/notificacoes/chat` com `IaServerException` responde **502** + `message` | `@WebMvcTest(PageController)` + mocks | `.../regression/ChatEndpointBadGatewayRegressionTest.java` |
| R-J6 | `POST /api/notificacoes/chat` com `RuntimeException` genérica responde **500** | `@WebMvcTest(PageController)` | `.../regression/ChatEndpointInternalErrorRegressionTest.java` |
| R-J7 | `Notificacao` permite null em `solucaoEsperada`, `tipoProblema`, `resumo`, `titulo` (nullable JPA) | Assert anotações `@Column` | `.../regression/NotificacaoNullableColumnsRegressionTest.java` |
| R-J8 | `.gitignore` lista `src/main/resources/application.properties` | Lê arquivo do repo | `.../regression/ApplicationPropertiesGitignoreRegressionTest.java` |

### Java (interface / contrato HTTP local — smoke opcional marcado)

| ID | Requisito | Como testar |
|----|-----------|-------------|
| R-JI1 | Com stack no ar: `GET /api/health` reflete consumer OK (200) ou 503 se down | Teste de integração tag `smoke` (pode `@Disabled` se env não setado) |

### Python (código)

| ID | Requisito | Como testar | Arquivo(s) exclusivos |
|----|-----------|-------------|----------------------|
| R-P1 | `GET /health` → 200 e JSON com `status` | ASGI + Settings MOCK/MEMORY | `tests/regression/test_health_contract.py` |
| R-P2 | `POST /consumer/prompt-notices` text-only → 201 e `notice` com texto | ASGI + corpus prepare + MOCK LLM | `tests/regression/test_prompt_notices_contract.py` |
| R-P3 | Dockerfile CMD inclui `--http` `h11` | Lê Dockerfile | `tests/regression/test_uvicorn_http11_dockerfile.py` |
| R-P4 | `compose.override.yaml` api command inclui `h11` | Lê compose.override.yaml | `tests/regression/test_uvicorn_http11_compose.py` |
| R-P5 | Com `api_auth_key` setada, `/health` continua público; rota consumer exige chave | ASGI secured | `tests/regression/test_api_key_gate_regression.py` |

### Python (interface)

| ID | Requisito | Como testar |
|----|-----------|-------------|
| R-PI1 | Stack no ar: `curl :8000/health` → 200 | smoke opcional `SMOKE=1` |

---

## 5. Tasks pequenas (paralelizáveis)

Ordem TDD: **escrever teste falhando → mínimo código/config se necessário → verde**.

### Wave A — foundation (sequencial curto, 1 agente)

| Task | Escopo | Arquivos |
|------|--------|----------|
| T-A1 | Garantir `spring-boot-starter-test` no `pom.xml` se ausente; criar `application-test.properties` mínimo | **só** `pom.xml`, `src/test/resources/application-test.properties` |

### Wave B — Java regressão (paralelo, arquivos distintos)

| Task | Req | Arquivo de teste |
|------|-----|------------------|
| T-B1 | R-J1 | `IaServerHttpVersionRegressionTest.java` |
| T-B2 | R-J2 | `IaServerHealthFailureRegressionTest.java` |
| T-B3 | R-J3 | `IaServerPromptSuccessRegressionTest.java` |
| T-B4 | R-J4 | `IaServerPromptFailureRegressionTest.java` |
| T-B5 | R-J5 | `ChatEndpointBadGatewayRegressionTest.java` |
| T-B6 | R-J6 | `ChatEndpointInternalErrorRegressionTest.java` |
| T-B7 | R-J7 | `NotificacaoNullableColumnsRegressionTest.java` |
| T-B8 | R-J8 | `ApplicationPropertiesGitignoreRegressionTest.java` |

### Wave C — Python regressão (paralelo)

| Task | Req | Arquivo |
|------|-----|---------|
| T-C1 | R-P1 | `tests/regression/test_health_contract.py` |
| T-C2 | R-P2 | `tests/regression/test_prompt_notices_contract.py` |
| T-C3 | R-P3 | `tests/regression/test_uvicorn_http11_dockerfile.py` |
| T-C4 | R-P4 | `tests/regression/test_uvicorn_http11_compose.py` |
| T-C5 | R-P5 | `tests/regression/test_api_key_gate_regression.py` |

### Wave D — validação

| Task | Escopo |
|------|--------|
| T-D1 | Rodar suíte Java regression + Python regression |
| T-D2 | Agente final confere cada R-* ↔ teste existente e verde |

---

## 6. Restrições de implementação

- **Não** commitar `application.properties` real.
- Preferir `com.sun.net.httpserver.HttpServer` no Java para não adicionar deps pesadas, **ou** `okhttp-mockwebserver` se já for aceitável no pom (KISS: JDK HttpServer).
- Python: `LLMProvider.MOCK` + `VectorStoreBackend.MEMORY`; não carregar JUÁ real.
- Não editar o mesmo arquivo em duas tasks da mesma wave.
- Mudanças de produção só se o teste TDD exigir (ex.: getter de versão HTTP) — manter mínimo.

---

## 7. Comandos

Documentados também nos READMEs de cada app.

```bash
# Java (ver README.md)
cd /opt/give-exit && ./mvnw -Dtest='com.lab.give_exit.regression.**' test

# Python (ver README.md / README-pt.md)
cd /opt/give-exit-python/give-exit && .venv/bin/python -m pytest tests/regression -q
```

---

## 8. Definition of Done

- [ ] Spec versionada nesta branch nos dois repos (espelho)
- [ ] Todos R-J* e R-P* com teste dedicado
- [ ] `./mvnw ... regression` verde
- [ ] `pytest tests/regression` verde
- [ ] Review final do agente D2 aprovado contra esta spec
