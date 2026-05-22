# Changelog

All notable changes to this fork of pyDATEV are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning per [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.1] — 2026-05-22

### Fixed

- **`Beleg` default-GUID was unstable across runs.** It used to derive
  from `uuid5(NS, os.path.abspath(filepath))`, so every time the same
  logical Beleg was written from a different working directory or
  TempDir, it got a fresh GUID. That defeats downstream auto-attach
  flows (e.g. BuchhaltungsButler's CSV-Beleglink matching against
  an already-uploaded Beleg-Archiv). Now derived from
  `uuid5(NS, archive_name)` — the same name that ends up in
  `document.xml` — so the GUID is location-independent and
  re-export-stable. Code that passes an explicit `guid=...` is
  unaffected.

## [0.2.0] — 2026-05-22

### Added

- **`Belegarchiv` data category** — DATEV Document Package
  (`belege.zip` + `document.xml` manifest) for both export and import,
  supporting schemas v04.0 and v06.0.
- **`Beleg` class** — file-backed object holding the metadata
  `document.xml` needs (`guid`, `filename`, `blob`, `belegtyp`).
  Constructed from a path on disk; validates extension against
  `SUPPORTED_BELEG_EXTENSIONS`.
- **Field-like Beleg-API on `Buchungsstapel` rows** — assign
  `entry['Beleg'] = path_or_beleg` to attach a file; pydatev sets the
  CSV `Beleglink` column to `BEDI "<UUID>"`, dedups by GUID, and writes
  `belege.zip` next to the CSV on `bs.save()`. `entry['Beleg']` also
  reads back the Beleg object after load.
- **Management functions** on `Belegarchiv`: `add()`, `remove()`,
  `clear()`, `get_by_guid()`, plus the container protocol
  (`__iter__`, `__len__`, `__contains__`, `__getitem__`).
- Module-level constants `BELEGTYP_RECHNUNGSEINGANG`,
  `BELEGTYP_RECHNUNGSAUSGANG`, and `SUPPORTED_BELEG_EXTENSIONS`.
- New [File-handling.md](./File-handling.md) — user documentation with
  architecture, API reference, examples, and edge cases.
- Unit tests at `tests/test_belegarchiv.py` (8 tests covering
  roundtrip v04.0/v06.0, dedup, orphan-survival, management
  functions, and backward compatibility).

### Changed

- README mission line and state-of-implementation table updated to
  reflect that Belege management is now in scope.
- `setup.cfg` description and version (0.1.0 → 0.2.0).

### Backward compatibility

Strictly additive. Pre-Beleg code that uses only `Buchungsstapel` is
unaffected — the CSV bytes produced for a stapel that never touches
Belege are bit-identical to v0.1.0 output, and no `belege.zip` is
written alongside.

## [0.1.0] — historical baseline

Initial published state of the fork (matches upstream
[Fjanks/pydatev](https://github.com/Fjanks/pydatev) at commit
`8ca2369`, "Allow quotation marks in Text values"). Includes
`Buchungsstapel` for DATEV format versions 9–13.
