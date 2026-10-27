#!/usr/bin/env bash
# Build a release tarball: the exact content of a commit (git archive), optionally tweaked.
#
# Usage: make_release.sh <git-ref|WORKTREE> <out.tar> [--version X.Y.Z] [--broken]
#
#   WORKTREE   package the working tree (tracked + untracked, honouring .gitignore)
#   --version  overwrite the VERSION file inside the tarball (to tell releases apart)
#   --broken   inject a crash at import time (used by the failure drill, never committed)
set -euo pipefail

REF="${1:-}"
OUT="${2:-}"
[ -n "$REF" ] && [ -n "$OUT" ] || { echo "usage: $0 <git-ref|WORKTREE> <out.tar> [--version X] [--broken]" >&2; exit 2; }
shift 2

VERSION_OVERRIDE=""
BROKEN=false
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION_OVERRIDE="$2"; shift 2 ;;
    --broken) BROKEN=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/tree"

if [ "$REF" = "WORKTREE" ]; then
  (cd "$repo_root" && git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf -) |
    tar -xf - -C "$work/tree"
else
  git -C "$repo_root" archive --format=tar "$REF" | tar -xf - -C "$work/tree"
fi

if [ -n "$VERSION_OVERRIDE" ]; then
  printf '%s\n' "$VERSION_OVERRIDE" >"$work/tree/VERSION"
fi
if [ "$BROKEN" = true ]; then
  printf '\nraise RuntimeError("simulated broken release")\n' >>"$work/tree/src/api.py"
fi

out_abs="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
tar -cf "$out_abs" -C "$work/tree" .
echo "release written to $OUT ($(wc -c <"$out_abs") bytes)"
