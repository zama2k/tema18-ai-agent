#!/usr/bin/env bash
# Start or stop the simulated deployment host (a container that behaves like the EC2 instance).
#
# Usage: sim_host.sh start <workdir> | stop
#
# `start` builds the image, creates an ephemeral SSH key pair and a known_hosts file pinned to the
# container's own host key (read out of band, so there is no trust-on-first-use), and waits for
# sshd. Files written to <workdir>: id_ed25519, id_ed25519.pub, known_hosts.
#
# Environment (defaults in brackets):
#   T18_HOST_SSH_PORT [2322]  T18_NGINX_PORT [8380]  T18_CONTAINER [t18ej2-host]
#   T18_IMAGE [t18ej2-host:local]  T18_REMOVE_IMAGE [0]
set -euo pipefail

# Git Bash on Windows rewrites arguments that look like POSIX paths (docker exec ... /etc/ssh).
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_PORT="${T18_HOST_SSH_PORT:-2322}"
NGINX_PORT="${T18_NGINX_PORT:-8380}"
CONTAINER="${T18_CONTAINER:-t18ej2-host}"
IMAGE="${T18_IMAGE:-t18ej2-host:local}"

start() {
  local workdir="$1"
  mkdir -p "$workdir"
  docker build -q -f "$ROOT/deploy/host/Dockerfile" -t "$IMAGE" "$ROOT" >/dev/null

  rm -f "$workdir/id_ed25519" "$workdir/id_ed25519.pub"
  ssh-keygen -q -t ed25519 -N "" -C "t18ej2-ephemeral" -f "$workdir/id_ed25519"
  chmod 600 "$workdir/id_ed25519"

  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" -p "${SSH_PORT}:22" -p "${NGINX_PORT}:80" \
    -e "AUTHORIZED_KEY=$(cat "$workdir/id_ed25519.pub")" "$IMAGE" >/dev/null

  local host_key
  host_key="$(docker exec "$CONTAINER" cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
  printf '[127.0.0.1]:%s %s\n' "$SSH_PORT" "$host_key" >"$workdir/known_hosts"

  local _
  for _ in $(seq 1 30); do
    if ssh -i "$workdir/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$workdir/known_hosts" \
      -p "$SSH_PORT" deploy@127.0.0.1 true 2>/dev/null; then
      echo "simulated host ready: ssh deploy@127.0.0.1:${SSH_PORT}, http://127.0.0.1:${NGINX_PORT}"
      return 0
    fi
    sleep 1
  done
  docker logs "$CONTAINER" 2>&1 | tail -20
  echo "SSH never became reachable on the simulated host" >&2
  return 1
}

stop() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [ "${T18_REMOVE_IMAGE:-0}" = "1" ]; then
    docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
  fi
}

case "${1:-}" in
  start) [ -n "${2:-}" ] || { echo "usage: $0 start <workdir>" >&2; exit 2; }; start "$2" ;;
  stop) stop ;;
  *) echo "usage: $0 start <workdir> | stop" >&2; exit 2 ;;
esac
