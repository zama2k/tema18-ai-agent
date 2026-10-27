#!/usr/bin/env bash
# Roll traffic back to the previous slot, executed ON the target host.
#
# Usage: rollback.sh
#
# The previous slot keeps running as a hot standby after every deployment, so this normally just
# repoints nginx. If the standby died, its service is restarted from the release it still links.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
source "$SCRIPT_DIR/lib.sh"

mkdir -p "$AGENT_ROOT"
exec 9>"$AGENT_ROOT/.deploy.lock"
flock -n 9 || die "another deployment is running"

current_port="$(read_current_port)"
[ -n "$current_port" ] || die "nothing is deployed: no current_port file"

if [ "$current_port" = "$BLUE_PORT" ]; then
  standby_color=green
  standby_port="$GREEN_PORT"
else
  standby_color=blue
  standby_port="$BLUE_PORT"
fi

standby_release="$(readlink -f "$AGENT_ROOT/slots/$standby_color" || true)"
[ -n "$standby_release" ] && [ -d "$standby_release" ] ||
  die "the standby slot (${standby_color}) has no release to roll back to"
standby_id="$(basename "$standby_release")"

if ! health_ok "http://127.0.0.1:${standby_port}/health" "$standby_id"; then
  log "standby is not healthy, restarting agent-${standby_color} (${standby_id})"
  ctl restart "agent-$standby_color"
  wait_healthy "$standby_port" "$standby_id" || die "standby ${standby_color} cannot be recovered"
fi

switch_nginx "$standby_port" || die "could not switch nginx to the standby"
wait_served "$standby_id" ||
  die "rollback verification failed through nginx"

write_current_port "$standby_port"
printf '%s\n' "$current_port" >"$AGENT_ROOT/previous_port"
ctl stop "agent-$(color_of "$current_port")" || true
log "rollback finished: ${standby_color}:${standby_port} serves ${standby_id}"
