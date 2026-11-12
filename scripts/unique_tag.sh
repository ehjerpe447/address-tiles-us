#!/usr/bin/env bash
# Prints a release tag that is not in a list of existing tags (INFRA-15).
#   unique_tag.sh BASE_TAG EXISTING_TAGS_FILE   (use "-" to read the list from stdin)
# The list holds one tag name per line: release tags (drafts included) and git tags. If BASE_TAG is free it is
# printed unchanged; otherwise -2, -3, ... is appended to BASE_TAG until the result is free (nad-r24-20261103,
# nad-r24-20261103-2, nad-r24-20261103-3). Matching is exact, whole-line. The suffix is always added to the base as
# given, so a base that already ends in "-2" becomes "...-2-2", never "...-3".
set -euo pipefail

if [ "$#" -ne 2 ] || [ -z "$1" ]; then
  echo "usage: unique_tag.sh BASE_TAG EXISTING_TAGS_FILE|-" >&2
  exit 2
fi
base="$1"
if [ "$2" = "-" ]; then
  existing="$(cat)"
else
  [ -r "$2" ] || { echo "unique_tag.sh: cannot read $2" >&2; exit 2; }
  existing="$(cat "$2")"
fi
# Tolerate Windows line endings.
existing="$(printf '%s\n' "$existing" | tr -d '\r')"

taken() { printf '%s\n' "$existing" | grep -Fxq -- "$1"; }

tag="$base"
n=1
while taken "$tag"; do
  n=$((n + 1))
  tag="${base}-${n}"
done
printf '%s\n' "$tag"
