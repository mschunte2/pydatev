"""Pin the boundary between core pydatev and the opt-in
`pydatev.belegarchiv` submodule. If anyone re-leaks Beleg names back
into the core namespace, or breaks the subclass relationship, these
tests fail loudly."""

import datetime
import subprocess
import sys

import pydatev
from pydatev import belegarchiv as pydatev_be


_BELEGE_NAMES = (
    "Beleg",
    "Belegarchiv",
    "Buchungsentry",
    "BELEGTYP_RECHNUNGSEINGANG",
    "BELEGTYP_RECHNUNGSAUSGANG",
    "SUPPORTED_BELEG_EXTENSIONS",
    "_beleg_uuid8",
)


def test_core_pydatev_exposes_no_beleg_names():
    leaked = [name for name in _BELEGE_NAMES if hasattr(pydatev, name)]
    assert not leaked, (
        "core pydatev unexpectedly exposes Beleg-related names: "
        f"{leaked}. They belong in pydatev.belegarchiv only."
    )


def test_belegarchiv_exposes_expected_names():
    for name in _BELEGE_NAMES:
        assert hasattr(pydatev_be, name), (
            f"pydatev.belegarchiv is missing expected export {name!r}"
        )


def test_belegarchiv_buchungsstapel_subclasses_core():
    assert issubclass(pydatev_be.Buchungsstapel, pydatev.Buchungsstapel), (
        "pydatev.belegarchiv.Buchungsstapel must subclass the core "
        "pydatev.Buchungsstapel so it's a drop-in replacement."
    )


def test_core_buchungsstapel_has_no_belege_attr():
    """A user who does `import pydatev` and constructs a stapel must
    get the upstream surface — no .belege attribute, no belege.zip
    side-effects on save()."""
    bs = pydatev.Buchungsstapel(
        berater=1001, mandant=1,
        wirtschaftsjahr_beginn=datetime.date(2025, 1, 1),
        sachkontennummernlänge=4,
        datum_von=datetime.date(2025, 1, 1),
        datum_bis=datetime.date(2025, 12, 31),
        waehrungskennzeichen="EUR",
    )
    assert not hasattr(bs, "belege"), (
        "core pydatev.Buchungsstapel must not own a Belegarchiv. "
        "If you need Belege, use pydatev.belegarchiv.Buchungsstapel."
    )


def test_belegarchiv_buchungsstapel_has_belege_attr():
    bs = pydatev_be.Buchungsstapel(
        berater=1001, mandant=1,
        wirtschaftsjahr_beginn=datetime.date(2025, 1, 1),
        sachkontennummernlänge=4,
        datum_von=datetime.date(2025, 1, 1),
        datum_bis=datetime.date(2025, 12, 31),
        waehrungskennzeichen="EUR",
    )
    assert isinstance(bs.belege, pydatev_be.Belegarchiv)


def test_dotted_import_does_not_leak_beleg_names_into_core():
    """`import pydatev.belegarchiv` (the dotted form) imports the
    submodule and, as a side effect, makes `pydatev` itself available
    — but it must NOT pull any Beleg names up into the core
    `pydatev.*` namespace. Run in a subprocess to get a clean
    interpreter state regardless of what previous tests imported."""
    names_list = ", ".join(repr(n) for n in _BELEGE_NAMES)
    code = (
        "import pydatev.belegarchiv\n"
        "import pydatev\n"
        f"names = [{names_list}]\n"
        "leaked = [n for n in names if hasattr(pydatev, n)]\n"
        "assert not leaked, "
        "f'dotted import leaked into core: {leaked}'\n"
        "assert pydatev.belegarchiv.Buchungsstapel is not None\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip().endswith("OK"), result.stdout
