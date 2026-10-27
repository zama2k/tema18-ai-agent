# Fuentes consultadas

Páginas oficiales leídas antes de escribir cada cliente. Fecha de consulta: 2026-10-27.

| Tema | URL | Qué se tomó |
|---|---|---|
| LangFuse, instrumentación del SDK de Python | https://langfuse.com/docs/observability/sdk/instrumentation | `start_as_current_observation(as_type="generation", model=...)`, `update(usage_details=...)`, `flush()` |
| LangFuse, guía de migración de SDK | https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4 | El SDK vigente es OpenTelemetry-nativo (v3 en adelante); versión instalada y probada: 5.0.4 |
| LangFuse, firma real del SDK instalado | `inspect.signature` de `Langfuse.create_score`, `score_current_trace` y `start_as_current_observation` | `cost_details`, `usage_details`, variables `LANGFUSE_*` |
| Discord, Execute Webhook | https://docs.discord.com/developers/resources/webhook | Cuerpo JSON (`content` <= 2000 caracteres, `allowed_mentions`), `204 No Content` |
| Precios de OpenAI | https://developers.openai.com/api/docs/pricing | gpt-4o-mini 0,15 / 0,60 USD por millón; gpt-5.6-sol 4,00 / 20,00 (tarifa de OpenAI, Azure puede facturar distinto) |
| Precios de Haiku 3 de Anthropic | tarifa publicada de Anthropic (0,25 / 1,25 USD por millón) | La página de precios de Bedrock no se pudo leer entera; no re-verificado en Bedrock |
| Sentry Python, recogida de datos | tipos `DataCollectionUserOptions` del SDK instalado (2.73.0) | `data_collection` sustituye a `send_default_pii` |
| Prometheus, Alertmanager, Grafana | imágenes `prom/prometheus:v3.15.1`, `prom/alertmanager:v0.34.1`, `grafana/grafana:13.2.4` | `promtool test rules`, `amtool check-config`, `discord_configs` con `webhook_url_file` |
| Acciones de GitHub | `git ls-remote --tags` sobre `actions/checkout`, `actions/setup-python`, `actions/upload-artifact` | SHA de cada tag (los tags son ligeros: el SHA es el del commit) |
