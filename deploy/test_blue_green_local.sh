#!/usr/bin/env bash
# End-to-end test of the blue/green scripts against the simulated host (Docker required).
#
#   v1 (blue) -> v2 (green, swap, zero downtime) -> rollback to v1 -> v2 again -> v3 BROKEN
#   (health check fails, traffic keeps flowing to v2)
#
# Environment (defaults in brackets):
#   T18_HOST_SSH_PORT [2322]  T18_NGINX_PORT [8380]  T18_CONTAINER [t18ej2-host]
#   T18_IMAGE [t18ej2-host:local]  RELEASE_REF [WORKTREE]  T18_REMOVE_IMAGE [0]
set -euo pipefail

# Git Bash on Windows rewrites arguments that look like POSIX paths (docker exec ... /opt/agent).
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SSH_PORT="${T18_HOST_SSH_PORT:-2322}"
NGINX_PORT="${T18_NGINX_PORT:-8380}"
CONTAINER="${T18_CONTAINER:-t18ej2-host}"
RELEASE_REF="${RELEASE_REF:-WORKTREE}"
TMP="$ROOT/deploy/.tmp"

PYTHON_BIN=python3
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python

# shellcheck source=deploy/sim_env.sh
source "$ROOT/deploy/sim_env.sh"

failures=0
PROBE_PID=""
log() { printf '\n=== %s\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; failures=$((failures + 1)); }
assert_eq() { # description actual expected
  if [ "$2" = "$3" ]; then pass "$1 (= $3)"; else fail "$1: got '$2', expected '$3'"; fi
}

cleanup() {
  touch "$TMP/probe.stop" 2>/dev/null || true
  "$ROOT/deploy/sim_host.sh" stop || true
  rm -rf "$TMP"
}
trap cleanup EXIT
rm -rf "$TMP"
mkdir -p "$TMP"

json_field() { "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }
served() { # field served through nginx (what real clients see)
  curl -fsS --max-time 5 "http://127.0.0.1:${NGINX_PORT}/health" | json_field "$1"
}
in_host() { docker exec "$CONTAINER" "$@"; }
remote_deploy() {
  "$ROOT/deploy/remote_deploy.sh" --host 127.0.0.1 --port "$SSH_PORT" --user deploy \
    --key "$TMP/sim/id_ed25519" --known-hosts "$TMP/sim/known_hosts" "$@" "${SIM_ENV_ARGS[@]}"
}
deploy() { # tarball id [extra --env args]
  local tarball="$1" id="$2"
  shift 2
  remote_deploy --action deploy --release "$tarball" --sha "$id" "$@"
}

start_probe() { # background load: one request every 100 ms through nginx
  rm -f "$TMP/probe.stop" "$TMP/probe.log"
  (
    while [ ! -f "$TMP/probe.stop" ]; do
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${NGINX_PORT}/health" || true)"
      echo "${code:-000}" >>"$TMP/probe.log"
      sleep 0.1
    done
  ) &
  PROBE_PID=$!
}
stop_probe() { # description
  touch "$TMP/probe.stop"
  wait "$PROBE_PID" 2>/dev/null || true
  local total bad
  total="$(wc -l <"$TMP/probe.log" | tr -d ' ')"
  bad="$(grep -vc '^200$' "$TMP/probe.log" || true)"
  echo "probe: ${total} requests during '$1', ${bad} not HTTP 200"
  assert_eq "no failed request during $1" "$bad" "0"
  if [ "$total" -lt 10 ]; then fail "probe sent only $total requests during $1"; fi
}

log "Start the simulated host (build image, ephemeral key, pinned host key)"
"$ROOT/deploy/sim_host.sh" start "$TMP/sim"
pass "SSH reachable with a pinned host key (StrictHostKeyChecking=yes)"

log "Build releases from ${RELEASE_REF}"
"$ROOT/deploy/make_release.sh" "$RELEASE_REF" "$TMP/v1.tar" --version 1.0.0
"$ROOT/deploy/make_release.sh" "$RELEASE_REF" "$TMP/v2.tar" --version 2.0.0
"$ROOT/deploy/make_release.sh" "$RELEASE_REF" "$TMP/v3.tar" --version 3.0.0 --broken

log "1) First deployment: v1 goes to blue (8000)"
deploy "$TMP/v1.tar" v1-base
assert_eq "nginx serves commit" "$(served commit)" "v1-base"
assert_eq "nginx serves color" "$(served color)" "blue"
assert_eq "nginx serves version" "$(served version)" "1.0.0"
assert_eq "current_port file" "$(in_host cat /opt/agent/current_port)" "8000"

log "2) Blue/green swap: v2 goes to green (8001) under constant load"
start_probe
deploy "$TMP/v2.tar" v2-new
stop_probe "the v1 -> v2 swap"
assert_eq "nginx serves commit" "$(served commit)" "v2-new"
assert_eq "nginx serves color" "$(served color)" "green"
assert_eq "current_port file" "$(in_host cat /opt/agent/current_port)" "8001"
assert_eq "blue is still running v1 as hot standby" \
  "$(in_host curl -fsS http://127.0.0.1:8000/health | json_field commit)" "v1-base"
assert_eq "nginx proxy_pass" \
  "$(in_host grep -o 'proxy_pass http://127.0.0.1:[0-9]*' /etc/nginx/conf.d/agent.conf)" \
  "proxy_pass http://127.0.0.1:8001"

log "3) rollback.sh: traffic goes back to v1 on blue"
remote_deploy --action rollback
assert_eq "nginx serves commit" "$(served commit)" "v1-base"
assert_eq "current_port file" "$(in_host cat /opt/agent/current_port)" "8000"

log "4) Deploy v2 again (target is green because blue is active)"
deploy "$TMP/v2.tar" v2-new
assert_eq "nginx serves commit" "$(served commit)" "v2-new"
assert_eq "current_port file" "$(in_host cat /opt/agent/current_port)" "8001"

log "5) FAILURE DRILL: v3 crashes on start; v2 must keep serving"
start_probe
set +e
deploy "$TMP/v3.tar" v3-broken --env HEALTH_ATTEMPTS=6 --env HEALTH_INTERVAL=1
broken_exit=$?
set -e
stop_probe "the failed v3 deployment"
if [ "$broken_exit" -ne 0 ]; then
  pass "deploy script exited non-zero ($broken_exit) for the broken release"
else
  fail "broken release was reported as success"
fi
assert_eq "nginx still serves commit" "$(served commit)" "v2-new"
assert_eq "current_port file unchanged" "$(in_host cat /opt/agent/current_port)" "8001"
if in_host curl -fsS --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  fail "the broken slot is still answering on 8000"
else
  pass "the broken slot was stopped (port 8000 refuses connections)"
fi

log "Result"
if [ "$failures" -ne 0 ]; then
  echo "blue/green local test FAILED with $failures failed assertion(s)"
  exit 1
fi
echo "blue/green local test PASSED"
