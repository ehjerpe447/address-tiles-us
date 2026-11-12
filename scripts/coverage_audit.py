#!/usr/bin/env python3
"""County-level coverage audit of a published address release (FUNC-60). Standard library only.

    python3 -I scripts/coverage_audit.py INDEX.json CO-EST2025-HU.xlsx [--coverage coverage.json] [--csv OUT.csv]

INDEX.json is a release's `index.json` (its per-state `counties` list and `addressCount`). CO-EST2025-HU.xlsx is the
Census Bureau's Vintage 2025 county housing-unit estimates, read as is (the July 1, 2025 column). Prints a Markdown
report on standard output and, with --csv, writes one row per Census county. Reads no address data.

- **Absent** is exact: no NAD row in the release names the county.
- With `--coverage` (the release's `coverage.json`, written by `nad_tiles.py index` from 2026-11-06), each present county
  is classed on its own rows per housing unit: **Thin** below 0.40, then **Stale** when its newest `DateUpdate` is more
  than 24 months before the release's build date, then **Partial** below 0.75, else **Fine**.
- Without it (releases before coverage.json), a present county gets its state's NAD rows divided by the housing units
  of the state's *present* counties, so every present county in a state gets the same class, and Stale is not measured.
  A state whose coverage.json list is empty (an older stats file) falls back the same way.
"""
import argparse
import csv
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict

# Same cut points as the state classes in nad_tiles.py, so a county class and its state class read the same way.
FINE_AT = 0.75
PARTIAL_AT = 0.40
CLASSES = ("absent", "thin", "stale", "partial", "fine")
STALE_MONTHS = 24

STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

# Generic words a county name may or may not carry. "city" is not here: Virginia has both Richmond County and
# Richmond city, and Maryland and Missouri have the same pattern.
SUFFIXES = ("city and borough", "census area", "planning region", "municipality", "borough", "county", "parish")


# --- Census table ---------------------------------------------------------------------------------------------------

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_xlsx_rows(path):
    """Rows of the first sheet as lists of strings (shared strings resolved, empty cells as '')."""
    with zipfile.ZipFile(path) as book:
        shared = []
        if "xl/sharedStrings.xml" in book.namelist():
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in item.iter("{%s}t" % NS["m"])))
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iter("{%s}row" % NS["m"]):
        cells = {}
        for cell in row.findall("m:c", NS):
            column = re.match(r"[A-Z]+", cell.get("r")).group(0)
            value = cell.find("m:v", NS)
            text = "" if value is None else value.text or ""
            if cell.get("t") == "s" and text:
                text = shared[int(text)]
            elif cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter("{%s}t" % NS["m"]))
            cells[column] = text
        width = max((col_index(c) for c in cells), default=-1) + 1
        rows.append([cells.get(col_name(i), "") for i in range(width)])
    return rows


def col_index(name):
    n = 0
    for ch in name:
        n = n * 26 + ord(ch) - 64
    return n - 1


def col_name(i):
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def read_census_counties(rows):
    """[(state code, county name, housing units)] from the CO-EST2025-HU layout: '.Autauga County, Alabama' in the
    first column, the newest July 1 estimate in the last filled column. The leading dot is optional: the May 2026
    file has one row without it (Benson County, North Dakota)."""
    out = []
    for row in rows:
        if not row:
            continue
        match = re.match(r"^\.?(.+), ([^,]+)$", row[0].strip())
        if not match or match.group(2) not in STATE_CODES:
            continue
        numbers = [c for c in row[1:] if re.fullmatch(r"\d+(\.0+)?", c or "")]
        if not numbers:
            continue
        out.append((STATE_CODES[match.group(2)], match.group(1), int(float(numbers[-1]))))
    return out


# --- name matching --------------------------------------------------------------------------------------------------

def norm(name):
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bsainte\b", "ste", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def base(name):
    text = norm(name)
    for suffix in SUFFIXES:
        if text.endswith(" " + suffix):
            return text[: -len(suffix) - 1]
    return text


def match_counties(nad_names, census_names):
    """Maps each NAD county name to a Census county name of the same state, or None. Tries the full name, then the
    name without its generic suffix, then both with spaces removed ('De Kalb' / 'DeKalb'), then a unique prefix (the
    builder's county column can be cut short: 'Lower Connecticut River Valley Planning')."""
    keys = [norm, base, lambda n: base(n).replace(" ", "")]
    tables = []
    for key in keys:
        table = defaultdict(list)
        for census in census_names:
            table[key(census)].append(census)
        tables.append(table)
    result = {}
    for nad in nad_names:
        found = None
        for key, table in zip(keys, tables):
            hits = table.get(key(nad), [])
            if len(hits) == 1:
                found = hits[0]
                break
        if found is None and len(norm(nad)) >= 10:
            hits = [c for c in census_names if norm(c).startswith(norm(nad))]
            if len(hits) == 1:
                found = hits[0]
        result[nad] = found
    return result


