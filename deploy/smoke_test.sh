#!/usr/bin/env bash
# Post-deployment checks against a base URL.
#
# Usage: smoke_test.sh <base-url> [--expect-sha SHA] [--full]
#
#   default  POST /chat with a real JSON payload and assert the "response" field exists
#   --full   production check: /health, /chat and /metrics must all answer HTTP 200
#            (and /health must report the expected commit)
#
# Exit codes: 0 all good, 1 a check failed (the caller decides whether to roll back), 2 usage.
set -euo pipefail

BASE_URL="${1:-}"
[ -n "$BASE_URL" ] || { echo "usage: $0 <base-url> [--expect-sha SHA] [--full]" >&2; exit 2; }
shift
EXPECT_SHA=""
FULL=false
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-sha) EXPECT_SHA="$2"; shift 2 ;;
    --full) FULL=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
BASE_URL="${BASE_URL%/}"
RETRIES="${SMOKE_RETRIES:-5}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

# status_of <method> <path> [data] -> prints "<http-code>" and stores the body in $BODY_FILE
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT
status_of() {
  local method="$1" path="$2" data="${3:-}" code
  if [ -n "$data" ]; then
    code="$(curl -sS -o "$BODY_FILE" -w '%{http_code}' --max-time 60 --retry "$RETRIES" \
      --retry-delay 3 --retry-connrefused -X "$method" -H 'Content-Type: application/json' \
      -d "$data" "$BASE_URL$path" || true)"
  else
    code="$(curl -sS -o "$BODY_FILE" -w '%{http_code}' --max-time 30 --retry "$RETRIES" \
      --retry-delay 3 --retry-connrefused -X "$method" "$BASE_URL$path" || true)"
  fi
  echo "${code:-000}"
}

check_chat() {
  local code
  code="$(status_of POST /chat '{"message": "Responde solo con la palabra OK"}')"
  [ "$code" = "200" ] || fail "POST /chat returned HTTP $code"
  "$PYTHON_BIN" - "$BODY_FILE" <<'PY' || fail "/chat JSON has no non-empty 'response' field"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert isinstance(data.get("response"), str) and data["response"].strip(), data
PY
  echo "ok: POST /chat -> 200 with a 'response' field"
}

check_health() {
  local code
  code="$(status_of GET /health)"
  [ "$code" = "200" ] || fail "GET /health returned HTTP $code"
  EXPECTED="$EXPECT_SHA" "$PYTHON_BIN" - "$BODY_FILE" <<'PY' || fail "/health payload is not as expected"
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert data.get("status") == "ok", data
expected = os.environ.get("EXPECTED", "")
assert not expected or data.get("commit") == expected, f"commit {data.get('commit')} != {expected}"
PY
  echo "ok: GET /health -> 200${EXPECT_SHA:+ (commit $EXPECT_SHA)}"
}

if [ "$FULL" = true ]; then
  check_health
  check_chat
  code="$(status_of GET /metrics)"
  [ "$code" = "200" ] || fail "GET /metrics returned HTTP $code"
  echo "ok: GET /metrics -> 200"
else
  check_chat
fi
echo "smoke test passed for $BASE_URL"
