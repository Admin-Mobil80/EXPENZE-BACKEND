#!/usr/bin/env bash
# Run every fixture against the deployed API and print the verdict for each.
#
# Usage: ./scripts/smoke_test.sh <api-url>
#   e.g. ./scripts/smoke_test.sh https://abc.execute-api.ap-southeast-1.amazonaws.com/poc/expenses
set -uo pipefail

API="${1:?usage: smoke_test.sh <api-url>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for f in "$ROOT"/fixtures/*.json; do
  printf '\n=== %s ===\n' "$(basename "$f" .json)"
  start=$(date +%s)
  body=$(curl -sS -X POST "$API" -H 'Content-Type: application/json' \
         --data-binary "@$f" --max-time 40)
  printf 'latency: %ss\n' "$(( $(date +%s) - start ))"
  printf '%s' "$body" | python3 "$ROOT/scripts/format_verdict.py"
done
