#!/usr/bin/env bash
# Format the files this build changed, as `npm run format` would, before the
# test gate runs `npm run lint` (TEST_GATE_PRECHECK).
#
# WHY: CI's first step is `npm run lint` (ESLint with prettier/prettier as an
# error). The gate ran only `npm test`, so formatting slipped through every
# build and CI failed on it: KAN-142, KAN-143 and KAN-144 (veriform-ui PRs
# #292-#294) each spent a fix round, ~40 min, and one of two attempts, on
# nothing but line breaks. Formatting is mechanical; the model is not needed.
#
# Only files changed against the base branch, and only what ESLint checks, so a
# build never reformats code it did not touch. Uses the project's own Prettier
# (no download). Never fails the gate itself: `npm run lint` is the verdict.
set -uo pipefail
cd /workspace || exit 0
[ -x node_modules/.bin/prettier ] || { echo "prettier not installed; skipping"; exit 0; }

base=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD 2>/dev/null)
mapfile -t files < <(
    { git diff --name-only "$base" 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } \
    | sort -u | grep -E '\.(ts|tsx|js|jsx|mjs|cjs)$' | while read -r f; do [ -f "$f" ] && echo "$f"; done
)
if [ ${#files[@]} -eq 0 ]; then
    echo "no changed source files to format"
    exit 0
fi
node_modules/.bin/prettier --write --log-level warn "${files[@]}" || true
echo "formatted ${#files[@]} changed file(s) with the project's Prettier"
exit 0
