from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import faf5_recipe.core as core
from faf5_recipe.core import Faf5Filters, OUTPUT_FIELDS, RecipeError


YEARS = (2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2030, 2035, 2040, 2045, 2050)
EXPECTED_SOURCE_HEADERS = (
    "fr_orig",
    "dms_orig",
    "dms_dest",
    "fr_dest",
    "fr_inmode",
    "dms_mode",
    "fr_outmode",
    "sctg2",
    "trade_type",
    "dist_band",
    *(f"tons_{year}" for year in YEARS),
    *(f"value_{year}" for year in YEARS),
    *(f"current_value_{year}" for year in range(2018, 2025)),
    *(f"tmiles_{year}" for year in YEARS),
)
EXPECTED_OUTPUT_FIELDS = (
    "release_id",
    "source_row_number",
    "estimate_year",
    "estimate_status",
    "scenario",
    "faf_origin",
    "faf_destination",
    "foreign_origin",
    "foreign_destination",
    "foreign_inbound_mode",
    "domestic_mode",
    "foreign_outbound_mode",
    "commodity_sctg2",
    "trade_type",
    "distance_band",
    "tons_thousand",
    "value_million_usd",
    "ton_miles_million",
)


class Faf5RecipeTests(unittest.TestCase):
    def _filters(self) -> Faf5Filters:
        return Faf5Filters.from_values(
            origins=["011", "012"],
            destinations=["014"],
            commodities=["01"],
            modes=["01"],
            trades=["1"],
            years=["2017", "2018", "2024", "2030"],
            scenarios=["annual_estimate", "baseline"],
        )

    @staticmethod
    def _row(origin: str = "011") -> dict[str, str]:
        row = {name: "" for name in EXPECTED_SOURCE_HEADERS}
        row.update(
            {
                "fr_orig": "09",
                "dms_orig": origin,
                "dms_dest": "014",
                "fr_dest": "08",
                "fr_inmode": "03",
                "dms_mode": "01",
                "fr_outmode": "04",
                "sctg2": "01",
                "trade_type": "1",
                "dist_band": "05",
                "tons_2017": "001.2300",
                "value_2017": "0002.5000",
                "tmiles_2017": "",
                "tons_2018": "4",
                "value_2018": "5",
                "tmiles_2018": "6",
                "tons_2024": "0",
                "value_2024": "",
                "tmiles_2024": "",
                "tons_2030": "1.000",
                "value_2030": "12345678901234567890.123456789",
                "tmiles_2030": "2.2500",
                "current_value_2018": "10.500",
            }
        )
        return row

    def _source_text(self) -> str:
        rows = [self._row(), self._row("012"), self._row("11")]
        rows[1].update(
            {
                "tons_2017": "2",
                "value_2017": "",
                "tmiles_2017": "",
                "tons_2018": "",
                "value_2018": "",
                "tmiles_2018": "",
                "tons_2024": "",
                "value_2024": "",
                "tmiles_2024": "",
                "tons_2030": "",
                "value_2030": "",
                "tmiles_2030": "",
            }
        )
        # A dimension-excluded row must not be silently interpreted as a fact;
        # its metric cells are intentionally not semantically normalized.
        rows[2]["tons_2017"] = "NaN"
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=EXPECTED_SOURCE_HEADERS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()

    def _run_synthetic_extract(self, source_text: str, output_dir: Path) -> dict[str, object]:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        return core._extract_stream_to_new_directory(
            io.StringIO(source_text, newline=""),
            output_dir,
            input_size_bytes=len(source_text.encode("utf-8")),
            input_sha256=source_hash,
            filters=self._filters(),
        )

    def test_release_schema_and_long_fact_semantics(self) -> None:
        self.assertEqual(core.SOURCE_HEADERS, EXPECTED_SOURCE_HEADERS)
        self.assertEqual(OUTPUT_FIELDS, EXPECTED_OUTPUT_FIELDS)
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "extract"
            manifest = self._run_synthetic_extract(self._source_text(), output_dir)

            with (output_dir / "faf5_flows.csv").open(encoding="utf-8", newline="") as handle:
                records = list(csv.DictReader(handle))
            self.assertEqual(len(records), 5)
            self.assertEqual(
                [(record["source_row_number"], record["estimate_year"]) for record in records],
                [("1", "2017"), ("1", "2018"), ("1", "2024"), ("1", "2030"), ("2", "2017")],
            )
            self.assertEqual(
                [(record["estimate_status"], record["scenario"]) for record in records[:4]],
                [
                    ("base_year", "annual_estimate"),
                    ("final_annual", "annual_estimate"),
                    ("preliminary_annual", "annual_estimate"),
                    ("forecast", "baseline"),
                ],
            )
            first = records[0]
            self.assertEqual(first["faf_origin"], "011")
            self.assertEqual(first["faf_destination"], "014")
            self.assertEqual(first["commodity_sctg2"], "01")
            self.assertEqual(first["domestic_mode"], "01")
            self.assertEqual(first["trade_type"], "1")
            self.assertEqual(first["foreign_origin"], "09")
            self.assertEqual(first["foreign_destination"], "08")
            self.assertEqual(first["foreign_inbound_mode"], "03")
            self.assertEqual(first["foreign_outbound_mode"], "04")
            self.assertEqual(first["distance_band"], "05")
            self.assertEqual(first["tons_thousand"], "001.2300")
            self.assertEqual(first["value_million_usd"], "0002.5000")
            self.assertEqual(first["ton_miles_million"], "")
            self.assertEqual(records[2]["tons_thousand"], "0")
            self.assertEqual(records[3]["value_million_usd"], "12345678901234567890.123456789")

            counts = manifest["counts"]
            self.assertEqual(counts["source_rows"], 3)
            self.assertEqual(counts["matched_rows"], 2)
            self.assertEqual(counts["excluded_rows"], 1)
            self.assertEqual(counts["output_facts"], 5)
            self.assertEqual(counts["no_measure_cases"], 3)
            self.assertEqual(counts["matched_rows_without_output_facts"], 0)
            self.assertEqual(manifest["selected_filters"]["origin"], ["011", "012"])
            self.assertEqual(manifest["value_basis"], "million_2017_constant_usd")
            actual_digest = hashlib.sha256((output_dir / "faf5_flows.csv").read_bytes()).hexdigest()
            self.assertEqual(manifest["csv"]["sha256"], actual_digest)
            self.assertEqual(
                manifest["input"]["sha256"],
                hashlib.sha256(self._source_text().encode("utf-8")).hexdigest(),
            )

    def test_year_and_scenario_filtering_are_fact_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "extract"
            filters = Faf5Filters.from_values(
                origins=["011"],
                destinations=["014"],
                commodities=["01"],
                modes=["01"],
                trades=["1"],
                years=["2035"],
                scenarios=["baseline"],
            )
            text = self._source_text()
            manifest = core._extract_stream_to_new_directory(
                io.StringIO(text, newline=""),
                output_dir,
                input_size_bytes=len(text.encode()),
                input_sha256=hashlib.sha256(text.encode()).hexdigest(),
                filters=filters,
            )
            with (output_dir / "faf5_flows.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            self.assertEqual(manifest["counts"]["matched_rows"], 1)
            self.assertEqual(manifest["counts"]["excluded_rows"], 2)
            self.assertEqual(manifest["counts"]["output_facts"], 0)
            self.assertEqual(manifest["counts"]["no_measure_cases"], 1)

    def test_replay_outputs_identical_bytes_without_volatile_manifest_fields(self) -> None:
        text = self._source_text()
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            self._run_synthetic_extract(text, first)
            self._run_synthetic_extract(text, second)
            self.assertEqual((first / "faf5_flows.csv").read_bytes(), (second / "faf5_flows.csv").read_bytes())
            self.assertEqual((first / "manifest.json").read_bytes(), (second / "manifest.json").read_bytes())
            manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("created_at", manifest)
            self.assertNotIn("input_path", manifest)
            self.assertNotIn("output_path", manifest)

    def test_unavailable_scenario_and_noncanonical_year_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Faf5Filters.from_values(scenarios=["low"])
        with self.assertRaises(ValueError):
            Faf5Filters.from_values(years=["02017"])
        with self.assertRaises(ValueError):
            Faf5Filters.from_values(origins="061")
        with self.assertRaises(ValueError):
            Faf5Filters.from_values(years=["2024"], scenarios=["baseline"])

    def test_invalid_matched_measure_leaves_no_completed_output_or_temp_directory(self) -> None:
        text = self._source_text()
        tons_index = EXPECTED_SOURCE_HEADERS.index("tons_2017")
        for invalid_value in ("NaN", "Infinity", "-1", "not-numeric"):
            rows = list(csv.reader(io.StringIO(text, newline="")))
            rows[1][tons_index] = invalid_value
            corrupted = io.StringIO(newline="")
            csv.writer(corrupted, lineterminator="\n").writerows(rows)
            with self.subTest(value=invalid_value), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary)
                output_dir = parent / "extract"
                before = set(parent.iterdir())
                with self.assertRaises(RecipeError):
                    self._run_synthetic_extract(corrupted.getvalue(), output_dir)
                self.assertFalse(output_dir.exists())
                self.assertEqual(set(parent.iterdir()), before)

    def test_duplicate_unknown_and_missing_required_schema_are_rejected(self) -> None:
        source = self._source_text()
        bad_headers: list[str] = []
        duplicate = list(csv.reader(io.StringIO(source, newline="")))
        duplicate[0][duplicate[0].index("tons_2018")] = "tons_2017"
        bad_headers.append(self._rows_to_text(duplicate))
        unknown = list(csv.reader(io.StringIO(source, newline="")))
        unknown[0][unknown[0].index("tons_2017")] = "tons_2099"
        bad_headers.append(self._rows_to_text(unknown))
        missing_dimension = list(csv.reader(io.StringIO(source, newline="")))
        missing_dimension[0].remove("dms_orig")
        for row in missing_dimension[1:]:
            row.pop(EXPECTED_SOURCE_HEADERS.index("dms_orig"))
        bad_headers.append(self._rows_to_text(missing_dimension))

        for index, text in enumerate(bad_headers):
            with self.subTest(schema_case=index), tempfile.TemporaryDirectory() as temporary:
                output_dir = Path(temporary) / "extract"
                with self.assertRaises(RecipeError):
                    self._run_synthetic_extract(text, output_dir)
                self.assertFalse(output_dir.exists())

    def test_missing_required_row_dimension_and_malformed_row_are_rejected(self) -> None:
        rows = list(csv.reader(io.StringIO(self._source_text(), newline="")))
        origin_index = EXPECTED_SOURCE_HEADERS.index("dms_orig")
        rows[1][origin_index] = ""
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "missing-dimension"
            with self.assertRaises(RecipeError):
                self._run_synthetic_extract(self._rows_to_text(rows), output_dir)
            self.assertFalse(output_dir.exists())

        rows = list(csv.reader(io.StringIO(self._source_text(), newline="")))
        rows[1].pop()
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "malformed-row"
            with self.assertRaises(RecipeError):
                self._run_synthetic_extract(self._rows_to_text(rows), output_dir)
            self.assertFalse(output_dir.exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        text = self._source_text()
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "existing"
            output_dir.mkdir()
            sentinel = output_dir / "keep.txt"
            sentinel.write_text("owner data", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                self._run_synthetic_extract(text, output_dir)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "owner data")

    def test_archive_digest_rejection_and_invalid_zip_leave_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            archive_path = parent / "mutated.zip"
            output_dir = parent / "digest-rejected"
            archive_path.write_bytes(b"changed bytes")
            size_rejected_output = parent / "size-rejected"
            with self.assertRaises(RecipeError):
                core.extract_archive(archive_path, size_rejected_output)
            self.assertFalse(size_rejected_output.exists())
            with patch.object(core, "_PINNED_SIZE_BYTES", len(b"changed bytes")):
                with self.assertRaises(RecipeError):
                    core.extract_archive(archive_path, output_dir)
            self.assertFalse(output_dir.exists())

            archive_path.write_bytes(b"not a zip archive")
            invalid_zip_output = parent / "invalid-zip"
            with (
                patch.object(core, "_PINNED_SIZE_BYTES", len(b"not a zip archive")),
                patch.object(
                    core,
                    "_PINNED_SHA256",
                    hashlib.sha256(b"not a zip archive").hexdigest(),
                ),
            ):
                with self.assertRaises(RecipeError):
                    core.extract_archive(archive_path, invalid_zip_output)
            self.assertFalse(invalid_zip_output.exists())

    @staticmethod
    def _rows_to_text(rows: list[list[str]]) -> str:
        buffer = io.StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerows(rows)
        return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
