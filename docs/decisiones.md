# Decisiones de diseño

Cada decisión sigue el mismo esquema: qué se eligió, por qué y qué se pierde.

## 1. OpenAI SDK sin LangChain

- **Elección:** el bucle de tool calling está escrito sobre el SDK de OpenAI (`src/agent.py`, unas 60 líneas útiles).
- **Por qué:** el tool calling es una función nativa de la API; LangChain solo añadiría un árbol de dependencias grande y una capa que oculta justo lo que hay que controlar y probar (límite de iteraciones, timeouts, coste por llamada). El enunciado dice "OpenAI/LangChain": se cumple la parte OpenAI.
- **Coste:** si en el futuro se necesitan cadenas complejas, loaders o memorias, habrá que escribirlos o adoptar la librería entonces.

## 2. Azure OpenAI como LLM real y Bedrock solo con Stubber

- El alumno no tiene cuenta de AWS y Haiku 3 de Anthropic está fuera de servicio en Bedrock (us-east-1) desde el 10-sep-2026.
- `BedrockProvider` y `parse_bedrock_response` están implementados y se prueban con `botocore.stub.Stubber`: demuestran el formato y el manejo de errores, **no** que funcionen contra AWS.
- Los despliegues usados son `gpt-4o-mini` (agente) y `gpt-5.6-sol` (juez, modelo de razonamiento: sin `temperature`, `top_p`, `stop` ni `max_tokens`; se usa `max_completion_tokens` con 4000 tokens de margen porque el razonamiento consume del mismo cupo).

## 3. Juez distinto del modelo evaluado y meta-test del juez

- gpt-4o-mini juzgándose a sí mismo es indulgente. El juez es un modelo de razonamiento más capaz y distinto.
- La puntuación global **no** la da el juez: devuelve cuatro criterios (corrección, completitud, idioma, seguridad) con esquema JSON estricto y el código los pondera (0,50 / 0,20 / 0,15 / 0,15).
- Antes de usar al juez se comprueba que sirve: una respuesta deliberadamente errónea ("la capital de Francia es Madrid") debe puntuar por debajo del umbral y con corrección < 0,3. Si no, la evaluación completa se rechaza (`judge_selftest`).
- Quality gate: media >= 0,75 y mínimo por caso >= 0,5 (variables `EVAL_MEAN_THRESHOLD` y `EVAL_MIN_THRESHOLD`).

## 4. Un contenido de despliegue por commit: `git archive`, no `git pull`

- El enunciado manda hacer "pull del último código de main". Eso despliega algo distinto de lo que pasó el CI si `main` se movió mientras tanto.
- `cd.yml` usa `github.event.workflow_run.head_sha`, empaqueta ese commit con `git archive` y lo sube por SSH. El host no necesita acceso a GitHub (sirve también para repos privados) y cada release vive en `releases/<sha>` con su virtualenv.
- El health check exige que `/health` devuelva **ese** `commit`: así no se da por bueno un proceso viejo que sigue vivo.

## 5. `StrictHostKeyChecking=yes` con `known_hosts` fijado

- El enunciado pide `StrictHostKeyChecking=no`. Eso deja pasar a cualquiera que intercepte la conexión, y la clave de despliegue es lo más valioso del host.
- Con host real: `EC2_KNOWN_HOSTS` (secreto con las claves del host) o, si falta, `ssh-keyscan` con aviso explícito de que es "confiar en el primer uso" solo para esa ejecución.
- Con host simulado: la clave del host se lee **del propio contenedor** (`docker exec`), sin confianza en el primer uso.

## 6. Host simulado en lugar de EC2

- Sin EC2, el despliegue por SSH habría quedado como un `echo`. El contenedor `deploy/host/` (Debian + sshd + nginx + git + venv + `agentctl`) hace que **los mismos scripts** se ejecuten de verdad: SSH, nginx real, venvs reales, procesos reales.
- `agentctl` imita `systemctl restart|stop|is-active agent-blue|agent-green`; los scripts toman el comando de `SERVICE_CTL` (por defecto `sudo systemctl`), de modo que en un EC2 real no cambia nada.
- Límite: no hay systemd, ni el firewall/security group, ni la latencia de una máquina real.

## 7. Blue/green: el viejo se queda encendido

