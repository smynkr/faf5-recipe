"""Pinned, streaming FAF5.7.1 ZIP-to-CSV transformation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO, Iterable, TextIO

RELEASE_ID = "FAF5.7.1-regional-mid"
SOURCE_URL = "https://faf.ornl.gov/faf5/Data/Download_Files/FAF5.7.1.zip"
_PINNED_SIZE_BYTES = 305_397_783
_PINNED_SHA256 = "1429be77a578f2b32913320a1299c0e3de8aa9c6180ce5aacfd65a7886ba55b8"
_CSV_MEMBER = "FAF5.7.1.csv"
_METADATA_MEMBER = "FAF5_metadata.xlsx"
_CSV_MEMBER_SIZE = 1_179_593_832
_CSV_MEMBER_COMPRESSED_SIZE = 305_373_689
_METADATA_MEMBER_SIZE = 28_786
_METADATA_MEMBER_COMPRESSED_SIZE = 23_860
_TRANSFORMATION_VERSION = "faf5-recipe.extract/1"
_MAX_CSV_FIELD_CHARS = 65_536
_HASH_CHUNK_SIZE = 1024 * 1024

YEARS = (2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2030, 2035, 2040, 2045, 2050)
_SOURCE_DIMENSIONS = (
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
)
_REQUIRED_DIMENSIONS = ("dms_orig", "dms_dest", "dms_mode", "sctg2", "trade_type")
_SOURCE_METRIC_COLUMNS = (
    *(f"tons_{year}" for year in YEARS),
    *(f"value_{year}" for year in YEARS),
    *(f"current_value_{year}" for year in range(2018, 2025)),
    *(f"tmiles_{year}" for year in YEARS),
)
SOURCE_HEADERS = (*_SOURCE_DIMENSIONS, *_SOURCE_METRIC_COLUMNS)
_METRIC_COLUMN_BY_YEAR = {
    year: {
        "tons": f"tons_{year}",
        "value": f"value_{year}",
        "tmiles": f"tmiles_{year}",
    }
    for year in YEARS
}
_CURRENT_VALUE_COLUMNS = tuple(f"current_value_{year}" for year in range(2018, 2025))

OUTPUT_FIELDS = (
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
_OUTPUT_DIMENSION_MAP = (
    ("faf_origin", "dms_orig"),
    ("faf_destination", "dms_dest"),
    ("foreign_origin", "fr_orig"),
    ("foreign_destination", "fr_dest"),
    ("foreign_inbound_mode", "fr_inmode"),
    ("domestic_mode", "dms_mode"),
    ("foreign_outbound_mode", "fr_outmode"),
    ("commodity_sctg2", "sctg2"),
    ("trade_type", "trade_type"),
    ("distance_band", "dist_band"),
)
_YEAR_SEMANTICS = {
    2017: ("base_year", "annual_estimate"),
    2018: ("final_annual", "annual_estimate"),
    2019: ("final_annual", "annual_estimate"),
    2020: ("final_annual", "annual_estimate"),
    2021: ("final_annual", "annual_estimate"),
    2022: ("final_annual", "annual_estimate"),
    2023: ("final_annual", "annual_estimate"),
    2024: ("preliminary_annual", "annual_estimate"),
    2030: ("forecast", "baseline"),
    2035: ("forecast", "baseline"),
    2040: ("forecast", "baseline"),
    2045: ("forecast", "baseline"),
    2050: ("forecast", "baseline"),
}
_SCENARIO_BY_YEAR = {
    str(year): semantics[1] for year, semantics in _YEAR_SEMANTICS.items()
}
_SUPPORTED_SCENARIOS = frozenset({"annual_estimate", "baseline"})
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class RecipeError(ValueError):
    """Input, schema, or transformation error with no completed output."""


@dataclass(frozen=True)
class Faf5Filters:
    """Exact string-code filters accepted by the pinned release."""

    origins: frozenset[str] = frozenset()
    destinations: frozenset[str] = frozenset()
    commodities: frozenset[str] = frozenset()
    modes: frozenset[str] = frozenset()
    trades: frozenset[str] = frozenset()
    years: frozenset[str] = frozenset()
    scenarios: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for attribute, name in (
            ("origins", "origin"),
            ("destinations", "destination"),
            ("commodities", "commodity"),
            ("modes", "mode"),
            ("trades", "trade"),
        ):
            object.__setattr__(self, attribute, _code_values(name, getattr(self, attribute)))
        object.__setattr__(self, "years", _year_values(self.years))
        object.__setattr__(self, "scenarios", _scenario_values(self.scenarios))

        if self.years and self.scenarios:
            unmatched_years = sorted(
                year
                for year in self.years
                if _SCENARIO_BY_YEAR[year] not in self.scenarios
            )
            if unmatched_years:
                raise RecipeError(
                    "selected scenario filters are unavailable for year(s): "
                    + ", ".join(unmatched_years)
                )

    @classmethod
    def from_values(
        cls,
        *,
        origins: Iterable[str] | None = None,
        destinations: Iterable[str] | None = None,
        commodities: Iterable[str] | None = None,
        modes: Iterable[str] | None = None,
        trades: Iterable[str] | None = None,
        years: Iterable[str] | None = None,
        scenarios: Iterable[str] | None = None,
    ) -> Faf5Filters:
        return cls(
            origins=_code_values("origin", origins),
            destinations=_code_values("destination", destinations),
            commodities=_code_values("commodity", commodities),
            modes=_code_values("mode", modes),
            trades=_code_values("trade", trades),
            years=_year_values(years),
            scenarios=_scenario_values(scenarios),
        )

    def manifest_values(self) -> dict[str, list[str]]:
        return {
            "origin": sorted(self.origins),
            "destination": sorted(self.destinations),
            "commodity": sorted(self.commodities),
            "mode": sorted(self.modes),
            "trade": sorted(self.trades),
            "year": sorted(self.years),
            "scenario": sorted(self.scenarios),
        }


@dataclass
class _Counts:
    source_rows: int = 0
    matched_rows: int = 0
    excluded_rows: int = 0
    output_facts: int = 0
    no_measure_cases: int = 0
    matched_rows_without_output_facts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "source_rows": self.source_rows,
            "matched_rows": self.matched_rows,
            "excluded_rows": self.excluded_rows,
            "output_facts": self.output_facts,
            "no_measure_cases": self.no_measure_cases,
            "matched_rows_without_output_facts": self.matched_rows_without_output_facts,
        }


def _code_values(name: str, values: Iterable[str] | None) -> frozenset[str]:
    if isinstance(values, str):
        raise RecipeError(f"{name} filters must be supplied as repeated code options, not one string")
    result: set[str] = set()
    for value in values or ():
        if not isinstance(value, str) or not value or value != value.strip():
            raise RecipeError(f"{name} filters must be nonempty exact code strings without surrounding whitespace")
        result.add(value)
    return frozenset(result)


def _year_values(values: Iterable[str] | None) -> frozenset[str]:
    result = _code_values("year", values)
    supported = {str(year) for year in YEARS}
    unknown = sorted(result - supported)
    if unknown:
        raise RecipeError(f"unsupported FAF5 year filter(s): {', '.join(unknown)}")
    return result


def _scenario_values(values: Iterable[str] | None) -> frozenset[str]:
    result = _code_values("scenario", values)
    unknown = sorted(result - _SUPPORTED_SCENARIOS)
    if unknown:
        raise RecipeError(
            "unsupported scenario filter(s): "
            + ", ".join(unknown)
            + "; the pinned release supports annual_estimate and baseline only"
        )
    return result


def _validate_new_output_dir(output_dir: Path) -> None:
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists and will not be replaced: {output_dir}")
    if not output_dir.parent.is_dir():
        raise NotADirectoryError(f"output parent directory must already exist: {output_dir.parent}")


def _sha256_fileobj(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    source.seek(0)
    while True:
        chunk = source.read(_HASH_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _verify_archive_members(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    infos = archive.infolist()
    expected_names = {_CSV_MEMBER, _METADATA_MEMBER}
    names = [info.filename for info in infos]
    if len(infos) != 2 or len(set(names)) != 2 or set(names) != expected_names:
        raise RecipeError(
            "pinned FAF5 ZIP must contain exactly FAF5.7.1.csv and FAF5_metadata.xlsx"
        )
    info_by_name = {info.filename: info for info in infos}
    csv_info = info_by_name[_CSV_MEMBER]
    metadata_info = info_by_name[_METADATA_MEMBER]
    if (
        csv_info.file_size != _CSV_MEMBER_SIZE
        or csv_info.compress_size != _CSV_MEMBER_COMPRESSED_SIZE
        or metadata_info.file_size != _METADATA_MEMBER_SIZE
        or metadata_info.compress_size != _METADATA_MEMBER_COMPRESSED_SIZE
    ):
        raise RecipeError("FAF5 ZIP member sizes do not match the pinned release")
    if csv_info.flag_bits & 0x1 or metadata_info.flag_bits & 0x1:
        raise RecipeError("encrypted FAF5 ZIP members are not supported")
    return csv_info


def _validate_source_header(header: list[str]) -> dict[str, int]:
    if len(header) != len(set(header)):
        raise RecipeError("FAF5 CSV has duplicate column headers")
    actual = set(header)
    expected = set(SOURCE_HEADERS)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unknown:
            details.append("unknown: " + ", ".join(unknown))
        raise RecipeError("FAF5 CSV header does not match the pinned schema (" + "; ".join(details) + ")")
    return {name: index for index, name in enumerate(header)}


def _validate_dimension(value: str, name: str, source_row_number: int, *, required: bool) -> None:
    if value != value.strip():
        raise RecipeError(f"source row {source_row_number}: dimension {name!r} has surrounding whitespace")
    if required and not value:
        raise RecipeError(f"source row {source_row_number}: required dimension {name!r} is empty")


def _validated_measure_text(value: str, column: str, source_row_number: int) -> str | None:
    if value == "":
        return None
    if value != value.strip() or not _DECIMAL_TEXT.fullmatch(value):
        raise RecipeError(
            f"source row {source_row_number}: measure {column!r} is not plain decimal numeric text"
        )
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise RecipeError(
            f"source row {source_row_number}: measure {column!r} is invalid"
        ) from error
    if not number.is_finite() or number < 0:
        raise RecipeError(
            f"source row {source_row_number}: measure {column!r} must be finite and nonnegative"
        )
    return value


def _row_matches_filters(row: list[str], indices: dict[str, int], filters: Faf5Filters) -> bool:
    return (
        (not filters.origins or row[indices["dms_orig"]] in filters.origins)
        and (not filters.destinations or row[indices["dms_dest"]] in filters.destinations)
        and (not filters.commodities or row[indices["sctg2"]] in filters.commodities)
        and (not filters.modes or row[indices["dms_mode"]] in filters.modes)
        and (not filters.trades or row[indices["trade_type"]] in filters.trades)
    )


def _write_long_csv(source: TextIO, csv_path: Path, filters: Faf5Filters) -> _Counts:
    counts = _Counts()
    prior_field_limit = csv.field_size_limit(_MAX_CSV_FIELD_CHARS)
    try:
        with csv_path.open("x", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(OUTPUT_FIELDS)
            reader = csv.reader(source, strict=True)
            try:
                header = next(reader)
            except StopIteration as error:
                raise RecipeError("FAF5 CSV is empty") from error
            indices = _validate_source_header(header)
            for source_row_number, row in enumerate(reader, start=1):
                counts.source_rows += 1
                if len(row) != len(header):
                    raise RecipeError(
                        f"source row {source_row_number}: found {len(row)} fields, expected {len(header)}"
                    )
                for name in _SOURCE_DIMENSIONS:
                    _validate_dimension(
                        row[indices[name]],
                        name,
                        source_row_number,
                        required=name in _REQUIRED_DIMENSIONS,
                    )
                if not _row_matches_filters(row, indices, filters):
                    counts.excluded_rows += 1
                    continue
                counts.matched_rows += 1

                output_dimensions = tuple(
                    row[indices[source_name]]
                    for _, source_name in _OUTPUT_DIMENSION_MAP
                )
                row_fact_count = 0
                for year in YEARS:
                    estimate_status, scenario = _YEAR_SEMANTICS[year]
                    metric_columns = _METRIC_COLUMN_BY_YEAR[year]
                    measures = tuple(
                        _validated_measure_text(
                            row[indices[column]], column, source_row_number
                        )
                        for column in metric_columns.values()
                    )
                    if filters.years and str(year) not in filters.years:
                        continue
                    if filters.scenarios and scenario not in filters.scenarios:
                        continue
                    if not any(value is not None for value in measures):
                        counts.no_measure_cases += 1
                        continue
                    writer.writerow(
                        (
                            RELEASE_ID,
                            source_row_number,
                            year,
                            estimate_status,
                            scenario,
                            *output_dimensions,
                            *(value or "" for value in measures),
                        )
                    )
                    counts.output_facts += 1
                    row_fact_count += 1
                for column in _CURRENT_VALUE_COLUMNS:
                    _validated_measure_text(row[indices[column]], column, source_row_number)
                if row_fact_count == 0:
                    counts.matched_rows_without_output_facts += 1
        if counts.matched_rows + counts.excluded_rows != counts.source_rows:
            raise RecipeError("internal source-row disposition count mismatch")
    except csv.Error as error:
        raise RecipeError(f"FAF5 CSV is malformed: {error}") from error
    finally:
        csv.field_size_limit(prior_field_limit)
    return counts


def _manifest(
    *,
    counts: _Counts,
    input_size_bytes: int,
    input_sha256: str,
    csv_sha256: str,
    filters: Faf5Filters,
) -> dict[str, object]:
    return {
        "release_id": RELEASE_ID,
        "source_url": SOURCE_URL,
        "input": {"size_bytes": input_size_bytes, "sha256": input_sha256},
        "transformation_version": _TRANSFORMATION_VERSION,
        "selected_filters": filters.manifest_values(),
        "year_semantics": [
            {
                "year": year,
                "estimate_status": _YEAR_SEMANTICS[year][0],
                "scenario": _YEAR_SEMANTICS[year][1],
            }
            for year in YEARS
        ],
        "units": {
            "tons_thousand": "thousand tons",
            "value_million_usd": "million US dollars",
            "ton_miles_million": "million ton-miles",
        },
        "value_basis": "million_2017_constant_usd",
        "scenario_assignment": "unsuffixed years 2017-2024 map to annual_estimate; unsuffixed forecast years map to baseline",
        "counts_definition": {
            "matched_rows": "source rows passing all supplied code-dimension filters",
            "excluded_rows": "source rows failing at least one supplied code-dimension filter",
            "output_facts": "matched source row/year records with one or more exported measures",
            "no_measure_cases": "matched source row/year combinations selected by year/scenario filters with all exported measures empty",
            "matched_rows_without_output_facts": "matched source rows that emitted no fact after year/scenario filters and measure checks",
            "year_and_scenario_filters": "applied to output facts, not to matched/excluded source-row disposition",
        },
        "measure_selection": {
            "exported_columns": ["tons_YEAR", "value_YEAR", "tmiles_YEAR"],
            "value_columns": "value_YEAR is reported in constant 2017 US dollars",
            "excluded_columns": ["current_value_2018 through current_value_2024"],
            "missing_values": "empty CSV cells; never imputed as zero",
        },
        "validation_scope": {
            "header": "exact supported FAF5.7.1 source column set; duplicate, missing, or unknown columns are rejected",
            "row_shape": "field count and required dimensions checked for every source row",
            "numeric_values": "Decimal finite/nonnegative validation of tons, value, tmiles, and current_value fields for dimension-matched rows",
            "dimension_excluded_rows": "their numeric measure cells are not validated",
            "not_checked": [
                "FAF codebook membership or code-label correctness",
                "cross-measure reconciliation or broader data semantics",
                "numeric measures of rows excluded by dimension filters",
            ],
        },
        "counts": counts.as_dict(),
        "csv": {"filename": "faf5_flows.csv", "sha256": csv_sha256},
    }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_output(
    temp_dir: Path,
    source: TextIO,
    *,
    input_size_bytes: int,
    input_sha256: str,
    filters: Faf5Filters,
) -> dict[str, object]:
    csv_path = temp_dir / "faf5_flows.csv"
    counts = _write_long_csv(source, csv_path, filters)
    manifest = _manifest(
        counts=counts,
        input_size_bytes=input_size_bytes,
        input_sha256=input_sha256,
        csv_sha256=_sha256_path(csv_path),
        filters=filters,
    )
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with (temp_dir / "manifest.json").open("xb") as target:
        target.write(manifest_bytes)
    return manifest


def _make_temp_dir(output_dir: Path) -> Path:
    name = output_dir.name or "faf5-extract"
    return Path(tempfile.mkdtemp(prefix=f".{name}.tmp-", dir=output_dir.parent))


def _commit_output(temp_dir: Path, output_dir: Path) -> None:
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists and will not be replaced: {output_dir}")
    temp_dir.rename(output_dir)


def _remove_temp_dir(temp_dir: Path | None) -> None:
    if temp_dir is not None and os.path.lexists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            pass


def _extract_stream_to_new_directory(
    source: TextIO,
    output_dir: Path,
    *,
    input_size_bytes: int,
    input_sha256: str,
    filters: Faf5Filters,
) -> dict[str, object]:
    """Internal stream parser used by tests; it does not authenticate a ZIP."""
    output_dir = Path(output_dir)
    _validate_new_output_dir(output_dir)
    temp_dir: Path | None = None
    published = False
    try:
        temp_dir = _make_temp_dir(output_dir)
        manifest = _prepare_output(
            temp_dir,
            source,
            input_size_bytes=input_size_bytes,
            input_sha256=input_sha256,
            filters=filters,
        )
        _commit_output(temp_dir, output_dir)
        published = True
        return manifest
    finally:
        if not published:
            _remove_temp_dir(temp_dir)


def extract_archive(
    input_zip: Path | str,
    output_dir: Path | str,
    *,
    filters: Faf5Filters | None = None,
) -> dict[str, object]:
    """Verify the immutable official ZIP, stream its CSV, and atomically publish outputs."""
    input_path = Path(input_zip)
    output_path = Path(output_dir)
    selected_filters = filters or Faf5Filters()
    _validate_new_output_dir(output_path)
    temp_dir: Path | None = None
    published = False
    try:
        with input_path.open("rb") as archive_file:
            file_stat = os.fstat(archive_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise RecipeError("FAF5 input must be a regular local file")
            if file_stat.st_size != _PINNED_SIZE_BYTES:
                raise RecipeError(
                    f"FAF5 input size mismatch: expected {_PINNED_SIZE_BYTES} bytes, got {file_stat.st_size}"
                )
            input_sha256 = _sha256_fileobj(archive_file)
            if input_sha256 != _PINNED_SHA256:
                raise RecipeError("FAF5 input SHA-256 does not match the pinned official release")
            archive_file.seek(0)
            with zipfile.ZipFile(archive_file, mode="r") as archive:
                csv_info = _verify_archive_members(archive)
                temp_dir = _make_temp_dir(output_path)
                with archive.open(csv_info, mode="r") as member:
                    with io.TextIOWrapper(member, encoding="utf-8-sig", newline="") as source:
                        manifest = _prepare_output(
                            temp_dir,
                            source,
                            input_size_bytes=file_stat.st_size,
                            input_sha256=input_sha256,
                            filters=selected_filters,
                        )
            if _sha256_fileobj(archive_file) != input_sha256:
                raise RecipeError("FAF5 input bytes changed while the archive was being parsed")
        _commit_output(temp_dir, output_path)
        published = True
        return manifest
    except zipfile.BadZipFile as error:
        raise RecipeError(f"FAF5 input is not a valid ZIP: {error}") from error
    except (zipfile.LargeZipFile, RuntimeError) as error:
        raise RecipeError(f"FAF5 ZIP could not be read safely: {error}") from error
    finally:
        if not published:
            _remove_temp_dir(temp_dir)
