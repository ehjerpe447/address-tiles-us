#!/usr/bin/env python3
"""Builds per-state address files from the National Address Database (NAD). Standard library only; self-contained.

    nad_tiles.py split  OUT_DIR                  < NAD.txt     one slim CSV per state (reads standard input)
    nad_tiles.py build  STATE.csv OUT.pmtiles --state IL --release "NAD r24" [--units]
    nad_tiles.py index  DIR --release "NAD r24" --etag ETAG --base-url URL [--built-at YYYY-MM-DD] [--expected IL,WI]
    nad_tiles.py query  OUT.pmtiles MIN_LON MIN_LAT MAX_LON MAX_LAT

`split` keeps only the columns the builder reads, so the per-state files add up to about 4 GB instead of the 40+ GB
NAD text file. `build` writes one PMTiles v3 archive (zoom-12 Web Mercator tiles of gzip'd JSON rows
`[number, street, city, lat, lon]`, plus, with --units, a sixth item: the sorted unit labels of a building that has 2 or
more distinct units) and `OUT.stats.json` beside it. `index` reads the stats files and the archive sizes in DIR and
writes DIR/index.json. Output is deterministic: sorted rows, fixed rounding, gzip mtime 0, no clock reads except the
`--built-at` default.

Format reference: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
Tile version 1 rows have 5 items; version 2 (`"v":2`, only with --units) may append the sixth.
"""
import argparse
import csv
import datetime
import glob
import gzip
import io
import json
import math
import os
import re
import struct
import sys
import urllib.request
from collections import defaultdict

csv.field_size_limit(1 << 24)

ZOOM = 12
HEADER_LEN = 127
ROOT_MAX = 16384 - HEADER_LEN  # spec: header plus compressed root directory fit in the first 16 KB
INDEX_SCHEMA = 1
NOTICE = ("Addresses come from the National Address Database (NAD), a work of the U.S. Department of "
          "Transportation compiled from state, local and tribal data, provided as is with no warranty of accuracy "
          "or completeness. Not for use as a mailing list.")
# Where a state file is expected; a code in this list with no built file is reported as having no rows.
ALL_STATES = ("AK AL AR AS AZ CA CO CT DC DE FL GA GU HI IA ID IL IN KS KY LA MA MD ME MI MN MO MP MS MT NC ND NE NH "
              "NJ NM NV NY OH OK OR PA PR RI SC SD TN TX UT VA VI VT WA WI WV WY").split()

ALIASES = {
    # Add_Number is the number on the door; AddNo_Full can carry a county grid prefix ("0N 606" for 606).
    "number": ["add_number", "addno_full", "number", "house_number", "housenumber"],
    "numsuf": ["addnum_suf"],
    "street": ["stnam_full", "street", "street_name", "streetname", "st_name"],
    "state": ["state"],
    "city": ["post_city", "city"],
    "muni": ["inc_muni", "uninc_comm"],
    "lat": ["latitude", "lat", "y"],
    "lon": ["longitude", "lon", "lng", "long", "x"],
    "unit": ["unit"], "subaddress": ["subaddress"], "county": ["county"],
    "address": ["address", "full_address", "fulladdress", "street_address", "addr"],
    "predir": ["st_predir"], "pretyp": ["st_pretyp"], "name": ["st_name"], "postyp": ["st_postyp"],
    "posdir": ["st_posdir"],
}


# --- rows -------------------------------------------------------------------------------------------------------

def find_columns(header):
    lowered = [h.strip().lower() for h in header]
    found = {}
    for key, names in ALIASES.items():
        for name in names:
            if name in lowered:
                found[key] = lowered.index(name)
                break
    return found


def cell(row, columns, key):
    index = columns.get(key)
    return row[index].strip() if index is not None and index < len(row) else ""


def tidy_street(street):
    return " ".join(w.capitalize() if w.isupper() and len(w) > 2 else w for w in street.split())


def parse_row(row, columns):
    """Returns (number, street, city, lat, lon) or None when the row is unusable."""
    try:
        lat, lon = float(cell(row, columns, "lat")), float(cell(row, columns, "lon"))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    number = (cell(row, columns, "number") + cell(row, columns, "numsuf")).strip()
    street = cell(row, columns, "street")
    if not street and "name" in columns:
        parts = [cell(row, columns, k) for k in ("predir", "pretyp", "name", "postyp", "posdir")]
        street = " ".join(p for p in parts if p)
    if (not number or not street) and cell(row, columns, "address"):
        first, _, rest = cell(row, columns, "address").partition(" ")
        number, street = number or first, street or rest.strip()
    if not number or not street:
        return None
    city = next((c for c in (cell(row, columns, "city"), cell(row, columns, "muni"))
                 if c and c.lower() != "not stated"), "")
    return number, tidy_street(street), city, round(lat, 6), round(lon, 6)


