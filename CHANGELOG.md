# Changelog

## 1.0.1 — 2026-10-07

### Fixed

- Hash a private, size-bounded temporary snapshot and parse that same snapshot, so concurrent changes to the input path cannot substitute unpinned FAF data after verification.
