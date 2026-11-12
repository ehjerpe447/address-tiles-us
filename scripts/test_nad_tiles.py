#!/usr/bin/env python3
"""Tests for nad_tiles.py. Standard library only.

    python3 -I data-repo/scripts/test_nad_tiles.py

Builds from a small synthetic NAD-shaped CSV written by the test (no real NAD data), checks the output is
deterministic, validates index.json, and checks tile format v2 units. If App/Resources/winfield-demo.csv exists (it
does in the repo this template was made in) it is also built and queried as a real-data smoke test.
"""
import gzip
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("nad_tiles", os.path.join(HERE, "nad_tiles.py"))
nt = importlib.util.module_from_spec(spec)
sys.modules["nad_tiles"] = nt
spec.loader.exec_module(nt)

DEMO = os.path.join(HERE, "..", "..", "App", "Resources", "winfield-demo.csv")
HEADER = "OID_,Add_Number,StNam_Full,Post_City,County,State,Latitude,Longitude,Unit,SubAddress,Ignored\n"
SAMPLE = HEADER + "".join([
    "1,10,MAIN Street,Town,Alpha,IL,41.9000,-88.1000,Apt 2,,x\n",
    "2,10,MAIN Street,Town,Alpha,IL,41.9001,-88.1001,#1,,x\n",
    "3,10,MAIN Street,Town,Alpha,IL,41.9002,-88.1002,UNIT 2,,x\n",
    "4,10,MAIN Street,Town,Alpha,IL,41.9003,-88.1003,1 BOX 4,,x\n",
    "5,12,MAIN Street,Town,Beta,IL,41.9100,-88.1100,,Ste 100,x\n",
    "6,12,MAIN Street,Town,Beta,IL,41.9101,-88.1101,,Ste 200,x\n",
    "7,14,MAIN Street,Town,Beta,IL,41.9200,-88.1200,5,,x\n",
    "8,14,MAIN Street,Town,Beta,IL,41.9201,-88.1201,5,,x\n",
    "9,16,MAIN Street,Town,Beta,IL,41.9300,-88.1300,,,x\n",
    "10,18,Oak Ave,Elsewhere,Gamma,WI,43.0000,-89.0000,,,x\n",
    "11,,Oak Ave,Elsewhere,Gamma,IL,43.0000,-89.0000,,,x\n",  # no number: skipped
])


def rb(path):
    with open(path, "rb") as handle:
        return handle.read()


def read_tiles(path):
    raw = rb(path)
    root_off, root_len = struct.unpack("<QQ", raw[8:24])
    data_off = struct.unpack("<Q", raw[56:64])[0]
    rows = []
    for _tid, offset, length, _run in nt.decode_directory(raw[root_off:root_off + root_len]):
        tile = json.loads(gzip.decompress(raw[data_off + offset:data_off + offset + length]))
        rows += [(tile["v"], r) for r in tile["a"]]
    return rows


class NadTilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "sample.csv")
        with open(self.csv, "w", encoding="utf-8") as handle:
            handle.write(SAMPLE)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_build_is_deterministic(self):
        nt.build(self.csv, self.path("a.pmtiles"), "NAD test", "IL")
        nt.build(self.csv, self.path("b.pmtiles"), "NAD test", "IL")
        self.assertEqual(rb(self.path("a.pmtiles")), rb(self.path("b.pmtiles")))
        stats = [json.loads(rb(self.path(f"{n}.stats.json"))) for n in "ab"]
        for item, name in zip(stats, "ab"):
            self.assertEqual(item.pop("file"), f"{name}.pmtiles")
        self.assertEqual(stats[0], stats[1])

    def test_state_filter_and_stats(self):
        stats = nt.build(self.csv, self.path("IL.pmtiles"), "NAD test", "IL")
        self.assertEqual(stats["addressCount"], 4)  # buildings 10, 12, 14, 16; the WI row and the numberless row drop
        self.assertEqual(stats["counties"], ["Alpha", "Beta"])
        lon_min, lat_min, lon_max, lat_max = stats["bbox"]
        self.assertLessEqual(lon_min, lon_max)
        self.assertEqual(lat_min, 41.9)
        self.assertEqual(lat_max, 41.93)
        self.assertTrue(all(len(r) == 5 and v == 1 for v, r in read_tiles(self.path("IL.pmtiles"))))

    def test_units_v2(self):
        nt.build(self.csv, self.path("IL.pmtiles"), "NAD test", "IL", units=True)
        rows = {r[0]: (v, r) for v, r in read_tiles(self.path("IL.pmtiles"))}
        self.assertTrue(all(v == 2 for v, _ in rows.values()))
        self.assertEqual(rows["10"][1][5], ["1", "2"])  # Apt 2, #1, UNIT 2 canonicalise; "1 BOX 4" is junk
        self.assertEqual(rows["12"][1][5], ["100", "200"])  # SubAddress is the fallback
        self.assertEqual(len(rows["14"][1]), 5)  # one distinct unit, repeated: no list
        self.assertEqual(len(rows["16"][1]), 5)

    def test_unit_stats_print_counts_and_labels_never_addresses(self):
        spec_stats = importlib.util.spec_from_file_location("unit_stats", os.path.join(HERE, "unit_stats.py"))
        us = importlib.util.module_from_spec(spec_stats)
        spec_stats.loader.exec_module(us)
        nt.build(self.csv, self.path("IL.pmtiles"), "NAD test", "IL", units=True)
        stats = us.summarize(self.path("IL.pmtiles"))
        self.assertEqual((stats["version"], stats["buildings"], stats["tokens"]), (2, 2, 4))
        text = us.render([stats])
        self.assertIn("| IL.pmtiles | 2 |", text)
        for private in ["MAIN", "Street", "Town", "41.9", "-88.1"]:
            self.assertNotIn(private, text)
        nt.build(self.csv, self.path("V1.pmtiles"), "NAD test", "IL")
        self.assertEqual(us.summarize(self.path("V1.pmtiles"))["buildings"], 0)

    def test_unit_tokens(self):
        for raw, want in [("Apt 3B", "3B"), ("#3b", "3B"), ("UNIT #4", "4"), ("Ste 100", "100"), ("APT3", "3"),
                          ("NORTH", "NORTH"), ("1 BOX 12", None), ("", None), ("N/A", None), ("-", None),
                          ("Building Annex 12", None)]:
            self.assertEqual(nt.unit_token(raw), want, raw)

    def test_unit_cap(self):
        rows = "".join(f"{i},10,Main,T,A,IL,41.9,-88.1,{i:04d},,\n" for i in range(400))
        path = self.path("many.csv")
        with open(path, "w") as handle:
            handle.write("OID_,Add_Number,StNam_Full,Post_City,County,State,Latitude,Longitude,Unit,SubAddress,X\n" + rows)
        nt.build(path, self.path("M.pmtiles"), "t", "IL", units=True)
        (_, row), = read_tiles(self.path("M.pmtiles"))
        self.assertEqual(len(row[5]), nt.MAX_UNITS_PER_BUILDING)
        self.assertEqual(row[5], sorted(row[5]))

    def test_split_is_slim_and_builds_the_same(self):
        out = self.path("by-state")
        nt.split(out, io.BytesIO(SAMPLE.encode()))
        self.assertEqual(sorted(os.listdir(out)), ["IL.csv", "WI.csv"])
        with open(os.path.join(out, "IL.csv")) as handle:
            header = handle.readline().strip().split(",")
        self.assertNotIn("Ignored", header)
        self.assertNotIn("OID_", header)
        nt.build(os.path.join(out, "IL.csv"), self.path("s.pmtiles"), "NAD test", "IL", units=True)
        nt.build(self.csv, self.path("d.pmtiles"), "NAD test", "IL", units=True)
        self.assertEqual(rb(self.path("s.pmtiles")), rb(self.path("d.pmtiles")))

    def test_index_json(self):
        tiles = self.path("tiles")
        os.makedirs(tiles)
        nt.build(self.csv, os.path.join(tiles, "IL.pmtiles"), "NAD test", "IL")
        index = nt.make_index(tiles, "NAD test", "etag-1", "https://example.invalid/r/", "2026-11-02", ["IL", "WI"])
        first = rb(os.path.join(tiles, "index.json"))
        nt.make_index(tiles, "NAD test", "etag-1", "https://example.invalid/r/", "2026-11-02", ["IL", "WI"])
        self.assertEqual(first, rb(os.path.join(tiles, "index.json")), "index.json is not deterministic")
        on_disk = json.loads(first)
        self.assertEqual(on_disk, index)
        self.assertEqual(on_disk["schema"], 1)
        self.assertEqual(on_disk["release"], "NAD test")
        self.assertEqual(on_disk["nadEtag"], "etag-1")
        self.assertEqual(on_disk["builtAt"], "2026-11-02")
        self.assertIn("Not for use as a mailing list", on_disk["notice"])
        self.assertEqual(on_disk["baseURL"], "https://example.invalid/r/")
        self.assertEqual(on_disk["statesWithoutRows"], ["WI"])
        (state,) = on_disk["states"]
        self.assertEqual(set(state), {"code", "file", "bytes", "addressCount", "bbox", "counties", "coverage", "blocks"})
        self.assertEqual(state["coverage"]["class"], "nearlyEmpty")  # a handful of rows against 5.5M homes
        self.assertIn("Census", on_disk["coverageSource"])
        self.assertEqual(state["code"], "IL")
        self.assertEqual(state["file"], "IL.pmtiles")
        self.assertEqual(state["bytes"], os.path.getsize(os.path.join(tiles, "IL.pmtiles")))
        self.assertEqual(len(state["bbox"]), 4)
        self.assertEqual(state["counties"], ["Alpha", "Beta"])

    def test_coverage_classes_at_each_threshold_edge(self):
        units = {"XX": 1000}
        cases = [(750, "fine"), (749, "partial"), (400, "partial"), (399, "thin"), (50, "thin"), (49, "nearlyEmpty"),
                 (0, "nearlyEmpty"), (1200, "fine")]
        for rows, expected in cases:
            self.assertEqual(nt.coverage_for("XX", rows, units)["class"], expected, rows)
        self.assertEqual((nt.FINE_AT, nt.PARTIAL_AT, nt.THIN_AT), (0.75, 0.40, 0.05))

    def test_coverage_rounds_the_ratio_but_classes_the_exact_one(self):
        # 0.7496 displays as 0.75 yet is not fine: the class never rounds up across an edge.
        found = nt.coverage_for("XX", 7496, {"XX": 10000})
        self.assertEqual((found["rowsPerHousingUnit"], found["class"]), (0.75, "partial"))
        self.assertEqual(found["housingUnits"], 10000)

    def test_state_missing_from_the_census_table_gets_no_coverage(self):
        self.assertIsNone(nt.coverage_for("VI", 500, nt.load_housing_units()))
        tiles = self.path("tiles")
        os.makedirs(tiles)
        nt.build(self.csv, os.path.join(tiles, "IL.pmtiles"), "t", "IL")
        table = self.path("census.csv")  # a table without IL stands in for a territory the Census table lacks
        with open(table, "w", encoding="utf-8") as handle:
            handle.write("state,housing_units_2025_07_01\nWI,100\n")
        index = nt.make_index(tiles, "t", "e", "https://example.invalid/", "2026-11-02", [], census_path=table)
        self.assertNotIn("coverage", index["states"][0])

    def test_bundled_census_table_is_complete(self):
        units = nt.load_housing_units()
        self.assertEqual(len(units), 51)
        self.assertTrue(all(value > 0 for value in units.values()))
        self.assertEqual(units["WY"], 283790)
        self.assertTrue({"DC", "CA"} <= set(units) and not {"PR", "VI", "GU"} & set(units))

    def test_coverage_in_the_index_is_deterministic(self):
        tiles = self.path("tiles")
        os.makedirs(tiles)
        nt.build(self.csv, os.path.join(tiles, "IL.pmtiles"), "t", "IL")
        args = (tiles, "t", "e", "https://example.invalid/", "2026-11-02", ["IL"])
        first = nt.make_index(*args)
        raw = rb(os.path.join(tiles, "index.json"))
        nt.make_index(*args)
        self.assertEqual(raw, rb(os.path.join(tiles, "index.json")))
        self.assertEqual(first["states"][0]["coverage"]["housingUnits"], 5504295)

    def test_sec10_blocks_sidecar_lists_every_block_hash(self):
        """SEC-10: the sidecar is the SHA-256 of each BLOCK_SIZE block, in order, and the index names its hash."""
        import hashlib
        tiles = self.path("tiles")
        os.makedirs(tiles)
        archive = os.path.join(tiles, "IL.pmtiles")
        nt.build(self.csv, archive, "NAD test", "IL")
        index = nt.make_index(tiles, "t", "e", "https://example.invalid/", "2026-11-02", ["IL"])
        (state,) = index["states"]
        data = rb(archive)
        sidecar = rb(os.path.join(tiles, state["blocks"]["file"]))
        expected = b"".join(hashlib.sha256(data[i:i + nt.BLOCK_SIZE]).digest()
                            for i in range(0, len(data), nt.BLOCK_SIZE))
        self.assertEqual(sidecar, expected)
        self.assertEqual(len(sidecar), 32 * -(-len(data) // nt.BLOCK_SIZE))
        self.assertEqual(state["blocks"], {"file": "IL.pmtiles.blocks", "sha256": hashlib.sha256(sidecar).hexdigest(),
                                           "blockSize": 16384})

    def test_index_rejects_mixed_tile_versions(self):
        tiles = self.path("tiles")
        os.makedirs(tiles)
        nt.build(self.csv, os.path.join(tiles, "IL.pmtiles"), "t", "IL")
        nt.build(self.csv, os.path.join(tiles, "WI.pmtiles"), "t", "WI", units=True)
        with self.assertRaises(SystemExit):
            nt.make_index(tiles, "t", "e", "https://example.invalid/", "2026-11-02", [])

    @unittest.skipUnless(os.path.exists(DEMO), "winfield-demo.csv is only in the repo this template was made in")
    def test_winfield_demo(self):
        tiles = self.path("tiles")
        os.makedirs(tiles)
        a, b = os.path.join(tiles, "IL.pmtiles"), self.path("again.pmtiles")
        stats = nt.build(DEMO, a, "fixture: DuPage sample", "IL")
        nt.build(DEMO, b, "fixture: DuPage sample", "IL")
        self.assertEqual(rb(a), rb(b))
        self.assertEqual(stats["addressCount"], 127)
        index = nt.make_index(tiles, "fixture: DuPage sample", "e", "https://example.invalid/", "2026-11-02", ["IL"])
        self.assertEqual(index["statesWithoutRows"], [])
        self.assertEqual(len(nt.read_tile_rows(a, -88.16, 41.87, -88.13, 41.90)), 127)


DATED = "OID_,Add_Number,StNam_Full,Post_City,County,State,Latitude,Longitude,DateUpdate\n" + "".join([
    "1,10,Main St,Town,Alpha,IL,41.9000,-88.1000,2025/10/30 00:00:00\n",
    "2,10,Main St,Town,Alpha,IL,41.9001,-88.1001,2023-01-05\n",   # same building: one row, two dates
    "3,12,Main St,Town,Alpha,IL,41.9100,-88.1100,1899-12-30\n",   # NAD's null date
    "4,14,Main St,Town,Beta,IL,41.9200,-88.1200,03/07/2021\n",
    "5,16,Main St,Town,,IL,41.9300,-88.1300,\n",                    # no county, no date
    "6,18,Main St,Town,Beta,IL,41.9400,-88.1400,2021-13-40\n",      # not a date
])


class CoverageJsonTests(unittest.TestCase):
    """FUNC-60: per-county rows and DateUpdate counts in the stats file and in coverage.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tiles = os.path.join(self.tmp.name, "tiles")
        os.makedirs(self.tiles)
        self.csv = os.path.join(self.tmp.name, "dated.csv")
        with open(self.csv, "w", encoding="utf-8") as handle:
            handle.write(DATED)

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_date_formats_and_nulls(self):
        self.assertEqual(nt.update_date("2025/10/30 00:00:00"), "2025-10-30")
        self.assertEqual(nt.update_date("2023-01-05"), "2023-01-05")
        self.assertEqual(nt.update_date("3/7/2021"), "2021-03-07")
        for raw in ("", "1899-12-30", "2021-13-40", "unknown"):
            self.assertIsNone(nt.update_date(raw), raw)

    def test_split_keeps_dateupdate(self):
        out = os.path.join(self.tmp.name, "split")
        nt.split(out, io.BytesIO(DATED.encode()))
        with open(os.path.join(out, "IL.csv"), encoding="utf-8") as handle:
            self.assertIn("DateUpdate", handle.readline())

    def test_county_stats_in_the_stats_file(self):
        stats = nt.build(self.csv, os.path.join(self.tiles, "IL.pmtiles"), "t", "IL")
        county = stats["countyStats"]
        self.assertEqual(stats["addressCount"], 5)
        self.assertEqual(sum(c["rows"] for c in county.values()), stats["addressCount"])
        self.assertEqual(county["Alpha"], {"rows": 2, "datedRows": 2, "newestUpdate": "2025-10-30",
                                           "rowsByYear": {"2023": 1, "2025": 1}})
        self.assertEqual(county["Beta"], {"rows": 2, "datedRows": 1, "newestUpdate": "2021-03-07",
                                          "rowsByYear": {"2021": 1}})
        self.assertEqual(county[""], {"rows": 1, "datedRows": 0, "newestUpdate": None, "rowsByYear": {}})

    def test_coverage_json_is_written_beside_the_index_and_kept_out_of_it(self):
        nt.build(self.csv, os.path.join(self.tiles, "IL.pmtiles"), "t", "IL")
        args = (self.tiles, "NAD test", "e", "https://example.invalid/", "2026-11-06", ["IL"])
        index = nt.make_index(*args)
        path = os.path.join(self.tiles, "coverage.json")
        first = rb(path)
        nt.make_index(*args)
        self.assertEqual(first, rb(path), "coverage.json is not deterministic")
        coverage = json.loads(first)
        self.assertEqual((coverage["schema"], coverage["release"], coverage["builtAt"]), (1, "NAD test", "2026-11-06"))
        self.assertIn("Not for use as a mailing list", coverage["notice"])
        self.assertEqual([c["county"] for c in coverage["states"]["IL"]], ["", "Alpha", "Beta"])
        self.assertNotIn("countyStats", index["states"][0])

    def test_old_stats_file_without_county_stats(self):
        nt.build(self.csv, os.path.join(self.tiles, "IL.pmtiles"), "t", "IL")
        stats_file = os.path.join(self.tiles, "IL.stats.json")
        with open(stats_file, encoding="utf-8") as handle:
            stats = json.load(handle)
        del stats["countyStats"]
        with open(stats_file, "w", encoding="utf-8") as handle:
            json.dump(stats, handle)
        nt.make_index(self.tiles, "t", "e", "https://example.invalid/", "2026-11-06", ["IL"])
        with open(os.path.join(self.tiles, "coverage.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["states"], {"IL": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
