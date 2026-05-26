# Handling documentation files (Belege) in pyDATEV

This document is for users who want to manage **Belege** — the
documentation files (invoices, receipts, contracts, …) that DATEV
expects to ship alongside a Buchungsstapel CSV. For plain
Buchungsstapel CSV handling, see [README.md](./README.md); this
document focuses on the Document-Package side.

---

## Overview

DATEV's Belegtransfer format pairs a Buchungsstapel CSV (the
booking lines) with a `belege.zip` ("Document Package") that
contains the actual documentation files plus a `document.xml`
manifest. Each booking row references its Beleg via the
`Beleglink` column (a `BEDI "<UUID>"` provider-prefix string),
and the same UUID appears as `<document guid="…">` in the
manifest.

pyDATEV implements both sides through three small components:

```text
┌──────────────────┐                ┌──────────────────┐
│   Beleg          │  add(beleg)    │   Belegarchiv    │  save() ┐
│ (file + meta)    │ ─────────────► │ (file manager)   │ ────────┼──► belege.zip
│  .guid           │                │  .data: list[…]  │         │   + document.xml
│  .filename       │                │  .add() (dedup)  │ load()  │
│  .blob           │                │  .save() / load()│ ◄───────┘
│  .belegtyp       │                └────────┬─────────┘
└──────▲───────────┘                         │
       │                              owns   │
       │                                     ▼
       │                            ┌──────────────────┐
       │  field-like API:           │  Buchungsstapel  │  save() ─► EXTF_…csv
       └────────────────────────────┤  .belege         │              + belege.zip
              entry['Beleg'] = beleg│  .data: entries  │
                                    └──────────────────┘
```

| Component | Responsibility |
|---|---|
| `Beleg` | A single documentation file plus the metadata that `document.xml` needs (guid, archive filename, blob, belegtyp). Constructed from a path on disk. |
| `Belegarchiv` | File manager: collects `Beleg` objects (idempotent by GUID), writes a `belege.zip` with a consistent `document.xml`, and reads the same back. Can be used stand-alone. |
| `Buchungsstapel` | Owns a `Belegarchiv` at `self.belege`. The `Buchungsentry` rows expose a field-like API: `entry['Beleg'] = path_or_beleg`. `bs.save(csv)` auto-writes `belege.zip` next to the CSV when needed. |

---

## Import

Belege classes live in an opt-in submodule so that plain
`import pydatev` keeps the original upstream surface unchanged.
Anything Beleg-related is reached via `pydatev.belegarchiv`:

```python
from pydatev.belegarchiv import Buchungsstapel, Beleg, Belegarchiv
from pydatev.belegarchiv import BELEGTYP_RECHNUNGSEINGANG
```

For terse code in this document we'll alias the whole submodule:

```python
from pydatev import belegarchiv as pydatev_be
```

The `pydatev_be.Buchungsstapel` exposed by the submodule is a
subclass of the core `pydatev.Buchungsstapel` — drop-in compatible,
plus the Belege hooks (`self.belege`, the `entry["Beleg"]` field-like
API, and `belege.zip` save/load orchestration).

---

## Requirements

