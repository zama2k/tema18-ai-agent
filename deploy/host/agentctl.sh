#!/usr/bin/env bash
# agentctl: a tiny stand-in for "systemctl <action> agent-blue|agent-green" inside a container
# that has no systemd. Must run as root (the deploy user calls it through sudo).
#
# Usage: agentctl <start|stop|restart|is-active|status> <agent-blue|agent-green>
set -euo pipefail

ACTION="${1:-}"
UNIT="${2:-}"
AGENT_ROOT="${AGENT_ROOT:-/opt/agent}"

case "$UNIT" in
  agent-blue) COLOR=blue PORT=8000 ;;
  agent-green) COLOR=green PORT=8001 ;;
  *) echo "usage: agentctl <start|stop|restart|is-active|status> <agent-blue|agent-green>" >&2; exit 2 ;;
esac

PIDFILE="/run/agent/$UNIT.pid"
LOGFILE="/var/log/agent/$UNIT.log"
SLOT="$AGENT_ROOT/slots/$COLOR"

is_active() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

do_stop() {
  if is_active; then
    local pid
    pid="$(cat "$PIDFILE")"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
}

do_start() {
  [ -d "$SLOT" ] || { echo "$UNIT: slot has no release" >&2; exit 1; }
  is_active && return 0
  set -a
  # shellcheck disable=SC1091
  . /etc/agent/agent.env
  # shellcheck disable=SC1090
  . "$AGENT_ROOT/slots/$COLOR.env"
  set +a
  cd "$SLOT"
  setsid runuser -u deploy -- env "PORT=$PORT" \
    "$SLOT/.venv/bin/python" -m uvicorn src.api:create_app --factory \
    --host 127.0.0.1 --port "$PORT" >>"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"
}

case "$ACTION" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_stop; do_start ;;
  is-active)
    if is_active; then echo active; else echo inactive; exit 3; fi
    ;;
  status)
    if is_active; then
      echo "$UNIT: active (pid $(cat "$PIDFILE"))"
    else
      echo "$UNIT: inactive"
      tail -n 20 "$LOGFILE" 2>/dev/null || true
      exit 3
    fi
    ;;
  *) echo "unknown action: $ACTION" >&2; exit 2 ;;
esac
