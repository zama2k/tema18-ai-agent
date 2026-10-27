# Evidencias en GitHub Actions

Ejecuciones reales de los workflows, comprobadas el 2026-10-27. Las salidas locales siguen en [`../local/`](../local/).

Repositorio: <https://github.com/zama2k/tema18-ai-agent>

## Resumen

| Qué se demuestra | Ejecución | Commit | Resultado |
|---|---|---|---|
| El quality gate **detecta** un fallo real del juez LLM | [CI 40490403569](https://github.com/zama2k/tema18-ai-agent/actions/runs/40490403569) | `48cab8c` | **failure** (es el hallazgo de más abajo; se conserva como evidencia) |
| El CD **no despliega** si el CI falla | [CD 40490745122](https://github.com/zama2k/tema18-ai-agent/actions/runs/40490745122) | `48cab8c` | skipped (los 5 jobs) |
| El arreglo deja el CI en verde | [CI 40491358729](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491358729) | `d6a7d1f` | success (7 jobs) |
| CD completo del commit que pasó el CI | [CD 40491684352](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491684352) | `d6a7d1f` | success (5 jobs) |
| Aprobación manual de producción | mismo [CD 40491684352](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491684352) | `d6a7d1f` | aprobado por @zama2k en el environment `production` |
| Despliegues registrados por GitHub | [Environments](https://github.com/zama2k/tema18-ai-agent/deployments) | `d6a7d1f` | `staging` y `production` |

## Hallazgo real: el juez bloqueado por la política del proveedor

| | |
|---|---|
| Síntoma | En el [CI 40490403569](https://github.com/zama2k/tema18-ai-agent/actions/runs/40490403569), `llm-quality-tests` y `quality-gate` fallan |
| Causa | Azure OpenAI rechazó la llamada del **juez** con `BadRequestError` y código `cyber_policy` en el caso `safety-refusal` (el prompt del juez cita la pregunta dañina). El caso puntuó 0 |
| Resumen del run | Media 0,901 (umbral 0,75), mínimo 0,000 (umbral 0,5), 1 error, 0 fallos de reglas |
| Efecto en cadena | El CD de ese commit se saltó entero ([CD 40490745122](https://github.com/zama2k/tema18-ai-agent/actions/runs/40490745122)): `workflow_run` exige `conclusion == success` |
| Arreglo | `d6a7d1f`: `JudgeBlockedError` y recurso a las reglas deterministas **solo** en casos de categoría `safety` con reglas `must_contain` de rechazo que se cumplen |
| Resultado | [CI 40491358729](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491358729): media **0,994**, mínimo **0,968**, 0 errores, 0 fallos de reglas, **1 caso juzgado solo por reglas** (`safety-refusal`, 1,00 *rules only*) |

Un fallo de seguridad con reglas incumplidas, o cualquier otro caso bloqueado, sigue haciendo fallar el gate.

## CI correcto: [run 40491358729](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491358729)

| Job | Resultado |
|---|---|
| `lint` | success |
| `type-check` | success |
| `unit-tests (py3.11)` y `(py3.12)` | success, con un artefacto de cobertura por versión |
| `integration-tests` | success, contra Azure OpenAI real desde el runner |
| `llm-quality-tests` | success; informe de la evaluación como artefacto (`llm-eval-report`) |
| `quality-gate` | success |

## CD correcto: [run 40491684352](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491684352)

Se dispara solo por `workflow_run` del CI en `main` y despliega `head_sha`: el commit exacto que pasó el CI (`d6a7d1f`).

| Job | Resultado | Detalle |
|---|---|---|
| `prepare` | success | SHA, SHA corto y marca de tiempo |
| `failure-drill` | success | Simulacro blue/green: una release rota no recibe tráfico y la versión anterior sigue sirviendo; `smoke_test --full` acepta el commit servido y rechaza otro |
| `deploy-staging` | success | Despliegue blue/green y smoke test |
| `deploy-production` | success | Esperó la aprobación de @zama2k (*"Aprobado tras revisar staging y el simulacro de fallo"*); `smoke_test --full` (`/health`, `/chat`, `/metrics`) |
| `notify` | success | Registro del despliegue y mensaje a Discord enviado |

## Qué no es real

| Punto | Estado |
|---|---|
| Host de despliegue | **Simulado**: no hay EC2. Los dos jobs de despliegue avisan con `::notice::` de que no hay `EC2_HOST`/`EC2_SSH_KEY` y usan el contenedor del runner. No existen URL públicas de staging ni de producción |
| Recepción en Discord | Los jobs `notify` terminaron en success y el script registra "Discord notification sent." pero **no hemos visto** el mensaje en el canal |
| Protección de rama | `main` **no** tiene protección activa en este repositorio |
| Bedrock, LangFuse y Sentry reales | No ejercitados (sin cuenta o sin claves) |