- Tras el cambio, el slot anterior sigue corriendo como reserva en caliente. `rollback.sh` solo tiene que reapuntar nginx (segundos) y, si el reserva murió, lo reinicia desde su release.
- El cambio de nginx se verifica **a través de nginx**: la recarga es asíncrona, así que se consulta con reintentos hasta ver el commit nuevo (esto lo descubrió el test de integración: la primera versión comprobaba demasiado pronto y revertía un despliegue sano).
- El `sed` reescribe el fichero en el sitio (sin renombrar) porque el usuario de despliegue puede ser dueño de `agent.conf` sin serlo de `/etc/nginx/conf.d`.

## 8. Despliegues sin cancelación, CI con cancelación

- `ci.yml` cancela ejecuciones antiguas del mismo PR o rama (`cancel-in-progress: true`).
- `cd.yml` usa un grupo por entorno con `cancel-in-progress: false`. La semántica real de GitHub es: por grupo hay **un job en curso y como mucho uno pendiente**. El job en curso nunca se cancela a medias. Si llega un tercero mientras hay uno pendiente, **el pendiente se cancela y gana el más reciente**. Consecuencia: un commit intermedio puede no llegar a desplegarse nunca, lo cual es aceptable porque cada despliegue lleva un commit completo y autocontenido.

## 9. Permisos y cadena de suministro

- `permissions: contents: read` en todos los workflows; ningún job necesita más.
- Acciones fijadas por SHA completo con el tag en un comentario. Versiones comprobadas con `git ls-remote --tags` el 2026-10-27: `actions/checkout` v7.0.1, `actions/setup-python` v7.1.0, `actions/upload-artifact` v7.0.2.
- Ningún dato controlable por usuarios se interpola en `run:`; todo pasa por `env:`. `actionlint` (que incluye shellcheck) sale limpio.
- Los secretos solo se inyectan en el paso que los usa.

## 10. Falsos verdes prohibidos

- Si faltan las credenciales en CI, los tests de integración **fallan** (`REQUIRE_LIVE_LLM=1`) en lugar de saltarse. En local se saltan con el motivo.
- `quality-gate` falla si un job obligatorio quedó en `skipped` en `main`.
- Un conjunto de datos vacío o corrupto hace fallar la evaluación (esto lo detectó un test unitario: el cálculo del mínimo reventaba con lista vacía).

## 11. Observabilidad

- Logs JSON con `request_id` (contextvar, también en la cabecera `X-Request-ID`, validada para evitar inyección de logs).
- Métricas con etiqueta `route` tomada de la **plantilla** de la ruta (`/chat`), no de la URL: una ráfaga de 404 no crea series infinitas.
- Sentry con `data_collection` desactivado y `before_send` que elimina cuerpo, cookies, query y cabeceras sensibles (defensa en profundidad). Los mensajes del chat pueden contener datos personales y no se envían.
- LangFuse (SDK actual, API de observaciones de la v3 en adelante) recibe trazas, generaciones, uso de tokens y coste. Cualquier fallo de LangFuse o Sentry se registra y se traga: la observabilidad nunca rompe una petición.
- El filtro de contenido de Azure (HTTP 400 `content_filter`) se distingue de un fallo del proveedor y devuelve 422, no 502: no es una caída ni debe disparar la alerta de errores.

## 12. Dependencias: hallazgos

- `openai>=3` usa `httpx2`, no `httpx`: `respx` no puede interceptar sus peticiones. Los tests inyectan un `httpx2.MockTransport` como cliente HTTP.
- `sentry-sdk` 2.7x marca `send_default_pii` como obsoleto; se usa `data_collection`.
- Solo las dependencias directas están fijadas con `==`; las transitivas las resuelve pip. Si se quiere reproducibilidad total, genera un `constraints.txt` con `pip freeze`.

## En qué mejora a lo habitual

| Habitual | Aquí |
|---|---|
| CD que hace checkout de `main` | CD sobre el `head_sha` que pasó el CI y health check del commit servido |
| `StrictHostKeyChecking=no` | clave de host fijada |
| Despliegue simulado con `echo` | despliegue SSH real contra un host simulado, con test de cero caídas y de fallo |
| "Tests de LLM" que solo miran palabras clave | reglas deterministas + juez con esquema estricto + meta-test del juez + umbrales de calidad |
| Alertas sin probar | 14 tests de reglas con `promtool` y una alerta disparada y recibida de verdad |
| Cobertura sin umbral | `--cov-fail-under=80` (medido: 98,9 %) |
