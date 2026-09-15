"""Inventory: user-supplied XML never reaches stdlib ElementTree.

`xml.etree.ElementTree` is safe against XXE — it refuses external entities,
so no file read and no SSRF. What it does NOT limit is INTERNAL entity
expansion. The classic billion-laughs payload is a few hundred bytes of
nested `<!ENTITY>` definitions that expand to gigabytes during parse:

    <!ENTITY lol "lol">
    <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
    ... nine levels ...

`engine/taxdocs.parse_ssa_xml` parses a file the user uploads through
POST /api/import/taxdoc/analyze, so on the hosted instance any signed-up
tenant could hand the shared container an out-of-memory kill — taking down
every other tenant on that host with them. defusedxml refuses DTD entities
outright, which is the whole fix.

Third-party parsers are in scope too: OFX 2 is XML and ofxtools' OFXTree
subclasses ElementTree. Its own tree builder is regex-based and does not
expand entities today, but that is an implementation detail we do not
lean on — the importer refuses any DTD before the library sees the bytes,
and the OFX 2 bomb below pins that.

The import assertion is the durable half. The next XML parser someone adds
will be written by reaching for the obvious stdlib import, and this fails
the moment they do.
"""

import pathlib
import re
import unittest

from oikonome.engine import taxdocs
from oikonome.sync import ofximport

SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 9 levels of tenfold expansion: ~450 bytes in, ~1 GB out, if anything expands
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
 <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
 <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
 <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<lolz>&lol9;</lolz>"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<foo>&xxe;</foo>"""

# OFX 2 is XML, and ofxtools' OFXTree SUBCLASSES ElementTree. The bank-file
# importer is therefore the other place user-supplied XML enters. The
# entity bomb rides inside a data element so an expanding parser would
# blow up building the tree, not while validating tags.
_OFX2_HEAD = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
              b'<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" '
              b'OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n')
_OFX2_DTD = b"""<!DOCTYPE OFX [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
 <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
 <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
 <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
"""


def _ofx2_body(name: bytes) -> bytes:
    return (b"<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE>"
            b"<SEVERITY>INFO</SEVERITY></STATUS><DTSERVER>20240101</DTSERVER>"
            b"<LANGUAGE>ENG</LANGUAGE></SONRS></SIGNONMSGSRSV1>"
            b"<BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID><STATUS><CODE>0</CODE>"
            b"<SEVERITY>INFO</SEVERITY></STATUS><STMTRS><CURDEF>USD</CURDEF>"
            b"<BANKACCTFROM><BANKID>1</BANKID><ACCTID>1234</ACCTID>"
            b"<ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>"
            b"<BANKTRANLIST><DTSTART>20240101</DTSTART><DTEND>20240131</DTEND>"
            b"<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20240102</DTPOSTED>"
            b"<TRNAMT>-5.00</TRNAMT><FITID>a1</FITID><NAME>" + name +
            b"</NAME></STMTTRN></BANKTRANLIST><LEDGERBAL><BALAMT>1</BALAMT>"
            b"<DTASOF>20240131</DTASOF></LEDGERBAL></STMTRS></STMTTRNRS>"
            b"</BANKMSGSRSV1></OFX>")


OFX2_BILLION_LAUGHS = _OFX2_HEAD + _OFX2_DTD + _ofx2_body(b"&lol9;")

# `import xml.etree...` / `from xml.etree import ...` / `from xml.dom import ...`
STDLIB_XML_IMPORT = re.compile(
    r"^\s*(?:import\s+xml\.|from\s+xml\.(?:etree|dom|sax)\b)", re.MULTILINE)


class XmlSafetyTests(unittest.TestCase):

    def test_entity_expansion_is_refused(self):
        """The bomb must not expand. A plain `assertRaises` is the whole
        test: if this regresses, the process dies here rather than in
        production, which is the trade we want."""
        with self.assertRaises(Exception) as caught:
            taxdocs.parse_ssa_xml(BILLION_LAUGHS)
        self.assertIn("Entities", type(caught.exception).__name__,
                      f"expected defusedxml's EntitiesForbidden, got "
                      f"{type(caught.exception).__name__} — is taxdocs "
                      "back on stdlib ElementTree?")

    def test_external_entities_are_refused(self):
        with self.assertRaises(Exception):
            taxdocs.parse_ssa_xml(XXE)

    def test_ordinary_xml_still_parses(self):
        """The guard must not have been bought by breaking the feature."""
        rows = taxdocs.parse_ssa_xml(
            b'<?xml version="1.0"?><osss:earnings xmlns:osss="http://x">'
            b'<osss:Earnings startYear="2020">'
            b'<osss:FicaEarnings>50000</osss:FicaEarnings>'
            b'<osss:MedicareEarnings>50000</osss:MedicareEarnings>'
            b'</osss:Earnings></osss:earnings>')
        self.assertEqual(1, len(rows))
        self.assertEqual(2020, rows[0]["year"])

    def test_ofx2_entity_bomb_is_refused_before_parsing(self):
        """A DTD in an OFX file is refused outright — no bank statement
        carries one, and the refusal must not depend on how the third-party
        parser happens to treat entities this release. Bounded so a
        regression to an expanding parser fails the test instead of the
        host: a 2 s alarm, and no row may come back a megabyte wide."""
        import signal

        def _boom(*_a):
            raise TimeoutError("OFX parse did not return — expanding?")
        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(2)
        try:
            with self.assertRaises(ValueError) as caught:
                rows = ofximport.parse_ofx(OFX2_BILLION_LAUGHS)
                self.fail(f"parsed instead of refusing; name width "
                          f"{len(rows[0]['name']) if rows else 0}")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertIn("DTD", str(caught.exception))
        # case and padding do not slip past the check
        with self.assertRaises(ValueError):
            ofximport.parse_ofx(_OFX2_HEAD + b" " * 20000
                                + _OFX2_DTD.replace(b"!DOCTYPE", b"!doctype")
                                + _ofx2_body(b"x"))

    def test_ordinary_ofx2_still_parses(self):
        rows = ofximport.parse_ofx(_OFX2_HEAD + _ofx2_body(b"SAFEWAY"))
        self.assertEqual(1, len(rows))
        self.assertEqual("SAFEWAY", rows[0]["name"])

    def test_no_module_imports_stdlib_xml(self):
        """Catches the NEXT parser, not this one."""
        offenders = []
        for path in sorted((SERVER_ROOT / "oikonome").rglob("*.py")):
            if STDLIB_XML_IMPORT.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(SERVER_ROOT)))
        self.assertEqual(
            [], offenders,
            f"{offenders} import stdlib XML. Every XML input this app sees "
            "is user-supplied (uploaded documents, aggregator payloads), and "
            "stdlib parsers expand internal entities without limit — a small "
            "file becomes an out-of-memory kill on a container shared by "
            "every tenant. Use `from defusedxml import ElementTree as ET`.")
