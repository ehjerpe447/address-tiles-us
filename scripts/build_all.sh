#!/usr/bin/env bash
# Builds every state's address file from the NAD zip and writes index.json. Publishes nothing. This is exactly what the
# weekly workflow runs, so the same command on a Mac is the manual fallback.
#
#   scripts/build_all.sh NAD.zip OUT_DIR "NAD r24" "<ETag of the download>" "https://host/path/" [--units]
#
# Needs 7-Zip (`brew install sevenzip`; the zip is Deflate64, which unzip cannot read) and Python 3.9+. Disk: the slim
# per-state CSVs are about 4 GB; each is deleted after its state is built unless KEEP_CSV=1. Memory: a state is held in
# memory while it builds (Texas is the largest, about 7 GB estimated), so the four biggest build one at a time and the
# rest JOBS at a time (default 2). Works with macOS's bash 3.2.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
zip="$1"; out="$2"; release="$3"; etag="$4"; base_url="$5"; units="${6:-}"
jobs="${JOBS:-2}"
[ -z "$units" ] || [ "$units" = "--units" ] || { echo "sixth argument must be --units or empty"; exit 2; }
command -v 7zz >/dev/null || { echo "7zz not found: brew install sevenzip"; exit 1; }
entry="$(7zz l -ba "$zip" | awk '{print $NF}' | grep -E 'TXT/.*\.txt$' | head -1)"
[ -n "$entry" ] || { echo "no .txt data file in $zip"; exit 1; }
mkdir -p "$out/by-state" "$out/tiles"

echo "== splitting by state"
7zz x -so -bd -bso0 -bsp0 "$zip" "$entry" | python3 -I "$here/nad_tiles.py" split "$out/by-state" | tee "$out/state-rows.txt"

[ "${DELETE_ZIP:-0}" != "1" ] || rm -f "$zip" # the zip is not needed again; frees 8.5 GB on a small runner

# Largest first, so the memory-hungry states never overlap.
list="$out/state-order.txt"
(cd "$out/by-state" && ls -S ./*.csv) | sed 's|^\./||' | grep -v '^UNKNOWN\.csv$' > "$list"
echo "== building $(wc -l < "$list" | tr -d ' ') states"
while read -r f; do
  "$here/build_state.sh" "$out/tiles" "${f%.csv}" "$out/by-state/$f" "$release" "$units"
done < <(head -4 "$list")
# shellcheck disable=SC2016  # the single-quoted script is meant to expand inside sh, not here
tail -n +5 "$list" | xargs -P "$jobs" -I{} sh -c \
  'f="{}"; exec "$1/build_state.sh" "$2/tiles" "${f%.csv}" "$2/by-state/$f" "$3" "$4"' _ "$here" "$out" "$release" "$units"

echo "== index.json"
python3 -I "$here/nad_tiles.py" index "$out/tiles" --release "$release" --etag "$etag" --base-url "$base_url"
