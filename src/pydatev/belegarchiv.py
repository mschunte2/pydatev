# -*- coding: utf-8 -*-
#
# pydatev.belegarchiv — DATEV Belegarchiv (Document Package) support
# layered on top of pydatev core.
#
# Users who only need plain Buchungsstapel CSV handling get the
# upstream surface via `import pydatev`. Users who want Belege opt
# in via `from pydatev.belegarchiv import Buchungsstapel, Beleg, ...`
# and receive a Buchungsstapel subclass that auto-writes belege.zip
# on save() and auto-loads it on load(), plus the field-like
# `entry["Beleg"] = path` API.

import datetime
import hashlib
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile

from .pydatev import (
    Buchungsstapel as _CoreBuchungsstapel,
    DatevEntry,
    DatevFormatError,
    specifications,
)


# DATEV-accepted file types for Belege. The authoritative list is
# maintained by DATEV in Hilfe-Center document 1000312, "Zulässige
# Dateiformate für die Übertragung digitaler Belege". That document
# is only reachable for authenticated DATEV customers via the
# Hilfe-Center search, so no public URL is cited here.
SUPPORTED_BELEG_EXTENSIONS = frozenset({
    "pdf",
    "jpg", "jpeg", "png", "tif", "tiff", "bmp", "gif",
    "doc", "docx", "xls", "xlsx", "odt", "ods",
    "txt", "rtf", "csv",
    "msg", "xml",
})

# Belegtyp IDs per Document_types_v040.xsd / Document_types_v060.xsd
BELEGTYP_RECHNUNGSEINGANG = "1"
BELEGTYP_RECHNUNGSAUSGANG = "2"

_BELEG_GUID_NAMESPACE = uuid.UUID("5d8e7f10-3b9a-4d2e-b8a6-9c1f7e0d4321")
_BELEG_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _beleg_uuid8(name: str, blob: bytes) -> str:
    '''Derive a deterministic UUIDv8 (RFC 9562, "custom") for a Beleg
    from its archive name and content bytes. The full SHA-256 of
    (namespace || name_utf8 || 0x00 || blob) is truncated to 128
    bits, then the version (8) and variant (RFC 4122) bits are set
    per RFC 9562 §5.8.

    Used instead of uuid5() because uuid5() is fixed to SHA-1
    internally, and SHA-1 has had practical collision attacks since
    2017 (SHAttered) / 2020 (chosen-prefix). For this use case
    (non-adversarial document identification) the 128-bit output
    bottleneck dominates either way, but SHA-256 in the chain is the
    forward-looking choice.

    Filenames cannot contain NUL on any real OS, so the 0x00
    separator unambiguously delimits name from blob.
    '''
    h = hashlib.sha256()
    h.update(_BELEG_GUID_NAMESPACE.bytes)
    h.update(name.encode("utf-8"))
    h.update(b"\0")
    h.update(blob)
    b = bytearray(h.digest()[:16])
    b[6] = (b[6] & 0x0F) | 0x80  # version 8
    b[8] = (b[8] & 0x3F) | 0x80  # RFC 4122/9562 variant
    return str(uuid.UUID(bytes=bytes(b)))