# --- audit ----------------------------------------------------------------------------------------------------------

def classify(ratio):
    if ratio >= FINE_AT:
        return "fine"
    if ratio >= PARTIAL_AT:
        return "partial"
    return "thin"


def stale_before(built_at):
    """The date STALE_MONTHS before the build date (YYYY-MM-DD); a newest update earlier than this is stale."""
    year, month, day = (int(p) for p in built_at[:10].split("-"))
    total = year * 12 + (month - 1) - STALE_MONTHS
    return f"{total // 12:04d}-{total % 12 + 1:02d}-{min(day, 28):02d}"


def per_county(entries, mapping):
    """Sums coverage.json entries per Census county: {census name: (rows, newest update or None)}."""
    out = {}
    for entry in entries:
        census = mapping.get(entry.get("county", ""))
        if not census:
            continue
        rows, newest = out.get(census, (0, None))
        date = entry.get("newestUpdate")
        out[census] = (rows + entry.get("rows", 0), max(filter(None, (newest, date)), default=None))
    return out


def audit(index, census, coverage=None):
    """Returns (counties, states, unmatched). counties: one dict per Census county. states: one dict per state code in
    the Census table. unmatched: {state: [NAD county names with no Census county]}. With `coverage` (a coverage.json),
    present counties are classed one by one and can be stale."""
    by_state = defaultdict(list)
    for code, name, units in census:
        by_state[code].append((name, units))
    releases = {s["code"]: s for s in index.get("states", [])}
    counties, states, unmatched = [], {}, {}
    for code in sorted(by_state):
        entry = releases.get(code, {"addressCount": 0, "counties": []})
        names = [n for n, _ in by_state[code]]
        mapping = match_counties(entry.get("counties", []), names)
        present = {c for c in mapping.values() if c}
        missing = sorted(n for n, c in mapping.items() if c is None)
        if missing:
            unmatched[code] = missing
        rows = entry.get("addressCount", 0)
        units_total = sum(u for _, u in by_state[code])
        units_present = sum(u for n, u in by_state[code] if n in present)
        ratio = rows / units_present if units_present else 0.0
        present_class = classify(ratio) if units_present else None
        entries = (coverage or {}).get("states", {}).get(code) or []
        own = per_county(entries, match_counties(sorted({e.get("county", "") for e in entries}), names))
        cutoff = stale_before(coverage["builtAt"]) if entries else None
        for name, units in by_state[code]:
            row = {"state": code, "county": name, "housingUnits": units,
                   "class": present_class if name in present else "absent"}
            if entries and name in present:
                county_rows, newest = own.get(name, (0, None))
                own_class = classify(county_rows / units) if units else "fine"
                if own_class != "thin" and newest is not None and newest < cutoff:
                    own_class = "stale"
                row.update({"class": own_class, "rows": county_rows, "newestUpdate": newest})
            counties.append(row)
        states[code] = {"rows": rows, "counties": len(names), "present": len(present),
                        "unitsTotal": units_total, "unitsPresent": units_present,
                        "ratioPresent": ratio, "presentClass": present_class, "byCounty": bool(entries)}
    return counties, states, unmatched


def cluster(state):
    """How a state's gap is shaped: whole state, most of it, scattered counties, or none."""
    if state["present"] == 0:
        return "whole state absent"
    absent_share = 1 - state["unitsPresent"] / state["unitsTotal"]
    if absent_share >= 0.5:
        return "most of the state absent"
    if state["present"] < state["counties"]:
        return "scattered absent counties"
    return "every county present"


# --- report ---------------------------------------------------------------------------------------------------------

def fmt(n):
    return f"{n:,}"


def pct(part, whole):
    return f"{100 * part / whole:.1f}%" if whole else "0%"