- **Python**: 3.6 or later (matches pyDATEV's base requirement). No
  new third-party dependencies are introduced — Beleg handling uses
  only the standard library (`zipfile`, `xml.etree.ElementTree`,
  `uuid`, `os`, `re`).
- **Disk-only input**: `Beleg(...)` reads from a file path. If your
  Beleg content lives in memory (e.g. a database blob), materialise
  it to a temporary file first and pass the path.
- **Schema-conformant GUIDs**: every Beleg needs a 36-character UUID
  matching the DATEV Belegtransfer schema constraint. pyDATEV derives
  one automatically (UUIDv8 from SHA-256 of namespace + archive name +
  blob — see `_beleg_uuid8`) if you don't supply one explicitly. The
  result is stable across re-exports of the same (name, content) and
  distinguishes both filename collisions and content changes.
- **Allowed file types**: only the DATEV-accepted extensions are
  permitted on construction — see *Supported file types* below.
  Imports of existing archives are **not** validated (round-trip
  stability).

---

## The `Beleg` class

```python
pydatev_be.Beleg(
    filepath,                  # str or os.PathLike — file on disk
    belegtyp=None,             # see Belegtyp constants below
    guid=None,                 # auto if omitted (UUIDv8 / SHA-256)
    archive_name=None,         # defaults to os.path.basename(filepath)
)
```

The constructor reads `filepath` into memory and sets:

- `.guid` — 36-char UUIDv8 (RFC 9562), derived from SHA-256 of
  `(namespace || archive_name || 0x00 || blob)` when not supplied.
- `.filename` — the name used inside the ZIP archive.
- `.blob` — the file contents as `bytes`.
- `.belegtyp` — `pydatev_be.BELEGTYP_RECHNUNGSEINGANG` (`"1"`),
  `pydatev_be.BELEGTYP_RECHNUNGSAUSGANG` (`"2"`), or `None`.

Convenience method:

```python
beleg.write_to(target_dir)     # writes the blob back to disk; returns path
```

Construction raises `pydatev.DatevFormatError` (the core class — also
re-exported as `pydatev_be.DatevFormatError`) for an unsupported
extension, a malformed GUID, or an invalid belegtyp.

---

## The `Belegarchiv` class

```python
pydatev_be.Belegarchiv(
    filename=None,             # if given, load from this ZIP
    description="",
    generating_system="pydatev",
    schema_version="v04.0",    # "v04.0" or "v06.0"
    created=None,              # datetime, defaults to now
)
```

Methods:

```python
archive.add(beleg)             # idempotent by guid; returns the stored Beleg
archive.remove(guid_or_beleg)  # remove; returns the removed Beleg; KeyError if absent
archive.clear()                # empty the archive (data + index)
archive.get_by_guid(guid)      # → Beleg or None
archive.save(zip_path)         # writes ZIP + document.xml manifest
archive.load(zip_path)         # populates archive.data from an existing ZIP
```

Container protocol — natural listing and inspection:

```python
len(archive)                   # number of Belege
for beleg in archive: ...      # iterate (same as `for b in archive.data`)
guid in archive                # membership (also accepts a Beleg instance)
archive[guid]                  # indexed access; raises KeyError if absent
```

`archive.data` is the underlying list of `Beleg` objects.

**Dedup semantics**: `add(b)` looks up `b.guid` in the archive. If a
Beleg with that GUID is already present, the existing entry is
returned untouched and the new one is ignored. This means attaching
the same file to several bookings produces one ZIP entry and one
GUID.

**Removal + consistency**: `remove()` drops the Beleg from both
`data` and the internal index. The next `save()` regenerates
`document.xml` and the ZIP payload from `data`, so the two stay in
sync automatically — no dangling `<document>` entry, no leftover
binary in the ZIP. If a Buchungsstapel entry still references the
removed GUID via `Beleglink`, that reference becomes a dangling
link; clear it with `entry['Beleg'] = None` on each affected entry.

**Round-trip guarantee**: `load(zip)` does not validate file
extensions or content — whatever is in the archive is preserved
as-is. A subsequent `save(zip)` produces a bit-equivalent archive
(same blobs, same filenames, same GUIDs).

---

## Buchungsstapel integration

A `pydatev_be.Buchungsstapel` instance owns one `Belegarchiv`,
accessible at `bs.belege`. Each booking row (a `Buchungsentry`,
subclass of `DatevEntry`) supports two extra pseudo-fields:

```python
entry['Beleg']    = './invoice-001.pdf'   # path OR a Beleg object
entry['Belegtyp'] = pydatev_be.BELEGTYP_RECHNUNGSEINGANG
```

Behaviour:

- Setting `entry['Beleg']` constructs a `Beleg` (if you pass a path),
  adds it to `bs.belege` (dedup), and writes `entry['Beleglink'] =
  'BEDI "<UUID>"'` into the CSV column.
- Setting `entry['Belegtyp']` adjusts the bound Beleg's belegtyp.
  Both orderings work — `Belegtyp` before `Beleg` is buffered and
  applied once a Beleg is attached.
- Reading `entry['Beleg']` returns the `Beleg` object (or `None` if
  none attached). The lookup goes through `Beleglink` → `bs.belege`.
- `bs.save(csv_path)` writes the CSV; if `bs.belege.data` is
  non-empty, it also writes `belege.zip` in the same directory.
- `pydatev_be.Buchungsstapel(filename=csv_path)` loads the CSV; if a
  `belege.zip` lives next to it, the archive is loaded into
  `bs.belege` and entries' `entry['Beleg']` lookups work.

---

## Examples

### Add a single invoice to a booking

```python
import datetime
from pydatev import belegarchiv as pydatev_be

bs = pydatev_be.Buchungsstapel(berater=1001, mandant=1,
    wirtschaftsjahr_beginn=datetime.date(2025,1,1),
    sachkontennummernlänge=4,
    datum_von=datetime.date(2025,1,1),
    datum_bis=datetime.date(2025,12,31),
    waehrungskennzeichen='EUR')

entry = bs.add_buchung(umsatz=34.56, soll_haben='S',
    konto='3333', gegenkonto='1111',
    belegdatum=datetime.date(2025,2,1))
entry['Beleg']    = './invoice-001.pdf'
entry['Belegtyp'] = pydatev_be.BELEGTYP_RECHNUNGSEINGANG

bs.save('./out/EXTF_buchungsstapel_2025.csv')
# Produces: ./out/EXTF_buchungsstapel_2025.csv + ./out/belege.zip
```

### Bulk-import a folder of PDFs into a stand-alone Belegarchiv

```python
import os
from pydatev import belegarchiv as pydatev_be

archive = pydatev_be.Belegarchiv(description="Belege 2025",
                                 generating_system="my-tool")
for name in sorted(os.listdir('./pdfs/')):
    if name.lower().endswith('.pdf'):
        archive.add(pydatev_be.Beleg(os.path.join('./pdfs/', name)))
archive.save('./belege.zip')
```

### Round-trip a Buchungsstapel through pyDATEV and back

```python
from pydatev import belegarchiv as pydatev_be

bs = pydatev_be.Buchungsstapel(filename='./EXTF_buchungsstapel_2025.csv')
# bs.belege.data populated from ./belege.zip if it exists

# … inspect or modify …
for entry in bs.data:
    beleg = entry['Beleg']
    if beleg:
        print(entry['Beleglink'], '->', beleg.filename, beleg.belegtyp)

bs.save('./EXTF_buchungsstapel_2025.csv')
# CSV and belege.zip rewritten — content bit-identical for unchanged
# parts (Beleg blobs are preserved verbatim from load → save).
```

### Extract every Beleg from a loaded archive back to disk

```python
import os
from pydatev import belegarchiv as pydatev_be

archive = pydatev_be.Belegarchiv(filename='./belege.zip')
os.makedirs('./extracted/', exist_ok=True)
for beleg in archive.data:
    beleg.write_to('./extracted/')
```

---

## Supported file types

The DATEV Belegtransfer specification permits the following file
types (`pydatev_be.SUPPORTED_BELEG_EXTENSIONS`). `Beleg(...)` rejects
any other extension at construction time; `Belegarchiv.load()` does
not enforce the allowlist (round-trip stability).

| Group | Extensions |
|---|---|
| Image | `jpg`, `jpeg`, `png`, `tif`, `tiff`, `bmp`, `gif` |
| Office | `doc`, `docx`, `xls`, `xlsx`, `odt`, `ods` |
| Text | `txt`, `rtf`, `csv` |
| Special | `pdf`, `msg`, `xml` |

(Source: DATEV Hilfe-Center Dok.-Nr. 1000312 — *"Zulässige
Dateiformate für die Übertragung digitaler Belege"*. The document
is gated to authenticated DATEV customers and reachable via the
Hilfe-Center search at <https://apps.datev.de/help-center>; no
public URL is available.)

---

## Schema versions

`document.xml` exists in two DATEV schema variants. pyDATEV defaults
to **v04.0** because it is the variant that DATEV partners
(BuchhaltungsButler, Kontolino, …) consistently produce on
round-trip exports, so it has been validated against real consumers.
**v06.0** is the newer DATEV-Belegtransfer revision and is available
for modern DATEV-Online integrations.

```python
archive = pydatev_be.Belegarchiv(schema_version="v06.0",
                                 description="Belege 2025")
```

On load, pyDATEV detects the namespace and selects the matching
schema automatically.

---

## Backward compatibility

The Beleg extension lives in an opt-in submodule, so the surface
seen by plain `import pydatev` is **unchanged** vs upstream
Fjanks/pydatev (modulo the v0.2.2 Text round-trip bugfix). Pre-Beleg
code that does `import pydatev; bs = pydatev.Buchungsstapel(...)`
works identically — no `belege.zip` written, no `.belege` attribute,
no Buchungsentry subclass exposed.

Users who want Belege opt in via
`from pydatev.belegarchiv import Buchungsstapel, Beleg, ...`:

- `pydatev_be.Buchungsstapel` is a subclass of
  `pydatev.Buchungsstapel`, so existing isinstance checks against
  the core class continue to work for both.
- `bs.add_buchung(...)` (on the submodule's Buchungsstapel) returns
  a `Buchungsentry` (subclass of `DatevEntry`).
  `isinstance(entry, DatevEntry)` continues to hold; only a brittle
  `type(entry) is DatevEntry` check would break.

---

## Edge cases

- **Same file on multiple bookings**: `entry['Beleg'] = path` on a
  second booking with byte-identical content under the same archive
  name reuses the existing Beleg — dedup is by GUID, derived as
  UUIDv8 over `(archive_name, sha256(blob))`. One Beleg in the ZIP;
  both bookings share the same `Beleglink` GUID. Different content
  under the same name, or the same content under different names,
  yield distinct GUIDs (no silent collisions).
- **Orphan Belege on load**: if a `belege.zip` contains documents
  that no booking row references, they remain in `bs.belege.data`
  after load. Round-trip preserves them on the next save.
- **Stand-alone Belege without a booking**: just call
  `bs.belege.add(pydatev_be.Beleg(path))` (or use a top-level
  `pydatev_be.Belegarchiv` without a Buchungsstapel). Useful when
  staging files before the matching booking exists, or when
  re-saving an archive whose bookings are managed elsewhere.
- **Beleglink round-trip with embedded quotes**: pyDATEV's CSV `Text`
  parser correctly un-doubles internal `"` on load (since v0.2.2),
  so a `Beleglink` written as `BEDI "<UUID>"` round-trips intact.
  `entry['Beleg']` extracts the GUID via regex from either the
  quoted or unquoted form for robustness against legacy archives.
- **Empty archive**: `Belegarchiv.save()` refuses to write an empty
  archive (DATEV consumers reject such files). `Buchungsstapel.save()`
  simply skips the ZIP step if `bs.belege.data` is empty.
