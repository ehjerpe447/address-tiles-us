#!/usr/bin/env python3
"""Tests for coverage_audit.py. Standard library only.

    python3 -I data-repo/scripts/test_coverage_audit.py

Builds a tiny Census-shaped .xlsx and a tiny index.json in a temporary directory (no real data) and checks the county
parsing, the name matching, the classes and the report.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("coverage_audit", os.path.join(HERE, "coverage_audit.py"))
ca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ca)

# Same layout as CO-EST2025-HU: title rows, a national row, dotted county rows (one without its dot), notes.
CENSUS_ROWS = [
    ["Annual Estimates of Housing Units for Counties in the United States", "", ""],
    ["Geographic Area", "April 1, 2020 Estimates Base", "2025"],
    ["United States", "999", "1000"],
    [".Alpha County, Illinois", "90", "100"],
    [".DeKalb County, Illinois", "190", "200"],
    ["Gamma County, Illinois", "290", "300"],                    # no leading dot, like Benson County, ND
    [".Richmond County, Virginia", "40", "50"],
    [".Richmond city, Virginia", "90", "100"],
    [".Lower Connecticut River Valley Planning Region, Connecticut", "400", "400"],
    [".Clark County, Nevada", "500", "500"],
    ["Note: the estimates are based on the 2020 Census.", "", ""],
]
INDEX = {
    "release": "NAD test", "builtAt": "2026-11-06",
    "states": [
        # IL: Alpha and De Kalb present, Gamma absent; 240 rows over 300 present units = 0.80, fine.
        {"code": "IL", "addressCount": 240, "counties": ["Alpha", "De Kalb", "Nowhere"]},
        # VA: only the city is present; 30 rows over 100 units = 0.30, thin. The county must not match it.
        {"code": "VA", "addressCount": 30, "counties": ["Richmond city"]},
        # CT: a name cut short by the builder still matches by prefix; 200 over 400 = 0.50, partial.
        {"code": "CT", "addressCount": 200, "counties": ["Lower Connecticut River Valley Planning"]},
        {"code": "VI", "addressCount": 5, "counties": ["St. John Island"]},
    ],
}


def write_xlsx(path, rows):
    strings, index = [], {}
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{ca.col_name(c)}{r}"
            if value == "":
                continue
            if value.isdigit():
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                if value not in index:
                    index[value] = len(strings)
                    strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{index[value]}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(
            f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
        book.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}'
                      "</sheetData></worksheet>")


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.xlsx = os.path.join(self.tmp.name, "census.xlsx")
        self.index = os.path.join(self.tmp.name, "index.json")
        write_xlsx(self.xlsx, CENSUS_ROWS)
        with open(self.index, "w", encoding="utf-8") as handle:
            json.dump(INDEX, handle)
        self.census = ca.read_census_counties(ca.read_xlsx_rows(self.xlsx))
        self.counties, self.states, self.unmatched = ca.audit(INDEX, self.census)
        self.by_name = {(c["state"], c["county"]): c for c in self.counties}

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_every_county_row_including_one_without_a_dot(self):
        self.assertEqual(len(self.census), 7)
        self.assertIn(("IL", "Gamma County", 300), self.census)
        self.assertIn(("IL", "Alpha County", 100), self.census)  # the last column, not the 2020 base

    def test_classes(self):
        self.assertEqual(self.by_name[("IL", "Alpha County")]["class"], "fine")
        self.assertEqual(self.by_name[("IL", "DeKalb County")]["class"], "fine")
        self.assertEqual(self.by_name[("IL", "Gamma County")]["class"], "absent")
        self.assertEqual(self.by_name[("VA", "Richmond city")]["class"], "thin")
        self.assertEqual(self.by_name[("VA", "Richmond County")]["class"], "absent")
        self.assertEqual(self.by_name[("CT", "Lower Connecticut River Valley Planning Region")]["class"], "partial")
        self.assertEqual(self.by_name[("NV", "Clark County")]["class"], "absent")

    def test_state_figures_and_shape(self):
        il = self.states["IL"]
        self.assertEqual((il["counties"], il["present"], il["unitsTotal"], il["unitsPresent"]), (3, 2, 600, 300))
        self.assertAlmostEqual(il["ratioPresent"], 0.8)
        self.assertEqual(ca.cluster(il), "most of the state absent")  # half the units are in Gamma
        self.assertEqual(ca.cluster(self.states["NV"]), "whole state absent")
        self.assertEqual(ca.cluster(self.states["CT"]), "every county present")
        self.assertEqual(ca.cluster(self.states["VA"]), "scattered absent counties")

    def test_unmatched_names_are_reported_not_counted(self):
        self.assertEqual(self.unmatched, {"IL": ["Nowhere"]})

    def test_matching_rules(self):
        names = ["Fairfax County", "Fairfax city", "St. Louis County", "St. Louis city", "LaSalle County"]
        got = ca.match_counties(["Fairfax", "Fairfax City", "Saint Louis", "La Salle", "Fair"], names)
        self.assertEqual(got, {"Fairfax": "Fairfax County", "Fairfax City": "Fairfax city",
                               "Saint Louis": "St. Louis County", "La Salle": "LaSalle County", "Fair": None})

    def test_command_line_report_and_csv(self):
        out_csv = os.path.join(self.tmp.name, "out.csv")
        result = subprocess.run([sys.executable, "-I", os.path.join(HERE, "coverage_audit.py"), self.index,
                                 self.xlsx, "--csv", out_csv], capture_output=True, text=True, check=True)
        self.assertIn("| absent | 3 |", result.stdout)
        self.assertIn("- IL: Nowhere", result.stdout)
        self.assertIn("not classed): VI.", result.stdout)
        with open(out_csv, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(lines[0], "state,county,housing_units_2025_07_01,class,nad_rows,newest_update")
        self.assertNotIn("| stale |", result.stdout)  # not measured without coverage.json
        self.assertEqual(len(lines), 8)

    def test_per_county_classes_and_stale_from_coverage_json(self):
        coverage = {"schema": 1, "release": "NAD test", "builtAt": "2026-11-06", "states": {
            "IL": [
                {"county": "Alpha", "rows": 90, "newestUpdate": "2025-10-30"},     # 0.90, fresh: fine
                {"county": "De Kalb", "rows": 100, "newestUpdate": "2024-11-05"},  # 0.50 before the cutoff: stale
                {"county": "Nowhere", "rows": 50, "newestUpdate": "2026-01-01"},   # no Census county: ignored
            ],
            "VA": [{"county": "Richmond city", "rows": 30, "newestUpdate": "2010-01-01"}],  # 0.30: thin wins
            # CT has no entries (an older stats file): falls back to the state ratio, partial.
        }}
        counties, states, _ = ca.audit(INDEX, self.census, coverage)
        by_name = {(c["state"], c["county"]): c for c in counties}
        self.assertEqual(ca.stale_before("2026-11-06"), "2024-11-06")
        self.assertEqual(by_name[("IL", "Alpha County")]["class"], "fine")
        self.assertEqual(by_name[("IL", "DeKalb County")]["class"], "stale")
        self.assertEqual(by_name[("IL", "DeKalb County")]["newestUpdate"], "2024-11-05")
        self.assertEqual(by_name[("IL", "Gamma County")]["class"], "absent")
        self.assertEqual(by_name[("VA", "Richmond city")]["class"], "thin")
        self.assertEqual(by_name[("CT", "Lower Connecticut River Valley Planning Region")]["class"], "partial")
        self.assertTrue(states["IL"]["byCounty"])
        self.assertFalse(states["CT"]["byCounty"])
        report = ca.render(INDEX, counties, states, {}, coverage)
        self.assertIn("| stale | 1 |", report)
        self.assertIn("before 2024-11-06", report)

    def test_rows_of_two_nad_spellings_add_up(self):
        mapping = {"De Kalb": "DeKalb County", "DeKalb": "DeKalb County"}
        got = ca.per_county([{"county": "De Kalb", "rows": 5, "newestUpdate": "2020-01-01"},
                             {"county": "DeKalb", "rows": 7, "newestUpdate": None}], mapping)
        self.assertEqual(got, {"DeKalb County": (12, "2020-01-01")})

    def test_empty_table_fails_loudly(self):
        empty = os.path.join(self.tmp.name, "empty.xlsx")
        write_xlsx(empty, [["nothing here"]])
        result = subprocess.run([sys.executable, "-I", os.path.join(HERE, "coverage_audit.py"), self.index, empty],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no county rows", result.stderr)


if __name__ == "__main__":
    unittest.main()
