#!/usr/bin/env bash
# Run a deployment or a rollback on a remote host over SSH (executed on the CI runner).
#
# Usage:
#   remote_deploy.sh --host H [--port 22] [--user deploy] --key FILE --known-hosts FILE \
#       --action deploy --release release.tar --sha ID [--env NAME=VALUE]...
#   remote_deploy.sh ... --action rollback [--env NAME=VALUE]...
#
# Host keys are verified against --known-hosts (StrictHostKeyChecking=yes). Disabling the check
# would let anybody who can intercept the connection receive our deployment key's authentication
# and the release; pinning the key (or at least pinning it once with ssh-keyscan in a trusted
# step, see cd.yml) closes that door.
set -euo pipefail

HOST="" PORT="22" USER_NAME="deploy" KEY="" KNOWN_HOSTS="" ACTION="deploy" RELEASE="" SHA=""
REMOTE_ENV=()

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --user) USER_NAME="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --known-hosts) KNOWN_HOSTS="$2"; shift 2 ;;
    --action) ACTION="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --sha) SHA="$2"; shift 2 ;;
    --env) REMOTE_ENV+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! { [ -n "$HOST" ] && [ -f "$KEY" ] && [ -f "$KNOWN_HOSTS" ]; }; then
  echo "--host, --key and --known-hosts (existing files) are required" >&2
  exit 2
fi
case "$ACTION" in
  deploy)
    if ! { [ -f "$RELEASE" ] && [ -n "$SHA" ]; }; then
      echo "deploy needs --release and --sha" >&2
      exit 2
    fi
    ;;
  rollback) ;;
  *) echo "--action must be deploy or rollback" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$KNOWN_HOSTS" -o ConnectTimeout=15)
TARGET="${USER_NAME}@${HOST}"
REMOTE_DIR="/tmp/agent-deploy-$$-$(date +%s)"

remote() { ssh "${SSH_OPTS[@]}" -p "$PORT" "$TARGET" "$@"; }

env_prefix=""
for pair in "${REMOTE_ENV[@]}"; do
  [[ "$pair" =~ ^[A-Z_][A-Z0-9_]*= ]] || { echo "invalid --env: $pair" >&2; exit 2; }
  env_prefix+="$(printf '%q ' "$pair")"
done

cleanup() { remote "rm -rf '$REMOTE_DIR'" >/dev/null 2>&1 || true; }
remote "mkdir -p '$REMOTE_DIR'"
trap cleanup EXIT

scp -q "${SSH_OPTS[@]}" -P "$PORT" \
  "$SCRIPT_DIR/lib.sh" "$SCRIPT_DIR/blue_green_deploy.sh" "$SCRIPT_DIR/rollback.sh" \
  "$TARGET:$REMOTE_DIR/"

if [ "$ACTION" = "deploy" ]; then
  scp -q "${SSH_OPTS[@]}" -P "$PORT" "$RELEASE" "$TARGET:$REMOTE_DIR/release.tar"
  remote "env ${env_prefix}bash '$REMOTE_DIR/blue_green_deploy.sh' '$REMOTE_DIR/release.tar' '$SHA'"
else
  remote "env ${env_prefix}bash '$REMOTE_DIR/rollback.sh'"
fi