class Beleg:
    '''A single documentation file (Beleg) plus the metadata that
    document.xml needs to describe it: guid, archive filename, blob,
    belegtyp. Constructed from a path on disk; the file content is
    read into self.blob.
    '''
    __slots__ = ("guid", "filename", "blob", "belegtyp")

    def __init__(self, filepath=None, belegtyp=None, guid=None,
                 archive_name=None, _raw=None):
        '''Create a Beleg from a file on disk, or (internal use) from
        a pre-built dict produced by Belegarchiv.load().

        Parameters
        ----------
        filepath:     str or os.PathLike, the file to read
        belegtyp:     BELEGTYP_RECHNUNGSEINGANG | BELEGTYP_RECHNUNGSAUSGANG | None
        guid:         optional 36-char UUID; if omitted, derived as
                      a deterministic UUIDv8 (RFC 9562) from
                      SHA-256(namespace || archive_name || 0x00 ||
                      blob) — see _beleg_uuid8. Same file content
                      under the same archive name always yields the
                      same GUID, regardless of disk path. Same name
                      with different content → different GUIDs (no
                      silent filename collisions). Different name
                      with same content → different GUIDs (preserves
                      business-identity-via-filename, e.g. two empty
                      placeholder Belege filed under distinct names).
        archive_name: optional name to store under in the archive;
                      defaults to os.path.basename(filepath)
        '''
        if _raw is not None:
            self.guid = _raw["guid"]
            self.filename = _raw["filename"]
            self.blob = _raw["blob"]
            self.belegtyp = _raw["belegtyp"]
            return
        filepath = os.fspath(filepath)
        name = archive_name or os.path.basename(filepath)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in SUPPORTED_BELEG_EXTENSIONS:
            raise DatevFormatError(
                "Extension .{} of {!r} is not in the DATEV allowlist {}"
                .format(ext, name, sorted(SUPPORTED_BELEG_EXTENSIONS)))
        if belegtyp not in (None, BELEGTYP_RECHNUNGSEINGANG,
                            BELEGTYP_RECHNUNGSAUSGANG):
            raise DatevFormatError(
                "belegtyp must be '1', '2', or None; got {!r}"
                .format(belegtyp))
        with open(filepath, "rb") as f:
            blob = f.read()
        if guid is None:
            guid = _beleg_uuid8(name, blob)
        if not _BELEG_GUID_RE.fullmatch(guid):
            raise DatevFormatError(
                "guid {!r} must be a 36-char UUID".format(guid))
        self.blob = blob
        self.guid = guid
        self.filename = name
        self.belegtyp = belegtyp

    def write_to(self, target_dir):
        '''Write this Beleg back to disk under target_dir using its
        archive filename. Returns the path written.'''
        target = os.path.join(target_dir, self.filename)
        with open(target, "wb") as f:
            f.write(self.blob)
        return target


