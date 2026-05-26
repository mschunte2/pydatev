# Changelog

All notable changes to this fork of pyDATEV are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning per [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] — 2026-05-26

### Changed

- **Beleg/Belegarchiv code moved out of `pydatev.py` into a new
  opt-in submodule `pydatev.belegarchiv`.** Users who only need
  plain Buchungsstapel CSV handling see the upstream-equivalent
  surface via `import pydatev` (no Beleg names exposed, no
  `Buchungsstapel.belege` attribute, no auto-write of
  `belege.zip`). Users who want Belege opt in via:

  ```python
  from pydatev.belegarchiv import Buchungsstapel, Beleg, Belegarchiv
  ```

  The `Buchungsstapel` exposed by `pydatev.belegarchiv` is a
  subclass of the core `pydatev.Buchungsstapel` (drop-in
  replacement) that adds:
  - `self.belege` (a `Belegarchiv` instance)
  - the field-like `entry["Beleg"] = path` row-attachment API
  - automatic `belege.zip` write next to the CSV on `save()`
  - automatic `belege.zip` read from the same directory on
    `load()`

  **Breaking change for downstream import paths**: callers must
  switch `pydatev.Beleg` → `pydatev.belegarchiv.Beleg`,
  `pydatev.BELEGTYP_*` → `pydatev.belegarchiv.BELEGTYP_*`, etc.
  Functional behavior of the moved classes is unchanged. Existing
  on-disk archives load identically.

- **`src/pydatev/pydatev.py` is now ~417 lines** (down from 826),
  matching the upstream `Fjanks/pydatev` baseline at commit
  `8ca2369` modulo a single 3-line hunk: the `datev2python` Text
  branch's CSV-escape fix (un-doubles internal quotes, strips outer
  wrappers — inverse of the existing `python2datev` escape). The
  diff vs upstream is now visually trivial to review.

- **New regression tests in `tests/test_module_isolation.py`** pin
  the surface boundary: `import pydatev` is asserted to expose no
  Beleg-related names, the core `Buchungsstapel` has no `belege`
  attribute, and `pydatev.belegarchiv.Buchungsstapel` is a true
  subclass of the core class.

## [0.3.1] — 2026-05-26

### Changed

- **`Beleg` default GUID is now a UUIDv8 (RFC 9562) backed by
  SHA-256** instead of a uuid5 (which is fixed to SHA-1 internally).
  The full derivation is:

  ```text
  digest = sha256(namespace.bytes || archive_name.utf8 || 0x00 || blob)
  guid   = uuid.UUID(bytes=digest[:16] with version=8, variant=RFC4122)
  ```

  Same (name, content) tuple → same GUID, same hybrid-identity
  semantics as 0.3.0. The change is the *algorithm*: SHA-1 has had
  practical collision attacks since 2017 (SHAttered) and chosen-prefix
  collisions since 2020, so SHA-256 in the chain is the forward-looking
  default. For this non-adversarial use case the 128-bit output is the
  real collision bottleneck either way; the upgrade is design hygiene.

  **Breaking change vs 0.3.0**: GUIDs of the form produced by 0.3.0's
  brief uuid5-with-content-hash construction will not match 0.3.1.
  Callers that pass an explicit `guid=` are unaffected. Existing
  archives load unchanged (GUIDs are read from `document.xml`, not
  recomputed).

  New helper `_beleg_uuid8(name, blob)` in `pydatev.pydatev`. New
  regression test `test_default_guid_is_uuidv8` confirms the version
  and variant bits.

## [0.3.0] — 2026-05-26

### Changed

- **`Beleg` default GUID derivation switched from
  `uuid5(NS, archive_name)` to `uuid5(NS, archive_name + "\0" +
  sha256(blob).hexdigest())`** — a hybrid of filename and content.
  This fixes two failure modes of the previous derivation:
  - *False collisions*: two genuinely different files filed under the
    same `archive_name` now get distinct GUIDs, so
    `Belegarchiv.add()` no longer silently drops the second one as a
    "duplicate".
  - *False splits avoided*: byte-identical files filed under
    different `archive_name`s still get distinct GUIDs, preserving
    business-identity-via-filename (e.g. two empty placeholder
    Belege filed under separate names stay as two Belege).
  Round-trip stability across re-exports (the v0.2.1 contract) is
  preserved as long as both the archive name and the file bytes are
  stable — which is the typical case for broker-export PDFs stored
  in a content-addressable backing store.

  **Breaking change**: re-exporting a Beleg whose source bytes have
  changed between exports will now produce a different default GUID.
  Code that overrides via `guid=...` is unaffected. Existing
  archives load unchanged (their GUIDs are read from `document.xml`,
  not recomputed).

  New regression tests at `tests/test_belegarchiv.py`:
  `test_default_guid_distinguishes_different_content_same_name` and
  `test_default_guid_distinguishes_same_content_different_name`.

## [0.2.2] — 2026-05-26

### Fixed

- **Round-trip of Text values with embedded quotes.** `python2datev`
  correctly CSV-escapes Text values by doubling internal `"` and
  wrapping in outer `"..."` (commit 8ca2369), but `datev2python` was
  stripping *all* quotes instead of performing the inverse unescape.
  As a result, saving an entry with `Beleglink = 'BEDI "<UUID>"'` and
  loading it back yielded `'BEDI <UUID>'`, breaking BEDI-based
  Beleg-Verknüpfung on round-trip. The Text branch in
  `DatevEntry.datev2python` now strips the outer wrapper and undoubles
  internal quotes (`string[1:-1].replace('""', '"')`). New regression
  test at `tests/test_text_roundtrip.py` covers the BEDI shape and
  seven other embedded-quote variants.

### Changed

- **Beleg file-types reference now names the specific DATEV
  Hilfe-Center document** (Dok.-Nr. 1000312 — *"Zulässige Dateiformate
  für die Übertragung digitaler Belege"*) instead of the generic
  help-center landing page. The Hilfe-Center document itself is gated
  to authenticated DATEV customers, so no public URL is cited;
  customers can find it via the Hilfe-Center search. Updated in both
  `SUPPORTED_BELEG_EXTENSIONS`'s code comment and `File-handling.md`.

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
