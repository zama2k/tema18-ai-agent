#!/usr/bin/env bash
# Deploy one commit to one environment and verify it. This is exactly what cd.yml runs, so it can
# be executed locally with the simulated host.
#
# Environment:
#   DEPLOY_ENVIRONMENT  staging | production (production runs the stricter checks)
#   RELEASE_SHA         full commit SHA to deploy (the one that passed CI)
#   EC2_HOST, EC2_SSH_KEY   real host and private key (content). If either is empty the script
#                           deploys to the simulated host container instead.
#   EC2_USER [deploy]  EC2_PORT [22]  EC2_KNOWN_HOSTS (optional, pinned host keys)
#   TARGET_URL          public base URL of the real environment (STAGING_URL / PROD_URL)
#   GITHUB_STEP_SUMMARY, GITHUB_OUTPUT  honoured when set
#
# Behaviour on failure: the new version never receives traffic if its health check fails; if the
# post-deploy smoke test fails, rollback.sh moves traffic back and the script exits non-zero.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENVIRONMENT="${DEPLOY_ENVIRONMENT:?DEPLOY_ENVIRONMENT is required}"
SHA="${RELEASE_SHA:?RELEASE_SHA is required}"
case "$ENVIRONMENT" in staging | production) ;; *) echo "DEPLOY_ENVIRONMENT must be staging or production" >&2; exit 2 ;; esac
[[ "$SHA" =~ ^[0-9a-f]{7,64}$ ]] || { echo "RELEASE_SHA must be a hex commit id" >&2; exit 2; }
SHORT="${SHA:0:7}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
OUTPUT="${GITHUB_OUTPUT:-/dev/null}"

work="$(mktemp -d)"
mode=simulated
cleanup() {
  rm -rf "$work"
  if [ "$mode" = "simulated" ]; then "$ROOT/deploy/sim_host.sh" stop || true; fi
}
trap cleanup EXIT

if [ -n "${EC2_HOST:-}" ] && [ -n "${EC2_SSH_KEY:-}" ]; then
  mode=real
else
  echo "::notice::EC2_HOST/EC2_SSH_KEY are not configured for '$ENVIRONMENT': deploying to the simulated host container instead."
fi

echo "== Building release for ${SHA}"
"$ROOT/deploy/make_release.sh" "$SHA" "$work/release.tar"

remote_args=()
if [ "$mode" = "real" ]; then
  [ -n "${TARGET_URL:-}" ] || { echo "::error::TARGET_URL (STAGING_URL / PROD_URL) is required for a real host"; exit 1; }
  umask 077
  printf '%s\n' "$EC2_SSH_KEY" >"$work/id_key"
  chmod 600 "$work/id_key"
  host="$EC2_HOST"
  port="${EC2_PORT:-22}"
  if [ -n "${EC2_KNOWN_HOSTS:-}" ]; then
    printf '%s\n' "$EC2_KNOWN_HOSTS" >"$work/known_hosts"
    echo "Using the pinned host keys from EC2_KNOWN_HOSTS."
  else
    # Trust on first use for this run only: still strict afterwards, but pin EC2_KNOWN_HOSTS to
    # be safe against an attacker who can intercept this very first connection.
    echo "::warning::EC2_KNOWN_HOSTS is not set: fetching the host key with ssh-keyscan (trust on first use)."
    ssh-keyscan -p "$port" -t ed25519 "$host" >"$work/known_hosts" 2>/dev/null
    [ -s "$work/known_hosts" ] || { echo "::error::ssh-keyscan returned no host key"; exit 1; }
  fi
  remote_args=(--host "$host" --port "$port" --user "${EC2_USER:-deploy}" --key "$work/id_key" --known-hosts "$work/known_hosts")
  base_url="${TARGET_URL%/}"
else
  export T18_REMOVE_IMAGE="${T18_REMOVE_IMAGE:-0}"
  "$ROOT/deploy/sim_host.sh" start "$work/sim"
  # shellcheck source=deploy/sim_env.sh
  source "$ROOT/deploy/sim_env.sh"
  remote_args=(--host 127.0.0.1 --port "${T18_HOST_SSH_PORT:-2322}" --user deploy
    --key "$work/sim/id_ed25519" --known-hosts "$work/sim/known_hosts" "${SIM_ENV_ARGS[@]}")
  base_url="http://127.0.0.1:${T18_NGINX_PORT:-8380}"

  # An empty host has no "previous version": install a base release first so the swap is real.
  echo "== Installing a base release so the swap to ${SHORT} is a real blue/green switch"
  "$ROOT/deploy/make_release.sh" "$SHA" "$work/base.tar" --version 0.0.0-base
  "$ROOT/deploy/remote_deploy.sh" "${remote_args[@]}" --action deploy \
    --release "$work/base.tar" --sha "base-${SHORT}"
fi

echo "== Deploying ${SHA} to ${ENVIRONMENT} (${mode})"
"$ROOT/deploy/remote_deploy.sh" "${remote_args[@]}" --action deploy --release "$work/release.tar" --sha "$SHA"

smoke_args=("$base_url" --expect-sha "$SHA")
[ "$ENVIRONMENT" = "production" ] && smoke_args+=(--full)
# The simulated host answers with the mock provider on a different port than the nginx default.
echo "== Verifying ${base_url}"
if ! "$ROOT/deploy/smoke_test.sh" "${smoke_args[@]}"; then
  echo "::error::post-deploy verification failed on ${ENVIRONMENT}: rolling back"
  "$ROOT/deploy/remote_deploy.sh" "${remote_args[@]}" --action rollback || echo "::error::ROLLBACK FAILED: manual intervention needed"
  {
    echo "### ${ENVIRONMENT}: deployment FAILED and was rolled back"
    echo "- commit \`${SHORT}\`, mode \`${mode}\`"
  } >>"$SUMMARY"
  exit 1
fi

checks='POST /chat'
if [ "$ENVIRONMENT" = production ]; then checks='/health, /chat, /metrics'; fi
{
  echo "### ${ENVIRONMENT}: deployed \`${SHORT}\`"
  echo "| | |"
  echo "|---|---|"
  echo "| Commit | \`${SHA}\` |"
  echo "| Mode | ${mode} |"
  echo "| Checks | ${checks} |"
  echo "| Finished (UTC) | $(date -u +%Y-%m-%dT%H:%M:%SZ) |"
} >>"$SUMMARY"
echo "mode=${mode}" >>"$OUTPUT"
echo "== ${ENVIRONMENT} deployment of ${SHORT} verified"
