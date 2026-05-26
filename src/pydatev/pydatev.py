# -*- coding: utf-8 -*-
#
# A python module to import and export DATEV files.
# Author: Frank Stollmeier
# License: GNU GPLv3
#

import hashlib
import os
import re
import uuid
import zipfile
import datetime
import xml.etree.ElementTree as ET
from collections import UserDict
import pickle
import pkg_resources
try:
    import pandas as pd
except ImportError:
    pass

class DatevFormatError(ValueError):
    '''Error for everything that conflicts with the DATEV file format specifications.'''
    pass

with pkg_resources.resource_stream(__name__, "format-specifications.dat") as f:
    specifications = pickle.load(f)


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


class DatevEntry(UserDict):
    '''A generic class for entries that are part of one of the data categories. The classes for entries of a specific data category should inherit from this class.
    An instance of this class behaves almost like a dictionary, but instead of arbitrary keys, only specific keys are allowed, and instead of arbitrary datatypes for the values, only specific datatypes are allowed.'''

    def __init__(self, fields):
        super().__init__()
        self._fields = fields
        self._labels = [f['Label'] for f in fields]
        self._aliases = dict([(f['LabelAlias'],f['Label']) for f in fields if ('LabelAlias' in f and not f['LabelAlias'] is None)])
        self._fields_dict = dict([(field['Label'],field) for field in fields])
        for field in self._fields:
            self[field['Label']] = None
        
    def __setitem__(self, key, value):
        #check if key is valid
        if key in self._aliases:
            key = self._aliases[key]
        if not key in self._labels:
            raise KeyError("Adding new keys is not allowed.")
        #check if datatype of value is valid
        format_type = self._fields_dict[key]['FormatType']
        decimal_places = int(self._fields_dict[key]['DecimalPlaces'])
        if not value is None:
            if format_type == 'Betrag':
                if not isinstance(value, float):
                    raise DatevFormatError("The value for key '{}' needs to be of type float.".format(key))
            elif format_type == 'Datum':
                if not type(value) is datetime.date: #Check with type(), because isinstance() would also accept datetime.datetime.
                    raise DatevFormatError("The value for key '{}' needs to be of type datetime.date.".format(key))
            elif format_type == 'Datum JJJJMMTT':
                if not type(value) is datetime.date: #Check with type(), because isinstance() would also accept datetime.datetime.
                    raise DatevFormatError("The value for key '{}' needs to be of type datetime.date.".format(key))
            elif format_type == 'Konto':
                if not isinstance(value, str):
                    raise DatevFormatError("The value for key '{}' needs to be of type str.".format(key))
                if not value.isdigit():
                    raise DatevFormatError("The value for key '{}' needs to be a string of digits.".format(key))
            elif format_type == 'Text':
                if not isinstance(value, str):
                    raise DatevFormatError("The value for key '{}' needs to be of type str.".format(key))
                # Quotation marks are legal inside DATEV Text fields
                # (e.g. column "Beleglink" expects `BEDI "<UUID>"`).
                # They are CSV-escaped by python2datev() via the
                # standard doubled-quote convention.
            elif format_type == 'Zahl' and decimal_places == 0:
                if not isinstance(value, int):
                    raise DatevFormatError("The value for key '{}' needs to be of type int.".format(key))
            elif format_type == 'Zahl' and decimal_places > 0:
                if not isinstance(value, float):
                    raise DatevFormatError("The value for key '{}' needs to be of type float.".format(key))
            elif format_type == 'Zeitstempel':
                if not isinstance(value, datetime.datetime):
                    raise DatevFormatError("The value for key '{}' needs to be of type datetime.datetime.".format(key))
            else:
                raise NotImplementedError("Unknown FormatType: {}".format(format_type))
        
        #set value
        super().__setitem__(key, value)
    
    def __str__(self):
        '''Show the date of the entry that is set, but not the fields that are set to None.'''
        s = '{'
        for label in self._labels:
            if self[label] is None:
                continue
            s += "{}: {}, ".format(label, self[label])
        s = s[:-2] + '}'
        return s
    
    def verify(self):
        '''Check whether all required fields are filled.'''
        missing = []
        for field in self._fields:
            if int(field['Necessary']) == 1:
                if self[field['Label']] is None:
                    missing.append(field['Label'])
        if len(missing) > 0:
            raise DatevFormatError('The following necessary values are missing: ' + str(missing))
        return True
    
    def python2datev(self, key):
        '''Return value in datev format.'''
        value = self[key]
        format_type = self._fields_dict[key]['FormatType']
        length = -1 if self._fields_dict[key]['Length'] is None else int(self._fields_dict[key]['Length'])
        decimal_places = int(self._fields_dict[key]['DecimalPlaces'])
        max_length = length + 1 + decimal_places if format_type in ['Betrag','Zahl'] else length
        
        if value is None:
            if format_type == 'Text':
                s = '""'
            else:
                s = ''
        else:
            if format_type == 'Betrag':
                s = '{:.{}f}'.format(value, decimal_places).replace('.',',')
            elif format_type == 'Datum':
                if length == 4:
                    s = "{:0>2d}".format(value.day) + "{:0>2d}".format(value.month)
                elif length == 8:
                    s = "{:0>2d}".format(value.day) + "{:0>2d}".format(value.month) + "{:0>4d}".format(value.year) 
                else:
                    raise NotImplementedError("Unknown date format.")
            elif format_type == 'Datum JJJJMMTT':
                s = "{:0>4d}".format(value.year) + "{:0>2d}".format(value.month) + "{:0>2d}".format(value.day)
                
            elif format_type == 'Konto':
                s = value
                
            elif format_type == 'Text':
                 s = '"' + value.replace('"', '""') + '"'
                
            elif format_type == 'Zahl':
                s =  '{:.{}f}'.format(value, decimal_places).replace('.',',')
            
            elif format_type == 'Zeitstempel':
                s = "{:0>4d}{:0>2d}{:0>2d}{:0>2d}{:0>2d}{:0>2d}{:0>3d}".format(value.year,value.month,value.day,value.hour,value.minute,value.second,value.microsecond)
            
            else:
                raise NotImplementedError("Unknown FormatType: {}".format(format_type))
        
        if length > 0:
            if len(s.replace('"','')) > max_length:
                raise DatevFormatError("The value {} has {} characters, but the DATEV file specification allows only {} characters for values at key {}.".format(s, len(s), max_length, key))
        
        return s

    def serialize(self):
        '''Convert data to a string as it is represented in a DATEV file. For the inverse operation, see self.parse().'''
        parts = [self.python2datev(field['Label']) for field in self._fields]
        return ';'.join(parts)
    
    def datev2python(self, key, string, year = None):
        '''Parse a string containing a single datum from a DATEV-file and save the content as a python datatype in self[key].
        
        Parameters
        ----------
        key:    str, the field identifier
        string: str, a single datum from a DATEV file  
        year:   int, only required if the string contains a date
        '''
        format_type = self._fields_dict[key]['FormatType']
        length = -1 if self._fields_dict[key]['Length'] is None else int(self._fields_dict[key]['Length'])
        decimal_places = int(self._fields_dict[key]['DecimalPlaces'])
        max_length = length + 1 + decimal_places if format_type in ['Betrag','Zahl'] else length
        
        if len(string) == 0 or string == '""':
            value = None
        elif format_type == 'Betrag':
            value = float(string.replace(',', '.')) 
        elif format_type == 'Datum':
            if length == 4 and ('FormatExpression' in self._fields_dict[key]) and (self._fields_dict[key]['FormatExpression'] == 'TTMM'):
                value = datetime.date(year, int(string[2:4]), int(string[0:2]))
            elif length == 8 and len(string) == 8:
                value = datetime.date(int(string[4:8]), int(string[2:4]), int(string[0:2]))  
            else:
                raise NotImplementedError("Unknown date format.")
        elif format_type == 'Datum JJJJMMTT':
            value = datetime.date(int(string[0:4]), int(string[4:6]), int(string[6:8]))  
        elif format_type == 'Konto':
            value = string
        elif format_type == 'Text':
            # Inverse of python2datev: strip the outer wrapping quotes
            # and un-double any embedded quotes (CSV-style escape).
            value = string[1:-1].replace('""', '"')
        elif format_type == 'Zahl':
            if decimal_places == 0:
                value = int(string)
            elif decimal_places > 0:
                value = float(string.replace(',', '.')) 
        elif format_type == 'Zeitstempel':
            t = string
            value = datetime.datetime(int(t[:4]),int(t[4:6]),int(t[6:8]),int(t[8:10]),int(t[10:12]),int(t[12:14]),int(t[14:17]))
        self[key] = value
    
    def parse(self, line, year = None):
        '''Read a string of one line from a DATEV file, convert the content to python datatypes and store the results. For the inverse operation, see self.serialize().
        
        Parameters
        ----------
        line:   str
        year:   int
        '''
        values = line.split(';')
        if len(values) < len(self._labels):
            raise IOError("Unable to parse line: " + line)
        elif len(values) > len(self._labels):
            ignore = values[len(self._labels):]
            values = values[:len(self._labels)]
            print("Warning: A line in the datev file has more columns than expected. The following columns are ignored: " + str(ignore))
        for field,value in zip(self._fields, values):
            self.datev2python(field['Label'], value, year)
    
    @property
    def required_keys(self):
        return [field['Label'] for field in self._fields if int(field['Necessary']) == 1]



