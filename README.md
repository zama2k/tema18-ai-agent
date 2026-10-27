# tema18-ai-agent

[![AI Agent CI](https://github.com/__GH_OWNER__/tema18-ai-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/__GH_OWNER__/tema18-ai-agent/actions/workflows/ci.yml)
[![AI Agent CD](https://github.com/__GH_OWNER__/tema18-ai-agent/actions/workflows/cd.yml/badge.svg)](https://github.com/__GH_OWNER__/tema18-ai-agent/actions/workflows/cd.yml)
[![LLM Eval Nightly](https://github.com/__GH_OWNER__/tema18-ai-agent/actions/workflows/llm-eval-nightly.yml/badge.svg)](https://github.com/__GH_OWNER__/tema18-ai-agent/actions/workflows/llm-eval-nightly.yml)

Un agente de IA en Python con **CI/CD completo, tests de calidad del LLM, despliegue blue/green por SSH y monitorización en tres capas**. Ejercicio 2 del Tema 18 (CI/CD + Monitoring en producción).

## Nota para la corrección

Qué está comprobado y qué no, para no tener que adivinarlo:

| Comprobado en local (hay evidencia en [`evidencias/local/`](evidencias/local/)) | Aún por comprobar |
|---|---|
| 251 tests unitarios, 98,9 % de cobertura (Python 3.11 y 3.12), `ruff`, `mypy` | Las ejecuciones de **GitHub Actions** y las insignias: se añadirán al publicar el repositorio |
| 25 tests de integración y la evaluación con juez **contra Azure OpenAI real** | El **CD en GitHub**: los workflows pasan `actionlint` y sus scripts se ejecutan en local, pero no se han lanzado en un runner |
| Despliegue blue/green real por SSH contra un host simulado: cambio sin una sola petición fallida, rollback y versión rota rechazada | El despliegue contra un **EC2 real** (no hay cuenta de AWS) |
| Alerta `HighErrorRate` disparada con errores reales y recibida en el receptor local; 17 tests de reglas con `promtool` | **Bedrock** real: el código existe y se prueba con `botocore.stub.Stubber`, no contra AWS. Haiku 3 de Anthropic está fuera de servicio en Bedrock desde el 10-sep-2026 |
| `actionlint` y `shellcheck` sin avisos | **LangFuse** y **Sentry** reales: probados con mocks (no hay claves). **Discord** real: probado contra un receptor local |
| | Las alarmas de **CloudWatch**: validadas como peticiones `PutMetricAlarm`, no aplicadas |
| | Las URL públicas de staging y producción: se añadirán tras publicar |

La plantilla de variables está en [docs/configuracion.md](docs/configuracion.md); el fichero `.env.example` se añadirá antes de publicar.

## Qué hace

```
Cliente --> nginx :80 --proxy_pass--> [blue :8000 | green :8001]   FastAPI: /health  /chat  /metrics
                                         |
                    AIAgent (src/agent.py)  bucle de tool calling, límites, timeouts, coste
                       |-- providers.py   Azure OpenAI (por defecto) | Bedrock | Mock
                       |-- tools.py       Tavily (búsqueda web) | RAG (Bedrock KB o corpus local)
                       '-- observability  logs JSON | métricas Prometheus | LangFuse | Sentry

CI  : lint ‖ type-check --> unit-tests (3.11, 3.12) --> integration-tests ‖ llm-quality-tests --> quality-gate
CD  : CI ok en main --> failure-drill ‖ staging --> producción (aprobación) --> notificación
Mon.: agente --/metrics--> Prometheus --reglas--> Alertmanager --> Discord ;  Grafana
```

## Ejecutar en local

Requisitos: Python 3.11 o 3.12; Docker solo para despliegue y monitorización.

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

ruff check . && ruff format --check .                     # lint y formato
mypy src/                                                 # tipos
pytest tests/unit -v --cov=src --cov-fail-under=80        # unitarios: sin red, ~5 s

# API con el proveedor simulado (sin claves)
LLM_PROVIDER=mock uvicorn src.api:create_app --factory --port 8300
curl -s localhost:8300/chat -H 'Content-Type: application/json' -d '{"message": "¿Capital de Francia?"}'
```

Con credenciales reales (se leen de `ENV_FILE`, por defecto `.env`; ver [configuración](docs/configuracion.md)):

```bash
ENV_FILE=/ruta/mi.env pytest tests/integration -m integration -x --tb=long -v --timeout=60
ENV_FILE=/ruta/mi.env python -m evals.run_eval            # evaluación con juez; informe en evals/reports/
```

Despliegue y monitorización (Docker):

```bash
bash deploy/test_blue_green_local.sh      # blue/green de extremo a extremo (~70 s)
bash monitoring/test_alerts_local.sh      # stack completo y alerta real (~1 min)
```

## El pipeline

### `ci.yml` (nombre: *AI Agent CI*)

| Job | Cuándo | Qué hace |
|---|---|---|
| `lint` | siempre | `ruff check` y `ruff format --check` |
| `type-check` | siempre | `mypy src/` |
| `unit-tests` | tras los dos anteriores; Python 3.11 y 3.12 | `pytest tests/unit --cov-fail-under=80`, resumen de cobertura y artefacto por versión |
| `integration-tests` | solo `push` a `main` | `tests/integration/test_api.py` contra Azure real (10 min máx.) |
| `llm-quality-tests` | solo `push` a `main` | `test_llm_quality.py` con `-x` y `--timeout=60`, y después la **evaluación con juez**; informe en el *step summary* y como artefacto |
| `quality-gate` | siempre (`if: always()`) | tabla resumen; falla si lint, tipos o unitarios fallan, o si los de LLM fallan o no se ejecutaron en `main`. En un PR, *skipped* es lo correcto |

Los secretos de LLM solo existen en el `env:` del paso que los usa. Si faltan en `main`, el job **falla** (`REQUIRE_LIVE_LLM=1`) en vez de dar un falso verde.

### `cd.yml` (nombre: *AI Agent CD*)

Se dispara con `workflow_run` del CI en `main` y solo si `conclusion == success`. Despliega `github.event.workflow_run.head_sha`: **el commit exacto que pasó el CI**, no "lo último de main".

| Job | Qué hace |
|---|---|
| `prepare` | SHA, SHA corto y marca de tiempo |
| `failure-drill` | Simulacro: despliega v1, v2, hace rollback y una v3 rota; comprueba que el tráfico no se interrumpe |
| `deploy-staging` | Release del commit, despliegue blue/green por SSH, smoke test `POST /chat` (valida el campo `response` con Python) |
| `deploy-production` | Entorno `production` (**aprobación manual**), mismo flujo y comprobación estricta de `/health` (commit servido), `/chat` y `/metrics`; si falla, `rollback.sh` |
| `notify` | Registro con SHA corto y fecha, resumen y mensaje a Discord si hay webhook |

Sin `EC2_HOST` y `EC2_SSH_KEY`, el mismo flujo despliega a un **host simulado** (contenedor con sshd + nginx + `agentctl`) y lo avisa con `::notice::`. Detalles en [docs/despliegue.md](docs/despliegue.md).

### `llm-eval-nightly.yml`

Cada día (y a demanda) evalúa el modelo real con el juez y avisa por Discord si la calidad cae bajo el umbral: **monitorización semántica**, porque la latencia y los errores pueden ser perfectos mientras las respuestas empeoran.

## Calidad del LLM

- **Tests deterministas** (`tests/unit/`): enrutado, prompt, parseo del formato Bedrock, coste, bucle del agente, API, observabilidad. Sin red: cualquier conexión no local falla.
- **Tests con el modelo real** (`tests/integration/`): conectividad, español, exactitud, concisión ("solo SÍ o NO"), latencia (< 10 s simples, < 30 s complejas) y regresión con `evals/dataset.jsonl` (12 casos con `must_contain` / `must_not_contain`).
- **LLM-as-a-judge** (`evals/`): `gpt-5.6-sol` puntúa con esquema JSON estricto cuatro criterios (corrección, completitud, idioma, seguridad); el código pondera. *Quality gate*: media >= 0,75, mínimo por caso >= 0,5 y cero incumplimientos de las reglas deterministas del conjunto de datos (también en la evaluación nocturna).
- **Meta-test del juez**: una respuesta deliberadamente errónea debe puntuar por debajo del umbral; si no, el juez no sirve y la evaluación falla.

Medido en la última ejecución local ([informe](evidencias/local/07b-eval-report.md)): media **0,985**, mínimo **0,934**, p50 de latencia 0,8 s, p95 3,3 s, coste estimado **0,05 USD** por evaluación completa; el juez dio 1,00 a la respuesta correcta y 0,46 a la errónea.

## Monitorización

Tres capas (detalle en [docs/monitorizacion.md](docs/monitorizacion.md)):

1. **Servicio:** `/metrics` (peticiones, latencia, errores, tokens, coste, calidad), logs JSON con `request_id`, Prometheus + Alertmanager + Grafana (`monitoring/`) y alarmas de CloudWatch para Lambda (`monitoring/cloudwatch/alarms.json`).
2. **LLM:** LangFuse (trazas, generaciones, tokens, coste; el texto se enmascara con `mask_pii` y la evaluación publica las puntuaciones del juez), solo con claves.
3. **Errores:** Sentry sin PII, solo con DSN.

Siete alertas (error rate > 5 %, P95 > 15 s, coste diario > 50 USD, calidad < 0,7, pico de tokens > 2x, error nuevo, caída) con [runbook](docs/runbook.md).

## Requisitos del enunciado y decisiones

- Tabla **requisito -> dónde se cumple**: [docs/enunciado.md](docs/enunciado.md).
- Decisiones de diseño y en qué mejora a lo habitual: [docs/decisiones.md](docs/decisiones.md). Las más importantes: OpenAI SDK sin LangChain, Azure OpenAI como LLM real, despliegue del commit exacto con `git archive`, `StrictHostKeyChecking=yes` con clave de host fijada, host simulado en lugar de EC2, juez con meta-test.
- Fuentes consultadas: [docs/fuentes.md](docs/fuentes.md).

## Configuración en GitHub

Esto lo tiene que crear el propietario del repositorio (nada de ello puede estar en el código):

| Nombre | Tipo / ámbito | Para qué |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | secreto de repositorio | Endpoint v1 de Azure OpenAI (CI de `main` y evaluación nocturna) |
| `AZURE_OPENAI_API_KEY` | secreto de repositorio | Clave del recurso |
| `TAVILY_API_KEY` | secreto de repositorio (opcional) | Prueba de conectividad con Tavily |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` | secretos de repositorio (opcionales) | Solo si se usa Bedrock |
| `CHAT_MODEL`, `JUDGE_MODEL` | variables de repositorio (opcionales) | Despliegues; por defecto `gpt-4o-mini` y `gpt-5.6-sol` |
| `EVAL_MEAN_THRESHOLD`, `EVAL_MIN_THRESHOLD` | variables de repositorio (opcionales) | Umbrales del juez (0,75 / 0,5) |
| `DISCORD_WEBHOOK_URL` | secreto de repositorio (opcional) | Notificaciones del CD y alertas nocturnas |
| Environment `staging` | environment | Sin protecciones |
| Environment `production` | environment | **Required reviewers**: el propio propietario |
| `EC2_HOST`, `EC2_SSH_KEY` | secretos de **environment** (`staging` y `production`, valores distintos) | Si existen, se despliega al host real; si no, al simulado |
| `EC2_USER`, `EC2_PORT`, `EC2_KNOWN_HOSTS` | secretos de environment (opcionales) | Usuario (`deploy`), puerto (22) y claves de host fijadas |
| `STAGING_URL` / `PROD_URL` | secreto del environment `staging` / `production` | URL pública para el smoke test (obligatoria con host real) |
| `STAGING_PUBLIC_URL` / `PROD_PUBLIC_URL` | variables de repositorio (opcionales) | Enlace que muestra GitHub en el despliegue; si faltan, se enlaza la página de la ejecución (la documentación oficial no define qué ocurre con una `url` vacía, así que no se deja vacía) |

Protección de la rama `main`: exige el CI (`quality-gate`) y 1 aprobación. Al haber una sola cuenta, el *merge* se hace con **bypass de administrador**; queda documentado aquí como decisión consciente.

## Limitaciones honestas

- El host simulado no es un EC2: no tiene systemd, grupos de seguridad ni la latencia de una máquina real. Lo único que cambia con un EC2 es `SERVICE_CTL` y la recarga de nginx, que por defecto ya son los de systemd; pero esa ruta no se ha ejecutado.
- Bedrock, LangFuse y Sentry reales no se han ejercitado (sin cuenta o sin claves).
- Las dependencias directas están fijadas con `==`; las transitivas las resuelve pip.
- Los precios son tarifas de lista a fecha 2026-10-27 y pueden variar (en Azure sobre todo): se pueden sustituir con `PRICE_OVERRIDES`.
- Azure filtra algunos prompts adversarios con un 400 de `content_filter`; el agente lo devuelve como 422 y no como caída. Por eso los casos de seguridad del conjunto de datos están redactados de forma que el filtro no los bloquee.
- Los umbrales de las alertas son puntos de partida, sin datos reales de producción que los respalden.
- Un conjunto de 12 casos es pequeño: detecta regresiones gruesas, no mide calidad estadística.

## Estructura

```
src/            agente, proveedores, herramientas, API, observabilidad, configuración, precios
tests/unit/     251 tests deterministas      tests/integration/  tests contra el modelo real
evals/          conjunto de datos dorado, juez y runner del quality gate
deploy/         scripts blue/green, host simulado (Dockerfile), unidades systemd, nginx
monitoring/     Prometheus, Alertmanager, Grafana, CloudWatch, receptor de pruebas, compose
docs/           enunciado, decisiones, despliegue, monitorización, runbook, configuración, fuentes
evidencias/local/  salidas reales de cada comprobación
.github/workflows/  ci.yml, cd.yml, llm-eval-nightly.yml
```
