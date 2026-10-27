#!/usr/bin/env bash
# Blue/green deployment, executed ON the target host (over SSH).
#
# Usage: blue_green_deploy.sh <release.tar> <release-id>
#
# The release tarball contains the exact code of one commit (git archive); the release id is the
# commit SHA. Flow: prepare the INACTIVE slot -> restart it -> health check -> switch nginx ->
# persist state. If anything fails before the switch, the active slot is never touched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

TARBALL="${1:-}"
RELEASE_ID="${2:-}"
[ -f "$TARBALL" ] || die "usage: $0 <release.tar> <release-id> (tarball not found: '$TARBALL')"
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "invalid release id: '$RELEASE_ID'"

mkdir -p "$AGENT_ROOT"/{releases,slots}

# Only one deployment at a time.
exec 9>"$AGENT_ROOT/.deploy.lock"
flock -n 9 || die "another deployment is running"

# --- 1. Decide the target slot -------------------------------------------------------------
current_port="$(read_current_port)"
if [ "$current_port" = "$BLUE_PORT" ]; then
  target_color=green
  target_port="$GREEN_PORT"
elif [ "$current_port" = "$GREEN_PORT" ]; then
  target_color=blue
  target_port="$BLUE_PORT"
else
  [ -z "$current_port" ] || die "current_port contains an unexpected value: '$current_port'"
  log "no active slot yet: first deployment goes to blue"
  target_color=blue
  target_port="$BLUE_PORT"
fi
log "active=${current_port:-none} target=${target_color}:${target_port} release=${RELEASE_ID}"

# --- 2. Prepare the release (code of the exact commit + its own virtualenv) -----------------
release_dir="$AGENT_ROOT/releases/$RELEASE_ID"
if [ ! -x "$release_dir/.venv/bin/python" ]; then
  rm -rf "$release_dir" "$release_dir.tmp"
  mkdir -p "$release_dir.tmp"
  tar -xf "$TARBALL" -C "$release_dir.tmp"
  "$PYTHON_BIN" -m venv "$release_dir.tmp/.venv"
  pip_args=(--quiet --disable-pip-version-check)
  if [ -d "$AGENT_ROOT/wheelhouse" ]; then
    pip_args+=(--find-links "$AGENT_ROOT/wheelhouse")
  fi
  "$release_dir.tmp/.venv/bin/pip" install "${pip_args[@]}" -r "$release_dir.tmp/requirements.txt"
  mv "$release_dir.tmp" "$release_dir"
  log "release prepared at ${release_dir}"
else
  log "release ${RELEASE_ID} already prepared, reusing it"
fi

# --- 3. Point the inactive slot at it and restart ONLY that service -------------------------
ln -sfn "$release_dir" "$AGENT_ROOT/slots/$target_color"
printf 'GIT_SHA=%s\nDEPLOY_COLOR=%s\n' "$RELEASE_ID" "$target_color" >"$AGENT_ROOT/slots/$target_color.env"
ctl restart "agent-$target_color"

# --- 4. Health check (30 attempts); failure leaves the active slot untouched ----------------
if ! wait_healthy "$target_port" "$RELEASE_ID"; then
  log "new version never became healthy: stopping agent-${target_color}, active slot untouched"
  ctl stop "agent-$target_color" || true
  exit 1
fi

# --- 5. Switch traffic ------------------------------------------------------------------------
if ! switch_nginx "$target_port"; then
  ctl stop "agent-$target_color" || true
  die "could not switch nginx; active slot untouched"
fi

# Verify through nginx itself: what clients see must be the new release.
if ! wait_served "$RELEASE_ID"; then
  log "post-switch verification failed: reverting nginx to the previous port"
  if [ -n "$current_port" ]; then
    switch_nginx "$current_port" || log "WARNING: could not revert nginx, use rollback.sh"
  fi
  ctl stop "agent-$target_color" || true
  exit 1
fi

# --- 6. Persist state ------------------------------------------------------------------------
if [ -n "$current_port" ]; then
  printf '%s\n' "$current_port" >"$AGENT_ROOT/previous_port"
fi
write_current_port "$target_port"

# Keep the 5 newest releases, never one that a slot points to. The old slot keeps running as a
# hot standby, which is what makes rollback.sh instantaneous.
mapfile -t keep < <(readlink -f "$AGENT_ROOT"/slots/blue "$AGENT_ROOT"/slots/green 2>/dev/null || true)
while IFS= read -r old; do
  skip=false
  for k in "${keep[@]}"; do
    [ "$old" = "$k" ] && skip=true
  done
  $skip || rm -rf "$old"
done < <(find "$AGENT_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p
' |
  sort -rn | tail -n +6 | cut -d' ' -f2-)

log "deployment finished: ${target_color}:${target_port} serves ${RELEASE_ID}"
