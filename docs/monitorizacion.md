# Monitorización: las tres capas

```
 CAPA 1  Infraestructura y servicio   Prometheus + Alertmanager + Grafana   (CloudWatch: alarms.json)
         "¿El servicio está sano?"     /metrics, logs JSON con request_id
 CAPA 2  Observabilidad del LLM        LangFuse (opcional)
         "¿El agente responde bien?"   trazas, generaciones, tokens, coste, puntuaciones
 CAPA 3  Seguimiento de errores        Sentry (opcional)
         "¿Qué se rompió y por qué?"   excepciones con stack trace, sin PII
```

LangFuse y Sentry se activan **solo** si hay claves (`LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`, `SENTRY_DSN`). Sin claves son no-op.

## Capa 1: métricas (`GET /metrics`)

| Métrica | Tipo | Etiquetas |
|---|---|---|
| `agent_http_requests_total` | counter | `method`, `route` (plantilla), `status` |
| `agent_http_request_duration_seconds` | histograma | `route` |
| `agent_requests_total` | counter | `agent_route`, `outcome` |
| `agent_errors_total` | counter | `error_type` |
| `agent_tokens_total` | counter | `direction` (input/output) |
| `agent_cost_usd_total` | counter | |
| `agent_tool_calls_total` | counter | `tool`, `outcome` |
| `agent_quality_score` | gauge | media del último informe de evaluación (`QUALITY_REPORT_FILE`) |
| `agent_build_info` | gauge | `version`, `commit`, `color` |

## Matriz de alertas (`monitoring/prometheus/alert_rules.yml`)

| Alerta | Condición | Severidad | Herramienta del curso |
|---|---|---|---|
| `AgentDown` | `up == 0` durante 1 min | critical | CloudWatch |
| `HighErrorRate` | > 5 % de `/chat` con 5xx en 5 min (y tráfico > 0,01 req/s) | critical | CloudWatch + Sentry |
| `HighLatencyP95` | P95 de `/chat` > 15 s durante 10 min | warning | CloudWatch |
| `DailyCostHigh` | > 50 USD en 24 h | warning | LangFuse + facturación |
| `QualityDegraded` | media del juez < 0,7 en 1 h | warning | LangFuse scores |
| `TokenUsageSpike` | tokens en 1 h > 2 x la media horaria del día | warning | LangFuse |
| `NewErrorTypeSeen` | `error_type` no visto en 7 días | info | Sentry |

Las 7 reglas tienen anotaciones y un enlace al [runbook](runbook.md). Los umbrales son puntos de partida: ajústalos con dos semanas de datos reales.

`QualityDegraded` es monitorización **semántica**: un servicio puede tener latencia y errores perfectos mientras sus respuestas empeoran (cambio de modelo, deriva del prompt). El workflow `llm-eval-nightly.yml` evalúa cada día el modelo real con el juez y avisa por Discord; el informe `latest.json` también puede montarse en el agente para alimentar el gauge.

## Probar las reglas y el circuito de alertas

```bash
# Reglas: sintaxis y 14 tests unitarios con promtool (sin levantar nada)
docker run --rm -v "$PWD/monitoring/prometheus:/p:ro" --entrypoint promtool prom/prometheus:v3.15.1 test rules /p/tests/alert_rules_test.yml

# De extremo a extremo (agente + Prometheus + Alertmanager + Grafana + receptor): ~1 min
bash monitoring/test_alerts_local.sh
```

`test_alerts_local.sh` genera peticiones con el fallo forzado `__fail__` (el proveedor `mock` lanza un error, la API responde 502), espera a que `HighErrorRate` pase a `firing` en Prometheus, y comprueba que Alertmanager entrega la notificación tanto como webhook genérico como con el formato de Discord al receptor local. Guarda los payloads en `evidencias/local/`. Para que el test termine en segundos usa una copia de las reglas con `for` de 10 s; las expresiones son las mismas.

Puertos del test: agente 8300, Prometheus 9390, Alertmanager 9393, Grafana 3390, receptor 9399.

## Producción: Discord

`monitoring/alertmanager/alertmanager.yml` envía a Discord con `discord_configs` y `webhook_url_file`:

```bash
mkdir -p monitoring/secrets                      # ignorado por git
printf '%s' "$DISCORD_WEBHOOK_URL" > monitoring/secrets/discord_webhook_url
GRAFANA_ADMIN_PASSWORD='elige-una' docker compose -f monitoring/docker-compose.monitoring.yml up -d --build
```

No hay contraseña de Grafana por defecto: el stack no arranca sin `GRAFANA_ADMIN_PASSWORD`.

## CloudWatch (`monitoring/cloudwatch/alarms.json`)

Cinco alarmas de Lambda de la tabla del curso: `Invocations` > 10.000/h, `Errors` > 5 % (como expresión `100 * errors / invocations`, no como recuento), `Duration` p95 > 10 s, `Throttles` > 0 y `ConcurrentExecutions` > 800. `tests/unit/test_cloudwatch_alarms.py` comprueba umbrales y valida cada alarma como una petición `PutMetricAlarm` real (botocore valida los parámetros en cliente).

Aplicar (no ejecutado, sin cuenta de AWS):

```bash
python -c "import json,sys; [print(json.dumps(a)) for a in json.load(open('monitoring/cloudwatch/alarms.json'))['Alarms']]" |
  while read -r alarm; do aws cloudwatch put-metric-alarm --cli-input-json "$alarm"; done
```

Sustituye antes `${ALERT_TOPIC_ARN}` por el ARN de un tema SNS y `FunctionName` por tu función.

## LangFuse y Sentry

- **LangFuse**: cada consulta abre una traza (`process_query`) y cada llamada al modelo una generación con modelo, tokens y coste en USD. Los scores se añaden con `Tracer.score`. Implementado con el SDK vigente (`start_as_current_observation`, `update(usage_details=..., cost_details=...)`; ver [fuentes](fuentes.md)). Probado con mocks; no contra un servidor LangFuse real.
- **Sentry**: sin `SENTRY_DSN` no se inicializa. Con DSN, no se envía PII: ni cuerpo de petición (los mensajes del chat), ni cookies, ni query string, ni cabeceras `Authorization`/`X-API-Key`.
