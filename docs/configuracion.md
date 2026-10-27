# Configuración

La configuración se carga con `pydantic-settings` (`src/config.py`) desde el entorno y, además, desde un fichero indicado por `ENV_FILE` (por defecto `.env`, ignorado por git). Así los tests y las ejecuciones locales pueden leer credenciales de fuera del repositorio sin copiarlas:

```bash
ENV_FILE=/ruta/a/mi.env python -m evals.run_eval
```

Una línea `CLAVE=` vacía se trata como no definida. Las claves son `SecretStr` y nunca aparecen en `repr`, logs ni mensajes de error de configuración.

## Variables

| Variable | Por defecto | Descripción |
|---|---|---|
| `LLM_PROVIDER` | `azure` | `azure`, `bedrock` o `mock` (determinista, sin secretos; lo usan el host simulado y el stack de monitorización) |
| `AZURE_OPENAI_ENDPOINT` | | Endpoint compatible con OpenAI (`https://<recurso>.openai.azure.com/openai/v1`). Obligatorio con `azure` |
| `AZURE_OPENAI_API_KEY` | | Clave. Obligatoria con `azure` |
| `CHAT_MODEL` | `gpt-4o-mini` | Despliegue del agente |
| `JUDGE_MODEL` | `gpt-5.6-sol` | Despliegue del juez (modelo de razonamiento) |
| `TAVILY_API_KEY` | | Activa la búsqueda web |
| `KB_ID` | | Knowledge Base de Bedrock para `RAGTool`; sin él se usa el corpus local |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-haiku-20240307-v1:0` | Solo con `LLM_PROVIDER=bedrock` |
| `AWS_DEFAULT_REGION` | `us-east-1` | Región de Bedrock |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | `https://cloud.langfuse.com` (host) | Activan LangFuse si están las dos claves |
| `SENTRY_DSN` | | Activa Sentry |
| `QUALITY_REPORT_FILE` | | `latest.json` del informe de evaluación; alimenta `agent_quality_score` |
| `MAX_ITERATIONS` / `DEEP_RESEARCH_MAX_ITERATIONS` | `5` / `8` | Tope del bucle de herramientas |
| `LLM_TIMEOUT_SECONDS` / `LLM_TOTAL_TIMEOUT_SECONDS` | `45` / `120` | Por intento HTTP / total con reintentos |
| `TOOL_TIMEOUT_SECONDS` | `15` | Por herramienta |
| `MAX_OUTPUT_TOKENS` | `1024` | Tokens visibles de salida |
| `PRICE_OVERRIDES` | | JSON `{"modelo": [entrada, salida]}` en USD por millón de tokens |
| `EVAL_MEAN_THRESHOLD` / `EVAL_MIN_THRESHOLD` | `0.75` / `0.5` | Umbrales del quality gate |
| `APP_ENV`, `LOG_LEVEL` | `local`, `INFO` | Entorno (aparece en `/health`) y nivel de logs |
| `GIT_SHA`, `DEPLOY_COLOR` | | Los fija el despliegue (`slots/<color>.env`) |

## Plantilla `.env.example`

La plantilla está en `.env.example` (raíz del repositorio). Cópiala a `.env` (ignorado por git) y rellénala; no lleva valores reales.

```dotenv
# --- LLM provider: azure (default) | bedrock | mock ---
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/openai/v1
AZURE_OPENAI_API_KEY=
CHAT_MODEL=gpt-4o-mini
JUDGE_MODEL=gpt-5.6-sol

# --- Optional tools ---
TAVILY_API_KEY=
KB_ID=
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0

# --- Optional observability (no-op when empty) ---
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
SENTRY_DSN=
QUALITY_REPORT_FILE=

# --- Runtime ---
APP_ENV=local
LOG_LEVEL=INFO
```
