# Requisito del enunciado -> dónde se cumple

Enunciado: `Prompts.Clase18.md` (cinco bloques y cinco prompts que forman un único proyecto).
Las desviaciones están marcadas con **(D)** y justificadas en la última columna o en
[decisiones.md](decisiones.md).

## Bloque 1: scaffolding del repositorio

| Requisito | Dónde | Notas |
|---|---|---|
| Estructura `src/`, `tests/unit`, `tests/integration`, `requirements*.txt`, `pyproject.toml`, `README.md` | árbol del repo | Añadidos `api.py`, `providers.py`, `observability.py`, `evals/`, `deploy/`, `monitoring/` para cubrir los bloques 4 y 5 |
| `[tool.ruff]` `line-length = 100`, `target-version = "py311"`, reglas E, F, I, N, W | `pyproject.toml` | Además B (bugbear) y UP (pyupgrade) |
| `[tool.mypy]` `python_version = "3.11"`, `check_untyped_defs`, `ignore_missing_imports` | `pyproject.toml` | Además `disallow_untyped_defs` y el plugin de pydantic |
| `[tool.pytest.ini_options]` `testpaths`, marcadores `integration` y `slow` | `pyproject.toml` | `-m 'not integration'` por defecto: los tests que cuestan dinero son opt-in |
| `ci.yml` de 5 etapas: lint, type-check, unit-tests, integration-tests, quality-gate | `.github/workflows/ci.yml` | Más `llm-quality-tests` (bloque 3) |
| `unit-tests` depende de lint y type-check, `--cov-fail-under=80`, sube artefacto de cobertura | job `unit-tests` | Matriz 3.11 y 3.12; un artefacto por celda |
| `integration-tests` solo en `push` a `main` | job `integration-tests` | `if: github.event_name == 'push' && github.ref == 'refs/heads/main'` |
| `quality-gate` falla si `unit-tests` falla | job `quality-gate` | `if: always()`; además exige lint, type-check y, en `main`, los dos jobs de LLM. "skipped" en PR es correcto |
| `AIAgent` asíncrono con `process_query(text)` y bucle de tool calling (OpenAI/LangChain) | `src/agent.py` | **(D)** OpenAI SDK sin LangChain; ver decisiones |
| `TavilySearchTool` asíncrona y `RAGTool` con Boto3 | `src/tools.py` | `RAGTool` usa `retrieve` de Bedrock KB si hay `KB_ID` y, si no, un corpus local |
| `config.py` con pydantic-settings, claves validadas | `src/config.py` | `SecretStr`, validadores, `ENV_FILE` |
| `requirements.txt`: langchain, openai, pydantic-settings, tavily-python, boto3, python-dotenv | `requirements.txt` | **(D)** sin `langchain`; añadidos fastapi, uvicorn, prometheus-client, langfuse, sentry-sdk. Versiones fijadas con `==` |
| `requirements-dev.txt`: pytest, pytest-asyncio, pytest-cov, pytest-mock, ruff, mypy | `requirements-dev.txt` | Más pytest-timeout (lo pide el bloque 3), pytest-socket, httpx2, PyYAML |
| Todo en inglés, docstrings Google, tipado estricto, `logging` sin `print`, `AgentBaseException` y subclases | `src/` | `src/exceptions.py` |
| Comandos para inicializar y probar | README, sección "Ejecutar en local" | |

## Bloque 2: `tests/unit/test_agent.py`

| Requisito | Dónde |
|---|---|
| `TestRouter` con `parametrize`: palabras clave, saludos, vacíos y puntuación | `tests/unit/test_agent.py::TestRouter` |
| `TestSystemPrompt`: rol de asistente e instrucción en español | `TestSystemPrompt` |
| `TestResponseParsing`: formato Bedrock, `ValueError` si vacío o malformado | `TestResponseParsing` (10 casos malformados) |
| `TestCostCalculation`: tiers (haiku...), tolerancias, caso cero | `TestCostCalculation` |
| Sin llamadas externas, deterministas, rápidos | `tests/unit/conftest.py` bloquea las conexiones no locales (pytest-socket) y `test_observability.py` lo comprueba |
| `unittest.mock` / `MagicMock` | `test_observability.py` (LangFuse y Sentry); proveedores con transportes falsos y `Stubber` de botocore |
| Cobertura 100 % de la lógica del agente | `src/agent.py` 100 % de líneas y ramas (ver `evidencias/local/04-*`) |

## Bloque 3: tests de calidad del LLM

| Requisito | Dónde | Notas |
|---|---|---|
| `tests/integration/test_api.py` con `@pytest.mark.integration` | `pytestmark` en el módulo | Conectividad real y API HTTP |
| Cliente real, helper `invoke_agent`, JSON de entrada/salida | `tests/integration/conftest.py` (`ask`) | **(D)** Azure OpenAI en lugar de Bedrock: el alumno no tiene cuenta de AWS y Haiku 3 de Anthropic está fuera de servicio en Bedrock desde el 10-sep-2026 |
| Español, exactitud factual, concisión ("solo SÍ o NO") | `TestLLMQuality` | |
| Latencia: simples < 10 s, complejas < 30 s | `TestLatency` | Medida con `time.perf_counter` |
| Regresión con `EVAL_DATASET`, `must_contain` / `must_not_contain` | `TestRegression` + `evals/dataset.jsonl` | 12 casos, parametrizados por id |
| Mensajes de error claros, tokens mínimos | aserciones con `f"..."`; prompts cortos | |
| Código Bedrock del prompt | `src/providers.py::BedrockProvider`, `src/bedrock_format.py` | **(D)** probado con `botocore.stub.Stubber`; no ejecutado contra AWS real |
| Extra: LLM-as-a-judge y meta-test del juez | `evals/judge.py`, `TestJudgeMetaTest` | gpt-5.6-sol con esquema estricto |

