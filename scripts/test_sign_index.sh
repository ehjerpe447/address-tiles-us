#!/usr/bin/env bash
# SEC-10: tests scripts/sign_index.sh with a throwaway key made here (never a real one): it signs, the signature
# verifies, an edited index no longer verifies, and a missing key fails instead of publishing unsigned.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

openssl genpkey -algorithm ed25519 -out "$work/test-key.pem"
openssl pkey -in "$work/test-key.pem" -pubout -out "$work/public.pem"
mkdir "$work/out"
printf '{"schema":1,"states":[]}\n' > "$work/out/index.json"

INDEX_SIGNING_KEY="$(cat "$work/test-key.pem")" "$here/sign_index.sh" "$work/out" > "$work/log"
[ "$(wc -c < "$work/out/index.json.sig" | tr -d ' ')" = 64 ] || fail "signature is not 64 bytes"
grep -q 'public key ' "$work/log" || fail "no public key printed"
grep -q 'PRIVATE' "$work/log" && fail "the private key reached the log"
openssl pkeyutl -verify -pubin -inkey "$work/public.pem" -rawin -in "$work/out/index.json" \
  -sigfile "$work/out/index.json.sig" >/dev/null || fail "signature does not verify"

printf ' ' >> "$work/out/index.json"
if openssl pkeyutl -verify -pubin -inkey "$work/public.pem" -rawin -in "$work/out/index.json" \
  -sigfile "$work/out/index.json.sig" >/dev/null 2>&1; then
  fail "an edited index still verifies"
fi

rm "$work/out/index.json.sig"
if INDEX_SIGNING_KEY="" "$here/sign_index.sh" "$work/out" 2>/dev/null; then fail "signed without a key"; fi
[ ! -e "$work/out/index.json.sig" ] || fail "a signature was written without a key"

echo "test_sign_index: OK"