UNIT_PREFIX = re.compile(r"^(?:APARTMENT|APT|UNIT|SUITE|STE|ROOM|RM)(?:[.\s#]+|(?=\d)|$)|^#\s*")
UNIT_JUNK = {"N/A", "NA", "NONE", "NULL", "UNKNOWN", "NOT STATED"}
MAX_UNIT_LENGTH = 8  # a longer "unit" is free text, not a door label
MAX_UNITS_PER_BUILDING = 300


def unit_token(raw):
    """Canonical unit label ("Apt 3B", "#3b", "UNIT 3B" all give "3B"), or None for junk."""
    token = " ".join(raw.upper().split())
    previous = None
    while previous != token:  # "UNIT #4" has two prefixes
        previous = token
        token = UNIT_PREFIX.sub("", token).strip()
    if not token or len(token) > MAX_UNIT_LENGTH or token in UNIT_JUNK or not any(c.isalnum() for c in token):
        return None
    if re.search(r"\bBOX\b", token):  # "1 BOX 4": a rural mailbox, not an apartment
        return None
    return token


def row_unit(row, columns):
    return unit_token(cell(row, columns, "unit") or cell(row, columns, "subaddress"))


# --- split ------------------------------------------------------------------------------------------------------

def split(out_dir, stream=None):
    """Standard input NAD CSV -> OUT_DIR/<STATE>.csv holding only the columns the builder reads."""
    os.makedirs(out_dir, exist_ok=True)
    reader = csv.reader(io.TextIOWrapper(stream or sys.stdin.buffer, encoding="utf-8-sig", newline=""))
    header = next(reader)
    columns = find_columns(header)
    if "state" not in columns:
        sys.exit("error: the input has no State column")
    keep = sorted(set(columns.values()))
    state_i = columns["state"]
    handles, writers, counts = {}, {}, {}
    try:
        for row in reader:
            state = (row[state_i].strip().upper() if state_i < len(row) else "") or "UNKNOWN"
            if state not in writers:
                handles[state] = open(os.path.join(out_dir, f"{state}.csv"), "w", newline="", encoding="utf-8",
                                      buffering=1 << 20)
                writers[state] = csv.writer(handles[state])
                writers[state].writerow([header[i] for i in keep])
                counts[state] = 0
            writers[state].writerow([row[i] if i < len(row) else "" for i in keep])
            counts[state] += 1
    finally:
        for handle in handles.values():
            handle.close()
    for state, count in sorted(counts.items()):
        print(f"{state} {count}")


# --- PMTiles ----------------------------------------------------------------------------------------------------

def lonlat_to_xy(lon, lat, z=ZOOM):
    n = 1 << z
    lat = max(min(lat, 85.0511), -85.0511)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def zxy_to_tileid(z, x, y):
    """Hilbert curve position, counted cumulatively from zoom 0 (PMTiles v3 spec)."""
    acc = ((1 << (2 * z)) - 1) // 3
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = n - 1 - x, n - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def _varint(value, out):
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def encode_directory(entries):
    """entries: [(tile_id, offset, length, run_length)] sorted by tile_id; run_length 0 marks a leaf pointer."""
    out = bytearray()
    _varint(len(entries), out)
    last = 0
    for tile_id, _, _, _ in entries:
        _varint(tile_id - last, out)
        last = tile_id
    for _, _, _, run in entries:
        _varint(run, out)
    for _, _, length, _ in entries:
        _varint(length, out)
    for i, (_, offset, _, _) in enumerate(entries):
        if i > 0 and offset == entries[i - 1][1] + entries[i - 1][2]:
            _varint(0, out)
        else:
            _varint(offset + 1, out)
    return gzip.compress(bytes(out), compresslevel=9, mtime=0)