## Bloque 3 bis: job `llm-quality-tests`

| Requisito | Dónde |
|---|---|
| Depende de `unit-tests` | `needs: [unit-tests]` |
| Solo `push` a `main` | `if:` del job |
| `timeout-minutes: 10` | job |
| Python 3.11, `requirements-dev.txt` | pasos del job |
| `pytest tests/integration/test_llm_quality.py -m integration -x --tb=long -v --timeout=60` | paso "Run LLM quality tests" |
| Secretos inyectados | `env:` del paso (no del job) con `AZURE_OPENAI_*`, `TAVILY_API_KEY`, `AWS_*` opcionales, `AWS_DEFAULT_REGION` por defecto `us-east-1` |
| `quality-gate` incluye `llm-quality-tests` y reporta su resultado | `needs` del job y tabla en `$GITHUB_STEP_SUMMARY` |
| Extra: evaluación con juez, informe en el summary y artefacto | pasos "Run LLM-as-a-judge evaluation", "Publish evaluation report" |

## Bloque 4: CD con blue/green

| Requisito | Dónde | Notas |
|---|---|---|
| `workflow_run` sobre "AI Agent CI", solo `main` y `success` | `cd.yml` (`on` + `if` de `prepare`) | |
| Despliega lo que pasó el CI | `github.event.workflow_run.head_sha` en todos los `checkout` | Mejora sobre el enunciado ("pull the latest code from main") |
| Staging automático y producción con aprobación manual | `environment: staging` / `production` | El reviewer se configura en GitHub, ver README |
| SSH con clave temporal, **StrictHostKeyChecking=no** | `deploy/remote_deploy.sh` | **(D)** `StrictHostKeyChecking=yes` con `known_hosts` fijado: ver decisiones |
| Lee `current_port` para saber qué servicio está activo (8000 / 8001) | `deploy/lib.sh::read_current_port`, `deploy/blue_green_deploy.sh` | |
| Instala dependencias en un virtualenv | `blue_green_deploy.sh` (un venv por release) | El código es el del SHA exacto (`git archive`) |
| Reinicia el servicio **inactivo** | `ctl restart agent-<color>` | |
| Health check de 30 intentos; si falla, para el servicio y sale con error | `wait_healthy` + rama de fallo | El activo no se toca |
| `sed` sobre `proxy_pass` y recarga de nginx | `lib.sh::switch_nginx` | Con `nginx -t` previo y restauración si falla |
| Persiste `current_port` | `write_current_port` (atómico) | |
| Smoke test de staging: `curl` con JSON real + Python valida `response` | `deploy/smoke_test.sh` | |
| Producción: chequeo más estricto de `/health` y `/chat`, `rollback.sh` si no es 200 | `smoke_test.sh --full` + `deploy/rollback.sh` desde `run_deployment.sh` | Comprueba también `/metrics` y que el commit servido sea el esperado |
| Secretos `EC2_HOST`, `EC2_SSH_KEY`, `STAGING_URL`, `PROD_URL` | `cd.yml` | **(D)** sin EC2 se despliega a un host simulado (`deploy/host/`) |
| `set -e` en los scripts | todos usan `set -euo pipefail` | |
| Notificación final con hash corto y marca de tiempo | job `notify` + `deploy/notify_discord.sh` | Discord si hay `DISCORD_WEBHOOK_URL` |
| Extra: simulacro de fallo | job `failure-drill` -> `deploy/test_blue_green_local.sh` | Una v3 rota no recibe tráfico |

## Bloque 5: monitorización

| Requisito | Dónde | Notas |
|---|---|---|
| Capa 1, infraestructura (CloudWatch) | `monitoring/cloudwatch/alarms.json` + test | Cinco alarmas de Lambda de la tabla, validadas como `put-metric-alarm`. No aplicadas a AWS |
| Capa 1 equivalente ejecutable | Prometheus + Alertmanager + Grafana (`monitoring/`) | Es lo que se prueba de verdad en local |
| Capa 2, LangFuse (traces, generations, scores, coste) | `src/observability.py::LangfuseTracer` | Texto enmascarado con `mask_pii`; scores del juez enviados desde `evals/run_eval.py`. Probado con mocks; no contra un servidor LangFuse real |
| Capa 3, Sentry | `src/observability.py::init_sentry`, `scrub_event` | Solo con `SENTRY_DSN`; limpia PII |
| Matriz de alertas | `monitoring/prometheus/alert_rules.yml` | 7 alertas; error rate, P95, coste diario, calidad, pico de tokens, error nuevo, caída. 17 tests de reglas con `promtool` |
| Alerta "cold starts / throttles" | `ai-agent-lambda-throttles` en CloudWatch | |
| Notificaciones | `monitoring/alertmanager/` | Discord con `webhook_url_file`; receptor local para pruebas |
| Calidad degradada (avg_score < 0.7 en 1 h) | gauge `agent_quality_score` alimentado por el informe de evaluación + `llm-eval-nightly.yml` | Monitorización semántica |
