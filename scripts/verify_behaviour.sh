#!/usr/bin/env bash
# Behaviour-preservation check for refactors.
#
# Runs the CLI across a set of scenarios twice -- once on the current working
# tree and once on stashed HEAD -- IN THE SAME TIMEFRAME, then diffs. Capturing
# both back-to-back controls for CBC solver non-determinism (equally-optimal
# plans can differ across separate process launches; see the cbc-nondeterminism
# memory), so any surviving diff is a genuine behaviour change, not solver flap.
#
# Usage:  bash scripts/verify_behaviour.sh
# Exit 0 = zero diff (behaviour preserved). Exit 1 = a real diff (inspect it).
# If the working tree is clean it just runs the scenarios once and passes.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
BASE="$WORK/base"
CUR="$WORK/cur"
mkdir -p "$BASE" "$CUR"

# Scenario name -> CLI args. Add rows here as coverage needs grow.
scenarios=(
  "default|"
  "default_b|-b"
  "plans8|--plans 8"
  "inv|--plan-mode inventory --current-stamina 100000"
  "inv_b|--plan-mode inventory --current-stamina 100000 -b"
  "cheapest|--cheapest-plan"
  "mastery|-m Gold Bar:5000000"
  "acquire|-i Steel:100000"
  "unobtainable|--unobtainable"
)

capture() {  # $1 = output dir
  local out="$1" row name args
  for row in "${scenarios[@]}"; do
    name="${row%%|*}"
    args="${row#*|}"
    # shellcheck disable=SC2086
    python "$ROOT/main.py" $args > "$out/$name.txt" 2>&1
  done
}

echo "Capturing current working tree..."
capture "$CUR"

if git -C "$ROOT" diff --quiet && git -C "$ROOT" diff --cached --quiet; then
  echo "Working tree clean; nothing to compare against. Ran scenarios OK."
  rm -rf "$WORK"
  exit 0
fi

echo "Stashing and capturing HEAD baseline (same timeframe)..."
git -C "$ROOT" stash push --quiet --include-untracked -- '*.py'
capture "$BASE"
git -C "$ROOT" stash pop --quiet

echo "=== diff (empty means behaviour preserved) ==="
if diff -rq "$BASE" "$CUR"; then
  echo "ZERO DIFF — behaviour preserved."
  rm -rf "$WORK"
  exit 0
else
  echo ""
  echo "Differences found; per-file detail above. Full outputs in: $WORK"
  exit 1
fi
