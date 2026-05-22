"""Tests for the Beleg/Belegarchiv/Buchungsentry extension."""

import datetime
import os
import zipfile
import xml.etree.ElementTree as ET

import pytest

import pydatev


# ---------- Helpers ----------

def _make_pdf(dirpath, name="test.pdf", content=b"%PDF-1.4 hello"):
    path = os.path.join(dirpath, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _new_stapel():
    return pydatev.Buchungsstapel(
        berater=1001, mandant=1,
        wirtschaftsjahr_beginn=datetime.date(2025, 1, 1),
        sachkontennummernlänge=4,
        datum_von=datetime.date(2025, 1, 1),
        datum_bis=datetime.date(2025, 12, 31),
        waehrungskennzeichen="EUR",
    )


# ---------- Tests ----------

def test_field_like_attach_read_roundtrip_v040(tmp_path):
    """entry['Beleg'] = path → save → load → entry['Beleg'] gives back
    equivalent Beleg-object (filename, blob, belegtyp preserved).
    """
    pdf = _make_pdf(str(tmp_path), name="doc-001.pdf", content=b"PAYLOAD")

    bs = _new_stapel()
    entry = bs.add_buchung(
        umsatz=10.0, soll_haben="S", konto="3333", gegenkonto="1111",
        belegdatum=datetime.date(2025, 2, 1))
    entry["Beleg"] = pdf
    entry["Belegtyp"] = pydatev.BELEGTYP_RECHNUNGSEINGANG

    csv_path = str(tmp_path / "EXTF_t.csv")
    bs.save(csv_path)
    assert (tmp_path / "belege.zip").exists()

    bs2 = pydatev.Buchungsstapel(filename=csv_path)
    assert len(bs2.belege.data) == 1
    e2 = bs2.data[0]
    beleg = e2["Beleg"]
    assert beleg is not None
    assert beleg.filename == "doc-001.pdf"
    assert beleg.blob == b"PAYLOAD"
    assert beleg.belegtyp == pydatev.BELEGTYP_RECHNUNGSEINGANG


def test_default_guid_is_stable_across_paths(tmp_path):
    """The default GUID must be derived from the *archive name* (what
    ends up in document.xml), not from the filesystem absolute path —
    so two files with the same archive_name but different on-disk
    locations get the same GUID. This is what downstream systems
    matching against an already-uploaded Beleg-Archiv depend on; the
    previous abspath-based derivation broke that contract because the
    path changes per run (TempDir etc.)."""
    # Same archive_name, two different directories on disk.
    dir_a = tmp_path / "run_one"
    dir_b = tmp_path / "run_two"
    dir_a.mkdir()
    dir_b.mkdir()
    pdf_a = _make_pdf(str(dir_a), name="VV-23.pdf", content=b"%PDF-A")
    pdf_b = _make_pdf(str(dir_b), name="VV-23.pdf", content=b"%PDF-A")
    b_a = pydatev.Beleg(filepath=pdf_a)
    b_b = pydatev.Beleg(filepath=pdf_b)
    assert b_a.guid == b_b.guid, (
        f"Default GUID must be stable across paths for the same "
        f"archive_name; got {b_a.guid!r} vs {b_b.guid!r}"
    )
    # And an explicit archive_name override should produce the same
    # GUID as the implicit one when the name matches.
    pdf_c = _make_pdf(str(dir_a), name="other-on-disk.pdf", content=b"x")
    b_c = pydatev.Beleg(filepath=pdf_c, archive_name="VV-23.pdf")
    assert b_c.guid == b_a.guid


def test_dedup_same_guid(tmp_path):
    """Two entries set the same file → archive contains it once;
    both entries' Beleglink GUIDs match the same Beleg."""
    pdf = _make_pdf(str(tmp_path), name="shared.pdf")
    bs = _new_stapel()
    e1 = bs.add_buchung(
        umsatz=1.0, soll_haben="S", konto="3333", gegenkonto="1111",
        belegdatum=datetime.date(2025, 2, 1))
    e2 = bs.add_buchung(
        umsatz=2.0, soll_haben="S", konto="3333", gegenkonto="1111",
        belegdatum=datetime.date(2025, 2, 1))
    e1["Beleg"] = pdf
    e2["Beleg"] = pdf

    assert len(bs.belege.data) == 1
    assert e1["Beleglink"] == e2["Beleglink"]


def test_orphan_beleg_survives_roundtrip(tmp_path):
    """Standalone Belege (no Buchung references them) stay in
    bs.belege.data through save → load."""
    pdf_referenced = _make_pdf(str(tmp_path), name="ref.pdf")
    pdf_orphan = _make_pdf(str(tmp_path), name="orph.pdf", content=b"orphan")

    bs = _new_stapel()
    entry = bs.add_buchung(
        umsatz=1.0, soll_haben="S", konto="3333", gegenkonto="1111",
        belegdatum=datetime.date(2025, 2, 1))
    entry["Beleg"] = pdf_referenced
    # Add an orphan directly through the archive
    bs.belege.add(pydatev.Beleg(pdf_orphan))

    csv = str(tmp_path / "EXTF_t.csv")
    bs.save(csv)
    bs2 = pydatev.Buchungsstapel(filename=csv)
    names = sorted(d.filename for d in bs2.belege.data)
    assert names == ["orph.pdf", "ref.pdf"]
    # Find the orphan: its GUID is not referenced by any entry's link
    linked = {bs2.data[0]["Beleglink"].split('"')[-2]
              if '"' in bs2.data[0]["Beleglink"]
              else bs2.data[0]["Beleglink"].split()[-1]}
    orphan = [b for b in bs2.belege.data if b.guid not in linked]
    assert len(orphan) == 1
    assert orphan[0].blob == b"orphan"


def test_beleg_rejects_unknown_extension(tmp_path):
    """Beleg() refuses .exe (and other non-DATEV types) on construction.
    Belegarchiv.load() does NOT validate — roundtrip-safe.
    """
    bad = tmp_path / "evil.exe"
    bad.write_bytes(b"MZ\x00\x00")
    with pytest.raises(pydatev.DatevFormatError):
        pydatev.Beleg(str(bad))

    # Manually craft a Belegarchiv ZIP with an .exe entry; load must
    # NOT raise.
    zip_path = tmp_path / "manipulated.zip"
    import uuid as _uuid
    guid = str(_uuid.uuid4())
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<archive xmlns="http://xml.datev.de/bedi/tps/document/v04.0" '
        'version="4.0" generatingSystem="test">\n'
        ' <header><date>2025-01-01T00:00:00</date>'
        '<description>test</description></header>\n'
        ' <content>\n'
        '  <document processID="1" guid="' + guid + '">\n'
        '   <extension xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:type="File" name="evil.exe"/>\n'
        '  </document>\n'
        ' </content>\n'
        '</archive>\n'
    ).encode("utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("document.xml", doc_xml)
        zf.writestr("evil.exe", b"MZ\x00\x00")
    loaded = pydatev.Belegarchiv(filename=str(zip_path))
    assert len(loaded.data) == 1
    assert loaded.data[0].filename == "evil.exe"


def test_backward_compat_no_belege_no_zip(tmp_path):
    """A Buchungsstapel without any Beleg touch must produce only the
    CSV (no belege.zip alongside), preserving pre-Beleg behavior.
    Also: the CSV content must be identical to a stapel created the
    same way and saved before the Belege extension existed — proxy:
    saving twice in a row produces the same bytes (deterministic +
    free of Beleg-side-effects in the CSV).
    """
    bs = _new_stapel()
    bs.add_buchung(
        umsatz=34.56, soll_haben="S", konto="3333", gegenkonto="1111",
        belegdatum=datetime.date(2025, 2, 1))
    bs.add_buchung(
        umsatz=3.66, soll_haben="S", konto="4683", gegenkonto="9632",
        belegdatum=datetime.date(2025, 2, 3))

    csv_a = tmp_path / "EXTF_a.csv"
    csv_b = tmp_path / "EXTF_b.csv"
    bs.save(str(csv_a))
    bs.save(str(csv_b))

    assert csv_a.exists() and csv_b.exists()
    assert not (tmp_path / "belege.zip").exists(), \
        "belege.zip must NOT be written when no Belege are attached"
    # Determinism between two saves with no Belege
    assert csv_a.read_bytes() == csv_b.read_bytes()


def test_iterate_contains_remove_keeps_xml_and_zip_consistent(tmp_path):
    """Container protocol + remove(). After removing one Beleg,
    save() must produce a ZIP where document.xml and the binary
    payload agree — no dangling <document> entry and no leftover
    file in the ZIP for the removed Beleg.
    """
    pdf_a = _make_pdf(str(tmp_path), name="a.pdf", content=b"AAA")
    pdf_b = _make_pdf(str(tmp_path), name="b.pdf", content=b"BBB")
    arch = pydatev.Belegarchiv(description="t", generating_system="test")
    ba = arch.add(pydatev.Beleg(pdf_a))
    bb = arch.add(pydatev.Beleg(pdf_b))

    # Container protocol
    assert len(arch) == 2
    assert ba.guid in arch
    assert bb in arch
    names = sorted(b.filename for b in arch)
    assert names == ["a.pdf", "b.pdf"]

    # Remove by guid string
    removed = arch.remove(ba.guid)
    assert removed is ba
    assert len(arch) == 1
    assert ba.guid not in arch
    assert arch.get_by_guid(ba.guid) is None

    # Save → load: document.xml and ZIP must be consistent
    zpath = str(tmp_path / "out.zip")
    arch.save(zpath)
    with zipfile.ZipFile(zpath) as zf:
        zip_names = set(zf.namelist())
        xml_bytes = zf.read("document.xml")
    assert zip_names == {"document.xml", "b.pdf"}
    root = ET.fromstring(xml_bytes)
    ns_uri = root.tag.split("}", 1)[0].lstrip("{")
    doc_filenames = {
        d.find("{{{}}}extension".format(ns_uri)).attrib["name"]
        for d in root.findall(
            "{{{0}}}content/{{{0}}}document".format(ns_uri))}
    assert doc_filenames == {"b.pdf"}, (
        "document.xml must list exactly the files present in the ZIP")

    # Remove by Beleg object reference also works
    arch.remove(bb)
    assert len(arch) == 0

    # Removing an unknown guid raises
    with pytest.raises(KeyError):
        arch.remove("00000000-0000-0000-0000-000000000000")


def test_getitem_and_clear(tmp_path):
    """archive[guid] returns the Beleg or raises KeyError; clear()
    empties both the data list and the internal index together."""
    pdf = _make_pdf(str(tmp_path))
    arch = pydatev.Belegarchiv(description="t")
    b = arch.add(pydatev.Beleg(pdf))

    assert arch[b.guid] is b
    with pytest.raises(KeyError):
        arch["00000000-0000-0000-0000-000000000000"]

    arch.clear()
    assert len(arch) == 0
    assert arch.get_by_guid(b.guid) is None


def test_belegarchiv_standalone_v060(tmp_path):
    """v06.0 schema works for standalone Belegarchiv save/load."""
    pdf = _make_pdf(str(tmp_path))
    arch = pydatev.Belegarchiv(
        description="test", generating_system="pytest",
        schema_version="v06.0",
    )
    arch.add(pydatev.Beleg(pdf, belegtyp=pydatev.BELEGTYP_RECHNUNGSAUSGANG))
    zpath = str(tmp_path / "belege.zip")
    arch.save(zpath)

    with zipfile.ZipFile(zpath) as zf:
        root = ET.fromstring(zf.read("document.xml"))
    ns_uri = root.tag.split("}", 1)[0].lstrip("{")
    assert ns_uri == "http://xml.datev.de/bedi/tps/document/v06.0"

    arch2 = pydatev.Belegarchiv(filename=zpath)
    assert arch2.schema_version == "v06.0"
    assert len(arch2.data) == 1
    assert arch2.data[0].belegtyp == pydatev.BELEGTYP_RECHNUNGSAUSGANG