def render(index, counties, states, unmatched, coverage=None):
    total_units = sum(c["housingUnits"] for c in counties)
    lines = [f"Release: {index.get('release', '?')}, built {index.get('builtAt', '?')}. "
             f"Census counties: {len(counties)}, housing units: {fmt(total_units)}.", ""]
    lines += ["### Counties and housing units per class", "",
              "| Class | Counties | Share of counties | Housing units | Share of housing units |",
              "| --- | ---: | ---: | ---: | ---: |"]
    if coverage:
        lines[2:2] = [f"Per-county classes from coverage.json ({coverage.get('release', '?')}); stale means the newest "
                        f"DateUpdate is before {stale_before(coverage['builtAt'])}. States without county counts fall "
                        "back to the state ratio over present counties.", ""]
    for cls in CLASSES if coverage else [c for c in CLASSES if c != "stale"]:
        group = [c for c in counties if c["class"] == cls]
        units = sum(c["housingUnits"] for c in group)
        lines.append(f"| {cls} | {fmt(len(group))} | {pct(len(group), len(counties))} | {fmt(units)} | "
                     f"{pct(units, total_units)} |")
    lines += ["", "### By state", "",
              "| State | Counties | Absent | Housing units | Absent units | NAD rows per unit, present counties | "
              "Class of present counties | Shape |",
              "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    order = sorted(states, key=lambda s: (-(states[s]["unitsTotal"] - states[s]["unitsPresent"]), s))
    for code in order:
        s = states[code]
        absent_units = s["unitsTotal"] - s["unitsPresent"]
        ratio = f"{s['ratioPresent']:.2f}" if s["unitsPresent"] else "n/a"
        lines.append(f"| {code} | {s['counties']} | {s['counties'] - s['present']} | {fmt(s['unitsTotal'])} | "
                     f"{fmt(absent_units)} | {ratio} | {s['presentClass'] or 'n/a'} | {cluster(s)} |")
    shapes = defaultdict(lambda: [0, 0, 0])
    for s in states.values():
        shape = shapes[cluster(s)]
        shape[0] += 1
        shape[1] += s["counties"] - s["present"]
        shape[2] += s["unitsTotal"] - s["unitsPresent"]
    lines += ["", "### Where the absent counties are", "",
              "| Shape | States | Absent counties | Absent housing units |", "| --- | ---: | ---: | ---: |"]
    for shape in ("whole state absent", "most of the state absent", "scattered absent counties",
                  "every county present"):
        n, c, u = shapes.get(shape, [0, 0, 0])
        lines.append(f"| {shape} | {n} | {fmt(c)} | {fmt(u)} |")
    lines += ["", "### Largest absent counties", "", "| State | County | Housing units |", "| --- | --- | ---: |"]
    absent = sorted((c for c in counties if c["class"] == "absent"), key=lambda c: -c["housingUnits"])
    for c in absent[:25]:
        lines.append(f"| {c['state']} | {c['county']} | {fmt(c['housingUnits'])} |")
    lines += ["", "### Absent counties by size (for the single-county threshold)", "",
              "| Housing units at least | Absent counties | Their housing units | Share of absent units |",
              "| ---: | ---: | ---: | ---: |"]
    absent_units = sum(c["housingUnits"] for c in absent)
    for floor in (250_000, 100_000, 50_000, 25_000, 10_000, 0):
        group = [c for c in absent if c["housingUnits"] >= floor]
        units = sum(c["housingUnits"] for c in group)
        lines.append(f"| {fmt(floor)} | {len(group)} | {fmt(units)} | {pct(units, absent_units)} |")
    lines += ["", "### NAD county names with no Census county (not counted as present)", ""]
    if not unmatched:
        lines.append("None.")
    for code in sorted(unmatched):
        lines.append(f"- {code}: " + ", ".join(unmatched[code]))
    others = sorted(set(s["code"] for s in index.get("states", [])) - set(states))
    lines += ["", f"Release states not in the Census county table (not classed): {', '.join(others) or 'none'}."]
    return "\n".join(lines)


def write_csv(path, counties):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["state", "county", "housing_units_2025_07_01", "class", "nad_rows", "newest_update"])
        for c in counties:
            writer.writerow([c["state"], c["county"], c["housingUnits"], c["class"], c.get("rows", ""),
                             c.get("newestUpdate") or ""])


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("index")
    parser.add_argument("census_xlsx")
    parser.add_argument("--coverage", help="the release's coverage.json, for per-county Thin and Stale")
    parser.add_argument("--csv")
    args = parser.parse_args(argv)
    with open(args.index, encoding="utf-8") as handle:
        index = json.load(handle)
    census = read_census_counties(read_xlsx_rows(args.census_xlsx))
    if not census:
        sys.exit("error: no county rows found in the Census table (expected '.Name County, State' rows)")
    coverage = None
    if args.coverage:
        with open(args.coverage, encoding="utf-8") as handle:
            coverage = json.load(handle)
        if coverage.get("schema") != 1 or "builtAt" not in coverage:
            sys.exit("error: coverage.json has an unknown schema or no builtAt")
    counties, states, unmatched = audit(index, census, coverage)
    if args.csv:
        write_csv(args.csv, counties)
    print(render(index, counties, states, unmatched, coverage))


if __name__ == "__main__":
    main(sys.argv[1:])
