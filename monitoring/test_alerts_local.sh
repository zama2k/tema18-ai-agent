#!/usr/bin/env bash
# End-to-end alert test: static checks (promtool, amtool), then the real stack. Errors generated
# against the API make HighErrorRate fire in Prometheus, Alertmanager routes it, and the notification
# arrives at the local webhook receiver (both the generic webhook and the Discord-shaped payload).
#
# Environment (defaults in brackets; the ports belong to this project's range):
#   AGENT_PORT [8300] PROMETHEUS_PORT [9390] ALERTMANAGER_PORT [9393] GRAFANA_PORT [3390]
#   WEBHOOK_PORT [9399]  EVIDENCE_DIR [evidencias/local]
set -euo pipefail

export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROJECT_NAME="t18ej2"
export AGENT_PORT="${AGENT_PORT:-8300}"
export PROMETHEUS_PORT="${PROMETHEUS_PORT:-9390}"
export ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9393}"
export GRAFANA_PORT="${GRAFANA_PORT:-3390}"
export WEBHOOK_PORT="${WEBHOOK_PORT:-9399}"
EVIDENCE_DIR="${EVIDENCE_DIR:-evidencias/local}"
PROM_IMAGE="prom/prometheus:v3.15.1"
AM_IMAGE="prom/alertmanager:v0.34.1"
COMPOSE=(docker compose -f monitoring/docker-compose.monitoring.yml --profile local)

PYTHON_BIN=python3
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python
mkdir -p "$EVIDENCE_DIR"

step() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
json() { "$PYTHON_BIN" -c "$1"; }

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf monitoring/.tmp
  if [ "${T18_REMOVE_IMAGE:-0}" = "1" ]; then docker rmi -f t18ej2-agent:local >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

step "1) Static checks: promtool (rules + unit tests) and amtool (both Alertmanager configs)"
docker run --rm -v "$(pwd -W 2>/dev/null || pwd)/monitoring/prometheus:/p:ro" --entrypoint promtool "$PROM_IMAGE" check rules /p/alert_rules.yml
docker run --rm -v "$(pwd -W 2>/dev/null || pwd)/monitoring/prometheus:/p:ro" --entrypoint promtool "$PROM_IMAGE" test rules /p/tests/alert_rules_test.yml
for cfg in alertmanager.yml alertmanager.local.yml; do
  docker run --rm -v "$(pwd -W 2>/dev/null || pwd)/monitoring/alertmanager:/c:ro" \
    -v "$(pwd -W 2>/dev/null || pwd)/monitoring/alertmanager/local/discord_webhook_url:/etc/alertmanager/secrets/discord_webhook_url:ro" \
    --entrypoint amtool "$AM_IMAGE" check-config "/c/$cfg"
done

step "2) Prepare fast rules (same expressions, short 'for' and scrape intervals)"
rm -rf monitoring/.tmp
mkdir -p monitoring/.tmp
cp -r monitoring/prometheus monitoring/.tmp/prometheus
"$PYTHON_BIN" - <<'PY'
from pathlib import Path

base = Path("monitoring/.tmp/prometheus")
rules = base / "alert_rules.yml"
rules.write_text(
    rules.read_text(encoding="utf-8").replace("for: 2m", "for: 10s").replace("for: 1m", "for: 10s"),
    encoding="utf-8",
)
conf = base / "prometheus.yml"
conf.write_text(conf.read_text(encoding="utf-8").replace("15s", "5s"), encoding="utf-8")
PY

step "3) Start the stack (agent, Prometheus, Alertmanager, Grafana, webhook receiver)"
GRAFANA_ADMIN_PASSWORD="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(18))')"
export GRAFANA_ADMIN_PASSWORD
export PROMETHEUS_DIR="./.tmp/prometheus"
export ALERTMANAGER_CONFIG="./alertmanager/alertmanager.local.yml"
export DISCORD_WEBHOOK_FILE="./alertmanager/local/discord_webhook_url"
"${COMPOSE[@]}" up -d --build 2>&1 | tail -15

wait_for() { # description, command...
  local what="$1"
  shift
  local _
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then echo "ok: $what"; return 0; fi
    sleep 2
  done
  fail "timeout waiting for $what"
}
wait_for "agent /health" curl -fsS "http://127.0.0.1:${AGENT_PORT}/health"
wait_for "Prometheus ready" curl -fsS "http://127.0.0.1:${PROMETHEUS_PORT}/-/ready"
wait_for "Alertmanager ready" curl -fsS "http://127.0.0.1:${ALERTMANAGER_PORT}/-/ready"
wait_for "webhook receiver" curl -fsS "http://127.0.0.1:${WEBHOOK_PORT}/health"
target_up() {
  curl -fsS "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/targets" |
    json 'import json,sys; t=json.load(sys.stdin)["data"]["activeTargets"]; sys.exit(0 if any(x["labels"]["job"]=="ai-agent" and x["health"]=="up" for x in t) else 1)'
}
wait_for "Prometheus scraping the agent" target_up

