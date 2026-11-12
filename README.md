# Address data

Per-state address files built from the U.S. Department of Transportation National Address Database (NAD), for apps
that read a small part of a state's addresses with HTTP Range requests instead of downloading the whole state.

This repository holds only the build scripts, a scheduled workflow, and (once published) release assets. There is no
application code here and no personal data.

## What a release contains

| File | What it is |
| --- | --- |
| `<STATE>.pmtiles` | One [PMTiles v3](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md) archive per state. Zoom-12 tiles of gzip-compressed JSON rows `[number, street, city, lat, lon]`. With tile format v2 a building with two or more distinct units has a sixth item, the sorted list of its unit labels. |
| `index.json` | `schema`, `release` (for example "NAD r24"), `builtAt`, `nadEtag`, `notice`, `baseURL`, `tileVersion`, one entry per state (`code`, `file`, `bytes`, `addressCount`, `bbox` as `[minLon, minLat, maxLon, maxLat]`, `counties`, and optionally `coverage`: `housingUnits`, `rowsPerHousingUnit` and a `class` of fine, partial, thin or nearlyEmpty from the bundled Census table `data/census-housing-units-2025.csv`), `coverageSource`, and `statesWithoutRows`. |
| `<STATE>.pmtiles.blocks` | SEC-10: the SHA-256 of every 16 KiB block of that state's archive, 32 bytes each, in order (the last block may be short). `index.json` names it in each state's `blocks` (`file`, `sha256`, `blockSize`). The app checks every block it reads against this list. |
| `index.json.sig` | SEC-10: a 64-byte Ed25519 signature over the exact bytes of `index.json`, made by `scripts/sign_index.sh` with the `INDEX_SIGNING_KEY` repository secret. The app checks it with the public key it ships with and refuses the data if it does not match. |
| `coverage.json` | Per-county counts for coverage audits, kept out of `index.json` so the file apps download stays small: `schema`, `release`, `builtAt`, `notice`, and `states`, mapping each state code to a list of `{county, rows, datedRows, newestUpdate, rowsByYear}`. `rows` counts addresses the way `addressCount` does; the date fields come from NAD's `DateUpdate` (empty and `1899-12-30` dates are left out). The county name is as NAD spells it; rows with no county are under `""`. |

The newest release is the one GitHub marks "Latest", so `releases/latest/download/index.json` always finds the
current index, and `baseURL` inside it says where that release's files are.

## Licence and notice: read before publishing anything

NAD is a work of the U.S. Department of Transportation compiled from state, local and tribal data. It is provided as is,
with no warranty of accuracy or completeness, and it is not for use as a mailing list. The same notice is written into
every `index.json` and every archive.

**How publishing works (the owner decided on 2026-11-02; the first release, NAD r24, is out):** the open questions about
NAD's licence were answered in the app's backlog (`SEC-4`), and the owner approves each release.

- A manual run has "Dry run" ticked by default and publishes nothing. Unticked, it publishes; with "draft" ticked it
  creates a draft instead (to test that path).
- The weekly run builds when the NAD download changed and creates a **draft** release, then opens an issue telling the
  owner it is waiting. Nothing is public until the owner publishes the draft (leave "Set as the latest release" ticked).
  While a draft is waiting, further weekly runs stop, so drafts do not pile up.
- If the repository variable `PUBLISH_ENABLED` is exactly `true`, the weekly run publishes straight away instead. It is
  unset; leave it unset unless automatic publishing is wanted.

## The weekly workflow

`.github/workflows/refresh.yml`:

1. Frees disk space on the runner.
2. Sends a `HEAD` request for the NAD download and compares its `ETag` with the one recorded in the last release's
   `index.json`. Unchanged means the run ends there.
3. Downloads the zip, streams it through `7zz` (the zip uses Deflate64), splits the rows by state into slim CSVs, and
   builds one archive per state, largest states first and one at a time.
4. Writes `index.json`, checks it, and writes a summary table to the run page.
5. Only when not a dry run: creates a release with the archives and `index.json`: published and marked Latest, or a
   draft (weekly runs), and then opens the issue.

It uses no secrets beyond the built-in `GITHUB_TOKEN`, and only the release and issue steps use it. Actions are pinned
to full commit SHAs.

## Manual fallback (the same script, on a Mac)

```bash
brew install sevenzip
scripts/build_all.sh ~/Downloads/NAD/TXT.zip ~/Downloads/NAD/out "NAD r24" "<ETag of the download>" \
  "https://github.com/OWNER/REPO/releases/download/TAG/"        # --units (tile format v2) is on by default in the workflow
```

This writes `out/tiles/*.pmtiles` and `out/tiles/index.json` and publishes nothing. Needs about 15 GB of free disk and
enough memory for the largest state (Texas is estimated at 7 GB). Set `KEEP_CSV=1` to keep the per-state CSVs, and
`JOBS=4` to build more states at once. Publishing by hand is `gh release create TAG out/tiles/*.pmtiles
out/tiles/index.json --latest`, after the same approval as above.

## Tests

```bash
python3 -I scripts/test_nad_tiles.py     # standard library only
python3 -I scripts/test_coverage_audit.py
```

The tests build from small synthetic NAD-shaped CSV files, check the output is byte-for-byte repeatable, check the
`index.json` fields, and check tile format v2 unit lists.

## Files

| Path | Role |
| --- | --- |
| `scripts/nad_tiles.py` | `split`, `build`, `index` and `query`; self-contained, standard library only |
| `scripts/build_all.sh`, `scripts/build_state.sh` | The whole build, and one state of it |
| `scripts/test_nad_tiles.py` | Tests |
| `scripts/coverage_audit.py`, `scripts/test_coverage_audit.py` | County coverage audit against Census housing units, and its tests. Not yet in `test.yml` (joins at the next sync, with the owner's go-ahead) |
| `.github/workflows/refresh.yml` | The weekly refresh |
| `.github/workflows/test.yml` | Runs the tests on every push |