class Belegarchiv:
    '''DATEV Belegarchiv (Document Package) — file manager for a
    collection of Belege. Writes them as a ZIP with a `document.xml`
    manifest; reads the same back.

    Dedup is by Beleg.guid: add() ignores a Beleg whose guid is
    already present and returns the existing one. Schema versions
    v04.0 (BuchhaltungsButler's round-trip variant; default) and
    v06.0 are supported. load() trusts existing archives (no
    extension check) so roundtrip is bit-stable.
    '''

    _NAMESPACES = {
        "v04.0": "http://xml.datev.de/bedi/tps/document/v04.0",
        "v06.0": "http://xml.datev.de/bedi/tps/document/v06.0",
    }
    _XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

    def __init__(self, filename=None, description="",
                 generating_system="pydatev",
                 schema_version="v04.0", created=None):
        self.data = []
        self._by_guid = {}
        if filename is not None:
            self.load(filename)
            return
        if schema_version not in self._NAMESPACES:
            raise DatevFormatError(
                "Unsupported schema_version {!r}".format(schema_version))
        self.schema_version = schema_version
        self.description = description
        self.generating_system = generating_system
        self.created = created or datetime.datetime.now()

    def add(self, beleg):
        '''Add a Beleg. If one with the same guid is already present,
        returns the existing entry (idempotent).'''
        existing = self._by_guid.get(beleg.guid)
        if existing is not None:
            return existing
        self.data.append(beleg)
        self._by_guid[beleg.guid] = beleg
        return beleg

    def remove(self, guid_or_beleg):
        '''Remove a Beleg by guid (str) or by reference (Beleg).
        Returns the removed Beleg. Raises KeyError if no matching
        Beleg exists.

        Both `document.xml` and the ZIP payload are rebuilt from
        self.data on each save(), so removing here keeps the two
        in sync automatically.

        Caveat: if this archive is owned by a Buchungsstapel and any
        entry still has `Beleglink = BEDI "<removed-guid>"`, that
        reference becomes a dangling link. Clear it explicitly with
        `entry['Beleg'] = None` on each affected entry, or let the
        caller manage the relationship.
        '''
        guid = (guid_or_beleg.guid
                if isinstance(guid_or_beleg, Beleg)
                else guid_or_beleg)
        beleg = self._by_guid.pop(guid, None)
        if beleg is None:
            raise KeyError(guid)
        self.data.remove(beleg)
        return beleg

    def get_by_guid(self, guid):
        '''Lookup by guid. Returns None if absent.'''
        return self._by_guid.get(guid)

    def clear(self):
        '''Remove every Beleg from the archive. Cheaper and safer than
        reaching into self.data/self._by_guid directly.'''
        self.data.clear()
        self._by_guid.clear()

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def __contains__(self, key):
        '''Membership: accepts a guid string OR a Beleg instance.'''
        if isinstance(key, Beleg):
            return key.guid in self._by_guid
        return key in self._by_guid

    def __getitem__(self, guid):
        '''Indexed access by guid. Raises KeyError if absent (use
        get_by_guid() for None-on-miss semantics).'''
        beleg = self._by_guid.get(guid)
        if beleg is None:
            raise KeyError(guid)
        return beleg

    def save(self, filename):
        '''Write the archive as a ZIP. Raises if empty (DATEV rejects
        empty archives).'''
        if not self.data:
            raise DatevFormatError("Cannot save an empty Belegarchiv.")
        manifest = self._build_document_xml()
        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("document.xml", manifest)
            for d in self.data:
                zf.writestr(d.filename, d.blob)

    def load(self, filename):
        '''Read a Belegarchiv ZIP into self.data. Trusts the source —
        no extension check, no validation. Required for roundtrip
        stability.'''
        with zipfile.ZipFile(filename) as zf:
            raw_list = self._parse_document_xml(zf.read("document.xml"))
            self.data = []
            self._by_guid = {}
            for raw in raw_list:
                raw["blob"] = zf.read(raw["filename"])
                beleg = Beleg(_raw=raw)
                self.data.append(beleg)
                self._by_guid[beleg.guid] = beleg

    def _build_document_xml(self):
        ns = self._NAMESPACES[self.schema_version]
        ET.register_namespace("", ns)
        ET.register_namespace("xsi", self._XSI_NS)
        ver = "4.0" if self.schema_version == "v04.0" else "6.0"
        # schema_version "v04.0" → "document_v040.xsd"
        # schema_version "v06.0" → "document_v060.xsd"
        xsd = "document_{}.xsd".format(
            self.schema_version.replace(".", ""))
        archive = ET.Element(ET.QName(ns, "archive"), attrib={
            ET.QName(self._XSI_NS, "schemaLocation"):
                "{} {}".format(ns, xsd),
            "version": ver,
            "generatingSystem": self.generating_system,
        })
        header = ET.SubElement(archive, ET.QName(ns, "header"))
        ET.SubElement(header, ET.QName(ns, "date")).text = \
            self.created.strftime("%Y-%m-%dT%H:%M:%S")
        ET.SubElement(header, ET.QName(ns, "description")).text = \
            self.description
        content = ET.SubElement(archive, ET.QName(ns, "content"))
        for d in self.data:
            attrs = {"processID": "1", "guid": d.guid}
            if d.belegtyp is not None:
                attrs["type"] = d.belegtyp
            doc_el = ET.SubElement(
                content, ET.QName(ns, "document"), attrib=attrs)
            ET.SubElement(doc_el, ET.QName(ns, "extension"), attrib={
                ET.QName(self._XSI_NS, "type"): "File",
                "name": d.filename,
            })
        ET.indent(archive, space=" ")
        return ET.tostring(archive, encoding="UTF-8", xml_declaration=True)

    def _parse_document_xml(self, xml_bytes):
        root = ET.fromstring(xml_bytes)
        ns_uri = root.tag.split("}", 1)[0].lstrip("{")
        matches = [label for label, uri in self._NAMESPACES.items()
                   if uri == ns_uri]
        if not matches:
            raise DatevFormatError(
                "Unsupported document.xml namespace {!r}".format(ns_uri))
        self.schema_version = matches[0]
        self.generating_system = root.attrib.get("generatingSystem", "")
        self.description = ""
        self.created = datetime.datetime.now()
        header = root.find("{{{}}}header".format(ns_uri))
        if header is not None:
            d = header.find("{{{}}}date".format(ns_uri))
            if d is not None and d.text:
                self.created = datetime.datetime.fromisoformat(d.text)
            desc = header.find("{{{}}}description".format(ns_uri))
            if desc is not None and desc.text:
                self.description = desc.text
        out = []
        for doc_el in root.findall(
                "{{{0}}}content/{{{0}}}document".format(ns_uri)):
            ext = doc_el.find("{{{}}}extension".format(ns_uri))
            out.append({
                "guid": doc_el.attrib["guid"],
                "filename":
                    ext.attrib["name"] if ext is not None else "",
                "blob": b"",
                "belegtyp": doc_el.attrib.get("type"),
            })
        return out