step "4) Grafana: datasource and provisioned dashboard"
GF="http://127.0.0.1:${GRAFANA_PORT}"
wait_for "Grafana health" curl -fsS "$GF/api/health"
dash_found() {
  curl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" "$GF/api/search?query=AI%20Agent" |
    json 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if any(d.get("uid")=="ai-agent-overview" for d in r) else 1)'
}
wait_for "dashboard 'AI Agent overview' provisioned" dash_found
# A query through Grafana's own proxy proves the provisioned datasource reaches Prometheus.
curl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" "$GF/api/datasources/proxy/uid/prometheus/api/v1/query?query=up" |
  json 'import json,sys; r=json.load(sys.stdin); print("datasource query via Grafana:", r["status"], len(r["data"]["result"]), "series"); sys.exit(0 if r["status"]=="success" and r["data"]["result"] else 1)'

step "5) Generate errors against the API until HighErrorRate fires"
chat() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST -H 'Content-Type: application/json' -d "$1" "http://127.0.0.1:${AGENT_PORT}/chat" || true; }
alert_state() {
  curl -fsS "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/alerts" |
    json 'import json,sys; a=[x for x in json.load(sys.stdin)["data"]["alerts"] if x["labels"]["alertname"]=="HighErrorRate"]; print(a[0]["state"] if a else "none")'
}
ok_code=""
fail_code=""
state="none"
for i in $(seq 1 90); do
  ok_code="$(chat '{"message": "hola"}')"
  fail_code="$(chat '{"message": "forced failure __fail__"}')"
  fail_code="$(chat '{"message": "forced failure __fail__"}')"
  state="$(alert_state || echo none)"
  if [ "$state" = "firing" ]; then echo "HighErrorRate is firing after $i rounds"; break; fi
  sleep 1
done
echo "last status codes: healthy request=${ok_code}, forced failure=${fail_code}"
[ "$ok_code" = "200" ] || fail "a healthy request did not return 200"
[ "$fail_code" = "502" ] || fail "forced failures should return 502 (got ${fail_code})"
[ "$state" = "firing" ] || fail "HighErrorRate never fired (last state: $state)"
curl -fsS "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/alerts" >"$EVIDENCE_DIR/alert-prometheus-firing.json"

step "6) The notification must reach the webhook receiver"
received() {
  curl -fsS "http://127.0.0.1:${WEBHOOK_PORT}/received" >"$EVIDENCE_DIR/alert-received-raw.json"
  json "
import json, sys
items = json.load(open('$EVIDENCE_DIR/alert-received-raw.json'))
def high_error(item):
    payload = item['payload']
    if item['path'] == '/webhook':
        return payload.get('status') == 'firing' and any(
            a['labels']['alertname'] == 'HighErrorRate' for a in payload['alerts'])
    if item['path'] == '/discord':
        return 'FIRING' in payload['embeds'][0]['title'] and 'HighErrorRate' in payload['embeds'][0]['title']
    return False
paths = {i['path'] for i in items if high_error(i)}
sys.exit(0 if paths == {'/webhook', '/discord'} else 1)
"
}
wait_for "HighErrorRate delivered to the webhook and Discord endpoints" received
json "
import json
items = json.load(open('$EVIDENCE_DIR/alert-received-raw.json'))
def pick(path, test):
    return next(i for i in items if i['path'] == path and test(i['payload']))
generic = pick('/webhook', lambda p: p.get('status') == 'firing' and any(a['labels']['alertname'] == 'HighErrorRate' for a in p['alerts']))
discord = pick('/discord', lambda p: 'FIRING' in p['embeds'][0]['title'] and 'HighErrorRate' in p['embeds'][0]['title'])
json.dump(generic, open('$EVIDENCE_DIR/alert-webhook-payload.json', 'w'), indent=2)
json.dump(discord, open('$EVIDENCE_DIR/alert-discord-payload.json', 'w'), indent=2)
alert = next(a for a in generic['payload']['alerts'] if a['labels']['alertname'] == 'HighErrorRate')
print('webhook alert:', alert['labels']['alertname'], alert['labels']['severity'], '| status:', alert['status'])
print('discord embed title:', discord['payload']['embeds'][0]['title'])
"
rm -f "$EVIDENCE_DIR/alert-received-raw.json"

step "7) Dashboard query sanity: the error ratio Grafana plots is above the 5 % threshold"
curl -fsS -G "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/query" \
  --data-urlencode 'query=sum(rate(agent_http_requests_total{route="/chat",status=~"5.."}[5m])) / sum(rate(agent_http_requests_total{route="/chat"}[5m]))' |
  json 'import json,sys; v=float(json.load(sys.stdin)["data"]["result"][0]["value"][1]); print("5xx ratio: %.2f" % v); sys.exit(0 if v > 0.05 else 1)'

echo
echo "monitoring alert test PASSED"
