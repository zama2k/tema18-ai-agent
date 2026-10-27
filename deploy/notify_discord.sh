#!/usr/bin/env bash
# Send a message to a Discord webhook. Never fails the calling job.
#
# Environment:
#   DISCORD_WEBHOOK_URL  webhook URL (secret). If empty the step is skipped with a ::notice::.
#   MESSAGE_TITLE        first line (bold)
#   MESSAGE_BODY         details (optional)
#
# API reference: https://docs.discord.com/developers/resources/webhook (Execute Webhook,
# consulted 2026-10-27): JSON body with "content" (max 2000 chars) and "allowed_mentions";
# 204 No Content on success.
set -euo pipefail

if [ -z "${DISCORD_WEBHOOK_URL:-}" ]; then
  echo "::notice::DISCORD_WEBHOOK_URL is not configured: notification skipped."
  exit 0
fi

PYTHON_BIN=python3
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python

payload="$(
  TITLE="${MESSAGE_TITLE:-Notification}" BODY="${MESSAGE_BODY:-}" "$PYTHON_BIN" - <<'PY'
import json
import os

text = f"**{os.environ['TITLE']}**"
if os.environ["BODY"]:
    text += "\n" + os.environ["BODY"]
payload = {
    "content": text[:1900],
    "username": "AI Agent CI/CD",
    # Never let message text ping @everyone or roles.
    "allowed_mentions": {"parse": []},
}
print(json.dumps(payload))
PY
)"

status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 --retry 2 \
  -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" || true)"
case "${status:-000}" in
  200 | 204) echo "Discord notification sent." ;;
  *) echo "::warning::Discord notification failed (HTTP ${status:-000})." ;;
esac
