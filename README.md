# faf5-recipe

`faf5-recipe` is a local, standard-library-only extractor for one official FAF5 release. It copies the local ZIP to a private temporary snapshot while hashing, then parses that same snapshot only if its byte size and SHA-256 match the fixed release pin. It streams the CSV member, applies exact code/year/scenario filters, and writes a deterministic long CSV with a provenance manifest. It does not download data, extract ZIP paths, contact a service, or update to a newer release automatically.

## Supported release and interpretation

Only **FAF5.7.1-regional-mid** is supported:

- Official download: [FAF5.7.1.zip](https://faf.ornl.gov/faf5/Data/Download_Files/FAF5.7.1.zip)
- Expected size: `305397783` bytes
- Expected SHA-256: `1429be77a578f2b32913320a1299c0e3de8aa9c6180ce5aacfd65a7886ba55b8`

The extractor rejects any other bytes or ZIP member/schema layout. It requires `FAF5.7.1.csv` and the bundled `FAF5_metadata.xlsx`; it reads the CSV directly from the ZIP without writing archive members to disk.

The checksum was computed from the downloaded official archive; it is a reproducibility pin, not a publisher signature or an independent guarantee of the dataset's accuracy.

For this release, `estimate_status` and `scenario` are assigned as follows:

| Year | `estimate_status` | `scenario` |
| --- | --- | --- |
| 2017 | `base_year` | `annual_estimate` |
| 2018–2023 | `final_annual` | `annual_estimate` |
| 2024 | `preliminary_annual` | `annual_estimate` |
| 2030, 2035, 2040, 2045, 2050 | `forecast` | `baseline` |

The pinned mid-release has no low/high forecast columns. Such scenario filters are rejected rather than inferred or synthesized. `annual_estimate` describes FAF estimates, not observed shipments. Do not describe these regional modeled flows as terminal-level, parcel-level, road-path, vessel, or shipment observations.

The output uses `tons_YEAR`, `value_YEAR`, and `tmiles_YEAR`: thousand tons, million US dollars in constant 2017 dollars, and million ton-miles, respectively. `current_value_2018` through `current_value_2024` are validated as numeric input fields for dimension-matched rows but intentionally excluded from output; the output value field is the constant-2017-dollar `value_YEAR`, not current dollars. Missing measures remain empty CSV cells, never zero.

Dimension codes remain strings, including leading zeros. Consult the [official FAF5 site and its codebook/documentation](https://faf.ornl.gov/faf5/) for code meanings; the extractor does not invent labels or validate codebook membership.

## Reproduce a corridor extract

Example question: for domestic FAF region code `061` (Los Angeles–Long Beach, CA CFS area) to `481` (Austin–Round Rock, TX CFS area), how do the 2024 preliminary annual estimate and 2030 baseline forecast compare across commodities for truck mode `1`, domestic trade type `1`? The bundled FAF metadata identifies the two region codes; use the official documentation for code definitions and scope.

Download and verify the exact release yourself:

```sh
curl --fail --location \
  --output FAF5.7.1.zip \
  https://faf.ornl.gov/faf5/Data/Download_Files/FAF5.7.1.zip

printf '%s  %s\n' \
  '1429be77a578f2b32913320a1299c0e3de8aa9c6180ce5aacfd65a7886ba55b8' \
  'FAF5.7.1.zip' | shasum -a 256 -c -
```

Install the package (Python 3.12 or newer):

```sh
python -m pip install "faf5-recipe @ git+https://github.com/smynkr/faf5-recipe.git"
```

Run the same pinned checks and extract only that corridor and the two requested years:

```sh
faf5-recipe extract FAF5.7.1.zip \
  --output-dir faf5-061-to-481 \
  --origin 061 \
  --destination 481 \
  --mode 1 \
  --trade 1 \
  --year 2024 \
  --year 2030 \
  --scenario annual_estimate \
  --scenario baseline
```

Equivalent module invocation is `python -m faf5_recipe extract ...`. Each filter option is repeatable: repeated codes within one dimension are ORed, while different dimensions are ANDed. Code and year values are compared as exact strings; leading zeros are significant.

Verified against the complete pinned archive on 2026-10-07: this command scans **2,671,386 source rows**, matches **28 rows**, and emits **56 facts**. Its `faf5_flows.csv` SHA-256 is `297790d4340a8f3b10de8a018f90fa2183c78641db11395e8be8dda30df6b6f3`. All exported dimensions and measure strings were reconciled against a separate source scan; replay produced byte-identical CSV and manifest files. Different filters intentionally produce different outputs.

The new output directory contains:

- `faf5_flows.csv`: one row per matching source row/year/scenario with at least one of tons, constant-2017-dollar value, or ton-miles present. Rows retain source order, then release year order; `source_row_number` is a one-based position in the CSV data rows. All domestic and foreign origin/destination, mode, commodity, trade, and distance dimensions are retained as strings.
- `manifest.json`: release and source identity, selected filters, year/scenario semantics, units/value basis, transformation version, row/fact/no-measure counts, validation limits, and the CSV SHA-256. Its input SHA-256 identifies the private snapshot that was parsed. It contains no local input/output path or run timestamp; the same input and filters produce byte-identical outputs.

`source_rows` is the number of data rows in the CSV. `matched_rows` and `excluded_rows` describe only the five optional code-dimension filters and sum to `source_rows`; year/scenario filters apply to facts after row matching. `no_measure_cases` counts matched row/year combinations selected by year/scenario whose three exported measures are all empty. The manifest makes explicit that numeric measures for dimension-excluded rows are not checked and that codebook membership and cross-measure semantics are not validated.

The extractor bounds input by the pinned archive/member sizes and a 65,536-character CSV field limit, and streams rows without retaining the national table in memory. Before ZIP parsing, it copies at most the pinned 305,397,783 archive bytes to a private temporary file in the system temp directory while calculating the SHA-256; only that verified snapshot is parsed. This requires up to 305,397,783 bytes of temporary storage and prevents changes to the original path from changing the bytes that are extracted. Long-form output may be larger than the compressed source. Both output files are staged in a temporary sibling directory and published together by a directory rename. The output path must not already exist and its parent must already exist. Run one writer at a time in a parent directory you control; the existence recheck is not a multi-writer lock and does not promise race-safe concurrent publication.

## Development and shipping

The runtime has no third-party dependencies and requires Python 3.12 or newer. The package defines both the `faf5-recipe` entry point and `python -m faf5_recipe`. The behavioral suite uses only `unittest`; after installing the package, run `python -m unittest discover -s tests`.

Use feature branches and pull requests. Exercise the installed CLI against the complete pinned release before release. Changes require independent review, resolved substantive findings, and successful candidate CI. Do not commit downloaded datasets, credentials, local review logs, or private project history. The initial default branch is an owner-authorized fresh repository bootstrap.

## License and data terms

The software is MIT licensed; see [LICENSE](LICENSE). That software license does not grant rights to FAF data or bundled source materials. Obtain the dataset from ORNL, follow its applicable terms and attribution guidance, and do not redistribute the downloaded dataset as part of this package. This is a small, release-pinned utility with no SLA, hosted service, or automatic update policy.

Source attribution: U.S. Department of Transportation, Bureau of Transportation Statistics and Federal Highway Administration, Freight Analysis Framework 5.7.1, distributed by Oak Ridge National Laboratory. This project is not affiliated with or endorsed by those organizations.
