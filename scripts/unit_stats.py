#!/usr/bin/env python3
"""Aggregate statistics about the unit lists in tile format v2 PMTiles files, printed as Markdown.

    scripts/unit_stats.py STATE.pmtiles [STATE.pmtiles ...]

Used by the refresh workflow's dry run to judge the unit lists on real NAD rows without publishing or attaching any
address data: it prints counts, shapes of unit labels, the most common labels and a few sample unit lists, and never an
address, street, city or coordinate. A v1 file (no unit lists) reports zero buildings with units.
"""
import gzip
import importlib.util
import json
import os
import re
import struct
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("nad_tiles", os.path.join(HERE, "nad_tiles.py"))
NT = importlib.util.module_from_spec(SPEC)
sys.modules["nad_tiles"] = NT
SPEC.loader.exec_module(NT)

BUCKETS = [(2, 9), (10, 49), (50, 299), (300, 10**9)]
SHAPES = [
    ("number", re.compile(r"^\d+$")),
    ("number+letter", re.compile(r"^\d+[A-Z]+$")),
    ("letter+number", re.compile(r"^[A-Z]+\d+$")),
    ("letter", re.compile(r"^[A-Z]+$")),
    ("with dash", re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")),
]


def iter_tiles(raw):
    """Every tile in a PMTiles v3 archive as a dict, following leaf directories."""
    root_off, root_len, _, _, leaf_off = struct.unpack("<QQQQQ", raw[8:48])
    data_off = struct.unpack("<Q", raw[56:64])[0]
    stack = [NT.decode_directory(raw[root_off:root_off + root_len])]
    while stack:
        for _tile_id, offset, length, run in stack.pop():
            if run == 0:
                stack.append(NT.decode_directory(raw[leaf_off + offset:leaf_off + offset + length]))
            else:
                yield json.loads(gzip.decompress(raw[data_off + offset:data_off + offset + length]))


def shape_of(token):
    for name, pattern in SHAPES:
        if pattern.match(token):
            return name
    return "other"


def summarize(path):
    """Counts only. Sample lists carry unit labels, never addresses."""
    with open(path, "rb") as handle:
        raw = handle.read()
    rows = buildings = tokens_total = 0
    sizes = Counter()
    shapes = Counter()
    common = Counter()
    samples = []
    version = 0
    for tile in iter_tiles(raw):
        version = max(version, tile.get("v", 1))
        for row in tile["a"]:
            rows += 1
            if len(row) < 6:
                continue
            units = row[5]
            buildings += 1
            tokens_total += len(units)
            for low, high in BUCKETS:
                if low <= len(units) <= high:
                    sizes[(low, high)] += 1
            for token in units:
                shapes[shape_of(token)] += 1
                common[token] += 1
            if len(samples) < 12 and buildings % 997 == 1:
                samples.append(units[:8])
    return {"file": os.path.basename(path), "version": version, "rows": rows, "buildings": buildings,
            "tokens": tokens_total, "sizes": sizes, "shapes": shapes, "common": common.most_common(12),
            "samples": samples}


def render(stats):
    lines = ["### Unit lists (tile format v2), counts only", ""]
    lines.append("| State file | Tile version | Addresses | Buildings with units | Unit labels | At the 300 cap |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for entry in stats:
        capped = entry["sizes"].get((300, 10**9), 0)
        lines.append(f"| {entry['file']} | {entry['version']} | {entry['rows']} | {entry['buildings']} | "
                     f"{entry['tokens']} | {capped} |")
    total = Counter()
    shapes = Counter()
    common = Counter()
    for entry in stats:
        total.update(entry["sizes"])
        shapes.update(entry["shapes"])
        common.update(dict(entry["common"]))
    lines += ["", "Buildings by number of units: " + ", ".join(
        f"{low}-{high if high < 10**9 else 'more'}: {total.get((low, high), 0)}" for low, high in BUCKETS)]
    lines.append("Label shapes: " + ", ".join(f"{name}: {count}" for name, count in shapes.most_common()))
    lines.append("Most common labels (junk shows up here): "
                 + ", ".join(f"`{token}` {count}" for token, count in common.most_common(15)))
    lines += ["", "Sample unit lists (labels only, first 8, from buildings picked by position, not by address), "
              "for the six files with the most buildings:"]
    for entry in sorted(stats, key=lambda e: -e["buildings"])[:6]:
        for sample in entry["samples"][:2]:
            lines.append(f"- {entry['file']}: " + ", ".join(sample))
    return "\n".join(lines)


def main(argv):
    if not argv:
        sys.exit("usage: unit_stats.py STATE.pmtiles [...]")
    print(render([summarize(path) for path in argv]))


if __name__ == "__main__":
    main(sys.argv[1:])
