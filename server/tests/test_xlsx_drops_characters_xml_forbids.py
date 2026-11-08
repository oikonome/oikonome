"""A spreadsheet download must stay well-formed XML whatever text a cell
carries. Text from outside the household can hold characters XML 1.0
forbids (control characters, the noncharacters U+FFFE and U+FFFF, lone
surrogates); one such cell would make a reader refuse or empty the whole
workbook, so they are dropped while ordinary text, tabs, newlines and
characters beyond the basic plane survive."""

import io
import unittest
import zipfile
from xml.dom import minidom

from oikonome.web import xlsx

BAD = "A￾B￿C\x01D\ud800E"
GOOD = "tab\there\nline é 😀"


def _sheet(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("xl/worksheets/sheet1.xml").decode()


def _texts(xml: str) -> list[str]:
    doc = minidom.parseString(xml)
    return ["".join(n.data for n in t.childNodes)
            for t in doc.getElementsByTagName("t")]


class XlsxDropsForbiddenCharacters(unittest.TestCase):
    def test_workbook_cell_with_forbidden_characters_parses(self):
        texts = _texts(_sheet(xlsx.workbook("T", ["payee"], [[BAD], [GOOD]])))
        self.assertIn("ABCDE", texts)
        self.assertIn(GOOD, texts)

    def test_streamed_workbook_with_forbidden_characters_parses(self):
        data = b"".join(xlsx.stream_workbook("T", ["payee"], [[[BAD]], [[GOOD]]]))
        texts = _texts(_sheet(data))
        self.assertIn("ABCDE", texts)
        self.assertIn(GOOD, texts)


if __name__ == "__main__":
    unittest.main()