class DatevDataCategory(object):
    '''This is the base class for Datev data categories. Each data category should inherit from this class.''' 
    
    def __init__(self):
        self._metadata = DatevEntry(specifications['Metadaten']['Andere']['Field'])
        self._data = []
        
    def load(self, filename):
        '''Load a datev file.
        
        Parameters
        ----------
        filename:       string
        '''
        with open(filename, 'r', encoding = 'ISO-8859-1') as f:
            content = f.read().splitlines()
        header_line = content[0]
        column_line = content[1]
        entry_lines = content[2:]
        
        self._metadata.parse(header_line)
        
        if not self._metadata['Formatname'] in specifications:
            raise ValueError(f"The category_type {self._metadata['Formatname']} is not supported.")
        if not str(self._metadata['Formatversion']) in specifications[self._metadata['Formatname']]:
            raise ValueError(f"The Format version {self._metadata['Formatversion']} is not supported.")
        
        self.parse_data(column_line, entry_lines)
        
    def save(self, filename):
        '''Save data to a Datev file. The Datev file specification require that the filename has the format EXTF_<arbitrary-name>.csv, e.g. EXTF_Buchungsstapel__<date_time>_<export number>.csv .
        
        Parameters
        ----------
        filename:    string
        '''
        fn = os.path.split(filename)[1]
        if not fn[:5] == 'EXTF_' or not os.path.splitext(fn)[1] == '.csv':
            raise DatevFormatError("The Datev file specification require that the filename has the format EXTF_<arbitrary-name>.csv, e.g. EXTF_Buchungsstapel__<date_time>_<export number>.csv .")
        with open(filename, 'w', encoding = 'ISO-8859-1') as f:
            f.write(self._metadata.serialize() + '\n')
            f.write(self.serialize_data())
    
    @property
    def data(self):
        return self._data
    
    @property
    def metadata(self):
        return self._metadata
    
    def add_entry(self):
        new_entry = DatevEntry(specifications[self._metadata['Formatname']][str(self._metadata['Formatversion'])]['Field'])
        self._data.append(new_entry)
        return new_entry
    
    def parse_data(self, column_line, entry_lines):
        '''Parse the body of a datev file. 
        
        Parameters
        ----------
        column_line:    string
        entry_lines:    list of strings
        '''
        for line in entry_lines:
            new_entry = self.add_entry()
            new_entry.parse(line, year = self._metadata['Wirtschaftsjahr-Beginn'].year)
            
    
    def serialize_data(self):
        '''Serialize the data of the body of a datev file. 
        '''
        lines = []
        #header
        first_entry = self._data[0]
        lines.append(';'.join(first_entry.keys())) 
        #body
        for entry in self._data:
            lines.append(entry.serialize())
        return '\n'.join(lines)
    
    def export_as_pandas_dataframe(self):
        '''Return data as a pandas DataFrame.'''
        data = []
        for entry in self._data:
            e = []
            for key in entry.keys():
                e.append(entry[key])
            data.append(e)
        try:   
            return pd.DataFrame(data, columns = self._data[0].keys())
        except NameError:
            raise RuntimeError("You need to install the python module 'pandas' to use this function.")
    
    def verify(self):
        '''Check wheter metadata and all entries are valid.'''
        errors = []
        if not self._metadata['DATEV-Format-KZ'] in ['DTVF','EXTF']:
            errors.append("Metadata: DATEV-Format-KZ needs to be either DTVF or EXTF, but not " + str(self._metadata['DATEV-Format-KZ']))
        try:
            self._metadata.verify()
        except DatevFormatError as dfe:
            errors.append("Metadata: ", dfe.args[0])
        for i,entry in enumerate(self._data):
            try:
                entry.verify()
            except DatevFormatError as dfe:
                errors.append("Entry {}: {}".format(i,dfe.args[0]))
        if len(errors) == 0:
            return True
        else:
            raise DatevFormatError("Invalid data.", errors)
        


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
        guid:         optional 36-char UUID; auto-derived from
                      (archive_name, sha256(blob)) via uuid5 if
                      omitted. Same file content under the same
                      archive name always yields the same GUID,
                      regardless of disk path. Same name with
                      different content → different GUIDs (no silent
                      filename collisions). Different name with same
                      content → different GUIDs (preserves
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
            # Derive from (archive_name, sha256(blob)) so re-exporting
            # the same byte-stable file under the same name yields the
            # same GUID (needed by downstream auto-attach flows, e.g.
            # BuchhaltungsButler), while filename collisions over
            # different content no longer silently dedup. The NUL
            # separator removes any (name, hash) parse ambiguity.
            digest = hashlib.sha256(blob).hexdigest()
            guid = str(uuid.uuid5(_BELEG_GUID_NAMESPACE,
                                  name + "\0" + digest))
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
            # pydatev's Text parser strips quotes on load, so 'BEDI
            # "<UUID>"' may arrive as 'BEDI <UUID>'. Extract the GUID
            # via regex to tolerate both forms.
            match = _BELEG_GUID_RE.search(link)
            if match is None:
                return None
            return self._parent.belege.get_by_guid(match.group(0))
        if key == "Belegtyp":
            beleg = self["Beleg"]
            return beleg.belegtyp if beleg else None
        return super().__getitem__(key)


class Buchungsstapel(DatevDataCategory):
    '''Datev Buchungsstapel'''

    def __init__(self, filename = None, berater = None, mandant = None, wirtschaftsjahr_beginn = None, sachkontennummernlänge = None, datum_von = None, datum_bis = None, waehrungskennzeichen = None, version = 9):
        '''If you specify the filename, the data will be loaded from there and the other parameters of this functions are ignored. If you don't specify the filename, a new empty Buchungsstapel will be created using the metadata of the other parameters.

        Parameters
        ----------
        filename:               str
        berater:                int
        mandant:                int
        wirtschaftsjahr_beginn: datetime.date
        sachkontennummernlänge: int
        datum_von:              datetime.date
        datum_bis:              datetime.date
        waehrungskennzeichen:   str, optional, but recommended
        version:                int, optional

        Notes
        -----
        The Buchungsstapel owns a `Belegarchiv` accessible at
        `self.belege`. If a `belege.zip` lives next to the CSV being
        loaded, it is read into `self.belege` automatically; on save,
        a `belege.zip` is written next to the CSV if (and only if)
        `self.belege.data` is non-empty. Buchungsstapel instances
        with no Belege touched behave identically to pre-Beleg pydatev
        — no zip on disk, no behavior change.
        '''
        super().__init__()
        self.belege = Belegarchiv(
            description="", generating_system="pydatev")
        if filename is None:
            if not wirtschaftsjahr_beginn <= datum_von < datum_bis < datetime.date(wirtschaftsjahr_beginn.year+1,wirtschaftsjahr_beginn.month,wirtschaftsjahr_beginn.day):
                raise DatevFormatError("The dates datum_von and datum_bis should be between wirtschaftsjahr_beginn and wirtschaftsjahr_beginn + 1 year.")  
            if not 4 <= sachkontennummernlänge <= 9:
                raise DatevFormatError("The sachkontennummernlänge needs to be between 4 and 9.")
            if not 1 <= mandant <= 99999:
                raise DatevFormatError("The mandant number needs to be between 1 and 99999.")
            if not 1001 <= berater <= 9999999:
                raise DatevFormatError("The berater number needs to be between 1001 and 9999999.")
            self._metadata['DATEV-Format-KZ'] = 'EXTF'
            self._metadata['Versionsnummer'] = 700
            self._metadata['Datenkategorie'] = 21
            self._metadata['Formatname'] = 'Buchungsstapel'
            self._metadata['Formatversion'] = version
            self._metadata['Berater'] = berater
            self._metadata['Mandant'] = mandant
            self._metadata['Wirtschaftsjahr-Beginn'] = wirtschaftsjahr_beginn
            self._metadata['Sachkontennummernlänge'] = sachkontennummernlänge
            self._metadata['Datum von'] = datum_von
            self._metadata['Datum bis'] = datum_bis
            self._metadata['Währungskennzeichen'] = waehrungskennzeichen
        else:
            self.load(filename)
    
    def add_entry(self):
        # Override to construct a Buchungsentry (which extends
        # DatevEntry with the 'Beleg'/'Belegtyp' pseudo-fields).
        # Mirrors DatevDataCategory.add_entry's body but uses our
        # subclass and passes us as the parent.
        fields = specifications[self._metadata['Formatname']][
            str(self._metadata['Formatversion'])]['Field']
        new_entry = Buchungsentry(fields, parent=self)
        self._data.append(new_entry)
        new_entry['WKZ Umsatz'] = self._metadata['Währungskennzeichen']
        return new_entry

    def save(self, filename):
        '''Save the CSV; additionally write a `belege.zip` next to it
        if `self.belege.data` is non-empty. Empty-Belege case writes
        only the CSV — identical to pre-Beleg behavior.'''
        super().save(filename)
        if self.belege is not None and self.belege.data:
            zip_path = os.path.join(
                os.path.dirname(filename) or ".", "belege.zip")
            self.belege.save(zip_path)

    def load(self, filename):
        '''Load the CSV; additionally read a `belege.zip` from the
        same directory if present. Missing `belege.zip` is silent —
        identical to pre-Beleg behavior.'''
        super().load(filename)
        zip_path = os.path.join(
            os.path.dirname(filename) or ".", "belege.zip")
        if os.path.exists(zip_path):
            self.belege.load(zip_path)

    def add_buchung(self, umsatz = None, soll_haben = None, konto = None, gegenkonto = None, belegdatum = None):
        '''Add Buchung to the batch. All parameters are optional, but required to make the entry valid. If not specified, the entry will be created, but the required fields need to be filled later.'''
        if len(self._data) == 99999:
            raise DatevFormatError("Datev file specification doesn't allow more than 99999 entries.")
        entry = self.add_entry()
        entry['Umsatz (ohne Soll/Haben-Kz)'] = umsatz
        entry['Soll/Haben-Kennzeichen'] = soll_haben
        entry['Kontonummer'] = konto
        entry['Gegenkonto (ohne BU-Schlüssel)'] = gegenkonto
        entry['Belegdatum'] = belegdatum
        return entry
    
    def verify(self):
        '''Check if all file format specifications are satisfied.'''
        errors = [] 
        try:
            super().verify()
        except DatevFormatError as dfe:
            errors.extend(dfe.args[1])
        for i,entry in enumerate(self._data):
            if not self._metadata['Datum von'] <= entry['Belegdatum'] <= self._metadata['Datum bis']:
                errors.append("The <Belegdatum> of Buchung {} is outside the specified time frame of this Buchungsstapel (from {} to {}).".format(i,str(self._metadata['Datum von']),str(self._metadata['Datum bis'])))
        if len(errors) > 0:
            raise DatevFormatError("Invalid data.", errors)
        else:
            return True




