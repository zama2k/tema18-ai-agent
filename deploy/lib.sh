#!/usr/bin/env bash
# Shared helpers for the blue/green scripts. Sourced, never executed.
# shellcheck shell=bash

: "${AGENT_ROOT:=/opt/agent}"
: "${BLUE_PORT:=8000}"
: "${GREEN_PORT:=8001}"
# Command used to control the services. A real host uses systemd; the simulated host uses agentctl.
: "${SERVICE_CTL:=sudo systemctl}"
: "${NGINX_CONF:=/etc/nginx/conf.d/agent.conf}"
: "${NGINX_TEST_CMD:=sudo nginx -t}"
: "${NGINX_RELOAD_CMD:=sudo systemctl reload nginx}"
# Port nginx listens on, as seen from the host itself (used for the post-switch verification).
: "${NGINX_LOCAL_PORT:=80}"
: "${HEALTH_ATTEMPTS:=30}"
: "${HEALTH_INTERVAL:=2}"
: "${PYTHON_BIN:=python3}"

log() { printf '%s [deploy] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# Control a service: ctl <action> <unit>. SERVICE_CTL is intentionally word-split.
ctl() {
  # shellcheck disable=SC2086
  $SERVICE_CTL "$@"
}

port_of() { # color -> port
  case "$1" in
    blue) echo "$BLUE_PORT" ;;
    green) echo "$GREEN_PORT" ;;
    *) die "unknown color: $1" ;;
  esac
}

color_of() { # port -> color
  if [ "$1" = "$BLUE_PORT" ]; then echo blue; else echo green; fi
}

# Read the active port. Empty output means nothing has been deployed yet.
read_current_port() {
  if [ -f "$AGENT_ROOT/current_port" ]; then
    tr -d '[:space:]' <"$AGENT_ROOT/current_port"
  fi
}

write_current_port() { # atomic replace so readers never see a partial file
  local tmp
  tmp="$(mktemp "$AGENT_ROOT/.current_port.XXXXXX")"
  printf '%s\n' "$1" >"$tmp"
  mv -f "$tmp" "$AGENT_ROOT/current_port"
}

# Succeeds when GET <url> answers 200 with status "ok"; when a sha is given it must match "commit".
health_ok() { # url [expected_sha]
  local body
  body="$(curl -fsS --max-time 3 "$1" 2>/dev/null)" || return 1
  HEALTH_BODY="$body" EXPECTED="${2:-}" "$PYTHON_BIN" - <<'PY'
import json
import os
import sys

try:
    data = json.loads(os.environ["HEALTH_BODY"])
except ValueError:
    sys.exit(1)
expected = os.environ.get("EXPECTED", "")
ok = data.get("status") == "ok" and (not expected or data.get("commit") == expected)
sys.exit(0 if ok else 1)
PY
}

wait_healthy() { # port expected_sha
  local port="$1" sha="$2" i
  for ((i = 1; i <= HEALTH_ATTEMPTS; i++)); do
    if health_ok "http://127.0.0.1:${port}/health" "$sha"; then
      log "port ${port} healthy with commit ${sha} (attempt ${i}/${HEALTH_ATTEMPTS})"
      return 0
    fi
    log "port ${port} not healthy yet (attempt ${i}/${HEALTH_ATTEMPTS})"
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# Point nginx at a port: edit proxy_pass, validate, reload. Restores the previous file on failure.
switch_nginx() { # port
  local port="$1" backup
  backup="$(mktemp "$AGENT_ROOT/.nginx-backup.XXXXXX")"
  cp "$NGINX_CONF" "$backup"
  # Rewrite in place (no rename): the deploy user may own the file but not /etc/nginx/conf.d.
  sed -E "s#(proxy_pass http://127\.0\.0\.1:)[0-9]+;#\1${port};#" "$backup" >"$NGINX_CONF"
  if ! grep -q "proxy_pass http://127.0.0.1:${port};" "$NGINX_CONF"; then
    cp "$backup" "$NGINX_CONF"
    rm -f "$backup"
    log "proxy_pass line not found in ${NGINX_CONF}"
    return 1
  fi
  # shellcheck disable=SC2086
  if ! $NGINX_TEST_CMD; then
    cp "$backup" "$NGINX_CONF"
    rm -f "$backup"
    log "nginx -t failed; configuration restored"
    return 1
  fi
  # shellcheck disable=SC2086
  $NGINX_RELOAD_CMD
  rm -f "$backup"
  log "nginx now proxies to ${port}"
}

# nginx applies a reload asynchronously (old workers drain), so poll until the new commit shows up.
wait_served() { # expected_sha
  local i
  for ((i = 1; i <= 20; i++)); do
    if health_ok "http://127.0.0.1:${NGINX_LOCAL_PORT}/health" "$1"; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}
