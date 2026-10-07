"""Command-line interface for the pinned FAF5 recipe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .core import Faf5Filters, RecipeError, YEARS, extract_archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faf5-recipe",
        description="Extract a filtered long CSV from the pinned FAF5.7.1 regional release.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser(
        "extract",
        help="verify and extract the pinned official FAF5.7.1 ZIP",
        description="The ZIP must match the release's fixed byte size and SHA-256 digest.",
    )
    extract.add_argument("input_zip", type=Path, help="local FAF5.7.1.zip downloaded from ORNL")
    extract.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        metavar="NEW_DIRECTORY",
        help="new output directory; an existing path is never replaced",
    )
    extract.add_argument(
        "--origin",
        action="append",
        default=None,
        metavar="CODE",
        help="exact domestic FAF origin code (repeatable; leading zeros are significant)",
    )
    extract.add_argument(
        "--destination",
        action="append",
        default=None,
        metavar="CODE",
        help="exact domestic FAF destination code (repeatable)",
    )
    extract.add_argument(
        "--commodity",
        action="append",
        default=None,
        metavar="CODE",
        help="exact SCTG2 commodity code (repeatable)",
    )
    extract.add_argument(
        "--mode",
        action="append",
        default=None,
        metavar="CODE",
        help="exact domestic mode code (repeatable)",
    )
    extract.add_argument(
        "--trade",
        action="append",
        default=None,
        metavar="CODE",
        help="exact trade type code (repeatable)",
    )
    extract.add_argument(
        "--year",
        action="append",
        default=None,
        choices=tuple(str(year) for year in YEARS),
        metavar="YEAR",
        help="estimate year from the pinned release (repeatable)",
    )
    extract.add_argument(
        "--scenario",
        action="append",
        default=None,
        choices=("annual_estimate", "baseline"),
        metavar="SCENARIO",
        help="annual_estimate for 2017-2024 or baseline for future years (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command != "extract":
        raise AssertionError("unreachable command")
    try:
        filters = Faf5Filters.from_values(
            origins=arguments.origin,
            destinations=arguments.destination,
            commodities=arguments.commodity,
            modes=arguments.mode,
            trades=arguments.trade,
            years=arguments.year,
            scenarios=arguments.scenario,
        )
        manifest = extract_archive(arguments.input_zip, arguments.output_dir, filters=filters)
    except (RecipeError, OSError, UnicodeError) as error:
        print(f"faf5-recipe: {error}", file=sys.stderr)
        return 2

    counts = manifest["counts"]
    print(
        f"Wrote {counts['output_facts']} facts from {counts['matched_rows']} matched source rows; "
        f"{counts['excluded_rows']} rows excluded."
    )
    print(f"CSV: {arguments.output_dir / 'faf5_flows.csv'}")
    print(f"Manifest: {arguments.output_dir / 'manifest.json'}")
    return 0
