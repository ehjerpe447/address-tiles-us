#!/usr/bin/env bash
# SEC-10: signs DIR/index.json with the release's Ed25519 key and writes DIR/index.json.sig, a detached 64-byte
# signature over the exact bytes of index.json. The app checks it with the public half it ships with, then trusts the
# per-state block hashes the index names (DIR/*.pmtiles.blocks, written by `nad_tiles.py index`).
#
# The private key arrives only in the environment variable INDEX_SIGNING_KEY (PEM, "BEGIN PRIVATE KEY"), from a
# repository secret. It is written to a 0600 file in a private temp folder for openssl and removed on exit; it is never
# printed. With no key the script fails, so a release cannot go out unsigned by accident.
#
# Usage: INDEX_SIGNING_KEY="$(cat key.pem)" scripts/sign_index.sh DIR
set -euo pipefail

dir="${1:?usage: sign_index.sh DIR}"
index="$dir/index.json"
[ -f "$index" ] || { echo "sign_index: $index is missing" >&2; exit 1; }
[ -n "${INDEX_SIGNING_KEY:-}" ] || { echo "sign_index: INDEX_SIGNING_KEY is not set; refusing to publish unsigned" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
umask 077
printf '%s\n' "$INDEX_SIGNING_KEY" > "$work/key.pem"

openssl pkeyutl -sign -inkey "$work/key.pem" -rawin -in "$index" -out "$dir/index.json.sig"
openssl pkey -in "$work/key.pem" -pubout -out "$work/public.pem"
# Check the signature with the public half before anything is published.
openssl pkeyutl -verify -pubin -inkey "$work/public.pem" -rawin -in "$index" -sigfile "$dir/index.json.sig" >/dev/null

size="$(wc -c < "$dir/index.json.sig" | tr -d ' ')"
[ "$size" = 64 ] || { echo "sign_index: expected a 64-byte Ed25519 signature, got $size bytes" >&2; exit 1; }
# The raw 32-byte public key, base64, as the app embeds it: compare it with AddressSigningKey in the app.
public_b64="$(openssl pkey -pubin -in "$work/public.pem" -outform DER | tail -c 32 | base64)"
echo "sign_index: signed $index; public key $public_b64"
