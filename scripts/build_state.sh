#!/usr/bin/env bash
# Builds one state's address file from its slim CSV (as written by `nad_tiles.py split`).
#
#   scripts/build_state.sh OUT_TILES_DIR IL OUT_BY_STATE/IL.csv "NAD r24" [--units]
#
# Deletes the CSV afterwards to free disk unless KEEP_CSV=1.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tiles="$1"; code="$2"; csv="$3"; release="$4"; units="${5:-}"
args=(build "$csv" "$tiles/$code.pmtiles" --state "$code" --release "$release")
[ -z "$units" ] || args+=("$units")
python3 -I "$here/nad_tiles.py" "${args[@]}"
[ "${KEEP_CSV:-0}" = "1" ] || rm -f "$csv"
