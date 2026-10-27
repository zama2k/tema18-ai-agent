# Runbook

Cada alerta de `alert_rules.yml` enlaza aquí. Primer vistazo siempre: dashboard "AI Agent overview" en Grafana y `GET /health` (versión, commit y color activos).

## Agent down
1. `curl http://<host>/health`. Si responde, el problema es de Prometheus (objetivo mal configurado).
2. En el host: `cat /opt/agent/current_port` y el estado del servicio activo (`systemctl status agent-blue|agent-green`).
3. Si el último despliegue es sospechoso: haz rollback con `deploy/remote_deploy.sh --action rollback --host ... --key ... --known-hosts ...` (sube y ejecuta `rollback.sh` en el host).

## High error rate
1. Mira `agent_errors_total` por `error_type` en Grafana ("Errors by type").
2. `provider`/`upstream_error`: caída o cuota del proveedor del LLM. `timeout`: latencia del proveedor. `internal_error`: bug; busca el stack trace en Sentry.
3. Si empezó con un despliegue, haz rollback.

## High latency
1. Panel "/chat latency" (p50 frente a p95). Un p95 alto con p50 normal suele ser el bucle de herramientas.
2. Revisa `agent_tool_calls_total` y las generaciones largas en LangFuse.
3. Baja `MAX_ITERATIONS` o `TOOL_TIMEOUT_SECONDS` si el bucle se dispara.

## Daily cost
1. Panel "Cost per hour" y tokens por dirección. Compáralo con el tráfico (`Requests per second`).
2. Un coste alto con tráfico normal indica prompts o respuestas largas, o bucles de herramientas.
3. Si hay abuso, limita en el proxy; si es legítimo, ajusta el umbral.

## Quality degraded
1. Abre el último informe (`evals/reports/eval-report.md` o el artefacto `llm-eval-nightly-report`) y localiza los casos con menor puntuación.
2. Causas típicas: cambio de versión del modelo, cambio del prompt del sistema, herramienta rota.
3. No vuelvas a desplegar hasta que `python -m evals.run_eval` pase el umbral.

## Token spike
1. Posible inyección de prompt o bucle de herramientas. Mira las trazas más caras en LangFuse.
2. Comprueba si las peticiones repiten el mismo origen y limita en el proxy si procede.

## New error type
1. Busca el tipo en Sentry (agrupa por causa) y decide si es un bug nuevo o un efecto de un cambio reciente.
2. Si es esperado (por ejemplo `content_filtered`), no requiere acción.