def decode_directory(data):
    raw = gzip.decompress(data)
    pos = 0

    def read():
        nonlocal pos
        shift = result = 0
        while True:
            byte = raw[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7

    count = read()
    ids, last = [], 0
    for _ in range(count):
        last += read()
        ids.append(last)
    runs = [read() for _ in range(count)]
    lengths = [read() for _ in range(count)]
    offsets = []
    for i in range(count):
        v = read()
        offsets.append(offsets[i - 1] + lengths[i - 1] if v == 0 and i > 0 else v - 1)
    return list(zip(ids, offsets, lengths, runs))


def _position(lon, lat):
    return struct.pack("<ii", int(round(lon * 10_000_000)), int(round(lat * 10_000_000)))


def stats_path(out_path):
    return os.path.splitext(out_path)[0] + ".stats.json"


def build(input_path, out_path, release, state, units=False):
    """Writes OUT.pmtiles and OUT.stats.json; returns the stats dict."""
    tiles = defaultdict(dict)
    unit_sets = defaultdict(set)  # (tile xy, building key) -> unit tokens; only filled with units=True
    counties = set()
    skipped = 0
    lon_min = lat_min = 180.0
    lon_max = lat_max = -180.0
    with open(input_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        columns = find_columns(next(reader, []))
        if "lat" not in columns or "lon" not in columns:
            sys.exit("error: the input needs latitude and longitude columns")
        # A State column filters; an input without one (a demo CSV) is taken whole and labelled with --state.
        filter_state = bool(state) and "state" in columns
        for row in reader:
            if filter_state and cell(row, columns, "state").upper() != state:
                continue
            parsed = parse_row(row, columns)
            if parsed is None:
                skipped += 1
                continue
            number, street, city, lat, lon = parsed
            lon_min, lon_max = min(lon_min, lon), max(lon_max, lon)
            lat_min, lat_max = min(lat_min, lat), max(lat_max, lat)
            county = cell(row, columns, "county")
            if county:
                counties.add(county)
            xy, key = lonlat_to_xy(lon, lat), (street.lower(), number.lower(), city.lower())
            tiles[xy].setdefault(key, parsed)  # the building is the dedupe key; the first row's point wins
            if units:
                token = row_unit(row, columns)
                if token:
                    unit_sets[(xy, key)].add(token)

    blobs, entries, offset, total = [], [], 0, 0
    for tile_id, (x, y) in sorted((zxy_to_tileid(ZOOM, x, y), (x, y)) for x, y in tiles):
        keys = sorted(tiles[(x, y)])
        rows = [list(tiles[(x, y)][k]) for k in keys]
        if units:
            for row, key in zip(rows, keys):
                found = unit_sets.get(((x, y), key), ())
                if len(found) >= 2:
                    row.append(sorted(found)[:MAX_UNITS_PER_BUILDING])
        total += len(rows)
        blob = gzip.compress(json.dumps({"v": 2 if units else 1, "z": ZOOM, "x": x, "y": y, "a": rows},
                                        separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                             compresslevel=9, mtime=0)
        blobs.append(blob)
        entries.append((tile_id, offset, len(blob), 1))
        offset += len(blob)

    root = encode_directory(entries)
    leaves = b""
    leaf_size = 4096
    while len(root) > ROOT_MAX:
        leaf_entries, chunks, leaf_offset = [], [], 0
        for i in range(0, len(entries), leaf_size):
            chunk = encode_directory(entries[i:i + leaf_size])
            leaf_entries.append((entries[i][0], leaf_offset, len(chunk), 0))
            chunks.append(chunk)
            leaf_offset += len(chunk)
        root, leaves = encode_directory(leaf_entries), b"".join(chunks)
        leaf_size *= 2

    if not blobs:  # an empty state has no file: the index lists it as having no rows
        print(f"{state or '?'}: no usable rows ({skipped} skipped); nothing written")
        return None
    meta = gzip.compress(json.dumps({
        "name": "NAD addresses", "format": f"address-tile-v{2 if units else 1} (gzip JSON)",
        "release": release, "states": [state] if state else [], "addressCount": total, "tileCount": len(blobs),
        "notice": NOTICE}, sort_keys=True, ensure_ascii=False).encode("utf-8"), compresslevel=9, mtime=0)

    root_offset = HEADER_LEN
    meta_offset = root_offset + len(root)
    leaf_offset = meta_offset + len(meta)
    data_offset = leaf_offset + len(leaves)
    header = b"PMTiles" + bytes([3]) + struct.pack(
        "<QQQQQQQQQQQ", root_offset, len(root), meta_offset, len(meta), leaf_offset, len(leaves),
        data_offset, offset, len(blobs), len(blobs), len(blobs))
    header += bytes([1, 2, 2, 0, ZOOM, ZOOM])  # clustered, gzip internal, gzip tiles, tile type unknown, min/max zoom
    header += _position(lon_min, lat_min) + _position(lon_max, lat_max)
    header += bytes([ZOOM]) + _position((lon_min + lon_max) / 2, (lat_min + lat_max) / 2)
    assert len(header) == HEADER_LEN
    with open(out_path, "wb") as out:
        out.write(header + root + meta + leaves)
        for blob in blobs:
            out.write(blob)
    stats = {"code": state or "", "file": os.path.basename(out_path), "bytes": os.path.getsize(out_path),
             "addressCount": total, "tileCount": len(blobs), "tileVersion": 2 if units else 1,
             "bbox": [round(lon_min, 6), round(lat_min, 6), round(lon_max, 6), round(lat_max, 6)],
             "counties": sorted(counties)}
    with open(stats_path(out_path), "w", encoding="utf-8") as out:
        json.dump(stats, out, indent=1, sort_keys=True, ensure_ascii=False)
        out.write("\n")
    print(f"{state or '?'}: {total} addresses in {len(blobs)} z{ZOOM} tiles; {skipped} rows skipped; "
          f"{stats['bytes']} B; wrote {out_path}")
    return stats


# --- index.json -------------------------------------------------------------------------------------------------

def make_index(directory, release, etag, base_url, built_at, expected):
    stats = []
    for path in sorted(glob.glob(os.path.join(directory, "*.stats.json"))):
        with open(path, encoding="utf-8") as handle:
            item = json.load(handle)
        archive = os.path.join(directory, item["file"])
        if not os.path.isfile(archive):
            sys.exit(f"error: {path} names {item['file']}, which is missing")
        item["bytes"] = os.path.getsize(archive)  # the published size, whatever the stats file said
        stats.append(item)
    stats.sort(key=lambda s: s["code"])
    versions = {s["tileVersion"] for s in stats}
    if len(versions) > 1:
        sys.exit("error: states were built with different tile versions; rebuild them all the same way")
    built = {s["code"] for s in stats}
    index = {
        "schema": INDEX_SCHEMA, "release": release, "builtAt": built_at, "nadEtag": etag, "notice": NOTICE,
        "baseURL": base_url, "tileVersion": versions.pop() if versions else 1,
        "states": [{"code": s["code"], "file": s["file"], "bytes": s["bytes"], "addressCount": s["addressCount"],
                    "bbox": s["bbox"], "counties": s["counties"]} for s in stats],
        "statesWithoutRows": sorted(set(expected) - built),
    }
    target = os.path.join(directory, "index.json")
    with open(target, "w", encoding="utf-8") as out:
        json.dump(index, out, indent=1, sort_keys=True, ensure_ascii=False)
        out.write("\n")
    print(f"wrote {target}: {len(stats)} states, {len(index['statesWithoutRows'])} without rows")
    return index


# --- query (the same walk a phone does; used by the tests and for spot checks) ------------------------------------

def read_tile_rows(path, min_lon, min_lat, max_lon, max_lat):
    """Rows inside the box, read from a local archive or an https URL with HTTP Range requests."""
    def read(offset, length):
        if path.startswith("https://"):
            request = urllib.request.Request(path, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    head = read(0, 16384)
    if head[:7] != b"PMTiles" or head[7] != 3:
        sys.exit("error: not a PMTiles v3 archive")
    root_off, root_len, _, _, leaf_off = struct.unpack("<QQQQQ", head[8:48])
    data_off = struct.unpack("<Q", head[56:64])[0]
    x0, y0 = lonlat_to_xy(min_lon, max_lat)
    x1, y1 = lonlat_to_xy(max_lon, min_lat)
    found = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            tile_id, directory = zxy_to_tileid(ZOOM, x, y), decode_directory(head[root_off:root_off + root_len])
            for _ in range(3):
                match = None
                for entry in directory:
                    if entry[0] > tile_id:
                        break
                    match = entry
                if match is None:
                    break
                if match[3] == 0:
                    directory = decode_directory(read(leaf_off + match[1], match[2]))
                    continue
                if tile_id < match[0] + match[3]:
                    tile = json.loads(gzip.decompress(read(data_off + match[1], match[2])))
                    found += [r for r in tile["a"] if min_lat <= r[3] <= max_lat and min_lon <= r[4] <= max_lon]
                break
    return found


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("split")
    p.add_argument("out_dir")
    p = sub.add_parser("build")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--state", required=True, help="two-letter code, for example IL")
    p.add_argument("--release", default="unlabelled extract")
    p.add_argument("--units", action="store_true", help="tile format v2: add unit lists for multi-unit buildings")
    p = sub.add_parser("index")
    p.add_argument("directory")
    p.add_argument("--release", required=True)
    p.add_argument("--etag", required=True)
    p.add_argument("--base-url", required=True, help="where the files are served from, ending in a slash")
    p.add_argument("--built-at", help="ISO date; default is SOURCE_DATE_EPOCH or today (UTC)")
    p.add_argument("--expected", default=",".join(ALL_STATES), help="comma-separated codes that should have a file")
    p = sub.add_parser("query")
    p.add_argument("archive")
    p.add_argument("box", nargs=4, type=float)
    args = parser.parse_args(argv)
    if args.command == "split":
        split(args.out_dir)
    elif args.command == "build":
        build(args.input, args.output, args.release, args.state.upper(), args.units)
    elif args.command == "index":
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        when = (datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc) if epoch
                else datetime.datetime.now(datetime.timezone.utc)).date().isoformat()
        expected = [c.strip().upper() for c in args.expected.split(",") if c.strip()]
        make_index(args.directory, args.release, args.etag, args.base_url, args.built_at or when, expected)
    else:
        print(json.dumps({"addressesInBox": len(read_tile_rows(args.archive, *args.box))}))


if __name__ == "__main__":
    main(sys.argv[1:])
