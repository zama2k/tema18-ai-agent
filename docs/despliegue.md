# Despliegue blue/green

## Cómo funciona

```
              nginx :80  --proxy_pass-->  127.0.0.1:8000  (blue)   o   127.0.0.1:8001  (green)
                                                   ^                         ^
   /opt/agent/current_port  ---- dice cuál está activo -----------------------+
   /opt/agent/slots/blue  -> releases/<sha-A>      (symlink por slot)
   /opt/agent/slots/green -> releases/<sha-B>
   /opt/agent/releases/<sha>/{src, requirements.txt, VERSION, .venv}
```

Pasos de `deploy/blue_green_deploy.sh` (se ejecuta **en el host**):

1. Toma un candado (`flock`): solo un despliegue a la vez.
2. Lee `current_port` y elige el slot **inactivo** (primera vez: blue).
3. Descomprime el release en `releases/<sha>`, crea su `.venv` e instala `requirements.txt`.
4. Apunta el slot inactivo al release, escribe `slots/<color>.env` (`GIT_SHA`, `DEPLOY_COLOR`) y reinicia solo ese servicio.
5. Health check: hasta 30 intentos a `http://127.0.0.1:<puerto>/health`; debe responder `status: ok` **y** `commit == <sha>`. Si no, para el servicio y sale con error; el slot activo no se ha tocado.
6. `sed` sobre la línea `proxy_pass`, `nginx -t` y recarga (si `nginx -t` falla se restaura el fichero).
7. Comprueba a través de nginx que ya sirve el commit nuevo; si no, revierte.
8. Escribe `current_port` (atómico) y `previous_port`. El slot viejo sigue encendido como reserva.
9. Conserva los 5 releases más recientes.

`deploy/rollback.sh`: reapunta nginx al slot reserva (reiniciándolo antes si estaba caído), verifica y actualiza `current_port`.

## Quién ejecuta qué

| Script | Se ejecuta en | Función |
|---|---|---|
| `deploy/run_deployment.sh` | runner de CI | Orquesta un entorno: release, despliegue, verificación y rollback si falla |
| `deploy/remote_deploy.sh` | runner de CI | `scp` + `ssh` con `StrictHostKeyChecking=yes` |
| `deploy/make_release.sh` | runner de CI | `git archive` del commit exacto |
| `deploy/smoke_test.sh` | runner de CI | `POST /chat` (staging) o `/health` + `/chat` + `/metrics` (`--full`, producción) |
| `deploy/blue_green_deploy.sh`, `rollback.sh`, `lib.sh` | host | Lógica blue/green |
| `deploy/sim_host.sh`, `deploy/host/` | runner / tu ordenador | Host simulado |
| `deploy/test_blue_green_local.sh` | runner / tu ordenador | Prueba de extremo a extremo |

## Probarlo en local (Docker)

```bash
bash deploy/test_blue_green_local.sh     # v1 -> v2 -> rollback -> v2 -> v3 rota; ~70 s
DEPLOY_ENVIRONMENT=production RELEASE_SHA=$(git rev-parse HEAD) bash deploy/run_deployment.sh
```

Puertos por defecto: SSH 2322 y nginx 8380 (`T18_HOST_SSH_PORT`, `T18_NGINX_PORT`). El test verifica:

- el primer despliegue va a blue (8000) y nginx sirve ese commit;
- v2 va a green (8001) **bajo carga continua** (una petición cada 100 ms a través de nginx) sin ninguna respuesta distinta de 200;
- blue sigue vivo con v1 como reserva;
- `rollback.sh` devuelve el tráfico a v1;
- una v3 que revienta al arrancar nunca recibe tráfico: el script sale con error, nginx sigue sirviendo v2, `current_port` no cambia y el slot roto queda parado;
- `smoke_test.sh --full` acepta el commit servido y rechaza cualquier otro.

## Usar un EC2 real

Requisitos del host (Amazon Linux o Debian/Ubuntu):

```bash
sudo useradd --create-home --shell /bin/bash deploy
sudo mkdir -p /opt/agent /etc/agent && sudo chown deploy:deploy /opt/agent
# nginx: copia deploy/nginx/agent.conf a /etc/nginx/conf.d/ y hazlo propiedad de deploy
sudo chown deploy:deploy /etc/nginx/conf.d/agent.conf
# systemd: copia deploy/systemd/agent-blue.service y agent-green.service a /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable agent-blue agent-green
# variables comunes de la aplicación (claves reales del LLM, etc.)
sudoedit /etc/agent/agent.env          # LLM_PROVIDER=azure, AZURE_OPENAI_*, APP_ENV=production...
# permisos mínimos para el usuario de despliegue
echo 'deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart agent-*, /usr/bin/systemctl stop agent-*, /usr/bin/systemctl reload nginx, /usr/sbin/nginx -t' | sudo tee /etc/sudoers.d/deploy
```

Después, en GitHub, crea los secretos de `staging` y `production` descritos en el README. En cuanto existan `EC2_HOST` y `EC2_SSH_KEY`, el mismo `cd.yml` despliega a ese host en lugar del simulado.

> No verificado: este camino (host real) no se ha ejecutado, porque no hay EC2. Lo único que difiere del host simulado es `SERVICE_CTL` (`sudo systemctl`) y la recarga de nginx (`sudo systemctl reload nginx`), que son los valores por defecto de `deploy/lib.sh`.

## Modo simulado en el CD

Sin `EC2_HOST`/`EC2_SSH_KEY`, `run_deployment.sh` levanta el contenedor en el runner, instala primero una versión base (para que el cambio sea un cambio real y no un primer despliegue) y despliega después el SHA que pasó el CI. Lo avisa con `::notice::`. El job `failure-drill` ejecuta además el test completo de arriba contra ese mismo SHA.

Así se ejecutó el [CD 40491684352](https://github.com/zama2k/tema18-ai-agent/actions/runs/40491684352) de GitHub: staging y producción se desplegaron en el host simulado del runner (con `::notice::` en ambos jobs), no en un EC2. Ver [evidencias/github](../evidencias/github/README.md).