class Buchungsentry(DatevEntry):
    '''A Buchungsstapel row that also accepts the pseudo-fields
    'Beleg' and 'Belegtyp'. These don't serialize into the CSV
    themselves — they delegate to the parent Buchungsstapel's
    Belegarchiv and to the row's 'Beleglink' field (which IS a CSV
    column).
    '''
    def __init__(self, fields, parent):
        super().__init__(fields)
        self._parent = parent
        self._pending_belegtyp = None

    def __setitem__(self, key, value):
        if key == "Beleg":
            if value is None:
                super().__setitem__("Beleglink", None)
                return
            beleg = value if isinstance(value, Beleg) else Beleg(value)
            beleg = self._parent.belege.add(beleg)  # dedup by guid
            # If Belegtyp was set first, apply it now.
            if self._pending_belegtyp is not None:
                beleg.belegtyp = self._pending_belegtyp
                self._pending_belegtyp = None
            super().__setitem__(
                "Beleglink", 'BEDI "{}"'.format(beleg.guid))
            return
        if key == "Belegtyp":
            beleg = self["Beleg"]
            if beleg is not None:
                beleg.belegtyp = value
            else:
                # Beleg not attached yet — remember and apply later.
                self._pending_belegtyp = value
            return
        super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "Beleg":
            link = self.data.get("Beleglink") or ""
            # pydatev's Text parser un-doubles internal quotes on
            # load, so 'BEDI "<UUID>"' round-trips intact. Extract
            # the GUID via regex either way.
            match = _BELEG_GUID_RE.search(link)
            if match is None:
                return None
            return self._parent.belege.get_by_guid(match.group(0))
        if key == "Belegtyp":
            beleg = self["Beleg"]
            return beleg.belegtyp if beleg else None
        return super().__getitem__(key)


class Buchungsstapel(_CoreBuchungsstapel):
    '''Buchungsstapel + attached Belege. Drop-in replacement for
    `pydatev.Buchungsstapel`: import this one and the row API gains
    the `entry["Beleg"] = path` field-like attachment, while
    `save()` writes `belege.zip` next to the CSV (if any Belege were
    attached) and `load()` reads it back if present.

    Empty-Belege case behaves identically to the core class — no zip
    on disk, no behavior change.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If filename= was passed, super().__init__ called self.load()
        # before .belege existed; backfill an empty archive and let the
        # caller re-load if they want Belege populated.
        if not hasattr(self, "belege"):
            self.belege = Belegarchiv(
                description="", generating_system="pydatev")

    def add_entry(self):
        # Mirror DatevDataCategory.add_entry's body but construct a
        # Buchungsentry (parent=self) instead of a plain DatevEntry,
        # so the field-like Beleg API dispatches correctly.
        fields = specifications[self._metadata['Formatname']][
            str(self._metadata['Formatversion'])]['Field']
        new_entry = Buchungsentry(fields, parent=self)
        self._data.append(new_entry)
        new_entry['WKZ Umsatz'] = self._metadata['Währungskennzeichen']
        return new_entry

    def save(self, filename):
        '''Save the CSV; additionally write a `belege.zip` next to it
        if `self.belege.data` is non-empty.'''
        super().save(filename)
        if self.belege.data:
            zip_path = os.path.join(
                os.path.dirname(filename) or ".", "belege.zip")
            self.belege.save(zip_path)

    def load(self, filename):
        '''Load the CSV; additionally read a `belege.zip` from the
        same directory if present. Missing `belege.zip` is silent.'''
        # super().__init__ may call load() before our __init__ has run
        # — make sure the archive exists.
        if not hasattr(self, "belege"):
            self.belege = Belegarchiv(
                description="", generating_system="pydatev")
        super().load(filename)
        zip_path = os.path.join(
            os.path.dirname(filename) or ".", "belege.zip")
        if os.path.exists(zip_path):
            self.belege.load(zip_path)
