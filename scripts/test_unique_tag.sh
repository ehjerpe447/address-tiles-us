#!/usr/bin/env bash
# Tests scripts/unique_tag.sh (INFRA-15): no collision, one, two, a suffix-like base, partial-name non-matches,
# stdin input, an empty list, and bad usage. Needs no network and no GitHub.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected '$2', got '$3'"; fail=1; fi
}
base="nad-r24-20261103"

printf '%s\n' nad-r24-20261027 nad-r23-20261103 > "$tmp/none"
check "no collision" "$base" "$(scripts/unique_tag.sh "$base" "$tmp/none")"

: > "$tmp/empty"
check "empty list" "$base" "$(scripts/unique_tag.sh "$base" "$tmp/empty")"

printf '%s\n' nad-r24-20261027 "$base" > "$tmp/one"
check "one collision" "$base-2" "$(scripts/unique_tag.sh "$base" "$tmp/one")"

printf '%s\n' "$base" "$base-2" nad-r24-20261027 > "$tmp/two"
check "two collisions" "$base-3" "$(scripts/unique_tag.sh "$base" "$tmp/two")"

printf '%s\n' "$base" "$base-3" > "$tmp/gap"
check "gap is filled" "$base-2" "$(scripts/unique_tag.sh "$base" "$tmp/gap")"

printf '%s\n' "$base-2" > "$tmp/suffixbase"
check "suffix-like base, free" "$base-2" "$(scripts/unique_tag.sh "$base-2" "$tmp/none")"
check "suffix-like base, taken" "$base-2-2" "$(scripts/unique_tag.sh "$base-2" "$tmp/suffixbase")"

printf '%s\n' "$base-2" "${base}x" "x$base" > "$tmp/partial"
check "only exact lines count" "$base" "$(scripts/unique_tag.sh "$base" "$tmp/partial")"

printf '%s\r\n' "$base" > "$tmp/crlf"
check "CRLF list" "$base-2" "$(scripts/unique_tag.sh "$base" "$tmp/crlf")"

check "stdin" "$base-2" "$(printf '%s\n' "$base" | scripts/unique_tag.sh "$base" -)"

if scripts/unique_tag.sh >/dev/null 2>&1; then echo "FAIL no arguments accepted"; fail=1; else echo "ok   no arguments rejected"; fi
if scripts/unique_tag.sh "$base" "$tmp/missing" >/dev/null 2>&1; then echo "FAIL missing file accepted"; fail=1; else echo "ok   missing file rejected"; fi

if [ "$fail" -eq 0 ]; then echo "all passed"; fi
exit "$fail"
